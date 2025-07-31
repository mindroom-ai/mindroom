"""Base agent functionality and shared utilities."""

from agno.agent import Agent
from agno.models.base import Model
from agno.storage.sqlite import SqliteStorage

# A simple in-memory cache for agent instances
_agent_cache: dict[str, Agent] = {}


def create_agent(
    agent_name: str,
    display_name: str,
    role: str,
    model: Model,
    tools: list | None = None,
    instructions: str | list[str] | None = None,
    num_history_runs: int = 5,
) -> Agent:
    """Create and cache an agent instance with reasoning and memory.

    Args:
        agent_name: Unique identifier for the agent (e.g., "research", "code")
        display_name: Display name for the agent (e.g., "ResearchAgent")
        role: Description of the agent's role
        model: The AI model to use
        tools: List of tools available to the agent
        instructions: Additional instructions for the agent (string or list of strings)
        num_history_runs: Number of historical runs to include in context

    Returns:
        Cached or newly created Agent instance
    """
    # Create a cache key that includes both agent name and model info
    cache_key = f"{agent_name}:{model.__class__.__name__}:{model.id}"
    if cache_key in _agent_cache:
        return _agent_cache[cache_key]

    storage = SqliteStorage(table_name=f"{agent_name}_sessions", db_file=f"tmp/{agent_name}.db")

    agent = Agent(
        name=display_name,
        role=role,
        model=model,
        tools=tools or [],
        instructions=instructions,
        storage=storage,
        add_history_to_messages=True,
        num_history_runs=num_history_runs,
        markdown=True,
    )

    _agent_cache[cache_key] = agent
    return agent


def clear_agent_cache() -> None:
    """Clear the agent cache. Useful for testing or reloading agents."""
    _agent_cache.clear()
