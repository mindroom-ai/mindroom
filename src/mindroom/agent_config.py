"""Agent loader that reads agent configurations from YAML file."""

from pathlib import Path

import yaml
from agno.agent import Agent
from agno.models.base import Model
from agno.storage.sqlite import SqliteStorage

from .logging_config import get_logger
from .models import Config
from .tools import get_tool_by_name

logger = get_logger(__name__)

# Constants
ROUTER_AGENT_NAME = "router"

# Default path to agents configuration file
DEFAULT_AGENTS_CONFIG = Path(__file__).parent.parent.parent / "config.yaml"

# Global caches
_config_cache: dict[Path, Config] = {}
_agent_cache: dict[str, Agent] = {}


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

    # Check cache
    if path in _config_cache:
        return _config_cache[path]

    if not path.exists():
        raise FileNotFoundError(f"Agent configuration file not found: {path}")

    with open(path) as f:
        data = yaml.safe_load(f)

    config = Config(**data)
    _config_cache[path] = config
    logger.info(f"Loaded agent configuration from {path}")
    logger.info(f"Found {len(config.agents)} agent configurations")

    return config


def create_agent(agent_name: str, model: Model, storage_path: Path, config_path: Path | None = None) -> Agent:
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
    # Check cache
    cache_key = f"{agent_name}:{model.__class__.__name__}:{model.id}"
    if cache_key in _agent_cache:
        return _agent_cache[cache_key]

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

    # Create agent with defaults applied
    agent = Agent(
        name=agent_config.display_name,
        role=agent_config.role,
        model=model,
        tools=tools,
        instructions=agent_config.instructions,
        storage=storage,
        add_history_to_messages=agent_config.add_history_to_messages
        if agent_config.add_history_to_messages is not None
        else defaults.add_history_to_messages,
        num_history_runs=agent_config.num_history_runs or defaults.num_history_runs,
        markdown=agent_config.markdown if agent_config.markdown is not None else defaults.markdown,
    )

    # Cache the agent
    _agent_cache[cache_key] = agent
    logger.info(f"Created agent '{agent_name}' ({agent_config.display_name}) with {len(tools)} tools")

    return agent


def describe_agent(agent_name: str, config_path: Path | None = None) -> str:
    """Generate a description of an agent based on its configuration.

    Args:
        agent_name: Name of the agent to describe
        config_path: Optional path to configuration file

    Returns:
        Human-readable description of the agent
    """
    # Handle built-in router agent
    if agent_name == ROUTER_AGENT_NAME:
        return "router\n  - Route messages to the most appropriate agent based on context and expertise.\n  - Analyzes incoming messages and determines which agent is best suited to respond."

    config = load_config(config_path)

    # Check if agent exists
    if agent_name not in config.agents:
        return f"{agent_name}: Unknown agent"

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


def clear_cache() -> None:
    """Clear all caches."""
    _config_cache.clear()
    _agent_cache.clear()
