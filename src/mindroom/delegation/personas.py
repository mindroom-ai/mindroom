"""Agent-authored subagent personas and the workspace profile files that store them.

A persona replaces only a child's presentation, its system prompt and visible
tools; the child keeps the caller's own principal, so it never reaches more
than the caller can.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from mindroom.delegation.state import SubagentPersona
from mindroom.path_confinement import open_directory_within_root, read_regular_file_within_root
from mindroom.tool_system.catalog import TOOL_METADATA
from mindroom.tool_system.dynamic_toolkits import visible_tool_surface
from mindroom.tool_system.skills import SkillMarkdownError, parse_skill_markdown, workspace_entry_names

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
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
    unknown = sorted(set(frontmatter) - _PROFILE_KEYS)
    if unknown:
        msg = f"unsupported frontmatter keys: {', '.join(map(str, unknown))}"
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


def _profile_text(directory_fd: int, name: str) -> str:
    """Read one profile file below its pinned directory, letting FileNotFoundError mean absent."""
    try:
        data = read_regular_file_within_root(
            directory_fd,
            f"{name}{_PROFILE_SUFFIX}",
            max_bytes=_MAX_PROFILE_FILE_BYTES,
        )
    except FileNotFoundError:
        raise
    except ValueError as exc:
        msg = "the file exceeds 64 KiB or is not a regular file"
        raise PersonaError(msg) from exc
    except OSError as exc:
        msg = "the file cannot be read as a regular file"
        raise PersonaError(msg) from exc
    try:
        return data.decode()
    except UnicodeDecodeError as exc:
        msg = "the file is not UTF-8 text"
        raise PersonaError(msg) from exc


def _read_profile(directory_fd: int, name: str) -> _PersonaProfile | _InvalidPersonaProfile | None:
    """Read one profile below its pinned directory; None when the file is absent."""
    if not _PROFILE_NAME.fullmatch(name):
        return _InvalidPersonaProfile(name=name, reason=_NAME_RULE)
    try:
        return _parse_profile(name, _profile_text(directory_fd, name))
    except FileNotFoundError:
        return None
    except PersonaError as exc:
        return _InvalidPersonaProfile(name=name, reason=str(exc))


def list_profiles(workspace_root: Path) -> list[_PersonaProfile | _InvalidPersonaProfile]:
    """Read up to 256 profiles from ``subagents/``, sorted by name, never following links."""
    try:
        with open_directory_within_root(workspace_root, _PROFILE_DIRNAME) as directory_fd:
            names = [
                entry.removesuffix(_PROFILE_SUFFIX)
                for entry in workspace_entry_names(directory_fd, directories=False).names
                if entry.endswith(_PROFILE_SUFFIX)
            ]
            entries = [_read_profile(directory_fd, name) for name in names[:_MAX_PROFILES]]
            return [entry for entry in entries if entry is not None]
    except OSError:
        return []


def load_profile(workspace_root: Path, name: str) -> _PersonaProfile:
    """Read one named profile, raising ``PersonaError`` with the user-facing reason."""
    if not _PROFILE_NAME.fullmatch(name):
        msg = f"Cannot delegate: {_NAME_RULE}."
        raise PersonaError(msg)
    not_found = f"Cannot delegate: subagent profile '{name}' was not found in {_PROFILE_DIRNAME}/."
    try:
        with open_directory_within_root(workspace_root, _PROFILE_DIRNAME) as directory_fd:
            entry = _read_profile(directory_fd, name)
    except OSError:
        entry = None
    if entry is None:
        raise PersonaError(not_found)
    if isinstance(entry, _InvalidPersonaProfile):
        msg = f"Cannot delegate: subagent profile '{name}' is invalid: {entry.reason}."
        raise PersonaError(msg)
    return entry


def render_profile_listing(entries: Sequence[_PersonaProfile | _InvalidPersonaProfile]) -> str:
    """Render profiles for the delegate instructions, bounded to 2,000 characters."""
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
    """Return every toolkit this agent may use, including deferred toolkits it loads on demand."""
    deferred = [entry.name for entry in config.resolve_entity(agent_name).authored_deferred_tool_configs]
    surface = visible_tool_surface(
        agent_name=agent_name,
        config=config,
        loaded_tools=deferred,
        delegation_depth=delegation_depth,
        enable_dynamic_tools_manager=True,
    )
    return [entry.name for entry in surface.runtime_tool_configs]


def missing_persona_tool(tools: tuple[str, ...] | None, available_toolkits: Sequence[str]) -> str | None:
    """Return the first entry naming neither a caller toolkit nor a known function of one."""
    available = set(available_toolkits)
    for entry in tools or ():
        toolkit, separator, function = entry.partition(".")
        metadata = TOOL_METADATA.get(toolkit)
        known = toolkit in available and (
            not separator
            or (
                bool(function)
                and (metadata is None or not metadata.function_names or function in metadata.function_names)
            )
        )
        if not known:
            return entry
    return None


def validate_persona_tools(tools: tuple[str, ...] | None, available_toolkits: Sequence[str]) -> None:
    """Require every entry to name one of the caller's toolkits, or a known function of one."""
    entry = missing_persona_tool(tools, available_toolkits)
    if entry is not None:
        msg = f"Cannot delegate: unknown tool '{entry}'. Your tools: {', '.join(sorted(set(available_toolkits)))}."
        raise PersonaError(msg)


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
    available_toolkits: Sequence[str],
) -> PersonaRequest | str:
    """Resolve ``run_subagent`` authoring arguments, returning a user-facing refusal when they are invalid."""
    mode: AgentMode = "minimal" if minimal else "standard"
    if system_prompt is None and tools is None and profile is None:
        return PersonaRequest(persona=None, model=model, agent_mode=mode)
    if agent_name != caller_name:
        return self_only_refusal(agent_name)
    if profile is not None and (system_prompt is not None or tools is not None):
        return "Cannot delegate: pass either profile or system_prompt and tools, not both."
    try:
        if profile is None:
            persona = inline_persona(system_prompt, tools)
            validate_persona_tools(persona.tools, available_toolkits)
            return PersonaRequest(persona=persona, model=model, agent_mode=mode)
        if not isinstance(profile, str):
            return "Cannot delegate: profile must be a profile name."
        if workspace_root is None:
            return "Cannot delegate: subagent profiles need an agent workspace."
        loaded = load_profile(workspace_root, profile)
        validate_persona_tools(loaded.persona.tools, available_toolkits)
    except PersonaError as exc:
        return str(exc)
    return PersonaRequest(
        persona=loaded.persona,
        model=model or loaded.model,
        agent_mode="minimal" if minimal else loaded.mode or "standard",
    )


def persona_function_filter(persona: SubagentPersona | None) -> Callable[[Function], bool] | None:
    """Return a filter keeping only functions the persona names, or None when it keeps every tool."""
    if persona is None or persona.tools is None:
        return None
    toolkits = frozenset(entry for entry in persona.tools if "." not in entry)
    functions = frozenset(entry for entry in persona.tools if "." in entry)

    def visible(function: Function) -> bool:
        owner = function.owning_toolkit
        return owner is not None and (owner in toolkits or f"{owner}.{function.name}" in functions)

    return visible


def persona_disabled_toolkits(persona: SubagentPersona | None, available_toolkits: Sequence[str]) -> frozenset[str]:
    """Return the caller toolkits a persona never uses, so they are not constructed."""
    if persona is None or persona.tools is None:
        return frozenset()
    named = {entry.partition(".")[0] for entry in persona.tools}
    return frozenset(toolkit for toolkit in available_toolkits if toolkit not in named)
