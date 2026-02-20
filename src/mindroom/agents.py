"""Agent loader that reads agent configurations from YAML file."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

from agno.agent import Agent
from agno.culture.manager import CultureManager
from agno.db.base import SessionType
from agno.db.sqlite import SqliteDb
from agno.learn import LearningMachine, LearningMode, UserMemoryConfig, UserProfileConfig
from agno.run.agent import RunOutput
from agno.session.agent import AgentSession

from . import agent_prompts
from . import tools as _tools_module  # noqa: F401
from .constants import ROUTER_AGENT_NAME, STORAGE_PATH_OBJ, resolve_config_relative_path
from .logging_config import get_logger
from .plugins import load_plugins
from .skills import build_agent_skills
from .tools_metadata import get_tool_by_name

if TYPE_CHECKING:
    from pathlib import Path

    from agno.knowledge.protocol import KnowledgeProtocol
    from agno.models.base import Model

    from .config import AgentConfig, Config, CultureConfig, CultureMode, DefaultsConfig

logger = get_logger(__name__)

# Maximum length for instruction descriptions to include in agent summary
MAX_INSTRUCTION_LENGTH = 100


@dataclass
class CachedCultureManager:
    """Cached culture manager with a signature for invalidation on config changes."""

    signature: tuple[str, str]
    manager: CultureManager


@dataclass(frozen=True)
class CultureAgentSettings:
    """Culture feature flags to apply to the Agent constructor."""

    add_culture_to_context: bool
    update_cultural_knowledge: bool
    enable_agentic_culture: bool


@dataclass
class AdditionalContextChunk:
    """Chunk of preload context with truncation priority metadata."""

    kind: str
    title: str
    body: str


_CULTURE_MANAGER_CACHE: dict[tuple[str, str], CachedCultureManager] = {}


def get_datetime_context(timezone_str: str) -> str:
    """Generate current date and time context for the agent.

    Args:
        timezone_str: Timezone string (e.g., 'America/New_York', 'UTC')

    Returns:
        Formatted string with current date and time information

    """
    tz = ZoneInfo(timezone_str)
    now = datetime.now(tz)

    # Format the datetime in a clear, readable way
    date_str = now.strftime("%A, %B %d, %Y")
    time_str = now.strftime("%H:%M %Z")  # 24-hour format

    return f"""## Current Date and Time
Today is {date_str}.
The current time is {time_str} ({timezone_str} timezone).

