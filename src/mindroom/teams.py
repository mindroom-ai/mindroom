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


logger = get_logger(__name__)


class TeamMode(str, Enum):
    """Team collaboration modes."""

    COORDINATE = "coordinate"  # Sequential, building on each other
    COLLABORATE = "collaborate"  # Parallel, synthesized


def should_form_team(
    tagged_agents: list[str],
    agents_in_thread: list[str],
) -> tuple[bool, list[str], TeamMode]:
    """Determine if a team should form and with which mode."""
    # Case 1: Multiple agents explicitly tagged
    if len(tagged_agents) > 1:
        logger.info(f"Forming explicit team with tagged agents: {tagged_agents}")
        return True, tagged_agents, TeamMode.COORDINATE

    # Case 2: No agents tagged but multiple in thread
    if len(tagged_agents) == 0 and len(agents_in_thread) > 1:
        logger.info(f"Forming implicit team with thread agents: {agents_in_thread}")
        return True, agents_in_thread, TeamMode.COLLABORATE

    return False, [], TeamMode.COLLABORATE


async def create_team_response(
    agent_names: list[str],
    mode: TeamMode,
    message: str,
    orchestrator: Any,
    storage_path: Path,
    thread_history: list[dict] | None = None,
) -> str:
    """Create a team and execute response."""
    # Handle case where orchestrator is None (in tests)
    if not orchestrator or not hasattr(orchestrator, "agent_bots"):
        return "Team collaboration not available (no orchestrator)"

    # Create agents for the team
    agents = []
    for name in agent_names:
        if name == ROUTER_AGENT_NAME:
            continue

        if name not in orchestrator.agent_bots:
            logger.warning(f"Agent '{name}' not found, skipping")
            continue

        model = get_model_instance("default")
        agent = create_agent(
            agent_name=name,
            model=model,
            storage_path=storage_path / "teams",
        )
        agents.append(agent)

    if not agents:
        return "Sorry, no agents available for team collaboration."

    # Build prompt with context
    prompt = message
    if thread_history:
        recent_messages = thread_history[-3:]  # Last 3 messages for context
        context_parts = []
        for msg in recent_messages:
            sender = msg.get("sender", "Unknown")
            body = msg.get("content", {}).get("body", "")
            if body and len(body) < 200:
                context_parts.append(f"{sender}: {body}")

        if context_parts:
            prompt = f"Context:\n{chr(10).join(context_parts)}\n\nUser: {message}"

    # Create and run team
    team = Team(
        members=agents,
        mode=mode.value,
        name=f"Team-{'-'.join(agent_names)}",
        model=get_model_instance("default"),
    )

    logger.info(f"Executing team response with {len(agents)} agents")
    response = await team.arun(prompt)

    # Extract response content
    if hasattr(response, "content") and response.content:
        return str(response.content)
    return str(response)
