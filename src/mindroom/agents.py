"""Agent loader that reads agent configurations from YAML file."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from functools import cached_property, partial
from pathlib import Path
from typing import TYPE_CHECKING

import mindroom.tools  # noqa: F401
from mindroom import constants
from mindroom.agent_cli.cli_shell_agent import STANDARD_CLI_NOTE, standard_cli_eligible, wrap_native_shell_window
from mindroom.agent_descriptions import describe_agent
from mindroom.agent_loop.build import AgentBuild, AgentSurface
from mindroom.agent_loop.prompt import PromptSpec, join_sections
from mindroom.agent_loop.spec import AgentSpec
from mindroom.agent_loop.tools import offered_toolkits, toolkit_instructions
from mindroom.approval.tool_approval import tool_may_require_approval
from mindroom.config.resolution import memory_backend
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.custom_tools.computer_announcement import computer_announcement_middleware
from mindroom.custom_tools.job import JobTools
from mindroom.delegation.personas import (
    caller_toolkit_names,
    persona_allows,
    persona_tool_policy,
    require_built_persona_tools,
)
from mindroom.delegation.state import DELEGATION_FUNCTIONS
from mindroom.entity_resolution import entity_identity_registry
from mindroom.error_handling import MinimalModeUnavailableError, minimal_mode_failure_message
from mindroom.failure_text import failure_text
from mindroom.hooks import HookBindings
from mindroom.knowledge.search import KNOWLEDGE_SEARCH_TOOL_NAME
from mindroom.llm.provider import DeferredTools, ModelRequest
from mindroom.logging_config import get_logger
from mindroom.mcp.toolkit import MindRoomMCPToolkit
from mindroom.path_confinement import read_regular_file_within_root
from mindroom.prompt_templates import build_agent_identity_context, openai_compat_identity, render_prompt_template
from mindroom.providers import registry as provider_registry
from mindroom.runtime_resolution import (
    ResolvedAgentRuntime,
    resolve_agent_runtime,
)
from mindroom.scheduling.scheduled_tool_calls import bind_live_tools
from mindroom.shared_directories.paths import describe_agent_shared_directories
from mindroom.system_prompt import render_session_context
from mindroom.timing import timed, timed_block
from mindroom.tool_jobs.authorization import authority_snapshot
from mindroom.tool_jobs.execution import JobActor, approval_can_run_as_job, assembled_job_tools, with_job_admission
from mindroom.tool_jobs.settings import background_tool_jobs_enabled, toolkit_is_background_excluded
from mindroom.tool_system.agent_tool_calls import DeferredAgentToolkit
from mindroom.tool_system.agent_tools import AgentTools, turn_room_flags
from mindroom.tool_system.agno_toolkits import (
    adapt_toolkit,
    configure_toolkit_functions,
    offered_refusal,
    toolkit_function_names,
    with_offered_instructions,
)
from mindroom.tool_system.catalog import get_tool_by_name
from mindroom.tool_system.construction import (
    ToolConstruction,
    bind_toolkit_construction,
    get_toolkit_construction,
    tool_config_signature,
)
from mindroom.tool_system.declarations import (
    MATRIX_ROOM_RUNTIME_TOOL_NAMES,
    ToolFileAccess,
    is_matrix_room_runtime_function,
)
from mindroom.tool_system.dynamic_toolkits import DeferredToolkit, ToolDeferral, defer_toolkit
from mindroom.tool_system.output_files import ToolOutputFilePolicy
from mindroom.tool_system.sandbox_proxy import ToolPlacement, tool_placement
from mindroom.tool_system.skills import SKILL_FUNCTION_NAMES, agent_workspace_skills_root, build_skills_toolkit
from mindroom.tool_system.tool_function import AdaptedToolkit, ToolGate
from mindroom.tool_system.tool_hooks import build_tool_hook_middleware
from mindroom.tool_system.toolkit_connections import run_connection
from mindroom.tool_system.worker_routing import (
    agent_workspace_root_path,
    resolve_agent_owned_path,
)
from mindroom.workers.backend_kinds import WorkerBackendKind, worker_backend_kind
from mindroom.workspaces import ensure_workspace_template

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from contextlib import AbstractContextManager

    from mindroom.agent_modes import AgentMode
    from mindroom.config.agent import AgentConfig
    from mindroom.config.main import Config
    from mindroom.config.models import EffectiveToolConfig, FileAccess
    from mindroom.config.resolution import ResolvedAgent
    from mindroom.delegation.state import SubagentPersona
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.knowledge.search import AgentKnowledge
    from mindroom.llm.provider import ModelProvider
    from mindroom.tool_system.agno_toolkits import AnyToolkit
    from mindroom.tool_system.invocation import ToolCall, ToolMiddleware, ToolNext
    from mindroom.tool_system.plugins import ConfigPlugins
    from mindroom.tool_system.registry import ToolRegistry
    from mindroom.tool_system.skills import RefusedWorkspaceSkill, SkillsToolkit
    from mindroom.tool_system.tool_function import ToolFunction
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity, WorkerScope
    from mindroom.turn_model import TurnModel

logger = get_logger(__name__)

_DEFAULT_MIND_AGENT_NAME = "mind"
_DEFAULT_MIND_CONTEXT_FILES = (
    "SOUL.md",
    "AGENTS.md",
    "USER.md",
    "IDENTITY.md",
    "TOOLS.md",
    "HEARTBEAT.md",
)
_PROJECTED_WORKER_ASSET_PATH_PREFIXES = (
    "./.mindroom-worker-assets/",
    ".mindroom-worker-assets/",
)
_MAX_WORKSPACE_CONTEXT_FILE_BYTES = 1 << 20


@dataclass
class _PreloadChunk:
    """One preloaded context file: its path as ``title``, its text, and what the preload cap removed from it.

    The rendered section names each file the cap cut and by how much, so a dropped file never vanishes without a trace.
    """

    title: str
    body: str
    omitted_chars: int = 0


@dataclass(frozen=True)
class _ToolkitEntry:
    """One toolkit an agent's run offers: what the run offers of it and, for a configured one, the toolkit built for it.

    ``adapted`` holds the functions a run may offer of the toolkit, adapted once right after the toolkit was built,
    each naming the authored toolkit it is offered from; every assembly step leaves functions out of it, never out of
    the built toolkit, and once the assembly resolved names across toolkits it holds only the functions a run offers.
    ``toolkit`` is the concrete toolkit built for a configured entry, which tool jobs check a call's grant and
    exclusions by; None for a toolkit generated for the agent, the knowledge search and the skills. ``deferral`` is set
    for a toolkit whose functions stay off the provider request until tool search finds them.
    """

    adapted: AdaptedToolkit
    toolkit: AnyToolkit | None = None
    deferral: ToolDeferral | None = None


@dataclass(frozen=True)
class _AgentToolAssembly:
    """Toolkits and dynamic-tool visibility assembled for one agent instance."""

    # Every toolkit a run offers, in offer order: the configured ones, then the ones generated beside them, each
    # holding only the functions a run offers of it.
    entries: tuple[_ToolkitEntry, ...]
    # The instructions the prompt shows of them.
    instructions: tuple[str, ...]
    local_tool_names: tuple[str, ...]
    worker_routed_tool_names: tuple[str, ...]
    cli_deferred: tuple[DeferredAgentToolkit, ...]
    # The tool-call policy the loop runs around every call, the hook bridge; a browser's computer announcement is
    # its functions' own.
    tool_middleware: tuple[ToolMiddleware, ...]

    @property
    def toolkits(self) -> tuple[AdaptedToolkit, ...]:
        """Return the toolkits a run offers, in offer order."""
        return tuple(entry.adapted for entry in self.entries)

    def job_sources(self) -> tuple[tuple[AnyToolkit, ToolFunction], ...]:
        """Return each function a run offers of a configured toolkit with the toolkit built for it, as tool jobs see it."""
        return tuple(
            (entry.toolkit, function)
            for entry in self.entries
            if entry.toolkit is not None
            for function in entry.adapted.functions
        )

    @cached_property
    def deferred_toolkits(self) -> tuple[DeferredToolkit, ...]:
        """Return each deferred toolkit with the functions a run offers of it, which only tool search shows."""
        return tuple(
            DeferredToolkit(entry.deferral, entry.adapted.functions)
            for entry in self.entries
            if entry.deferral is not None
        )

    @property
    def deferred_tool_names(self) -> tuple[str, ...]:
        """Return authored deferred-tool domains with a surviving function surface."""
        return tuple(
            dict.fromkeys(
                deferred.deferral.domain_name for deferred in self.deferred_toolkits if deferred.wire_function_names
            ),
        )

    @property
    def deferred_wire_tool_names(self) -> frozenset[str]:
        """Return deferred wire names from the final projected toolkit surface."""
        return frozenset(
            function_name for deferred in self.deferred_toolkits for function_name in deferred.wire_function_names
        )


@dataclass(frozen=True)
class _AgentRoleContext:
    """The fully rendered role prompt for one agent instance and the preload files it was rendered from."""

    role: str
    context_documents: tuple[_PreloadChunk, ...]

    def workspace_context_files(self, workspace_root: Path) -> list[str]:
        """List loaded context files that Bash can read relative to its workspace."""
        paths = (Path(document.title) for document in self.context_documents)
        return [path.relative_to(workspace_root).as_posix() for path in paths if path.is_relative_to(workspace_root)]


def _uses_default_mind_workspace_scaffold(config: Config, agent_name: str) -> bool:
    agent_config = config.agents[agent_name]
    return (
        agent_name == _DEFAULT_MIND_AGENT_NAME
        and agent_config.private is None
        and memory_backend(config, agent_name) == "file"
        and tuple(agent_config.context_files) == _DEFAULT_MIND_CONTEXT_FILES
    )


def _ensure_default_mind_workspace(storage_path: Path) -> None:
    workspace_path = agent_workspace_root_path(storage_path, _DEFAULT_MIND_AGENT_NAME)
    try:
        ensure_workspace_template(workspace_path, template="mind")
    except (OSError, ValueError) as exc:
        # Worker code writes the workspace; a planted entry must not fail every agent build.
        logger.warning("default_mind_workspace_scaffold_skipped", path=str(workspace_path), error=str(exc))


def ensure_default_agent_workspaces(config: Config, storage_path: Path) -> None:
    """Materialize built-in starter workspaces under the active runtime storage root."""
    for agent_name in config.agents:
        if _uses_default_mind_workspace_scaffold(config, agent_name):
            _ensure_default_mind_workspace(storage_path)


def agent_build_can_overlap_file_memory(agent_name: str, config: Config, storage_path: Path) -> bool:
    """Return True when it is safe to overlap agent construction with file-memory reads."""
    if not _uses_default_mind_workspace_scaffold(config, agent_name):
        return True
    memory_path = agent_workspace_root_path(storage_path, agent_name) / "MEMORY.md"
    return memory_path.is_file()


def _get_mind_runtime_context(agent_name: str, runtime_paths: constants.RuntimePaths) -> str:
    """Render live installation facts for the default Mind agent."""
    if agent_name != _DEFAULT_MIND_AGENT_NAME:
        return ""
    homeserver = constants.runtime_matrix_homeserver(runtime_paths=runtime_paths)
    return f"\n## MindRoom Runtime\n- Active Matrix homeserver: `{homeserver}`\n"


def _load_context_files(
    context_files: list[Path | str],
    runtime_paths: constants.RuntimePaths,
    agent_name: str | None = None,
    storage_path: Path | None = None,
    *,
    workspace_root: Path,
) -> list[_PreloadChunk]:
    """Load configured context files; ``Path`` entries and agent-owned strings are workspace files."""
    loaded_parts: list[_PreloadChunk] = []
    for raw_path in context_files:
        owning_workspace: Path | None = None
        if isinstance(raw_path, Path):
            resolved_path = raw_path
            owning_workspace = workspace_root
        elif raw_path.startswith(_PROJECTED_WORKER_ASSET_PATH_PREFIXES):
            resolved_path = constants.resolve_config_relative_path(raw_path, runtime_paths)
        elif agent_name is not None and storage_path is not None:
            try:
                resolved_path = resolve_agent_owned_path(
                    raw_path,
                    agent_name=agent_name,
                    base_storage_path=storage_path,
                )
            except ValueError:
                # A link agent code planted out of the workspace is refused, not an agent-build failure.
                logger.warning("context_file_refused", agent=agent_name, path=raw_path, error_type="ValueError")
                continue
            owning_workspace = agent_workspace_root_path(storage_path, agent_name).resolve()
        else:
            resolved_path = constants.resolve_config_relative_path(raw_path, runtime_paths)
        body = _read_context_file(resolved_path, workspace_root=owning_workspace, agent_name=agent_name)
        if body is None:
            continue
        loaded_parts.append(
            # The title is the full path so the rendered prompt tells the
            # model exactly which file on disk each part came from.
            _PreloadChunk(
                title=str(resolved_path),
                body=body,
            ),
        )
    return loaded_parts


@timed("system_prompt_assembly.agent_create.context_file_read")
def _read_context_file(resolved_path: Path, *, workspace_root: Path | None, agent_name: str | None) -> str | None:
    """Return one context file's text, or warn and return ``None`` when it is missing or refused."""
    try:
        if workspace_root is None:
            payload = resolved_path.read_bytes()
        else:
            relative_path = resolved_path.relative_to(workspace_root)
            payload = read_regular_file_within_root(
                workspace_root,
                relative_path,
                max_bytes=_MAX_WORKSPACE_CONTEXT_FILE_BYTES,
                truncate=True,
            )
    except FileNotFoundError:
        logger.warning("context_file_not_found", agent=agent_name, path=str(resolved_path))
        return None
    except (OSError, ValueError) as exc:
        logger.warning("context_file_refused", agent=agent_name, path=str(resolved_path), error_type=type(exc).__name__)
        return None
    if len(payload) >= _MAX_WORKSPACE_CONTEXT_FILE_BYTES:
        logger.warning("context_file_truncated", agent=agent_name, path=str(resolved_path))
    return payload.decode("utf-8", errors="replace").strip()


