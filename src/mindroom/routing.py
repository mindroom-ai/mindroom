"""Simple AI routing for multi-agent threads."""

from typing import Any

from agno.agent import Agent
from pydantic import BaseModel, Field

from .ai import get_model_instance
from .logging_config import get_logger

logger = get_logger(__name__)


class AgentSuggestion(BaseModel):
    """Structured output for agent routing decisions."""

    agent_name: str = Field(description="The name of the agent that should respond")
    reasoning: str = Field(description="Brief explanation of why this agent was chosen")


async def suggest_agent_for_message(
    message: str,
    available_agents: list[str],
    thread_context: list[dict[str, Any]] | None = None,
) -> str | None:
    """Use AI to suggest which agent should respond to a message."""
    try:
        agents_list = ", ".join(available_agents)
        prompt = f"""Decide which agent should respond to this message.

Available agents: {agents_list}

Agent capabilities:
- calculator: Math, calculations, numbers
- general: General conversation, explanations
- code: Programming, development
- shell: System commands, terminal
- summary: Text summarization
- research: Information lookup
- finance: Financial analysis
- news: Current events
- data_analyst: Data analysis

Message: "{message}"

Choose the most appropriate agent."""

        if thread_context:
            context = "Previous messages:\n"
            for msg in thread_context[-3:]:  # Last 3 messages
                sender = msg.get("sender", "").split(":")[-1]
                body = msg.get("body", "")[:100]
                context += f"{sender}: {body}\n"
            prompt = context + "\n" + prompt

        model = get_model_instance()
        agent = Agent(
            name="Router",
            role="Route messages to appropriate agents",
            model=model,
            response_model=AgentSuggestion,
        )

        response = await agent.arun(prompt, session_id="routing")
        suggestion = response.content

        if not isinstance(suggestion, AgentSuggestion):
            return None

        if suggestion.agent_name not in available_agents:
            logger.warning(f"Suggested unavailable agent: {suggestion.agent_name}")
            return available_agents[0]  # Fallback

        logger.info(f"Routing to {suggestion.agent_name}: {suggestion.reasoning}")
        return suggestion.agent_name

    except Exception as e:
        logger.error(f"Routing error: {e}")
        return None
