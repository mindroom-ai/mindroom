"""Agent loader that reads agent configurations from YAML file."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from agno.agent import Agent
from agno.storage.sqlite import SqliteStorage

from . import agent_prompts
from . import tools as _tools_module  # noqa: F401
from .constants import ROUTER_AGENT_NAME, SESSIONS_DIR
from .logging_config import get_logger
from .tools_metadata import get_tool_by_name

if TYPE_CHECKING:
    from .config import Config

logger = get_logger(__name__)

# Maximum length for instruction descriptions to include in agent summary
MAX_INSTRUCTION_LENGTH = 100


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


def create_agent(agent_name: str, config: Config) -> Agent:
    """Create an agent instance from configuration.

    Args:
        agent_name: Name of the agent to create
        config: Application configuration

    Returns:
        Configured Agent instance

    Raises:
        ValueError: If agent_name is not found in configuration

    """
    from .ai import get_model_instance  # noqa: PLC0415

    # Use passed config (config_path is deprecated)
    agent_config = config.get_agent(agent_name)
    defaults = config.defaults

    # Create tools
    tools: list = []  # Use list type to satisfy Agent's parameter type
    for tool_name in agent_config.tools:
        try:
            tool = get_tool_by_name(tool_name)
            tools.append(tool)
        except ValueError as e:
            logger.warning(f"Could not load tool '{tool_name}' for agent '{agent_name}': {e}")

    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    storage = SqliteStorage(table_name=f"{agent_name}_sessions", db_file=str(SESSIONS_DIR / f"{agent_name}.db"))

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

    instructions.append(agent_prompts.INTERACTIVE_QUESTION_PROMPT)

    agent = Agent(
        name=agent_config.display_name,
        role=role,
        model=model,
        tools=tools,
        instructions=instructions,
        storage=storage,
        add_history_to_messages=agent_config.add_history_to_messages
        if agent_config.add_history_to_messages is not None
        else defaults.add_history_to_messages,
        num_history_runs=agent_config.num_history_runs or defaults.num_history_runs,
        markdown=agent_config.markdown if agent_config.markdown is not None else defaults.markdown,
    )
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
    if agent_config.tools:
        tool_list = ", ".join(agent_config.tools)
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
