"""Summary agent for text summarization and content extraction."""

from agno.agent import Agent
from agno.models.base import Model

from . import register_agent
from .base import create_agent


def create_summary_agent(model: Model) -> Agent:
    """Create a summary agent for text summarization."""
    return create_agent(
        agent_name="summary",
        display_name="SummaryAgent",
        role="Summarize long texts, articles, conversations, and documents.",
        model=model,
        tools=[],
        instructions=[
            "Identify the main points and key arguments",
            "Preserve important details while removing redundancy",
            "Maintain the original tone and intent",
            "Use clear, concise language",
            "Organize summaries logically (chronological, thematic, etc.)",
            "Include relevant quotes when impactful",
            "Note any biases or limitations in the source material",
            "Provide different summary lengths if requested (brief, standard, detailed)",
        ],
        num_history_runs=5,
    )


# Register this agent
register_agent("summary", create_summary_agent)
