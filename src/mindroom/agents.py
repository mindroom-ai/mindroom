"""Agent loader that reads agent configurations from YAML file."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from weakref import WeakValueDictionary
from zoneinfo import ZoneInfo

from agno.agent import Agent
from agno.culture.manager import CultureManager
from agno.db.base import SessionType
from agno.db.sqlite import SqliteDb
from agno.learn import LearningMachine, LearningMode, UserMemoryConfig, UserProfileConfig
from agno.run.agent import RunOutput
from agno.run.team import TeamRunOutput
from agno.session.agent import AgentSession
from agno.session.team import TeamSession

import mindroom.tools  # noqa: F401
from mindroom import agent_prompts, agent_storage, constants, model_loading
from mindroom.agent_descriptions import describe_agent
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.credentials import get_runtime_credentials_manager
from mindroom.hooks import HookRegistry
from mindroom.logging_config import get_logger
from mindroom.matrix.identity import MatrixID
from mindroom.runtime_resolution import (
    ResolvedAgentRuntime,
    resolve_agent_runtime,
    resolve_private_requester_scope_root,
)
from mindroom.timing import timed
from mindroom.tool_system.catalog import TOOL_METADATA, get_tool_by_name
from mindroom.tool_system.dynamic_toolkits import (
    DynamicToolkitSelection,
    resolve_dynamic_toolkit_selection,
    resolve_special_tool_names,
)
from mindroom.tool_system.output_files import ToolOutputFilePolicy, wrap_toolkit_for_output_files
from mindroom.tool_system.plugins import load_plugins
from mindroom.tool_system.runtime_context import ToolDispatchContext
from mindroom.tool_system.skills import build_agent_skills
from mindroom.tool_system.tool_hooks import (
    build_tool_hook_bridge,
    prepend_tool_hook_bridge,
)
from mindroom.tool_system.worker_routing import (
    agent_workspace_root_path,
    build_worker_target_from_runtime_env,
    resolve_agent_owned_path,
    shared_storage_root,
)
from mindroom.workspaces import ensure_workspace_template

if TYPE_CHECKING:
    from collections.abc import Callable

    from agno.knowledge.protocol import KnowledgeProtocol
    from agno.models.base import Model
    from agno.skills import Skills
    from agno.team import Team
    from agno.tools.toolkit import Toolkit

    from mindroom.config.agent import AgentConfig, CultureConfig, CultureMode
    from mindroom.config.main import Config
    from mindroom.config.models import DefaultsConfig
    from mindroom.credentials import CredentialsManager
    from mindroom.hooks import HookRegistryPlugin
    from mindroom.knowledge.refresh_owner import KnowledgeRefreshOwner
    from mindroom.tool_system.worker_routing import (
        ToolExecutionIdentity,
        WorkerScope,
    )

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


@dataclass
class _CachedCultureManager:
    """Cached culture manager with a signature for invalidation on config changes."""

    signature: tuple[str, str]
    manager: CultureManager


@dataclass(frozen=True)
class _CultureAgentSettings:
    """Culture feature flags to apply to the Agent constructor."""

    add_culture_to_context: bool
    update_cultural_knowledge: bool
    enable_agentic_culture: bool


@dataclass
class _AdditionalContextChunk:
    """Chunk of preload context with truncation priority metadata."""

    kind: str
    title: str
    body: str


_CULTURE_MANAGER_CACHE: dict[tuple[str, str], _CachedCultureManager] = {}
_PRIVATE_CULTURE_MANAGER_CACHE: WeakValueDictionary[
    tuple[str, str, tuple[str, str]],
    CultureManager,
] = WeakValueDictionary()


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
    ensure_workspace_template(workspace_path, template="mind")


def ensure_default_agent_workspaces(config: Config, storage_path: Path) -> None:
    """Materialize built-in starter workspaces under the active runtime storage root."""
    for agent_name, agent_config in config.agents.items():
        if _uses_default_mind_workspace_scaffold(agent_name, agent_config):
            _ensure_default_mind_workspace(storage_path)


def _get_datetime_context(timezone_str: str) -> str:
    """Generate current date context for the agent.

    Args:
        timezone_str: Timezone string (e.g., 'America/New_York', 'UTC')

    Returns:
        Formatted string with current date and timezone information

    """
    tz = ZoneInfo(timezone_str)
    now = datetime.now(tz)

    date_str = now.strftime("%A, %B %d, %Y")
    timezone_abbrev = now.tzname() or timezone_str

    return f"""## Current Date and Time
Today is {date_str}.
Timezone: {timezone_str} ({timezone_abbrev})

