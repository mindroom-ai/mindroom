"""Agent loader that reads agent configurations from YAML file."""

from pathlib import Path

import yaml
from agno.agent import Agent
from agno.storage.sqlite import SqliteStorage

from . import agent_prompts
from .logging_config import get_logger
from .models import Config
from .tools import get_tool_by_name

logger = get_logger(__name__)

# Constants
ROUTER_AGENT_NAME = "router"

# Default path to agents configuration file
DEFAULT_AGENTS_CONFIG = Path(__file__).parent.parent.parent / "config.yaml"

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


def load_config(config_path: Path | None = None) -> Config:
    """Load agent configuration from YAML file.

    Args:
        config_path: Path to agents configuration file. If None, uses default.

    Returns:
        Config object

    Raises:
        FileNotFoundError: If configuration file not found
    """
    path = config_path or DEFAULT_AGENTS_CONFIG

    if not path.exists():
        raise FileNotFoundError(f"Agent configuration file not found: {path}")

    with open(path) as f:
        data = yaml.safe_load(f)

    # Handle None values for optional dictionaries
    if data.get("teams") is None:
        data["teams"] = {}
    if data.get("room_models") is None:
        data["room_models"] = {}

    config = Config(**data)
    logger.info(f"Loaded agent configuration from {path}")
    logger.info(f"Found {len(config.agents)} agent configurations")

    return config


def create_agent(agent_name: str, storage_path: Path, config_path: Path | None = None) -> Agent:
    """Create an agent instance from configuration.

    Args:
        agent_name: Name of the agent to create
        model: The AI model to use
        storage_path: Base directory for storing agent data
        config_path: Optional path to configuration file

    Returns:
        Configured Agent instance

    Raises:
        ValueError: If agent_name is not found in configuration
    """
    from .ai import get_model_instance

    # Load config and get agent
    config = load_config(config_path)
    agent_config = config.get_agent(agent_name)
    defaults = config.defaults

    # Create tools
    tools = []
    for tool_name in agent_config.tools:
        try:
            tool = get_tool_by_name(tool_name)
            tools.append(tool)
        except ValueError as e:
            logger.warning(f"Could not load tool '{tool_name}' for agent '{agent_name}': {e}")

    # Create storage
    storage_path.mkdir(parents=True, exist_ok=True)
    storage = SqliteStorage(table_name=f"{agent_name}_sessions", db_file=str(storage_path / f"{agent_name}.db"))

    # Add identity context to all agents using the unified template
    identity_context = agent_prompts.AGENT_IDENTITY_CONTEXT.format(
        display_name=agent_config.display_name, agent_name=agent_name
    )

    # Use rich prompt if available, otherwise use YAML config
    if agent_name in RICH_PROMPTS:
        logger.info(f"Using rich prompt for agent: {agent_name}")
        # Prepend identity context to the rich prompt
        role = identity_context + RICH_PROMPTS[agent_name]
        instructions = []  # Instructions are in the rich prompt
    else:
        logger.info(f"Using YAML config for agent: {agent_name}")
        # For YAML agents, prepend identity to role and keep original instructions
        role = identity_context + agent_config.role
        instructions = agent_config.instructions

    # Create agent with defaults applied
    model = get_model_instance(agent_config.model)
    logger.info(f"Creating agent '{agent_name}' with model: {model.__class__.__name__}(id={model.id})")
    logger.info(f"Storage path: {storage_path}, DB file: {storage_path / f'{agent_name}.db'}")

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


def describe_agent(agent_name: str, config_path: Path | None = None) -> str:
    """Generate a description of an agent or team based on its configuration.

    Args:
        agent_name: Name of the agent or team to describe
        config_path: Optional path to configuration file

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

    config = load_config(config_path)

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
        if len(first_instruction) < 100:  # Only include if reasonably short
            parts.append(f"- {first_instruction}")

    return "\n  ".join(parts)


def get_agent_ids_for_room(room_key: str, config: Config | None = None, homeserver: str | None = None) -> list[str]:
    """Get all agent Matrix IDs assigned to a specific room."""
    if config is None:
        config = load_config()

    from .matrix import MATRIX_HOMESERVER
    from .matrix.identity import MatrixID, extract_server_name_from_homeserver

    # Determine server name
    server_url = homeserver or MATRIX_HOMESERVER
    server_name = extract_server_name_from_homeserver(server_url)

    # Always include the router agent
    agent_ids = [MatrixID.from_agent(ROUTER_AGENT_NAME, server_name).full_id]

    # Add agents from config
    for agent_name, agent_cfg in config.agents.items():
        if room_key in agent_cfg.rooms:
            agent_ids.append(MatrixID.from_agent(agent_name, server_name).full_id)

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
