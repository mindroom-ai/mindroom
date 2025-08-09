"""Team-based collaboration for multiple agents."""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Any, NamedTuple

from agno.agent import Agent
from agno.models.message import Message
from agno.run.response import RunResponse
from agno.run.team import TeamRunResponse
from agno.team import Team

from .agent_config import ROUTER_AGENT_NAME, load_config
from .ai import get_model_instance
from .logging_config import get_logger
from .matrix import get_room_alias_from_id

if TYPE_CHECKING:
    from .bot import MultiAgentOrchestrator


logger = get_logger(__name__)


class TeamMode(str, Enum):
    """Team collaboration modes."""

    COORDINATE = "coordinate"  # Sequential, building on each other
    COLLABORATE = "collaborate"  # Parallel, synthesized


def extract_team_member_contributions(response: TeamRunResponse | RunResponse) -> list[str]:
    """Extract and format member contributions from a team response.

    Handles nested teams recursively with proper indentation.

    Args:
        response: The team or agent response to extract contributions from

    Returns:
        List of formatted contribution strings
    """
    return _extract_contributions_recursive(response, indent=0, include_consensus=True)


def _extract_contributions_recursive(
    response: TeamRunResponse | RunResponse,
    indent: int,
    include_consensus: bool,
) -> list[str]:
    """Internal recursive function for extracting contributions.

    Args:
        response: The response to extract from
        indent: Current indentation level
        include_consensus: Whether to include team consensus

    Returns:
        List of formatted contribution strings
    """
    parts = []
    indent_str = "  " * indent

    if isinstance(response, TeamRunResponse):
        # Extract member contributions
        if response.member_responses:
            for member_resp in response.member_responses:
                if isinstance(member_resp, TeamRunResponse):
                    # Nested team
                    team_name = member_resp.team_name or "Nested Team"
                    parts.append(f"{indent_str}**{team_name}** (Team):")
                    nested_parts = _extract_contributions_recursive(
                        member_resp,
                        indent=indent + 1,
                        include_consensus=False,  # No consensus for nested teams
                    )
                    parts.extend(nested_parts)
                elif isinstance(member_resp, RunResponse):
                    # Regular agent
                    agent_name = member_resp.agent_name or "Team Member"
                    content = _extract_content(member_resp)
                    if content:
                        parts.append(f"{indent_str}**{agent_name}**: {content}")

        # Add team consensus if requested
        if include_consensus:
            if response.content:
                if parts:  # Separator only if we have member contributions
                    parts.append(f"\n{indent_str}**Team Consensus**:")
                parts.append(f"{indent_str}{response.content}")
            elif parts:
                # If no consensus but we have member responses, note that
                parts.append(f"\n{indent_str}*No team consensus - showing individual responses only*")

    elif isinstance(response, RunResponse):
        # Single agent response
        agent_name = response.agent_name or "Agent"
        content = _extract_content(response)
        if content:
            parts.append(f"{indent_str}**{agent_name}**: {content}")

    return parts


def _extract_content(response: TeamRunResponse | RunResponse) -> str:
    """Extract content from a response object.

    Args:
        response: The response to extract content from

    Returns:
        The extracted content as a string
    """
    # Direct content takes priority
    if response.content:
        return str(response.content)

    # Fall back to extracting from messages
    # Note: This concatenates ALL assistant messages, which might include
    # multiple turns in a conversation. Consider if you want just the
    # last message or all of them.
    if response.messages:
        content_parts = []
        messages_list: list[Any] = response.messages
        for msg in messages_list:
            if isinstance(msg, Message) and msg.role == "assistant" and msg.content:
                content_parts.append(str(msg.content))

        # Join with newlines to preserve message boundaries
        return "\n\n".join(content_parts) if content_parts else ""

    return ""


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

    # Find room alias from room ID
    room_alias = get_room_alias_from_id(room_id)

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
        if agent_bot.agent is not None:
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
        # Enable features for better team collaboration visibility
        show_members_responses=True,  # Show individual member responses
        enable_agentic_context=True,  # Share context between team members
        debug_mode=False,  # Set to True for debugging
        # Agno will automatically list members with their names, roles, and tools
        # No need for custom descriptions or instructions
    )

    # Create agent list for logging
    agent_list = ", ".join(str(a.name) for a in agents if a.name)

    logger.info(f"Executing team response with {len(agents)} agents in {mode.value} mode")
    logger.info(f"TEAM PROMPT: {prompt[:500]}")  # Log first 500 chars of prompt

    response = await team.arun(prompt)

    # Extract response content using our universal extraction function
    if isinstance(response, TeamRunResponse):
        # Log member responses for debugging
        if response.member_responses:
            logger.debug(f"Team had {len(response.member_responses)} member responses")

        # Log the team consensus content for debugging
        logger.info(f"Team consensus content: {response.content[:200] if response.content else 'None'}")

        # Extract all contributions (including nested teams if any)
        parts = extract_team_member_contributions(response)

        # Combine all parts
        team_response = "\n\n".join(parts) if parts else "No team response generated."
    else:
        logger.warning(f"Unexpected response type: {type(response)}", response=response)
        team_response = str(response)

    # Log the team response
    logger.info(f"TEAM RESPONSE ({agent_list}): {team_response[:500]}")
    if len(team_response) > 500:
        logger.debug(f"TEAM RESPONSE (full): {team_response}")

    # Prepend team information to the response
    # Don't use @ mentions as that would trigger the agents again
    team_header = f"🤝 **Team Response** ({agent_list}):\n\n"

    return team_header + team_response
