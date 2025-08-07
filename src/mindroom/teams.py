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
    agent_list = ", ".join([name for name in agent_names if name != ROUTER_AGENT_NAME])

    logger.info(f"Executing team response with {len(agents)} agents in {mode.value} mode")
    logger.info(f"TEAM PROMPT: {prompt[:500]}")  # Log first 500 chars of prompt

    response = await team.arun(prompt)

    # Extract response content
    if isinstance(response, TeamRunResponse):
        # Build comprehensive team response
        parts = []

        # Include member responses if available (when show_members_responses=True)
        if hasattr(response, "member_responses") and response.member_responses:
            logger.debug(f"Team had {len(response.member_responses)} member responses")

            # Add individual member contributions
            for member_resp in response.member_responses:
                # Extract member name from the response if available
                member_name = getattr(member_resp, "member_id", "Unknown Agent")
                member_content = ""

                # Handle both TeamRunResponse and RunResponse types
                if hasattr(member_resp, "content"):
                    member_content = str(member_resp.content or "")
                elif hasattr(member_resp, "messages"):
                    # Extract assistant messages from the member
                    messages = getattr(member_resp, "messages", [])
                    if messages:
                        for msg in messages:
                            if hasattr(msg, "role") and msg.role == "assistant" and hasattr(msg, "content"):
                                member_content += str(msg.content or "")

                if member_content:
                    parts.append(f"**{member_name}**: {member_content}")

        # Add the final aggregated response
        if response.content:
            if parts:  # If we have member responses, add a separator
                parts.append("\n**Team Consensus**:")
            parts.append(str(response.content))

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