"""


def _load_context_files(
    context_files: list[Path | str],
    runtime_paths: constants.RuntimePaths,
    agent_name: str | None = None,
    storage_path: Path | None = None,
) -> list[_AdditionalContextChunk]:
    """Load configured context files."""
    loaded_parts: list[_AdditionalContextChunk] = []
    for raw_path in context_files:
        if isinstance(raw_path, Path):
            resolved_path = raw_path
        elif agent_name is not None and storage_path is not None:
            resolved_path = resolve_agent_owned_path(
                raw_path,
                agent_name=agent_name,
                base_storage_path=storage_path,
            )
        else:
            resolved_path = constants.resolve_config_relative_path(raw_path, runtime_paths)
        if resolved_path.is_file():
            body = _read_context_file(resolved_path)
            loaded_parts.append(
                _AdditionalContextChunk(
                    kind="personality",
                    title=resolved_path.name,
                    body=body,
                ),
            )
        else:
            logger.warning("context_file_not_found", agent=agent_name, path=str(resolved_path))
    return loaded_parts


@timed("system_prompt_assembly.agent_create.context_file_read")
def _read_context_file(resolved_path: Path) -> str:
    return resolved_path.read_text(encoding="utf-8").strip()


def _render_context_chunks(section_heading: str, chunks: list[_AdditionalContextChunk]) -> str:
    """Render context chunks into a markdown section."""
    rendered = [f"### {chunk.title}\n{chunk.body.strip()}" for chunk in chunks if chunk.body.strip()]
    if not rendered:
        return ""
    return f"{section_heading}\n" + "\n\n".join(rendered) + "\n\n"


def _render_additional_context(personality_chunks: list[_AdditionalContextChunk]) -> str:
    """Render full additional context from personality chunks."""
    return _render_context_chunks("## Personality Context", personality_chunks)


def _build_preload_truncation_groups(
    personality_chunks: list[_AdditionalContextChunk],
) -> list[list[_AdditionalContextChunk]]:
    """Return truncation groups ordered from least to most critical context."""
    return [[chunk for chunk in personality_chunks if chunk.kind == "personality"]]


def _drop_whole_chunks(
    groups: list[list[_AdditionalContextChunk]],
    personality_chunks: list[_AdditionalContextChunk],
    max_preload_chars: int,
) -> int:
    """Drop entire chunk bodies (least critical first) until under the cap."""
    omitted = 0
    for group in groups:
        for chunk in group:
            if len(_render_additional_context(personality_chunks)) <= max_preload_chars:
                return omitted
            if not chunk.body:
                continue
            omitted += len(chunk.body)
            chunk.body = ""
    return omitted


def _trim_chunk_tails(
    groups: list[list[_AdditionalContextChunk]],
    personality_chunks: list[_AdditionalContextChunk],
    max_preload_chars: int,
) -> int:
    """Trim from the *end* of chunks to preserve headers/identity at the top."""
    omitted = 0
    for group in groups:
        for chunk in group:
            overflow = len(_render_additional_context(personality_chunks)) - max_preload_chars
            if overflow <= 0:
                return omitted
            if not chunk.body:
                continue
            remove_count = min(overflow, len(chunk.body))
            chunk.body = chunk.body[: len(chunk.body) - remove_count].rstrip()
            omitted += remove_count
    return omitted


def _apply_preload_cap(personality_chunks: list[_AdditionalContextChunk], max_preload_chars: int) -> tuple[str, int]:
    """Apply hard preload cap with deterministic truncation priority.

    Truncation order is by file list order.
    First drops whole chunks, then trims from the *end* of remaining chunks.
    """
    rendered = _render_additional_context(personality_chunks)
    if len(rendered) <= max_preload_chars:
        return rendered, 0

    groups = _build_preload_truncation_groups(personality_chunks)
    omitted_chars = _drop_whole_chunks(groups, personality_chunks, max_preload_chars)
    omitted_chars += _trim_chunk_tails(groups, personality_chunks, max_preload_chars)

    rendered = _render_additional_context(personality_chunks)
    if omitted_chars <= 0:
        return rendered, 0

    marker = f"[Content truncated - {omitted_chars} chars omitted. Use search_knowledge_base for older history.]"
    marker_block = f"\n\n{marker}\n\n"
    budget = max_preload_chars - len(marker_block)
    if budget <= 0:
        return marker_block[:max_preload_chars], omitted_chars
    if len(rendered) > budget:
        rendered = rendered[len(rendered) - budget :]
    return rendered.rstrip("\n") + marker_block, omitted_chars


@timed("system_prompt_assembly.agent_create.additional_context")
def _build_additional_context(
    agent_name: str,
    agent_config: AgentConfig,
    max_preload_chars: int,
    *,
    workspace_context_files: tuple[Path, ...] = (),
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
        )

    additional_context, omitted_chars = _apply_preload_cap(personality_chunks, max_preload_chars)
    if omitted_chars > 0:
        logger.warning(
            "Preload context exceeded max_preload_chars and was truncated",
            omitted_chars=omitted_chars,
            max_preload_chars=max_preload_chars,
        )
    return additional_context


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
    routing_agent_is_private: bool,
    execution_identity: ToolExecutionIdentity | None,
    runtime_overrides: dict[str, object] | None,
) -> Toolkit:
    """Build one registered toolkit using the resolved routing inputs for this agent."""
    worker_target = build_worker_target_from_runtime_env(
        worker_scope,
        agent_name,
        execution_identity=execution_identity,
        runtime_paths=runtime_paths,
        private_agent_names=(
            frozenset({agent_name})
            if worker_scope == "user_agent" and routing_agent_is_private
            else (frozenset() if worker_scope == "user_agent" else None)
        ),
    )

    return get_tool_by_name(
        tool_name,
        runtime_paths,
        credentials_manager=credentials_manager,
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
        worker_target=worker_target,
    )


def _wrap_direct_agent_toolkit_for_output_files(
    toolkit: Toolkit,
    *,
    agent_runtime: ResolvedAgentRuntime,
    runtime_paths: constants.RuntimePaths,
) -> Toolkit:
    """Apply the central output-file wrapper to MindRoom-owned direct toolkits."""
    policy = (
        ToolOutputFilePolicy.from_runtime(agent_runtime.tool_base_dir, runtime_paths)
        if agent_runtime.tool_base_dir is not None
        else None
    )
    return wrap_toolkit_for_output_files(toolkit, policy)


@timed("system_prompt_assembly.agent_create.model_instance")
def _load_agent_model_instance(
    config: Config,
    runtime_paths: constants.RuntimePaths,
    model_name: str,
    execution_identity: ToolExecutionIdentity | None = None,
) -> Model:
    """Load one agent model while preserving prompt-assembly timing attribution."""
    return model_loading.get_model_instance(
        config,
        runtime_paths,
        model_name,
        execution_identity=execution_identity,
    )


@timed("system_prompt_assembly.agent_create.toolkit_build")
def build_agent_toolkit(  # noqa: C901, PLR0911
    tool_name: str,
    *,
    agent_name: str,
    config: Config,
    runtime_paths: constants.RuntimePaths,
    worker_tools: list[str],
    agent_runtime: ResolvedAgentRuntime | None = None,
    tool_config_overrides: dict[str, object] | None = None,
    execution_identity: ToolExecutionIdentity | None,
    session_id: str | None = None,
    delegation_depth: int = 0,
    refresh_owner: KnowledgeRefreshOwner | None = None,
) -> Toolkit | None:
    """Build one configured toolkit for an agent.

    Returns ``None`` when the configured tool should be skipped, such as an
    explicit ``delegate`` entry without valid delegation targets.
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

    if tool_name == "memory":
        from mindroom.custom_tools.memory import MemoryTools  # noqa: PLC0415

        # MemoryTools resolves the canonical per-agent storage roots internally via the
        # shared memory facade, so it should receive the caller-visible runtime root here.
        return _wrap_direct_agent_toolkit_for_output_files(
            MemoryTools(
                agent_name=agent_name,
                storage_path=storage_path,
                config=config,
                runtime_paths=runtime_paths,
                execution_identity=execution_identity,
            ),
            agent_runtime=agent_runtime,
            runtime_paths=runtime_paths,
        )

    if tool_name == "delegate":
        # Imported lazily to avoid a circular import through DelegateTools -> create_agent.
        from mindroom.custom_tools import delegate  # noqa: PLC0415

        if not agent_config.delegate_to:
            logger.warning(
                "Skipping delegate tool because delegate_to is empty",
                agent=agent_name,
            )
            return None
        if delegation_depth >= delegate.MAX_DELEGATION_DEPTH:
            logger.warning(
                "Skipping delegate tool because delegation depth limit was reached",
                agent=agent_name,
                delegation_depth=delegation_depth,
                max_delegation_depth=delegate.MAX_DELEGATION_DEPTH,
            )
            return None
        return _wrap_direct_agent_toolkit_for_output_files(
            delegate.DelegateTools(
                agent_name=agent_name,
                delegate_to=agent_config.delegate_to,
                runtime_paths=runtime_paths,
                config=config,
                execution_identity=execution_identity,
                delegation_depth=delegation_depth,
                refresh_owner=refresh_owner,
            ),
            agent_runtime=agent_runtime,
            runtime_paths=runtime_paths,
        )

    if tool_name == "self_config":
        from mindroom.custom_tools.self_config import SelfConfigTools  # noqa: PLC0415

        return _wrap_direct_agent_toolkit_for_output_files(
            SelfConfigTools(agent_name=agent_name, runtime_paths=runtime_paths),
            agent_runtime=agent_runtime,
            runtime_paths=runtime_paths,
        )

    if tool_name == "compact_context":
        from mindroom.custom_tools.compact_context import CompactContextTools  # noqa: PLC0415

        return _wrap_direct_agent_toolkit_for_output_files(
            CompactContextTools(
                agent_name=agent_name,
                config=config,
                runtime_paths=runtime_paths,
                execution_identity=execution_identity,
            ),
            agent_runtime=agent_runtime,
            runtime_paths=runtime_paths,
        )

    if tool_name == "dynamic_tools":
        from mindroom.custom_tools.dynamic_tools import DynamicToolsToolkit  # noqa: PLC0415

        if not agent_config.allowed_toolkits:
            logger.warning(
                "Skipping 'dynamic_tools' tool for agent '%s': allowed_toolkits is empty",
                agent_name,
            )
            return None
        if session_id is None:
            logger.warning(
                "Skipping 'dynamic_tools' tool for agent '%s': no stable session_id is available",
                agent_name,
            )
            return None
        return _wrap_direct_agent_toolkit_for_output_files(
            DynamicToolsToolkit(
                agent_name=agent_name,
                config=config,
                session_id=session_id,
            ),
            agent_runtime=agent_runtime,
            runtime_paths=runtime_paths,
        )

    return _build_registered_agent_tool(
        tool_name,
        runtime_paths,
        credentials_manager,
        shared_storage_path,
        worker_tools,
        agent_runtime.execution_scope,
        (config.get_worker_grantable_credentials() if agent_runtime.execution_scope is not None else None),
        agent_name,
        tool_config_overrides,
        agent_runtime.tool_base_dir,
        agent_runtime.is_private,
        execution_identity,
        config.get_agent_tool_runtime_overrides(agent_name, tool_name, runtime_paths=runtime_paths),
    )


