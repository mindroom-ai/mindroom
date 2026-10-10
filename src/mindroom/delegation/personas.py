"""Agent-authored subagent personas and the workspace profile files that store them.

A persona replaces only a child's presentation, its system prompt and visible
tools; the child keeps the caller's own principal, so it never reaches more
than the caller can.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, cast

from mindroom.agent_cli.shell_contract import SHELL_OPERATION_NAMES
from mindroom.delegation.state import SubagentPersona
from mindroom.path_confinement import open_directory_within_root, read_regular_file_within_root
from mindroom.tool_system.catalog import TOOL_METADATA
from mindroom.tool_system.declarations import ToolAuthoredOverrideValidator
from mindroom.tool_system.dynamic_toolkits import visible_tool_surface
from mindroom.tool_system.skills import SkillMarkdownError, parse_skill_markdown, workspace_entry_names

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Mapping, Sequence
    from pathlib import Path

    from agno.tools.function import Function

    from mindroom.agent_modes import AgentMode
    from mindroom.config.main import Config
    from mindroom.delegation.state import PersonaSourceKind

_MAX_PERSONA_PROMPT_BYTES = 64 << 10
_PROFILE_DIRNAME = "subagents"
_PROFILE_SUFFIX = ".md"
_MAX_PROFILE_FILE_BYTES = 64 << 10
_MAX_PROFILES = 256
_MAX_LISTED_PROFILE_BYTES = 1 << 20
_MAX_DESCRIPTION_CHARS = 1024
_MAX_LISTING_CHARS = 2000
_PROFILE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_PROFILE_KEYS = frozenset({"description", "tools", "model", "mode"})
_MODES: tuple[AgentMode, ...] = ("standard", "minimal")
_NAME_RULE = "profile names use lowercase letters, digits, '-', and '_', at most 64 characters"


class PersonaError(ValueError):
    """A persona or profile that cannot be used; the message is user-facing."""


@dataclass(frozen=True)
class _PersonaProfile:
    """One valid ``subagents/<name>.md`` file."""

    name: str
    description: str
    persona: SubagentPersona
    model: str | None
    mode: AgentMode | None


@dataclass(frozen=True)
class _InvalidPersonaProfile:
    """One profile file the agent must fix before it can run."""

    name: str
    reason: str


def _system_prompt(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        msg = "Cannot delegate: system_prompt must be a non-empty string."
        raise PersonaError(msg)
    if len(value.encode()) > _MAX_PERSONA_PROMPT_BYTES:
        msg = "Cannot delegate: system_prompt exceeds 64 KiB."
        raise PersonaError(msg)
    return value


def _tool_entries(value: object) -> tuple[str, ...] | None:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or not all(isinstance(tool, str) and tool.strip() for tool in value):
        msg = "Cannot delegate: tools must be a list of tool names."
        raise PersonaError(msg)
    return tuple(dict.fromkeys(cast("str", tool).strip() for tool in value))


def inline_persona(
    system_prompt: object,
    tools: object,
    *,
    source_kind: PersonaSourceKind = "inline",
    source_name: str = "",
) -> SubagentPersona:
    """Validate an authored prompt and optional tool list into a persona."""
    return SubagentPersona(
        source_kind=source_kind,
        source_name=source_name,
        system_prompt=_system_prompt(system_prompt),
        tools=_tool_entries(tools),
    )


def _parse_profile(name: str, content: str) -> _PersonaProfile:
    """Parse one profile file; ``PersonaError`` carries the reason it is invalid."""
    try:
        frontmatter, body = parse_skill_markdown(content)
    except SkillMarkdownError as exc:
        msg = "it must start with a YAML mapping between --- lines"
        raise PersonaError(msg) from exc
    unknown = sorted(map(str, set(frontmatter) - _PROFILE_KEYS))
    if unknown:
        msg = f"unsupported frontmatter keys: {', '.join(unknown)}"
        raise PersonaError(msg)
    description = frontmatter.get("description")
    if not isinstance(description, str) or not description.strip():
        msg = "description is required"
        raise PersonaError(msg)
    if len(description) > _MAX_DESCRIPTION_CHARS:
        msg = f"description exceeds {_MAX_DESCRIPTION_CHARS} characters"
        raise PersonaError(msg)
    model = frontmatter.get("model")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        msg = "model must be a model name"
        raise PersonaError(msg)
    mode = frontmatter.get("mode")
    if mode is not None and mode not in _MODES:
        msg = "mode must be standard or minimal"
        raise PersonaError(msg)
    if not body:
        msg = "the body after the frontmatter is the system prompt and must not be empty"
        raise PersonaError(msg)
    try:
        persona = inline_persona(body, frontmatter.get("tools"), source_kind="profile", source_name=name)
    except PersonaError as exc:
        msg = str(exc).removeprefix("Cannot delegate: ").removesuffix(".")
        raise PersonaError(msg) from exc
    return _PersonaProfile(
        name=name,
        description=description.strip(),
        persona=persona,
        model=model.strip() if isinstance(model, str) else None,
        mode=cast("AgentMode | None", mode),
    )


def _read_profile(  # noqa: PLR0911 - each refusal is its own reason
    directory_fd: int,
    name: str,
) -> tuple[_PersonaProfile | _InvalidPersonaProfile | None, int]:
    """Read one profile below its pinned directory with the bytes read; None when the file is absent."""
    if not _PROFILE_NAME.fullmatch(name):
        return _InvalidPersonaProfile(name=name, reason=_NAME_RULE), 0
    try:
        data = read_regular_file_within_root(
            directory_fd,
            f"{name}{_PROFILE_SUFFIX}",
            max_bytes=_MAX_PROFILE_FILE_BYTES,
        )
    except FileNotFoundError:
        return None, 0
    except ValueError:
        return _InvalidPersonaProfile(name=name, reason="the file exceeds 64 KiB or is not a regular file"), 0
    except OSError:
        return _InvalidPersonaProfile(name=name, reason="the file cannot be read as a regular file"), 0
    try:
        return _parse_profile(name, data.decode()), len(data)
    except UnicodeDecodeError:
        return _InvalidPersonaProfile(name=name, reason="the file is not UTF-8 text"), len(data)
    except PersonaError as exc:
        return _InvalidPersonaProfile(name=name, reason=str(exc)), len(data)


def list_profiles(workspace_root: Path) -> list[_PersonaProfile | _InvalidPersonaProfile]:
    """Read up to 256 profiles and 1 MiB from ``subagents/``, sorted by name, never following links.

    Profiles past either limit are left out of the listing but still load by name.
    """
    entries: list[_PersonaProfile | _InvalidPersonaProfile] = []
    remaining = _MAX_LISTED_PROFILE_BYTES
    try:
        with open_directory_within_root(workspace_root, _PROFILE_DIRNAME) as directory_fd:
            names = [
                entry.removesuffix(_PROFILE_SUFFIX)
                for entry in workspace_entry_names(directory_fd, directories=False).names
                if entry.endswith(_PROFILE_SUFFIX)
            ]
            for name in names[:_MAX_PROFILES]:
                entry, size = _read_profile(directory_fd, name)
                remaining -= size
                if remaining < 0:
                    break
                if entry is not None:
                    entries.append(entry)
    except OSError:
        return []
    return entries


def load_profile(workspace_root: Path | None, name: str) -> _PersonaProfile:
    """Read one named profile, raising ``PersonaError`` with the user-facing reason."""
    if workspace_root is None:
        msg = "Cannot delegate: subagent profiles need an agent workspace."
        raise PersonaError(msg)
    if not _PROFILE_NAME.fullmatch(name):
        msg = f"Cannot delegate: {_NAME_RULE}."
        raise PersonaError(msg)
    not_found = f"Cannot delegate: subagent profile '{name}' was not found in {_PROFILE_DIRNAME}/."
    try:
        with open_directory_within_root(workspace_root, _PROFILE_DIRNAME) as directory_fd:
            entry, _size = _read_profile(directory_fd, name)
    except OSError:
        entry = None
    if entry is None:
        raise PersonaError(not_found)
    if isinstance(entry, _InvalidPersonaProfile):
        msg = f"Cannot delegate: subagent profile '{name}' is invalid: {entry.reason}."
        raise PersonaError(msg)
    return entry


def render_profile_listing(entries: Sequence[_PersonaProfile | _InvalidPersonaProfile]) -> str:
    """Render profiles for the `run_subagent` description, bounded to 2,000 characters."""
    lines = [
        f"- {entry.name}: {entry.description}"
        if isinstance(entry, _PersonaProfile)
        else f"- {entry.name} (invalid: {entry.reason})"
        for entry in entries
    ]
    listing = "\n".join(lines)
    if len(listing) <= _MAX_LISTING_CHARS:
        return listing
    return f"{len(entries)} subagent profiles are saved in {_PROFILE_DIRNAME}/; list that directory to see them."


def caller_toolkit_names(agent_name: str, config: Config, *, delegation_depth: int) -> list[str]:
    """Return every toolkit this agent may use, including deferred toolkits it loads on demand.

    Matrix room tools are listed too; a child naming one outside a Matrix room refuses to start.
    The deferred-tool manager is not, since an explicit tool list loads every toolkit it names.
    """
    if agent_name not in config.agents:
        # A reload can remove the caller mid-response; its copies then have no tools to inherit.
        return []
    deferred = [entry.name for entry in config.resolve_entity(agent_name).authored_deferred_tool_configs]
    surface = visible_tool_surface(
        agent_name=agent_name,
        config=config,
        loaded_tools=deferred,
        delegation_depth=delegation_depth,
        enable_dynamic_tools_manager=False,
        include_matrix_room_runtime_tools=True,
    )
    # A preset has no functions of its own; its member toolkits are listed by their own names.
    return [entry.name for entry in surface.runtime_tool_configs if not config.is_tool_preset(entry.name)]


def declared_function_names(toolkit: str) -> tuple[str, ...] | None:
    """Return the functions a toolkit declares up front, or None when they are known only once it is built.

    An MCP toolkit discovers its functions from its server, so its metadata may list only bridge functions.
    """
    metadata = TOOL_METADATA.get(toolkit)
    if (
        metadata is None
        or not metadata.function_names
        or metadata.authored_override_validator == ToolAuthoredOverrideValidator.MCP
    ):
        return None
    return metadata.function_names


def persona_allows(entries: tuple[str, ...], toolkit: str, function: str) -> bool:
    """Return whether persona entries keep one function of one concrete toolkit."""
    return toolkit in entries or f"{toolkit}.{function}" in entries


def missing_persona_tool(
    tools: tuple[str, ...] | None,
    available_toolkits: Sequence[str],
    cap: tuple[str, ...] | None = None,
) -> str | None:
    """Return the first entry naming neither a caller toolkit nor a known function of one, or outside ``cap``."""
    available = set(available_toolkits)
    for entry in tools or ():
        toolkit, separator, function = entry.partition(".")
        declared = declared_function_names(toolkit)
        known = toolkit in available and (
            not separator or (bool(function) and (declared is None or function in declared))
        )
        if not known or (cap is not None and entry not in cap and toolkit not in cap):
            return entry
    return None


def validate_persona_tools(
    tools: tuple[str, ...] | None,
    available_toolkits: Sequence[str],
    cap: tuple[str, ...] | None = None,
) -> None:
    """Require every entry to name one of the caller's toolkits, or a known function of one, within ``cap``."""
    entry = missing_persona_tool(tools, available_toolkits, cap)
    if entry is not None:
        yours = cap if cap is not None else sorted(set(available_toolkits))
        msg = f"Cannot delegate: unknown tool '{entry}'. Your tools: {', '.join(yours) or 'none'}."
        raise PersonaError(msg)


