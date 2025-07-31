"""Agent loader that reads agent configurations from YAML file."""

from pathlib import Path

import yaml
from agno.agent import Agent
from agno.models.base import Model
from agno.storage.sqlite import SqliteStorage
from loguru import logger

from .models import AgentConfig, AgentsConfig
from .tools import get_tool_by_name

# Default path to agents configuration file
DEFAULT_AGENTS_CONFIG = Path(__file__).parent.parent.parent / "agents.yaml"

# Global caches
_config_cache: dict[Path, AgentsConfig] = {}
_agent_cache: dict[str, Agent] = {}


def load_config(config_path: Path | None = None) -> AgentsConfig:
    """Load agent configuration from YAML file.

    Args:
        config_path: Path to agents configuration file. If None, uses default.

    Returns:
        AgentsConfig object

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

    config = AgentsConfig(**data)
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


def list_agents(config_path: Path | None = None) -> list[str]:
    """Get a list of all available agent names from configuration.

    Args:
        config_path: Optional path to configuration file

    Returns:
        Sorted list of agent names
    """
    config = load_config(config_path)
    return config.list_agents()


def get_agent_info(agent_name: str, config_path: Path | None = None) -> AgentConfig:
    """Get information about a specific agent.

    Args:
        agent_name: Name of the agent
        config_path: Optional path to configuration file

    Returns:
        AgentConfig for the requested agent

    Raises:
        ValueError: If agent_name is not found
    """
    config = load_config(config_path)
    return config.get_agent(agent_name)


def clear_cache() -> None:
    """Clear all caches."""
    _config_cache.clear()
    _agent_cache.clear()