def get_agent_toolkit_names(
    agent_name: str,
    config: Config,
    *,
    delegation_depth: int = 0,
) -> list[str]:
    """Return the complete ordered toolkit list for an agent runtime."""
    tool_names = list(config.get_agent_tools(agent_name))
    for tool_name in resolve_special_tool_names(
        agent_name=agent_name,
        config=config,
        delegation_depth=delegation_depth,
        enable_dynamic_tools_manager=True,
    ):
        if tool_name not in tool_names:
            tool_names.append(tool_name)

    return tool_names


def _resolve_runtime_worker_tools(
    agent_name: str,
    config: Config,
    runtime_paths: constants.RuntimePaths,
    runtime_tool_names: list[str],
) -> list[str]:
    """Return worker-routed tools for one concrete runtime tool selection."""
    agent_config = config.get_agent(agent_name)
    configured = agent_config.worker_tools
    if configured is None:
        configured = config.defaults.worker_tools
    if configured is not None:
        return config.expand_tool_names(list(configured))

    from mindroom.tool_system.catalog import default_worker_routed_tools, ensure_tool_registry_loaded  # noqa: PLC0415

    ensure_tool_registry_loaded(runtime_paths, config)
    return default_worker_routed_tools(runtime_tool_names)


# Rich prompt mapping - agents that use detailed prompts instead of simple roles
_RICH_PROMPTS = {
    "code": agent_prompts.CODE_AGENT_PROMPT,
    "research": agent_prompts.RESEARCH_AGENT_PROMPT,
    "calculator": agent_prompts.CALCULATOR_AGENT_PROMPT,
    "general": agent_prompts.GENERAL_AGENT_PROMPT,
    "shell": agent_prompts.SHELL_AGENT_PROMPT,
    "summary": agent_prompts.SUMMARY_AGENT_PROMPT,
    "finance": agent_prompts.FINANCE_AGENT_PROMPT,
    "news": agent_prompts.NEWS_AGENT_PROMPT,
    "data_analyst": agent_prompts.DATA_ANALYST_AGENT_PROMPT,
}


