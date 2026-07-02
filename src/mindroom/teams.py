"""Team-based collaboration for multiple agents."""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal, cast

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
from mindroom.agent_run_context import append_knowledge_availability_enrichment
from mindroom.agent_storage import get_team_session
from mindroom.agents import create_agent, enable_all_history_replay
from mindroom.ai import resolve_run_correlation_id
from mindroom.ai_run_metadata import build_ai_run_metadata_content, build_prepared_history_metadata_content
from mindroom.authorization import get_available_responders_in_room
from mindroom.constants import MATRIX_SEEN_EVENT_IDS_METADATA_KEY, ROUTER_AGENT_NAME
from mindroom.entity_resolution import entity_identity_registry
from mindroom.error_handling import get_user_friendly_error_message
from mindroom.execution_preparation import (
    ThreadHistoryRenderLimits,
    prepare_bound_team_run_context,
    render_prepared_messages_text,
    render_prepared_team_messages_text,
)
from mindroom.history import (
    ScopeSessionContext,
    close_team_runtime_state_dbs,
    note_prepared_history_timing,
    open_bound_scope_session_context,
    resolve_bound_team_scope_context,
    team_tool_definition_payloads_for_logging,
    update_scope_seen_event_ids,
)
from mindroom.history.interrupted_replay import (
    persist_interrupted_replay,
    split_interrupted_tool_trace,
    tool_execution_call_id,
)
from mindroom.hooks import EnrichmentItem, render_system_enrichment_block
from mindroom.knowledge import KnowledgeAvailabilityDetail, resolve_agent_knowledge_access
from mindroom.llm_request_logging import (
    bind_llm_request_log_context,
    build_llm_request_log_context,
    model_params_payload,
    stream_with_llm_request_log_context,
)
from mindroom.logging_config import get_logger
from mindroom.media_fallback import (
    append_inline_media_fallback_prompt,
    build_model_media_route,
    filter_media_inputs_for_route,
    retry_media_inputs_after_failure,
)
from mindroom.media_inputs import MediaInputs
from mindroom.metadata_merge import deep_merge_metadata
from mindroom.response_turn import (
    AttemptResolved,
    BlockingTurnAdapter,
    CancelledAttempt,
    CompletedAttempt,
    ContinuationCapability,
    DynamicContinuationRunState,
    EmptyRunCapability,
    ErroredAttempt,
    HandledAttempt,
    ResponseTurnContext,
    StreamingTurnAdapter,
    TurnPartialSnapshot,
    TurnSinks,
    build_matrix_run_metadata,
    run_blocking_response_turn,
    stream_response_turn,
)
from mindroom.team_exact_members import (
    ResolvedExactTeamMembers,
    materialize_exact_requested_team_members,
    resolve_live_shared_agent_names,
    resolve_team_materializable_agent_names,
)
from mindroom.timing import emit_timing_event
from mindroom.tool_system.events import (
    StreamingToolTracker,
    StructuredStreamChunk,
    ToolTraceEntry,
    complete_pending_tool_block,
    format_tool_completed_event,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Callable, Collection, Sequence

    import nio
    from agno.db.base import BaseDb
    from agno.models.response import ToolExecution

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.history import CompactionLifecycle, CompactionOutcome, PreparedHistoryState
    from mindroom.history.turn_recorder import TurnRecorder
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.matrix.identity import MatrixID
    from mindroom.response_turn import EmptyRunDiscard, StandaloneReplaySnapshot, TurnRunState
    from mindroom.runtime_protocols import OrchestratorRuntime
    from mindroom.timing import DispatchPipelineTiming
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


logger = get_logger(__name__)


def _team_run_input_text(run_input: str | list[Message]) -> str:
    if isinstance(run_input, str):
        return run_input
    return render_prepared_messages_text(run_input)


def _team_request_log_context(
    *,
    team_name: str,
    session_id: str | None,
    room_id: str | None,
    thread_id: str | None,
    reply_to_event_id: str | None,
    requester_id: str | None,
    correlation_id: str,
    prompt: str,
    run_input: str | list[Message],
    metadata: dict[str, object] | None,
) -> dict[str, object]:
    return build_llm_request_log_context(
        agent_id=team_name,
        session_id=session_id or "",
        room_id=room_id,
        thread_id=thread_id,
        reply_to_event_id=reply_to_event_id,
        requester_id=requester_id,
        correlation_id=correlation_id,
        prompt=prompt,
        model_prompt=None,
        full_prompt=_team_run_input_text(run_input),
        metadata=metadata,
    )


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


def _append_additional_context(entity: Agent | Team, context_chunk: str) -> None:
    existing_context = entity.additional_context.strip() if entity.additional_context else ""
    entity.additional_context = f"{existing_context}\n\n{context_chunk}" if existing_context else context_chunk


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
    prepared_history: PreparedHistoryState
    # Configured model name resolved at preparation time. Run metadata must use
    # this snapshot instead of re-resolving, because the per-thread override
    # store can change mid-run (for example via the thread_model tool).
    runtime_model_name: str

    @property
    def prepared_prompt(self) -> str:
        """Return the prompt-visible text derived from canonical live messages."""
        return render_prepared_team_messages_text(self.messages)

    @property
    def context_messages(self) -> tuple[Message, ...]:
        """Return replayed context messages without the current user turn."""
        return self.messages[:-1]


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


def _has_visible_team_output(response: TeamRunOutput | RunOutput) -> bool:
    """Return whether one terminal team output carries real member or consensus text.

    The "No team response generated." placeholder does not count, so the
    dynamic-tool continuation limit message can replace it.
    """
    return bool(format_team_response(response)) or bool(_get_response_content(response))


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
    ai_runtime.scrub_queued_notice_session_context(
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
    prompt = config.render_prompt(
        "TEAM_MODE_SELECTION_PROMPT_TEMPLATE",
        message=message,
        agent_names=", ".join(agent_names),
    )

    try:
        model = model_loading.get_model_instance(config, runtime_paths, "default")
        agent = Agent(
            name="TeamModeDecider",
            role="Determine team mode",
            model=model,
            output_schema=_TeamModeDecision,
            telemetry=False,
        )
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


def decide_team_formation(
    tagged_agents: list[MatrixID],
    agents_in_thread: list[MatrixID],
    all_mentioned_in_thread: list[MatrixID],
    room: nio.MatrixRoom | None,
    runtime_paths: RuntimePaths,
    config: Config | None = None,
    is_dm_room: bool = False,
    is_thread: bool = False,
    available_responders_in_room: list[MatrixID] | None = None,
    materializable_agent_names: set[str] | None = None,
    allow_explicit_private_agents: bool = False,
) -> TeamResolution:
    """Determine if a team should form, purely from mention, thread, and room context.

    Team resolutions carry a heuristic provisional mode: COORDINATE when agents
    are explicitly tagged (they likely have different roles), COLLABORATE when
    agents come from thread history (likely discussing the same topic). The
    execution layer may refine it with ``select_ad_hoc_team_mode``.

    Args:
        tagged_agents: Raw agents explicitly mentioned in the current message
        agents_in_thread: Agents that have participated in the thread
        all_mentioned_in_thread: All agents ever mentioned in the thread
        room: The Matrix room object when room-membership visibility is available
        runtime_paths: Explicit runtime context for permissions and identity resolution
        config: Application configuration
        is_dm_room: Whether this is a DM room
        is_thread: Whether the current message is in a thread
        available_responders_in_room: Optional pre-filtered room responders for sender-visible availability
        materializable_agent_names: Optional live agent names that can currently produce a response
        allow_explicit_private_agents: Whether explicitly tagged requester-private agents may join

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
        available_responders_in_room=available_responders_in_room,
    )
    if team_request.intent is None or not team_request.requested_members:
        return TeamResolution.none()

    allow_direct_private_agents = allow_explicit_private_agents and team_request.intent is TeamIntent.EXPLICIT_MEMBERS
    materializable_agent_names = (
        resolve_team_materializable_agent_names(
            config,
            materializable_agent_names,
            allow_direct_private_agents=allow_direct_private_agents,
        )
        if config is not None
        else materializable_agent_names
    )
    member_statuses = _evaluate_team_members(
        team_request.requested_members,
        config,
        runtime_paths,
        room=room,
        sender_visible_responders=available_responders_in_room,
        materializable_agent_names=materializable_agent_names,
        allow_direct_private_agents=allow_direct_private_agents,
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

    mode = TeamMode.COORDINATE if len(tagged_agents) > 1 else TeamMode.COLLABORATE
    return replace(resolution, mode=mode)


async def select_ad_hoc_team_mode(
    message: str,
    team_agents: list[MatrixID],
    config: Config,
    runtime_paths: RuntimePaths,
) -> TeamMode:
    """Select the collaboration mode for one ad-hoc team at execution time.

    Team formation itself is pure; this AI refinement must run only where the
    team response actually executes.
    """
    agent_names = [_team_member_name(agent_id, config, runtime_paths) for agent_id in team_agents]
    return await _select_team_mode(message, agent_names, config, runtime_paths)


def _team_member_name(
    agent_id: MatrixID,
    config: Config | None,
    runtime_paths: RuntimePaths,
) -> str:
    """Return the canonical agent name used throughout team resolution."""
    if config is None:
        return agent_id.username
    return (
        entity_identity_registry(config, runtime_paths).current_entity_name_for_user_id(
            agent_id.full_id,
            include_router=True,
        )
        or agent_id.username
    )


def _filter_team_request_members(
    agent_ids: list[MatrixID],
    config: Config | None,
    runtime_paths: RuntimePaths,
) -> list[MatrixID]:
    """Keep only actual teamable agents while preserving the requested order."""
    filtered: list[MatrixID] = []
    for agent_id in agent_ids:
        if config is not None:
            agent_name = entity_identity_registry(config, runtime_paths).current_entity_name_for_user_id(
                agent_id.full_id,
                include_router=False,
            )
            if agent_name is None or agent_name not in config.agents:
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
    available_responders_in_room: list[MatrixID] | None,
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

    available_responders = available_responders_in_room
    if available_responders is None:
        available_responders = get_available_responders_in_room(room, config, runtime_paths)
    normalized_available_responders = _normalize_team_request_members(available_responders, config, runtime_paths)
    if len(normalized_available_responders) <= 1:
        return _SelectedTeamRequest(None, [])

    logger.info(
        "team_formation_requested",
        trigger="dm_room_multiple_agents",
        agents=[responder.full_id for responder in normalized_available_responders],
    )
    return _SelectedTeamRequest(TeamIntent.DM_AUTO_TEAM, normalized_available_responders)


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
    sender_visible_responders: list[MatrixID] | None,
    materializable_agent_names: set[str] | None,
    allow_direct_private_agents: bool,
) -> list[TeamResolutionMember]:
    """Evaluate one status and response capability for each requested member."""
    room_visible_ids: set[str] | None = None
    if sender_visible_responders is None and room is not None and config is not None:
        room_visible_ids = {
            agent_id.full_id
            for agent_id in _normalize_team_request_members(
                get_available_responders_in_room(room, config, runtime_paths),
                config,
                runtime_paths,
            )
        }
    sender_visible_ids: set[str] | None = None
    if sender_visible_responders is not None:
        sender_visible_ids = {
            agent_id.full_id
            for agent_id in _normalize_team_request_members(sender_visible_responders, config, runtime_paths)
        }

    unsupported_agents: dict[str, tuple[str, ...] | None] = {}
    if config is not None:
        unsupported_agents = config.get_unsupported_team_agents(
            [_team_member_name(agent_id, config, runtime_paths) for agent_id in requested_members],
            allow_direct_private_agents=allow_direct_private_agents,
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
        return (
            f"agent '{member.name}' is private and can only join explicit Matrix ad hoc teams with requester identity"
        )
    if len(private_targets) != 1:
        return (
            f"agent '{member.name}' reaches private agents "
            f"{', '.join(repr(target) for target in private_targets)} via delegation "
            "and private delegation is not supported for teams"
        )
    return (
        f"agent '{member.name}' reaches private agent '{private_targets[0]}' via delegation "
        "and private delegation is not supported for teams"
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
        sender_visible_responders=None,
        materializable_agent_names=materializable_agent_names,
        allow_direct_private_agents=False,
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


def _collect_team_tool_executions(response: TeamRunOutput | RunOutput) -> list[ToolExecution]:
    """Collect raw tool executions from a possibly nested team response."""
    tools: list[ToolExecution] = list(response.tools or [])
    if isinstance(response, TeamRunOutput):
        for member_response in response.member_responses:
            if isinstance(member_response, TeamRunOutput | RunOutput):
                tools.extend(_collect_team_tool_executions(member_response))
    return tools


def _build_team_run_metadata_content(
    *,
    config: Config,
    prepared_execution: _PreparedMaterializedTeamExecution,
    response: TeamRunOutput | RunOutput,
    session_id: str | None,
    tool_count: int,
) -> dict[str, Any]:
    """Build the versioned run/model/token metadata payload for one team run."""
    return build_ai_run_metadata_content(
        config=config,
        model_name=prepared_execution.runtime_model_name,
        run_id=response.run_id,
        session_id=response.session_id or session_id,
        status=response.status,
        model=response.model,
        model_provider=response.model_provider,
        metrics=response.metrics,
        context_input_tokens=prepared_execution.prepared_history.prepared_context_tokens,
        tool_count=tool_count,
        prepared_history=prepared_execution.prepared_history,
    )


def _is_cancellation_boilerplate(content: str) -> bool:
    """Return whether one string is just Agno cancellation boilerplate."""
    normalized = content.strip().lower()
    return normalized.startswith("run ") and "cancel" in normalized


def _empty_partial_text() -> str:
    return ""


def _resolved_team_session_id(scope_context: ScopeSessionContext | None, session_id: str | None) -> str | None:
    """Return the effective team session id, falling back to the opened scope session."""
    if session_id is not None:
        return session_id
    if scope_context is not None and scope_context.session is not None:
        return scope_context.session.session_id
    return None


def _build_team_turn_capabilities(
    *,
    message: str,
    session_id: str | None,
    entity_name: str,
) -> tuple[EmptyRunCapability, Callable[[ScopeSessionContext | None, StandaloneReplaySnapshot], None]]:
    """Build the empty-run guard and standalone-replay persister for one team turn."""

    def _discard_empty_team_run(scope_context: ScopeSessionContext | None, discard: EmptyRunDiscard) -> None:
        resolved_session_id = _resolved_team_session_id(scope_context, discard.session_id or session_id)
        if resolved_session_id is None:
            return
        ai_runtime.discard_empty_completed_run(
            scope_context=scope_context,
            session_id=resolved_session_id,
            run_id=discard.run_id,
            session_type=SessionType.TEAM,
            entity_name=entity_name,
            output_tokens=discard.output_tokens,
        )

    def _persist_team_standalone_replay(
        scope_context: ScopeSessionContext | None,
        snapshot: StandaloneReplaySnapshot,
    ) -> None:
        resolved_session_id = _resolved_team_session_id(scope_context, snapshot.session_id or session_id)
        if resolved_session_id is None:
            return
        persist_interrupted_replay(
            scope_context=scope_context,
            session_id=resolved_session_id,
            run_id=snapshot.run_id,
            user_message=message,
            partial_text=snapshot.partial_text,
            completed_tools=snapshot.completed_tools,
            interrupted_tools=snapshot.interrupted_tools,
            run_metadata=snapshot.run_metadata,
            is_team=True,
        )

    return (
        EmptyRunCapability(notice_text=ai_runtime.EMPTY_RESPONSE_NOTICE, discard=_discard_empty_team_run),
        _persist_team_standalone_replay,
    )


@dataclass
class _TeamTurnHolder:
    """Live per-turn team state shared between attempt closures and adapter callbacks."""

    team: Team | None = None
    team_members: ResolvedExactTeamMembers | None = None
    last_response: RunOutput | TeamRunOutput | None = None
    attempt_started: bool = False
    attempt_run_id: str | None = None
    render_partial: Callable[[], str] = _empty_partial_text
    tool_tracker: StreamingToolTracker = field(default_factory=StreamingToolTracker)


def _build_team_runtime_db_callbacks(
    holder: _TeamTurnHolder,
) -> tuple[Callable[[ScopeSessionContext | None], None], Callable[[ScopeSessionContext | None], None]]:
    """Build the release/close runtime-DB callbacks for one team turn.

    Releasing between continuation attempts closes the spent member agents'
    runtime DBs and drops them so the next attempt rebuilds members with the
    updated dynamic-tool schema baked in.
    """

    def _release_attempt_members(scope_context: ScopeSessionContext | None) -> None:
        members = holder.team_members
        if members is None:
            return
        close_team_runtime_state_dbs(
            agents=members.agents,
            team_db=cast("BaseDb | None", holder.team.db) if holder.team is not None else None,
            shared_scope_storage=scope_context.storage if scope_context is not None else None,
        )
        holder.team_members = None
        holder.team = None

    def _close_runtime_dbs(scope_context: ScopeSessionContext | None) -> None:
        members = holder.team_members
        close_team_runtime_state_dbs(
            agents=members.agents if members is not None else [],
            team_db=cast("BaseDb | None", holder.team.db) if holder.team is not None else None,
            shared_scope_storage=scope_context.storage if scope_context is not None else None,
        )

    return _release_attempt_members, _close_runtime_dbs


def materialize_exact_team_members(
    requested_agent_names: list[str],
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
    session_id: str | None = None,
    include_openai_compat_guidance: bool = False,
    materializable_agent_names: set[str] | None = None,
    unavailable_bases: dict[str, KnowledgeAvailabilityDetail] | None = None,
    refresh_scheduler: KnowledgeRefreshScheduler | None = None,
    reason_prefix: str = "Team request",
    dynamic_tool_continuation: bool = False,
) -> ResolvedExactTeamMembers:
    """Materialize the exact team-member set without silent fallback.

    ``dynamic_tool_continuation`` must only be enabled by callers that run the
    members under a continuation-capable response-turn driver; other callers
    (for example the OpenAI-compatible API) leave it off so a load/unload does
    not truncate the run.
    """
    if not requested_agent_names:
        raise ValueError(_NO_AGENTS_RESPONSE)

    def _build_member(agent_name: str) -> Agent:
        knowledge_resolution = resolve_agent_knowledge_access(
            agent_name,
            config,
            runtime_paths,
            refresh_scheduler=refresh_scheduler,
            execution_identity=execution_identity,
        )
        if unavailable_bases is not None:
            unavailable_bases.update(knowledge_resolution.unavailable)
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
            knowledge=knowledge_resolution.knowledge,
            include_interactive_questions=False,
            include_openai_compat_guidance=include_openai_compat_guidance,
            refresh_scheduler=refresh_scheduler,
            dynamic_tool_continuation=dynamic_tool_continuation,
        )

    team_members = materialize_exact_requested_team_members(
        requested_agent_names,
        materializable_agent_names=materializable_agent_names,
        build_member=_build_member,
    )
    if team_members.failed_agent_names:
        close_team_runtime_state_dbs(
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
    orchestrator: OrchestratorRuntime,
    execution_identity: ToolExecutionIdentity | None,
    *,
    session_id: str | None = None,
    configured_team_name: str | None = None,
    unavailable_bases: dict[str, KnowledgeAvailabilityDetail] | None = None,
    reason_prefix: str = "Team request",
) -> ResolvedExactTeamMembers:
    """Materialize the exact requested team-member set without silent fallback."""
    requested_agent_names = _requested_team_agent_names(agent_names)
    assert orchestrator.config is not None
    materializable_agent_names = resolve_team_materializable_agent_names(
        orchestrator.config,
        resolve_live_shared_agent_names(orchestrator),
        allow_direct_private_agents=_allow_direct_private_team_agents(
            execution_identity,
            configured_team_name=configured_team_name,
        ),
    )

    return materialize_exact_team_members(
        requested_agent_names,
        config=orchestrator.config,
        runtime_paths=orchestrator.runtime_paths,
        execution_identity=execution_identity,
        session_id=session_id,
        materializable_agent_names=materializable_agent_names,
        unavailable_bases=unavailable_bases,
        refresh_scheduler=orchestrator.knowledge_refresh_scheduler,
        dynamic_tool_continuation=True,
        reason_prefix=reason_prefix,
    )


def _allow_direct_private_team_agents(
    execution_identity: ToolExecutionIdentity | None,
    *,
    configured_team_name: str | None,
) -> bool:
    """Return whether direct private members may join this team request."""
    return (
        configured_team_name is None
        and execution_identity is not None
        and execution_identity.channel == "matrix"
        and bool(execution_identity.requester_id)
    )


def _resolve_team_instance_id(
    *,
    agents: list[Agent],
    config: Config,
    team_display_name: str,
    configured_team_name: str | None,
    scope_context: ScopeSessionContext | None,
    execution_identity: ToolExecutionIdentity | None,
) -> str:
    """Return the Team.id for scoped and no-session team runs."""
    if scope_context is not None:
        return scope_context.scope.scope_id
    bound_scope = resolve_bound_team_scope_context(
        agents=agents,
        config=config,
        team_name=configured_team_name,
        execution_identity=execution_identity,
    )
    if bound_scope is not None:
        return bound_scope.scope.scope_id
    return team_display_name


def _create_team_instance(
    *,
    agents: list[Agent],
    mode: TeamMode,
    config: Config,
    runtime_paths: RuntimePaths,
    team_display_name: str,
    scope_context: ScopeSessionContext | None,
    execution_identity: ToolExecutionIdentity | None,
    model_name: str | None = None,
    configured_team_name: str | None = None,
) -> Team:
    """Create a configured Team instance.

    Args:
        agents: List of Agent instances for the team
        mode: Team collaboration mode
        config: Active runtime configuration
        runtime_paths: Active runtime paths
        team_display_name: Human-readable Team name passed to Agno
        scope_context: Already-open team history scope, when persisted history
            is available for this request.
        execution_identity: Request execution identity used for provider-specific
            model behavior such as codex prompt-cache keying
        model_name: Optional model name override
        configured_team_name: Optional configured team id for stable team-scope history

    Returns:
        Configured Team instance

    """
    model = model_loading.get_model_instance(
        config,
        runtime_paths,
        model_name or "default",
        execution_identity=execution_identity,
    )
    # Coordinate-mode tool calls run through the shared team model in v1.
    # Member-agent models are intentionally not wrapped here.
    ai_runtime.install_queued_message_notice_hook(
        model,
        notice_text=config.get_prompt("QUEUED_MESSAGE_NOTICE_TEXT"),
    )
    history_settings = config.resolve_entity(
        configured_team_name if configured_team_name is not None and configured_team_name in config.teams else None,
    ).history_settings
    team_id = _resolve_team_instance_id(
        agents=agents,
        config=config,
        team_display_name=team_display_name,
        configured_team_name=configured_team_name,
        scope_context=scope_context,
        execution_identity=execution_identity,
    )

    for agent in agents:
        # Team-owned replay should come from the shared TeamSession, not from
        # each member independently replaying their own session state.
        agent.add_history_to_context = False
        agent.add_session_summary_to_context = False

    team_members: list[Agent | Team] = [*agents]
    team = Team(
        members=team_members,
        id=team_id,
        name=team_display_name,
        model=model,
        db=scope_context.storage if scope_context is not None else None,
        delegate_to_all_members=mode == TeamMode.COLLABORATE,
        add_history_to_context=True,
        add_session_summary_to_context=True,
        num_history_runs=history_settings.policy.num_history_runs,
        num_history_messages=history_settings.policy.num_history_messages,
        max_tool_calls_from_history=history_settings.max_tool_calls_from_history,
        store_history_messages=False,
        show_members_responses=True,
        debug_mode=False,
        telemetry=False,
        # Agno will automatically list members with their names, roles, and tools
    )
    if history_settings.policy.mode == "all":
        enable_all_history_replay(team)
    return team


def select_model_for_team(
    team_name: str,
    room_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    thread_id: str | None = None,
) -> str:
    """Get the appropriate model for a team in a specific room.

    Priority:
    1. Thread-specific model override
    2. Room-specific model from room_models
    3. Team's configured model
    4. Global default model

    Args:
        team_name: Name of the team
        room_id: Matrix room ID
        config: Application configuration
        runtime_paths: Explicit runtime context for room alias resolution
        thread_id: Optional resolved Matrix thread root for thread model overrides

    Returns:
        Model name to use

    """
    model_name = config.resolve_runtime_model(
        entity_name=team_name,
        room_id=room_id,
        thread_id=thread_id,
        runtime_paths=runtime_paths,
    ).model_name
    logger.info("selected_team_model", team_name=team_name, model_name=model_name)
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
    execution_identity: ToolExecutionIdentity | None,
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
        scope_context=scope_context,
        execution_identity=execution_identity,
        model_name=resolved_team_model_name,
        configured_team_name=configured_team_name,
    )


async def prepare_materialized_team_execution(
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
    room_id: str | None,
    thread_id: str | None,
    requester_id: str | None,
    correlation_id: str | None,
    compaction_outcomes_collector: list[CompactionOutcome] | None,
    configured_team_name: str | None,
    current_timestamp_ms: float | None = None,
    current_prompt_is_structured: bool = False,
    compaction_lifecycle: CompactionLifecycle | None = None,
    thread_history_render_limits: ThreadHistoryRenderLimits | None = None,
    matrix_run_metadata: dict[str, Any] | None = None,
    pipeline_timing: DispatchPipelineTiming | None = None,
    system_enrichment_items: Sequence[EnrichmentItem] = (),
) -> _PreparedMaterializedTeamExecution:
    """Prepare one materialized team for execution."""
    if system_enrichment_items:
        rendered_system_context = render_system_enrichment_block(system_enrichment_items)
        _append_additional_context(team, rendered_system_context)
        for agent in agents:
            _append_additional_context(agent, rendered_system_context)
    runtime_model = config.resolve_runtime_model(
        entity_name=configured_team_name,
        active_model_name=active_model_name,
        room_id=room_id,
        thread_id=thread_id,
        runtime_paths=runtime_paths,
    )
    prepared_execution = await prepare_bound_team_run_context(
        scope_context=scope_context,
        agents=agents,
        team=team,
        prompt=message,
        thread_history=thread_history,
        runtime_paths=runtime_paths,
        config=config,
        entity_name=configured_team_name,
        active_model_name=active_model_name,
        active_context_window=runtime_model.context_window,
        room_id=room_id,
        reply_to_event_id=reply_to_event_id,
        active_event_ids=active_event_ids,
        response_sender_id=response_sender_id,
        current_sender_id=current_sender_id,
        current_timestamp_ms=current_timestamp_ms,
        current_prompt_is_structured=current_prompt_is_structured,
        compaction_outcomes_collector=compaction_outcomes_collector,
        compaction_lifecycle=compaction_lifecycle,
        thread_history_render_limits=thread_history_render_limits,
        pipeline_timing=pipeline_timing,
    )
    prepared_history = prepared_execution.prepared_history
    if pipeline_timing is not None:
        pipeline_timing.mark("history_ready")
        note_prepared_history_timing(pipeline_timing, prepared_history)
    run_extra_content = build_prepared_history_metadata_content(prepared_history)
    run_metadata = build_matrix_run_metadata(
        reply_to_event_id,
        prepared_execution.unseen_event_ids,
        room_id=room_id,
        thread_id=thread_id,
        requester_id=requester_id,
        correlation_id=correlation_id,
        tools_schema=team_tool_definition_payloads_for_logging(team),
        model_params=model_params_payload(team.model) if team.model is not None else {},
        extra_metadata=deep_merge_metadata(matrix_run_metadata, run_extra_content),
    )
    return _PreparedMaterializedTeamExecution(
        messages=prepared_execution.messages,
        run_metadata=run_metadata,
        unseen_event_ids=prepared_execution.unseen_event_ids,
        prepared_history=prepared_history,
        runtime_model_name=runtime_model.model_name,
    )


async def team_response(  # noqa: C901, PLR0915
    agent_names: list[str],
    mode: TeamMode,
    message: str,
    orchestrator: OrchestratorRuntime,
    execution_identity: ToolExecutionIdentity | None,
    thread_history: Sequence[ResolvedVisibleMessage] | None = None,
    model_name: str | None = None,
    media: MediaInputs | None = None,
    session_id: str | None = None,
    run_id: str | None = None,
    run_id_callback: Callable[[str], None] | None = None,
    user_id: str | None = None,
    reply_to_event_id: str | None = None,
    current_timestamp_ms: float | None = None,
    current_prompt_is_structured: bool = False,
    correlation_id: str | None = None,
    active_event_ids: Collection[str] = frozenset(),
    response_sender_id: str | None = None,
    compaction_outcomes_collector: list[CompactionOutcome] | None = None,
    compaction_lifecycle: CompactionLifecycle | None = None,
    run_metadata_collector: dict[str, Any] | None = None,
    configured_team_name: str | None = None,
    matrix_run_metadata: dict[str, Any] | None = None,
    system_enrichment_items: Sequence[EnrichmentItem] = (),
    pipeline_timing: DispatchPipelineTiming | None = None,
    *,
    turn_recorder: TurnRecorder | None = None,
    reason_prefix: str = "Team request",
) -> str:
    """Create a team and execute response."""
    assert orchestrator.config is not None
    requested_agent_names = _requested_team_agent_names(agent_names)
    allow_direct_private_agents = _allow_direct_private_team_agents(
        execution_identity,
        configured_team_name=configured_team_name,
    )
    orchestrator.config.assert_team_agents_supported(
        requested_agent_names,
        allow_direct_private_agents=allow_direct_private_agents,
    )
    unavailable_bases: dict[str, KnowledgeAvailabilityDetail] = {}
    room_id = execution_identity.room_id if execution_identity is not None else None
    thread_id = (
        execution_identity.resolved_thread_id or execution_identity.thread_id
        if execution_identity is not None
        else None
    )
    requester_id = user_id or (execution_identity.requester_id if execution_identity is not None else None)
    correlation_id = resolve_run_correlation_id(
        correlation_id,
        reply_to_event_id=reply_to_event_id,
        matrix_run_metadata=matrix_run_metadata,
    )
    try:
        # Member agent builds walk the filesystem (workspace scaffolding,
        # context files, session storage); keep them off the event loop (#1260).
        team_members = await asyncio.to_thread(
            _materialize_team_members,
            agent_names,
            orchestrator,
            execution_identity,
            session_id=session_id,
            unavailable_bases=unavailable_bases,
            reason_prefix=reason_prefix,
            configured_team_name=configured_team_name,
        )
    except ValueError as exc:
        return str(exc)
    system_enrichment_items = append_knowledge_availability_enrichment(
        system_enrichment_items,
        unavailable_bases,
    )
    agents = team_members.agents

    agent_list = ", ".join(str(a.name) for a in agents if a.name)
    team_name = f"Team ({agent_list})"
    base_media_inputs = media or MediaInputs()
    config = orchestrator.config
    enrichment_items = system_enrichment_items
    holder = _TeamTurnHolder(team_members=team_members)

    async def _run_team_attempt(  # noqa: C901, PLR0912, PLR0915
        run: TurnRunState,
        continuation_state: DynamicContinuationRunState,
    ) -> CompletedAttempt | CancelledAttempt | ErroredAttempt:
        """Run one team attempt, including its media-fallback retries."""
        attempt_members = holder.team_members
        if attempt_members is None:
            # A continuation released the spent members; rebuild them so the
            # loaded dynamic-tool schema is baked into fresh member agents.
            attempt_members = await asyncio.to_thread(
                _materialize_team_members,
                agent_names,
                orchestrator,
                execution_identity,
                session_id=session_id,
                reason_prefix=reason_prefix,
                configured_team_name=configured_team_name,
            )
            holder.team_members = attempt_members
        attempt_agents = attempt_members.agents
        team = build_materialized_team_instance(
            requested_agent_names=attempt_members.requested_agent_names,
            agents=attempt_agents,
            mode=mode,
            config=config,
            runtime_paths=orchestrator.runtime_paths,
            scope_context=run.scope_context,
            model_name=model_name,
            configured_team_name=configured_team_name,
            execution_identity=execution_identity,
        )
        holder.team = team
        prepared_execution = await prepare_materialized_team_execution(
            scope_context=run.scope_context,
            agents=attempt_agents,
            team=team,
            message=continuation_state.active_prompt,
            thread_history=thread_history,
            config=config,
            runtime_paths=orchestrator.runtime_paths,
            active_model_name=model_name,
            reply_to_event_id=reply_to_event_id,
            active_event_ids=active_event_ids,
            response_sender_id=response_sender_id,
            current_sender_id=user_id,
            room_id=room_id,
            thread_id=thread_id,
            requester_id=requester_id,
            correlation_id=correlation_id,
            compaction_outcomes_collector=compaction_outcomes_collector,
            current_timestamp_ms=continuation_state.active_current_timestamp_ms,
            current_prompt_is_structured=continuation_state.active_current_prompt_is_structured,
            compaction_lifecycle=compaction_lifecycle,
            configured_team_name=configured_team_name,
            thread_history_render_limits=_MATRIX_TEAM_THREAD_HISTORY_RENDER_LIMITS,
            matrix_run_metadata=matrix_run_metadata,
            pipeline_timing=pipeline_timing,
            system_enrichment_items=enrichment_items,
        )
        prompt = prepared_execution.prepared_prompt
        run.unseen_event_ids = prepared_execution.unseen_event_ids
        run.run_metadata = prepared_execution.run_metadata
        run_metadata = run.run_metadata
        if turn_recorder is not None:
            turn_recorder.set_run_metadata(run_metadata)
        # Team runs flatten context messages to text, so media pinned to
        # thread-history messages is re-collected onto the current turn.
        context_media_inputs = ai_runtime.media_inputs_from_run_input(prepared_execution.messages)
        media_inputs = context_media_inputs.merge(base_media_inputs)
        logger.info("executing_team_response", agent_count=len(agents), mode=mode.value)
        logger.info("team_prompt_preview", agents=agent_list, prompt_preview=prompt[:500])

        async def _run(
            current_prompt: str,
            current_media_inputs: MediaInputs,
            current_run_id: str | None,
        ) -> object:
            ai_runtime.note_attempt_run_id(run_id_callback, current_run_id)
            prepared_input = (
                ai_runtime.attach_media_to_run_input(current_prompt, current_media_inputs)
                if current_media_inputs.has_any()
                else current_prompt
            )
            with bind_llm_request_log_context(
                **_team_request_log_context(
                    team_name=configured_team_name or team_name,
                    session_id=session_id,
                    room_id=room_id,
                    thread_id=thread_id,
                    reply_to_event_id=reply_to_event_id,
                    requester_id=requester_id,
                    correlation_id=correlation_id,
                    prompt=message,
                    run_input=prepared_input,
                    metadata=run_metadata,
                ),
            ):
                return await team.arun(
                    prepared_input,
                    session_id=session_id,
                    run_id=current_run_id,
                    user_id=user_id,
                    metadata=run_metadata,
                )

        response: object | None = None
        attempt_run_id = continuation_state.active_run_id
        holder.attempt_run_id = attempt_run_id
        inline_media_fallback_prompt = config.get_prompt("INLINE_MEDIA_FALLBACK_PROMPT")
        media_route = build_model_media_route(team.model) if media_inputs.has_any() else None
        media_filter = filter_media_inputs_for_route(media_route, media_inputs)
        attempt_prompt = (
            append_inline_media_fallback_prompt(
                prompt,
                fallback_prompt=inline_media_fallback_prompt,
            )
            if media_filter.removed_kinds
            else prompt
        )
        attempt_media_inputs = media_filter.media_inputs
        holder.attempt_started = True
        for retried_after_media_fallback in (False, True):
            response = None
            holder.last_response = None
            try:
                response = await _run(attempt_prompt, attempt_media_inputs, attempt_run_id)
            except Exception as e:
                retry_decision = retry_media_inputs_after_failure(
                    media_route,
                    e,
                    attempt_media_inputs,
                )
                if not retried_after_media_fallback and retry_decision.should_retry:
                    logger.warning(
                        "Retrying team response after inline media validation error",
                        agents=agent_list,
                        error=str(e),
                        removed_media_kinds=sorted(retry_decision.removed_kinds),
                    )
                    _scrub_team_retry_notice_state(
                        scope_context=run.scope_context,
                        entity_name=configured_team_name or team_name,
                    )
                    attempt_prompt = append_inline_media_fallback_prompt(
                        prompt,
                        fallback_prompt=inline_media_fallback_prompt,
                    )
                    attempt_media_inputs = retry_decision.media_inputs
                    attempt_run_id = ai_runtime.next_retry_run_id(continuation_state.active_run_id)
                    holder.attempt_run_id = attempt_run_id
                    continue

                logger.exception("team_response_failed", agents=agent_list)
                return ErroredAttempt(get_user_friendly_error_message(e, team_name))

            if isinstance(response, (TeamRunOutput, RunOutput)):
                holder.last_response = response
            if isinstance(response, (TeamRunOutput, RunOutput)) and is_errored_run_output(response):
                error_text = str(response.content or "Unknown team error")
                retry_decision = retry_media_inputs_after_failure(
                    media_route,
                    error_text,
                    attempt_media_inputs,
                )
                if not retried_after_media_fallback and retry_decision.should_retry:
                    logger.warning(
                        "Retrying team response after inline media errored run output",
                        agents=agent_list,
                        error=error_text,
                        removed_media_kinds=sorted(retry_decision.removed_kinds),
                    )
                    _cleanup_team_notice_state(
                        run_output=response,
                        scope_context=run.scope_context,
                        session_id=session_id,
                        entity_name=configured_team_name or team_name,
                    )
                    _scrub_team_retry_notice_state(
                        scope_context=run.scope_context,
                        entity_name=configured_team_name or team_name,
                    )
                    attempt_prompt = append_inline_media_fallback_prompt(
                        prompt,
                        fallback_prompt=inline_media_fallback_prompt,
                    )
                    attempt_media_inputs = retry_decision.media_inputs
                    attempt_run_id = ai_runtime.next_retry_run_id(continuation_state.active_run_id)
                    holder.attempt_run_id = attempt_run_id
                    continue
                logger.warning("Team response returned errored run output", agents=agent_list, error=error_text)

            break

        assert response is not None
        run_tool_executions = (
            _collect_team_tool_executions(response) if isinstance(response, (TeamRunOutput, RunOutput)) else []
        )
        metadata_content: dict[str, Any] | None = None
        if run_metadata_collector is not None and isinstance(response, (TeamRunOutput, RunOutput)):
            metadata_content = _build_team_run_metadata_content(
                config=config,
                prepared_execution=prepared_execution,
                response=response,
                session_id=session_id,
                tool_count=len(run_tool_executions),
            )
        if isinstance(response, (TeamRunOutput, RunOutput)) and is_cancelled_run_output(response):
            partial_text = _extract_interrupted_team_partial_text(response)
            completed_tools, interrupted_tools = _extract_cancelled_team_tool_trace(response)
            return CancelledAttempt(
                reason=response.content,
                partial_text=partial_text,
                completed_tools=tuple(completed_tools),
                interrupted_tools=tuple(interrupted_tools),
                session_id=response.session_id,
                run_id=response.run_id or attempt_run_id,
                metadata_content=metadata_content,
            )
        if isinstance(response, (TeamRunOutput, RunOutput)) and is_errored_run_output(response):
            return ErroredAttempt(
                get_user_friendly_error_message(
                    Exception(str(response.content or "Unknown team error")),
                    team_name,
                ),
                metadata_content=metadata_content,
            )
        if reply_to_event_id:
            _persist_bound_seen_event_ids(
                scope_context=run.scope_context,
                session_id=session_id,
                event_ids=_run_metadata_seen_event_ids(run_metadata),
            )

        response_session_id: str | None = None
        response_run_id: str | None = None
        response_output_tokens: int | None = None
        is_empty_run = False
        if isinstance(response, (TeamRunOutput, RunOutput)):
            if isinstance(response, TeamRunOutput) and response.member_responses:
                logger.debug("team_member_response_count", response_count=len(response.member_responses))

            if isinstance(response, TeamRunOutput):
                logger.info(
                    "team_consensus_preview",
                    content_preview=response.content[:200] if response.content else None,
                )
            team_response_text = _team_response_text(response)
            has_visible_output = _has_visible_team_output(response)
            response_session_id = response.session_id
            response_run_id = response.run_id
            response_output_tokens = response.metrics.output_tokens if response.metrics is not None else None
            is_empty_run = response.status is RunStatus.completed and not run_tool_executions and not has_visible_output
        else:
            logger.warning(
                "team_response_unexpected_type",
                response_type=type(response).__name__,
                response=response,
            )
            team_response_text = str(response)
            has_visible_output = bool(team_response_text)

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
        return CompletedAttempt(
            response_text=response_text,
            replayable_text=response_text,
            has_visible_content=has_visible_output,
            is_empty=is_empty_run,
            session_id=response_session_id,
            run_id=response_run_id,
            attempt_run_id=attempt_run_id,
            output_tokens=response_output_tokens,
            tool_executions=tuple(run_tool_executions),
            completed_tools=tuple(
                _extract_completed_team_tool_trace(response)
                if isinstance(response, (TeamRunOutput, RunOutput))
                else [],
            ),
            metadata_content=metadata_content,
        )

    def _finalize_team_attempt(scope_context: ScopeSessionContext | None) -> None:
        if not holder.attempt_started:
            return
        holder.attempt_started = False
        _cleanup_team_notice_state(
            run_output=holder.last_response,
            scope_context=scope_context,
            session_id=session_id,
            entity_name=configured_team_name or team_name,
        )

    team_empty_run, persist_team_replay = _build_team_turn_capabilities(
        message=message,
        session_id=session_id,
        entity_name=configured_team_name or team_name,
    )
    _release_team_attempt_members, _close_team_attempt_dbs = _build_team_runtime_db_callbacks(holder)
    adapter = BlockingTurnAdapter(
        open_scope=lambda: open_bound_scope_session_context(
            agents=agents,
            session_id=session_id,
            runtime_paths=orchestrator.runtime_paths,
            config=config,
            execution_identity=execution_identity,
            team_name=configured_team_name,
        ),
        run_attempt=_run_team_attempt,
        snapshot_partial=lambda: TurnPartialSnapshot(attempt_run_id=holder.attempt_run_id),
        release_attempt_entity=_release_team_attempt_members,
        close_runtime_dbs=_close_team_attempt_dbs,
        finalize_attempt=_finalize_team_attempt,
        unexpected_error_text=lambda e: get_user_friendly_error_message(e, team_name),
        continuation=ContinuationCapability(),
        empty_run=team_empty_run,
        persist_standalone_replay=persist_team_replay,
    )
    return await run_blocking_response_turn(
        ResponseTurnContext(
            entity_label=team_name,
            session_id=session_id,
            run_id=run_id,
            correlation_id=correlation_id,
            reply_to_event_id=reply_to_event_id,
            room_id=room_id,
            thread_id=thread_id,
            requester_id=requester_id,
            matrix_run_metadata=matrix_run_metadata,
        ),
        adapter,
        TurnSinks(turn_recorder=turn_recorder, run_metadata_collector=run_metadata_collector),
        continuation=DynamicContinuationRunState.initial(
            prompt=message,
            model_prompt=None,
            current_timestamp_ms=current_timestamp_ms,
            current_prompt_is_structured=current_prompt_is_structured,
            run_id=run_id,
            continuation_model_prompt_tail="",
        ),
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


async def team_response_stream(  # noqa: C901, PLR0915
    agent_ids: list[MatrixID],
    message: str,
    orchestrator: OrchestratorRuntime,
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
    current_timestamp_ms: float | None = None,
    current_prompt_is_structured: bool = False,
    correlation_id: str | None = None,
    active_event_ids: Collection[str] = frozenset(),
    response_sender_id: str | None = None,
    compaction_outcomes_collector: list[CompactionOutcome] | None = None,
    compaction_lifecycle: CompactionLifecycle | None = None,
    run_metadata_collector: dict[str, Any] | None = None,
    configured_team_name: str | None = None,
    matrix_run_metadata: dict[str, Any] | None = None,
    system_enrichment_items: Sequence[EnrichmentItem] = (),
    pipeline_timing: DispatchPipelineTiming | None = None,
    *,
    turn_recorder: TurnRecorder | None = None,
    reason_prefix: str = "Team request",
) -> AsyncIterator[_TeamStreamChunk]:
    """Aggregate team streaming into a non-stream-style document, live.

    Renders a header and per-member sections, optionally adding a team
    consensus if present. Rebuilds the entire document as new events
    arrive so the final shape matches the non-stream style.
    """
    assert orchestrator.config is not None
    requested_agent_names = _requested_team_agent_names(
        [_team_member_name(mid, orchestrator.config, orchestrator.runtime_paths) for mid in agent_ids],
    )
    allow_direct_private_agents = _allow_direct_private_team_agents(
        execution_identity,
        configured_team_name=configured_team_name,
    )
    orchestrator.config.assert_team_agents_supported(
        requested_agent_names,
        allow_direct_private_agents=allow_direct_private_agents,
    )
    unavailable_bases: dict[str, KnowledgeAvailabilityDetail] = {}
    room_id = execution_identity.room_id if execution_identity is not None else None
    thread_id = (
        execution_identity.resolved_thread_id or execution_identity.thread_id
        if execution_identity is not None
        else None
    )
    requester_id = user_id or (execution_identity.requester_id if execution_identity is not None else None)
    correlation_id = resolve_run_correlation_id(
        correlation_id,
        reply_to_event_id=reply_to_event_id,
        matrix_run_metadata=matrix_run_metadata,
    )
    try:
        # Member agent builds walk the filesystem (workspace scaffolding,
        # context files, session storage); keep them off the event loop (#1260).
        team_members = await asyncio.to_thread(
            _materialize_team_members,
            requested_agent_names,
            orchestrator,
            execution_identity,
            session_id=session_id,
            unavailable_bases=unavailable_bases,
            reason_prefix=reason_prefix,
            configured_team_name=configured_team_name,
        )
    except ValueError as exc:
        yield str(exc)
        return
    system_enrichment_items = append_knowledge_availability_enrichment(
        system_enrichment_items,
        unavailable_bases,
    )
    agent_names = team_members.display_names
    display_names = team_members.display_names
    team_label = f"Team ({', '.join(agent_names)})"
    base_media_inputs = media or MediaInputs()
    config = orchestrator.config
    enrichment_items = system_enrichment_items
    holder = _TeamTurnHolder(team_members=team_members)

    async def _run_team_stream_attempt(  # noqa: C901, PLR0912, PLR0915
        run: TurnRunState,
        continuation_state: DynamicContinuationRunState,
    ) -> AsyncGenerator[_TeamStreamChunk | AttemptResolved, None]:
        """Stream one team attempt, ending with its ``AttemptResolved`` sentinel."""
        attempt_members = holder.team_members
        if attempt_members is None:
            # A continuation released the spent members; rebuild them so the
            # loaded dynamic-tool schema is baked into fresh member agents.
            attempt_members = await asyncio.to_thread(
                _materialize_team_members,
                requested_agent_names,
                orchestrator,
                execution_identity,
                session_id=session_id,
                reason_prefix=reason_prefix,
                configured_team_name=configured_team_name,
            )
            holder.team_members = attempt_members
        attempt_agents = attempt_members.agents
        team = build_materialized_team_instance(
            requested_agent_names=attempt_members.requested_agent_names,
            agents=attempt_agents,
            mode=mode,
            config=config,
            runtime_paths=orchestrator.runtime_paths,
            scope_context=run.scope_context,
            model_name=model_name,
            configured_team_name=configured_team_name,
            execution_identity=execution_identity,
        )
        holder.team = team
        prepared_execution = await prepare_materialized_team_execution(
            scope_context=run.scope_context,
            agents=attempt_agents,
            team=team,
            message=continuation_state.active_prompt,
            thread_history=thread_history,
            config=config,
            runtime_paths=orchestrator.runtime_paths,
            active_model_name=model_name,
            reply_to_event_id=reply_to_event_id,
            active_event_ids=active_event_ids,
            response_sender_id=response_sender_id,
            current_sender_id=user_id,
            room_id=room_id,
            thread_id=thread_id,
            requester_id=requester_id,
            correlation_id=correlation_id,
            compaction_outcomes_collector=compaction_outcomes_collector,
            current_timestamp_ms=continuation_state.active_current_timestamp_ms,
            current_prompt_is_structured=continuation_state.active_current_prompt_is_structured,
            compaction_lifecycle=compaction_lifecycle,
            configured_team_name=configured_team_name,
            thread_history_render_limits=_MATRIX_TEAM_THREAD_HISTORY_RENDER_LIMITS,
            matrix_run_metadata=matrix_run_metadata,
            pipeline_timing=pipeline_timing,
            system_enrichment_items=enrichment_items,
        )
        prepared_prompt = prepared_execution.prepared_prompt
        run.unseen_event_ids = prepared_execution.unseen_event_ids
        run.run_metadata = prepared_execution.run_metadata
        run_metadata = run.run_metadata
        unseen_event_ids = run.unseen_event_ids
        if turn_recorder is not None:
            turn_recorder.set_run_metadata(run_metadata)
        logger.info("team_streaming_setup", agents=agent_names, display_names=display_names)
        # Team runs flatten context messages to text, so media pinned to
        # thread-history messages is re-collected onto the current turn.
        context_media_inputs = ai_runtime.media_inputs_from_run_input(prepared_execution.messages)
        media_inputs = context_media_inputs.merge(base_media_inputs)
        inline_media_fallback_prompt = config.get_prompt("INLINE_MEDIA_FALLBACK_PROMPT")
        media_route = build_model_media_route(team.model) if media_inputs.has_any() else None
        media_filter = filter_media_inputs_for_route(media_route, media_inputs)
        attempt_prompt = (
            append_inline_media_fallback_prompt(
                prepared_prompt,
                fallback_prompt=inline_media_fallback_prompt,
            )
            if media_filter.removed_kinds
            else prepared_prompt
        )
        attempt_media_inputs = media_filter.media_inputs
        attempt_run_id = continuation_state.active_run_id
        holder.attempt_run_id = attempt_run_id

        canonical_per_member: dict[str, str] = {}
        canonical_consensus = ""
        tool_tracker = StreamingToolTracker()
        completed_tools = tool_tracker.completed_tools
        pending_tools = tool_tracker.pending_tools
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

        holder.render_partial = _current_canonical_partial_text

        def _sync_live_turn_recorder() -> None:
            run.turn_state.sync_partial(
                turn_recorder,
                run_metadata=run_metadata,
                assistant_text=_current_canonical_partial_text(),
                completed_tools=completed_tools,
                interrupted_tools=[pending.trace_entry for pending in pending_tools],
            )

        def _start_tool(
            *,
            scope_key: str,
            apply_visible_text: Callable[[str], None],
            tool: ToolExecution | None,
        ) -> None:
            nonlocal next_tool_index
            tool_index = next_tool_index if show_tool_calls else None
            tool_msg, trace_entry = tool_tracker.start(tool, scope_key=scope_key, tool_index=tool_index)
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
            completion = tool_tracker.complete(tool, scope_key=scope_key)
            if completion is None:
                return
            tool_name, result, pending_tool, completed_trace = completion

            if not show_tool_calls:
                return

            if pending_tool is None or pending_tool.visible_tool_index is None:
                logger.warning(
                    "Missing pending tool start in team stream; skipping completion marker",
                    tool_name=tool_name,
                    scope=scope_key,
                )
                return

            updated_text, _ = complete_pending_tool_block(
                get_visible_text(),
                tool_name,
                result,
                tool_index=pending_tool.visible_tool_index,
            )
            set_visible_text(updated_text)

            if not tool_tracker.update_visible_trace_entry(tool_trace, pending_tool, completed_trace):
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

        def _emit_tool_timing(
            *,
            phase: str,
            tool_scope: Literal["member", "team"],
            agent_name: str | None,
            tool: ToolExecution | None,
        ) -> None:
            emit_timing_event(
                "Dispatch tool-call timing",
                phase=phase,
                team_name=configured_team_name or team_label,
                tool_scope=tool_scope,
                agent_name=agent_name,
                tool_name=tool.tool_name if tool is not None else None,
                tool_call_id=tool_execution_call_id(tool),
                show_tool_calls=show_tool_calls,
            )

        holder.attempt_started = True
        for retried_after_media_fallback in (False, True):
            canonical_per_member = dict.fromkeys(display_names, "")
            visible_per_member = dict.fromkeys(display_names, "")
            canonical_consensus = ""
            visible_consensus = ""
            tool_trace = []
            tool_tracker = StreamingToolTracker()
            completed_tools = tool_tracker.completed_tools
            next_tool_index = 1
            pending_tools = tool_tracker.pending_tools
            holder.tool_tracker = tool_tracker
            completed_tool_executions: list[ToolExecution] = []
            emitted_output = False
            media_fallback_retry_requested = False

            ai_runtime.note_attempt_run_id(run_id_callback, attempt_run_id)

            raw_stream = await _team_response_stream_raw(
                team=team,
                team_members=attempt_members,
                prompt=attempt_prompt,
                metadata=run_metadata,
                media=attempt_media_inputs,
                session_id=session_id,
                run_id=attempt_run_id,
                user_id=user_id,
            )
            stream_run_input = (
                ai_runtime.attach_media_to_run_input(attempt_prompt, attempt_media_inputs)
                if attempt_media_inputs.has_any()
                else attempt_prompt
            )
            raw_stream = stream_with_llm_request_log_context(
                cast("AsyncGenerator[Any, None]", raw_stream),
                request_context=_team_request_log_context(
                    team_name=configured_team_name or team_label,
                    session_id=session_id,
                    room_id=room_id,
                    thread_id=thread_id,
                    reply_to_event_id=reply_to_event_id,
                    requester_id=requester_id,
                    correlation_id=correlation_id,
                    prompt=message,
                    run_input=stream_run_input,
                    metadata=run_metadata,
                ),
            )
            async for event in raw_stream:
                if isinstance(event, (TeamRunOutput, RunOutput)):
                    _cleanup_team_notice_state(
                        run_output=event,
                        scope_context=run.scope_context,
                        session_id=session_id,
                        entity_name=configured_team_name or team_label,
                    )
                    event_tool_executions = _collect_team_tool_executions(event)
                    event_metadata_content: dict[str, Any] | None = None
                    if run_metadata_collector is not None:
                        event_metadata_content = _build_team_run_metadata_content(
                            config=config,
                            prepared_execution=prepared_execution,
                            response=event,
                            session_id=session_id,
                            tool_count=len(event_tool_executions),
                        )

                    if is_cancelled_run_output(event):
                        partial_text = _extract_interrupted_team_partial_text(event)
                        completed_tool_trace, interrupted_tool_trace = _extract_cancelled_team_tool_trace(event)
                        yield AttemptResolved(
                            CancelledAttempt(
                                reason=event.content,
                                partial_text=partial_text,
                                completed_tools=tuple(completed_tool_trace),
                                interrupted_tools=tuple(interrupted_tool_trace),
                                metadata_content=event_metadata_content,
                            ),
                        )
                        return

                    if is_errored_run_output(event):
                        error_text = str(event.content or "Unknown team error")
                        retry_decision = retry_media_inputs_after_failure(
                            media_route,
                            error_text,
                            attempt_media_inputs,
                        )
                        if (
                            not retried_after_media_fallback
                            and not (emitted_output or pending_tools or completed_tools)
                            and retry_decision.should_retry
                        ):
                            logger.warning(
                                "Retrying team streaming after inline media errored run output",
                                agents=", ".join(agent_names),
                                error=error_text,
                                removed_media_kinds=sorted(retry_decision.removed_kinds),
                            )
                            _scrub_team_retry_notice_state(
                                scope_context=run.scope_context,
                                entity_name=configured_team_name or team_label,
                            )
                            attempt_prompt = append_inline_media_fallback_prompt(
                                prepared_prompt,
                                fallback_prompt=inline_media_fallback_prompt,
                            )
                            attempt_media_inputs = retry_decision.media_inputs
                            attempt_run_id = ai_runtime.next_retry_run_id(continuation_state.active_run_id)
                            holder.attempt_run_id = attempt_run_id
                            media_fallback_retry_requested = True
                            break
                        yield get_user_friendly_error_message(Exception(error_text), team_label)
                        yield AttemptResolved(HandledAttempt())
                        return

                    if reply_to_event_id:
                        _persist_bound_seen_event_ids(
                            scope_context=run.scope_context,
                            session_id=session_id,
                            event_ids=[reply_to_event_id, *unseen_event_ids],
                        )
                    response_text = _format_terminal_team_response(
                        event,
                        team_display_names=team_members.display_names,
                    )
                    event_has_visible = _has_visible_team_output(event)
                    yield response_text
                    yield AttemptResolved(
                        CompletedAttempt(
                            replayable_text=response_text,
                            has_visible_content=event_has_visible,
                            is_empty=(
                                event.status is RunStatus.completed
                                and not event_tool_executions
                                and not event_has_visible
                            ),
                            session_id=event.session_id,
                            run_id=event.run_id,
                            attempt_run_id=attempt_run_id,
                            output_tokens=event.metrics.output_tokens if event.metrics is not None else None,
                            tool_executions=tuple(event_tool_executions),
                            completed_tools=tuple(_extract_completed_team_tool_trace(event)),
                            metadata_content=event_metadata_content,
                        ),
                    )
                    return

                if isinstance(event, TeamRunErrorEvent):
                    error_text = event.content or "Unknown team error"
                    retry_decision = retry_media_inputs_after_failure(
                        media_route,
                        error_text,
                        attempt_media_inputs,
                    )
                    if (
                        not retried_after_media_fallback
                        and not (emitted_output or pending_tools or completed_tools)
                        and retry_decision.should_retry
                    ):
                        logger.warning(
                            "Retrying team streaming after inline media team error",
                            agents=", ".join(agent_names),
                            error=error_text,
                            removed_media_kinds=sorted(retry_decision.removed_kinds),
                        )
                        _scrub_team_retry_notice_state(
                            scope_context=run.scope_context,
                            entity_name=configured_team_name or team_label,
                        )
                        attempt_prompt = append_inline_media_fallback_prompt(
                            prepared_prompt,
                            fallback_prompt=inline_media_fallback_prompt,
                        )
                        attempt_media_inputs = retry_decision.media_inputs
                        attempt_run_id = ai_runtime.next_retry_run_id(continuation_state.active_run_id)
                        holder.attempt_run_id = attempt_run_id
                        media_fallback_retry_requested = True
                        break
                    yield get_user_friendly_error_message(Exception(error_text), team_label)
                    yield AttemptResolved(HandledAttempt())
                    return

                if isinstance(event, TeamRunCancelledEvent):
                    yield AttemptResolved(
                        CancelledAttempt(
                            reason=event.reason,
                            partial_text=_current_canonical_partial_text(),
                            completed_tools=tuple(completed_tools),
                            interrupted_tools=tuple(pending.trace_entry for pending in pending_tools),
                        ),
                    )
                    return

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
                        _emit_tool_timing(
                            phase="agno_tool_call_started",
                            tool_scope="member",
                            agent_name=member_name,
                            tool=event.tool,
                        )
                        _start_tool_for_member(member_name, event.tool)
                elif isinstance(event, AgentToolCallCompletedEvent):
                    member_name = event.agent_name
                    if member_name:
                        _emit_tool_timing(
                            phase="agno_tool_call_completed",
                            tool_scope="member",
                            agent_name=member_name,
                            tool=event.tool,
                        )
                        if event.tool is not None:
                            completed_tool_executions.append(event.tool)
                        _complete_tool_for_member(member_name, event.tool)
                elif isinstance(event, TeamRunContentEvent):
                    if event.content:
                        content = str(event.content)
                        canonical_consensus += content
                        visible_consensus += content
                    else:
                        logger.debug("Empty team consensus event received")
                elif isinstance(event, TeamToolCallStartedEvent):
                    _emit_tool_timing(
                        phase="agno_tool_call_started",
                        tool_scope="team",
                        agent_name=None,
                        tool=event.tool,
                    )
                    _start_tool(
                        scope_key="team",
                        apply_visible_text=_append_to_visible_consensus,
                        tool=event.tool,
                    )
                elif isinstance(event, TeamToolCallCompletedEvent):
                    _emit_tool_timing(
                        phase="agno_tool_call_completed",
                        tool_scope="team",
                        agent_name=None,
                        tool=event.tool,
                    )
                    if event.tool is not None:
                        completed_tool_executions.append(event.tool)
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

            if media_fallback_retry_requested:
                continue
            if emitted_output and reply_to_event_id:
                _persist_bound_seen_event_ids(
                    scope_context=run.scope_context,
                    session_id=session_id,
                    event_ids=[reply_to_event_id, *unseen_event_ids],
                )
            if emitted_output or completed_tool_executions:
                canonical_text = _current_canonical_partial_text()
                yield AttemptResolved(
                    CompletedAttempt(
                        replayable_text=(
                            _format_team_header(team_members.display_names) + canonical_text if canonical_text else ""
                        ),
                        has_visible_content=bool(canonical_text),
                        attempt_run_id=attempt_run_id,
                        tool_executions=tuple(completed_tool_executions),
                        completed_tools=tuple(completed_tools),
                    ),
                )
            return

    def _finalize_team_stream_attempt(scope_context: ScopeSessionContext | None) -> None:
        if not holder.attempt_started:
            return
        holder.attempt_started = False
        _cleanup_team_notice_state(
            run_output=None,
            scope_context=scope_context,
            session_id=session_id,
            entity_name=configured_team_name or team_label,
        )

    team_empty_run, persist_team_replay = _build_team_turn_capabilities(
        message=message,
        session_id=session_id,
        entity_name=configured_team_name or team_label,
    )

    def _snapshot_stream_partial() -> TurnPartialSnapshot:
        return TurnPartialSnapshot(
            assistant_text=holder.render_partial(),
            completed_tools=tuple(holder.tool_tracker.completed_tools),
            interrupted_tools=tuple(pending.trace_entry for pending in holder.tool_tracker.pending_tools),
            attempt_run_id=holder.attempt_run_id,
        )

    _release_team_attempt_members, _close_team_attempt_dbs = _build_team_runtime_db_callbacks(holder)
    adapter = StreamingTurnAdapter[_TeamStreamChunk](
        open_scope=lambda: open_bound_scope_session_context(
            agents=team_members.agents,
            session_id=session_id,
            runtime_paths=orchestrator.runtime_paths,
            config=config,
            execution_identity=execution_identity,
            team_name=configured_team_name,
        ),
        run_attempt=_run_team_stream_attempt,
        snapshot_partial=_snapshot_stream_partial,
        release_attempt_entity=_release_team_attempt_members,
        close_runtime_dbs=_close_team_attempt_dbs,
        finalize_attempt=_finalize_team_stream_attempt,
        make_notice_chunk=lambda text: text,
        unexpected_error_text=lambda e: get_user_friendly_error_message(e, team_label),
        continuation=ContinuationCapability(),
        empty_run=team_empty_run,
        persist_standalone_replay=persist_team_replay,
    )
    response_stream = stream_response_turn(
        ResponseTurnContext(
            entity_label=team_label,
            session_id=session_id,
            run_id=run_id,
            correlation_id=correlation_id,
            reply_to_event_id=reply_to_event_id,
            room_id=room_id,
            thread_id=thread_id,
            requester_id=requester_id,
            matrix_run_metadata=matrix_run_metadata,
        ),
        adapter,
        TurnSinks(turn_recorder=turn_recorder, run_metadata_collector=run_metadata_collector),
        continuation=DynamicContinuationRunState.initial(
            prompt=message,
            model_prompt=None,
            current_timestamp_ms=current_timestamp_ms,
            current_prompt_is_structured=current_prompt_is_structured,
            run_id=run_id,
            continuation_model_prompt_tail="",
        ),
    )
    # Close the driver generator deterministically when this wrapper unwinds so
    # its cleanup does not wait for event-loop async-generator finalization.
    async with aclosing(response_stream) as closing_stream:
        async for chunk in closing_stream:
            yield chunk


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
    "prepare_materialized_team_execution",
    "resolve_configured_team",
    "resolve_live_shared_agent_names",
    "select_ad_hoc_team_mode",
    "select_model_for_team",
    "team_response",
    "team_response_stream",
]
