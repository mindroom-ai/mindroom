"""Agent module exports and registry."""

from collections.abc import Callable

from agno.agent import Agent
from agno.models.base import Model

from .base import clear_agent_cache, create_agent

# Registry of all available agents
# Only including agents that don't require additional dependencies
AGENT_REGISTRY: dict[str, Callable[[Model], Agent]] = {}


def register_agent(name: str, func: Callable[[Model], Agent]) -> None:
    """Register an agent creation function.

    Args:
        name: The name to register the agent under
        func: The agent creation function
    """
    AGENT_REGISTRY[name] = func


def get_agent(agent_name: str, model: Model) -> Agent:
    """Get an agent by name.

    Args:
        agent_name: Name of the agent to create
        model: The AI model to use

    Returns:
        Agent instance

    Raises:
        ValueError: If agent_name is not recognized
    """
    if agent_name not in AGENT_REGISTRY:
        available = ", ".join(sorted(AGENT_REGISTRY.keys()))
        msg = f"Unknown agent: {agent_name}. Available agents: {available}"
        raise ValueError(msg)

    return AGENT_REGISTRY[agent_name](model)


def list_agents() -> list[str]:
    """Get a list of all available agent names."""
    return sorted(AGENT_REGISTRY.keys())


# Import agent modules to trigger registration
from . import calculator, code, general, shell, summary  # noqa: E402, F401

__all__ = [
    "create_agent",
    "clear_agent_cache",
    "get_agent",
    "list_agents",
    "AGENT_REGISTRY",
    "register_agent",
]