def _is_learning_enabled(agent_config: AgentConfig, defaults: DefaultsConfig) -> bool:
    """Check if learning is enabled for an agent, falling back to defaults."""
    learning = agent_config.learning if agent_config.learning is not None else defaults.learning
    return learning is not False


def _resolve_agent_learning(
    agent_config: AgentConfig,
    defaults: DefaultsConfig,
    learning_storage: SqliteDb | None = None,
) -> bool | LearningMachine:
    """Resolve Agent.learning setting from MindRoom agent configuration."""
    if not _is_learning_enabled(agent_config, defaults):
        return False

    learning_mode = agent_config.learning_mode or defaults.learning_mode
    learning_mode_value = LearningMode.AGENTIC if learning_mode == "agentic" else LearningMode.ALWAYS

    return LearningMachine(
        db=learning_storage,
        user_profile=UserProfileConfig(mode=learning_mode_value),
        user_memory=UserMemoryConfig(mode=learning_mode_value),
    )


def _build_dynamic_tooling_instruction_block(
    config: Config,
    agent_name: str,
    *,
    loaded_toolkits: tuple[str, ...],
    enable_dynamic_tools_manager: bool,
) -> str | None:
    """Return compact prompt guidance for dynamic toolkit loading."""
    agent_config = config.get_agent(agent_name)
    if not enable_dynamic_tools_manager or not agent_config.allowed_toolkits:
        return None

    toolkit_lines: list[str] = []
    for toolkit_name in agent_config.allowed_toolkits:
        description = config.get_toolkit(toolkit_name).description.strip()
        if description:
            toolkit_lines.append(f"- {toolkit_name}: {description}")
        else:
            toolkit_lines.append(f"- {toolkit_name}")

    current_toolkits = ", ".join(loaded_toolkits) if loaded_toolkits else "(none)"
    sticky_toolkits = ", ".join(agent_config.initial_toolkits) if agent_config.initial_toolkits else "(none)"
    toolkit_catalog = "\n".join(toolkit_lines)
    return (
        "## Dynamic Toolkits\n"
        "You may manage optional tool bundles with the `dynamic_tools` tool.\n"
        "Allowed toolkits:\n"
        f"{toolkit_catalog}\n"
        f"Currently loaded: {current_toolkits}\n"
        f"Sticky initial toolkits that cannot be unloaded: {sticky_toolkits}\n"
        "Use `list_toolkits()` when unsure which toolkit contains a capability.\n"
        "Use `load_tools(toolkit)` or `unload_tools(toolkit)` to change the loaded set.\n"
        "In team conversations, each member manages its own toolkit state, so loading one member does not load the others.\n"
        "Those changes take effect on the next request in the same session, not later in this run."
    )


def create_session_storage(
    agent_name: str,
    config: Config,
    runtime_paths: constants.RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
) -> SqliteDb:
    """Create persistent session storage for an agent."""
    return _create_agent_state_db(
        agent_name,
        config,
        runtime_paths,
        subdir="sessions",
        session_table=f"{agent_name}_sessions",
        execution_identity=execution_identity,
    )


