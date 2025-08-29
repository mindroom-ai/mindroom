"""Team-based collaboration for multiple agents."""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

from agno.agent import Agent
from agno.models.message import Message
from agno.run.response import RunResponse
from agno.run.team import TeamRunResponse
from agno.team import Team
from pydantic import BaseModel, Field

from .ai import get_model_instance
from .constants import ROUTER_AGENT_NAME
from .logging_config import get_logger
from .matrix.rooms import get_room_alias_from_id
from .thread_utils import get_available_agents_in_room

if TYPE_CHECKING:
    import nio

    from .bot import MultiAgentOrchestrator
    from .config import Config
    from .matrix.identity import MatrixID


logger = get_logger(__name__)

# Message length limits for team context and logging
MAX_CONTEXT_MESSAGE_LENGTH = 200  # Maximum length for messages to include in thread context
MAX_LOG_MESSAGE_LENGTH = 500  # Maximum length for messages in team response logs


class TeamMode(str, Enum):
    """Team collaboration modes."""

    COORDINATE = "coordinate"  # Leader delegates and synthesizes (can be sequential OR parallel)
    COLLABORATE = "collaborate"  # All members work on same task in parallel


class TeamModeDecision(BaseModel):
    """AI decision for team collaboration mode."""

    mode: Literal["coordinate", "collaborate"] = Field(
        description="coordinate for delegation and synthesis, collaborate for all working on same task",
    )
    reasoning: str = Field(description="Brief explanation of why this mode was chosen")


def extract_team_member_contributions(response: TeamRunResponse | RunResponse) -> list[str]:
    """Extract and format member contributions from a team response.

    Handles nested teams recursively with proper indentation.

    Args:
        response: The team or agent response to extract contributions from

    Returns:
        List of formatted contribution strings

    """
    return _extract_contributions_recursive(response, indent=0, include_consensus=True)