def _render_context_chunk(chunk: _PreloadChunk, *, chunk_marker_template: str) -> str:
    """Render one chunk under its source path, marking what the cap removed."""
    body = chunk.body.strip()
    if not chunk.omitted_chars:
        return f"### {chunk.title}\n{body}" if body else ""
    marker = render_prompt_template(
        chunk_marker_template,
        title=chunk.title,
        omitted_chars=chunk.omitted_chars,
    )
    return f"### {chunk.title}\n{body}\n{marker}" if body else f"### {chunk.title}\n{marker}"


def _render_context_chunks(
    chunks: list[_PreloadChunk],
    *,
    section_heading: str,
    chunk_marker_template: str,
) -> str:
    """Render every chunk that still carries content or an omission marker."""
    rendered = [
        block
        for chunk in chunks
        if (block := _render_context_chunk(chunk, chunk_marker_template=chunk_marker_template))
    ]
    if not rendered:
        return ""
    return f"{section_heading}\n" + "\n\n".join(rendered) + "\n\n"


def _drop_whole_chunks(
    chunks: list[_PreloadChunk],
    max_preload_chars: int,
    *,
    render: Callable[[list[_PreloadChunk]], str],
) -> int:
    """Drop entire chunk bodies (least critical first) until under the cap."""
    omitted = 0
    remaining_body_chunks = sum(bool(chunk.body) for chunk in chunks)
    for chunk in chunks:
        if len(render(chunks)) <= max_preload_chars:
            return omitted
        if not chunk.body:
            continue
        if remaining_body_chunks == 1:
            return omitted
        omitted += len(chunk.body)
        chunk.omitted_chars += len(chunk.body)
        chunk.body = ""
        remaining_body_chunks -= 1
    return omitted


def _trim_chunk_tails(
    chunks: list[_PreloadChunk],
    max_preload_chars: int,
    *,
    render: Callable[[list[_PreloadChunk]], str],
) -> int:
    """Trim from the *end* of chunks to preserve headers/identity at the top."""
    omitted = 0
    for chunk in chunks:
        while chunk.body:
            overflow = len(render(chunks)) - max_preload_chars
            if overflow <= 0:
                return omitted
            remove_count = min(overflow, len(chunk.body))
            original_length = len(chunk.body)
            chunk.body = chunk.body[: original_length - remove_count].rstrip()
            actual_removed = original_length - len(chunk.body)
            chunk.omitted_chars += actual_removed
            omitted += actual_removed
    return omitted


def _apply_preload_cap(
    personality_chunks: list[_PreloadChunk],
    max_preload_chars: int,
    *,
    section_heading: str,
    truncation_marker_template: str,
    chunk_marker_template: str,
) -> tuple[str, int]:
    """Apply hard preload cap with deterministic truncation priority.

    Truncation order is by file list order.
    First drops whole chunks, then trims from the *end* of remaining chunks.
    Every touched file keeps a marker naming it and the chars it lost, so a
    dropped context file is visible to the model instead of silently missing.
    """
    render = partial(
        _render_context_chunks,
        section_heading=section_heading,
        chunk_marker_template=chunk_marker_template,
    )
    rendered = render(personality_chunks)
    if len(rendered) <= max_preload_chars:
        return rendered, 0

    # Trim against a budget that already reserves room for the summary marker,
    # so file bodies cannot consume space required by omission metadata.
    marker_upper_bound = render_prompt_template(
        truncation_marker_template,
        omitted_chars=sum(len(chunk.body) for chunk in personality_chunks),
    )
    marker_upper_bound_block = f"\n\n{marker_upper_bound}\n\n"
    required_marker_chunks = [
        replace(
            chunk,
            body="",
            omitted_chars=chunk.omitted_chars + len(chunk.body),
        )
        for chunk in personality_chunks
    ]
    minimum_rendered = render(required_marker_chunks).rstrip("\n") + marker_upper_bound_block
    if len(minimum_rendered) > max_preload_chars:
        msg = (
            f"max_preload_chars={max_preload_chars} cannot fit required context headings and omission markers; "
            f"at least {len(minimum_rendered)} characters are required"
        )
        raise ValueError(msg)
    trim_budget = max_preload_chars - len(marker_upper_bound_block)

    omitted_chars = _drop_whole_chunks(
        personality_chunks,
        trim_budget,
        render=render,
    )
    omitted_chars += _trim_chunk_tails(
        personality_chunks,
        trim_budget,
        render=render,
    )

    rendered = render(personality_chunks)
    if omitted_chars <= 0:
        return rendered, 0

    marker = render_prompt_template(truncation_marker_template, omitted_chars=omitted_chars)
    marker_block = f"\n\n{marker}\n\n"
    return rendered.rstrip("\n") + marker_block, omitted_chars