def _enable_all_history_replay(entity: Agent | Team) -> None:
    """Undo Agno's default three-run history fallback."""
    entity.num_history_runs = None


def _create_agent_state_db(
    agent_name: str,
    config: Config,
    runtime_paths: constants.RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
    *,
    subdir: str,
    session_table: str,
) -> SqliteDb:
    """Create a persistent SQLite database for one agent state category."""
    state_storage_path = resolve_agent_runtime(
        agent_name,
        config,
        runtime_paths,
        execution_identity=execution_identity,
    ).state_root
    return agent_storage.create_state_storage_db(
        storage_name=agent_name,
        state_root=state_storage_path,
        subdir=subdir,
        session_table=session_table,
    )


def _create_culture_storage(culture_name: str, storage_path: Path) -> SqliteDb:
    """Create persistent culture storage shared by all agents in a culture."""
    culture_dir = storage_path / "culture"
    culture_dir.mkdir(parents=True, exist_ok=True)
    return SqliteDb(db_file=str(culture_dir / f"{culture_name}.db"))


def get_agent_session(storage: SqliteDb, session_id: str) -> AgentSession | None:
    """Retrieve and deserialize an AgentSession from storage."""
    raw = storage.get_session(session_id, SessionType.AGENT)
    if raw is None:
        return None
    if isinstance(raw, AgentSession):
        return raw
    if isinstance(raw, dict):
        return AgentSession.from_dict(cast("dict[str, Any]", raw))
    return None


def get_team_session(storage: SqliteDb, session_id: str) -> TeamSession | None:
    """Retrieve and deserialize a TeamSession from storage."""
    raw = storage.get_session(session_id, SessionType.TEAM)
    if raw is None:
        return None
    if isinstance(raw, TeamSession):
        return raw
    if isinstance(raw, dict):
        return TeamSession.from_dict(cast("dict[str, Any]", raw))
    return None


def remove_run_by_event_id(
    storage: SqliteDb,
    session_id: str,
    event_id: str,
    *,
    session_type: SessionType = SessionType.AGENT,
) -> bool:
    """Remove a run whose Matrix anchor or coalesced source membership matches.

    Returns True if a run was removed.
    """
    session = (
        get_team_session(storage, session_id)
        if session_type is SessionType.TEAM
        else get_agent_session(
            storage,
            session_id,
        )
    )
    if session is None or not session.runs:
        return False
    original_len = len(session.runs)
    filtered_runs: list[Any] = []
    for run in session.runs:
        if not isinstance(run, (RunOutput, TeamRunOutput)) or not run.metadata:
            filtered_runs.append(run)
            continue
        raw_source_event_ids = run.metadata.get(constants.MATRIX_SOURCE_EVENT_IDS_METADATA_KEY)
        source_event_ids = (
            [candidate for candidate in raw_source_event_ids if isinstance(candidate, str)]
            if isinstance(raw_source_event_ids, list)
            else []
        )
        matches_event_id = run.metadata.get(constants.MATRIX_EVENT_ID_METADATA_KEY) == event_id
        if matches_event_id or event_id in source_event_ids:
            continue
        filtered_runs.append(run)
    session.runs = filtered_runs
    if len(session.runs) == original_len:
        return False
    storage.upsert_session(session)
    return True


def _resolve_culture_settings(mode: CultureMode) -> _CultureAgentSettings:
    """Map a culture mode to Agno culture feature flags."""
    if mode == "automatic":
        return _CultureAgentSettings(
            add_culture_to_context=True,
            update_cultural_knowledge=True,
            enable_agentic_culture=False,
        )
    if mode == "agentic":
        return _CultureAgentSettings(
            add_culture_to_context=True,
            update_cultural_knowledge=False,
            enable_agentic_culture=True,
        )
    return _CultureAgentSettings(
        add_culture_to_context=True,
        update_cultural_knowledge=False,
        enable_agentic_culture=False,
    )


def _culture_signature(culture_config: CultureConfig) -> tuple[str, str]:
    return (culture_config.mode, culture_config.description)


@timed("system_prompt_assembly.agent_create.culture_manager")
def _resolve_agent_culture(
    agent_name: str,
    config: Config,
    storage_path: Path,
    model: Model,
    *,
    cache_private: bool = False,
) -> tuple[CultureManager | None, _CultureAgentSettings | None]:
    """Resolve shared culture manager and feature flags for an agent."""
    culture_assignment = config.get_agent_culture(agent_name)
    if culture_assignment is None:
        return None, None

    culture_name, culture_config = culture_assignment
    settings = _resolve_culture_settings(culture_config.mode)
    cache_key = (str(storage_path.resolve()), culture_name)
    signature = _culture_signature(culture_config)
    if cache_private:
        private_cache_key = (*cache_key, signature)
        cached_private_manager = _PRIVATE_CULTURE_MANAGER_CACHE.get(private_cache_key)
        if cached_private_manager is not None:
            cached_private_manager.model = model
            return cached_private_manager, settings
    else:
        cached_manager = _CULTURE_MANAGER_CACHE.get(cache_key)
        if cached_manager is not None and cached_manager.signature == signature:
            cached_manager.manager.model = model
            return cached_manager.manager, settings

    culture_scope = culture_config.description.strip() or "Shared best practices and principles."
    culture_manager = CultureManager(
        model=model,
        db=_create_culture_storage(culture_name, storage_path),
        culture_capture_instructions=f"Culture '{culture_name}': {culture_scope}",
        add_knowledge=culture_config.mode != "manual",
        update_knowledge=culture_config.mode != "manual",
        delete_knowledge=False,
        clear_knowledge=False,
    )
    if cache_private:
        _PRIVATE_CULTURE_MANAGER_CACHE[private_cache_key] = culture_manager
    else:
        _CULTURE_MANAGER_CACHE[cache_key] = _CachedCultureManager(
            signature=signature,
            manager=culture_manager,
        )
    return culture_manager, settings