def require_built_persona_tools(
    tools: tuple[str, ...],
    built: Mapping[str, Collection[str]],
    hidden: Mapping[str, Collection[str]],
) -> None:
    """Refuse to start an authored child without every tool it names, whatever removed it.

    ``built`` maps each toolkit the child built to its functions after the caller's filters, and
    ``hidden`` the functions the session's MCP collision projection removed from them.
    """
    for entry in tools:
        toolkit, separator, function = entry.partition(".")
        if toolkit not in built or (
            separator and (function not in built[toolkit] or function in hidden.get(toolkit, ()))
        ):
            msg = f"Cannot delegate: tool '{entry}' is not available to you."
            raise PersonaError(msg)


def follow_up_refusal(
    persona: SubagentPersona | None,
    agent_name: str,
    config: Config,
    *,
    delegation_depth: int,
) -> str | None:
    """Refuse a follow-up of a workflow participant, or once the caller lost a tool its subagent names."""
    if persona is not None and persona.source_kind == "workflow":
        # A participant's tools are usable only under its workflow's pre-approval.
        return "Subagent belongs to a Dynamic Workflow run and cannot be continued outside it; start a new subagent."
    if persona is None or persona.tools is None:
        return None
    available = caller_toolkit_names(agent_name, config, delegation_depth=delegation_depth)
    missing = missing_persona_tool(persona.tools, available)
    return (
        None if missing is None else f"Subagent tool '{missing}' is no longer available to you; start a new subagent."
    )


