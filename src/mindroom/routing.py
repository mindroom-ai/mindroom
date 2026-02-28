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
    from .config.main import Config

logger = get_logger(__name__)


class AgentSuggestion(BaseModel):
    """Structured output for agent routing decisions."""

    agent_name: str = Field(description="The name of the agent that should respond")
    reasoning: str = Field(description="Brief explanation of why this agent was chosen")


async def suggest_agent(
    message: str,
    available_agent_names: list[str],
    config: Config,
    thread_context: list[dict[str, Any]] | None = None,
) -> str | None:
    """Use AI to suggest which agent should respond to a message.

    This is the core routing logic, independent of any transport layer.

    Args:
        message: The user message to route.
        available_agent_names: Plain agent names (e.g. ["code", "research"]).
        config: Application configuration.
        thread_context: Optional recent messages for context.
            Each dict should have "sender" and "body" keys.

    Returns:
        The suggested agent name, or None if routing fails.

    """
    try:
        # Build agent descriptions
        agent_descriptions = []
        for agent_name in available_agent_names:
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
            output_schema=AgentSuggestion,
        )

        response = await agent.arun(prompt, session_id="routing")
        suggestion = response.content

        # With output_schema, we should always get the correct type
        if not isinstance(suggestion, AgentSuggestion):
            logger.error(
                "Unexpected response type from AI routing",
                expected="AgentSuggestion",
                actual=type(suggestion).__name__,
            )
            return None

        # The AI should only suggest agents from the available list
        if suggestion.agent_name not in available_agent_names:
            logger.warning(
                "AI suggested invalid agent",
                suggested=suggestion.agent_name,
                available=available_agent_names,
            )
            return None

        logger.info("Routing decision", agent=suggestion.agent_name, reason=suggestion.reasoning)
    except Exception as e:
        # Log error and return None - the router will fall back to not routing
        logger.exception("Routing failed", error=str(e))
        return None
    else:
        return suggestion.agent_name


async def suggest_agent_for_message(
    message: str,
    available_agents: list[MatrixID],
    config: Config,
    thread_context: list[dict[str, Any]] | None = None,
) -> str | None:
    """Use AI to suggest which agent should respond to a message.

    Matrix-aware wrapper around suggest_agent() that converts MatrixID
    objects to plain agent names and resolves sender identities in
    thread context.
    """
    agent_names = [name for mid in available_agents if (name := mid.agent_name(config)) is not None]

    # Resolve Matrix sender IDs to readable names for thread context
    resolved_context = None
    if thread_context:
        resolved_context = []
        for msg in thread_context:
            sender = msg.get("sender", "")
            if sender.startswith("@") and ":" in sender:
                sender_id = MatrixID.parse(sender)
                sender = sender_id.agent_name(config) or sender_id.domain
            resolved_context.append({"sender": sender, "body": msg.get("body", "")})

    return await suggest_agent(message, agent_names, config, resolved_context)