@timed("system_prompt_assembly.agent_create.load_plugins")
def _load_agent_plugins(config: Config, runtime_paths: constants.RuntimePaths) -> list[HookRegistryPlugin]:
    return cast("list[HookRegistryPlugin]", load_plugins(config, runtime_paths))


@timed("system_prompt_assembly.agent_create.hook_bridge")
def _build_agent_tool_hook_bridge(
    *,
    hook_registry: HookRegistry | None,
    plugins: list[HookRegistryPlugin],
    agent_name: str,
    dispatch_context: ToolDispatchContext | None,
    config: Config,
    runtime_paths: constants.RuntimePaths,
) -> Callable[..., Any] | None:
    active_hook_registry = hook_registry if hook_registry is not None else HookRegistry.from_plugins(plugins)
    return build_tool_hook_bridge(
        active_hook_registry,
        agent_name=agent_name,
        dispatch_context=dispatch_context,
        config=config,
        runtime_paths=runtime_paths,
    )


@timed("system_prompt_assembly.agent_create.dynamic_tool_selection")
def _resolve_agent_dynamic_tool_selection(
    *,
    agent_name: str,
    config: Config,
    session_id: str | None,
    delegation_depth: int,
) -> DynamicToolkitSelection:
    return resolve_dynamic_toolkit_selection(
        agent_name=agent_name,
        config=config,
        session_id=session_id,
        delegation_depth=delegation_depth,
    )


@timed("system_prompt_assembly.agent_create.skills_load")
def _load_agent_skills(
    agent_name: str,
    config: Config,
    runtime_paths: constants.RuntimePaths,
) -> Skills | None:
    return build_agent_skills(agent_name, config, runtime_paths)


@timed("system_prompt_assembly.agent_create.agent_init")
def _initialize_agent_instance(**agent_kwargs: Any) -> Agent:  # noqa: ANN401
    return Agent(**agent_kwargs)