def self_only_refusal(agent_name: str) -> str:
    """Explain that a caller may author only its own subagents."""
    return f"Cannot author a subagent for '{agent_name}': system_prompt, tools, and profile apply only to yourself."


@dataclass(frozen=True)
class PersonaRequest:
    """The persona, model, and mode one ``run_subagent`` call asks for."""

    persona: SubagentPersona | None
    model: str | None
    agent_mode: AgentMode


def resolve_persona_request(  # noqa: PLR0911
    *,
    caller_name: str,
    agent_name: str,
    system_prompt: object,
    tools: object,
    profile: object,
    model: str | None,
    minimal: bool,
    workspace_root: Path | None,
    available_toolkits: Callable[[], Sequence[str]],
    cap: tuple[str, ...] | None = None,
) -> PersonaRequest | str:
    """Resolve ``run_subagent`` authoring arguments, returning a user-facing refusal when they are invalid.

    ``cap`` holds the tools of the authored subagent making this call, if any: copies it
    authors stay within them, and it cannot start an unauthored copy with every caller tool.
    """
    mode: AgentMode = "minimal" if minimal else "standard"
    # Some models fill every optional argument; empty values author nothing.
    system_prompt = system_prompt or None
    profile = profile or None
    if system_prompt is None and not tools:
        tools = None
    if system_prompt is None and tools is None and profile is None:
        if cap is not None and agent_name == caller_name:
            return (
                f"Cannot start a configured copy of '{agent_name}' from an authored subagent; "
                "pass system_prompt or profile so the copy stays within your tools."
            )
        return PersonaRequest(persona=None, model=model, agent_mode=mode)
    if agent_name != caller_name:
        return self_only_refusal(agent_name)
    if profile is not None and (system_prompt is not None or tools is not None):
        return "Cannot delegate: pass either profile or system_prompt and tools, not both."
    try:
        if profile is None:
            persona = inline_persona(system_prompt, tools)
        elif not isinstance(profile, str):
            return "Cannot delegate: profile must be a profile name."
        else:
            loaded = load_profile(workspace_root, profile)
            persona, model = loaded.persona, model or loaded.model
            mode = "minimal" if minimal else loaded.mode or "standard"
        persona = _capped_persona(persona, available_toolkits(), cap)
        require_minimal_shell(persona, mode)
    except PersonaError as exc:
        return str(exc)
    return PersonaRequest(persona=persona, model=model, agent_mode=mode)


