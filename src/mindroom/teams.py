"""Team-based collaboration for multiple agents."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

from agno.agent import Agent
from agno.models.message import Message
from agno.run.agent import RunContentEvent as AgentRunContentEvent
from agno.run.agent import RunOutput
from agno.run.agent import ToolCallCompletedEvent as AgentToolCallCompletedEvent
from agno.run.agent import ToolCallStartedEvent as AgentToolCallStartedEvent
from agno.run.team import RunContentEvent as TeamRunContentEvent
from agno.run.team import RunErrorEvent as TeamRunErrorEvent
from agno.run.team import TeamRunOutput
from agno.run.team import ToolCallCompletedEvent as TeamToolCallCompletedEvent
from agno.run.team import ToolCallStartedEvent as TeamToolCallStartedEvent
from agno.team import Team
from pydantic import BaseModel, Field

from mindroom.agents import create_agent
from mindroom.ai import get_model_instance
from mindroom.authorization import get_available_agents_in_room
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.error_handling import get_user_friendly_error_message
from mindroom.knowledge.utils import ensure_request_knowledge_managers, get_agent_knowledge
from mindroom.logging_config import get_logger
from mindroom.matrix.rooms import get_room_alias_from_id
from mindroom.media_fallback import append_inline_media_fallback_prompt, should_retry_without_inline_media
from mindroom.media_inputs import MediaInputs
from mindroom.tool_system.events import (
    StructuredStreamChunk,
    ToolTraceEntry,
    complete_pending_tool_block,
    extract_tool_completed_info,
    format_tool_started_event,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping

    import nio
    from agno.models.response import ToolExecution

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.knowledge.manager import KnowledgeManager
    from mindroom.matrix.identity import MatrixID
    from mindroom.orchestrator import MultiAgentOrchestrator
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


logger = get_logger(__name__)

# Message length limits for team context and logging
_MAX_CONTEXT_MESSAGE_LENGTH = 200  # Maximum length for messages to include in thread context
_MAX_LOG_MESSAGE_LENGTH = 500  # Maximum length for messages in team response logs
_TeamStreamChunk = str | StructuredStreamChunk


class TeamMode(str, Enum):
    """Team collaboration modes."""

    COORDINATE = "coordinate"  # Leader delegates and synthesizes (can be sequential OR parallel)
    COLLABORATE = "collaborate"  # All members work on same task in parallel


class _TeamModeDecision(BaseModel):
    """AI decision for team collaboration mode."""

    mode: Literal["coordinate", "collaborate"] = Field(
        description="coordinate for delegation and synthesis, collaborate for all working on same task",
    )
    reasoning: str = Field(description="Brief explanation of why this mode was chosen")


@dataclass(frozen=True)
class _ResolvedTeamMembers:
    """One resolved team-member set shared across team execution and rendering."""

    requested_agent_names: list[str]
    available_agent_names: list[str]
    agents: list[Agent]
    display_names: list[str]


def _format_team_header(agent_names: list[str]) -> str:
    """Format the team response header.

    Args:
        agent_names: List of agent names in the team

    Returns:
        Formatted header string

    """
    return f"🤝 **Team Response** ({', '.join(agent_names)}):\n\n"


def _format_member_contribution(agent_name: str, content: str, indent: int = 0) -> str:
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


def _format_team_consensus(consensus: str, indent: int = 0) -> list[str]:
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


def _format_no_consensus_note(indent: int = 0) -> str:
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
                        parts.append(_format_member_contribution(agent_name, content, indent))

        if include_consensus:
            if response.content:
                parts.extend(_format_team_consensus(response.content, indent))
            elif parts:
                parts.append(_format_no_consensus_note(indent))

    elif isinstance(response, RunOutput):
        agent_name = response.agent_name or "Agent"
        content = _get_response_content(response)
        if content:
            parts.append(_format_member_contribution(agent_name, content, indent))

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


@dataclass(frozen=True)
class TeamFormationDecision:
    """Result of decide_team_formation."""

    kind: Literal["team", "none", "reject"]
    agents: list[MatrixID]
    mode: TeamMode

    @classmethod
    def none(cls) -> TeamFormationDecision:
        """Return the no-team outcome."""
        return cls(kind="none", agents=[], mode=TeamMode.COLLABORATE)

    @classmethod
    def reject(cls) -> TeamFormationDecision:
        """Return the explicit-rejection outcome."""
        return cls(kind="reject", agents=[], mode=TeamMode.COLLABORATE)

    @classmethod
    def team(cls, *, agents: list[MatrixID], mode: TeamMode) -> TeamFormationDecision:
        """Return the successful team-formation outcome."""
        return cls(kind="team", agents=agents, mode=mode)


class _FilteredTeamMembers(NamedTuple):
    """Filtered team members plus whether the request was explicitly rejected."""

    agents: list[MatrixID]
    rejected_request: bool


class _CandidateTeamMembers(NamedTuple):
    """Candidate team members and whether the source may degrade by filtering."""

    agents: list[MatrixID]
    allow_partial: bool


async def _select_team_mode(
    message: str,
    agent_names: list[str],
    config: Config,
    runtime_paths: RuntimePaths,
) -> TeamMode:
    """Use AI to determine optimal team collaboration mode.

    Args:
        message: The user's message/task
        agent_names: List of agents that will form the team
        config: Application configuration for model access
        runtime_paths: Explicit runtime context for model and Matrix identity resolution

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

    model = get_model_instance(config, runtime_paths, "default")
    agent = Agent(
        name="TeamModeDecider",
        role="Determine team mode",
        model=model,
        output_schema=_TeamModeDecision,
    )

    try:
        response = await agent.arun(prompt, session_id="team_mode_decision")
        decision = response.content
        if isinstance(decision, _TeamModeDecision):
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
    runtime_paths: RuntimePaths,
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
        runtime_paths: Explicit runtime context for permissions and identity resolution
        message: The user's message (for AI decision context)
        config: Application configuration (for AI model access)
        use_ai_decision: Whether to use AI for mode selection
        is_dm_room: Whether this is a DM room
        is_thread: Whether the current message is in a thread
        available_agents_in_room: Optional pre-filtered room agents for DM fallback logic

    Returns:
        TeamFormationDecision with team formation decision

    """
    candidate_team_members = _candidate_team_agents(
        tagged_agents,
        all_mentioned_in_thread,
        agents_in_thread,
        room,
        config,
        runtime_paths,
        is_dm_room=is_dm_room,
        is_thread=is_thread,
        available_agents_in_room=available_agents_in_room,
    )
    team_agents = candidate_team_members.agents

    if not team_agents:
        return TeamFormationDecision.none()

    if config is not None:
        filtered_team_members = _filter_supported_team_agents(
            team_agents,
            config,
            runtime_paths,
            allow_partial=candidate_team_members.allow_partial,
        )
        team_agents = filtered_team_members.agents
        if len(team_agents) < 2:
            if filtered_team_members.rejected_request:
                return TeamFormationDecision.reject()
            return TeamFormationDecision.none()

    is_first_agent = min(team_agents, key=lambda x: x.username) == agent
    # Only do this AI call for the first agent to avoid duplication
    if use_ai_decision and message and config and is_first_agent:
        agent_names = [mid.agent_name(config, runtime_paths) or mid.username for mid in team_agents]
        mode = await _select_team_mode(message, agent_names, config, runtime_paths)
    else:
        # Fallback to hardcoded logic when AI decision is disabled or unavailable
        # Use COORDINATE when agents are explicitly tagged (they likely have different roles)
        # Use COLLABORATE when agents are from thread history (likely discussing same topic)
        mode = TeamMode.COORDINATE if len(tagged_agents) > 1 else TeamMode.COLLABORATE
        logger.info(f"Using hardcoded mode selection: {mode.value}")

    return TeamFormationDecision.team(agents=team_agents, mode=mode)


def _candidate_team_agents(
    tagged_agents: list[MatrixID],
    all_mentioned_in_thread: list[MatrixID],
    agents_in_thread: list[MatrixID],
    room: nio.MatrixRoom,
    config: Config | None,
    runtime_paths: RuntimePaths,
    *,
    is_dm_room: bool,
    is_thread: bool,
    available_agents_in_room: list[MatrixID] | None,
) -> _CandidateTeamMembers:
    """Return the candidate team members for one response."""
    if len(tagged_agents) > 1:
        logger.info(f"Team formation needed for tagged agents: {tagged_agents}")
        return _CandidateTeamMembers(tagged_agents, False)
    if not tagged_agents and len(all_mentioned_in_thread) > 1:
        logger.info(f"Team formation needed for previously mentioned agents: {all_mentioned_in_thread}")
        return _CandidateTeamMembers(all_mentioned_in_thread, False)
    if not tagged_agents and len(agents_in_thread) > 1:
        logger.info(f"Team formation needed for thread agents: {agents_in_thread}")
        return _CandidateTeamMembers(agents_in_thread, False)
    if not (is_dm_room and not is_thread and not tagged_agents and room and config):
        return _CandidateTeamMembers([], False)

    available_agents = available_agents_in_room
    if available_agents is None:
        available_agents = get_available_agents_in_room(room, config, runtime_paths)
    if len(available_agents) <= 1:
        return _CandidateTeamMembers([], False)

    logger.info(f"Team formation needed for DM room with multiple agents: {available_agents}")
    return _CandidateTeamMembers(available_agents, True)


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
        body = msg.get("body", "")
        if body and len(body) < _MAX_CONTEXT_MESSAGE_LENGTH:
            context_parts.append(f"{sender}: {body}")

    if context_parts:
        context = "\n".join(context_parts)
        return f"Thread Context:\n{context}\n\nUser: {message}"

    return message


def _filter_supported_team_agents(
    agent_ids: list[MatrixID],
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    allow_partial: bool,
) -> _FilteredTeamMembers:
    """Return the team-eligible agents from one candidate set."""
    candidate_agents: list[tuple[MatrixID, str]] = []
    for agent_id in agent_ids:
        agent_name = agent_id.agent_name(config, runtime_paths) or agent_id.username
        if agent_name == ROUTER_AGENT_NAME:
            continue
        candidate_agents.append((agent_id, agent_name))
    unsupported_agents = config.get_unsupported_team_agents([name for _, name in candidate_agents])
    if unsupported_agents and not allow_partial:
        logger.info(
            "Rejecting team formation because requested members are unsupported",
            unsupported_agents={
                agent_name: config.unsupported_team_agent_message(
                    agent_name,
                    prefix="Team request",
                    private_targets=private_targets,
                )
                for agent_name, private_targets in unsupported_agents.items()
            },
        )
        return _FilteredTeamMembers([], True)
    if unsupported_agents:
        logger.info(
            "Skipping unsupported team members during ad hoc team formation",
            skipped_agents=sorted(unsupported_agents),
        )
    return _FilteredTeamMembers(
        [agent_id for agent_id, agent_name in candidate_agents if agent_name not in unsupported_agents],
        False,
    )


def _get_agents_from_orchestrator(
    agent_names: list[str],
    orchestrator: MultiAgentOrchestrator,
    execution_identity: ToolExecutionIdentity | None,
    request_knowledge_managers: Mapping[str, KnowledgeManager] | None = None,
) -> list[Agent]:
    """Get Agent instances from orchestrator for the given agent names."""
    assert orchestrator.config is not None
    agents: list[Agent] = []

    def _shared_manager(base_id: str) -> KnowledgeManager | None:
        return orchestrator.knowledge_managers.get(base_id)

    def _on_missing_agent_bases(agent_name: str, missing_base_ids: list[str]) -> None:
        logger.warning(
            "Knowledge bases not available for team agent",
            agent_name=agent_name,
            knowledge_bases=missing_base_ids,
        )

    for name in agent_names:
        if name not in orchestrator.agent_bots:
            logger.warning(f"Agent '{name}' not found in orchestrator - may not be in room")
            continue
        knowledge = get_agent_knowledge(
            name,
            orchestrator.config,
            orchestrator.runtime_paths,
            request_knowledge_managers=request_knowledge_managers,
            shared_manager_lookup=_shared_manager,
            on_missing_bases=lambda missing_base_ids, agent_name=name: _on_missing_agent_bases(
                agent_name,
                missing_base_ids,
            ),
        )
        agent = create_agent(
            name,
            orchestrator.config,
            orchestrator.runtime_paths,
            execution_identity=execution_identity,
            knowledge=knowledge,
            include_interactive_questions=False,
        )
        agents.append(agent)

    return agents


def _available_team_agent_names(
    agent_names: list[str],
    orchestrator: MultiAgentOrchestrator,
) -> list[str]:
    """Return team member names that can be materialized from the orchestrator."""
    available_agent_names: list[str] = []
    for name in agent_names:
        if name == ROUTER_AGENT_NAME:
            continue
        if name not in orchestrator.agent_bots:
            logger.warning(f"Agent '{name}' not found in orchestrator - may not be in room")
            continue
        available_agent_names.append(name)
    return available_agent_names


def _requested_team_agent_names(agent_names: list[str]) -> list[str]:
    """Return the requested team members, excluding router placeholders."""
    return [name for name in agent_names if name != ROUTER_AGENT_NAME]


def _resolve_team_members(
    agent_names: list[str],
    orchestrator: MultiAgentOrchestrator,
    execution_identity: ToolExecutionIdentity | None,
    request_knowledge_managers: Mapping[str, KnowledgeManager] | None = None,
) -> _ResolvedTeamMembers:
    """Resolve the materialized team-member set for one request."""
    assert orchestrator.config is not None
    requested_agent_names = _requested_team_agent_names(agent_names)
    orchestrator.config.assert_team_agents_supported(requested_agent_names)
    available_agent_names = _available_team_agent_names(requested_agent_names, orchestrator)
    agents = _get_agents_from_orchestrator(
        available_agent_names,
        orchestrator,
        execution_identity,
        request_knowledge_managers=request_knowledge_managers,
    )
    display_names = [str(agent.name) for agent in agents if agent.name]
    return _ResolvedTeamMembers(
        requested_agent_names=requested_agent_names,
        available_agent_names=available_agent_names,
        agents=agents,
        display_names=display_names,
    )


async def _ensure_request_team_knowledge_managers(
    agent_names: list[str],
    orchestrator: MultiAgentOrchestrator,
    execution_identity: ToolExecutionIdentity | None,
) -> dict[str, KnowledgeManager]:
    """Resolve request-scoped knowledge managers needed for one team request."""
    if execution_identity is None:
        return {}
    assert orchestrator.config is not None
    try:
        return await ensure_request_knowledge_managers(
            agent_names,
            config=orchestrator.config,
            runtime_paths=orchestrator.runtime_paths,
            execution_identity=execution_identity,
        )
    except Exception:
        logger.exception(
            "Failed to initialize request-scoped knowledge managers for team request",
            agent_names=agent_names,
        )
        return {}


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
    model = get_model_instance(orchestrator.config, orchestrator.runtime_paths, model_name or "default")

    return Team(
        members=agents,  # type: ignore[arg-type]
        name=f"Team-{'-'.join(agent_names)}",
        model=model,
        delegate_to_all_members=mode == TeamMode.COLLABORATE,
        show_members_responses=True,
        debug_mode=False,
        # Agno will automatically list members with their names, roles, and tools
    )


def select_model_for_team(
    team_name: str,
    room_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> str:
    """Get the appropriate model for a team in a specific room.

    Priority:
    1. Room-specific model from room_models
    2. Team's configured model
    3. Global default model

    Args:
        team_name: Name of the team
        room_id: Matrix room ID
        config: Application configuration
        runtime_paths: Explicit runtime context for room alias resolution

    Returns:
        Model name to use

    """
    room_alias = get_room_alias_from_id(room_id, runtime_paths)

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


_NO_AGENTS_RESPONSE = "Sorry, no agents available for team collaboration."


async def team_response(
    agent_names: list[str],
    mode: TeamMode,
    message: str,
    orchestrator: MultiAgentOrchestrator,
    execution_identity: ToolExecutionIdentity | None,
    thread_history: list[dict] | None = None,
    model_name: str | None = None,
    media: MediaInputs | None = None,
    session_id: str | None = None,
    user_id: str | None = None,
) -> str:
    """Create a team and execute response."""
    assert orchestrator.config is not None
    requested_agent_names = _requested_team_agent_names(agent_names)
    orchestrator.config.assert_team_agents_supported(requested_agent_names)
    request_knowledge_managers = await _ensure_request_team_knowledge_managers(
        requested_agent_names,
        orchestrator,
        execution_identity,
    )
    team_members = _resolve_team_members(
        agent_names,
        orchestrator,
        execution_identity,
        request_knowledge_managers=request_knowledge_managers,
    )
    agents = team_members.agents
    if not agents:
        return _NO_AGENTS_RESPONSE

    media_inputs = media or MediaInputs()
    prompt = _build_prompt_with_context(message, thread_history)
    team = _create_team_instance(agents, team_members.available_agent_names, mode, orchestrator, model_name)
    agent_list = ", ".join(str(a.name) for a in agents if a.name)
    team_name = f"Team ({agent_list})"

    logger.info(f"Executing team response with {len(agents)} agents in {mode.value} mode")
    logger.info(f"TEAM PROMPT: {prompt[:500]}")

    async def _run(current_prompt: str, current_media_inputs: MediaInputs) -> object:
        return await team.arun(
            current_prompt,
            session_id=session_id,
            user_id=user_id,
            audio=current_media_inputs.audio,
            images=current_media_inputs.images,
            files=current_media_inputs.files,
            videos=current_media_inputs.videos,
        )

    try:
        response = await _run(prompt, media_inputs)
    except Exception as e:
        if not should_retry_without_inline_media(e, media_inputs):
            logger.exception(f"Error in team response with agents {agent_list}")
            return get_user_friendly_error_message(e, team_name)
        logger.warning(
            "Retrying team response without inline media after validation error",
            agents=agent_list,
            error=str(e),
        )
        try:
            response = await _run(append_inline_media_fallback_prompt(prompt), MediaInputs())
        except Exception as retry_error:
            logger.exception(f"Error in team response with agents {agent_list}")
            return get_user_friendly_error_message(retry_error, team_name)

    if isinstance(response, TeamRunOutput):
        if response.member_responses:
            logger.debug(f"Team had {len(response.member_responses)} member responses")

        logger.info(f"Team consensus content: {response.content[:200] if response.content else 'None'}")

        parts = format_team_response(response)
        team_response_text = "\n\n".join(parts) if parts else "No team response generated."
    else:
        logger.warning(f"Unexpected response type: {type(response)}", response=response)
        team_response_text = str(response)

    logger.info(f"TEAM RESPONSE ({agent_list}): {team_response_text[:_MAX_LOG_MESSAGE_LENGTH]}")
    if len(team_response_text) > _MAX_LOG_MESSAGE_LENGTH:
        logger.debug(f"TEAM RESPONSE (full): {team_response_text}")

    team_header = _format_team_header(team_members.display_names)
    return team_header + team_response_text


async def _team_response_stream_raw(
    team_members: _ResolvedTeamMembers,
    mode: TeamMode,
    message: str,
    orchestrator: MultiAgentOrchestrator,
    thread_history: list[dict] | None = None,
    model_name: str | None = None,
    media: MediaInputs | None = None,
    session_id: str | None = None,
    user_id: str | None = None,
) -> AsyncIterator[Any]:
    """Yield raw team events (for structured live rendering). Falls back to a final response.

    Returns an async iterator of Agno events when supported; otherwise yields a
    single TeamRunOutput for non-streaming providers.
    """
    agents = team_members.agents

    if not agents:

        async def _empty() -> AsyncIterator[RunOutput]:
            yield RunOutput(content=_NO_AGENTS_RESPONSE)

        return _empty()

    media_inputs = media or MediaInputs()
    prompt = _build_prompt_with_context(message, thread_history)
    team = _create_team_instance(agents, team_members.available_agent_names, mode, orchestrator, model_name)
    logger.info(f"Created team with {len(agents)} agents in {mode.value} mode")
    for agent in agents:
        logger.debug(f"Team member: {agent.name}")

    def _start_stream(current_prompt: str, current_media_inputs: MediaInputs) -> AsyncIterator[Any]:
        return team.arun(
            current_prompt,
            stream=True,
            stream_events=True,
            session_id=session_id,
            user_id=user_id,
            audio=current_media_inputs.audio,
            images=current_media_inputs.images,
            files=current_media_inputs.files,
            videos=current_media_inputs.videos,
        )

    try:
        return _start_stream(prompt, media_inputs)
    except Exception as e:
        logger.exception(f"Error in team streaming with agents {team_members.display_names}")
        error_text = str(e)

        async def _error(content: str = error_text) -> AsyncIterator[TeamRunErrorEvent]:
            yield TeamRunErrorEvent(content=content)

        return _error()


async def team_response_stream(  # noqa: C901, PLR0912, PLR0915
    agent_ids: list[MatrixID],
    message: str,
    orchestrator: MultiAgentOrchestrator,
    execution_identity: ToolExecutionIdentity | None,
    mode: TeamMode = TeamMode.COORDINATE,
    thread_history: list[dict] | None = None,
    model_name: str | None = None,
    media: MediaInputs | None = None,
    show_tool_calls: bool = True,
    session_id: str | None = None,
    user_id: str | None = None,
) -> AsyncIterator[_TeamStreamChunk]:
    """Aggregate team streaming into a non-stream-style document, live.

    Renders a header and per-member sections, optionally adding a team
    consensus if present. Rebuilds the entire document as new events
    arrive so the final shape matches the non-stream style.
    """
    assert orchestrator.config is not None
    requested_agent_names = [
        mid.agent_name(orchestrator.config, orchestrator.runtime_paths) or mid.username for mid in agent_ids
    ]
    request_knowledge_managers = await _ensure_request_team_knowledge_managers(
        requested_agent_names,
        orchestrator,
        execution_identity,
    )
    team_members = _resolve_team_members(
        requested_agent_names,
        orchestrator,
        execution_identity,
        request_knowledge_managers=request_knowledge_managers,
    )
    agent_names = team_members.display_names
    display_names = team_members.display_names

    logger.info(f"Team streaming setup - agents: {agent_names}, display names: {display_names}")
    media_inputs = media or MediaInputs()
    attempt_message = message
    attempt_media_inputs = media_inputs
    team_name = f"Team ({', '.join(agent_names)})"

    per_member: dict[str, str] = {}
    consensus: str = ""
    tool_trace: list[ToolTraceEntry] = []
    next_tool_index = 1
    pending_tools: list[tuple[str, int, str]] = []

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

    def _start_tool_for_member(agent_name: str, tool: ToolExecution | None) -> None:
        if agent_name not in per_member:
            per_member[agent_name] = ""

        def _get_text() -> str:
            return per_member[agent_name]

        def _apply_text(text: str) -> None:
            per_member[agent_name] += text

        _start_tool(
            scope_key=_scope_key_for_agent(agent_name),
            get_text=_get_text,
            apply_text=_apply_text,
            tool=tool,
        )

    def _complete_tool_for_member(agent_name: str, tool: ToolExecution | None) -> None:
        if agent_name not in per_member:
            per_member[agent_name] = ""

        def _get_text() -> str:
            return per_member[agent_name]

        def _set_text(value: str) -> None:
            per_member[agent_name] = value

        _complete_tool(
            scope_key=_scope_key_for_agent(agent_name),
            get_text=_get_text,
            set_text=_set_text,
            tool=tool,
        )

    for retried_without_inline_media in (False, True):
        # Buffers keyed by display names (Agno emits display name as agent_name)
        per_member = dict.fromkeys(display_names, "")
        consensus = ""
        tool_trace = []
        next_tool_index = 1
        pending_tools = []
        emitted_output = False
        retry_requested = False

        raw_stream = await _team_response_stream_raw(
            team_members=team_members,
            mode=mode,
            message=attempt_message,
            orchestrator=orchestrator,
            thread_history=thread_history,
            model_name=model_name,
            media=attempt_media_inputs,
            session_id=session_id,
            user_id=user_id,
        )
        async for event in raw_stream:
            # Handle explicit fallback stream outputs (for example no agents available)
            if isinstance(event, RunOutput):
                content = _get_response_content(event)
                yield content
                return

            # Handle setup/stream-time team errors from provider/model
            if isinstance(event, TeamRunErrorEvent):
                error_text = event.content or "Unknown team error"
                if (
                    not retried_without_inline_media
                    and not emitted_output
                    and should_retry_without_inline_media(error_text, attempt_media_inputs)
                ):
                    logger.warning(
                        "Retrying team streaming without inline media after team error",
                        agents=", ".join(agent_names),
                        error=error_text,
                    )
                    attempt_message = append_inline_media_fallback_prompt(attempt_message)
                    attempt_media_inputs = MediaInputs()
                    retry_requested = True
                    break
                yield get_user_friendly_error_message(Exception(error_text), team_name)
                return

            # Individual agent response event
            if isinstance(event, AgentRunContentEvent):
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
                    _start_tool_for_member(agent_name, event.tool)

            # Agent tool call completed
            elif isinstance(event, AgentToolCallCompletedEvent):
                agent_name = event.agent_name
                if agent_name:
                    _complete_tool_for_member(agent_name, event.tool)

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
                    parts.append(_format_member_contribution(display, body))
            # Then render any late/unknown agents that appeared during stream
            for display, body in per_member.items():
                if display not in display_names and body.strip():
                    parts.append(_format_member_contribution(display, body.strip()))

            if consensus.strip():
                parts.extend(_format_team_consensus(consensus.strip()))
            elif parts:
                parts.append(_format_no_consensus_note())

            if parts:
                emitted_output = True
                header = _format_team_header(team_members.display_names)
                full_text = "\n\n".join(parts)
                chunk_tool_trace = tool_trace.copy() if show_tool_calls and tool_trace else None
                yield StructuredStreamChunk(content=header + full_text, tool_trace=chunk_tool_trace)

        if retry_requested:
            continue

        return