@timed("system_prompt_assembly.agent_create")
def create_agent(  # noqa: PLR0915, C901, PLR0912
    agent_name: str,
    config: Config,
    runtime_paths: constants.RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
    *,
    session_id: str | None = None,
    hook_registry: HookRegistry | None = None,
    knowledge: KnowledgeProtocol | None = None,
    history_storage: SqliteDb | None = None,
    active_model_name: str | None = None,
    include_interactive_questions: bool = True,
    include_openai_compat_guidance: bool = False,
    delegation_depth: int = 0,
    refresh_owner: KnowledgeRefreshOwner | None = None,
    timing_scope: str | None = None,
) -> Agent:
    """Create an agent instance from configuration.

    Args:
        agent_name: Name of the agent to create
        config: Application configuration
        runtime_paths: Explicit runtime context for paths, env, and credentials.
        execution_identity: Request execution identity used to resolve scoped
            state, workspaces, worker routing, and requester-local storage.
        session_id: Stable Agno session id used to resolve session-scoped
            dynamic toolkit state.
        hook_registry: Optional hook registry for plugin-based tool call
            interception and event hooks.
        knowledge: Optional shared knowledge base instance for RAG-enabled agents.
        history_storage: Optional already-open session storage to reuse for this agent.
        active_model_name: Optional runtime-selected model name overriding the configured model.
        include_interactive_questions: Whether to include the interactive
            question authoring prompt. Set to False for channels that do not
            support Matrix reaction-based question flows.
        include_openai_compat_guidance: Whether to include OpenAI-compatible
            history-format guidance in the shared identity prompt.
        delegation_depth: Current delegation nesting depth. Used to prevent
            infinite recursion when agents delegate to each other.
        refresh_owner: Optional runtime-owned shared knowledge refresh scheduler
            passed through to delegated child agents.
        timing_scope: Optional correlated timing scope id for nested
            `system_prompt_assembly` sub-timers.

    Returns:
        Configured Agent instance

    Raises:
        ValueError: If agent_name is not found in configuration

    """
    del timing_scope
    resolved_storage_path = runtime_paths.storage_root
    agent_runtime = resolve_agent_runtime(
        agent_name,
        config,
        runtime_paths,
        execution_identity=execution_identity,
        create=True,
    )

    agent_config = config.get_agent(agent_name)
    ensure_default_agent_workspaces(config, resolved_storage_path)
    defaults = config.defaults

    plugins = _load_agent_plugins(config, runtime_paths)
    tool_hook_bridge = _build_agent_tool_hook_bridge(
        hook_registry=hook_registry,
        plugins=plugins,
        agent_name=agent_name,
        dispatch_context=(
            ToolDispatchContext(execution_identity=execution_identity) if execution_identity is not None else None
        ),
        config=config,
        runtime_paths=runtime_paths,
    )

    storage = (
        history_storage
        if history_storage is not None
        else agent_storage.create_state_storage_db(
            agent_name,
            agent_runtime.state_root,
            subdir="sessions",
            session_table=f"{agent_name}_sessions",
        )
    )
    # Dynamic toolkit state remains per-agent in V1 because each agent keeps its
    # own session DB. Team members may share one conversation session_id, but
    # toolkit loads do not cross agent boundaries yet.
    dynamic_tool_selection = _resolve_agent_dynamic_tool_selection(
        agent_name=agent_name,
        config=config,
        session_id=session_id,
        delegation_depth=delegation_depth,
    )
    resolved_tool_configs = {
        entry.name: entry.tool_config_overrides for entry in dynamic_tool_selection.runtime_tool_configs
    }
    worker_tools = _resolve_runtime_worker_tools(
        agent_name,
        config,
        runtime_paths,
        list(resolved_tool_configs),
    )
    workspace = agent_runtime.workspace
    tools: list[Toolkit] = []
    for tool_name in resolved_tool_configs:
        try:
            toolkit = build_agent_toolkit(
                tool_name,
                agent_name=agent_name,
                config=config,
                runtime_paths=runtime_paths,
                worker_tools=worker_tools,
                agent_runtime=agent_runtime,
                tool_config_overrides=resolved_tool_configs.get(tool_name),
                session_id=session_id,
                execution_identity=execution_identity,
                delegation_depth=delegation_depth,
                refresh_owner=refresh_owner,
            )
            if toolkit:
                tools.append(prepend_tool_hook_bridge(toolkit, tool_hook_bridge))
        except (ValueError, ImportError) as exc:
            logger.warning(
                "Could not load tool for agent construction",
                tool=tool_name,
                agent=agent_name,
                error=str(exc),
            )
    learning_storage = (
        agent_storage.create_state_storage_db(
            storage_name=agent_name,
            state_root=agent_runtime.state_root,
            subdir="learning",
            session_table=f"{agent_name}_learning_sessions",
        )
        if _is_learning_enabled(agent_config, defaults)
        else None
    )

    # Get model config for identity context
    model_name = active_model_name or agent_config.model or "default"
    if model_name in config.models:
        model_config = config.models[model_name]
        model_provider = model_config.provider.title()  # Capitalize provider name
        model_id = model_config.id
    else:
        # Fallback if model not found
        model_provider = "AI"
        model_id = model_name

    # Add identity context to all agents using the unified template
    matrix_id = MatrixID.from_agent(
        agent_name,
        config.get_domain(runtime_paths),
        runtime_paths,
    ).full_id
    identity_context = agent_prompts.build_agent_identity_context(
        display_name=agent_config.display_name,
        matrix_id=matrix_id,
        model_provider=model_provider,
        model_id=model_id,
        include_openai_compat_guidance=include_openai_compat_guidance,
    )

    # Add current date context with the user's configured timezone
    datetime_context = _get_datetime_context(config.timezone)

    # Combine identity and datetime contexts
    full_context = identity_context + datetime_context

    full_context += _build_additional_context(
        agent_name,
        agent_config,
        config.defaults.max_preload_chars,
        workspace_context_files=workspace.context_files if workspace is not None else (),
        storage_path=resolved_storage_path,
        runtime_paths=runtime_paths,
    )

    # Use rich prompt if available, otherwise use YAML config
    if agent_name in _RICH_PROMPTS:
        logger.info("using_rich_prompt", agent=agent_name)
        # Prepend full context to the rich prompt
        role = full_context + _RICH_PROMPTS[agent_name]
        instructions = []  # Instructions are in the rich prompt
    else:
        logger.info("using_yaml_agent_config", agent=agent_name)
        # For YAML agents, prepend full context to role and keep original instructions
        role = full_context + agent_config.role
        instructions = list(agent_config.instructions)

    # Create agent with defaults applied
    model = _load_agent_model_instance(config, runtime_paths, model_name, execution_identity)
    logger.info(
        "create_agent",
        agent=agent_name,
        model_class=model.__class__.__name__,
        model_id=model.id,
    )

    skills = _load_agent_skills(agent_name, config, runtime_paths)
    if skills and skills.get_skill_names():
        instructions.append(agent_prompts.SKILLS_TOOL_USAGE_PROMPT)

    dynamic_tooling_block = _build_dynamic_tooling_instruction_block(
        config,
        agent_name,
        loaded_toolkits=dynamic_tool_selection.loaded_toolkits,
        enable_dynamic_tools_manager=session_id is not None,
    )
    if dynamic_tooling_block is not None:
        instructions.append(dynamic_tooling_block)

    if agent_runtime.tool_base_dir is not None:
        instructions.append(agent_prompts.OUTPUT_REDIRECT_PROMPT)

    show_tool_calls = show_tool_calls_for_agent(config, agent_name)
    if not show_tool_calls:
        instructions.append(agent_prompts.HIDDEN_TOOL_CALLS_PROMPT)

    if include_interactive_questions:
        instructions.append(agent_prompts.INTERACTIVE_QUESTION_PROMPT)

    knowledge_enabled = bool(config.get_agent_knowledge_base_ids(agent_name)) and knowledge is not None
    culture_storage_root = resolved_storage_path
    cache_private_culture = False
    if agent_runtime.is_private:
        worker_key = agent_runtime.worker_key
        if worker_key is None:
            msg = f"Private agent '{agent_name}' requires a worker key to resolve culture state"
            raise ValueError(msg)
        execution_scope = agent_runtime.execution_scope
        execution_identity = agent_runtime.execution_identity
        if execution_scope is None or execution_identity is None:
            msg = f"Private agent '{agent_name}' requires an execution scope and identity to resolve culture state"
            raise ValueError(msg)
        culture_storage_root = resolve_private_requester_scope_root(
            runtime_paths=runtime_paths,
            execution_scope=execution_scope,
            execution_identity=execution_identity,
            worker_key=worker_key,
        )
        cache_private_culture = True
    culture_manager, culture_settings = _resolve_agent_culture(
        agent_name,
        config,
        culture_storage_root,
        model,
        cache_private=cache_private_culture,
    )

    add_culture_to_context: bool | None = None
    update_cultural_knowledge = False
    enable_agentic_culture = False
    if culture_settings is not None:
        add_culture_to_context = culture_settings.add_culture_to_context
        update_cultural_knowledge = culture_settings.update_cultural_knowledge
        enable_agentic_culture = culture_settings.enable_agentic_culture

    # Resolve history settings: per-agent override → defaults.
    # When agent sets one knob, force the other to None to avoid Agno
    # receiving both (it warns and drops num_history_messages).
    if agent_config.num_history_messages is not None:
        num_history_runs = None
        num_history_messages = agent_config.num_history_messages
    elif agent_config.num_history_runs is not None:
        num_history_runs = agent_config.num_history_runs
        num_history_messages = None
    else:
        num_history_runs = defaults.num_history_runs
        num_history_messages = defaults.num_history_messages

    # Track whether we want "all history" to bypass Agno's default after construction
    include_all_history = num_history_runs is None and num_history_messages is None

    compress_tool_results = (
        agent_config.compress_tool_results
        if agent_config.compress_tool_results is not None
        else defaults.compress_tool_results
    )

    max_tool_calls_from_history = (
        agent_config.max_tool_calls_from_history
        if agent_config.max_tool_calls_from_history is not None
        else defaults.max_tool_calls_from_history
    )

    agent = _initialize_agent_instance(
        name=agent_config.display_name,
        id=agent_name,
        role=role,
        model=model,
        tools=tools,
        skills=skills,
        instructions=instructions,
        db=storage,
        learning=_resolve_agent_learning(agent_config, defaults, learning_storage),
        markdown=agent_config.markdown if agent_config.markdown is not None else defaults.markdown,
        knowledge=knowledge if knowledge_enabled else None,
        search_knowledge=knowledge_enabled,
        add_history_to_context=True,
        add_session_summary_to_context=True,
        num_history_runs=num_history_runs,
        num_history_messages=num_history_messages,
        # Keep persisted runs raw even though Agno replays history natively.
        store_history_messages=False,
        culture_manager=culture_manager,
        add_culture_to_context=add_culture_to_context,
        update_cultural_knowledge=update_cultural_knowledge,
        enable_agentic_culture=enable_agentic_culture,
        compress_tool_results=compress_tool_results,
        max_tool_calls_from_history=max_tool_calls_from_history,
    )
    if include_all_history:
        _enable_all_history_replay(agent)

    logger.info(
        "Created agent",
        agent=agent_name,
        display_name=agent_config.display_name,
        tool_count=len(tools),
        loaded_dynamic_toolkits=list(dynamic_tool_selection.loaded_toolkits),
    )

    return agent


