"""Team-based collaboration for multiple agents."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal, NoReturn, cast
from uuid import uuid4

from agno.agent import Agent
from agno.db.base import SessionType
from agno.models.message import Message
from agno.run.agent import RunContentEvent as AgentRunContentEvent
from agno.run.agent import RunOutput
from agno.run.agent import ToolCallCompletedEvent as AgentToolCallCompletedEvent
from agno.run.agent import ToolCallStartedEvent as AgentToolCallStartedEvent
from agno.run.base import RunStatus
from agno.run.team import RunCancelledEvent as TeamRunCancelledEvent
from agno.run.team import RunContentEvent as TeamRunContentEvent
from agno.run.team import RunErrorEvent as TeamRunErrorEvent
from agno.run.team import TeamRunOutput
from agno.run.team import ToolCallCompletedEvent as TeamToolCallCompletedEvent
from agno.run.team import ToolCallStartedEvent as TeamToolCallStartedEvent
from agno.session.team import TeamSession
from agno.team import Team
from pydantic import BaseModel, Field

from mindroom import ai_runtime, model_loading
from mindroom.agents import create_agent, get_team_session
from mindroom.ai import build_matrix_run_metadata
from mindroom.authorization import get_available_agents_in_room
from mindroom.constants import MATRIX_SEEN_EVENT_IDS_METADATA_KEY, ROUTER_AGENT_NAME
from mindroom.error_handling import get_user_friendly_error_message
from mindroom.execution_preparation import (
    ThreadHistoryRenderLimits,
    prepare_bound_team_execution_context,
    render_prepared_team_messages_text,
)
from mindroom.history.interrupted_replay import split_interrupted_tool_trace, tool_execution_call_id
from mindroom.history.runtime import (
    ScopeSessionContext,
    apply_replay_plan,
    close_team_runtime_sqlite_dbs,
    open_bound_scope_session_context,
    resolve_bound_team_scope_context,
    scrub_scope_session_queued_notices,
)
from mindroom.history.storage import update_scope_seen_event_ids
from mindroom.hooks import EnrichmentItem, render_system_enrichment_block
from mindroom.knowledge import KnowledgeManager, ensure_request_knowledge_managers, get_agent_knowledge
from mindroom.logging_config import get_logger
from mindroom.matrix.rooms import get_room_alias_from_id
from mindroom.media_fallback import append_inline_media_fallback_prompt, should_retry_without_inline_media
from mindroom.media_inputs import MediaInputs
from mindroom.team_exact_members import (
    ResolvedExactTeamMembers,
    materialize_exact_requested_team_members,
    resolve_live_shared_agent_names,
)
from mindroom.tool_system.events import (
    StructuredStreamChunk,
    ToolTraceEntry,
    complete_pending_tool_block,
    extract_tool_completed_info,
    format_tool_completed_event,
    format_tool_started_event,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Collection, Mapping, Sequence

    import nio
    from agno.db.sqlite import SqliteDb
    from agno.models.response import ToolExecution

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.history import CompactionOutcome
    from mindroom.history.turn_recorder import TurnRecorder
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.matrix.identity import MatrixID
    from mindroom.orchestrator import MultiAgentOrchestrator
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


logger = get_logger(__name__)

# Message length limits for team context and logging
_MAX_CONTEXT_MESSAGE_LENGTH = 200  # Maximum length for messages to include in thread context
_MAX_LOG_MESSAGE_LENGTH = 500  # Maximum length for messages in team response logs
_TeamStreamChunk = str | StructuredStreamChunk
_NO_AGENTS_RESPONSE = "Sorry, no agents available for team collaboration."
_MATRIX_TEAM_THREAD_HISTORY_RENDER_LIMITS = ThreadHistoryRenderLimits(
    max_messages=30,
    max_message_length=_MAX_CONTEXT_MESSAGE_LENGTH,
    missing_sender_label="Unknown",
)


@dataclass
class _PendingTeamTool:
    scope_key: str
    tool_name: str
    trace_entry: ToolTraceEntry
    tool_call_id: str | None = None
    visible_tool_index: int | None = None


class TeamMode(str, Enum):
    """Team collaboration modes."""

    COORDINATE = "coordinate"  # Leader delegates and synthesizes (can be sequential OR parallel)
    COLLABORATE = "collaborate"  # All members work on same task in parallel


@dataclass(frozen=True)
class _PreparedMaterializedTeamExecution:
    """Shared prepared team execution state used by stream and non-stream paths."""

    messages: tuple[Message, ...]
    run_metadata: dict[str, Any] | None
    unseen_event_ids: list[str]

    @property
    def prepared_prompt(self) -> str:
        """Return the prompt-visible text derived from canonical live messages."""
        return render_prepared_team_messages_text(self.messages)

    @property
    def context_messages(self) -> tuple[Message, ...]:
        """Return replayed context messages without the current user turn."""
        return self.messages[:-1]


def _next_retry_run_id(run_id: str | None) -> str | None:
    """Return a fresh Agno run identifier for a retry attempt."""
    if run_id is None:
        return None
    return str(uuid4())


class _TeamModeDecision(BaseModel):
    """AI decision for team collaboration mode."""

    mode: Literal["coordinate", "collaborate"] = Field(
        description="coordinate for delegation and synthesis, collaborate for all working on same task",
    )
    reasoning: str = Field(description="Brief explanation of why this mode was chosen")


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


def is_errored_run_output(response: TeamRunOutput | RunOutput) -> bool:
    """Return whether a team or agent fallback run ended in an error state."""
    status = response.status.value if isinstance(response.status, RunStatus) else response.status
    return str(status).lower() == "error"


def is_cancelled_run_output(response: TeamRunOutput | RunOutput) -> bool:
    """Return whether a team or agent fallback run ended in a cancelled state."""
    status = response.status.value if isinstance(response.status, RunStatus) else response.status
    return str(status).lower() == "cancelled"


def _team_response_text(response: TeamRunOutput | RunOutput) -> str:
    """Render one final team response body without the shared team header."""
    parts = format_team_response(response)
    return "\n\n".join(parts) if parts else (_get_response_content(response) or "No team response generated.")


def _format_terminal_team_response(
    response: TeamRunOutput | RunOutput,
    *,
    team_display_names: list[str],
) -> str:
    """Render the final user-visible text for one terminal team fallback output."""
    return _format_team_header(team_display_names) + _team_response_text(response)


def _cleanup_team_notice_state(
    *,
    run_output: TeamRunOutput | RunOutput | None,
    scope_context: ScopeSessionContext | None,
    session_id: str | None,
    entity_name: str,
) -> None:
    """Strip queued-message notices from returned and persisted team state."""
    ai_runtime.cleanup_queued_notice_state(
        run_output=run_output,
        storage=scope_context.storage if scope_context is not None else None,
        session_id=session_id,
        session_type=SessionType.TEAM,
        entity_name=entity_name,
    )


def _scrub_team_retry_notice_state(
    *,
    scope_context: ScopeSessionContext | None,
    entity_name: str,
) -> None:
    """Strip queued-message notices from the loaded team session before retry."""
    scrub_scope_session_queued_notices(
        scope_context=scope_context,
        entity_name=entity_name,
    )


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


class TeamIntent(str, Enum):
    """How one team request was formed."""

    EXPLICIT_MEMBERS = "explicit_members"
    CONFIGURED_TEAM = "configured_team"
    IMPLICIT_THREAD_TEAM = "implicit_thread_team"
    DM_AUTO_TEAM = "dm_auto_team"


class TeamMemberStatus(str, Enum):
    """Eligibility of one requested team member."""

    ELIGIBLE = "eligible"
    NOT_IN_ROOM = "not_in_room"
    HIDDEN_FROM_SENDER = "hidden_from_sender"
    UNSUPPORTED_FOR_TEAM = "unsupported_for_team"
    NOT_MATERIALIZABLE = "not_materializable"


class TeamOutcome(str, Enum):
    """Final resolution outcome for one team request."""

    TEAM = "team"
    INDIVIDUAL = "individual"
    NONE = "none"
    REJECT = "reject"


@dataclass(frozen=True)
class TeamResolutionMember:
    """Status of one requested team member."""

    agent: MatrixID
    name: str
    status: TeamMemberStatus
    private_targets: tuple[str, ...] | None = None


@dataclass(frozen=True)
class TeamResolution:
    """First-class team resolution result consumed across bot and API flows."""

    intent: TeamIntent | None
    requested_members: list[MatrixID]
    member_statuses: list[TeamResolutionMember]
    eligible_members: list[MatrixID]
    outcome: TeamOutcome
    reason: str | None = None
    mode: TeamMode | None = None

    def __post_init__(self) -> None:
        """Keep the resolution internally coherent."""
        if self.outcome is TeamOutcome.TEAM:
            if self.mode is None:
                msg = "Team resolutions require a mode."
                raise ValueError(msg)
            if not self.eligible_members:
                msg = "Team resolutions require at least one eligible member."
                raise ValueError(msg)
            return

        if self.mode is not None:
            msg = "Only team resolutions may include a mode."
            raise ValueError(msg)
        if self.outcome is TeamOutcome.INDIVIDUAL and len(self.eligible_members) != 1:
            msg = "Individual resolutions require exactly one eligible member."
            raise ValueError(msg)
        if self.outcome is TeamOutcome.NONE and self.eligible_members:
            msg = "None resolutions cannot include eligible members."
            raise ValueError(msg)
        if self.outcome is TeamOutcome.REJECT and self.reason is None:
            msg = "Reject resolutions require a reason."
            raise ValueError(msg)

    @classmethod
    def none(cls) -> TeamResolution:
        """Return the no-team outcome."""
        return cls(
            intent=None,
            requested_members=[],
            member_statuses=[],
            eligible_members=[],
            outcome=TeamOutcome.NONE,
        )

    @classmethod
    def reject(
        cls,
        *,
        intent: TeamIntent,
        requested_members: list[MatrixID],
        member_statuses: list[TeamResolutionMember],
        reason: str,
    ) -> TeamResolution:
        """Return an explicit rejection result."""
        eligible_members = [member.agent for member in member_statuses if member.status is TeamMemberStatus.ELIGIBLE]
        return cls(
            intent=intent,
            requested_members=requested_members,
            member_statuses=member_statuses,
            eligible_members=eligible_members,
            outcome=TeamOutcome.REJECT,
            reason=reason,
        )

    @classmethod
    def team(
        cls,
        *,
        intent: TeamIntent,
        requested_members: list[MatrixID],
        member_statuses: list[TeamResolutionMember],
        eligible_members: list[MatrixID],
        mode: TeamMode,
    ) -> TeamResolution:
        """Return the successful team outcome."""
        return cls(
            intent=intent,
            requested_members=requested_members,
            member_statuses=member_statuses,
            eligible_members=eligible_members,
            outcome=TeamOutcome.TEAM,
            mode=mode,
        )

    @classmethod
    def individual(
        cls,
        *,
        intent: TeamIntent,
        requested_members: list[MatrixID],
        member_statuses: list[TeamResolutionMember],
        agent: MatrixID,
    ) -> TeamResolution:
        """Return the single-agent degraded outcome."""
        return cls(
            intent=intent,
            requested_members=requested_members,
            member_statuses=member_statuses,
            eligible_members=[agent],
            outcome=TeamOutcome.INDIVIDUAL,
        )


@dataclass(frozen=True)
class _SelectedTeamRequest:
    """Normalized team request selected from one message context."""

    intent: TeamIntent | None
    requested_members: list[MatrixID]


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

    model = model_loading.get_model_instance(config, runtime_paths, "default")
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
            logger.info("team_mode_decided", mode=decision.mode, reasoning=decision.reasoning)
            return TeamMode.COORDINATE if decision.mode == "coordinate" else TeamMode.COLLABORATE
        # Fallback if response is unexpected
        logger.debug(
            "team_mode_decision_unexpected_type",
            response_type=type(decision).__name__,
        )
        return TeamMode.COLLABORATE  # noqa: TRY300
    except Exception as e:
        logger.warning("team_mode_decision_failed", error=str(e))
        return TeamMode.COLLABORATE


async def decide_team_formation(
    agent: MatrixID,
    tagged_agents: list[MatrixID],
    agents_in_thread: list[MatrixID],
    all_mentioned_in_thread: list[MatrixID],
    room: nio.MatrixRoom | None,
    runtime_paths: RuntimePaths,
    message: str | None = None,
    config: Config | None = None,
    use_ai_decision: bool = True,
    is_dm_room: bool = False,
    is_thread: bool = False,
    available_agents_in_room: list[MatrixID] | None = None,
    materializable_agent_names: set[str] | None = None,
) -> TeamResolution:
    """Determine if a team should form and with which mode.

    Args:
        agent: The agent calling this function
        tagged_agents: Raw agents explicitly mentioned in the current message
        agents_in_thread: Agents that have participated in the thread
        all_mentioned_in_thread: All agents ever mentioned in the thread
        room: The Matrix room object when room-membership visibility is available
        runtime_paths: Explicit runtime context for permissions and identity resolution
        message: The user's message (for AI decision context)
        config: Application configuration (for AI model access)
        use_ai_decision: Whether to use AI for mode selection
        is_dm_room: Whether this is a DM room
        is_thread: Whether the current message is in a thread
        available_agents_in_room: Optional pre-filtered room agents for sender-visible availability
        materializable_agent_names: Optional live agent names that can currently produce a response

    Returns:
        TeamResolution with explicit intent, member statuses, and final outcome

    """
    team_request = _select_team_request(
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
    if team_request.intent is None or not team_request.requested_members:
        return TeamResolution.none()

    member_statuses = _evaluate_team_members(
        team_request.requested_members,
        config,
        runtime_paths,
        room=room,
        sender_visible_agents=available_agents_in_room,
        materializable_agent_names=materializable_agent_names,
    )
    resolution = _resolve_team_request(
        intent=team_request.intent,
        requested_members=team_request.requested_members,
        member_statuses=member_statuses,
        config=config,
        reason_prefix="Team request",
    )
    if resolution.outcome is not TeamOutcome.TEAM:
        return resolution

    team_agents = resolution.eligible_members

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
        logger.info("team_mode_selected_hardcoded", mode=mode.value)

    return replace(resolution, mode=mode)


def _team_member_name(
    agent_id: MatrixID,
    config: Config | None,
    runtime_paths: RuntimePaths,
) -> str:
    """Return the canonical agent name used throughout team resolution."""
    if config is None:
        return agent_id.username
    return agent_id.agent_name(config, runtime_paths) or agent_id.username


def _filter_team_request_members(
    agent_ids: list[MatrixID],
    config: Config | None,
    runtime_paths: RuntimePaths,
) -> list[MatrixID]:
    """Keep only actual teamable agents while preserving the requested order."""
    filtered: list[MatrixID] = []
    for agent_id in agent_ids:
        if config is not None:
            agent_name = agent_id.agent_name(config, runtime_paths)
            if agent_name is None or agent_name == ROUTER_AGENT_NAME or agent_name not in config.agents:
                continue
        filtered.append(agent_id)
    return filtered


def _normalize_team_request_members(
    agent_ids: list[MatrixID],
    config: Config | None,
    runtime_paths: RuntimePaths,
) -> list[MatrixID]:
    """Deduplicate ad hoc candidates after filtering out non-teamable agents."""
    normalized: list[MatrixID] = []
    seen_ids: set[str] = set()
    for agent_id in _filter_team_request_members(agent_ids, config, runtime_paths):
        if agent_id.full_id in seen_ids:
            continue
        normalized.append(agent_id)
        seen_ids.add(agent_id.full_id)
    return normalized


def _select_team_request(
    tagged_agents: list[MatrixID],
    all_mentioned_in_thread: list[MatrixID],
    agents_in_thread: list[MatrixID],
    room: nio.MatrixRoom | None,
    config: Config | None,
    runtime_paths: RuntimePaths,
    *,
    is_dm_room: bool,
    is_thread: bool,
    available_agents_in_room: list[MatrixID] | None,
) -> _SelectedTeamRequest:
    """Return the normalized team request implied by one message context."""
    normalized_tagged_agents = _normalize_team_request_members(tagged_agents, config, runtime_paths)
    if len(normalized_tagged_agents) > 1:
        logger.info(
            "team_formation_requested",
            trigger="tagged_agents",
            agents=[agent.full_id for agent in normalized_tagged_agents],
        )
        return _SelectedTeamRequest(TeamIntent.EXPLICIT_MEMBERS, normalized_tagged_agents)

    normalized_mentioned_agents = _normalize_team_request_members(all_mentioned_in_thread, config, runtime_paths)
    if not normalized_tagged_agents and len(normalized_mentioned_agents) > 1:
        logger.info(
            "team_formation_requested",
            trigger="previously_mentioned_agents",
            agents=[agent.full_id for agent in normalized_mentioned_agents],
        )
        return _SelectedTeamRequest(TeamIntent.IMPLICIT_THREAD_TEAM, normalized_mentioned_agents)

    normalized_thread_agents = _normalize_team_request_members(agents_in_thread, config, runtime_paths)
    if not normalized_tagged_agents and len(normalized_thread_agents) > 1:
        logger.info(
            "team_formation_requested",
            trigger="thread_agents",
            agents=[agent.full_id for agent in normalized_thread_agents],
        )
        return _SelectedTeamRequest(TeamIntent.IMPLICIT_THREAD_TEAM, normalized_thread_agents)

    if not (is_dm_room and not is_thread and not normalized_tagged_agents and room and config):
        return _SelectedTeamRequest(None, [])

    available_agents = available_agents_in_room
    if available_agents is None:
        available_agents = get_available_agents_in_room(room, config, runtime_paths)
    normalized_available_agents = _normalize_team_request_members(available_agents, config, runtime_paths)
    if len(normalized_available_agents) <= 1:
        return _SelectedTeamRequest(None, [])

    logger.info(
        "team_formation_requested",
        trigger="dm_room_multiple_agents",
        agents=[agent.full_id for agent in normalized_available_agents],
    )
    return _SelectedTeamRequest(TeamIntent.DM_AUTO_TEAM, normalized_available_agents)


def _sender_unavailable_team_agents_message(agent_names: list[str], *, prefix: str = "Team request") -> str:
    """Return the rejection message for requested agents hidden from this sender."""
    if len(agent_names) == 1:
        return f"{prefix} includes agent '{agent_names[0]}' that is not available to you in this room."
    return (
        f"{prefix} includes agents "
        + ", ".join(f"'{agent_name}'" for agent_name in agent_names)
        + " that are not available to you in this room."
    )


def _mixed_unavailable_team_agents_message(agent_names: list[str], *, prefix: str = "Team request") -> str:
    """Return the rejection message when requested agents fail multiple availability checks."""
    if len(agent_names) == 1:
        return f"{prefix} includes agent '{agent_names[0]}' that is not available for this request."
    return (
        f"{prefix} includes agents "
        + ", ".join(f"'{agent_name}'" for agent_name in agent_names)
        + " that are not available for this request."
    )


def _room_unavailable_team_agents_message(agent_names: list[str], *, prefix: str = "Team request") -> str:
    """Return the rejection message for team members that are not available in the room."""
    if len(agent_names) == 1:
        return f"{prefix} includes agent '{agent_names[0]}' that is not available in this room."
    return (
        f"{prefix} includes agents "
        + ", ".join(f"'{agent_name}'" for agent_name in agent_names)
        + " that are not available in this room."
    )


def _not_materializable_team_agents_message(agent_names: list[str], *, prefix: str) -> str:
    """Return the rejection message for members that could not be materialized."""
    if len(agent_names) == 1:
        return f"{prefix} includes agent '{agent_names[0]}' that could not be materialized for this request."
    return (
        f"{prefix} includes agents "
        + ", ".join(f"'{agent_name}'" for agent_name in agent_names)
        + " that could not be materialized for this request."
    )


def _evaluate_team_members(
    requested_members: list[MatrixID],
    config: Config | None,
    runtime_paths: RuntimePaths,
    *,
    room: nio.MatrixRoom | None,
    sender_visible_agents: list[MatrixID] | None,
    materializable_agent_names: set[str] | None,
) -> list[TeamResolutionMember]:
    """Evaluate one status and response capability for each requested member."""
    room_visible_ids: set[str] | None = None
    if room is not None and config is not None:
        room_visible_ids = {
            agent_id.full_id
            for agent_id in _normalize_team_request_members(
                get_available_agents_in_room(room, config, runtime_paths),
                config,
                runtime_paths,
            )
        }
    sender_visible_ids: set[str] | None = None
    if sender_visible_agents is not None:
        sender_visible_ids = {
            agent_id.full_id
            for agent_id in _normalize_team_request_members(sender_visible_agents, config, runtime_paths)
        }

    unsupported_agents: dict[str, tuple[str, ...] | None] = {}
    if config is not None:
        unsupported_agents = config.get_unsupported_team_agents(
            [_team_member_name(agent_id, config, runtime_paths) for agent_id in requested_members],
        )

    member_statuses: list[TeamResolutionMember] = []
    for agent_id in requested_members:
        agent_name = _team_member_name(agent_id, config, runtime_paths)
        private_targets = unsupported_agents.get(agent_name)
        is_room_visible = room_visible_ids is None or agent_id.full_id in room_visible_ids
        is_sender_visible = sender_visible_ids is None or agent_id.full_id in sender_visible_ids
        is_materializable = materializable_agent_names is None or agent_name in materializable_agent_names
        if not is_room_visible:
            status = TeamMemberStatus.NOT_IN_ROOM
        elif not is_sender_visible:
            status = TeamMemberStatus.HIDDEN_FROM_SENDER
        elif agent_name in unsupported_agents:
            status = TeamMemberStatus.UNSUPPORTED_FOR_TEAM
        elif not is_materializable:
            status = TeamMemberStatus.NOT_MATERIALIZABLE
        else:
            status = TeamMemberStatus.ELIGIBLE
        member_statuses.append(
            TeamResolutionMember(
                agent=agent_id,
                name=agent_name,
                status=status,
                private_targets=private_targets,
            ),
        )
    return member_statuses


def _resolve_team_request(
    *,
    intent: TeamIntent,
    requested_members: list[MatrixID],
    member_statuses: list[TeamResolutionMember],
    config: Config | None,
    reason_prefix: str,
    mode: TeamMode | None = None,
) -> TeamResolution:
    """Apply one clear outcome policy after intent and member evaluation."""
    eligible_members = [member.agent for member in member_statuses if member.status is TeamMemberStatus.ELIGIBLE]
    rejected_members = [member for member in member_statuses if member.status is not TeamMemberStatus.ELIGIBLE]

    if intent in {TeamIntent.EXPLICIT_MEMBERS, TeamIntent.CONFIGURED_TEAM}:
        if rejected_members:
            return TeamResolution.reject(
                intent=intent,
                requested_members=requested_members,
                member_statuses=member_statuses,
                reason=_team_resolution_reason(rejected_members, config, reason_prefix=reason_prefix),
            )
        if intent is TeamIntent.CONFIGURED_TEAM or len(eligible_members) >= 2:
            return TeamResolution.team(
                intent=intent,
                requested_members=requested_members,
                member_statuses=member_statuses,
                eligible_members=eligible_members,
                mode=mode or TeamMode.COLLABORATE,
            )
        return TeamResolution.none()

    if len(eligible_members) >= 2:
        return TeamResolution.team(
            intent=intent,
            requested_members=requested_members,
            member_statuses=member_statuses,
            eligible_members=eligible_members,
            mode=mode or TeamMode.COLLABORATE,
        )
    if len(eligible_members) == 1:
        return TeamResolution.individual(
            intent=intent,
            requested_members=requested_members,
            member_statuses=member_statuses,
            agent=eligible_members[0],
        )
    return TeamResolution(
        intent=intent,
        requested_members=requested_members,
        member_statuses=member_statuses,
        eligible_members=[],
        outcome=TeamOutcome.NONE,
    )


def _team_resolution_reason(
    rejected_members: list[TeamResolutionMember],
    config: Config | None,
    *,
    reason_prefix: str,
) -> str:
    """Return one shared user-facing explanation for a rejected team request."""
    rejection_statuses = {member.status for member in rejected_members}
    if len(rejection_statuses) > 1:
        return _mixed_team_resolution_reason(
            rejected_members,
            config,
            reason_prefix=reason_prefix,
        )

    unsupported_members = [
        member for member in rejected_members if member.status is TeamMemberStatus.UNSUPPORTED_FOR_TEAM
    ]
    if unsupported_members and len(unsupported_members) == len(rejected_members) and config is not None:
        first_unsupported_member = unsupported_members[0]
        return config.unsupported_team_agent_message(
            first_unsupported_member.name,
            prefix=reason_prefix,
            private_targets=first_unsupported_member.private_targets,
        )

    not_materializable_members = [
        member for member in rejected_members if member.status is TeamMemberStatus.NOT_MATERIALIZABLE
    ]
    if not_materializable_members and len(not_materializable_members) == len(rejected_members):
        return _not_materializable_team_agents_message(
            [member.name for member in not_materializable_members],
            prefix=reason_prefix,
        )

    room_unavailable_members = [
        member.name for member in rejected_members if member.status is TeamMemberStatus.NOT_IN_ROOM
    ]
    hidden_members = [
        member.name for member in rejected_members if member.status is TeamMemberStatus.HIDDEN_FROM_SENDER
    ]
    if room_unavailable_members and not hidden_members and len(room_unavailable_members) == len(rejected_members):
        return _room_unavailable_team_agents_message(room_unavailable_members, prefix=reason_prefix)
    if hidden_members and not room_unavailable_members and len(hidden_members) == len(rejected_members):
        return _sender_unavailable_team_agents_message(hidden_members, prefix=reason_prefix)
    return _mixed_unavailable_team_agents_message([member.name for member in rejected_members], prefix=reason_prefix)


def _mixed_team_resolution_reason(
    rejected_members: list[TeamResolutionMember],
    config: Config | None,
    *,
    reason_prefix: str,
) -> str:
    """Return a per-member explanation when one reject has mixed failure causes."""
    details = [_team_resolution_member_detail(member, config) for member in rejected_members]
    return f"{reason_prefix} cannot be satisfied: " + "; ".join(details)


def _unsupported_team_member_detail(
    member: TeamResolutionMember,
    config: Config | None,
) -> str:
    """Return the unsupported-team explanation for one member."""
    if config is None:
        return f"agent '{member.name}' is unsupported for team requests"

    private_targets = member.private_targets
    if private_targets is None:
        return f"agent '{member.name}' is unknown"
    if member.name in private_targets:
        return f"agent '{member.name}' is private and cannot participate in teams yet"
    if len(private_targets) != 1:
        return (
            f"agent '{member.name}' reaches private agents "
            f"{', '.join(repr(target) for target in private_targets)} via delegation "
            "and cannot participate in teams yet"
        )
    return (
        f"agent '{member.name}' reaches private agent '{private_targets[0]}' via delegation "
        "and cannot participate in teams yet"
    )


def _team_resolution_member_detail(
    member: TeamResolutionMember,
    config: Config | None,
) -> str:
    """Return a member-specific reason fragment for mixed team-request rejects."""
    if member.status is TeamMemberStatus.UNSUPPORTED_FOR_TEAM:
        return _unsupported_team_member_detail(member, config)
    if member.status is TeamMemberStatus.NOT_MATERIALIZABLE:
        return f"agent '{member.name}' could not be materialized for this request"
    if member.status is TeamMemberStatus.NOT_IN_ROOM:
        return f"agent '{member.name}' is not available in this room"
    if member.status is TeamMemberStatus.HIDDEN_FROM_SENDER:
        return f"agent '{member.name}' is not available to you in this room"
    return f"agent '{member.name}' is not available for this request"


def resolve_configured_team(
    team_name: str,
    team_members: list[MatrixID],
    mode: TeamMode,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    materializable_agent_names: set[str] | None = None,
) -> TeamResolution:
    """Resolve one configured team while preserving the exact configured membership."""
    requested_members = _filter_team_request_members(team_members, config, runtime_paths)
    member_statuses = _evaluate_team_members(
        requested_members,
        config,
        runtime_paths,
        room=None,
        sender_visible_agents=None,
        materializable_agent_names=materializable_agent_names,
    )
    return _resolve_team_request(
        intent=TeamIntent.CONFIGURED_TEAM,
        requested_members=requested_members,
        member_statuses=member_statuses,
        config=config,
        reason_prefix=f"Team '{team_name}'",
        mode=mode,
    )


def _persist_bound_seen_event_ids(
    *,
    scope_context: ScopeSessionContext | None,
    session_id: str | None,
    event_ids: list[str],
) -> None:
    if not event_ids or scope_context is None or session_id is None:
        return
    session = scope_context.session or get_team_session(scope_context.storage, session_id)
    if session is None:
        created_at = int(datetime.now(UTC).timestamp())
        session = TeamSession(
            session_id=session_id,
            team_id=scope_context.scope.scope_id,
            metadata={},
            runs=[],
            created_at=created_at,
            updated_at=created_at,
        )
    if update_scope_seen_event_ids(session, scope_context.scope, event_ids):
        scope_context.storage.upsert_session(session)


def _run_metadata_seen_event_ids(run_metadata: dict[str, Any] | None) -> list[str]:
    if not isinstance(run_metadata, dict):
        return []
    raw_event_ids = run_metadata.get(MATRIX_SEEN_EVENT_IDS_METADATA_KEY)
    if not isinstance(raw_event_ids, list):
        return []
    return [event_id for event_id in raw_event_ids if isinstance(event_id, str) and event_id]


def _extract_interrupted_team_partial_text(response: TeamRunOutput | RunOutput) -> str:
    """Extract persisted interrupted text for a top-level team run."""
    if isinstance(response, TeamRunOutput):
        team_response = response
        if isinstance(response.content, str) and _is_cancellation_boilerplate(response.content):
            team_response = replace(response, content=None)
        parts = format_team_response(team_response)
        if parts:
            return "\n\n".join(parts).strip()
    content = _get_response_content(response).strip()
    normalized = content.lower()
    if normalized.startswith("run ") and "cancel" in normalized:
        return ""
    return content


def _extract_completed_team_tool_trace(response: TeamRunOutput | RunOutput) -> list[ToolTraceEntry]:
    """Extract completed tool calls from a possibly nested team response."""
    trace: list[ToolTraceEntry] = []
    for tool in response.tools or []:
        _, trace_entry = format_tool_completed_event(tool)
        if trace_entry is not None:
            trace.append(trace_entry)
    if isinstance(response, TeamRunOutput):
        for member_response in response.member_responses:
            if isinstance(member_response, TeamRunOutput | RunOutput):
                trace.extend(_extract_completed_team_tool_trace(member_response))
    return trace


def _extract_cancelled_team_tool_trace(
    response: TeamRunOutput | RunOutput,
) -> tuple[list[ToolTraceEntry], list[ToolTraceEntry]]:
    """Extract completed and interrupted tool calls from a cancelled team response."""
    completed_trace, interrupted_trace = split_interrupted_tool_trace(response.tools)
    if isinstance(response, TeamRunOutput):
        for member_response in response.member_responses:
            if isinstance(member_response, TeamRunOutput | RunOutput):
                member_completed, member_interrupted = _extract_cancelled_team_tool_trace(member_response)
                completed_trace.extend(member_completed)
                interrupted_trace.extend(member_interrupted)
    return completed_trace, interrupted_trace


def _is_cancellation_boilerplate(content: str) -> bool:
    """Return whether one string is just Agno cancellation boilerplate."""
    normalized = content.strip().lower()
    return normalized.startswith("run ") and "cancel" in normalized


def _raise_team_run_cancelled(reason: str | None) -> NoReturn:
    """Raise the canonical team cancellation error."""
    raise asyncio.CancelledError(reason or "Run cancelled")


def materialize_exact_team_members(
    requested_agent_names: list[str],
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
    session_id: str | None = None,
    include_openai_compat_guidance: bool = False,
    materializable_agent_names: set[str] | None = None,
    request_knowledge_managers: Mapping[str, KnowledgeManager] | None = None,
    shared_manager_lookup: Callable[[str], KnowledgeManager | None] | None = None,
    reason_prefix: str = "Team request",
) -> ResolvedExactTeamMembers:
    """Materialize the exact team-member set without silent fallback."""
    if not requested_agent_names:
        raise ValueError(_NO_AGENTS_RESPONSE)

    def _build_member(agent_name: str) -> Agent:
        def _on_missing_agent_bases(missing_base_ids: list[str]) -> None:
            logger.warning(
                "Knowledge bases not available for team agent",
                agent_name=agent_name,
                knowledge_bases=missing_base_ids,
            )

        knowledge = get_agent_knowledge(
            agent_name,
            config,
            runtime_paths,
            request_knowledge_managers=request_knowledge_managers,
            shared_manager_lookup=shared_manager_lookup,
            on_missing_bases=_on_missing_agent_bases,
        )
        return create_agent(
            agent_name,
            config,
            runtime_paths,
            execution_identity=execution_identity,
            session_id=session_id
            if session_id is not None
            else execution_identity.session_id
            if execution_identity
            else None,
            knowledge=knowledge,
            include_interactive_questions=False,
            include_openai_compat_guidance=include_openai_compat_guidance,
        )

    team_members = materialize_exact_requested_team_members(
        requested_agent_names,
        materializable_agent_names=materializable_agent_names,
        build_member=_build_member,
    )
    if team_members.failed_agent_names:
        close_team_runtime_sqlite_dbs(
            agents=team_members.agents,
            team_db=None,
            shared_scope_storage=None,
        )
        raise ValueError(
            _not_materializable_team_agents_message(team_members.failed_agent_names, prefix=reason_prefix),
        )
    return team_members


def _requested_team_agent_names(agent_names: list[str]) -> list[str]:
    """Return the requested team members, excluding router placeholders."""
    return [name for name in agent_names if name != ROUTER_AGENT_NAME]


def _materialize_team_members(
    agent_names: list[str],
    orchestrator: MultiAgentOrchestrator,
    execution_identity: ToolExecutionIdentity | None,
    *,
    session_id: str | None = None,
    request_knowledge_managers: Mapping[str, KnowledgeManager] | None = None,
    reason_prefix: str = "Team request",
) -> ResolvedExactTeamMembers:
    """Materialize the exact requested team-member set without silent fallback."""
    requested_agent_names = _requested_team_agent_names(agent_names)
    assert orchestrator.config is not None

    def _shared_manager(base_id: str) -> KnowledgeManager | None:
        return orchestrator.knowledge_managers.get(base_id)

    return materialize_exact_team_members(
        requested_agent_names,
        config=orchestrator.config,
        runtime_paths=orchestrator.runtime_paths,
        execution_identity=execution_identity,
        session_id=session_id,
        materializable_agent_names=resolve_live_shared_agent_names(orchestrator),
        request_knowledge_managers=request_knowledge_managers,
        shared_manager_lookup=_shared_manager,
        reason_prefix=reason_prefix,
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
    *,
    agents: list[Agent],
    mode: TeamMode,
    config: Config,
    runtime_paths: RuntimePaths,
    team_display_name: str,
    fallback_team_id: str,
    model_name: str | None = None,
    configured_team_name: str | None = None,
    history_storage: SqliteDb | None = None,
) -> Team:
    """Create a configured Team instance.

    Args:
        agents: List of Agent instances for the team
        mode: Team collaboration mode
        config: Active runtime configuration
        runtime_paths: Active runtime paths
        team_display_name: Human-readable Team name passed to Agno
        fallback_team_id: Stable fallback id when no team scope storage is available
        model_name: Optional model name override
        configured_team_name: Optional configured team id for stable team-scope history
        history_storage: Optional already-open shared team history storage
            owned by the caller for this request.

    Returns:
        Configured Team instance

    """
    model = model_loading.get_model_instance(config, runtime_paths, model_name or "default")
    # Coordinate-mode tool calls run through the shared team model in v1.
    # Member-agent models are intentionally not wrapped here.
    ai_runtime.install_queued_message_notice_hook(model)
    if configured_team_name is not None and configured_team_name in config.teams:
        history_settings = config.get_entity_history_settings(configured_team_name)
    else:
        history_settings = config.get_default_history_settings()
    scope_context = resolve_bound_team_scope_context(
        agents=agents,
        config=config,
        team_name=configured_team_name,
    )
    team_id = scope_context.scope.scope_id if scope_context is not None else fallback_team_id

    for agent in agents:
        # Team-owned replay should come from the shared TeamSession, not from
        # each member independently replaying their own session state.
        agent.add_history_to_context = False
        agent.add_session_summary_to_context = False

    team = Team(
        members=agents,  # type: ignore[arg-type]
        id=team_id,
        name=team_display_name,
        model=model,
        db=history_storage,
        delegate_to_all_members=mode == TeamMode.COLLABORATE,
        add_history_to_context=True,
        add_session_summary_to_context=True,
        num_history_runs=history_settings.policy.limit if history_settings.policy.mode == "runs" else None,
        num_history_messages=history_settings.policy.limit if history_settings.policy.mode == "messages" else None,
        max_tool_calls_from_history=history_settings.max_tool_calls_from_history,
        store_history_messages=False,
        show_members_responses=True,
        debug_mode=False,
        # Agno will automatically list members with their names, roles, and tools
    )
    if history_settings.policy.mode == "all":
        team.num_history_runs = None
    return team


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
    model_name = config.resolve_runtime_model(
        entity_name=team_name,
        room_id=room_id,
        runtime_paths=runtime_paths,
    ).model_name
    room_alias = get_room_alias_from_id(room_id, runtime_paths)
    if room_alias and room_alias in config.room_models:
        logger.info("using_room_specific_team_model", team_name=team_name, room_alias=room_alias, model_name=model_name)
    elif team_name in config.teams and config.teams[team_name].model:
        logger.info("using_team_specific_model", team_name=team_name, model_name=model_name)
    else:
        logger.info("using_default_team_model", team_name=team_name)
    return model_name


def build_materialized_team_instance(
    *,
    requested_agent_names: list[str],
    agents: list[Agent],
    mode: TeamMode,
    config: Config,
    runtime_paths: RuntimePaths,
    scope_context: ScopeSessionContext | None,
    model_name: str | None,
    configured_team_name: str | None,
) -> Team:
    """Build one agno.Team instance for already-materialized members."""
    resolved_team_runtime_model = config.resolve_runtime_model(
        entity_name=configured_team_name,
        active_model_name=model_name,
    )
    resolved_team_model_name = resolved_team_runtime_model.model_name
    team_label = f"Team-{'-'.join(requested_agent_names)}"
    return _create_team_instance(
        agents=agents,
        mode=mode,
        config=config,
        runtime_paths=runtime_paths,
        team_display_name=team_label,
        fallback_team_id=team_label,
        model_name=resolved_team_model_name,
        configured_team_name=configured_team_name,
        history_storage=scope_context.storage if scope_context is not None else None,
    )


async def _prepare_materialized_team_execution(
    *,
    scope_context: ScopeSessionContext | None,
    agents: list[Agent],
    team: Team,
    message: str,
    thread_history: Sequence[ResolvedVisibleMessage] | None,
    config: Config,
    runtime_paths: RuntimePaths,
    active_model_name: str | None,
    reply_to_event_id: str | None,
    active_event_ids: Collection[str],
    response_sender_id: str | None,
    current_sender_id: str | None,
    compaction_outcomes_collector: list[CompactionOutcome] | None,
    configured_team_name: str | None,
    thread_history_render_limits: ThreadHistoryRenderLimits | None = None,
    matrix_run_metadata: dict[str, Any] | None = None,
    system_enrichment_items: Sequence[EnrichmentItem] = (),
) -> _PreparedMaterializedTeamExecution:
    """Prepare one materialized team for execution."""
    ai_runtime.scrub_queued_notice_session_context(
        scope_context=scope_context,
        entity_name=configured_team_name or str(team.name or "Team"),
    )
    if system_enrichment_items:
        rendered_system_context = render_system_enrichment_block(system_enrichment_items)
        team.additional_context = rendered_system_context
        for agent in agents:
            agent.additional_context = rendered_system_context
    prepared_execution = await prepare_bound_team_execution_context(
        scope_context=scope_context,
        agents=agents,
        team=team,
        prompt=message,
        thread_history=thread_history,
        runtime_paths=runtime_paths,
        config=config,
        team_name=configured_team_name,
        active_model_name=active_model_name,
        active_context_window=config.resolve_runtime_model(
            entity_name=configured_team_name,
            active_model_name=active_model_name,
        ).context_window,
        reply_to_event_id=reply_to_event_id,
        active_event_ids=active_event_ids,
        response_sender_id=response_sender_id,
        current_sender_id=current_sender_id,
        compaction_outcomes_collector=compaction_outcomes_collector,
        thread_history_render_limits=thread_history_render_limits,
    )
    if prepared_execution.replay_plan is not None:
        apply_replay_plan(target=team, replay_plan=prepared_execution.replay_plan)
    run_metadata = build_matrix_run_metadata(
        reply_to_event_id,
        prepared_execution.unseen_event_ids,
        extra_metadata=matrix_run_metadata,
    )
    return _PreparedMaterializedTeamExecution(
        messages=prepared_execution.messages,
        run_metadata=run_metadata,
        unseen_event_ids=prepared_execution.unseen_event_ids,
    )


async def team_response(  # noqa: C901, PLR0912, PLR0915
    agent_names: list[str],
    mode: TeamMode,
    message: str,
    orchestrator: MultiAgentOrchestrator,
    execution_identity: ToolExecutionIdentity | None,
    thread_history: Sequence[ResolvedVisibleMessage] | None = None,
    model_name: str | None = None,
    media: MediaInputs | None = None,
    session_id: str | None = None,
    run_id: str | None = None,
    run_id_callback: Callable[[str], None] | None = None,
    user_id: str | None = None,
    reply_to_event_id: str | None = None,
    active_event_ids: Collection[str] = frozenset(),
    response_sender_id: str | None = None,
    compaction_outcomes_collector: list[CompactionOutcome] | None = None,
    configured_team_name: str | None = None,
    matrix_run_metadata: dict[str, Any] | None = None,
    system_enrichment_items: Sequence[EnrichmentItem] = (),
    *,
    turn_recorder: TurnRecorder,
    reason_prefix: str = "Team request",
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
    try:
        team_members = _materialize_team_members(
            agent_names,
            orchestrator,
            execution_identity,
            session_id=session_id,
            request_knowledge_managers=request_knowledge_managers,
            reason_prefix=reason_prefix,
        )
    except ValueError as exc:
        return str(exc)
    agents = team_members.agents

    agent_list = ", ".join(str(a.name) for a in agents if a.name)
    team_name = f"Team ({agent_list})"
    media_inputs = media or MediaInputs()
    team: Team | None = None
    scope_context: ScopeSessionContext | None = None
    unseen_event_ids: list[str] = []
    attempt_run_id = run_id

    try:
        with open_bound_scope_session_context(
            agents=agents,
            session_id=session_id,
            runtime_paths=orchestrator.runtime_paths,
            config=orchestrator.config,
            execution_identity=execution_identity,
            team_name=configured_team_name,
        ) as opened_scope_context:
            scope_context = opened_scope_context
            ai_runtime.scrub_queued_notice_session_context(
                scope_context=scope_context,
                entity_name=configured_team_name or team_name,
            )
            team = build_materialized_team_instance(
                requested_agent_names=team_members.requested_agent_names,
                agents=agents,
                mode=mode,
                config=orchestrator.config,
                runtime_paths=orchestrator.runtime_paths,
                scope_context=scope_context,
                model_name=model_name,
                configured_team_name=configured_team_name,
            )
            prepared_execution = await _prepare_materialized_team_execution(
                scope_context=scope_context,
                agents=agents,
                team=team,
                message=message,
                thread_history=thread_history,
                config=orchestrator.config,
                runtime_paths=orchestrator.runtime_paths,
                active_model_name=model_name,
                reply_to_event_id=reply_to_event_id,
                active_event_ids=active_event_ids,
                response_sender_id=response_sender_id,
                current_sender_id=user_id,
                compaction_outcomes_collector=compaction_outcomes_collector,
                configured_team_name=configured_team_name,
                thread_history_render_limits=_MATRIX_TEAM_THREAD_HISTORY_RENDER_LIMITS,
                matrix_run_metadata=matrix_run_metadata,
                system_enrichment_items=system_enrichment_items,
            )
            prompt = prepared_execution.prepared_prompt
            unseen_event_ids = prepared_execution.unseen_event_ids
            run_metadata = prepared_execution.run_metadata
            turn_recorder.set_run_metadata(run_metadata)
            logger.info("executing_team_response", agent_count=len(agents), mode=mode.value)
            logger.info("team_prompt_preview", agents=agent_list, prompt_preview=prompt[:500])

            async def _run(
                current_prompt: str,
                current_media_inputs: MediaInputs,
                current_run_id: str | None,
            ) -> object:
                if run_id_callback is not None and current_run_id is not None:
                    run_id_callback(current_run_id)
                prepared_input = (
                    ai_runtime.attach_media_to_run_input(current_prompt, current_media_inputs)
                    if current_media_inputs.has_any()
                    else current_prompt
                )
                return await team.arun(
                    prepared_input,
                    session_id=session_id,
                    run_id=current_run_id,
                    user_id=user_id,
                    metadata=run_metadata,
                )

            response: object | None = None
            cleaned_response: RunOutput | TeamRunOutput | None = None
            attempt_prompt = prompt
            attempt_media_inputs = media_inputs
            try:
                for retried_without_inline_media in (False, True):
                    response = None
                    cleaned_response = None
                    try:
                        response = await _run(attempt_prompt, attempt_media_inputs, attempt_run_id)
                    except Exception as e:
                        if not retried_without_inline_media and should_retry_without_inline_media(
                            e,
                            attempt_media_inputs,
                        ):
                            logger.warning(
                                "Retrying team response without inline media after validation error",
                                agents=agent_list,
                                error=str(e),
                            )
                            _scrub_team_retry_notice_state(
                                scope_context=scope_context,
                                entity_name=configured_team_name or team_name,
                            )
                            attempt_prompt = append_inline_media_fallback_prompt(prompt)
                            attempt_media_inputs = MediaInputs()
                            attempt_run_id = _next_retry_run_id(run_id)
                            continue

                        logger.exception("team_response_failed", agents=agent_list)
                        return get_user_friendly_error_message(e, team_name)

                    if isinstance(response, (TeamRunOutput, RunOutput)):
                        cleaned_response = response
                    if isinstance(response, (TeamRunOutput, RunOutput)) and is_errored_run_output(response):
                        error_text = str(response.content or "Unknown team error")
                        if not retried_without_inline_media and should_retry_without_inline_media(
                            error_text,
                            attempt_media_inputs,
                        ):
                            logger.warning(
                                "Retrying team response without inline media after errored run output",
                                agents=agent_list,
                                error=error_text,
                            )
                            _cleanup_team_notice_state(
                                run_output=response,
                                scope_context=scope_context,
                                session_id=session_id,
                                entity_name=configured_team_name or team_name,
                            )
                            _scrub_team_retry_notice_state(
                                scope_context=scope_context,
                                entity_name=configured_team_name or team_name,
                            )
                            attempt_prompt = append_inline_media_fallback_prompt(prompt)
                            attempt_media_inputs = MediaInputs()
                            attempt_run_id = _next_retry_run_id(run_id)
                            continue
                        logger.warning("Team response returned errored run output", agents=agent_list, error=error_text)

                    break

                assert response is not None
                if isinstance(response, (TeamRunOutput, RunOutput)) and is_cancelled_run_output(response):
                    partial_text = _extract_interrupted_team_partial_text(response)
                    completed_tools, interrupted_tools = _extract_cancelled_team_tool_trace(response)
                    turn_recorder.record_interrupted(
                        run_metadata=run_metadata,
                        assistant_text=partial_text,
                        completed_tools=completed_tools,
                        interrupted_tools=interrupted_tools,
                    )
                    _raise_team_run_cancelled(response.content)
                if isinstance(response, (TeamRunOutput, RunOutput)) and is_errored_run_output(response):
                    return get_user_friendly_error_message(
                        Exception(str(response.content or "Unknown team error")),
                        team_name,
                    )
                if reply_to_event_id:
                    _persist_bound_seen_event_ids(
                        scope_context=scope_context,
                        session_id=session_id,
                        event_ids=_run_metadata_seen_event_ids(run_metadata),
                    )

                if isinstance(response, (TeamRunOutput, RunOutput)):
                    if isinstance(response, TeamRunOutput) and response.member_responses:
                        logger.debug("team_member_response_count", response_count=len(response.member_responses))

                    if isinstance(response, TeamRunOutput):
                        logger.info(
                            "team_consensus_preview",
                            content_preview=response.content[:200] if response.content else None,
                        )
                    team_response_text = _team_response_text(response)
                else:
                    logger.warning(
                        "team_response_unexpected_type",
                        response_type=type(response).__name__,
                        response=response,
                    )
                    team_response_text = str(response)

                logger.info(
                    "team_response_preview",
                    agents=agent_list,
                    response_preview=team_response_text[:_MAX_LOG_MESSAGE_LENGTH],
                )
                if len(team_response_text) > _MAX_LOG_MESSAGE_LENGTH:
                    logger.debug("team_response_full", agents=agent_list, response=team_response_text)

                response_text = (
                    _format_terminal_team_response(
                        response,
                        team_display_names=team_members.display_names,
                    )
                    if isinstance(response, (TeamRunOutput, RunOutput))
                    else _format_team_header(team_members.display_names) + team_response_text
                )
                turn_recorder.record_completed(
                    run_metadata=run_metadata,
                    assistant_text=response_text,
                    completed_tools=(
                        _extract_completed_team_tool_trace(response)
                        if isinstance(response, (TeamRunOutput, RunOutput))
                        else []
                    ),
                )
                return response_text
            finally:
                _cleanup_team_notice_state(
                    run_output=cleaned_response,
                    scope_context=scope_context,
                    session_id=session_id,
                    entity_name=configured_team_name or team_name,
                )
    except asyncio.CancelledError:
        turn_recorder.record_interrupted(
            run_metadata=build_matrix_run_metadata(
                reply_to_event_id,
                unseen_event_ids,
                extra_metadata=matrix_run_metadata,
            ),
            assistant_text=turn_recorder.assistant_text,
            completed_tools=turn_recorder.completed_tools,
            interrupted_tools=turn_recorder.interrupted_tools,
        )
        raise
    except Exception as e:
        logger.exception("Error preparing team members", agents=agent_list)
        return get_user_friendly_error_message(e, team_name)
    finally:
        close_team_runtime_sqlite_dbs(
            agents=agents,
            team_db=cast("SqliteDb | None", team.db) if team is not None else None,
            shared_scope_storage=scope_context.storage if scope_context is not None else None,
        )


async def _team_response_stream_raw(
    team: Team,
    team_members: ResolvedExactTeamMembers,
    prompt: str,
    metadata: dict[str, Any] | None = None,
    media: MediaInputs | None = None,
    session_id: str | None = None,
    run_id: str | None = None,
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
    logger.info(
        "team_created",
        agent_count=len(agents),
        mode=(team.delegate_to_all_members and "collaborate") or "coordinate",
    )
    for agent in agents:
        logger.debug("team_member", agent=agent.name)

    def _start_stream(current_prompt: str, current_media_inputs: MediaInputs) -> AsyncIterator[Any]:
        prepared_input = (
            ai_runtime.attach_media_to_run_input(current_prompt, current_media_inputs)
            if current_media_inputs.has_any()
            else current_prompt
        )
        return team.arun(
            prepared_input,
            stream=True,
            stream_events=True,
            session_id=session_id,
            run_id=run_id,
            user_id=user_id,
            metadata=metadata,
        )

    try:
        return _start_stream(prompt, media_inputs)
    except Exception as e:
        logger.exception("team_streaming_failed", agents=team_members.display_names)
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
    thread_history: Sequence[ResolvedVisibleMessage] | None = None,
    model_name: str | None = None,
    media: MediaInputs | None = None,
    show_tool_calls: bool = True,
    session_id: str | None = None,
    run_id: str | None = None,
    run_id_callback: Callable[[str], None] | None = None,
    user_id: str | None = None,
    reply_to_event_id: str | None = None,
    active_event_ids: Collection[str] = frozenset(),
    response_sender_id: str | None = None,
    compaction_outcomes_collector: list[CompactionOutcome] | None = None,
    configured_team_name: str | None = None,
    matrix_run_metadata: dict[str, Any] | None = None,
    system_enrichment_items: Sequence[EnrichmentItem] = (),
    *,
    turn_recorder: TurnRecorder,
    reason_prefix: str = "Team request",
) -> AsyncIterator[_TeamStreamChunk]:
    """Aggregate team streaming into a non-stream-style document, live.

    Renders a header and per-member sections, optionally adding a team
    consensus if present. Rebuilds the entire document as new events
    arrive so the final shape matches the non-stream style.
    """
    assert orchestrator.config is not None
    requested_agent_names = _requested_team_agent_names(
        [mid.agent_name(orchestrator.config, orchestrator.runtime_paths) or mid.username for mid in agent_ids],
    )
    orchestrator.config.assert_team_agents_supported(requested_agent_names)
    request_knowledge_managers = await _ensure_request_team_knowledge_managers(
        requested_agent_names,
        orchestrator,
        execution_identity,
    )
    try:
        team_members = _materialize_team_members(
            requested_agent_names,
            orchestrator,
            execution_identity,
            session_id=session_id,
            request_knowledge_managers=request_knowledge_managers,
            reason_prefix=reason_prefix,
        )
    except ValueError as exc:
        yield str(exc)
        return
    agent_names = team_members.display_names
    display_names = team_members.display_names
    team: Team | None = None
    scope_context: ScopeSessionContext | None = None
    team_label = f"Team ({', '.join(agent_names)})"
    unseen_event_ids: list[str] = []
    attempt_run_id = run_id
    canonical_per_member: dict[str, str] = {}
    canonical_consensus = ""
    completed_tools: list[ToolTraceEntry] = []
    pending_tools: list[_PendingTeamTool] = []

    def _empty_canonical_partial_text() -> str:
        return ""

    render_canonical_partial_text: Callable[[], str] = _empty_canonical_partial_text

    try:
        with open_bound_scope_session_context(
            agents=team_members.agents,
            session_id=session_id,
            runtime_paths=orchestrator.runtime_paths,
            config=orchestrator.config,
            execution_identity=execution_identity,
            team_name=configured_team_name,
        ) as opened_scope_context:
            scope_context = opened_scope_context
            ai_runtime.scrub_queued_notice_session_context(
                scope_context=scope_context,
                entity_name=team_label,
            )
            team = build_materialized_team_instance(
                requested_agent_names=team_members.requested_agent_names,
                agents=team_members.agents,
                mode=mode,
                config=orchestrator.config,
                runtime_paths=orchestrator.runtime_paths,
                scope_context=scope_context,
                model_name=model_name,
                configured_team_name=configured_team_name,
            )
            prepared_execution = await _prepare_materialized_team_execution(
                scope_context=scope_context,
                agents=team_members.agents,
                team=team,
                message=message,
                thread_history=thread_history,
                config=orchestrator.config,
                runtime_paths=orchestrator.runtime_paths,
                active_model_name=model_name,
                reply_to_event_id=reply_to_event_id,
                active_event_ids=active_event_ids,
                response_sender_id=response_sender_id,
                current_sender_id=user_id,
                compaction_outcomes_collector=compaction_outcomes_collector,
                configured_team_name=configured_team_name,
                thread_history_render_limits=_MATRIX_TEAM_THREAD_HISTORY_RENDER_LIMITS,
                matrix_run_metadata=matrix_run_metadata,
                system_enrichment_items=system_enrichment_items,
            )
            prepared_prompt = prepared_execution.prepared_prompt
            unseen_event_ids = prepared_execution.unseen_event_ids
            run_metadata = prepared_execution.run_metadata
            turn_recorder.set_run_metadata(run_metadata)
            logger.info("team_streaming_setup", agents=agent_names, display_names=display_names)
            media_inputs = media or MediaInputs()
            attempt_prompt = prepared_prompt
            attempt_media_inputs = media_inputs

            visible_per_member: dict[str, str] = {}
            visible_consensus: str = ""
            tool_trace: list[ToolTraceEntry] = []
            next_tool_index = 1

            def _scope_key_for_agent(agent_name: str) -> str:
                return f"agent:{agent_name}"

            def _get_visible_consensus() -> str:
                return visible_consensus

            def _append_to_visible_consensus(text: str) -> None:
                nonlocal visible_consensus
                visible_consensus += text

            def _set_visible_consensus(value: str) -> None:
                nonlocal visible_consensus
                visible_consensus = value

            def _render_team_parts(
                *,
                per_member: dict[str, str],
                consensus: str,
            ) -> list[str]:
                parts: list[str] = []
                for display in display_names:
                    body = per_member.get(display, "").strip()
                    if body:
                        parts.append(_format_member_contribution(display, body))
                for display, body in per_member.items():
                    if display not in display_names and body.strip():
                        parts.append(_format_member_contribution(display, body.strip()))

                if consensus.strip():
                    parts.extend(_format_team_consensus(consensus.strip()))
                elif parts:
                    parts.append(_format_no_consensus_note())
                return parts

            def _current_canonical_partial_text() -> str:
                return "\n\n".join(
                    _render_team_parts(
                        per_member=canonical_per_member,
                        consensus=canonical_consensus,
                    ),
                )

            render_canonical_partial_text = _current_canonical_partial_text

            def _sync_live_turn_recorder() -> None:
                turn_recorder.sync_partial_state(
                    run_metadata=run_metadata,
                    assistant_text=render_canonical_partial_text(),
                    completed_tools=completed_tools,
                    interrupted_tools=[pending.trace_entry for pending in pending_tools],
                )

            def _find_pending_tool_index(
                *,
                scope_key: str,
                tool: ToolExecution | None,
            ) -> int | None:
                call_id = tool_execution_call_id(tool)
                if call_id is not None:
                    for pos in range(len(pending_tools) - 1, -1, -1):
                        pending_tool = pending_tools[pos]
                        if pending_tool.scope_key == scope_key and pending_tool.tool_call_id == call_id:
                            return pos
                info = extract_tool_completed_info(tool)
                if not info:
                    return None
                tool_name, _ = info
                for pos in range(len(pending_tools) - 1, -1, -1):
                    pending_tool = pending_tools[pos]
                    if (
                        pending_tool.scope_key == scope_key
                        and pending_tool.tool_call_id is None
                        and pending_tool.tool_name == tool_name
                    ):
                        return pos
                return None

            def _start_tool(
                *,
                scope_key: str,
                apply_visible_text: Callable[[str], None],
                tool: ToolExecution | None,
            ) -> None:
                nonlocal next_tool_index
                tool_index = next_tool_index if show_tool_calls else None
                tool_msg, trace_entry = format_tool_started_event(tool, tool_index=tool_index)
                if trace_entry is not None:
                    pending_tools.append(
                        _PendingTeamTool(
                            scope_key=scope_key,
                            tool_name=trace_entry.tool_name,
                            trace_entry=trace_entry,
                            tool_call_id=tool_execution_call_id(tool),
                            visible_tool_index=tool_index,
                        ),
                    )
                if not show_tool_calls or tool_index is None:
                    return
                if tool_msg:
                    apply_visible_text(tool_msg)
                if trace_entry is not None:
                    tool_trace.append(trace_entry)
                next_tool_index += 1

            def _complete_tool(
                *,
                scope_key: str,
                get_visible_text: Callable[[], str],
                set_visible_text: Callable[[str], None],
                tool: ToolExecution | None,
            ) -> None:
                info = extract_tool_completed_info(tool)
                if not info:
                    return

                tool_name, result = info
                pending_trace_pos = _find_pending_tool_index(scope_key=scope_key, tool=tool)
                pending_tool = pending_tools.pop(pending_trace_pos) if pending_trace_pos is not None else None
                _, completed_trace = format_tool_completed_event(tool)
                if completed_trace is not None:
                    completed_tools.append(completed_trace)
                if not show_tool_calls:
                    return

                if pending_tool is None or pending_tool.visible_tool_index is None:
                    logger.warning(
                        "Missing pending tool start in team stream; skipping completion marker",
                        tool_name=tool_name,
                        scope=scope_key,
                    )
                    return

                updated_text, trace_entry = complete_pending_tool_block(
                    get_visible_text(),
                    tool_name,
                    result,
                    tool_index=pending_tool.visible_tool_index,
                )
                set_visible_text(updated_text)

                if 0 < pending_tool.visible_tool_index <= len(tool_trace):
                    existing_entry = tool_trace[pending_tool.visible_tool_index - 1]
                    existing_entry.type = "tool_call_completed"
                    existing_entry.result_preview = trace_entry.result_preview
                    existing_entry.truncated = existing_entry.truncated or trace_entry.truncated
                else:
                    logger.warning(
                        "Missing tool trace slot in team stream for completion",
                        tool_name=tool_name,
                        tool_index=pending_tool.visible_tool_index,
                        trace_len=len(tool_trace),
                    )

            def _start_tool_for_member(agent_name: str, tool: ToolExecution | None) -> None:
                if agent_name not in visible_per_member:
                    visible_per_member[agent_name] = ""

                def _apply_visible_text(text: str) -> None:
                    visible_per_member[agent_name] += text

                _start_tool(
                    scope_key=_scope_key_for_agent(agent_name),
                    apply_visible_text=_apply_visible_text,
                    tool=tool,
                )

            def _complete_tool_for_member(agent_name: str, tool: ToolExecution | None) -> None:
                if agent_name not in visible_per_member:
                    visible_per_member[agent_name] = ""

                def _get_visible_text() -> str:
                    return visible_per_member[agent_name]

                def _set_visible_text(value: str) -> None:
                    visible_per_member[agent_name] = value

                _complete_tool(
                    scope_key=_scope_key_for_agent(agent_name),
                    get_visible_text=_get_visible_text,
                    set_visible_text=_set_visible_text,
                    tool=tool,
                )

            try:
                for retried_without_inline_media in (False, True):
                    canonical_per_member = dict.fromkeys(display_names, "")
                    visible_per_member = dict.fromkeys(display_names, "")
                    canonical_consensus = ""
                    visible_consensus = ""
                    tool_trace = []
                    completed_tools = []
                    next_tool_index = 1
                    pending_tools = []
                    emitted_output = False
                    retry_requested = False

                    if run_id_callback is not None and attempt_run_id is not None:
                        run_id_callback(attempt_run_id)

                    raw_stream = await _team_response_stream_raw(
                        team=team,
                        team_members=team_members,
                        prompt=attempt_prompt,
                        metadata=run_metadata,
                        media=attempt_media_inputs,
                        session_id=session_id,
                        run_id=attempt_run_id,
                        user_id=user_id,
                    )
                    async for event in raw_stream:
                        if isinstance(event, (TeamRunOutput, RunOutput)):
                            _cleanup_team_notice_state(
                                run_output=event,
                                scope_context=scope_context,
                                session_id=session_id,
                                entity_name=team_label,
                            )

                            if is_cancelled_run_output(event):
                                partial_text = _extract_interrupted_team_partial_text(event)
                                completed_tool_trace, interrupted_tool_trace = _extract_cancelled_team_tool_trace(event)
                                turn_recorder.record_interrupted(
                                    run_metadata=run_metadata,
                                    assistant_text=partial_text,
                                    completed_tools=completed_tool_trace,
                                    interrupted_tools=interrupted_tool_trace,
                                )
                                _raise_team_run_cancelled(event.content)

                            if is_errored_run_output(event):
                                error_text = str(event.content or "Unknown team error")
                                if (
                                    not retried_without_inline_media
                                    and not (emitted_output or pending_tools or completed_tools)
                                    and should_retry_without_inline_media(error_text, attempt_media_inputs)
                                ):
                                    logger.warning(
                                        "Retrying team streaming without inline media after errored run output",
                                        agents=", ".join(agent_names),
                                        error=error_text,
                                    )
                                    _scrub_team_retry_notice_state(
                                        scope_context=scope_context,
                                        entity_name=team_label,
                                    )
                                    attempt_prompt = append_inline_media_fallback_prompt(prepared_prompt)
                                    attempt_media_inputs = MediaInputs()
                                    attempt_run_id = _next_retry_run_id(run_id)
                                    retry_requested = True
                                    break
                                yield get_user_friendly_error_message(Exception(error_text), team_label)
                                return

                            if reply_to_event_id:
                                _persist_bound_seen_event_ids(
                                    scope_context=scope_context,
                                    session_id=session_id,
                                    event_ids=[reply_to_event_id, *unseen_event_ids],
                                )
                            response_text = _format_terminal_team_response(
                                event,
                                team_display_names=team_members.display_names,
                            )
                            turn_recorder.record_completed(
                                run_metadata=run_metadata,
                                assistant_text=response_text,
                                completed_tools=_extract_completed_team_tool_trace(event),
                            )
                            yield response_text
                            return

                        if isinstance(event, TeamRunErrorEvent):
                            error_text = event.content or "Unknown team error"
                            if (
                                not retried_without_inline_media
                                and not (emitted_output or pending_tools or completed_tools)
                                and should_retry_without_inline_media(error_text, attempt_media_inputs)
                            ):
                                logger.warning(
                                    "Retrying team streaming without inline media after team error",
                                    agents=", ".join(agent_names),
                                    error=error_text,
                                )
                                _scrub_team_retry_notice_state(
                                    scope_context=scope_context,
                                    entity_name=team_label,
                                )
                                attempt_prompt = append_inline_media_fallback_prompt(prepared_prompt)
                                attempt_media_inputs = MediaInputs()
                                attempt_run_id = _next_retry_run_id(run_id)
                                retry_requested = True
                                break
                            yield get_user_friendly_error_message(Exception(error_text), team_label)
                            return

                        if isinstance(event, TeamRunCancelledEvent):
                            interrupted_tool_trace = [pending.trace_entry for pending in pending_tools]
                            partial_text = render_canonical_partial_text()
                            turn_recorder.record_interrupted(
                                run_metadata=run_metadata,
                                assistant_text=partial_text,
                                completed_tools=completed_tools,
                                interrupted_tools=interrupted_tool_trace,
                            )
                            _raise_team_run_cancelled(event.reason)

                        if isinstance(event, AgentRunContentEvent):
                            member_name = event.agent_name
                            if member_name:
                                if member_name not in canonical_per_member:
                                    canonical_per_member[member_name] = ""
                                    visible_per_member[member_name] = ""
                                content = str(event.content or "")
                                canonical_per_member[member_name] += content
                                visible_per_member[member_name] += content
                        elif isinstance(event, AgentToolCallStartedEvent):
                            member_name = event.agent_name
                            if member_name:
                                _start_tool_for_member(member_name, event.tool)
                        elif isinstance(event, AgentToolCallCompletedEvent):
                            member_name = event.agent_name
                            if member_name:
                                _complete_tool_for_member(member_name, event.tool)
                        elif isinstance(event, TeamRunContentEvent):
                            if event.content:
                                content = str(event.content)
                                canonical_consensus += content
                                visible_consensus += content
                            else:
                                logger.debug("Empty team consensus event received")
                        elif isinstance(event, TeamToolCallStartedEvent):
                            _start_tool(
                                scope_key="team",
                                apply_visible_text=_append_to_visible_consensus,
                                tool=event.tool,
                            )
                        elif isinstance(event, TeamToolCallCompletedEvent):
                            _complete_tool(
                                scope_key="team",
                                get_visible_text=_get_visible_consensus,
                                set_visible_text=_set_visible_consensus,
                                tool=event.tool,
                            )
                        else:
                            logger.debug("ignoring_team_stream_event_type", event_type=type(event).__name__)
                            continue

                        _sync_live_turn_recorder()
                        parts = _render_team_parts(
                            per_member=visible_per_member,
                            consensus=visible_consensus,
                        )
                        if parts:
                            emitted_output = True
                            header = _format_team_header(team_members.display_names)
                            full_text = "\n\n".join(parts)
                            chunk_tool_trace = tool_trace.copy() if show_tool_calls and tool_trace else None
                            yield StructuredStreamChunk(content=header + full_text, tool_trace=chunk_tool_trace)

                    if retry_requested:
                        continue
                    if emitted_output and reply_to_event_id:
                        _persist_bound_seen_event_ids(
                            scope_context=scope_context,
                            session_id=session_id,
                            event_ids=[reply_to_event_id, *unseen_event_ids],
                        )
                    return
            finally:
                _cleanup_team_notice_state(
                    run_output=None,
                    scope_context=scope_context,
                    session_id=session_id,
                    entity_name=team_label,
                )
    except asyncio.CancelledError:
        turn_recorder.record_interrupted(
            run_metadata=build_matrix_run_metadata(
                reply_to_event_id,
                unseen_event_ids,
                extra_metadata=matrix_run_metadata,
            ),
            assistant_text=render_canonical_partial_text(),
            completed_tools=completed_tools,
            interrupted_tools=[pending.trace_entry for pending in pending_tools],
        )
        raise
    except Exception as e:
        logger.exception("Error preparing team members for streaming", agents=agent_names)
        yield get_user_friendly_error_message(e, team_label)
        return
    finally:
        close_team_runtime_sqlite_dbs(
            agents=team_members.agents,
            team_db=cast("SqliteDb | None", team.db) if team is not None else None,
            shared_scope_storage=scope_context.storage if scope_context is not None else None,
        )


__all__ = [
    "TeamIntent",
    "TeamMemberStatus",
    "TeamMode",
    "TeamOutcome",
    "TeamResolution",
    "TeamResolutionMember",
    "build_materialized_team_instance",
    "decide_team_formation",
    "format_team_response",
    "is_cancelled_run_output",
    "is_errored_run_output",
    "materialize_exact_team_members",
    "resolve_configured_team",
    "resolve_live_shared_agent_names",
    "select_model_for_team",
    "team_response",
    "team_response_stream",
]
