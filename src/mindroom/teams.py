"""Team-based collaboration for multiple agents."""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, NamedTuple

from agno.agent import Agent
from agno.run.team import TeamRunResponse
from agno.team import Team

from .agent_config import ROUTER_AGENT_NAME, load_config
from .ai import get_model_instance
from .logging_config import get_logger
from .matrix import get_room_aliases

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
    all_mentioned_in_thread: list[str],
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

    # Case 2: No agents tagged but multiple were mentioned before in thread
    if not tagged_agents and len(all_mentioned_in_thread) > 1:
        logger.info(f"Team formation needed for previously mentioned agents: {all_mentioned_in_thread}")
        return ShouldFormTeamResult(
            should_form_team=True,
            agents=all_mentioned_in_thread,
            mode=TeamMode.COLLABORATE,
        )

    # Case 3: No agents tagged but multiple in thread
    if not tagged_agents and len(agents_in_thread) > 1:
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


def get_team_model(team_name: str, room_id: str) -> str:
    """Get the appropriate model for a team in a specific room.

    Priority:
    1. Room-specific model from room_models
    2. Team's configured model
    3. Global default model

    Args:
        team_name: Name of the team
        room_id: Matrix room ID

    Returns:
        Model name to use
    """
    config = load_config()
    room_aliases = get_room_aliases()

    # Find room alias from room ID
    room_alias = None
    for alias, rid in room_aliases.items():
        if rid == room_id:
            room_alias = alias
            break

    # Check room-specific model first
    if room_alias and room_alias in config.room_models:
        model = config.room_models[room_alias]
        logger.info(f"Using room-specific model for {team_name} in {room_alias}: {model}")
        return model

    # Check team's configured model
    if team_name in config.teams:
        team_config = config.teams[team_name]
        if team_config.model:
            logger.info(f"Using team-specific model for {team_name}: {team_config.model}")
            return team_config.model

    # Fall back to default
    logger.info(f"Using default model for {team_name}")
    return "default"


async def create_team_response(
    agent_names: list[str],
    mode: TeamMode,
    message: str,
    orchestrator: MultiAgentOrchestrator,
    thread_history: list[dict] | None = None,
    model_name: str | None = None,
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

    # Build the user message with thread context if available
    prompt = message
    if thread_history:
        recent_messages = thread_history[-30:]  # Last 30 messages for context
        context_parts = []
        for msg in recent_messages:
            sender = msg.get("sender", "Unknown")
            body = msg.get("content", {}).get("body", "")
            if body and len(body) < 200:
                context_parts.append(f"{sender}: {body}")

        if context_parts:
            context = "\n".join(context_parts)
            prompt = f"Thread Context:\n{context}\n\nUser: {message}"

    # Use provided model or default
    model = get_model_instance(model_name or "default")

    # Let Agno Team handle everything - it already knows how to describe members
    team = Team(
        members=agents,  # type: ignore[arg-type]
        mode=mode.value,
        name=f"Team-{'-'.join(agent_names)}",
        model=model,
        # Agno will automatically list members with their names, roles, and tools
        # No need for custom descriptions or instructions
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
    # Don't use @ mentions as that would trigger the agents again
    agent_list = ", ".join([name for name in agent_names if name != ROUTER_AGENT_NAME])
    team_header = f"🤝 **Team Response** ({agent_list}):\n\n"

    return team_header + team_response
