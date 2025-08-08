"""Team-based collaboration for multiple agents."""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Any, NamedTuple

from agno.agent import Agent
from agno.models.message import Message
from agno.run.response import RunResponse
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


def extract_team_member_contributions(
    response: TeamRunResponse | RunResponse,
    include_consensus: bool = True,
    indent_level: int = 0,
) -> list[str]:
    """Extract member contributions from a team response, handling nested teams recursively.

    Args:
        response: The team or agent response to extract contributions from
        include_consensus: Whether to include the final team consensus
        indent_level: Current indentation level for nested teams

    Returns:
        List of formatted contribution strings
    """
    parts = []
    indent = "  " * indent_level  # Indentation for nested teams

    # Handle TeamRunResponse
    if isinstance(response, TeamRunResponse):
        # Process member responses if available
        if response.member_responses:
            for member_resp in response.member_responses:
                # Recursively handle nested teams
                if isinstance(member_resp, TeamRunResponse):
                    # This is a nested team
                    team_name = getattr(member_resp, "team_name", "Nested Team")
                    parts.append(f"{indent}**{team_name}** (Team):")
                    # Recursively extract from nested team
                    nested_parts = extract_team_member_contributions(
                        member_resp,
                        include_consensus=False,  # Don't include consensus for nested teams
                        indent_level=indent_level + 1,
                    )
                    parts.extend(nested_parts)
                elif isinstance(member_resp, RunResponse):
                    # Regular agent response
                    agent_name = member_resp.agent_name if member_resp.agent_name else "Team Member"
                    content = extract_content_from_response(member_resp)
                    if content:
                        parts.append(f"{indent}**{agent_name}**: {content}")

        # Add the final consensus if requested
        if include_consensus and response.content:
            if parts:  # Only add separator if we have member contributions
                parts.append(f"\n{indent}**Team Consensus**:")
            parts.append(f"{indent}{response.content}")

    # Handle RunResponse (single agent)
    elif isinstance(response, RunResponse):
        agent_name = response.agent_name if response.agent_name else "Agent"
        content = extract_content_from_response(response)
        if content:
            parts.append(f"{indent}**{agent_name}**: {content}")

    return parts


def extract_content_from_response(response: TeamRunResponse | RunResponse) -> str:
    """Extract content from a response object.

    Args:
        response: The response to extract content from

    Returns:
        The extracted content as a string
    """
    content = ""

    # Try to get content directly
    if response.content:
        content = str(response.content)
    # Fall back to extracting from messages
    elif response.messages:
        messages_list: list[Any] = response.messages
        for msg in messages_list:
            if isinstance(msg, Message) and msg.role == "assistant" and msg.content:
                content += str(msg.content)

    return content


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

    # Let Agno Team handle everything - it already knows how to describe members
    team = Team(
        members=agents,  # type: ignore[arg-type]
        mode=mode.value,
        name=f"Team-{'-'.join(agent_names)}",
        model=get_model_instance("default"),
        # Enable features for better team collaboration visibility
        show_members_responses=True,  # Show individual member responses
        enable_agentic_context=True,  # Share context between team members
        debug_mode=False,  # Set to True for debugging
        # Agno will automatically list members with their names, roles, and tools
        # No need for custom descriptions or instructions
    )

    # Create agent list for logging
    agent_list = ", ".join(a.name for a in agents if a.name)

    logger.info(f"Executing team response with {len(agents)} agents in {mode.value} mode")
    logger.info(f"TEAM PROMPT: {prompt[:500]}")  # Log first 500 chars of prompt

    response = await team.arun(prompt)

    # Extract response content using our universal extraction function
    if isinstance(response, TeamRunResponse):
        # Log member responses for debugging
        if response.member_responses:
            logger.debug(f"Team had {len(response.member_responses)} member responses")

        # Extract all contributions (including nested teams if any)
        parts = extract_team_member_contributions(response, include_consensus=True)

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
