"""Router agent that decides which agent should respond to a message.

IMPORTANT: The router agent is SPECIAL and different from regular agents:
1. It uses structured output (Pydantic models) instead of free-form text
2. It doesn't provide answers - it only suggests which agent should respond
3. It only activates in multi-agent threads when no specific agent is mentioned
4. It uses a different bot class (RouterBot) with specialized logic
5. It automatically joins all rooms where multiple agents are present

The router helps coordinate multi-agent conversations by intelligently routing
messages to the most appropriate specialist agent.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .logging_config import get_logger

logger = get_logger(__name__)


class AgentSuggestion(BaseModel):
    """Structured output for agent routing decisions."""

    agent_name: str = Field(
        description="The name of the agent that should respond (e.g., 'calculator', 'general', 'code')"
    )
    reasoning: str = Field(description="Brief explanation of why this agent was chosen")
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence score between 0 and 1")


@dataclass
class RouterAgent:
    """Agent that routes messages to appropriate specialized agents."""

    def create_routing_prompt(
        self, message: str, available_agents: list[str], thread_context: list[dict[str, Any]] | None = None
    ) -> str:
        """Create prompt for routing decision."""
        agents_list = ", ".join(available_agents)

        prompt = f"""You are a routing agent that decides which specialized agent should respond to a message.

Available agents: {agents_list}

Agent capabilities:
- calculator: Mathematical calculations, arithmetic, algebra
- general: General questions, casual conversation, explanations
- code: Programming, code generation, debugging
- shell: System commands, shell scripting, terminal operations
- summary: Summarizing text, creating concise overviews
- research: Finding information, web searches, fact-checking
- finance: Financial calculations, investment analysis, budgeting
- news: Current events, news summaries, trends
- data_analyst: Data analysis, statistics, visualization

Message to route: "{message}"
"""

        if thread_context:
            context_summary = self._summarize_thread(thread_context)
            prompt += f"\nThread context: {context_summary}"

        prompt += "\n\nAnalyze the message and determine which agent is best suited to respond."

        return prompt

    def _summarize_thread(self, thread_context: list[dict[str, Any]]) -> str:
        """Create a brief summary of thread context."""
        if not thread_context:
            return "No previous messages in thread"

        # Take last 3 messages for context
        recent = thread_context[-3:]
        summaries = []

        for msg in recent:
            sender = msg.get("sender", "unknown").split(":")[-1]
            body = msg.get("body", "")[:100]  # First 100 chars
            summaries.append(f"{sender}: {body}")

        return " | ".join(summaries)

    async def suggest_agent(
        self,
        message: str,
        available_agents: list[str],
        thread_context: list[dict[str, Any]] | None = None,
        storage_path: Path | None = None,
    ) -> AgentSuggestion | None:
        """Suggest which agent should respond to the message."""
        try:
            # Import here to avoid circular dependencies
            import json
            from pathlib import Path

            from .ai import ai_response

            # Create prompt that asks for JSON response
            base_prompt = self.create_routing_prompt(message, available_agents, thread_context)
            json_prompt = (
                base_prompt
                + """

Please respond with a JSON object containing your routing decision:
{
  "agent_name": "name_of_suggested_agent",
  "reasoning": "brief explanation of why this agent was chosen",
  "confidence": 0.85
}

Only return the JSON object, no other text."""
            )

            # Use the existing agent system
            if not storage_path:
                storage_path = Path.cwd() / "tmp"
                storage_path.mkdir(exist_ok=True)

            session_id = f"router_{hash(message) % 10000}"
            response_text = await ai_response(
                agent_name="router",
                prompt=json_prompt,
                session_id=session_id,
                storage_path=storage_path,
                thread_history=thread_context,
            )

            # Parse JSON response
            response_text = response_text.strip()
            if response_text.startswith("```json"):
                response_text = response_text.replace("```json", "").replace("```", "").strip()
            elif response_text.startswith("```"):
                response_text = response_text.replace("```", "").strip()

            # Try to extract JSON from the response
            try:
                suggestion_data = json.loads(response_text)
            except json.JSONDecodeError:
                # Try to find JSON in the response
                import re

                json_match = re.search(r"\{[^}]+\}", response_text)
                if json_match:
                    suggestion_data = json.loads(json_match.group())
                else:
                    logger.error(f"Could not parse JSON from response: {response_text}")
                    return None

            # Validate the agent is in available list
            if suggestion_data.get("agent_name") not in available_agents:
                logger.warning(f"Router suggested unavailable agent: {suggestion_data.get('agent_name')}")
                # Fall back to first available agent
                suggestion_data["agent_name"] = available_agents[0]
                suggestion_data["reasoning"] = (
                    f"Fallback to {available_agents[0]} (original suggestion was unavailable)"
                )
                suggestion_data["confidence"] = 0.5

            suggestion = AgentSuggestion(**suggestion_data)

            logger.info(f"Router suggested agent: {suggestion.agent_name} (confidence: {suggestion.confidence:.2f})")

            return suggestion

        except Exception as e:
            logger.error(f"Router agent error: {e}")
            return None


def should_router_handle(mentioned_agents: list[str], agents_in_thread: list[str], is_thread: bool) -> bool:
    """Determine if router should handle this message.

    Args:
        mentioned_agents: List of agents mentioned in the message
        agents_in_thread: List of agents that have participated in thread
        is_thread: Whether this is a thread message

    Returns:
        True if router should handle, False otherwise
    """
    # If agents are mentioned, they handle it
    if mentioned_agents:
        return False

    # Not in a thread, no routing needed
    if not is_thread:
        return False

    # Multiple agents in thread but none mentioned -> router decides
    # Single agent or no agents in thread -> no routing needed
    return len(agents_in_thread) > 1