@timed("system_prompt_assembly.agent_create.preloaded_files")
def _render_preloaded_files(
    agent_name: str,
    agent_config: AgentConfig,
    max_preload_chars: int,
    *,
    personality_section_heading: str,
    truncation_marker_template: str,
    chunk_marker_template: str,
    workspace_context_files: tuple[Path, ...],
    workspace_root: Path,
    context_documents: list[_PreloadChunk],
    storage_path: Path,
    runtime_paths: constants.RuntimePaths,
) -> str:
    """Render the configured and workspace context files as the role's preload section, within ``max_preload_chars``.

    This is evaluated each time one agent instance is created.
    The normal Matrix and OpenAI-compatible request paths build fresh agent
    instances per reply/request, so edits in the canonical agent workspace are
    reflected on the next reply without a process restart.
    """
    personality_chunks: list[_PreloadChunk] = []
    context_files: list[Path | str] = [*agent_config.context_files, *workspace_context_files]
    if context_files:
        personality_chunks = _load_context_files(
            context_files,
            runtime_paths,
            agent_name,
            storage_path,
            workspace_root=workspace_root,
        )

    # Preload truncation mutates chunks; keep complete files for CLI reads.
    context_documents.extend(replace(chunk) for chunk in personality_chunks)

    rendered, omitted_chars = _apply_preload_cap(
        personality_chunks,
        max_preload_chars,
        section_heading=personality_section_heading,
        truncation_marker_template=truncation_marker_template,
        chunk_marker_template=chunk_marker_template,
    )
    if omitted_chars > 0:
        logger.warning(
            "Preload context exceeded max_preload_chars and was truncated",
            omitted_chars=omitted_chars,
            max_preload_chars=max_preload_chars,
        )
    return rendered


def _file_mode_knowledge_instruction_block(
    knowledge_base_ids: tuple[str, ...],
    config: Config,
    workspace_root: Path,
) -> str | None:
    """Describe workspace-accessible file-only knowledge bases to the model."""
    lines = [
        "File-only knowledge bases are available in the workspace.",
        "Use file or coding tools such as grep, list, and read instead of search_knowledge_base for these sources.",
        "Available file-only sources:",
    ]
    source_lines: list[str] = []
    for base_id in knowledge_base_ids:
        base_config = config.get_knowledge_base_config(base_id)
        if base_config.mode != "files":
            continue
        relative_path = Path("knowledge") / base_id
        resolved_path = workspace_root / relative_path
        if not resolved_path.exists():
            continue
        description = " ".join(base_config.description.split()) or "No description configured."
        source_lines.append(f"- {base_id}: `{relative_path.as_posix()}`. {description}")
    if not source_lines:
        return None
    return "\n".join([*lines, *source_lines])


def _tool_base_dir_override(tool_name: str, *, tools: ToolRegistry, workspace_path: Path) -> dict[str, object] | None:
    """Point a registered tool that exposes a ``base_dir`` config field at the agent's workspace."""
    metadata = tools.get(tool_name)
    config_fields = (metadata.config_fields if metadata is not None else None) or ()
    if not any(field.name == "base_dir" for field in config_fields):
        return None
    return {"base_dir": str(workspace_path)}


def _offered_entries(
    entries: Sequence[_ToolkitEntry],
    *,
    agent_name: str,
) -> tuple[tuple[_ToolkitEntry, ...], dict[str, set[str]]]:
    """Return ``entries``, each holding only the functions a run offers of it, and by tool the functions left out.

    A run offers one function per name. An MCP function leaves whenever another toolkit offers its name, or the name
    is reserved for Matrix room recovery, because its server's catalog chooses that name and can change it without
    anyone editing the config, so a new remote tool never shadows a local function or another server's; every other
    name keeps its first function (``offered_toolkits``). A configured toolkit whose every function another took is
    reported.
    """
    offered = offered_toolkits(_without_colliding_mcp_functions(entries, agent_name=agent_name), agent=agent_name)
    offered_entries: list[_ToolkitEntry] = []
    left_out: dict[str, set[str]] = {}
    for entry, toolkit in zip(entries, offered, strict=True):
        offered_entries.append(replace(entry, adapted=toolkit))
        if entry.toolkit is not None and entry.adapted.functions and not toolkit.functions:
            logger.warning(
                "Configured toolkit exposes no unique model functions because function names collide",
                agent=agent_name,
                toolkit=toolkit.name,
                function_names=sorted(function.name for function in entry.adapted.functions),
            )
        offered_names = {function.name for function in toolkit.functions}
        for function in entry.adapted.functions:
            if function.name not in offered_names and function.registered_toolkit is not None:
                left_out.setdefault(function.registered_toolkit, set()).add(function.name)
    return tuple(offered_entries), left_out


def _without_colliding_mcp_functions(entries: Sequence[_ToolkitEntry], *, agent_name: str) -> list[AdaptedToolkit]:
    """Return the toolkits of ``entries``, each MCP one without the functions whose name another toolkit offers."""
    # Each owner is an entry, named by its toolkit; a reserved room-recovery function is its registered tool's,
    # offered or not.
    owners: dict[str, list[tuple[_ToolkitEntry | None, str]]] = {
        name: [(None, name)] for name in MATRIX_ROOM_RUNTIME_TOOL_NAMES
    }
    for entry in entries:
        for function in entry.adapted.functions:
            owners.setdefault(function.name, []).append((entry, entry.adapted.name))
    toolkits: list[AdaptedToolkit] = []
    for entry in entries:
        toolkit = entry.adapted
        if isinstance(entry.toolkit, MindRoomMCPToolkit):
            kept: list[ToolFunction] = []
            for function in toolkit.functions:
                others = [*dict.fromkeys(name for owner, name in owners[function.name] if owner is not entry)]
                if not others:
                    kept.append(function)
                    continue
                logger.warning(
                    "Hiding an MCP function whose name another toolkit offers",
                    agent=agent_name,
                    toolkit=toolkit.name,
                    function_name=function.name,
                    colliding_toolkits=others,
                )
            toolkit = replace(toolkit, functions=tuple(kept))
        toolkits.append(toolkit)
    return toolkits


class _MatrixRoomRuntimeToolCollisionError(ValueError):
    """Raised when another registered tool claims a reserved runtime function."""


def _reject_matrix_room_runtime_tool_function_collisions(registered_tool_name: str, toolkit: AnyToolkit) -> None:
    """Keep reserved room-runtime functions owned by their registered built-in tool, whatever else the toolkit holds.

    An MCP server's catalog chooses its names, so its function of a reserved name leaves at offer time instead.
    """
    if isinstance(toolkit, MindRoomMCPToolkit):
        return
    reserved_names = set(MATRIX_ROOM_RUNTIME_TOOL_NAMES)
    function_names = set(toolkit_function_names(toolkit))
    collisions = sorted(name for name in function_names & reserved_names if registered_tool_name != name)
    if collisions:
        msg = f"Tool function name(s) {', '.join(collisions)} are reserved for Matrix room recovery tools."
        raise _MatrixRoomRuntimeToolCollisionError(msg)


def _agent_tool_output_file_policy(
    agent_runtime: ResolvedAgentRuntime,
    runtime_paths: constants.RuntimePaths,
    tool_output_auto_save_threshold_bytes: int,
) -> ToolOutputFilePolicy:
    """Resolve the shared output-file policy for one agent's in-process tools."""
    return ToolOutputFilePolicy.from_runtime(
        agent_runtime.workspace.root,
        runtime_paths,
        auto_save_threshold_bytes=tool_output_auto_save_threshold_bytes,
    )


@timed("system_prompt_assembly.agent_create.model_instance")
def _build_agent_model_provider(
    config: Config,
    runtime_paths: constants.RuntimePaths,
    model_name: str,
    execution_identity: ToolExecutionIdentity | None,
) -> ModelProvider:
    """Build one agent's model provider while preserving prompt-assembly timing attribution."""
    return provider_registry.build_model_provider(
        config,
        runtime_paths,
        model_name,
        execution_identity=execution_identity,
    )