def get_agent_ids_for_room(
    room_key: str,
    config: Config,
    runtime_paths: constants.RuntimePaths,
) -> list[str]:
    """Get all agent Matrix IDs assigned to a specific room."""
    config_ids = config.get_ids(runtime_paths)
    # Always include the router agent
    agent_ids = [config_ids[ROUTER_AGENT_NAME].full_id]

    # Add agents from config
    for agent_name, agent_cfg in config.agents.items():
        if room_key in agent_cfg.rooms:
            agent_ids.append(config_ids[agent_name].full_id)
    return agent_ids


def get_rooms_for_entity(entity_name: str, config: Config) -> list[str]:
    """Get the list of room aliases that an entity (agent/team) should be in.

    Args:
        entity_name: Name of the agent or team
        config: Configuration object

    Returns:
        List of room aliases the entity should be in

    """
    # TeamBot check (teams)
    if entity_name in config.teams:
        return config.teams[entity_name].rooms

    # Router agent special case - gets all rooms
    if entity_name == ROUTER_AGENT_NAME:
        return list(config.get_all_configured_rooms())

    # Regular agents
    if entity_name in config.agents:
        return config.agents[entity_name].rooms

    return []


__all__ = [
    "build_agent_toolkit",
    "create_agent",
    "create_session_storage",
    "describe_agent",
    "ensure_default_agent_workspaces",
    "get_agent_ids_for_room",
    "get_agent_session",
    "get_agent_toolkit_names",
    "get_rooms_for_entity",
    "get_team_session",
    "remove_run_by_event_id",
    "show_tool_calls_for_agent",
]
