"""Agent loader that reads agent configurations from YAML file."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import mindroom.tools  # noqa: F401
from mindroom import constants
from mindroom.agent_cli.cli_shell_agent import STANDARD_CLI_NOTE, standard_cli_eligible, wrap_native_shell_window
from mindroom.agent_descriptions import describe_agent
from mindroom.agent_loop.build import AgentBuild, AgentSurface
from mindroom.agent_loop.prompt import PromptSpec, model_note_for
from mindroom.agent_loop.spec import AgentSpec
from mindroom.agent_loop.tools import prepare_run_tools
from mindroom.approval.tool_approval import (
    JOB_APPROVAL_TYPE,
    POLICY_CONFIRMATION_APPROVAL_TYPE,
    tool_may_require_approval,
)
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.custom_tools.computer_announcement import computer_announcement_middleware
from mindroom.custom_tools.job import JobTools
from mindroom.delegation.personas import (
    caller_toolkit_names,
    persona_allows,
    persona_tool_policy,
    require_built_persona_tools,
)
from mindroom.entity_resolution import entity_identity_registry
from mindroom.error_handling import MinimalModeUnavailableError, minimal_mode_failure_message
from mindroom.hooks import HookRegistry
from mindroom.knowledge.search import KNOWLEDGE_SEARCH_TOOL_NAME
from mindroom.llm.provider import ModelRequest
from mindroom.logging_config import get_logger
from mindroom.mcp.registry import mcp_tool_name
from mindroom.mcp.toolkit import hide_mcp_function_collisions
from mindroom.path_confinement import read_regular_file_within_root
from mindroom.prompt_templates import build_agent_identity_context, render_prompt_template
from mindroom.providers.registry import build_model_provider
from mindroom.runtime_resolution import (
    ResolvedAgentRuntime,
    resolve_agent_runtime,
)
from mindroom.session_storage import agent_storage
from mindroom.shared_directories.paths import describe_agent_shared_directories
from mindroom.system_prompt import render_date_context, render_session_context
from mindroom.timing import timed, timed_block
from mindroom.tool_jobs.agno_compat_execution import install_tool_job_execution
from mindroom.tool_jobs.agno_execution import approval_can_run_as_job
from mindroom.tool_jobs.authorization import bind_toolkit_authority
from mindroom.tool_jobs.notices import install_job_notice_hook
from mindroom.tool_jobs.settings import background_tool_jobs_enabled, toolkit_is_background_excluded
from mindroom.tool_system.agent_tool_calls import DeferredAgentToolkit
from mindroom.tool_system.agno_toolkits import AdaptedToolkit, adapt_function, adapt_toolkit
from mindroom.tool_system.catalog import (
    TOOL_METADATA,
    ensure_tool_registry_loaded,
    get_tool_by_name,
)
from mindroom.tool_system.construction import ToolConstruction, bind_toolkit_construction, tool_config_signature
from mindroom.tool_system.declarations import (
    MATRIX_ROOM_RUNTIME_APPROVAL_TYPE,
    MATRIX_ROOM_RUNTIME_TOOL_NAMES,
    ToolFileAccess,
)
from mindroom.tool_system.dynamic_toolkits import (
    DeferredToolkit,
    VisibleToolSurface,
    defer_toolkit,
    drop_shadowed_deferred_functions,
    visible_tool_surface,
)
from mindroom.tool_system.output_files import ToolOutputFilePolicy, wrap_toolkit_for_output_files
from mindroom.tool_system.plugins import load_plugins
from mindroom.tool_system.runtime_context import ToolDispatchContext
from mindroom.tool_system.sandbox_proxy import sandbox_proxy_enabled_for_tool
from mindroom.tool_system.skills import SKILL_FUNCTION_NAMES, agent_workspace_skills_root, build_skills_toolkit
from mindroom.tool_system.tool_hooks import build_tool_hook_bridge, build_tool_hook_middleware, prepend_tool_hook_bridge
from mindroom.tool_system.worker_routing import (
    agent_workspace_root_path,
    build_agent_toolkit_worker_target,
    resolve_agent_owned_path,
    shared_storage_root,
)
from mindroom.workers.runtime import primary_worker_backend_name
from mindroom.workspaces import ensure_workspace_template

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from contextlib import AbstractContextManager

    from agno.models.base import Model
    from agno.tools.function import Function
    from agno.tools.toolkit import Toolkit

    from mindroom.agent_modes import AgentMode
    from mindroom.config.agent import AgentConfig
    from mindroom.config.main import Config
    from mindroom.config.models import EffectiveToolConfig, FileAccess
    from mindroom.credentials.manager import CredentialsManager
    from mindroom.delegation.state import SubagentPersona
    from mindroom.hooks import HookRegistryPlugin
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.knowledge.search import AgentKnowledge
    from mindroom.llm.provider import ModelProvider
    from mindroom.session_storage.store import ConversationStore
    from mindroom.tool_system.invocation import ToolMiddleware
    from mindroom.tool_system.skills import RefusedWorkspaceSkill, SkillsToolkit
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity, WorkerScope

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
class _AdditionalContextChunk:
    """Chunk of preload context with omission metadata.

    ``omitted_chars`` records what the preload cap removed from this chunk so
    the rendered section can tell the model which file was cut and by how much,
    instead of letting a dropped file vanish without a trace.
    """

    title: str
    body: str
    omitted_chars: int = 0


@dataclass(frozen=True)
class _AgentToolAssembly:
    """Toolkits and dynamic-tool visibility assembled for one agent instance."""

    tools: list[Toolkit]
    # Toolkits whose surviving functions stay off the provider request until tool search finds them.
    # Keep the owners rather than a pre-projection name snapshot because final
    # MCP collision projection may remove a deferred function while preserving
    # an eager local function with the same wire name.
    deferred_toolkits: tuple[DeferredToolkit, ...]
    local_tool_names: tuple[str, ...]
    worker_routed_tool_names: tuple[str, ...]
    cli_deferred: tuple[DeferredAgentToolkit, ...]
    # The tool-call policy the loop runs around every call: the hook bridge, then each browser toolkit's
    # computer announcement.
    tool_middleware: tuple[ToolMiddleware, ...]

    @property
    def deferred_tool_names(self) -> tuple[str, ...]:
        """Return authored deferred-tool domains with a surviving function surface."""
        return tuple(
            dict.fromkeys(deferred.domain_name for deferred in self.deferred_toolkits if deferred.wire_function_names),
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
    context_documents: tuple[_AdditionalContextChunk, ...]

    def workspace_context_files(self, workspace_root: Path | None) -> list[str]:
        """List loaded context files that Bash can read relative to its workspace."""
        if workspace_root is None:
            return []
        paths = (Path(document.title) for document in self.context_documents)
        return [path.relative_to(workspace_root).as_posix() for path in paths if path.is_relative_to(workspace_root)]


def show_tool_calls_for_agent(config: Config, agent_name: str) -> bool:
    """Resolve tool-call visibility for one agent from current config."""
    agent_config = config.agents.get(agent_name)
    if agent_config and agent_config.show_tool_calls is not None:
        return agent_config.show_tool_calls
    return config.defaults.show_tool_calls


def _uses_default_mind_workspace_scaffold(agent_name: str, agent_config: AgentConfig) -> bool:
    return (
        agent_name == _DEFAULT_MIND_AGENT_NAME
        and agent_config.private is None
        and agent_config.memory_backend == "file"
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
    for agent_name, agent_config in config.agents.items():
        if _uses_default_mind_workspace_scaffold(agent_name, agent_config):
            _ensure_default_mind_workspace(storage_path)


def agent_build_can_overlap_file_memory(agent_name: str, config: Config, storage_path: Path) -> bool:
    """Return True when it is safe to overlap agent construction with file-memory reads."""
    agent_config = config.get_agent(agent_name)
    if not _uses_default_mind_workspace_scaffold(agent_name, agent_config):
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
    workspace_root: Path | None = None,
) -> list[_AdditionalContextChunk]:
    """Load configured context files; ``Path`` entries and agent-owned strings are workspace files."""
    loaded_parts: list[_AdditionalContextChunk] = []
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
            _AdditionalContextChunk(
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


def _render_context_chunk(chunk: _AdditionalContextChunk, *, chunk_marker_template: str) -> str:
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
    chunks: list[_AdditionalContextChunk],
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
    chunks: list[_AdditionalContextChunk],
    max_preload_chars: int,
    *,
    render: Callable[[list[_AdditionalContextChunk]], str],
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
    chunks: list[_AdditionalContextChunk],
    max_preload_chars: int,
    *,
    render: Callable[[list[_AdditionalContextChunk]], str],
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
    personality_chunks: list[_AdditionalContextChunk],
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


@timed("system_prompt_assembly.agent_create.additional_context")
def _build_additional_context(
    agent_name: str,
    agent_config: AgentConfig,
    max_preload_chars: int,
    *,
    personality_section_heading: str,
    truncation_marker_template: str,
    chunk_marker_template: str,
    workspace_context_files: tuple[Path, ...] = (),
    workspace_root: Path | None = None,
    context_documents: list[_AdditionalContextChunk],
    storage_path: Path,
    runtime_paths: constants.RuntimePaths,
) -> str:
    """Build additional role context from configured files/directories.

    This is evaluated each time one agent instance is created.
    The normal Matrix and OpenAI-compatible request paths build fresh agent
    instances per reply/request, so edits in the canonical agent workspace are
    reflected on the next reply without a process restart.
    """
    personality_chunks: list[_AdditionalContextChunk] = []
    context_files: list[Path | str] = [*agent_config.context_files, *workspace_context_files]
    if context_files:
        personality_chunks = _load_context_files(
            context_files,
            runtime_paths,
            agent_name,
            storage_path,
            workspace_root,
        )

    # Preload truncation mutates chunks; keep complete files for CLI reads.
    context_documents.extend(replace(chunk) for chunk in personality_chunks)

    additional_context, omitted_chars = _apply_preload_cap(
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
    return additional_context


def _file_mode_knowledge_instruction_block(
    agent_name: str,
    config: Config,
    agent_runtime: ResolvedAgentRuntime,
) -> str | None:
    """Describe workspace-accessible file-only knowledge bases to the model."""
    workspace = agent_runtime.workspace
    if workspace is None:
        return None

    lines = [
        "File-only knowledge bases are available in the workspace.",
        "Use file or coding tools such as grep, list, and read instead of search_knowledge_base for these sources.",
        "Available file-only sources:",
    ]
    source_lines: list[str] = []
    for base_id in config.resolve_entity(agent_name).knowledge_base_ids:
        base_config = config.get_knowledge_base_config(base_id)
        if base_config.mode != "files":
            continue
        relative_path = Path("knowledge") / base_id
        resolved_path = workspace.root / relative_path
        if not resolved_path.exists():
            continue
        description = " ".join(base_config.description.split()) or "No description configured."
        source_lines.append(f"- {base_id}: `{relative_path.as_posix()}`. {description}")
    if not source_lines:
        return None
    return "\n".join([*lines, *source_lines])


def _tool_supports_base_dir(tool_name: str) -> bool:
    """Return whether a registered tool exposes a base_dir config field."""
    metadata = TOOL_METADATA.get(tool_name)
    if metadata is None or not metadata.config_fields:
        return False
    return any(field.name == "base_dir" for field in metadata.config_fields)


def _tool_base_dir_override(
    tool_name: str,
    *,
    workspace_path: Path | None,
) -> dict[str, object] | None:
    """Build per-agent tool overrides for workspace-aware local tools."""
    if workspace_path is None or not _tool_supports_base_dir(tool_name):
        return None
    return {"base_dir": str(workspace_path)}


def _build_registered_agent_tool(
    tool_name: str,
    runtime_paths: constants.RuntimePaths,
    credentials_manager: CredentialsManager,
    shared_storage_path: Path,
    worker_tools: list[str],
    worker_scope: WorkerScope | None,
    allowed_shared_services: frozenset[str] | None,
    agent_name: str,
    tool_config_overrides: dict[str, object] | None,
    workspace_path: Path | None,
    agent_state_root: Path,
    tool_output_auto_save_threshold_bytes: int,
    routing_agent_is_private: bool,
    execution_identity: ToolExecutionIdentity | None,
    runtime_overrides: dict[str, object] | None,
    config: Config,
) -> Toolkit:
    """Build one registered toolkit using the resolved routing inputs for this agent."""
    worker_target = build_agent_toolkit_worker_target(
        worker_scope,
        agent_name,
        is_private=routing_agent_is_private,
        execution_identity=execution_identity,
        runtime_paths=runtime_paths,
    )

    return get_tool_by_name(
        tool_name,
        runtime_paths,
        credentials_manager=credentials_manager,
        runtime_config=config,
        tool_config_overrides=tool_config_overrides,
        tool_init_overrides=_tool_base_dir_override(
            tool_name,
            workspace_path=workspace_path,
        ),
        runtime_overrides=runtime_overrides,
        shared_storage_root_path=shared_storage_path,
        worker_tools_override=worker_tools,
        allowed_shared_services=allowed_shared_services,
        tool_output_workspace_root=workspace_path,
        agent_state_root=agent_state_root,
        tool_output_auto_save_threshold_bytes=tool_output_auto_save_threshold_bytes,
        worker_target=worker_target,
    )


def _log_toolkits_without_unique_model_functions(
    toolkits: list[Toolkit],
    *,
    agent_name: str,
) -> None:
    """Warn when a configured toolkit offers no function of its own because earlier toolkits took every name."""
    seen_function_names: set[str] = set()
    for toolkit in toolkits:
        function_names = set(toolkit.get_async_functions())
        if function_names and function_names <= seen_function_names:
            logger.warning(
                "Configured toolkit exposes no unique model functions because function names collide",
                agent=agent_name,
                toolkit=toolkit.name,
                function_names=sorted(function_names),
            )
        seen_function_names.update(function_names)


def _hide_session_mcp_function_collisions(toolkits: list[Toolkit], *, agent_name: str) -> dict[str, tuple[str, ...]]:
    """Project MCP functions against the exact session-local tool surface, returning those hidden per tool."""
    hidden = hide_mcp_function_collisions(toolkits)
    for server_id, function_names in hidden.items():
        logger.warning(
            "Hiding colliding MCP functions from session tool surface",
            agent=agent_name,
            server_id=server_id,
            function_names=list(function_names),
        )
    return {mcp_tool_name(server_id): function_names for server_id, function_names in hidden.items()}


class _MatrixRoomRuntimeToolCollisionError(ValueError):
    """Raised when another registered tool claims a reserved runtime function."""


def _reject_matrix_room_runtime_tool_function_collisions(
    registered_tool_name: str,
    toolkit: Toolkit,
) -> None:
    """Keep reserved room-runtime functions owned by their registered built-in tool."""
    reserved_names = set(MATRIX_ROOM_RUNTIME_TOOL_NAMES)
    function_names = set(toolkit.get_functions()) | set(toolkit.get_async_functions())
    collisions = sorted(name for name in function_names & reserved_names if registered_tool_name != name)
    if collisions:
        msg = f"Tool function name(s) {', '.join(collisions)} are reserved for Matrix room recovery tools."
        raise _MatrixRoomRuntimeToolCollisionError(msg)


def _agent_tool_output_file_policy(
    agent_runtime: ResolvedAgentRuntime,
    runtime_paths: constants.RuntimePaths,
    tool_output_auto_save_threshold_bytes: int,
) -> ToolOutputFilePolicy | None:
    """Resolve the shared output-file policy for one agent's in-process tools."""
    if agent_runtime.tool_base_dir is None:
        return None
    return ToolOutputFilePolicy.from_runtime(
        agent_runtime.tool_base_dir,
        runtime_paths,
        auto_save_threshold_bytes=tool_output_auto_save_threshold_bytes,
    )