@timed("system_prompt_assembly.agent_create.toolkit_build")
def build_agent_toolkit(  # noqa: C901, PLR0911
    tool_name: str,
    *,
    agent_tools: AgentTools,
    placement: ToolPlacement,
    runtime_paths: constants.RuntimePaths,
    agent_runtime: ResolvedAgentRuntime | None = None,
    tool_config_overrides: dict[str, object] | None = None,
    execution_identity: ToolExecutionIdentity | None,
    workspace_in_worker: bool = False,
    delegation_depth: int = 0,
    refresh_scheduler: KnowledgeRefreshScheduler | None = None,
    persona: SubagentPersona | None = None,
    deferred_toolkits: Callable[[], Sequence[DeferredToolkit]] | None = None,
) -> AnyToolkit | None:
    """Build one of the toolkits ``agent_tools`` offers the agent, where ``placement`` runs it.

    ``persona`` is an authored subagent's persona; its ``delegate`` toolkit keeps
    the copies it authors within the persona's tools, skills, and knowledge bases.
    ``workspace_in_worker`` says whether the build's workspace-path toolkits run in a worker, where attachments save.

    ``deferred_toolkits`` returns the wire-deferred toolkits that ``dynamic_tools`` searches, as the agent's runs offer
    them; without it ``dynamic_tools`` is skipped and None is returned.
    """
    config = agent_tools.config
    agent_name = agent_tools.agent_name
    agent_config = config.get_agent(agent_name)
    if agent_runtime is None:
        agent_runtime = resolve_agent_runtime(
            agent_name,
            config,
            runtime_paths,
            execution_identity=execution_identity,
            create=True,
        )
    credentials_manager = get_runtime_credentials_manager(runtime_paths)

    def constructed[T: AnyToolkit](toolkit: T) -> T:
        return bind_toolkit_construction(
            toolkit,
            ToolConstruction(tool_name, None, tool_config_signature(tool_config_overrides)),
        )

    def wrap_direct[T: AnyToolkit](toolkit: T) -> T:
        threshold = config.defaults.tool_output_auto_save_threshold_bytes
        policy = _agent_tool_output_file_policy(agent_runtime, runtime_paths, threshold)
        return constructed(configure_toolkit_functions(toolkit, output_files=policy))

    if tool_name == "memory":
        from mindroom.custom_tools.memory import MemoryTools  # noqa: PLC0415

        return wrap_direct(
            MemoryTools(
                agent_name=agent_name,
                config=config,
                runtime_paths=runtime_paths,
                execution_identity=execution_identity,
            ),
        )

    if tool_name == "knowledge_graph":
        from mindroom.custom_tools.knowledge_graph import KnowledgeGraphTools  # noqa: PLC0415

        return wrap_direct(KnowledgeGraphTools(agent_name, config, runtime_paths, execution_identity))

    if tool_name == "delegate":
        # Imported lazily to avoid a circular import through DelegateTools -> create_agent.
        from mindroom.custom_tools import delegate  # noqa: PLC0415

        return wrap_direct(
            delegate.DelegateTools(
                agent_name=agent_name,
                runtime_paths=runtime_paths,
                config=config,
                execution_identity=execution_identity,
                delegation_depth=delegation_depth,
                refresh_scheduler=refresh_scheduler,
                workspace_root=agent_runtime.workspace.root,
                persona=persona,
            ),
        )

    if tool_name == "self_config":
        from mindroom.custom_tools.self_config import SelfConfigTools  # noqa: PLC0415

        return wrap_direct(
            SelfConfigTools(agent_name=agent_name, runtime_paths=runtime_paths),
        )

    if tool_name == "agent_manager":
        from mindroom.custom_tools.agent_manager import AgentManagerTools  # noqa: PLC0415

        can_create = agent_config.created_agents_per_owner > 0
        info = config.generated_agent(agent_name)
        generated, created = info is not None, info is not None and info.created
        return wrap_direct(AgentManagerTools(agent_name, can_create=can_create, generated=generated, created=created))

    if tool_name == "skill_manage":
        from mindroom.custom_tools.skill_manage import SkillManageTools  # noqa: PLC0415

        # It keeps its own output handling, but background tool jobs still need its construction identity.
        return constructed(
            SkillManageTools(
                agent_name,
                config,
                runtime_paths,
                agent_workspace_skills_root(agent_runtime.workspace.root),
            ),
        )

    if tool_name == "compact_context":
        from mindroom.custom_tools.compact_context import CompactContextTools  # noqa: PLC0415

        return wrap_direct(
            CompactContextTools(
                agent_name=agent_name,
                config=config,
                runtime_paths=runtime_paths,
                execution_identity=execution_identity,
            ),
        )

    if tool_name == "dynamic_workflow":
        from mindroom.custom_tools.dynamic_workflow import DynamicWorkflowTools  # noqa: PLC0415

        return wrap_direct(
            DynamicWorkflowTools(),
        )

    if tool_name == "report_publishing":
        from mindroom.custom_tools.report_publishing import ReportPublishingTools  # noqa: PLC0415

        return wrap_direct(
            ReportPublishingTools(),
        )

    if tool_name == "dynamic_tools":
        from mindroom.custom_tools.dynamic_tools import DynamicToolsToolkit  # noqa: PLC0415

        if deferred_toolkits is None:
            return None
        # Search results must reach the model inline, so they skip output-file redirection.
        return constructed(DynamicToolsToolkit(deferred_toolkits=deferred_toolkits))

    worker_scope = agent_runtime.execution.execution_scope
    return get_tool_by_name(
        tool_name,
        runtime_paths,
        tools=agent_tools.tools,
        credentials_manager=credentials_manager,
        runtime_config=config,
        tool_config_overrides=tool_config_overrides,
        tool_init_overrides=_tool_base_dir_override(
            tool_name,
            tools=agent_tools.tools,
            workspace_path=agent_runtime.workspace.root,
        ),
        runs_in_worker=agent_tools.runs_in_worker(tool_name, placement),
        allowed_shared_services=config.get_worker_grantable_credentials() if worker_scope is not None else None,
        tool_output_workspace_root=agent_runtime.workspace.root,
        agent_state_root=agent_runtime.state_root,
        tool_output_auto_save_threshold_bytes=config.defaults.tool_output_auto_save_threshold_bytes,
        workspace_in_worker=workspace_in_worker,
        worker_target=agent_runtime.execution.worker_target,
    )


def _render_tool_execution_environment(
    *,
    runtime_paths: constants.RuntimePaths,
    local_tool_names: tuple[str, ...],
    worker_routed_tool_names: tuple[str, ...],
    worker_scope: WorkerScope | None,
    file_access: FileAccess,
    unconfined_tool_names: tuple[str, ...],
    primary_only_unconfined_tool_names: tuple[str, ...],
    shared_directories: tuple[str, ...],
) -> str:
    """Describe effective per-tool execution routing and file access to the model."""

    def tool_list(names: tuple[str, ...]) -> str:
        return ", ".join(f"`{name}`" for name in names) if names else "none"

    if not worker_routed_tool_names and not local_tool_names:
        return "## Tool Execution Environment\n- No tools are available in this runtime."

    file_access_description = {
        "workspace": "agent workspace and attachments only",
        "unrestricted": "any path the tool's process can reach",
    }[file_access]
    file_access_lines = [f"- File access for path tools: `{file_access}` ({file_access_description})."]
    if shared_directories:
        file_access_lines.append(f"- Shared directories: {', '.join(shared_directories)}.")
    if unconfined_tool_names:
        file_access_lines.append(
            f"- Not confined by file_access (only a worker isolates them): {tool_list(unconfined_tool_names)}.",
        )
    if primary_only_unconfined_tool_names:
        file_access_lines.append(
            "- Not confined by file_access and unable to run in a worker (trusted primary runtime only): "
            f"{tool_list(primary_only_unconfined_tool_names)}.",
        )

    if not worker_routed_tool_names:
        return "\n".join(
            [
                "## Tool Execution Environment",
                f"- All available tools run in the primary MindRoom runtime: {tool_list(local_tool_names)}.",
                "- No tools use a worker runtime.",
                *file_access_lines,
            ],
        )

    backend = worker_backend_kind(runtime_paths)
    lines = [
        "## Tool Execution Environment",
        f"- Local tools (primary MindRoom runtime): {tool_list(local_tool_names)}.",
        f"- Worker-routed tools: {tool_list(worker_routed_tool_names)}.",
        f"- Worker backend: `{backend.value}`.",
    ]
    if backend is WorkerBackendKind.STATIC_RUNNER:
        lines.append(
            "- Worker reuse: requests use the configured static runner; no per-user or per-agent "
            "worker state boundary is selected.",
        )
    else:
        scope_description = {
            None: "one runtime for this agent within the current tenant or account",
            "shared": "one runtime for this agent, shared by all users",
            "user": "one runtime for this user, shared across this user's agents",
            "user_agent": "one runtime used only by this user-agent pair",
        }[worker_scope]
        idle_behavior = {
            WorkerBackendKind.DOCKER: "the container stops",
            WorkerBackendKind.KUBERNETES: "the deployment scales to zero",
        }[backend]
        lines.extend(
            (
                f"- Worker reuse: {scope_description}.",
                f"- Worker state is reused across turns for that scope. After the configured idle timeout, "
                f"{idle_behavior}; persisted files and caches remain until an operator deletes that worker state.",
            ),
        )
    lines.extend(file_access_lines)
    lines.append("- Execution location is determined per tool; this agent is not sandboxed as a whole.")
    return "\n".join(lines)


def _unconfined_tool_names(
    tool_names: tuple[str, ...],
    *,
    tools: ToolRegistry,
    requires_primary_runtime: bool,
) -> tuple[str, ...]:
    """Return sorted tools not confined by file_access, split by whether a worker can isolate them."""
    return tuple(
        sorted(
            name
            for name in tool_names
            if (metadata := tools.get(name)) is not None
            and metadata.file_access is ToolFileAccess.UNCONFINED
            and metadata.requires_primary_runtime is requires_primary_runtime
        ),
    )


def _build_dynamic_tooling_instruction_block(
    config: Config,
    deferred_toolkits: Sequence[DeferredToolkit],
) -> str | None:
    """Return the static deferred-tool catalog for portable tool search."""
    catalog = {
        deferred.deferral.domain_name: deferred.deferral.description.strip()
        for deferred in deferred_toolkits
        if deferred.wire_function_names
    }
    if not catalog:
        return None
    return config.render_prompt(
        "DYNAMIC_TOOLING_INSTRUCTION_TEMPLATE",
        tool_catalog="\n".join(
            f"{name} - {description}" if description else name for name, description in catalog.items()
        ),
    )


def _keep_functions(toolkit: AdaptedToolkit, keep: Callable[[ToolFunction], bool]) -> AdaptedToolkit:
    """Return ``toolkit`` offering only the functions ``keep`` accepts."""
    return replace(toolkit, functions=tuple(function for function in toolkit.functions if keep(function)))


def _approval_may_gate(config: Config, function_name: str, registered_tool_name: str) -> bool:
    """Return whether a config approval rule may gate one function of the toolkit registered as this name."""
    # A scheduled call runs only with the approval its requester gave when it was scheduled.
    is_scheduled_call_runner = registered_tool_name == "scheduler" and function_name == "run_scheduled_call"
    return (
        not is_matrix_room_runtime_function(registered_tool_name, function_name)
        and not is_scheduled_call_runner
        and tool_may_require_approval(config, function_name)
    )