def _extract_contributions_recursive(  # noqa: C901
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
        messages_list: list[Any] = response.messages
        content_parts = [
            str(msg.content)
            for msg in messages_list
            if isinstance(msg, Message) and msg.role == "assistant" and msg.content
        ]

        # Join with newlines to preserve message boundaries
        return "\n\n".join(content_parts) if content_parts else ""

    return ""


class ShouldFormTeamResult(NamedTuple):
    """Result of should_form_team."""

    should_form_team: bool
    agents: list[MatrixID]
    mode: TeamMode


async def determine_team_mode(
    message: str,
    agent_names: list[str],
    config: Config,
) -> TeamMode:
    """Use AI to determine optimal team collaboration mode.

    Args:
        message: The user's message/task
        agent_names: List of agents that will form the team
        config: Application configuration for model access

    Returns:
        TeamMode.COORDINATE or TeamMode.COLLABORATE

    """
    prompt = f"""Determine the best team collaboration mode for this task.

Task: {message}
Agents: {", ".join(agent_names)}

Team Modes (from Agno documentation):
- "coordinate": Team leader delegates tasks to members and synthesizes their outputs.
               The leader decides whether to send tasks sequentially or in parallel based on what's appropriate.
- "collaborate": All team members are given the SAME task and work on it simultaneously.
                The leader synthesizes all their outputs into a cohesive response.

Decision Guidelines:
- Use "coordinate" when agents need to do DIFFERENT subtasks (whether sequential or parallel)
- Use "collaborate" when you want ALL agents working on the SAME problem for diverse perspectives

Examples:
- "Email me then call me" → coordinate (different tasks: email agent sends email, phone agent makes call)
- "Get weather and news" → coordinate (different tasks: weather agent gets weather, news agent gets news)
- "Research this topic and analyze the data" → coordinate (different subtasks for each agent)
- "What do you think about X?" → collaborate (all agents provide their perspective on the same question)
- "Brainstorm solutions" → collaborate (all agents work on the same brainstorming task)

Return the mode and a one-sentence reason why."""

    model = get_model_instance(config, "default")
    agent = Agent(
        name="TeamModeDecider",
        role="Determine team mode",
        model=model,
        response_model=TeamModeDecision,
    )

    try:
        response = await agent.arun(prompt, session_id="team_mode_decision")
        decision = response.content
        if isinstance(decision, TeamModeDecision):
            logger.info(f"Team mode: {decision.mode} - {decision.reasoning}")
            return TeamMode.COORDINATE if decision.mode == "coordinate" else TeamMode.COLLABORATE
        # Fallback if response is unexpected
        logger.warning("Unexpected response type from AI, defaulting to collaborate")
        return TeamMode.COLLABORATE  # noqa: TRY300
    except Exception as e:
        logger.warning(f"AI decision failed: {e}, defaulting to collaborate")
        return TeamMode.COLLABORATE


async def should_form_team(
    tagged_agents: list[MatrixID],
    agents_in_thread: list[MatrixID],
    all_mentioned_in_thread: list[MatrixID],
    room: nio.MatrixRoom,
    message: str | None = None,
    config: Config | None = None,
    use_ai_decision: bool = True,
    is_dm_room: bool = False,
    is_thread: bool = False,
) -> ShouldFormTeamResult:
    """Determine if a team should form and with which mode.

    Args:
        tagged_agents: Agents explicitly mentioned in the current message
        agents_in_thread: Agents that have participated in the thread
        all_mentioned_in_thread: All agents ever mentioned in the thread
        room: The Matrix room object (for checking available agents)
        message: The user's message (for AI decision context)
        config: Application configuration (for AI model access)
        use_ai_decision: Whether to use AI for mode selection
        is_dm_room: Whether this is a DM room
        is_thread: Whether the current message is in a thread

    Returns:
        ShouldFormTeamResult with team formation decision

    """
    # Determine which agents will form the team
    team_agents: list[MatrixID] = []

    # Case 1: Multiple agents explicitly tagged
    if len(tagged_agents) > 1:
        logger.info(f"Team formation needed for tagged agents: {tagged_agents}")
        team_agents = tagged_agents

    # Case 2: No agents tagged but multiple were mentioned before in thread
    elif not tagged_agents and len(all_mentioned_in_thread) > 1:
        logger.info(f"Team formation needed for previously mentioned agents: {all_mentioned_in_thread}")
        team_agents = all_mentioned_in_thread

    # Case 3: No agents tagged but multiple in thread
    elif not tagged_agents and len(agents_in_thread) > 1:
        logger.info(f"Team formation needed for thread agents: {agents_in_thread}")
        team_agents = agents_in_thread

    # Case 4: DM room with multiple agents and no mentions (main timeline only)
    # We avoid forming a team inside an existing thread to preserve
    # single-agent ownership unless the thread itself involves multiple agents
    elif is_dm_room and not is_thread and not tagged_agents and room and config:
        available_agents = get_available_agents_in_room(room, config)
        if len(available_agents) > 1:
            logger.info(f"Team formation needed for DM room with multiple agents: {available_agents}")
            team_agents = available_agents

    # No team needed
    if not team_agents:
        return ShouldFormTeamResult(
            should_form_team=False,
            agents=[],
            mode=TeamMode.COLLABORATE,
        )

    # Determine the mode - use AI if enabled and we have the necessary context
    if use_ai_decision and message and config:
        # Convert MatrixID to agent names for the AI prompt
        agent_names = [mid.agent_name(config) or mid.username for mid in team_agents]
        mode = await determine_team_mode(message, agent_names, config)
    else:
        # Fallback to hardcoded logic when AI decision is disabled or unavailable
        # Use COORDINATE when agents are explicitly tagged (they likely have different roles)
        # Use COLLABORATE when agents are from thread history (likely discussing same topic)
        mode = TeamMode.COORDINATE if len(tagged_agents) > 1 else TeamMode.COLLABORATE
        logger.info(f"Using hardcoded mode selection: {mode.value}")

    return ShouldFormTeamResult(
        should_form_team=True,
        agents=team_agents,
        mode=mode,
    )


def get_team_model(team_name: str, room_id: str, config: Config) -> str:
    """Get the appropriate model for a team in a specific room.

    Priority:
    1. Room-specific model from room_models
    2. Team's configured model
    3. Global default model

    Args:
        team_name: Name of the team
        room_id: Matrix room ID
        config: Application configuration

    Returns:
        Model name to use

    """
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


async def create_team_response(  # noqa: C901, PLR0912
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

        # Check if agent exists in orchestrator
        if name not in orchestrator.agent_bots:
            logger.warning(f"Agent '{name}' not found in orchestrator - may not be in room")
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
            if body and len(body) < MAX_CONTEXT_MESSAGE_LENGTH:
                context_parts.append(f"{sender}: {body}")

        if context_parts:
            context = "\n".join(context_parts)
            prompt = f"Thread Context:\n{context}\n\nUser: {message}"

    # Use provided model or default
    assert orchestrator.config is not None
    model = get_model_instance(orchestrator.config, model_name or "default")

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
    logger.info(f"TEAM RESPONSE ({agent_list}): {team_response[:MAX_LOG_MESSAGE_LENGTH]}")
    if len(team_response) > MAX_LOG_MESSAGE_LENGTH:
        logger.debug(f"TEAM RESPONSE (full): {team_response}")

    # Prepend team information to the response
    # Don't use @ mentions as that would trigger the agents again
    team_header = f"🤝 **Team Response** ({agent_list}):\n\n"

    return team_header + team_response


async def handle_team_formation(
    agent_name: str,
    form_team_agents: list[MatrixID],
    form_team_mode: TeamMode,
    event_body: str,
    room_id: str,
    orchestrator: MultiAgentOrchestrator,
    thread_history: list[dict],
    config: Config,
) -> str | None:
    """Handle team formation and response generation.

    Returns the team response text if this agent should handle it, None otherwise.
    Only the first agent alphabetically handles the team response to avoid duplicates.

    Args:
        agent_name: Name of the current agent
        form_team_agents: List of agents that should form a team
        form_team_mode: Mode for team collaboration
        event_body: The message body to respond to
        room_id: The room ID where the message was sent
        orchestrator: The orchestrator instance for team coordination
        thread_history: History of messages in the thread
        config: Application configuration

    Returns:
        Team response text if this agent handles it, None if another agent should handle it

    """
    # Let the first agent alphabetically handle the team
    # Convert MatrixID objects to agent names for comparison and team response
    agent_names = [mid.agent_name(config) or mid.username for mid in form_team_agents]
    first_agent = min(agent_names)
    logger.debug("Team formation", agent_names=agent_names, first_agent=first_agent, current_agent=agent_name)
    if agent_name != first_agent:
        # Other agents in the team don't respond individually
        logger.debug(f"Agent {agent_name} is not first agent {first_agent}, returning None")
        return None

    # Create and execute team response
    model_name = get_team_model(agent_name, room_id, config)
    return await create_team_response(
        agent_names=agent_names,
        mode=form_team_mode,
        message=event_body,
        orchestrator=orchestrator,
        thread_history=thread_history,
        model_name=model_name,
    )
