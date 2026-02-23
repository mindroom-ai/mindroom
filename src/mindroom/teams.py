"""Team-based collaboration for multiple agents."""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

from agno.agent import Agent
from agno.models.message import Message
from agno.run.agent import RunContentEvent as AgentRunContentEvent
from agno.run.agent import RunOutput
from agno.run.agent import ToolCallCompletedEvent as AgentToolCallCompletedEvent
from agno.run.agent import ToolCallStartedEvent as AgentToolCallStartedEvent
from agno.run.team import RunContentEvent as TeamRunContentEvent
from agno.run.team import TeamRunOutput
from agno.run.team import ToolCallCompletedEvent as TeamToolCallCompletedEvent
from agno.run.team import ToolCallStartedEvent as TeamToolCallStartedEvent
from agno.team import Team
from pydantic import BaseModel, Field

from . import agent_prompts
from .ai import get_model_instance
from .constants import ROUTER_AGENT_NAME
from .error_handling import get_user_friendly_error_message
from .logging_config import get_logger
from .matrix.rooms import get_room_alias_from_id
from .thread_utils import get_available_agents_in_room
from .tool_events import (
    StructuredStreamChunk,
    ToolTraceEntry,
    complete_pending_tool_block,
    extract_tool_completed_info,
    format_tool_started_event,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Sequence

    import nio
    from agno.media import Image
    from agno.models.response import ToolExecution

    from .bot import MultiAgentOrchestrator
    from .config import Config
    from .matrix.identity import MatrixID


logger = get_logger(__name__)

# Message length limits for team context and logging
MAX_CONTEXT_MESSAGE_LENGTH = 200  # Maximum length for messages to include in thread context
MAX_LOG_MESSAGE_LENGTH = 500  # Maximum length for messages in team response logs
TeamStreamChunk = str | StructuredStreamChunk


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


def format_team_header(agent_names: list[str]) -> str:
    """Format the team response header.

    Args:
        agent_names: List of agent names in the team

    Returns:
        Formatted header string

    """
    return f"🤝 **Team Response** ({', '.join(agent_names)}):\n\n"


def format_member_contribution(agent_name: str, content: str, indent: int = 0) -> str:
    """Format a single team member's contribution.

    Args:
        agent_name: Name of the agent
        content: The agent's response content
        indent: Indentation level

    Returns:
        Formatted contribution string

    """
    indent_str = "  " * indent
    return f"{indent_str}**{agent_name}**: {content}"


def format_team_consensus(consensus: str, indent: int = 0) -> list[str]:
    """Format the team consensus section.

    Args:
        consensus: The consensus content
        indent: Indentation level

    Returns:
        List of formatted lines for the consensus

    """
    indent_str = "  " * indent
    parts = []
    if consensus:
        parts.append(f"\n{indent_str}**Team Consensus**:")
        parts.append(f"{indent_str}{consensus}")
    return parts


def format_no_consensus_note(indent: int = 0) -> str:
    """Format the note when there's no team consensus.

    Args:
        indent: Indentation level

    Returns:
        Formatted note string

    """
    indent_str = "  " * indent
    return f"\n{indent_str}*No team consensus - showing individual responses only*"


def format_team_response(response: TeamRunOutput | RunOutput) -> list[str]:
    """Format a complete team response with member contributions.

    Handles nested teams recursively with proper indentation.

    Args:
        response: The team or agent response to extract contributions from

    Returns:
        List of formatted contribution strings

    """
    return _format_contributions_recursive(response, indent=0, include_consensus=True)


def _format_contributions_recursive(  # noqa: C901
    response: TeamRunOutput | RunOutput,
    indent: int,
    include_consensus: bool,
) -> list[str]:
    """Internal recursive function for formatting contributions.

    Args:
        response: The response to extract from
        indent: Current indentation level
        include_consensus: Whether to include team consensus

    Returns:
        List of formatted contribution strings

    """
    parts = []
    indent_str = "  " * indent

    if isinstance(response, TeamRunOutput):
        if response.member_responses:
            for member_resp in response.member_responses:
                if isinstance(member_resp, TeamRunOutput):
                    team_name = member_resp.team_name or "Nested Team"
                    parts.append(f"{indent_str}**{team_name}** (Team):")
                    nested_parts = _format_contributions_recursive(
                        member_resp,
                        indent=indent + 1,
                        include_consensus=False,  # No consensus for nested teams
                    )
                    parts.extend(nested_parts)
                elif isinstance(member_resp, RunOutput):
                    agent_name = member_resp.agent_name or "Team Member"
                    content = _get_response_content(member_resp)
                    if content:
                        parts.append(format_member_contribution(agent_name, content, indent))

        if include_consensus:
            if response.content:
                parts.extend(format_team_consensus(response.content, indent))
            elif parts:
                parts.append(format_no_consensus_note(indent))

    elif isinstance(response, RunOutput):
        agent_name = response.agent_name or "Agent"
        content = _get_response_content(response)
        if content:
            parts.append(format_member_contribution(agent_name, content, indent))

    return parts


def _get_response_content(response: TeamRunOutput | RunOutput) -> str:
    """Get content from a response object.

    Args:
        response: The response to extract content from

    Returns:
        The extracted content as a string

    """
    if response.content:
        return str(response.content)

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

        return "\n\n".join(content_parts) if content_parts else ""

    return ""


class TeamFormationDecision(NamedTuple):
    """Result of decide_team_formation."""

    should_form_team: bool
    agents: list[MatrixID]
    mode: TeamMode


async def select_team_mode(
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
        output_schema=TeamModeDecision,
    )

    try:
        response = await agent.arun(prompt, session_id="team_mode_decision")
        decision = response.content
        if isinstance(decision, TeamModeDecision):
            logger.info(f"Team mode: {decision.mode} - {decision.reasoning}")
            return TeamMode.COORDINATE if decision.mode == "coordinate" else TeamMode.COLLABORATE
        # Fallback if response is unexpected
        logger.debug(f"Unexpected response type from AI: {type(decision).__name__}, defaulting to collaborate")
        return TeamMode.COLLABORATE  # noqa: TRY300
    except Exception as e:
        logger.debug(f"AI team mode decision failed (will use default): {e}")
        return TeamMode.COLLABORATE


async def decide_team_formation(
    agent: MatrixID,
    tagged_agents: list[MatrixID],
    agents_in_thread: list[MatrixID],
    all_mentioned_in_thread: list[MatrixID],
    room: nio.MatrixRoom,
    message: str | None = None,
    config: Config | None = None,
    use_ai_decision: bool = True,
    is_dm_room: bool = False,
    is_thread: bool = False,
    available_agents_in_room: list[MatrixID] | None = None,
) -> TeamFormationDecision:
    """Determine if a team should form and with which mode.

    Args:
        agent: The agent calling this function
        tagged_agents: Agents explicitly mentioned in the current message
        agents_in_thread: Agents that have participated in the thread
        all_mentioned_in_thread: All agents ever mentioned in the thread
        room: The Matrix room object (for checking available agents)
        message: The user's message (for AI decision context)
        config: Application configuration (for AI model access)
        use_ai_decision: Whether to use AI for mode selection
        is_dm_room: Whether this is a DM room
        is_thread: Whether the current message is in a thread
        available_agents_in_room: Optional pre-filtered room agents for DM fallback logic

    Returns:
        TeamFormationDecision with team formation decision

    """
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
        available_agents = available_agents_in_room
        if available_agents is None:
            available_agents = get_available_agents_in_room(room, config)
        if len(available_agents) > 1:
            logger.info(f"Team formation needed for DM room with multiple agents: {available_agents}")
            team_agents = available_agents

    if not team_agents:
        return TeamFormationDecision(
            should_form_team=False,
            agents=[],
            mode=TeamMode.COLLABORATE,
        )

    is_first_agent = min(team_agents, key=lambda x: x.username) == agent
    # Only do this AI call for the first agent to avoid duplication
    if use_ai_decision and message and config and is_first_agent:
        agent_names = [mid.agent_name(config) or mid.username for mid in team_agents]
        mode = await select_team_mode(message, agent_names, config)
    else:
        # Fallback to hardcoded logic when AI decision is disabled or unavailable
        # Use COORDINATE when agents are explicitly tagged (they likely have different roles)
        # Use COLLABORATE when agents are from thread history (likely discussing same topic)
        mode = TeamMode.COORDINATE if len(tagged_agents) > 1 else TeamMode.COLLABORATE
        logger.info(f"Using hardcoded mode selection: {mode.value}")

    return TeamFormationDecision(should_form_team=True, agents=team_agents, mode=mode)


def _build_prompt_with_context(
    message: str,
    thread_history: list[dict] | None = None,
) -> str:
    """Build a prompt with thread context if available.

    Args:
        message: The user's message
        thread_history: Optional thread history for context

    Returns:
        Formatted prompt with context

    """
    if not thread_history:
        return message

    recent_messages = thread_history[-30:]  # Last 30 messages for context
    context_parts = []
    for msg in recent_messages:
        sender = msg.get("sender", "Unknown")
        body = msg.get("content", {}).get("body", "")
        if body and len(body) < MAX_CONTEXT_MESSAGE_LENGTH:
            context_parts.append(f"{sender}: {body}")

    if context_parts:
        context = "\n".join(context_parts)
        return f"Thread Context:\n{context}\n\nUser: {message}"

    return message


def _get_agents_from_orchestrator(
    agent_names: list[str],
    orchestrator: MultiAgentOrchestrator,
) -> list[Agent]:
    """Get Agent instances from orchestrator for the given agent names.

    Args:
        agent_names: List of agent names to get
        orchestrator: The orchestrator containing agent bots

    Returns:
        List of Agent instances (excluding router and missing agents)

    """
    agents: list[Agent] = []
    for name in agent_names:
        if name == ROUTER_AGENT_NAME:
            continue

        if name not in orchestrator.agent_bots:
            logger.warning(f"Agent '{name}' not found in orchestrator - may not be in room")
            continue

        agent_bot = orchestrator.agent_bots[name]
        if agent_bot.agent is not None:
            agent = agent_bot.agent
            # Remove interactive question prompts to prevent emoji conflicts in team responses
            if isinstance(agent.instructions, list):
                agent.instructions = [
                    instr
                    for instr in agent.instructions
                    if isinstance(instr, str) and instr != agent_prompts.INTERACTIVE_QUESTION_PROMPT
                ]
            agents.append(agent)
        else:
            logger.warning(f"Agent bot '{name}' has no agent instance")

    return agents


def _create_team_instance(
    agents: list[Agent],
    agent_names: list[str],
    mode: TeamMode,
    orchestrator: MultiAgentOrchestrator,
    model_name: str | None = None,
) -> Team:
    """Create a configured Team instance.

    Args:
        agents: List of Agent instances for the team
        agent_names: List of agent names (for team name)
        mode: Team collaboration mode
        orchestrator: The orchestrator containing configuration
        model_name: Optional model name override

    Returns:
        Configured Team instance

    """
    assert orchestrator.config is not None
    model = get_model_instance(orchestrator.config, model_name or "default")

    return Team(
        members=agents,  # type: ignore[arg-type]
        name=f"Team-{'-'.join(agent_names)}",
        model=model,
        delegate_to_all_members=mode == TeamMode.COLLABORATE,
        show_members_responses=True,
        debug_mode=False,
        # Agno will automatically list members with their names, roles, and tools
    )


def select_model_for_team(team_name: str, room_id: str, config: Config) -> str:
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
    room_alias = get_room_alias_from_id(room_id)

    if room_alias and room_alias in config.room_models:
        model = config.room_models[room_alias]
        logger.info(f"Using room-specific model for {team_name} in {room_alias}: {model}")
        return model

    if team_name in config.teams:
        team_config = config.teams[team_name]
        if team_config.model:
            logger.info(f"Using team-specific model for {team_name}: {team_config.model}")
            return team_config.model

    logger.info(f"Using default model for {team_name}")
    return "default"


NO_AGENTS_RESPONSE = "Sorry, no agents available for team collaboration."


async def team_response(
    agent_names: list[str],
    mode: TeamMode,
    message: str,
    orchestrator: MultiAgentOrchestrator,
    thread_history: list[dict] | None = None,
    model_name: str | None = None,
    images: Sequence[Image] | None = None,
) -> str:
    """Create a team and execute response."""
    agents = _get_agents_from_orchestrator(agent_names, orchestrator)

    if not agents:
        return NO_AGENTS_RESPONSE

    prompt = _build_prompt_with_context(message, thread_history)
    team = _create_team_instance(agents, agent_names, mode, orchestrator, model_name)
    agent_list = ", ".join(str(a.name) for a in agents if a.name)

    logger.info(f"Executing team response with {len(agents)} agents in {mode.value} mode")
    logger.info(f"TEAM PROMPT: {prompt[:500]}")

    try:
        response = await team.arun(prompt, images=images)
    except Exception as e:
        logger.exception(f"Error in team response with agents {agent_list}")
        # Return user-friendly error message
        team_name = f"Team ({agent_list})"
        return get_user_friendly_error_message(e, team_name)

    if isinstance(response, TeamRunOutput):
        if response.member_responses:
            logger.debug(f"Team had {len(response.member_responses)} member responses")

        logger.info(f"Team consensus content: {response.content[:200] if response.content else 'None'}")

        parts = format_team_response(response)
        team_response = "\n\n".join(parts) if parts else "No team response generated."
    else:
        logger.warning(f"Unexpected response type: {type(response)}", response=response)
        team_response = str(response)

    logger.info(f"TEAM RESPONSE ({agent_list}): {team_response[:MAX_LOG_MESSAGE_LENGTH]}")
    if len(team_response) > MAX_LOG_MESSAGE_LENGTH:
        logger.debug(f"TEAM RESPONSE (full): {team_response}")

    # Don't use @ mentions as that would trigger the agents again
    agent_names = [str(a.name) for a in agents if a.name]
    team_header = format_team_header(agent_names)

    return team_header + team_response


async def team_response_stream_raw(
    agent_ids: list[MatrixID],
    mode: TeamMode,
    message: str,
    orchestrator: MultiAgentOrchestrator,
    thread_history: list[dict] | None = None,
    model_name: str | None = None,
    images: Sequence[Image] | None = None,
) -> AsyncIterator[Any]:
    """Yield raw team events (for structured live rendering). Falls back to a final response.

    Returns an async iterator of Agno events when supported; otherwise yields a
    single TeamRunOutput for non-streaming providers.
    """
    assert orchestrator.config is not None
    agent_names = [mid.agent_name(orchestrator.config) or mid.username for mid in agent_ids]
    agents = _get_agents_from_orchestrator(agent_names, orchestrator)

    if not agents:

        async def _empty() -> AsyncIterator[RunOutput]:
            yield RunOutput(content=NO_AGENTS_RESPONSE)

        return _empty()

    prompt = _build_prompt_with_context(message, thread_history)
    team = _create_team_instance(agents, agent_names, mode, orchestrator, model_name)

    logger.info(f"Created team with {len(agents)} agents in {mode.value} mode")
    for agent in agents:
        logger.debug(f"Team member: {agent.name}")

    try:
        return team.arun(prompt, stream=True, stream_events=True, images=images)
    except Exception as e:
        logger.exception(f"Error in team streaming with agents {agent_names}")
        team_name = f"Team ({', '.join(agent_names)})"
        error_message = get_user_friendly_error_message(e, team_name)

        async def _error() -> AsyncIterator[RunOutput]:
            yield RunOutput(content=error_message)

        return _error()


async def team_response_stream(  # noqa: C901, PLR0912, PLR0915
    agent_ids: list[MatrixID],
    message: str,
    orchestrator: MultiAgentOrchestrator,
    mode: TeamMode = TeamMode.COORDINATE,
    thread_history: list[dict] | None = None,
    model_name: str | None = None,
    images: Sequence[Image] | None = None,
    show_tool_calls: bool = True,
) -> AsyncIterator[TeamStreamChunk]:
    """Aggregate team streaming into a non-stream-style document, live.

    Renders a header and per-member sections, optionally adding a team
    consensus if present. Rebuilds the entire document as new events
    arrive so the final shape matches the non-stream style.
    """
    assert orchestrator.config is not None
    agent_names: list[str] = []
    display_names: list[str] = []

    for mid in agent_ids:
        agent_name = mid.agent_name(orchestrator.config)
        assert agent_name is not None
        agent_names.append(agent_name)

        agent_config = orchestrator.config.agents[agent_name]
        display_name = agent_config.display_name or agent_name
        display_names.append(display_name)

    # Buffers keyed by display names (Agno emits display name as agent_name)
    per_member: dict[str, str] = dict.fromkeys(display_names, "")
    consensus: str = ""
    tool_trace: list[ToolTraceEntry] = []
    next_tool_index = 1
    pending_tools: list[tuple[str, int, str]] = []

    logger.info(f"Team streaming setup - agents: {agent_names}, display names: {display_names}")

    # Acquire raw event stream
    raw_stream = await team_response_stream_raw(
        agent_ids=agent_ids,
        mode=mode,
        message=message,
        orchestrator=orchestrator,
        thread_history=thread_history,
        model_name=model_name,
        images=images,
    )

    def _scope_key_for_agent(agent_name: str) -> str:
        return f"agent:{agent_name}"

    def _get_consensus() -> str:
        return consensus

    def _append_to_consensus(text: str) -> None:
        nonlocal consensus
        consensus += text

    def _set_consensus(value: str) -> None:
        nonlocal consensus
        consensus = value

    def _ensure_hidden_tool_gap(*, get_text: Callable[[], str], apply_text: Callable[[str], None]) -> None:
        if not get_text().endswith("\n\n"):
            apply_text("\n\n")

    def _start_tool(
        *,
        scope_key: str,
        get_text: Callable[[], str],
        apply_text: Callable[[str], None],
        tool: ToolExecution | None,
    ) -> None:
        nonlocal next_tool_index
        if not show_tool_calls:
            _ensure_hidden_tool_gap(get_text=get_text, apply_text=apply_text)
            return

        tool_msg, trace_entry = format_tool_started_event(tool, tool_index=next_tool_index)
        if tool_msg:
            apply_text(tool_msg)
        if trace_entry is not None:
            tool_trace.append(trace_entry)
            pending_tools.append((trace_entry.tool_name, next_tool_index, scope_key))
            next_tool_index += 1

    def _complete_tool(
        *,
        scope_key: str,
        get_text: Callable[[], str],
        set_text: Callable[[str], None],
        tool: ToolExecution | None,
    ) -> None:
        info = extract_tool_completed_info(tool)
        if not info:
            return
        if not show_tool_calls:
            return

        tool_name, result = info
        match_pos = next(
            (
                pos
                for pos in range(len(pending_tools) - 1, -1, -1)
                if pending_tools[pos][0] == tool_name and pending_tools[pos][2] == scope_key
            ),
            None,
        )
        if match_pos is None:
            logger.warning(
                "Missing pending tool start in team stream; skipping completion marker",
                tool_name=tool_name,
                scope=scope_key,
            )
            return

        _, tool_index, _ = pending_tools.pop(match_pos)
        updated_text, trace_entry = complete_pending_tool_block(
            get_text(),
            tool_name,
            result,
            tool_index=tool_index,
        )
        set_text(updated_text)

        if 0 < tool_index <= len(tool_trace):
            existing_entry = tool_trace[tool_index - 1]
            existing_entry.type = "tool_call_completed"
            existing_entry.result_preview = trace_entry.result_preview
            existing_entry.truncated = existing_entry.truncated or trace_entry.truncated
        else:
            logger.warning(
                "Missing tool trace slot in team stream for completion",
                tool_name=tool_name,
                tool_index=tool_index,
                trace_len=len(tool_trace),
            )

    async for event in raw_stream:
        # Handle error case
        if isinstance(event, RunOutput):
            content = _get_response_content(event)
            if NO_AGENTS_RESPONSE in content:
                yield content
                return
            logger.warning(f"Unexpected RunOutput in team stream: {content[:100]}")
            continue

        # Individual agent response event
        elif isinstance(event, AgentRunContentEvent):
            agent_name = event.agent_name
            if agent_name:
                content = str(event.content or "")
                if agent_name not in per_member:
                    per_member[agent_name] = ""
                per_member[agent_name] += content

        # Agent tool call started
        elif isinstance(event, AgentToolCallStartedEvent):
            agent_name = event.agent_name
            if agent_name:
                if agent_name not in per_member:
                    per_member[agent_name] = ""
                _start_tool(
                    scope_key=_scope_key_for_agent(agent_name),
                    get_text=lambda agent_name=agent_name: per_member[agent_name],
                    apply_text=lambda text, agent_name=agent_name: per_member.__setitem__(
                        agent_name,
                        per_member[agent_name] + text,
                    ),
                    tool=event.tool,
                )

        # Agent tool call completed
        elif isinstance(event, AgentToolCallCompletedEvent):
            agent_name = event.agent_name
            if agent_name:
                if agent_name not in per_member:
                    per_member[agent_name] = ""
                _complete_tool(
                    scope_key=_scope_key_for_agent(agent_name),
                    get_text=lambda agent_name=agent_name: per_member[agent_name],
                    set_text=lambda value, agent_name=agent_name: per_member.__setitem__(agent_name, value),
                    tool=event.tool,
                )

        # Team consensus content event
        elif isinstance(event, TeamRunContentEvent):
            if event.content:
                consensus += str(event.content)
            else:
                logger.debug("Empty team consensus event received")

        # Team-level tool call events (no specific agent context)
        elif isinstance(event, TeamToolCallStartedEvent):
            _start_tool(
                scope_key="team",
                get_text=_get_consensus,
                apply_text=_append_to_consensus,
                tool=event.tool,
            )

        elif isinstance(event, TeamToolCallCompletedEvent):
            _complete_tool(
                scope_key="team",
                get_text=_get_consensus,
                set_text=_set_consensus,
                tool=event.tool,
            )

        # Skip other event types
        else:
            logger.debug(f"Ignoring event type: {type(event).__name__}")
            continue

        parts: list[str] = []

        # First render configured agents (display names) in order
        for display in display_names:
            body = per_member.get(display, "").strip()
            if body:
                parts.append(format_member_contribution(display, body))
        # Then render any late/unknown agents that appeared during stream
        for display, body in per_member.items():
            if display not in display_names and body.strip():
                parts.append(format_member_contribution(display, body.strip()))

        if consensus.strip():
            parts.extend(format_team_consensus(consensus.strip()))
        elif parts:
            parts.append(format_no_consensus_note())

        if parts:
            header = format_team_header(agent_names)
            full_text = "\n\n".join(parts)
            chunk_tool_trace = tool_trace.copy() if show_tool_calls and tool_trace else None
            yield StructuredStreamChunk(content=header + full_text, tool_trace=chunk_tool_trace)