def apply_tool_approval_capability(
    adapted: AdaptedToolkit,
    toolkit: AnyToolkit,
    config: Config,
    *,
    registered_tool_name: str,
    resumable: bool,
    approvals_as_jobs: bool,
) -> AdaptedToolkit:
    """Apply the approval rules to the functions of ``adapted``, the toolkit ``toolkit`` was adapted to.

    A caller that cannot resume a paused run (``resumable`` False) is offered none of the functions that ask for
    approval. Otherwise each function an approval rule gates pauses the run for its approval, or, with
    ``approvals_as_jobs``, asks for it as the first phase of its call's job when the call can become one; the
    delegation functions pause for their driver, which owns the policy gate and the exact child wait.
    """

    def offered(function: ToolFunction) -> ToolFunction | None:
        may_gate = _approval_may_gate(config, function.name, registered_tool_name)
        if not resumable:
            return None if function.gate is ToolGate.CONFIRM or may_gate else function
        if registered_tool_name == "delegate" and function.name in DELEGATION_FUNCTIONS:
            return replace(function, gate=ToolGate.DELEGATION)
        # A function that asks for its own confirmation keeps it; a rule may gate any other.
        if function.gate is ToolGate.CONFIRM or not may_gate:
            return function
        if approvals_as_jobs and approval_can_run_as_job(function, toolkit):
            return replace(function, gate=ToolGate.JOB)
        return replace(function, gate=ToolGate.POLICY)

    functions = (offered(function) for function in adapted.functions)
    return replace(adapted, functions=tuple(function for function in functions if function is not None))


def _render_agent_identity_context(
    agent_name: str,
    display_name: str,
    config: Config,
    runtime_paths: constants.RuntimePaths,
    *,
    model_provider: str,
    model_id: str,
    include_openai_compat_guidance: bool,
) -> str:
    openai_compat_history_guidance = config.get_prompt("OPENAI_COMPAT_HISTORY_GUIDANCE")
    if include_openai_compat_guidance:
        return openai_compat_identity(
            config.prompts,
            openai_identity=render_prompt_template(
                config.get_prompt("OPENAI_COMPAT_AGENT_IDENTITY_CONTEXT_TEMPLATE"),
                agent_name=agent_name,
                display_name=display_name,
                model_provider=model_provider,
                model_id=model_id,
                openai_compat_history_guidance=openai_compat_history_guidance,
            ),
            matrix_identity=render_prompt_template(
                config.get_prompt("AGENT_IDENTITY_CONTEXT_TEMPLATE"),
                display_name=display_name,
                matrix_id="not available in OpenAI-compatible API",
                model_provider=model_provider,
                model_id=model_id,
                openai_compat_history_guidance=openai_compat_history_guidance,
            ),
            openai_prompt_name="OPENAI_COMPAT_AGENT_IDENTITY_CONTEXT_TEMPLATE",
            matrix_prompt_name="AGENT_IDENTITY_CONTEXT_TEMPLATE",
        )

    matrix_id = entity_identity_registry(config, runtime_paths).current_id(agent_name).full_id
    return build_agent_identity_context(
        display_name=display_name,
        matrix_id=matrix_id,
        model_provider=model_provider,
        model_id=model_id,
        include_openai_compat_guidance=False,
        identity_context_template=config.get_prompt("AGENT_IDENTITY_CONTEXT_TEMPLATE"),
        openai_compat_history_guidance=openai_compat_history_guidance,
    )


_load_agent_skills = timed("system_prompt_assembly.agent_create.skills_load")(build_skills_toolkit)


def _generated_function_visible(
    config: Config,
    tool_function_filter: Callable[[ToolFunction], bool] | None,
    function: ToolFunction,
) -> bool:
    """Hide generated functions an approval rule may gate, because they have no toolkit origin to pause and resume."""
    return not tool_may_require_approval(config, function.name) and (
        tool_function_filter is None or tool_function_filter(function)
    )


def _agent_create_timing(label: str, **event_data: object) -> AbstractContextManager[None]:
    return timed_block(f"system_prompt_assembly.agent_create.{label}", scope=None, **event_data)


def _assemble_agent_toolkits(  # noqa: C901, PLR0915 - eager and deferred tools share one construction path
    agent_tools: AgentTools,
    runtime_paths: constants.RuntimePaths,
    agent_runtime: ResolvedAgentRuntime,
    *,
    execution_identity: ToolExecutionIdentity | None,
    disable_runtime_capabilities: bool,
    disabled_tool_names: frozenset[str],
    tool_function_filter: Callable[[ToolFunction], bool] | None,
    delegation_depth: int,
    refresh_scheduler: KnowledgeRefreshScheduler | None,
    supports_native_tool_approval: bool,
    wire_deferred_tools: bool,
    portable_deferred_tools: bool,
    eager_deferred_tools: bool,
    required_tool_names: tuple[str, ...],
    minimal_mode: bool,
    persona: SubagentPersona | None,
    plugins: ConfigPlugins,
    knowledge: AgentKnowledge | None,
    skills: SkillsToolkit | None,
) -> _AgentToolAssembly:
    """Assemble the toolkits one agent instance offers and its dynamic-tool visibility.

    The configured toolkits come first and the knowledge search and skills generated beside them after, so a
    generated function never displaces a configured one; the names across all of them are resolved once, here.
    """
    config = agent_tools.config
    agent_name = agent_tools.agent_name
    approvals_as_jobs = (
        background_tool_jobs_enabled(config, runtime_paths) and delegation_depth == 0 and not minimal_mode
    )
    persona_tools = persona.tools if persona is not None else None
    hook_middleware = build_tool_hook_middleware(HookBindings(registry=plugins.hooks), config, runtime_paths)
    room_tools, matrix_room_tools = turn_room_flags(execution_identity)
    # Minimal mode calls deferred toolkits through mindroom-agent, so it attaches only initial ones and the toolkits a
    # saved approval needs; every other runtime attaches them all. Native tool search sends deferred ones marked as
    # deferred, portable tool search keeps them off the wire, and personas with their own tool list, plus voice calls
    # on models without native tool search, send them as ordinary tools.
    selected = agent_tools.candidates(
        depth=delegation_depth,
        room_tools=room_tools,
        matrix_room_tools=matrix_room_tools,
        deferred=required_tool_names if minimal_mode and not eager_deferred_tools else None,
        portable_search=portable_deferred_tools,
    )
    resolved_tool_configs = (
        {}
        if disable_runtime_capabilities
        else {entry.name: entry for entry in selected if entry.name not in disabled_tool_names}
    )
    deferred_configs = {}
    if minimal_mode:
        complete = agent_tools.candidates(
            depth=delegation_depth,
            room_tools=room_tools,
            matrix_room_tools=matrix_room_tools,
        )
        deferred_configs = {
            entry.name: entry
            for entry in complete
            if entry.name not in resolved_tool_configs and entry.name not in disabled_tool_names
        }
    placement = tool_placement(runtime_paths, agent_tools.tools)
    workspace_in_worker = agent_tools.workspace_in_worker([*resolved_tool_configs, *deferred_configs], placement)
    entries: list[_ToolkitEntry] = []
    assembly: _AgentToolAssembly | None = None

    def searched_toolkits() -> tuple[DeferredToolkit, ...]:
        # Tool search is one of the toolkits the assembly below is made of, and its names take part in the steps
        # across toolkits, yet it searches what that assembly offers, so it reads the assembly when it runs.
        assert assembly is not None
        return assembly.deferred_toolkits

    local_tool_names: list[str] = []
    worker_routed_tool_names: list[str] = []
    persona_built: dict[str, frozenset[str]] = {}

    def build_entry(tool_entry: EffectiveToolConfig) -> _ToolkitEntry | None:
        """Build and adapt one configured toolkit; what the agent records of it waits until both succeeded."""
        tool_name = tool_entry.name
        authored_name = tool_entry.authored_name or tool_name
        with _agent_create_timing("toolkit_build.one", tool_name=tool_name):
            toolkit = build_agent_toolkit(
                tool_name,
                agent_tools=agent_tools,
                placement=placement,
                runtime_paths=runtime_paths,
                agent_runtime=agent_runtime,
                tool_config_overrides=tool_entry.tool_config_overrides,
                execution_identity=execution_identity,
                workspace_in_worker=workspace_in_worker,
                delegation_depth=delegation_depth,
                refresh_scheduler=refresh_scheduler,
                persona=persona,
                deferred_toolkits=searched_toolkits,
            )
        if toolkit is None:
            return None
        _reject_matrix_room_runtime_tool_function_collisions(tool_name, toolkit)
        # Adapted once, right after it is built; every step below works on the functions it offers.
        adapted = adapt_toolkit(toolkit, key=authored_name, registered=tool_name)
        if persona_tools is not None:
            # Match the concrete toolkit; a preset's or implied tool's functions name the tool bringing them in.
            adapted = _keep_functions(adapted, lambda function: persona_allows(persona_tools, tool_name, function.name))
        if tool_function_filter is not None:
            adapted = _keep_functions(adapted, tool_function_filter)
        if not adapted.functions:
            return None
        # What survives the caller's filters; the approval policy below may still hide gated calls.
        filtered = frozenset(function.name for function in adapted.functions)
        adapted = apply_tool_approval_capability(
            adapted,
            toolkit,
            config,
            registered_tool_name=tool_name,
            resumable=supports_native_tool_approval,
            # Only a top-level agent's own calls ask as their job; the same setting installs the job executor.
            approvals_as_jobs=approvals_as_jobs
            and not toolkit_is_background_excluded(tool_name, config, runtime_paths),
        )
        # Instructions describe only the functions left; Agno's defaults that name a missing one are dropped.
        construction = get_toolkit_construction(toolkit)
        adapted = with_offered_instructions(
            toolkit,
            adapted,
            authored_instructions=construction is not None and construction.authored_instructions,
        )
        entry = offered_entry(toolkit, adapted, tool_entry, authored_name) if adapted.functions else None
        if persona_tools is not None:
            persona_built[tool_name] = filtered
        return entry

    def offered_entry(
        toolkit: AnyToolkit,
        adapted: AdaptedToolkit,
        tool_entry: EffectiveToolConfig,
        authored_name: str,
    ) -> _ToolkitEntry:
        """Return the entry of a toolkit that offers ``adapted``'s functions: deferred or eager, with its own policy."""
        tool_name = tool_entry.name
        deferral = None
        # initial deferred tools stay in the rendered prompt prefix as plain non-deferred tools.
        if wire_deferred_tools and tool_entry.defer and not tool_entry.initial:
            metadata = agent_tools.tools.get(authored_name)
            deferral, adapted = defer_toolkit(
                authored_name,
                metadata.description if metadata else "",
                adapted,
                portable_search=portable_deferred_tools,
            )
        # Refused only now, so a function the filters and the approval policy left out costs its toolkit nothing.
        if (refusal := offered_refusal(adapted)) is not None:
            raise ValueError(refusal)
        announcement = computer_announcement_middleware(
            toolkit,
            tool_name,
            agent_name=agent_name,
            config=config,
            runtime_paths=runtime_paths,
        )
        if announcement is not None:
            # The browser's own calls announce the computer, so they do whoever offers them, a deferred one's too.
            functions = tuple(
                replace(function, middleware=(*function.middleware, announcement)) for function in adapted.functions
            )
            adapted = replace(adapted, functions=functions)
        adapted = replace(adapted, connect=run_connection(toolkit))
        return _ToolkitEntry(adapted=adapted, toolkit=toolkit, deferral=deferral)

    cli_deferred = []
    for deferred_entry in deferred_configs.values():

        async def materialize(tool_entry: EffectiveToolConfig = deferred_entry) -> AdaptedToolkit:
            entry = await asyncio.to_thread(build_entry, tool_entry)
            if entry is None:
                msg = f"Configured toolkit {tool_entry.name!r} is unavailable"
                raise ValueError(msg)
            return entry.adapted

        metadata = agent_tools.tools.get(deferred_entry.name)
        cli_deferred.append(
            DeferredAgentToolkit(deferred_entry.name, metadata.description if metadata else "", materialize),
        )

    dynamic_tools_entry = resolved_tool_configs.pop("dynamic_tools", None)

    def tool_entries() -> Iterator[tuple[str, EffectiveToolConfig]]:
        yield from resolved_tool_configs.items()
        # The search toolkit comes after every deferred toolkit it searches, and only portable tool search rewrites
        # call_tool, so an authored dynamic_tools entry is ignored everywhere else.
        if (
            dynamic_tools_entry is not None
            and portable_deferred_tools
            and any(entry.deferral is not None for entry in entries)
        ):
            yield dynamic_tools_entry.name, dynamic_tools_entry

    for tool_name, tool_entry in tool_entries():
        try:
            entry = build_entry(tool_entry)
        except _MatrixRoomRuntimeToolCollisionError:
            raise
        except Exception as exc:
            # One toolkit's construction or adaptation failure must never stop the agent.
            if minimal_mode:
                raise MinimalModeUnavailableError(
                    minimal_mode_failure_message(failure_text(exc), agent_name, subagent=delegation_depth > 0),
                ) from exc
            logger.warning(
                "Could not load tool for agent construction",
                tool=tool_name,
                agent=agent_name,
                error=str(exc),
                exc_info=not isinstance(exc, ValueError | ImportError),
            )
            continue
        if entry is not None:
            entries.append(entry)
            target_names = (
                worker_routed_tool_names if agent_tools.runs_in_worker(tool_name, placement) else local_tool_names
            )
            target_names.append(tool_name)
    controls = JobTools.build(
        {function.name for entry in entries for function in entry.adapted.functions},
        runtime_paths,
        execution_identity,
        depth=delegation_depth,
        enabled=not disable_runtime_capabilities and background_tool_jobs_enabled(config, runtime_paths),
        output_file_policy=_agent_tool_output_file_policy(
            agent_runtime,
            runtime_paths,
            config.defaults.tool_output_auto_save_threshold_bytes,
        ),
    )
    if controls is not None:
        entries.append(_job_controls_entry(controls))
    built = [
        *entries,
        *_generated_entries(
            entries,
            knowledge,
            skills,
            partial(_generated_function_visible, config, tool_function_filter),
        ),
    ]
    offered, left_out = _offered_entries(built, agent_name=agent_name)
    if persona_tools is not None:
        require_built_persona_tools(persona_tools, persona_built, left_out)
    assembly = _AgentToolAssembly(
        entries=offered,
        instructions=toolkit_instructions(entry.adapted for entry in offered),
        cli_deferred=tuple(cli_deferred),
        local_tool_names=tuple(local_tool_names),
        worker_routed_tool_names=tuple(worker_routed_tool_names),
        tool_middleware=(hook_middleware,),
    )
    return assembly


