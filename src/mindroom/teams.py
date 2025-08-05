"""Team-based collaboration for multiple agents."""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Any

from agno.team import Team

from .agent_config import ROUTER_AGENT_NAME, create_agent
from .ai import get_model_instance
from .logging_config import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from agno.agent import Agent


logger = get_logger(__name__)


class TeamMode(str, Enum):
    """Team collaboration modes."""

    COORDINATE = "coordinate"  # Sequential, building on each other
    COLLABORATE = "collaborate"  # Parallel, synthesized
    ROUTE = "route"  # Delegated by lead agent


class TeamManager:
    """Manages team formation and coordination for multi-agent collaboration."""

    def __init__(self, orchestrator: Any, storage_path: Path) -> None:
        """Initialize the team manager.

        Args:
            orchestrator: The orchestrator managing all agents
            storage_path: Base directory for storing team data
        """
        self.orchestrator = orchestrator
        self.storage_path = storage_path
        self.active_teams: dict[str, Team] = {}  # thread_id -> Team

    def should_form_team(
        self,
        tagged_agents: list[str],
        agents_in_thread: list[str],
        message: str,
        thread_id: str | None = None,
    ) -> tuple[bool, list[str], TeamMode]:
        """Determine if a team should form and with which mode.

        Args:
            tagged_agents: List of explicitly mentioned agent names
            agents_in_thread: List of agents already participating in thread
            message: The user's message
            thread_id: Optional thread ID for tracking

        Returns:
            Tuple of (should_form_team, agent_names, team_mode)
        """
        # Case 1: Multiple agents explicitly tagged
        if len(tagged_agents) > 1:
            logger.info(f"Forming explicit team with tagged agents: {tagged_agents}")
            return True, tagged_agents, TeamMode.COORDINATE

        # Case 2: No agents tagged but multiple in thread
        if len(tagged_agents) == 0 and len(agents_in_thread) > 1:
            logger.info(f"Forming implicit team with thread agents: {agents_in_thread}")
            return True, agents_in_thread, TeamMode.COLLABORATE

        # Case 3: Router decides complex query needs team
        # This would be handled by the router agent itself
        # For now, we don't auto-form teams from router

        return False, [], TeamMode.COLLABORATE

    async def create_team(
        self,
        agent_names: list[str],
        mode: TeamMode,
        thread_id: str | None = None,
    ) -> Team:
        """Create an Agno Team instance with the specified agents.

        Args:
            agent_names: List of agent names to include in team
            mode: The collaboration mode
            thread_id: Optional thread ID for tracking

        Returns:
            Configured Team instance
        """
        # Get agent instances from orchestrator
        agents: list[Agent | Team] = []
        for name in agent_names:
            # Skip router agent - it doesn't participate in teams
            if name == ROUTER_AGENT_NAME:
                continue

            # Get the bot instance
            bot = self.orchestrator.agent_bots.get(name)
            if not bot:
                logger.warning(f"Agent '{name}' not found in orchestrator, skipping")
                continue

            # Create an Agno Agent for the team
            # We need to create fresh agent instances for the team
            # Use the default model configuration (all agents use the same model)
            model = get_model_instance("default")
            agent = create_agent(
                agent_name=name,
                model=model,
                storage_path=self.storage_path / "teams" / (thread_id or "default"),
            )
            agents.append(agent)

        if not agents:
            raise ValueError("No valid agents found for team creation")

        # Create team with appropriate configuration based on mode
        team_name = f"Team-{'-'.join(agent_names)}"
        if mode == TeamMode.COORDINATE:
            # For coordinate mode, agents work sequentially
            instructions = [
                "Work together sequentially to solve the user's request.",
                "Each agent should build upon the previous agent's work.",
                "The final response should synthesize all contributions.",
            ]
        elif mode == TeamMode.COLLABORATE:
            # For collaborate mode, agents work in parallel
            instructions = [
                "Work together to provide different perspectives on the user's request.",
                "Each agent should contribute their unique expertise.",
                "The final response should combine all perspectives into a unified answer.",
            ]
        else:  # ROUTE mode
            # For route mode, lead agent delegates
            instructions = [
                "The first agent should analyze the request and delegate tasks.",
                "Other agents should handle their assigned portions.",
                "The final response should integrate all delegated work.",
            ]

        # Create the team
        # Use the default model configuration for the team coordinator
        team_model = get_model_instance("default")
        team = Team(
            members=agents,
            mode=mode.value,
            name=team_name,
            instructions=instructions,
            model=team_model,  # Specify model to avoid OpenAI default
        )

        # Track active team
        if thread_id:
            self.active_teams[thread_id] = team

        logger.info(f"Created team '{team_name}' with {len(agents)} agents in {mode.value} mode")
        return team

    async def execute_team_response(
        self,
        team: Team,
        message: str,
        context: dict[str, Any],
        thread_id: str | None = None,
    ) -> str:
        """Execute team response and return synthesized result.

        Args:
            team: The Team instance
            message: User's message
            context: Additional context (thread history, etc.)
            thread_id: Optional thread ID

        Returns:
            Synthesized team response
        """
        try:
            # Build context-aware prompt if we have thread history
            prompt = message
            if "thread_history" in context:
                # Add recent context
                history_context = self._build_context_from_history(context["thread_history"])
                if history_context:
                    prompt = f"Context from conversation:\n{history_context}\n\nUser: {message}"

            # Execute team response
            logger.info(f"Executing team response for: {message[:100]}...")
            response = await team.arun(prompt)

            # Debug log the response type and content
            logger.debug(f"Team response type: {type(response)}")
            logger.debug(f"Team response attributes: {dir(response) if hasattr(response, '__dict__') else 'N/A'}")
            if hasattr(response, "content"):
                logger.debug(f"Team response content: {response.content}")
            logger.debug(f"Team response str: {str(response)[:200]}")

            # Extract the text content from the response
            if hasattr(response, "content") and response.content:
                # TeamRunResponse has content as a string
                if isinstance(response.content, str):
                    result = response.content
                else:
                    # Handle other response types where content might be iterable
                    content_parts = []
                    try:
                        for content_item in response.content:
                            if hasattr(content_item, "text"):
                                content_parts.append(content_item.text)
                            else:
                                content_parts.append(str(content_item))
                        result = "\n\n".join(content_parts)
                    except TypeError:
                        # If content is not iterable, just convert to string
                        result = str(response.content)
            else:
                # Fallback for other response types
                result = str(response)

            logger.info(f"Team response completed, length: {len(result)}")
            return result

        except Exception as e:
            logger.error(f"Error executing team response: {e}", exc_info=True)
            # Return a fallback response
            return "I apologize, but the team encountered an error while processing your request. Please try again."

        finally:
            # Clean up active team tracking
            if thread_id and thread_id in self.active_teams:
                del self.active_teams[thread_id]

    def is_team_active(self, thread_id: str) -> bool:
        """Check if a team is currently active for a thread.

        Args:
            thread_id: The thread ID to check

        Returns:
            True if a team is active for this thread
        """
        return thread_id in self.active_teams

    def get_team_members(self, thread_id: str) -> list[str]:
        """Get the members of an active team.

        Args:
            thread_id: The thread ID

        Returns:
            List of agent names in the team, or empty list if no team
        """
        team = self.active_teams.get(thread_id)
        if not team:
            return []

        return [agent.name for agent in team.members if hasattr(agent, "name") and agent.name]

    def _build_context_from_history(self, thread_history: list[dict]) -> str:
        """Build context string from thread history.

        Args:
            thread_history: List of thread messages

        Returns:
            Formatted context string
        """
        if not thread_history:
            return ""

        # Take last few messages for context
        recent_messages = thread_history[-5:]  # Last 5 messages
        context_parts = []

        for msg in recent_messages:
            sender = msg.get("sender", "Unknown")
            body = msg.get("content", {}).get("body", "")
            if body:
                # Truncate long messages
                if len(body) > 200:
                    body = body[:200] + "..."
                context_parts.append(f"{sender}: {body}")

        return "\n".join(context_parts)


def is_part_of_team_response(
    agent_name: str,
    thread_id: str | None,
    team_manager: TeamManager | None,
) -> bool:
    """Check if an agent is part of an active team response.

    Args:
        agent_name: The agent to check
        thread_id: The thread ID
        team_manager: The team manager instance

    Returns:
        True if the agent is part of an active team
    """
    if not thread_id or not team_manager:
        return False

    if not team_manager.is_team_active(thread_id):
        return False

    return agent_name in team_manager.get_team_members(thread_id)
