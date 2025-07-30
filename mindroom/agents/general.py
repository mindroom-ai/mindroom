"""General-purpose conversational assistant agent."""

from agno.agent import Agent
from agno.models.base import Model

from . import register_agent
from .base import create_agent


def create_general_agent(model: Model) -> Agent:
    """Create a general-purpose assistant agent."""
    return create_agent(
        agent_name="general",
        display_name="GeneralAgent",
        role="A general-purpose assistant that provides helpful, conversational responses to users.",
        model=model,
        tools=[],
        instructions=[
            "Always provide a clear, helpful response to the user after any reasoning.",
            "Remember context from the conversation.",
            "Be conversational and friendly.",
            "Ask clarifying questions when needed.",
        ],
        num_history_runs=5,
    )


# Register this agent
register_agent("general", create_general_agent)