def _build_agent_role_context(
    agent_name: str,
    agent_config: AgentConfig,
    config: Config,
    runtime_paths: constants.RuntimePaths,
    agent_runtime: ResolvedAgentRuntime,
    *,
    model_name: str,
    include_openai_compat_guidance: bool,
    disable_runtime_capabilities: bool,
    local_tool_names: tuple[str, ...],
    worker_routed_tool_names: tuple[str, ...],
    tools: ToolRegistry,
    file_access: FileAccess,
    include_identity: bool,
) -> _AgentRoleContext:
    """Render the identity, the runtime facts, the preloaded files, and the authored role into the role, as sections.

    Without ``include_identity``, as for an authored subagent whose prompt replaces it, the role holds only the
    runtime facts.
    """
    runtime_facts = join_sections(
        (
            _get_mind_runtime_context(agent_name, runtime_paths),
            None
            if disable_runtime_capabilities
            else _render_tool_execution_environment(
                runtime_paths=runtime_paths,
                local_tool_names=local_tool_names,
                worker_routed_tool_names=worker_routed_tool_names,
                worker_scope=agent_runtime.execution.execution_scope,
                file_access=file_access,
                unconfined_tool_names=_unconfined_tool_names(
                    (*local_tool_names, *worker_routed_tool_names),
                    tools=tools,
                    requires_primary_runtime=False,
                ),
                primary_only_unconfined_tool_names=_unconfined_tool_names(
                    (*local_tool_names, *worker_routed_tool_names),
                    tools=tools,
                    requires_primary_runtime=True,
                ),
                shared_directories=describe_agent_shared_directories(config, runtime_paths, agent_name),
            ),
        ),
    )
    if not include_identity:
        return _AgentRoleContext(role=runtime_facts, context_documents=())

    model_config = config.models.get(model_name)
    model_provider = model_config.provider.title() if model_config is not None else "AI"
    model_id = model_config.id if model_config is not None else model_name
    with _agent_create_timing("identity_context"):
        identity_context = _render_agent_identity_context(
            agent_name,
            agent_config.display_name,
            config,
            runtime_paths,
            model_provider=model_provider,
            model_id=model_id,
            include_openai_compat_guidance=include_openai_compat_guidance,
        )

    context_documents: list[_PreloadChunk] = []
    preloaded_files = None
    if not disable_runtime_capabilities:
        workspace = agent_runtime.workspace
        preloaded_files = _render_preloaded_files(
            agent_name,
            agent_config,
            config.defaults.max_preload_chars,
            personality_section_heading=config.get_prompt("PERSONALITY_CONTEXT_SECTION_HEADING"),
            truncation_marker_template=config.get_prompt("CONTEXT_TRUNCATION_MARKER_TEMPLATE"),
            chunk_marker_template=config.get_prompt("CONTEXT_CHUNK_OMITTED_MARKER_TEMPLATE"),
            workspace_context_files=workspace.context_files,
            workspace_root=workspace.root,
            context_documents=context_documents,
            storage_path=runtime_paths.storage_root,
            runtime_paths=runtime_paths,
        )

    return _AgentRoleContext(
        role=join_sections((identity_context, runtime_facts, preloaded_files, agent_config.role)),
        context_documents=tuple(context_documents),
    )


def _skill_usage_instructions(
    config: Config,
    skills: SkillsToolkit | None,
    hidden_functions: frozenset[str],
    refused: Sequence[RefusedWorkspaceSkill],
) -> list[str]:
    """Return the skill-function guidance for an agent offered skills, naming any skill function approval hides.

    Each workspace skill in ``refused`` gets a line saying why it did not load, also when no skill loads.
    """
    if hidden_functions == SKILL_FUNCTION_NAMES:
        return []
    refusals = [
        f"Workspace skill `{skill.path}` did not load. {skill.reason}. Fix the file and it loads on your next run."
        for skill in refused
    ]
    if skills is None or not skills.skills:
        return refusals
    # This guidance and the skills prompt block both name every skill function, so name the ones the agent lacks.
    hidden = f"These skill functions are not available to you: {', '.join(sorted(hidden_functions))}."
    return [config.get_prompt("SKILLS_TOOL_USAGE_PROMPT"), *((hidden,) if hidden_functions else ()), *refusals]