def require_minimal_shell(persona: SubagentPersona, mode: AgentMode) -> None:
    """Refuse a minimal persona whose tool list drops shell, which minimal mode runs through."""
    tools = persona.tools
    if mode != "minimal" or tools is None:
        return
    if "shell" in tools or all(f"shell.{name}" in tools for name in SHELL_OPERATION_NAMES):
        return
    msg = "Cannot delegate: a minimal subagent needs shell among its tools."
    raise PersonaError(msg)


def _capped_persona(
    persona: SubagentPersona,
    available_toolkits: Sequence[str],
    cap: tuple[str, ...] | None,
) -> SubagentPersona:
    """Validate a persona's tools against the caller and ``cap``; a persona without tools inherits ``cap``.

    ``available_toolkits`` lists the caller's toolkits at the copy's depth, so a copy at the maximum
    depth inherits ``cap`` without ``delegate``; any other inherited tool the caller lost still refuses.
    """
    if persona.tools is None and cap is not None:
        keep_delegate = "delegate" in available_toolkits
        tools = tuple(entry for entry in cap if keep_delegate or entry.partition(".")[0] != "delegate")
        persona = replace(persona, tools=tools)
    validate_persona_tools(persona.tools, available_toolkits, cap)
    return persona


def persona_tool_policy(
    persona_tools: tuple[str, ...] | None,
    available_toolkits: Callable[[], Sequence[str]],
    tool_function_filter: Callable[[Function], bool] | None,
    disabled_tool_names: frozenset[str],
) -> tuple[Callable[[Function], bool] | None, frozenset[str]]:
    """Narrow an agent's own tools to an authored persona's explicit list; its principal is unchanged.

    Toolkits the list never names are not built, nor is the deferred-tool manager, since a persona
    loads every toolkit it names. Generated functions such as skills and knowledge search stay hidden;
    toolkit functions are narrowed by concrete toolkit name while the toolkits are built.
    """
    if persona_tools is None:
        return tool_function_filter, disabled_tool_names
    named = {entry.partition(".")[0] for entry in persona_tools}
    unused = {toolkit for toolkit in available_toolkits() if toolkit not in named}

    def visible(function: Function) -> bool:
        return function.owning_toolkit is not None and (tool_function_filter is None or tool_function_filter(function))

    return visible, disabled_tool_names | unused | {"dynamic_tools"}