"""


def _load_context_files(context_files: list[str]) -> list[AdditionalContextChunk]:
    """Load configured context files."""
    loaded_parts: list[AdditionalContextChunk] = []
    for raw_path in context_files:
        resolved_path = resolve_config_relative_path(raw_path)
        if resolved_path.is_file():
            loaded_parts.append(
                AdditionalContextChunk(
                    kind="personality",
                    title=resolved_path.name,
                    body=resolved_path.read_text(encoding="utf-8").strip(),
                ),
            )
        else:
            logger.warning(f"Context file not found: {resolved_path}")
    return loaded_parts


def _load_memory_dir_context(memory_dir: str, timezone_str: str) -> list[AdditionalContextChunk]:
    """Load MEMORY.md plus today's and yesterday's dated memory files."""
    resolved_dir = resolve_config_relative_path(memory_dir)
    if not resolved_dir.is_dir():
        logger.warning(f"Memory directory not found: {resolved_dir}")
        return []

    memory_parts: list[AdditionalContextChunk] = []
    memory_md = resolved_dir / "MEMORY.md"
    if memory_md.is_file():
        memory_parts.append(
            AdditionalContextChunk(
                kind="memory",
                title="MEMORY.md",
                body=memory_md.read_text(encoding="utf-8").strip(),
            ),
        )

    today = datetime.now(ZoneInfo(timezone_str)).date()
    yesterday = today - timedelta(days=1)
    for target_date in (yesterday, today):
        target_file = resolved_dir / f"{target_date.isoformat()}.md"
        if target_file.is_file():
            memory_parts.append(
                AdditionalContextChunk(
                    kind="daily",
                    title=target_file.name,
                    body=target_file.read_text(encoding="utf-8").strip(),
                ),
            )

    return memory_parts


def _render_context_chunks(section_heading: str, chunks: list[AdditionalContextChunk]) -> str:
    """Render context chunks into a markdown section."""
    rendered = [f"### {chunk.title}\n{chunk.body.strip()}" for chunk in chunks if chunk.body.strip()]
    if not rendered:
        return ""
    return f"{section_heading}\n" + "\n\n".join(rendered) + "\n\n"


def _render_additional_context(
    personality_chunks: list[AdditionalContextChunk],
    memory_chunks: list[AdditionalContextChunk],
) -> str:
    """Render full additional context from personality and memory chunks."""
    parts = [
        _render_context_chunks("## Personality Context", personality_chunks),
        _render_context_chunks("## Memory Context", memory_chunks),
    ]
    return "".join(part for part in parts if part)


def _build_preload_truncation_groups(
    personality_chunks: list[AdditionalContextChunk],
    memory_chunks: list[AdditionalContextChunk],
) -> list[list[AdditionalContextChunk]]:
    """Return truncation groups ordered from least to most critical context."""
    return [
        [chunk for chunk in memory_chunks if chunk.kind == "daily"],
        [chunk for chunk in memory_chunks if chunk.kind == "memory"],
        [chunk for chunk in personality_chunks if chunk.kind == "personality"],
    ]


def _drop_whole_chunks(
    groups: list[list[AdditionalContextChunk]],
    personality_chunks: list[AdditionalContextChunk],
    memory_chunks: list[AdditionalContextChunk],
    max_preload_chars: int,
) -> int:
    """Drop entire chunk bodies (least critical first) until under the cap."""
    omitted = 0
    for group in groups:
        for chunk in group:
            if len(_render_additional_context(personality_chunks, memory_chunks)) <= max_preload_chars:
                return omitted
            if not chunk.body:
                continue
            omitted += len(chunk.body)
            chunk.body = ""
    return omitted


def _trim_chunk_tails(
    groups: list[list[AdditionalContextChunk]],
    personality_chunks: list[AdditionalContextChunk],
    memory_chunks: list[AdditionalContextChunk],
    max_preload_chars: int,
) -> int:
    """Trim from the *end* of chunks to preserve headers/identity at the top."""
    omitted = 0
    for group in groups:
        for chunk in group:
            overflow = len(_render_additional_context(personality_chunks, memory_chunks)) - max_preload_chars
            if overflow <= 0:
                return omitted
            if not chunk.body:
                continue
            remove_count = min(overflow, len(chunk.body))
            chunk.body = chunk.body[: len(chunk.body) - remove_count].rstrip()
            omitted += remove_count
    return omitted


def _apply_preload_cap(
    personality_chunks: list[AdditionalContextChunk],
    memory_chunks: list[AdditionalContextChunk],
    max_preload_chars: int,
) -> tuple[str, int]:
    """Apply hard preload cap with deterministic truncation priority.

    Truncation order (least → most critical): daily → memory → personality.
    First drops whole chunks, then trims from the *end* of remaining chunks.
    """
    rendered = _render_additional_context(personality_chunks, memory_chunks)
    if len(rendered) <= max_preload_chars:
        return rendered, 0

    groups = _build_preload_truncation_groups(personality_chunks, memory_chunks)
    omitted_chars = _drop_whole_chunks(groups, personality_chunks, memory_chunks, max_preload_chars)
    omitted_chars += _trim_chunk_tails(groups, personality_chunks, memory_chunks, max_preload_chars)

    rendered = _render_additional_context(personality_chunks, memory_chunks)
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


def _build_additional_context(
    agent_config: AgentConfig,
    timezone_str: str,
    max_preload_chars: int,
) -> str:
    """Build additional role context from configured files/directories.

    This is evaluated when the agent is created (and re-created on config
    reload), so file content snapshots update on agent hot-reload.
    """
    personality_chunks: list[AdditionalContextChunk] = []
    if agent_config.context_files:
        personality_chunks = _load_context_files(agent_config.context_files)

    memory_chunks: list[AdditionalContextChunk] = []
    if agent_config.memory_dir:
        memory_chunks = _load_memory_dir_context(agent_config.memory_dir, timezone_str)

    additional_context, omitted_chars = _apply_preload_cap(
        personality_chunks,
        memory_chunks,
        max_preload_chars,
    )
    if omitted_chars > 0:
        logger.warning(
            "Preload context exceeded max_preload_chars and was truncated",
            omitted_chars=omitted_chars,
            max_preload_chars=max_preload_chars,
        )
    return additional_context


# Rich prompt mapping - agents that use detailed prompts instead of simple roles
RICH_PROMPTS = {
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


def is_learning_enabled(agent_config: AgentConfig, defaults: DefaultsConfig) -> bool:
    """Check if learning is enabled for an agent, falling back to defaults."""
    learning = agent_config.learning if agent_config.learning is not None else defaults.learning
    return learning is not False


def resolve_agent_learning(
    agent_config: AgentConfig,
    defaults: DefaultsConfig,
    learning_storage: SqliteDb | None = None,
) -> bool | LearningMachine:
    """Resolve Agent.learning setting from MindRoom agent configuration."""
    if not is_learning_enabled(agent_config, defaults):
        return False

    learning_mode = agent_config.learning_mode or defaults.learning_mode
    learning_mode_value = LearningMode.AGENTIC if learning_mode == "agentic" else LearningMode.ALWAYS

    return LearningMachine(
        db=learning_storage,
        user_profile=UserProfileConfig(mode=learning_mode_value),
        user_memory=UserMemoryConfig(mode=learning_mode_value),
    )


def create_session_storage(agent_name: str, storage_path: Path) -> SqliteDb:
    """Create persistent session storage for an agent."""
    sessions_dir = storage_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    return SqliteDb(session_table=f"{agent_name}_sessions", db_file=str(sessions_dir / f"{agent_name}.db"))


def create_learning_storage(agent_name: str, storage_path: Path) -> SqliteDb:
    """Create persistent learning storage for an agent."""
    learning_dir = storage_path / "learning"
    learning_dir.mkdir(parents=True, exist_ok=True)
    return SqliteDb(session_table=f"{agent_name}_learning_sessions", db_file=str(learning_dir / f"{agent_name}.db"))


def create_culture_storage(culture_name: str, storage_path: Path) -> SqliteDb:
    """Create persistent culture storage shared by all agents in a culture."""
    culture_dir = storage_path / "culture"
    culture_dir.mkdir(parents=True, exist_ok=True)
    return SqliteDb(db_file=str(culture_dir / f"{culture_name}.db"))


def _get_agent_session(storage: SqliteDb, session_id: str) -> AgentSession | None:
    """Retrieve and deserialize an AgentSession from storage."""
    raw = storage.get_session(session_id, SessionType.AGENT)
    if raw is None:
        return None
    if isinstance(raw, AgentSession):
        return raw
    if isinstance(raw, dict):
        return AgentSession.from_dict(cast("dict[str, Any]", raw))
    return None


def get_seen_event_ids(session: AgentSession) -> set[str]:
    """Return union of all matrix_seen_event_ids from run metadata."""
    if not session.runs:
        return set()
    seen: set[str] = set()
    for run in session.runs:
        if isinstance(run, RunOutput) and run.metadata:
            seen_ids = run.metadata.get("matrix_seen_event_ids")
            if isinstance(seen_ids, list):
                seen.update(seen_ids)
    return seen


def remove_run_by_event_id(storage: SqliteDb, session_id: str, event_id: str) -> bool:
    """Remove a run whose matrix_event_id matches, save session.

    Returns True if a run was removed.
    """
    session = _get_agent_session(storage, session_id)
    if session is None or not session.runs:
        return False
    original_len = len(session.runs)
    session.runs = [
        run
        for run in session.runs
        if not (isinstance(run, RunOutput) and run.metadata and run.metadata.get("matrix_event_id") == event_id)
    ]
    if len(session.runs) == original_len:
        return False
    storage.upsert_session(session)
    return True


def resolve_culture_settings(mode: CultureMode) -> CultureAgentSettings:
    """Map a culture mode to Agno culture feature flags."""
    if mode == "automatic":
        return CultureAgentSettings(
            add_culture_to_context=True,
            update_cultural_knowledge=True,
            enable_agentic_culture=False,
        )
    if mode == "agentic":
        return CultureAgentSettings(
            add_culture_to_context=True,
            update_cultural_knowledge=False,
            enable_agentic_culture=True,
        )
    return CultureAgentSettings(
        add_culture_to_context=True,
        update_cultural_knowledge=False,
        enable_agentic_culture=False,
    )


def _culture_signature(culture_config: CultureConfig) -> tuple[str, str]:
    return (culture_config.mode, culture_config.description)


def resolve_agent_culture(
    agent_name: str,
    config: Config,
    storage_path: Path,
    model: Model,
) -> tuple[CultureManager | None, CultureAgentSettings | None]:
    """Resolve shared culture manager and feature flags for an agent."""
    culture_assignment = config.get_agent_culture(agent_name)
    if culture_assignment is None:
        return None, None

    culture_name, culture_config = culture_assignment
    settings = resolve_culture_settings(culture_config.mode)
    cache_key = (str(storage_path.resolve()), culture_name)
    signature = _culture_signature(culture_config)
    cached_manager = _CULTURE_MANAGER_CACHE.get(cache_key)
    if cached_manager is not None and cached_manager.signature == signature:
        cached_manager.manager.model = model
        return cached_manager.manager, settings

    culture_scope = culture_config.description.strip() or "Shared best practices and principles."
    culture_manager = CultureManager(
        model=model,
        db=create_culture_storage(culture_name, storage_path),
        culture_capture_instructions=f"Culture '{culture_name}': {culture_scope}",
        add_knowledge=culture_config.mode != "manual",
        update_knowledge=culture_config.mode != "manual",
        delete_knowledge=False,
        clear_knowledge=False,
    )
    _CULTURE_MANAGER_CACHE[cache_key] = CachedCultureManager(
        signature=signature,
        manager=culture_manager,
    )
    return culture_manager, settings


def create_agent(  # noqa: PLR0915, C901, PLR0912
    agent_name: str,
    config: Config,
    *,
    storage_path: Path | None = None,
    knowledge: KnowledgeProtocol | None = None,
    include_interactive_questions: bool = True,
    config_path: Path | None = None,
) -> Agent:
    """Create an agent instance from configuration.

    Args:
        agent_name: Name of the agent to create
        config: Application configuration
        storage_path: Runtime storage path. Falls back to the
            module-level ``STORAGE_PATH_OBJ`` when *None*.
        knowledge: Optional shared knowledge base instance for RAG-enabled agents.
        include_interactive_questions: Whether to include the interactive
            question authoring prompt. Set to False for channels that do not
            support Matrix reaction-based question flows.
        config_path: Path to the YAML config file used by tools that
            read/write config at runtime (e.g. ``self_config``).  Falls back
            to the module-level ``CONFIG_PATH`` when *None*.

    Returns:
        Configured Agent instance

    Raises:
        ValueError: If agent_name is not found in configuration

    """
    from .ai import get_model_instance  # noqa: PLC0415

    resolved_storage_path = storage_path if storage_path is not None else STORAGE_PATH_OBJ

    # Use passed config (config_path is deprecated)
    agent_config = config.get_agent(agent_name)
    defaults = config.defaults

    load_plugins(config)

    tool_names = config.get_agent_tools(agent_name)
    sandbox_tools = config.get_agent_sandbox_tools(agent_name)

    # Create tools
    tools: list = []  # Use list type to satisfy Agent's parameter type
    for tool_name in tool_names:
        try:
            if tool_name == "memory":
                from .custom_tools.memory import MemoryTools  # noqa: PLC0415

                tools.append(
                    MemoryTools(
                        agent_name=agent_name,
                        storage_path=resolved_storage_path,
                        config=config,
                    ),
                )
            elif tool_name == "self_config":
                from .custom_tools.self_config import SelfConfigTools  # noqa: PLC0415

                tools.append(SelfConfigTools(agent_name=agent_name, config_path=config_path))
            else:
                tools.append(get_tool_by_name(tool_name, sandbox_tools_override=sandbox_tools))
        except ValueError as e:
            logger.warning(f"Could not load tool '{tool_name}' for agent '{agent_name}': {e}")

    # Auto-inject self-config tool when allow_self_config is enabled
    allow_self_config = (
        agent_config.allow_self_config if agent_config.allow_self_config is not None else defaults.allow_self_config
    )
    if allow_self_config and not any(getattr(tool, "name", None) == "self_config" for tool in tools):
        from .custom_tools.self_config import SelfConfigTools  # noqa: PLC0415

        tools.append(SelfConfigTools(agent_name=agent_name, config_path=config_path))

    storage = create_session_storage(agent_name, resolved_storage_path)
    learning_storage = (
        create_learning_storage(agent_name, resolved_storage_path)
        if is_learning_enabled(agent_config, defaults)
        else None
    )

    # Get model config for identity context
    model_name = agent_config.model or "default"
    if model_name in config.models:
        model_config = config.models[model_name]
        model_provider = model_config.provider.title()  # Capitalize provider name
        model_id = model_config.id
    else:
        # Fallback if model not found
        model_provider = "AI"
        model_id = model_name

    # Add identity context to all agents using the unified template
    identity_context = agent_prompts.AGENT_IDENTITY_CONTEXT.format(
        display_name=agent_config.display_name,
        agent_name=agent_name,
        model_provider=model_provider,
        model_id=model_id,
    )

    # Add current date and time context with user's configured timezone
    datetime_context = get_datetime_context(config.timezone)

    # Combine identity and datetime contexts
    full_context = identity_context + datetime_context

    full_context += _build_additional_context(
        agent_config,
        config.timezone,
        config.defaults.max_preload_chars,
    )

    # Use rich prompt if available, otherwise use YAML config
    if agent_name in RICH_PROMPTS:
        logger.info(f"Using rich prompt for agent: {agent_name}")
        # Prepend full context to the rich prompt
        role = full_context + RICH_PROMPTS[agent_name]
        instructions = []  # Instructions are in the rich prompt
    else:
        logger.info(f"Using YAML config for agent: {agent_name}")
        # For YAML agents, prepend full context to role and keep original instructions
        role = full_context + agent_config.role
        instructions = agent_config.instructions

    # Create agent with defaults applied
    model = get_model_instance(config, agent_config.model)
    logger.info(f"Creating agent '{agent_name}' with model: {model.__class__.__name__}(id={model.id})")

    skills = build_agent_skills(agent_name, config)
    if skills and skills.get_skill_names():
        instructions.append(agent_prompts.SKILLS_TOOL_USAGE_PROMPT)

    if include_interactive_questions:
        instructions.append(agent_prompts.INTERACTIVE_QUESTION_PROMPT)

    knowledge_enabled = bool(agent_config.knowledge_bases) and knowledge is not None
    culture_manager, culture_settings = resolve_agent_culture(
        agent_name,
        config,
        resolved_storage_path,
        model,
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

    enable_session_summaries = (
        agent_config.enable_session_summaries
        if agent_config.enable_session_summaries is not None
        else defaults.enable_session_summaries
    )

    max_tool_calls_from_history = (
        agent_config.max_tool_calls_from_history
        if agent_config.max_tool_calls_from_history is not None
        else defaults.max_tool_calls_from_history
    )

    agent = Agent(
        name=agent_config.display_name,
        id=agent_name,
        role=role,
        model=model,
        tools=tools,
        skills=skills,
        instructions=instructions,
        db=storage,
        learning=resolve_agent_learning(agent_config, defaults, learning_storage),
        markdown=agent_config.markdown if agent_config.markdown is not None else defaults.markdown,
        knowledge=knowledge if knowledge_enabled else None,
        search_knowledge=knowledge_enabled,
        add_history_to_context=True,
        num_history_runs=num_history_runs,
        num_history_messages=num_history_messages,
        culture_manager=culture_manager,
        add_culture_to_context=add_culture_to_context,
        update_cultural_knowledge=update_cultural_knowledge,
        enable_agentic_culture=enable_agentic_culture,
        compress_tool_results=compress_tool_results,
        enable_session_summaries=enable_session_summaries,
        max_tool_calls_from_history=max_tool_calls_from_history,
    )
    # Agno hardcodes num_history_runs=3 when both are None. Override after
    # construction so get_messages receives None and returns all runs.
    if include_all_history:
        agent.num_history_runs = None

    logger.info(f"Created agent '{agent_name}' ({agent_config.display_name}) with {len(tools)} tools")

    return agent


def describe_agent(agent_name: str, config: Config) -> str:
    """Generate a description of an agent or team based on its configuration.

    Args:
        agent_name: Name of the agent or team to describe
        config: Application configuration

    Returns:
        Human-readable description of the agent or team

    """
    # Handle built-in router agent
    if agent_name == ROUTER_AGENT_NAME:
        return (
            "router\n"
            "  - Route messages to the most appropriate agent based on context and expertise.\n"
            "  - Analyzes incoming messages and determines which agent is best suited to respond."
        )

    # Check if it's a team
    if agent_name in config.teams:
        team_config = config.teams[agent_name]
        parts = [f"{agent_name}"]
        if team_config.role:
            parts.append(f"- {team_config.role}")
        parts.append(f"- Team of agents: {', '.join(team_config.agents)}")
        parts.append(f"- Collaboration mode: {team_config.mode}")
        return "\n  ".join(parts)

    # Check if agent exists
    if agent_name not in config.agents:
        return f"{agent_name}: Unknown agent or team"

    agent_config = config.agents[agent_name]

    # Start with agent name (not display name, for routing consistency)
    parts = [f"{agent_name}"]
    if agent_config.role:
        parts.append(f"- {agent_config.role}")

    # Add tools if any
    effective_tools = config.get_agent_tools(agent_name)
    if effective_tools:
        tool_list = ", ".join(effective_tools)
        parts.append(f"- Tools: {tool_list}")

    # Add key instructions if any
    if agent_config.instructions:
        # Take first instruction as it's usually the most descriptive
        first_instruction = agent_config.instructions[0]
        if len(first_instruction) < MAX_INSTRUCTION_LENGTH:  # Only include if reasonably short
            parts.append(f"- {first_instruction}")

    return "\n  ".join(parts)


def get_agent_ids_for_room(room_key: str, config: Config) -> list[str]:
    """Get all agent Matrix IDs assigned to a specific room."""
    # Always include the router agent
    agent_ids = [config.ids[ROUTER_AGENT_NAME].full_id]

    # Add agents from config
    for agent_name, agent_cfg in config.agents.items():
        if room_key in agent_cfg.rooms:
            agent_ids.append(config.ids[agent_name].full_id)
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