def _build_agent_instructions(
    agent: ResolvedAgent,
    configured_instructions: Sequence[str],
    config: Config,
    workspace_root: Path,
    *,
    skill_instructions: Sequence[str],
    include_interactive_questions: bool,
    disable_runtime_capabilities: bool,
    native_deferred_tool_names: tuple[str, ...],
    portable_deferred_toolkits: Sequence[DeferredToolkit],
    skill_authoring: bool,
) -> list[str]:
    """Accumulate the configured and runtime instruction blocks for one agent instance.

    ``skill_authoring`` adds the note on writing workspace skills, for agents that can use skills.
    """
    instructions = [*configured_instructions, *skill_instructions]
    if native_deferred_tool_names:
        # Compact discovery guidance for provider-native deferred tools.
        instructions.append(
            config.render_prompt(
                "NATIVE_TOOL_SEARCH_INSTRUCTION_TEMPLATE",
                tool_domains=", ".join(native_deferred_tool_names),
            ),
        )

    dynamic_tooling_block = _build_dynamic_tooling_instruction_block(config, portable_deferred_toolkits)
    if dynamic_tooling_block is not None:
        instructions.append(dynamic_tooling_block)

    if not disable_runtime_capabilities:
        instructions.append(config.get_prompt("OUTPUT_REDIRECT_PROMPT"))
        # A conditional expression keeps this builder within the complexity limit.
        instructions.extend((config.get_prompt("WORKSPACE_SKILL_AUTHORING_PROMPT"),) if skill_authoring else ())

    file_mode_knowledge_instruction = (
        None
        if disable_runtime_capabilities
        else _file_mode_knowledge_instruction_block(agent.knowledge_base_ids, config, workspace_root)
    )
    if file_mode_knowledge_instruction is not None:
        instructions.append(file_mode_knowledge_instruction)

    if not agent.show_tool_calls:
        instructions.append(config.get_prompt("HIDDEN_TOOL_CALLS_PROMPT"))

    if include_interactive_questions:
        instructions.append(config.get_prompt("INTERACTIVE_QUESTION_PROMPT"))

    return instructions


def _job_controls_entry(controls: AnyToolkit) -> _ToolkitEntry:
    """Assemble the job controls as their own authored toolkit, which no approval rule gates.

    The loop runs the caller's tool hooks around their calls as around any other, but they never ask for approval:
    they act only on the caller's own jobs, each of which passed its own.
    """
    return _ToolkitEntry(adapted=adapt_toolkit(controls, key=controls.name), toolkit=controls)


@dataclass(frozen=True)
class LeaderJobs:
    """A team leader's job controls as the agent loop offers them, the tool-call policy they run under, and the team.

    ``middleware`` runs outermost first, job admission before the hook bridge, which only the job controls' calls
    pass; ``actor`` is the team as tool jobs see it, whose leader hears of the jobs that ended.
    """

    controls: AdaptedToolkit
    middleware: tuple[ToolMiddleware, ...]
    actor: JobActor


def team_background_jobs(
    config: Config,
    runtime_paths: constants.RuntimePaths,
    owner: ToolExecutionIdentity | None,
    plugins: ConfigPlugins,
) -> LeaderJobs | None:
    """Return a team leader's job controls while the instance runs managed jobs for a Matrix owner.

    The team owns the jobs its members start for it: its leader collects them through these controls; their tool
    hooks come from ``plugins``, the view the team turn captured.
    """
    if not background_tool_jobs_enabled(config, runtime_paths) or owner is None:
        return None
    controls = JobTools.build((), runtime_paths, owner, depth=0, enabled=True)
    if controls is None:
        return None
    hooks = build_tool_hook_middleware(HookBindings(registry=plugins.hooks), config, runtime_paths)
    entry = _job_controls_entry(controls)
    control_names = frozenset(function.name for function in entry.adapted.functions)

    async def control_hooks(call: ToolCall, call_next: ToolNext) -> object:
        # The leader's delegations run without the tool-call policy, as they do with jobs off.
        if call.function.name not in control_names:
            return await call_next(call.arguments)
        return await hooks(call, call_next)

    actor = JobActor(
        name=owner.recipient,
        team=True,
        depth=0,
        # A team has no execution scope of its own, so its calls run under the grants of the work around them.
        tools=assembled_job_tools(((controls, function) for function in entry.adapted.functions), {}),
    )
    return LeaderJobs(controls=entry.adapted, middleware=with_job_admission(actor, (control_hooks,)), actor=actor)


_MEMORY_SEARCH_TOOL_NAME = "search_memories"


def _offers_function(entries: Sequence[_ToolkitEntry], function_name: str) -> bool:
    return any(function.name == function_name for entry in entries for function in entry.adapted.functions)


def _generated_entries(
    entries: Sequence[_ToolkitEntry],
    knowledge: AgentKnowledge | None,
    skills: SkillsToolkit | None,
    generated_visible: Callable[[ToolFunction], bool],
) -> list[_ToolkitEntry]:
    """Return the toolkits generated beside the configured ``entries``, which a run offers after them.

    The knowledge search and skill functions get the approval and persona policy here that configured functions got
    while their toolkits were built.
    """
    generated: list[AdaptedToolkit] = []
    if knowledge is not None:
        search = knowledge.search_function(memory_search_available=_offers_function(entries, _MEMORY_SEARCH_TOOL_NAME))
        if generated_visible(search):
            generated.append(AdaptedToolkit(name=KNOWLEDGE_SEARCH_TOOL_NAME, functions=(search,), instructions=None))
    if skills is not None and (visible_skills := _keep_functions(adapt_toolkit(skills), generated_visible)).functions:
        generated.append(visible_skills)
    return [_ToolkitEntry(adapted=toolkit) for toolkit in generated]


