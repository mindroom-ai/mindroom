"""Team-based collaboration for multiple agents."""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, NamedTuple

from agno.agent import Agent
from agno.run.team import TeamRunResponse
from agno.team import Team

from .agent_config import ROUTER_AGENT_NAME
from .ai import get_model_instance
from .logging_config import get_logger

if TYPE_CHECKING:
    from .bot import MultiAgentOrchestrator


logger = get_logger(__name__)


class TeamMode(str, Enum):
    """Team collaboration modes."""

    COORDINATE = "coordinate"  # Sequential, building on each other
    COLLABORATE = "collaborate"  # Parallel, synthesized


class ShouldFormTeamResult(NamedTuple):
    """Result of should_form_team."""

    should_form_team: bool
    agents: list[str]
    mode: TeamMode


def should_form_team(
    tagged_agents: list[str],
    agents_in_thread: list[str],
) -> ShouldFormTeamResult:
    """Determine if a team should form and with which mode."""
    # Case 1: Multiple agents explicitly tagged
    if len(tagged_agents) > 1:
        logger.info(f"Team formation needed for tagged agents: {tagged_agents}")
        return ShouldFormTeamResult(
            should_form_team=True,
            agents=tagged_agents,
            mode=TeamMode.COORDINATE,
        )

    # Case 2: No agents tagged but multiple in thread
    if len(tagged_agents) == 0 and len(agents_in_thread) > 1:
        logger.info(f"Team formation needed for thread agents: {agents_in_thread}")
        return ShouldFormTeamResult(
            should_form_team=True,
            agents=agents_in_thread,
            mode=TeamMode.COLLABORATE,
        )

    return ShouldFormTeamResult(
        should_form_team=False,
        agents=[],
        mode=TeamMode.COLLABORATE,
    )


async def create_team_response(
    agent_names: list[str],
    mode: TeamMode,
    message: str,
    orchestrator: MultiAgentOrchestrator,
    thread_history: list[dict] | None = None,
) -> str:
    """Create a team and execute response."""

    # Get existing agent instances from the orchestrator
    agents: list[Agent] = []
    for name in agent_names:
        if name == ROUTER_AGENT_NAME:
            continue

        # Use the existing agent instance from the bot
        agent_bot = orchestrator.agent_bots[name]
        agents.append(agent_bot.agent)

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
        members=agents,  # type: ignore[arg-type]
        mode=mode.value,
        name=f"Team-{'-'.join(agent_names)}",
        model=get_model_instance("default"),
    )

    logger.info(f"Executing team response with {len(agents)} agents")
    response = await team.arun(prompt)

    # Extract response content
    if isinstance(response, TeamRunResponse):
        team_response = str(response.content)
    else:
        logger.warning(f"Unexpected response type: {type(response)}", response=response)
        team_response = str(response)

    # Prepend team information to the response
    agent_mentions = " ".join([f"@{name}" for name in agent_names if name != ROUTER_AGENT_NAME])
    team_header = f"🤝 **Team Response** ({agent_mentions}):\n\n"

    return team_header + team_response
