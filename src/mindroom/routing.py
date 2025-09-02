"""Simple AI routing for multi-agent threads."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agno.agent import Agent
from pydantic import BaseModel, Field

from .agents import describe_agent
from .ai import get_model_instance
from .logging_config import get_logger
from .matrix.identity import MatrixID

if TYPE_CHECKING:
    from .config import Config

logger = get_logger(__name__)


class AgentSuggestion(BaseModel):
    """Structured output for agent routing decisions."""

    agent_name: str = Field(description="The name of the agent that should respond")
    reasoning: str = Field(description="Brief explanation of why this agent was chosen")


async def suggest_agent_for_message(
    message: str,
    available_agents: list[MatrixID],
    config: Config,
    thread_context: list[dict[str, Any]] | None = None,
) -> str | None:
    """Use AI to suggest which agent should respond to a message."""
    try:
        # The available_agents list already contains only configured agents
        agent_names = [mid.agent_name(config) for mid in available_agents]

        # Build agent descriptions
        agent_descriptions = []
        for agent_name in agent_names:
            assert agent_name is not None
            description = describe_agent(agent_name, config)
            agent_descriptions.append(f"{agent_name}:\n  {description}")

        agents_info = "\n\n".join(agent_descriptions)

        prompt = f"""Decide which agent should respond to this message.

Available agents and their capabilities:

{agents_info}

Message: "{message}"

Choose the most appropriate agent based on their role, tools, and instructions."""

        if thread_context:
            context = "Previous messages:\n"
            for msg in thread_context[-3:]:  # Last 3 messages
                sender = msg.get("sender", "")
                # For display, just show the username or domain
                if sender.startswith("@") and ":" in sender:
                    sender_id = MatrixID.parse(sender)
                    # Show agent name or just domain for users
                    sender = sender_id.agent_name(config) or sender_id.domain
                body = msg.get("body", "")[:100]
                context += f"{sender}: {body}\n"
            prompt = context + "\n" + prompt

        # Get router model from config
        router_model_name = config.router.model

        model = get_model_instance(config, router_model_name)
        logger.info(f"Using router model: {router_model_name} -> {model.__class__.__name__}(id={model.id})")

        agent = Agent(
            name="Router",
            role="Route messages to appropriate agents",
            model=model,
            response_model=AgentSuggestion,
        )

        response = await agent.arun(prompt, session_id="routing")
        suggestion = response.content

        # With response_model, we should always get the correct type
        if not isinstance(suggestion, AgentSuggestion):
            logger.error(
                "Unexpected response type from AI routing",
                expected="AgentSuggestion",
                actual=type(suggestion).__name__,
            )
            return None

        # The AI should only suggest agents from the available list
        if suggestion.agent_name not in agent_names:
            logger.warning("AI suggested invalid agent", suggested=suggestion.agent_name, available=agent_names)
            return None

        logger.info("Routing decision", agent=suggestion.agent_name, reason=suggestion.reasoning)
    except Exception as e:
        # Log error and return None - the router will fall back to not routing
        logger.exception("Routing failed", error=str(e))
        return None
    else:
        return suggestion.agent_name