@timed("system_prompt_assembly.agent_create")
def create_agent(
    agent_name: str,
    config: Config,
    runtime_paths: constants.RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
    *,
    knowledge: AgentKnowledge | None,
    model: TurnModel,
    plugins: ConfigPlugins,
    include_interactive_questions: bool = True,
    include_openai_compat_guidance: bool = False,
    disable_runtime_capabilities: bool = False,
    tool_function_filter: Callable[[ToolFunction], bool] | None = None,
    delegation_depth: int = 0,
    refresh_scheduler: KnowledgeRefreshScheduler | None = None,
    supports_native_tool_approval: bool = False,
    eager_deferred_tools: bool = False,
    required_tool_names: tuple[str, ...] = (),
    agent_mode: AgentMode = "standard",
    agent_cli_in_shell: bool = False,
    persona: SubagentPersona | None = None,
    provider: ModelProvider | None = None,
) -> AgentBuild:
    """Build an agent from configuration: its model, tools, prompt, request settings, and surface.

    Args:
        agent_name: Name of the agent to create
        config: Application configuration
        runtime_paths: Explicit runtime context for paths, env, and credentials.
        execution_identity: Request execution identity used to resolve scoped
            state, workspaces, worker routing, and requester-local storage.
        knowledge: The shared knowledge of a RAG-enabled agent, or None; for a persona, callers resolve
            it narrowed to the persona's kept knowledge bases. A graph-enabled agent's graph is added to it,
            unless a persona names the bases it keeps.
        model: The model this agent's turn runs, which the caller resolved for the turn.
        provider: ``model``'s provider, when the caller already built it for the turn.
        plugins: The plugin view the caller captured for this operation: the build's tools, placement, skills, and
            tool hooks all come from it, deferred toolkits included.
        include_interactive_questions: Whether to include the interactive
            question authoring prompt. Set to False for channels that do not
            support Matrix reaction-based question flows.
        include_openai_compat_guidance: Whether to include OpenAI-compatible
            history-format guidance in the shared identity prompt.
        disable_runtime_capabilities: Whether to omit tools, skills, knowledge,
            and preloaded context files for a restricted in-process agent run.
        tool_function_filter: Optional policy applied to each fully resolved
            function after normal toolkit construction.
        delegation_depth: Current delegation nesting depth. Used to prevent
            infinite recursion when agents delegate to each other.
        refresh_scheduler: Optional runtime-owned shared knowledge refresh scheduler
            passed through to delegated child agents.
        supports_native_tool_approval: Whether the caller can persist and resume
            confirmation pauses. Gated functions are hidden when false.
        eager_deferred_tools: Whether to send deferred toolkits as ordinary tools instead of through `call_tool`,
            for a runtime that exposes every tool from the start; models with native tool search keep native
            deferral.
        agent_mode: Operating mode frozen by the response owner; standard by default.
        agent_cli_in_shell: Offer `mindroom-agent` inside standard shell commands when the shell can
            reach MindRoom; only callers that bind the response turn to the agent pass True.
        persona: Authored subagent presentation: its authored prompt, which replaces the configured identity, and optional tool, skill, and knowledge base subsets.
        required_tool_names: Authored toolkits needed by a saved approval, which a minimal-mode agent
            attaches directly instead of through mindroom-agent.

    Returns:
        The agent build the agent loop runs.

    Raises:
        ValueError: If agent_name is not found in configuration

    """
    create_runtime_state = not disable_runtime_capabilities
    with _agent_create_timing("resolve_runtime"):
        agent_runtime = resolve_agent_runtime(
            agent_name,
            config,
            runtime_paths,
            execution_identity=execution_identity,
            create=create_runtime_state,
        )

    agent_config = config.get_agent(agent_name)
    if create_runtime_state:
        ensure_default_agent_workspaces(config, runtime_paths.storage_root)

    model_name = model.model_name
    if provider is None:
        provider = _build_agent_model_provider(config, runtime_paths, model_name, execution_identity)
    # An authored persona's prompt replaces the agent's identity; it gets only the caller skills and knowledge bases
    # its persona keeps.
    # An explicit tool list is small and must be present from the first request, so every toolkit it names loads
    # up front; it also hides generated functions without an owning toolkit, other than kept skills and knowledge.
    persona_prompt = persona.system_prompt if persona is not None else None
    persona_tools = persona.tools if persona is not None else None
    eager_deferred_tools = eager_deferred_tools or persona_tools is not None
    native_deferred_tools = (
        agent_mode == "standard" and persona_tools is None and provider.capabilities.native_tool_search
    )
    portable_deferred_tools = agent_mode == "standard" and not eager_deferred_tools and not native_deferred_tools

    agent = config.resolve_agent(agent_name)
    agent_tools = AgentTools(config=config, facts=agent.tools, tools=plugins.tools)
    tool_function_filter, disabled_tool_names = persona_tool_policy(
        persona_tools,
        lambda: caller_toolkit_names(agent_name, config, plugins.tools, delegation_depth=delegation_depth),
        tool_function_filter,
    )
    workspace = agent_runtime.workspace
    output_file_policy = _agent_tool_output_file_policy(
        agent_runtime,
        runtime_paths,
        config.defaults.tool_output_auto_save_threshold_bytes,
    )
    # Approval rules hide generated functions, so skill guidance names the skill functions they hide
    # and is left out when they hide all of them.
    hidden_skill_functions = frozenset(name for name in SKILL_FUNCTION_NAMES if tool_may_require_approval(config, name))
    refused_skills: list[RefusedWorkspaceSkill] = []
    skills = (
        None
        if disable_runtime_capabilities
        else _load_agent_skills(
            agent_name,
            config,
            runtime_paths,
            workspace_root=workspace.root,
            plugin_skill_roots=plugins.skill_roots,
            output_file_policy=output_file_policy,
            only=persona.kept_skills if persona is not None else None,
            refused=refused_skills,
        )
    )
    prompt_skills = None if hidden_skill_functions == SKILL_FUNCTION_NAMES else skills
    # The graph is one more source of the knowledge search, which agents call far more than search_graph.
    # A persona that names its knowledge bases searches only those, as it skips file memory.
    if agent.knowledge_graph and (persona is None or persona.kept_knowledge is None):
        from mindroom.custom_tools.knowledge_graph import with_knowledge_graph  # noqa: PLC0415

        knowledge = with_knowledge_graph(knowledge, agent_name, config, runtime_paths, execution_identity)
    enabled_knowledge = (
        knowledge
        if not disable_runtime_capabilities
        and (persona is None or persona.kept_knowledge != ())
        and not tool_may_require_approval(config, KNOWLEDGE_SEARCH_TOOL_NAME)
        else None
    )
    # Minimal mode presents skill contents as context documents instead of through skill functions.
    offered_skills = skills if agent_mode == "minimal" else prompt_skills
    tool_assembly = _assemble_agent_toolkits(
        agent_tools,
        runtime_paths,
        agent_runtime,
        execution_identity=execution_identity,
        disable_runtime_capabilities=disable_runtime_capabilities,
        disabled_tool_names=disabled_tool_names,
        tool_function_filter=tool_function_filter,
        delegation_depth=delegation_depth,
        refresh_scheduler=refresh_scheduler,
        supports_native_tool_approval=supports_native_tool_approval,
        wire_deferred_tools=native_deferred_tools or portable_deferred_tools,
        portable_deferred_tools=portable_deferred_tools,
        eager_deferred_tools=eager_deferred_tools,
        required_tool_names=required_tool_names,
        minimal_mode=agent_mode == "minimal",
        persona=persona,
        plugins=plugins,
        knowledge=enabled_knowledge,
        skills=offered_skills,
    )

    role_context = _build_agent_role_context(
        agent_name,
        agent_config,
        config,
        runtime_paths,
        agent_runtime,
        model_name=model_name,
        include_openai_compat_guidance=include_openai_compat_guidance,
        disable_runtime_capabilities=disable_runtime_capabilities,
        local_tool_names=tool_assembly.local_tool_names,
        tools=plugins.tools,
        worker_routed_tool_names=tool_assembly.worker_routed_tool_names,
        file_access=agent.file_access,
        include_identity=persona is None,
    )
    deferred_names = tool_assembly.deferred_wire_tool_names
    deferred_tools = DeferredTools(names=deferred_names, portable=portable_deferred_tools and bool(deferred_names))
    logger.info(
        "create_agent",
        agent=agent_name,
        model_provider=provider.identity.provider,
        model_id=provider.identity.id,
        deferred_tool_search=("portable" if deferred_tools.portable else "native") if deferred_names else None,
        deferred_tools=sorted(deferred_names),
    )

    instructions = _build_agent_instructions(
        agent,
        agent_config.instructions if persona is None else (),
        config,
        workspace.root,
        # Only the agent that owns the workspace is told which of its skills did not load, not a persona borrowing them.
        skill_instructions=_skill_usage_instructions(
            config,
            prompt_skills,
            hidden_skill_functions,
            refused_skills if persona is None else (),
        ),
        include_interactive_questions=include_interactive_questions,
        disable_runtime_capabilities=disable_runtime_capabilities,
        native_deferred_tool_names=() if portable_deferred_tools else tool_assembly.deferred_tool_names,
        portable_deferred_toolkits=tool_assembly.deferred_toolkits if portable_deferred_tools else (),
        skill_authoring=persona is None,
    )

    toolkits = tool_assembly.toolkits
    # A standard agent's own shell commands can call its other tools when the shell reaches MindRoom.
    # A subagent runs inside its caller's response, whose CLI belongs to the caller, so its shell gets none.
    # The window only swaps the shell command's entrypoint, so the run offers the same functions.
    windowed_toolkits = (
        wrap_native_shell_window(toolkits)
        if agent_cli_in_shell
        and agent_mode == "standard"
        and delegation_depth == 0
        and not disable_runtime_capabilities
        and standard_cli_eligible(config, runtime_paths, agent_name, execution_identity)
        else None
    )
    cli_shell = windowed_toolkits is not None
    if cli_shell:
        instructions = [*instructions, STANDARD_CLI_NOTE]

    spec = AgentSpec(
        id=agent_name,
        name=agent_config.display_name,
        model=provider,
        system_messages=(),
        toolkits=toolkits if windowed_toolkits is None else windowed_toolkits,
        max_tool_calls_per_turn=agent.max_tool_calls_per_turn,
    )
    prompt = PromptSpec(
        persona=persona_prompt,
        role=role_context.role,
        instructions=tuple(instructions),
        markdown=agent.markdown,
        tool_instructions=tool_assembly.instructions,
        turn_context=render_session_context(config, agent.timezone),
        skills_block=None if offered_skills is None else offered_skills.prompt_block(),
        knowledge_block=None if enabled_knowledge is None else enabled_knowledge.prompt_block(),
    )

    surface = AgentSurface(
        mode=agent_mode,
        cli_shell=cli_shell,
        deferred=tool_assembly.cli_deferred,
        context_documents=tuple(document.body for document in role_context.context_documents),
        context_files=tuple(role_context.workspace_context_files(workspace.root)),
        knowledge_sources=()
        if enabled_knowledge is None
        else tuple(source.description for source in enabled_knowledge.sources),
        output_file_policy=output_file_policy,
    )
    if agent_mode == "minimal":
        # The roster names each toolkit by the registered tool the agent CLI reaches it under.
        # A persona's roster names the toolkits it actually built, deferred ones it named included.
        roster = (
            agent_tools.toolkit_names(agent_tools.candidates())
            if persona is None
            else [*tool_assembly.local_tool_names, *tool_assembly.worker_routed_tool_names]
        )
        surface = replace(
            surface,
            identity=persona_prompt,
            toolkit_names=(*roster, *(deferred.name for deferred in tool_assembly.cli_deferred)),
            memory_root=(
                workspace.file_memory_path.relative_to(workspace.root)
                if workspace.file_memory_path is not None
                else None
            ),
            runtime_context=_get_mind_runtime_context(agent_name, runtime_paths),
            minimal_instructions=tuple(agent_config.minimal_instructions) if persona is None else (),
            interactive_prompt=config.get_prompt("INTERACTIVE_QUESTION_PROMPT")
            if include_interactive_questions
            else "",
            skills=() if offered_skills is None else offered_skills.skills,
        )

    logger.info(
        "Created agent",
        agent=agent_name,
        display_name=agent_config.display_name,
        tool_count=sum(entry.toolkit is not None for entry in tool_assembly.entries),
    )
    # The agent's calls can run as jobs whenever the instance runs them, granted as the agent is now.
    tool_jobs = (
        JobActor(
            name=agent_name,
            team=False,
            depth=delegation_depth,
            tools=assembled_job_tools(tool_assembly.job_sources(), authority_snapshot(config, agent_name)),
        )
        if background_tool_jobs_enabled(config, runtime_paths)
        else None
    )
    return AgentBuild(
        spec=spec,
        prompt=prompt,
        request_template=ModelRequest(messages=(), deferred_tools=deferred_tools),
        history_settings=agent.history_settings,
        tool_policy=bind_live_tools(spec.functions, tool_assembly.tool_middleware),
        tool_jobs=tool_jobs,
        surface=surface,
    )


__all__ = [
    "LeaderJobs",
    "agent_build_can_overlap_file_memory",
    "apply_tool_approval_capability",
    "build_agent_toolkit",
    "create_agent",
    "describe_agent",
    "ensure_default_agent_workspaces",
    "team_background_jobs",
]