def _wrap_direct_agent_toolkit_for_output_files(
    toolkit: Toolkit,
    *,
    tool_name: str,
    tool_config_overrides: dict[str, object] | None,
    agent_runtime: ResolvedAgentRuntime,
    runtime_paths: constants.RuntimePaths,
    tool_output_auto_save_threshold_bytes: int,
) -> Toolkit:
    """Apply the central output-file wrapper to MindRoom-owned direct toolkits."""
    policy = _agent_tool_output_file_policy(
        agent_runtime,
        runtime_paths,
        tool_output_auto_save_threshold_bytes,
    )
    return bind_toolkit_construction(
        wrap_toolkit_for_output_files(toolkit, policy),
        ToolConstruction(tool_name, None, tool_config_signature(tool_config_overrides)),
    )


@timed("system_prompt_assembly.agent_create.model_instance")
def _build_agent_model_provider(
    config: Config,
    runtime_paths: constants.RuntimePaths,
    model_name: str,
    execution_identity: ToolExecutionIdentity | None,
) -> ModelProvider:
    """Build one agent's model provider while preserving prompt-assembly timing attribution."""
    return build_model_provider(config, runtime_paths, model_name, execution_identity=execution_identity)


@timed("system_prompt_assembly.agent_create.toolkit_build")
def build_agent_toolkit(  # noqa: C901, PLR0911, PLR0912
    tool_name: str,
    *,
    agent_name: str,
    config: Config,
    runtime_paths: constants.RuntimePaths,
    worker_tools: list[str],
    runtime_overrides: dict[str, object] | None,
    agent_runtime: ResolvedAgentRuntime | None = None,
    tool_config_overrides: dict[str, object] | None = None,
    execution_identity: ToolExecutionIdentity | None,
    delegation_depth: int = 0,
    refresh_scheduler: KnowledgeRefreshScheduler | None = None,
    persona: SubagentPersona | None = None,
    deferred_toolkits: Sequence[DeferredToolkit] = (),
) -> Toolkit | None:
    """Build one configured toolkit for an agent.

    ``persona`` is an authored subagent's persona; its ``delegate`` toolkit keeps
    the copies it authors within the persona's tools, skills, and knowledge bases.

    Callers own runtime override resolution before invoking this builder.
    Returns ``None`` when the configured tool should be skipped, such as an
    explicit ``delegate`` entry without valid delegation targets.

    ``deferred_toolkits`` are the wire-deferred toolkits that ``dynamic_tools`` searches.
    """
    agent_config = config.get_agent(agent_name)
    if agent_runtime is None:
        agent_runtime = resolve_agent_runtime(
            agent_name,
            config,
            runtime_paths,
            execution_identity=execution_identity,
            create=True,
        )
    storage_path = runtime_paths.storage_root
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    shared_storage_path = shared_storage_root(storage_path)
    # Every MindRoom-owned direct toolkit gets the same output-file wrapping and construction identity.
    wrap_direct = partial(
        _wrap_direct_agent_toolkit_for_output_files,
        tool_name=tool_name,
        tool_config_overrides=tool_config_overrides,
        agent_runtime=agent_runtime,
        runtime_paths=runtime_paths,
        tool_output_auto_save_threshold_bytes=config.defaults.tool_output_auto_save_threshold_bytes,
    )

    if tool_name == "memory":
        if config.resolve_entity(agent_name).memory_backend == "none":
            return None

        from mindroom.custom_tools.memory import MemoryTools  # noqa: PLC0415

        # MemoryTools resolves the canonical per-agent storage roots internally via the
        # shared memory facade, so it should receive the caller-visible runtime root here.
        return wrap_direct(
            MemoryTools(
                agent_name=agent_name,
                storage_path=storage_path,
                config=config,
                runtime_paths=runtime_paths,
                execution_identity=execution_identity,
            ),
        )

    if tool_name == "knowledge_graph":
        if not config.agent_uses_knowledge_graph(agent_name):
            return None

        from mindroom.custom_tools.knowledge_graph import KnowledgeGraphTools  # noqa: PLC0415

        return wrap_direct(KnowledgeGraphTools(agent_name, config, runtime_paths, execution_identity))

    if tool_name == "delegate":
        # Imported lazily to avoid a circular import through DelegateTools -> create_agent.
        from mindroom.custom_tools import delegate  # noqa: PLC0415
        from mindroom.delegation.lifecycle import MAX_DELEGATION_DEPTH  # noqa: PLC0415

        if not agent_config.delegate_to:
            logger.warning(
                "Skipping delegate tool because delegate_to is empty",
                agent=agent_name,
            )
            return None
        if delegation_depth >= MAX_DELEGATION_DEPTH:
            logger.warning(
                "Skipping delegate tool because delegation depth limit was reached",
                agent=agent_name,
                delegation_depth=delegation_depth,
                max_delegation_depth=MAX_DELEGATION_DEPTH,
            )
            return None
        return wrap_direct(
            delegate.DelegateTools(
                agent_name=agent_name,
                delegate_to=agent_config.delegate_to,
                runtime_paths=runtime_paths,
                config=config,
                execution_identity=execution_identity,
                delegation_depth=delegation_depth,
                refresh_scheduler=refresh_scheduler,
                workspace_root=agent_runtime.workspace.root if agent_runtime.workspace is not None else None,
                persona=persona,
            ),
        )

    if tool_name == "self_config":
        from mindroom.custom_tools.self_config import SelfConfigTools  # noqa: PLC0415

        return wrap_direct(
            SelfConfigTools(agent_name=agent_name, runtime_paths=runtime_paths),
        )

    if tool_name == "skill_manage":
        from mindroom.custom_tools.skill_manage import SkillManageTools  # noqa: PLC0415

        # It keeps its own output handling, but background tool jobs still need its construction identity.
        return bind_toolkit_construction(
            SkillManageTools(
                agent_name,
                config,
                runtime_paths,
                agent_workspace_skills_root(
                    runtime_paths,
                    agent_name,
                    workspace_root=agent_runtime.workspace.root if agent_runtime.workspace is not None else None,
                ),
            ),
            ToolConstruction(tool_name, None, tool_config_signature(tool_config_overrides)),
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

        if not deferred_toolkits:
            return None
        # Search results must reach the model inline, so they skip output-file redirection.
        return bind_toolkit_construction(
            DynamicToolsToolkit(deferred_toolkits=deferred_toolkits),
            ToolConstruction(tool_name, None, tool_config_signature(tool_config_overrides)),
        )

    return _build_registered_agent_tool(
        tool_name,
        runtime_paths,
        credentials_manager,
        shared_storage_path,
        worker_tools,
        agent_runtime.execution.execution_scope,
        (config.get_worker_grantable_credentials() if agent_runtime.execution.execution_scope is not None else None),
        agent_name,
        tool_config_overrides,
        agent_runtime.tool_base_dir,
        agent_runtime.state_root,
        config.defaults.tool_output_auto_save_threshold_bytes,
        agent_runtime.execution.is_private,
        execution_identity,
        runtime_overrides,
        config,
    )


def get_agent_toolkit_names(
    agent_name: str,
    config: Config,
    *,
    delegation_depth: int = 0,
) -> list[str]:
    """Return every toolkit a new agent runtime can call, deferred ones included."""
    return [
        entry.name
        for entry in visible_tool_surface(
            agent_name=agent_name,
            config=config,
            delegation_depth=delegation_depth,
        ).runtime_tool_configs
    ]


def resolve_runtime_worker_tools(
    agent_name: str,
    config: Config,
    runtime_paths: constants.RuntimePaths,
    runtime_tool_names: list[str],
    *,
    tool_registry_preloaded: bool = False,
) -> list[str]:
    """Return worker-routed tools for one concrete runtime tool selection."""
    configured = config.get_agent_worker_tools(agent_name)
    if configured is not None:
        return configured

    if not tool_registry_preloaded:
        ensure_tool_registry_loaded(runtime_paths, config)
    return [
        tool_name
        for tool_name in runtime_tool_names
        if sandbox_proxy_enabled_for_tool(tool_name, runtime_paths=runtime_paths)
    ]


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

    backend = primary_worker_backend_name(runtime_paths)
    lines = [
        "## Tool Execution Environment",
        f"- Local tools (primary MindRoom runtime): {tool_list(local_tool_names)}.",
        f"- Worker-routed tools: {tool_list(worker_routed_tool_names)}.",
        f"- Worker backend: `{backend}`.",
    ]
    if backend == "static_runner":
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
            "docker": "the container stops",
            "kubernetes": "the deployment scales to zero",
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


def _unconfined_tool_names(tool_names: tuple[str, ...], *, requires_primary_runtime: bool) -> tuple[str, ...]:
    """Return sorted tools not confined by file_access, split by whether a worker can isolate them."""
    return tuple(
        sorted(
            name
            for name in tool_names
            if name in TOOL_METADATA
            and TOOL_METADATA[name].file_access is ToolFileAccess.UNCONFINED
            and TOOL_METADATA[name].requires_primary_runtime is requires_primary_runtime
        ),
    )


def _registry_tool_routes_through_worker(
    tool_name: str,
    *,
    runtime_paths: constants.RuntimePaths,
    worker_tools: list[str],
) -> bool:
    """Return whether one successfully built registry toolkit uses worker routing."""
    metadata = TOOL_METADATA.get(tool_name)
    return (
        metadata is not None
        and metadata.factory is not None
        and sandbox_proxy_enabled_for_tool(
            tool_name,
            runtime_paths=runtime_paths,
            worker_tools_override=worker_tools,
        )
    )


def _context_hidden_toolkits(execution_identity: ToolExecutionIdentity | None) -> frozenset[str]:
    if execution_identity is None or execution_identity.room_id is not None:
        return frozenset()
    return frozenset(tool_name for tool_name, metadata in TOOL_METADATA.items() if metadata.requires_room_context)


def _build_dynamic_tooling_instruction_block(
    config: Config,
    deferred_toolkits: Sequence[DeferredToolkit],
) -> str | None:
    """Return the static deferred-tool catalog for portable tool search."""
    catalog = {
        deferred.domain_name: deferred.description.strip()
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


def _build_native_tool_search_instruction_blocks(
    config: Config,
    deferred_tool_names: tuple[str, ...],
) -> tuple[str, ...]:
    """Return compact discovery guidance for provider-native deferred tools."""
    if not deferred_tool_names:
        return ()
    return (
        config.render_prompt(
            "NATIVE_TOOL_SEARCH_INSTRUCTION_TEMPLATE",
            tool_domains=", ".join(deferred_tool_names),
        ),
    )


@timed("system_prompt_assembly.agent_create.load_plugins")
def _load_agent_plugins(config: Config, runtime_paths: constants.RuntimePaths) -> list[HookRegistryPlugin]:
    return cast("list[HookRegistryPlugin]", load_plugins(config, runtime_paths))


@timed("system_prompt_assembly.agent_create.tool_registry_sync")
def _sync_agent_tool_registry(config: Config, runtime_paths: constants.RuntimePaths) -> None:
    ensure_tool_registry_loaded(runtime_paths, config, load_plugin_tools=False)


@timed("system_prompt_assembly.agent_create.hook_bridge")
def _build_agent_tool_hook_bridge(
    *,
    hook_registry: HookRegistry | None,
    plugins: list[HookRegistryPlugin],
    agent_name: str,
    dispatch_context: ToolDispatchContext | None,
    config: Config,
    runtime_paths: constants.RuntimePaths,
) -> Callable[..., Any]:
    """Return the tool-call policy as the Agno tool hook the team path still prepends to its job controls."""
    active_hook_registry = hook_registry if hook_registry is not None else HookRegistry.from_plugins(plugins)
    return build_tool_hook_bridge(
        active_hook_registry,
        agent_name=agent_name,
        dispatch_context=dispatch_context,
        config=config,
        runtime_paths=runtime_paths,
    )


@timed("system_prompt_assembly.agent_create.hook_bridge")
def _build_agent_tool_middleware(
    *,
    hook_registry: HookRegistry | None,
    plugins: list[HookRegistryPlugin],
    agent_name: str,
    dispatch_context: ToolDispatchContext | None,
    config: Config,
    runtime_paths: constants.RuntimePaths,
) -> ToolMiddleware:
    """Return the tool-call policy as the middleware the owned loop runs around every call of this agent."""
    active_hook_registry = hook_registry if hook_registry is not None else HookRegistry.from_plugins(plugins)
    return build_tool_hook_middleware(
        active_hook_registry,
        agent_name=agent_name,
        dispatch_context=dispatch_context,
        config=config,
        runtime_paths=runtime_paths,
    )


def _prune_toolkit_functions(
    toolkit: Toolkit,
    tool_function_filter: Callable[[Function], bool] | None,
) -> Toolkit | None:
    """Apply the final function policy and discard toolkits with no callable surface."""
    if tool_function_filter is not None:
        toolkit.functions = {
            name: function for name, function in toolkit.functions.items() if tool_function_filter(function)
        }
        toolkit.async_functions = {
            name: function for name, function in toolkit.async_functions.items() if tool_function_filter(function)
        }
    return toolkit if toolkit.functions or toolkit.async_functions else None


def apply_tool_approval_capability(
    toolkit: Toolkit | None,
    config: Config,
    *,
    supports_native_tool_approval: bool,
    registered_tool_name: str | None = None,
    approvals_as_jobs: bool = False,
) -> Toolkit | None:
    """Expose gated functions only where a paused run can be resumed, or where their call's job asks first.

    With ``approvals_as_jobs``, a gated function whose call can become a managed job asks for its approval as that
    job's first phase instead of pausing the run.
    """
    if toolkit is None:
        return None

    def function_may_require_approval(function: Function) -> bool:
        is_matrix_room_runtime_function = (
            registered_tool_name == function.name
            and function.name in MATRIX_ROOM_RUNTIME_TOOL_NAMES
            and function.approval_type == MATRIX_ROOM_RUNTIME_APPROVAL_TYPE
        )
        # A scheduled call runs only with the approval its requester gave when it was scheduled.
        is_scheduled_call_runner = registered_tool_name == "scheduler" and function.name == "run_scheduled_call"
        return (
            not is_matrix_room_runtime_function
            and not is_scheduled_call_runner
            and tool_may_require_approval(config, function.name)
        )

    if supports_native_tool_approval:
        for function in (*toolkit.functions.values(), *toolkit.async_functions.values()):
            if registered_tool_name == "delegate" and function.name in {"run_subagent", "continue_subagent"}:
                # The delegation driver owns the policy gate and exact child wait.
                function.external_execution = True
                function.external_execution_silent = True
                function.requires_confirmation = False
                continue
            if function_may_require_approval(function) and function.requires_confirmation is not True:
                if approvals_as_jobs and approval_can_run_as_job(function, toolkit):
                    function.approval_type = JOB_APPROVAL_TYPE
                    continue
                function.requires_confirmation = True
                function.approval_type = POLICY_CONFIRMATION_APPROVAL_TYPE
        return toolkit
    toolkit.functions = {
        name: function
        for name, function in toolkit.functions.items()
        if function.requires_confirmation is not True and not function_may_require_approval(function)
    }
    toolkit.async_functions = {
        name: function
        for name, function in toolkit.async_functions.items()
        if function.requires_confirmation is not True and not function_may_require_approval(function)
    }
    return toolkit if toolkit.functions or toolkit.async_functions else None


@timed("system_prompt_assembly.agent_create.dynamic_tool_selection")
def _resolve_agent_dynamic_tool_selection(
    *,
    agent_name: str,
    config: Config,
    delegation_depth: int,
    minimal_mode: bool,
    eager_deferred_tools: bool,
    portable_deferred_tools: bool,
    include_matrix_room_runtime_tools: bool,
    required_tool_names: tuple[str, ...],
) -> VisibleToolSurface:
    # Minimal mode calls deferred toolkits through mindroom-agent, so it attaches only initial ones and the toolkits a
    # saved approval needs; every other runtime attaches them all. Native tool search sends deferred ones marked as
    # deferred, portable tool search keeps them off the wire, and personas with their own tool list, plus voice calls
    # on models without native tool search, send them as ordinary tools.
    return visible_tool_surface(
        agent_name=agent_name,
        config=config,
        deferred_tools=required_tool_names if minimal_mode and not eager_deferred_tools else None,
        delegation_depth=delegation_depth,
        enable_dynamic_tools_manager=portable_deferred_tools,
        include_matrix_room_runtime_tools=include_matrix_room_runtime_tools,
    )


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
        openai_context = render_prompt_template(
            config.get_prompt("OPENAI_COMPAT_AGENT_IDENTITY_CONTEXT_TEMPLATE"),
            agent_name=agent_name,
            display_name=display_name,
            model_provider=model_provider,
            model_id=model_id,
            openai_compat_history_guidance=openai_compat_history_guidance,
        )
        has_custom_matrix_identity = "AGENT_IDENTITY_CONTEXT_TEMPLATE" in config.prompts
        has_custom_openai_identity = "OPENAI_COMPAT_AGENT_IDENTITY_CONTEXT_TEMPLATE" in config.prompts
        if not has_custom_matrix_identity or has_custom_openai_identity:
            return openai_context
        overridden_identity_context = render_prompt_template(
            config.get_prompt("AGENT_IDENTITY_CONTEXT_TEMPLATE"),
            display_name=display_name,
            matrix_id="not available in OpenAI-compatible API",
            model_provider=model_provider,
            model_id=model_id,
            openai_compat_history_guidance=openai_compat_history_guidance,
        )
        return overridden_identity_context + openai_context

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


@timed("system_prompt_assembly.agent_create.skills_load")
def _load_agent_skills(
    agent_name: str,
    config: Config,
    runtime_paths: constants.RuntimePaths,
    *,
    workspace_root: Path | None = None,
    output_file_policy: ToolOutputFilePolicy | None = None,
    only: tuple[str, ...] | None = None,
    refused: list[RefusedWorkspaceSkill] | None = None,
) -> SkillsToolkit | None:
    return build_skills_toolkit(
        agent_name,
        config,
        runtime_paths,
        workspace_root=workspace_root,
        output_file_policy=output_file_policy,
        only=only,
        refused=refused,
    )


def _generated_function_visible(
    config: Config,
    tool_function_filter: Callable[[Function], bool] | None,
    function: Function,
) -> bool:
    """Hide generated functions an approval rule may gate, because they have no toolkit origin to pause and resume."""
    return not tool_may_require_approval(config, function.name) and (
        tool_function_filter is None or tool_function_filter(function)
    )


def _agent_create_timing(label: str, **event_data: object) -> AbstractContextManager[None]:
    return timed_block(f"system_prompt_assembly.agent_create.{label}", scope=None, **event_data)


def _keep_persona_functions(toolkit: Toolkit, tool_name: str, persona_tools: tuple[str, ...]) -> None:
    """Drop the functions of one concrete toolkit that an authored persona does not name."""
    for functions in (toolkit.functions, toolkit.async_functions):
        for name in [name for name in functions if not persona_allows(persona_tools, tool_name, name)]:
            del functions[name]


def _set_toolkit_approval_origin(toolkit: Toolkit, authored_name: str) -> None:
    """Attach the configured toolkit identity to its executable functions."""
    for function in (*toolkit.functions.values(), *toolkit.get_async_functions().values()):
        function.owning_toolkit = authored_name


def _assemble_agent_toolkits(  # noqa: C901, PLR0912, PLR0915 - eager and deferred tools share one construction path
    agent_name: str,
    config: Config,
    runtime_paths: constants.RuntimePaths,
    agent_runtime: ResolvedAgentRuntime,
    *,
    execution_identity: ToolExecutionIdentity | None,
    hook_registry: HookRegistry | None,
    disable_runtime_capabilities: bool,
    disabled_tool_names: frozenset[str],
    tool_function_filter: Callable[[Function], bool] | None,
    delegation_depth: int,
    refresh_scheduler: KnowledgeRefreshScheduler | None,
    supports_native_tool_approval: bool,
    wire_deferred_tools: bool,
    portable_deferred_tools: bool,
    eager_deferred_tools: bool,
    required_tool_names: tuple[str, ...],
    minimal_mode: bool,
    persona: SubagentPersona | None,
) -> _AgentToolAssembly:
    """Assemble runtime toolkits and the dynamic-tool visibility for one agent instance."""
    approvals_as_jobs = (
        background_tool_jobs_enabled(config, runtime_paths) and delegation_depth == 0 and not minimal_mode
    )
    persona_tools = persona.tools if persona is not None else None
    plugins = _load_agent_plugins(config, runtime_paths)
    _sync_agent_tool_registry(config, runtime_paths)
    tool_middleware: list[ToolMiddleware] = [
        _build_agent_tool_middleware(
            hook_registry=hook_registry,
            plugins=plugins,
            agent_name=agent_name,
            dispatch_context=(
                ToolDispatchContext(execution_identity=execution_identity) if execution_identity is not None else None
            ),
            config=config,
            runtime_paths=runtime_paths,
        ),
    ]
    include_matrix_room_runtime_tools = (
        execution_identity is not None
        and execution_identity.channel == "matrix"
        and execution_identity.room_id is not None
    )
    dynamic_tool_selection = _resolve_agent_dynamic_tool_selection(
        agent_name=agent_name,
        config=config,
        delegation_depth=delegation_depth,
        minimal_mode=minimal_mode,
        eager_deferred_tools=eager_deferred_tools,
        portable_deferred_tools=portable_deferred_tools,
        include_matrix_room_runtime_tools=include_matrix_room_runtime_tools,
        required_tool_names=required_tool_names,
    )
    hidden_toolkits = _context_hidden_toolkits(execution_identity)
    resolved_tool_configs = {entry.name: entry for entry in dynamic_tool_selection.runtime_tool_configs}
    if disable_runtime_capabilities:
        resolved_tool_configs = {}
    elif disabled_tool_names:
        resolved_tool_configs = {
            tool_name: entry
            for tool_name, entry in resolved_tool_configs.items()
            if tool_name not in disabled_tool_names
        }
    if hidden_toolkits:
        resolved_tool_configs = {
            tool_name: entry for tool_name, entry in resolved_tool_configs.items() if tool_name not in hidden_toolkits
        }
    deferred_configs = {}
    if minimal_mode:
        complete = visible_tool_surface(
            agent_name=agent_name,
            config=config,
            delegation_depth=delegation_depth,
            include_matrix_room_runtime_tools=include_matrix_room_runtime_tools,
        )
        deferred_configs = {
            entry.name: entry
            for entry in complete.runtime_tool_configs
            if entry.name not in resolved_tool_configs
            and entry.name not in hidden_toolkits
            and entry.name not in disabled_tool_names
        }
    with _agent_create_timing("resolve_worker_tools"):
        worker_tools = resolve_runtime_worker_tools(
            agent_name,
            config,
            runtime_paths,
            [*resolved_tool_configs, *deferred_configs],
            tool_registry_preloaded=True,
        )
    entity_view = config.resolve_entity(agent_name)
    tools: list[Toolkit] = []
    local_tool_names: list[str] = []
    worker_routed_tool_names: list[str] = []
    deferred_toolkits: list[DeferredToolkit] = []
    persona_built: dict[str, frozenset[str]] = {}

    def build_entry(tool_entry: EffectiveToolConfig) -> Toolkit | None:
        tool_name = tool_entry.name
        runtime_overrides = entity_view.tool_runtime_overrides(tool_name)
        with _agent_create_timing("toolkit_build.one", tool_name=tool_name):
            toolkit = build_agent_toolkit(
                tool_name,
                agent_name=agent_name,
                config=config,
                runtime_paths=runtime_paths,
                worker_tools=worker_tools,
                runtime_overrides=runtime_overrides,
                agent_runtime=agent_runtime,
                tool_config_overrides=tool_entry.tool_config_overrides,
                execution_identity=execution_identity,
                delegation_depth=delegation_depth,
                refresh_scheduler=refresh_scheduler,
                persona=persona,
                deferred_toolkits=tuple(deferred_toolkits),
            )
        if toolkit:
            _reject_matrix_room_runtime_tool_function_collisions(tool_name, toolkit)
            if persona_tools is not None:
                # Match the concrete toolkit, which a preset or implied tool reaches under its authored name.
                _keep_persona_functions(toolkit, tool_name, persona_tools)
            # Function policies match on the owning toolkit, so stamp it before pruning.
            _set_toolkit_approval_origin(toolkit, tool_entry.authored_name or tool_name)
            toolkit = _prune_toolkit_functions(toolkit, tool_function_filter)
            if toolkit is not None and persona_tools is not None:
                # Record what survives the caller's filters; approval capability below may still hide gated calls.
                persona_built[tool_name] = frozenset((*toolkit.functions, *toolkit.async_functions))
        toolkit = apply_tool_approval_capability(
            toolkit,
            config,
            supports_native_tool_approval=supports_native_tool_approval,
            registered_tool_name=tool_name,
            # Only a top-level agent's own calls ask as their job; the same setting installs the job executor.
            approvals_as_jobs=approvals_as_jobs
            and not toolkit_is_background_excluded(tool_name, config, runtime_paths),
        )
        if toolkit:
            announcement = computer_announcement_middleware(
                toolkit,
                tool_name,
                agent_name=agent_name,
                config=config,
                runtime_paths=runtime_paths,
            )
            if announcement is not None:
                tool_middleware.append(announcement)
            bind_toolkit_authority(toolkit, authored_name=tool_entry.authored_name or tool_name)
        return toolkit

    cli_deferred = []
    for entry in deferred_configs.values():

        async def materialize(entry: EffectiveToolConfig = entry) -> Toolkit:
            toolkit = await asyncio.to_thread(build_entry, entry)
            if toolkit is None:
                msg = f"Configured toolkit {entry.name!r} is unavailable"
                raise ValueError(msg)
            return toolkit

        metadata = TOOL_METADATA.get(entry.name)
        cli_deferred.append(DeferredAgentToolkit(entry.name, metadata.description if metadata else "", materialize))

    dynamic_tools_entry = resolved_tool_configs.pop("dynamic_tools", None)

    def tool_entries() -> Iterator[tuple[str, EffectiveToolConfig]]:
        yield from resolved_tool_configs.items()
        # The search toolkit comes after every deferred toolkit it searches, and only portable tool search rewrites
        # call_tool, so an authored dynamic_tools entry is ignored everywhere else.
        if dynamic_tools_entry is not None and portable_deferred_tools and deferred_toolkits:
            yield dynamic_tools_entry.name, dynamic_tools_entry

    for tool_name, tool_entry in tool_entries():
        try:
            toolkit = build_entry(tool_entry)
            if toolkit:
                tools.append(toolkit)
                target_names = (
                    worker_routed_tool_names
                    if _registry_tool_routes_through_worker(
                        tool_name,
                        runtime_paths=runtime_paths,
                        worker_tools=worker_tools,
                    )
                    else local_tool_names
                )
                target_names.append(tool_name)
                # initial deferred tools stay in the rendered prompt prefix as plain non-deferred tools.
                if wire_deferred_tools and tool_entry.defer and not tool_entry.initial:
                    domain_name = tool_entry.authored_name or tool_name
                    metadata = TOOL_METADATA.get(domain_name)
                    deferred_toolkits.append(
                        defer_toolkit(domain_name, metadata.description if metadata else "", toolkit),
                    )
        except _MatrixRoomRuntimeToolCollisionError:
            raise
        except Exception as exc:
            # One toolkit's construction failure must never stop the agent.
            if minimal_mode:
                raise MinimalModeUnavailableError(
                    minimal_mode_failure_message(str(exc), agent_name, subagent=delegation_depth > 0),
                ) from exc
            logger.warning(
                "Could not load tool for agent construction",
                tool=tool_name,
                agent=agent_name,
                error=str(exc),
                exc_info=not isinstance(exc, ValueError | ImportError),
            )
    controls = _job_controls(
        tools,
        runtime_paths,
        replace(execution_identity, agent_name=agent_name) if execution_identity is not None else None,
        depth=delegation_depth,
        enabled=not disable_runtime_capabilities and background_tool_jobs_enabled(config, runtime_paths),
        # The owned loop runs the hook bridge as middleware around every call, job controls included.
        tool_hook_bridge=None,
        output_file_policy=_agent_tool_output_file_policy(
            agent_runtime,
            runtime_paths,
            config.defaults.tool_output_auto_save_threshold_bytes,
        ),
    )
    if controls is not None:
        tools.append(controls)
    hidden_mcp_functions = _hide_session_mcp_function_collisions(tools, agent_name=agent_name)
    drop_shadowed_deferred_functions(tools, deferred_toolkits)
    if persona_tools is not None:
        require_built_persona_tools(persona_tools, persona_built, hidden_mcp_functions)
    return _AgentToolAssembly(
        tools=tools,
        deferred_toolkits=tuple(deferred_toolkits),
        cli_deferred=tuple(cli_deferred),
        local_tool_names=tuple(local_tool_names),
        worker_routed_tool_names=tuple(worker_routed_tool_names),
        tool_middleware=tuple(tool_middleware),
    )


def _open_agent_session_storage(
    agent_name: str,
    agent_runtime: ResolvedAgentRuntime,
    *,
    history_storage: ConversationStore | None,
    persist_runtime_state: bool,
) -> ConversationStore | None:
    """Open (or reuse) the durable conversation store for one agent instance."""
    with _agent_create_timing("storage_open"):
        if not persist_runtime_state:
            return None
        if history_storage is not None:
            return history_storage
        return agent_storage.create_state_storage(
            agent_name,
            agent_runtime.session_state_root,
            session_table=f"{agent_name}_sessions",
        )


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
    include_identity: bool,
) -> _AgentRoleContext:
    """Render the runtime facts, the identity, and preload context into the role.

    Without ``include_identity``, as for an authored subagent whose prompt replaces it, the role holds only the
    runtime facts.
    """
    runtime_facts = _get_mind_runtime_context(agent_name, runtime_paths)
    if not disable_runtime_capabilities:
        runtime_facts += "\n\n" + _render_tool_execution_environment(
            runtime_paths=runtime_paths,
            local_tool_names=local_tool_names,
            worker_routed_tool_names=worker_routed_tool_names,
            worker_scope=agent_runtime.execution.execution_scope,
            file_access=config.resolve_entity(agent_name).file_access,
            unconfined_tool_names=_unconfined_tool_names(
                (*local_tool_names, *worker_routed_tool_names),
                requires_primary_runtime=False,
            ),
            primary_only_unconfined_tool_names=_unconfined_tool_names(
                (*local_tool_names, *worker_routed_tool_names),
                requires_primary_runtime=True,
            ),
            shared_directories=describe_agent_shared_directories(config, runtime_paths, agent_name),
        )
    if not include_identity:
        return _AgentRoleContext(role=runtime_facts.lstrip(), context_documents=())

    # Get model config for identity context
    if model_name in config.models:
        model_config = config.models[model_name]
        model_provider = model_config.provider.title()  # Capitalize provider name
        model_id = model_config.id
    else:
        # Fallback if model not found
        model_provider = "AI"
        model_id = model_name

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

    full_context = identity_context + runtime_facts
    context_documents: list[_AdditionalContextChunk] = []
    if not disable_runtime_capabilities:
        workspace = agent_runtime.workspace
        full_context += _build_additional_context(
            agent_name,
            agent_config,
            config.defaults.max_preload_chars,
            personality_section_heading=config.get_prompt("PERSONALITY_CONTEXT_SECTION_HEADING"),
            truncation_marker_template=config.get_prompt("CONTEXT_TRUNCATION_MARKER_TEMPLATE"),
            chunk_marker_template=config.get_prompt("CONTEXT_CHUNK_OMITTED_MARKER_TEMPLATE"),
            workspace_context_files=workspace.context_files if workspace is not None else (),
            workspace_root=workspace.root if workspace is not None else None,
            context_documents=context_documents,
            storage_path=runtime_paths.storage_root,
            runtime_paths=runtime_paths,
        )

    return _AgentRoleContext(role=full_context + agent_config.role, context_documents=tuple(context_documents))


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
    agent_name: str,
    configured_instructions: Sequence[str],
    config: Config,
    agent_runtime: ResolvedAgentRuntime,
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

    instructions.extend(
        _build_native_tool_search_instruction_blocks(
            config,
            native_deferred_tool_names,
        ),
    )

    dynamic_tooling_block = _build_dynamic_tooling_instruction_block(config, portable_deferred_toolkits)
    if dynamic_tooling_block is not None:
        instructions.append(dynamic_tooling_block)

    if agent_runtime.tool_base_dir is not None and not disable_runtime_capabilities:
        instructions.append(config.get_prompt("OUTPUT_REDIRECT_PROMPT"))
        # A conditional expression keeps this builder within the complexity limit.
        instructions.extend((config.get_prompt("WORKSPACE_SKILL_AUTHORING_PROMPT"),) if skill_authoring else ())

    file_mode_knowledge_instruction = (
        None
        if disable_runtime_capabilities
        else _file_mode_knowledge_instruction_block(agent_name, config, agent_runtime)
    )
    if file_mode_knowledge_instruction is not None:
        instructions.append(file_mode_knowledge_instruction)

    show_tool_calls = show_tool_calls_for_agent(config, agent_name)
    if not show_tool_calls:
        instructions.append(config.get_prompt("HIDDEN_TOOL_CALLS_PROMPT"))

    if include_interactive_questions:
        instructions.append(config.get_prompt("INTERACTIVE_QUESTION_PROMPT"))

    return instructions


def _job_controls(
    tools: Sequence[Toolkit],
    runtime_paths: constants.RuntimePaths,
    owner: ToolExecutionIdentity | None,
    *,
    depth: int,
    enabled: bool,
    tool_hook_bridge: Callable[..., Any] | None,
    output_file_policy: ToolOutputFilePolicy | None = None,
) -> Toolkit | None:
    """Build the reserved job toolkit behind its caller's tool hooks.

    It never asks for approval: it acts only on the caller's own jobs, each of which passed its own.
    """
    controls = JobTools.build(
        list(tools),
        runtime_paths,
        owner,
        depth=depth,
        enabled=enabled,
        output_file_policy=output_file_policy,
    )
    return None if controls is None else prepend_tool_hook_bridge(controls, tool_hook_bridge)


def team_background_jobs(
    model: Model,
    config: Config,
    runtime_paths: constants.RuntimePaths,
    owner: ToolExecutionIdentity | None,
) -> Toolkit | None:
    """Let a team's calls run as background jobs, and return its leader's job controls.

    The team owns the jobs its members start for it: its leader hears when they end and collects them.
    """
    if not background_tool_jobs_enabled(config, runtime_paths):
        return None
    install_tool_job_execution(model)
    if owner is None:
        return None
    install_job_notice_hook(model, entity_name=owner.recipient)
    bridge = _build_agent_tool_hook_bridge(
        hook_registry=None,
        plugins=_load_agent_plugins(config, runtime_paths),
        agent_name=owner.recipient,
        dispatch_context=ToolDispatchContext(execution_identity=owner),
        config=config,
        runtime_paths=runtime_paths,
    )
    return _job_controls((), runtime_paths, owner, depth=0, enabled=True, tool_hook_bridge=bridge)


_MEMORY_SEARCH_TOOL_NAME = "search_memories"


def _offers_function(toolkits: Sequence[Toolkit], function_name: str) -> bool:
    return any(function_name in toolkit.get_async_functions() for toolkit in toolkits)


def _agent_toolkits(
    toolkits: Sequence[Toolkit],
    knowledge: AgentKnowledge | None,
    skills: SkillsToolkit | None,
    generated_visible: Callable[[Function], bool],
) -> tuple[AdaptedToolkit, ...]:
    """Adapt the agent's tools in the order Agno offered them: configured toolkits, the knowledge search, skills.

    The knowledge search and skill functions are generated beside the configured toolkits, so they get the
    approval and persona policy here that configured functions got while their toolkits were built.
    """
    adapted = [adapt_toolkit(toolkit) for toolkit in toolkits]
    if knowledge is not None:
        search = knowledge.search_function(
            memory_search_available=_offers_function(toolkits, _MEMORY_SEARCH_TOOL_NAME),
        )
        if generated_visible(search):
            adapted.append(
                AdaptedToolkit(name=KNOWLEDGE_SEARCH_TOOL_NAME, functions=(adapt_function(search),), instructions=None),
            )
    if skills is not None and (offered := _prune_toolkit_functions(skills, generated_visible)) is not None:
        adapted.append(adapt_toolkit(offered))
    return tuple(adapted)


def _tool_owners(toolkits: Sequence[Toolkit]) -> dict[str, str]:
    """Map each function the agent offers to the authored toolkit it came from; the first toolkit of a name wins."""
    owners: dict[str, str] = {}
    for toolkit in toolkits:
        for name, function in toolkit.get_async_functions().items():
            if function.owning_toolkit is not None:
                owners.setdefault(name, function.owning_toolkit)
    return owners


@timed("system_prompt_assembly.agent_create")
def create_agent(
    agent_name: str,
    config: Config,
    runtime_paths: constants.RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
    *,
    hook_registry: HookRegistry | None = None,
    knowledge: AgentKnowledge | None = None,
    history_storage: ConversationStore | None = None,
    active_model_name: str | None = None,
    include_interactive_questions: bool = True,
    include_openai_compat_guidance: bool = False,
    persist_runtime_state: bool = True,
    disable_runtime_capabilities: bool = False,
    disabled_tool_names: frozenset[str] = frozenset(),
    tool_function_filter: Callable[[Function], bool] | None = None,
    delegation_depth: int = 0,
    refresh_scheduler: KnowledgeRefreshScheduler | None = None,
    supports_native_tool_approval: bool = False,
    eager_deferred_tools: bool = False,
    required_tool_names: tuple[str, ...] = (),
    agent_mode: AgentMode = "standard",
    agent_cli_in_shell: bool = False,
    persona: SubagentPersona | None = None,
) -> AgentBuild:
    """Build an agent from configuration: its model, tools, prompt, request settings, store, and surface.

    Args:
        agent_name: Name of the agent to create
        config: Application configuration
        runtime_paths: Explicit runtime context for paths, env, and credentials.
        execution_identity: Request execution identity used to resolve scoped
            state, workspaces, worker routing, and requester-local storage.
        hook_registry: Optional hook registry for plugin-based tool call
            interception and event hooks.
        knowledge: Optional shared knowledge for RAG-enabled agents; for a persona, callers resolve
            it narrowed to the persona's kept knowledge bases.
        history_storage: Optional already-open conversation store to reuse for this agent.
        active_model_name: Optional runtime-selected model name overriding the configured model.
        include_interactive_questions: Whether to include the interactive
            question authoring prompt. Set to False for channels that do not
            support Matrix reaction-based question flows.
        include_openai_compat_guidance: Whether to include OpenAI-compatible
            history-format guidance in the shared identity prompt.
        persist_runtime_state: Whether this agent's runs should write durable history.
        disable_runtime_capabilities: Whether to omit tools, skills, knowledge,
            and preloaded context files for a restricted in-process agent run.
        disabled_tool_names: Resolved tool names to omit from this instance.
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
        The agent as the owned loop runs it.

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
    defaults = config.defaults

    # Gate on this agent's resolved runtime model (thread overrides and team
    # members resolve per agent), not on any surrounding team's model.
    model_name = active_model_name or agent_config.model or "default"
    provider = _build_agent_model_provider(
        config,
        runtime_paths,
        model_name,
        replace(execution_identity, agent_name=agent_name) if execution_identity is not None else None,
    )
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

    tool_function_filter, disabled_tool_names = persona_tool_policy(
        persona_tools,
        lambda: caller_toolkit_names(agent_name, config, delegation_depth=delegation_depth),
        tool_function_filter,
        disabled_tool_names,
    )
    tool_assembly = _assemble_agent_toolkits(
        agent_name,
        config,
        runtime_paths,
        agent_runtime,
        execution_identity=execution_identity,
        hook_registry=hook_registry,
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
    )
    store = _open_agent_session_storage(
        agent_name,
        agent_runtime,
        history_storage=history_storage,
        persist_runtime_state=persist_runtime_state,
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
        worker_routed_tool_names=tool_assembly.worker_routed_tool_names,
        include_identity=persona is None,
    )
    logger.info(
        "create_agent",
        agent=agent_name,
        model_provider=provider.identity.provider,
        model_id=provider.identity.id,
        deferred_tool_search=("portable" if portable_deferred_tools else "native")
        if tool_assembly.deferred_wire_tool_names
        else None,
        deferred_tools=sorted(tool_assembly.deferred_wire_tool_names),
    )

    workspace = agent_runtime.workspace
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
            workspace_root=workspace.root if workspace is not None else None,
            output_file_policy=_agent_tool_output_file_policy(
                agent_runtime,
                runtime_paths,
                config.defaults.tool_output_auto_save_threshold_bytes,
            ),
            only=persona.kept_skills if persona is not None else None,
            refused=refused_skills,
        )
    )
    prompt_skills = None if hidden_skill_functions == SKILL_FUNCTION_NAMES else skills
    instructions = _build_agent_instructions(
        agent_name,
        agent_config.instructions if persona is None else (),
        config,
        agent_runtime,
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

    _log_toolkits_without_unique_model_functions(tool_assembly.tools, agent_name=agent_name)
    # A standard agent's own shell commands can call its other tools when the shell reaches MindRoom.
    # A subagent runs inside its caller's response, whose CLI belongs to the caller, so its shell gets none.
    cli_shell = (
        agent_cli_in_shell
        and agent_mode == "standard"
        and delegation_depth == 0
        and not disable_runtime_capabilities
        and standard_cli_eligible(config, runtime_paths, agent_name, execution_identity)
        and wrap_native_shell_window(tool_assembly.tools)
    )
    if cli_shell:
        instructions = [*instructions, STANDARD_CLI_NOTE]

    entity_view = config.resolve_entity(agent_name)
    enabled_knowledge = (
        knowledge
        if not disable_runtime_capabilities
        and (persona is None or persona.kept_knowledge != ())
        and not tool_may_require_approval(config, KNOWLEDGE_SEARCH_TOOL_NAME)
        else None
    )
    # Minimal mode presents skill contents as context documents instead of through skill functions.
    offered_skills = skills if agent_mode == "minimal" else prompt_skills
    spec = AgentSpec(
        id=agent_name,
        name=agent_config.display_name,
        model=provider,
        system_message=None,
        toolkits=_agent_toolkits(
            tool_assembly.tools,
            enabled_knowledge,
            offered_skills,
            partial(_generated_function_visible, config, tool_function_filter),
        ),
        tool_call_limit=entity_view.max_tool_calls_per_turn,
    )
    prepared = prepare_run_tools(spec)
    prompt = PromptSpec(
        # An authored prompt leads the system message in place of the agent's identity.
        description=persona_prompt,
        role=role_context.role,
        instructions=tuple(instructions),
        markdown=agent_config.markdown if agent_config.markdown is not None else defaults.markdown,
        tool_instructions=prepared.instructions,
        additional_context=render_session_context(
            render_date_context(
                config.timezone,
                datetime_context_template=config.get_prompt("DATETIME_CONTEXT_TEMPLATE"),
            ),
        ),
        skills_block=None if offered_skills is None else offered_skills.prompt_block(),
        knowledge_block=None if enabled_knowledge is None else enabled_knowledge.prompt_block(),
        model_note=model_note_for(provider.capabilities, offers_tools=bool(prepared.functions)),
    )

    logger.info(
        "Created agent",
        agent=agent_name,
        display_name=agent_config.display_name,
        tool_count=len(tool_assembly.tools),
    )

    return AgentBuild(
        spec=spec,
        prompt=prompt,
        request_template=ModelRequest(messages=(), deferred_tool_names=tool_assembly.deferred_wire_tool_names),
        history_settings=entity_view.history_settings,
        store=store,
        tool_owners=_tool_owners(tool_assembly.tools),
        tool_middleware=tool_assembly.tool_middleware,
        surface=AgentSurface(
            mode=agent_mode,
            cli_shell=cli_shell,
            deferred=tool_assembly.cli_deferred,
            context_documents=tuple(document.body for document in role_context.context_documents),
            context_files=tuple(role_context.workspace_context_files(agent_runtime.tool_base_dir)),
            knowledge_sources=()
            if enabled_knowledge is None
            else tuple(source.description for source in enabled_knowledge.sources),
        ),
    )


__all__ = [
    "agent_build_can_overlap_file_memory",
    "apply_tool_approval_capability",
    "build_agent_toolkit",
    "create_agent",
    "describe_agent",
    "ensure_default_agent_workspaces",
    "get_agent_toolkit_names",
    "resolve_runtime_worker_tools",
    "show_tool_calls_for_agent",
    "team_background_jobs",
]
