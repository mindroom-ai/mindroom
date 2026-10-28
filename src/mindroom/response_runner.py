"""Response lifecycle execution extracted from ``bot.py``."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager, nullcontext, suppress
from copy import deepcopy
from dataclasses import dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast
from uuid import uuid4

from mindroom import reply_lifecycle as rl
from mindroom.agent_cli.cli_approval_waits import CliApprovalWaits
from mindroom.agent_modes import resolve_agent_mode
from mindroom.agent_run_context import append_knowledge_availability_enrichment
from mindroom.agents import show_tool_calls_for_agent
from mindroom.ai import ResponseTurnContext, stream_agent_response
from mindroom.ai_run_metadata import ai_run_extra_content_from_metadata
from mindroom.ai_runtime import bind_mid_turn_conversation_context
from mindroom.approval.execution import AgentApprovalExecution
from mindroom.approval.receipt_readers import approval_receipt_context
from mindroom.approval.response import (
    ApprovalResponseCoordinator,
    continuation_target,
    identify_approval_tools,
    plan_approval_calls,
    require_ordered_pause_presentation,
)
from mindroom.authorization import ReplyMembershipPendingError, is_sender_allowed_for_entity_replies_in_room
from mindroom.automations.steps import is_automation_hook_source
from mindroom.background_tasks import create_background_task, run_coroutine_until_complete
from mindroom.budgets.monitor import budget_model
from mindroom.constants import (
    ATTACHMENT_IDS_KEY,
    HOOK_SOURCE_RUN_METADATA_KEY,
    ROUTER_AGENT_NAME,
    STREAM_STATUS_APPROVAL_PENDING,
    STREAM_STATUS_KEY,
    STREAM_STATUS_PENDING,
    TURN_INTENT_RUN_METADATA_KEY,
)
from mindroom.conversation_state_writer import ReplySession, session_started_on_creation
from mindroom.dispatch_source import SILENT_SCHEDULE_SOURCE_KIND, is_automation_source_kind
from mindroom.entity_resolution import current_internal_sender_ids
from mindroom.error_handling import get_user_friendly_error_message
from mindroom.event_journal import (
    INTERRUPTED_FAILURE_REASON,
    ApprovalAdvance,
    ApprovalContinuation,
    ApprovalMemoryTurn,
    MatrixDelivery,
)
from mindroom.event_journal import (
    ApprovalDecision as ContinuationDecision,
)
from mindroom.final_delivery import FinalDeliveryOutcome, StreamTransportOutcome
from mindroom.history.storage import has_queued_force_compaction
from mindroom.history.turn_recorder import TurnRecorder
from mindroom.hooks import EnrichmentItem, MessageEnvelope
from mindroom.interactive import InteractiveMetadata
from mindroom.judgment.cold_cache import idle_seconds_before_turn, select_cold_cache_model
from mindroom.judgment.conversation import conversation_context_before_sources
from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
from mindroom.matrix.presence import should_use_streaming
from mindroom.matrix.typing import typing_indicator
from mindroom.memory import store_conversation_memory
from mindroom.mid_turn_judgment import create_mid_turn_gate
from mindroom.orchestration.runtime import (
    cancel_failure_reason,
    cancel_source_from_failure_reason,
    classify_cancel_source,
    current_task_is_process_shutdown,
    log_cancelled_response,
)
from mindroom.participation import ParticipationGate
from mindroom.participation_judgment import create_participation_decider
from mindroom.post_response_effects import PostResponseEffectsSupport, ResponseOutcome
from mindroom.reply_presentation import (
    AGENT_PLACEHOLDER,
    TEAM_PLACEHOLDER,
    NoteKind,
    Presentation,
    current_answer,
    decode_presentation,
    note_segment,
    render_body,
)
from mindroom.reply_scope import (
    ClaimRefused,
    ReplyWriteRefusedError,
    SpanHandle,
    SpanSlot,
    current_slot,
    current_span,
    initial_write,
    interruption_note,
    pause_decision,
    pause_write,
    release_decision,
    suppress_decision,
    terminal_source_decision,
)
from mindroom.reply_turn.enrichment import (
    agent_has_matrix_messaging_tool,
    interrupted_attempt_model_prompt,
    matrix_message_target_item,
    prepare_memory_and_model_context,
    with_matrix_message_target,
    with_silent_schedule_delivery,
)
from mindroom.reply_turn.inbox import InboxResponses
from mindroom.response_attempt import ResponseAttemptDeps, ResponseAttemptRequest, ResponseAttemptRunner, SpanAttempt
from mindroom.response_sources import ResponseSources
from mindroom.response_turn import (
    CompletedApprovalRun,
    PausedAnswer,
    PausedAttempt,
    ResponsePausedForApproval,
    TurnSinks,
    collect_streamed_response_content,
)
from mindroom.runtime_resolution import resolve_agent_storage
from mindroom.scheduling.scheduled_run_records import record_silent_schedule_started_if_needed
from mindroom.skill_learning.capture import SkillReviewCapture
from mindroom.streaming import (
    PROGRESS_PLACEHOLDER,
    ReplacementStreamingResponse,
    StreamingDeliveryError,
    StreamingResponse,
    UnfinishedStreamedReply,
)
from mindroom.teams.envelope import TeamTurnSpec, team_response_stream
from mindroom.teams.members import TeamTurnModelSelection, resolve_team_turn_models, select_model_for_team
from mindroom.teams.resume import continue_paused_team_run
from mindroom.teams.spec import TeamMode
from mindroom.thread_models import set_thread_model_override
from mindroom.thread_summary import thread_summary_message_count_hint
from mindroom.timing import DispatchPipelineTiming, timed
from mindroom.tool_jobs.follow_ups import follow_up_envelope, follow_up_jobs, follow_up_key
from mindroom.tool_jobs.runtime import get_background_runtime
from mindroom.tool_system.context_bound_streams import closing_async_stream
from mindroom.tool_system.events import CollectedStreamPresentation, body_without_tool_markers
from mindroom.tool_system.runtime_context import (
    LiveToolDispatchContext,
    ToolDispatchContext,
    runtime_context_from_dispatch_context,
)
from mindroom.tool_system.worker_routing import (
    run_with_tool_execution_identity,
    stream_with_tool_execution_identity,
)
from mindroom.turn_origin import SenderKind, TurnIntent
from mindroom.turn_record import RevisionSnapshotChangedError

from .delivery_gateway import (
    DeliveryGateway,
    EditTextRequest,
    FinalDeliveryRequest,
    FinalizeStreamedResponseRequest,
    MatrixCompactionLifecycle,
    ResponseIdentity,
    SendTextRequest,
    StreamingDeliveryRequest,
)
from .media_inputs import MediaInputs
from .response_activity import ActiveResponseIdentity
from .response_admission import ResponseAdmissionRefusedError
from .response_lifecycle import (
    QueuedHumanNoticeReservation,
    ResponseLifecycle,
    ResponseLifecycleCoordinator,
    ResponseLifecycleDeps,
    ResponseLifecycleReservation,
)


def _approval_interruption_cancel_source(reason: str) -> Literal["sync_restart", "interrupted"] | None:
    """Recover the cancellation provenance persisted for an interrupted approval."""
    cancel_source = cancel_source_from_failure_reason(reason)
    if cancel_source == "user_stop" or cancel_failure_reason(cancel_source) != reason:
        return None
    return cancel_source


if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Mapping, Sequence
    from contextlib import AbstractAsyncContextManager
    from pathlib import Path

    import nio
    import structlog

    from mindroom.bot_runtime_view import BotRuntimeView
    from mindroom.config.main import Config, ResolvedRuntimeModel
    from mindroom.config.participation import ParticipationConfig
    from mindroom.constants import RuntimePaths
    from mindroom.conversation_resolver import ConversationResolver
    from mindroom.conversation_state_writer import ConversationStateWriter
    from mindroom.dispatch_source import ScheduledHistoryBudget
    from mindroom.event_journal import JournalEvent, PrincipalStore
    from mindroom.history.types import HistoryScope
    from mindroom.judgment.state import JudgmentMessage
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.knowledge.search import AgentKnowledge
    from mindroom.knowledge.utils import KnowledgeAccessSupport
    from mindroom.llm.runs import RunRequirement
    from mindroom.message_target import MessageTarget
    from mindroom.mid_turn import MidTurnGate
    from mindroom.post_response_effects import PostResponseEffectsDeps
    from mindroom.reply_scope import ReplyRuntime
    from mindroom.response_payload_preparation import ResponsePayloadPreparation, ResponsePayloadPreparer
    from mindroom.streaming import ProgressPublisher, StreamInputChunk
    from mindroom.tool_jobs.conversation import ConversationKey
    from mindroom.tool_system.events import ToolTraceEntry
    from mindroom.tool_system.runtime_context import ToolRuntimeSupport
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity
    from mindroom.turn_origin import TurnOrigin

    from .response_admission import ResponseAdmissionGate

_ToolContextResult = TypeVar("_ToolContextResult")
_ToolStreamChunk = TypeVar("_ToolStreamChunk")
# How often a message waiting behind an approval rechecks that the approval still exists.
_APPROVAL_HOLD_RECHECK_SECONDS = 30.0
# A resume span that ended this way wrote its own ending; only a restart's or a release's hands work back.
_UNANSWERED_RESUME_OUTCOMES = frozenset({rl.SpanOutcome.FAILED, rl.SpanOutcome.CANCELLED, rl.SpanOutcome.SUPPRESSED})


def _terminal_status_of_span(span: rl.Span) -> Literal["completed", "cancelled", "error"]:
    """Return the status a frozen approval FINAL reports, from the span that wrote it."""
    if span.outcome is rl.SpanOutcome.COMPLETED:
        return "completed"
    return "cancelled" if span.outcome is rl.SpanOutcome.CANCELLED else "error"


def _merge_response_extra_content(
    extra_content: dict[str, Any] | None,
    attachment_ids: Sequence[str] | None,
) -> dict[str, Any] | None:
    """Merge optional attachment IDs into response metadata."""
    merged_extra_content = extra_content if extra_content is not None else {}
    if attachment_ids:
        merged_extra_content[ATTACHMENT_IDS_KEY] = list(attachment_ids)
    return merged_extra_content if extra_content is not None or attachment_ids else None


def _paused_with_committed_presentation(
    error: ResponsePausedForApproval,
    *,
    show_tool_calls: bool,
) -> PausedAttempt:
    """Attach only the response state acknowledged before the stream suspended."""
    if error.presentation is None:
        return error.paused
    if show_tool_calls:
        tool_trace = error.presentation.tool_trace
        presentation_state = error.presentation.state or {}
    else:
        tool_trace = error.paused.tool_trace
        presentation_state = error.paused.response_presentation_state
    return replace(
        error.paused,
        response_text=error.presentation.response_text,
        acknowledged_response_text=error.presentation.rendered_response_text,
        tool_trace=tool_trace,
        response_presentation_state=presentation_state,
    )


def _require_frozen_tool_visibility(show_tool_calls: bool | None) -> bool:
    """Return one turn's policy snapshot or reject an unfrozen approval handoff."""
    if show_tool_calls is None:
        msg = "Approval suspension requires turn-frozen tool visibility"
        raise RuntimeError(msg)
    return show_tool_calls


def _interruption_note_landed(handle: SpanHandle | None) -> bool:
    """Return whether a cancellation ended its turn with the interruption note.

    That note is the turn's terminal outcome, since nothing resumes it. A span
    that ended with the note recorded settled its sources even before Matrix
    acknowledged it; one released without it leaves the turn to replay.
    """
    return handle is not None and handle.span.outcome in {rl.SpanOutcome.FAILED, rl.SpanOutcome.CANCELLED}


def _resumed_reply() -> UnfinishedStreamedReply | None:
    """Return what the stopped attempt at the current span's reply showed, which this attempt streams below."""
    handle = current_span()
    return None if handle is None else handle.resumed


def _existing_event(request: ResponseRequest) -> tuple[str | None, bool]:
    """Return the event this response writes over, and whether that event is a placeholder rather than an answer.

    Inside a span its reply's records say. Before the claim, the request names
    the event it was dispatched for: an interactive selection's acknowledgement
    is the placeholder its answer fills, while a regeneration or an approval
    resume replaces an answer.
    """
    handle = current_span()
    if handle is None:
        event_id = request.existing_event_id
        return event_id, event_id is not None and request.interactive_span_id is not None
    reply = handle.reply
    replaces_answer = handle.span.kind in {rl.SpanKind.REGENERATION, rl.SpanKind.APPROVAL_RESUME}
    return reply.event_id, reply.event_id is not None and (not replaces_answer or reply.placeholder_only)


def _reply_event_id() -> str | None:
    """Return the event the current span's reply shows, if any."""
    handle = current_span()
    return None if handle is None else handle.reply.event_id


def _existing_event_id(request: ResponseRequest) -> str | None:
    """Return the event this response writes over, from its reply's records once claimed."""
    return _existing_event(request)[0]


def _shows_placeholder(request: ResponseRequest) -> bool:
    """Return whether the event this response writes over is a placeholder rather than an answer."""
    return _existing_event(request)[1]


def _replaceable_placeholder(request: ResponseRequest) -> bool:
    """Return whether the adopted event holds only a placeholder that terminal handling may replace or redact.

    A resumed reply shows its stopped attempt's work, which no failure may remove.
    """
    return _shows_placeholder(request) and _resumed_reply() is None


def _agent_run_metadata(request: ResponseRequest) -> dict[str, Any]:
    """Record what started this turn, so stored runs show which automation asked or that MindRoom wrote the prompt.

    The turn recorder gets it too, so a turn stopped before its run starts keeps it.
    """
    envelope = request.response_envelope
    recorded = {**(request.matrix_run_metadata or {}), TURN_INTENT_RUN_METADATA_KEY: envelope.origin.intent.value}
    if envelope.hook_source is not None:
        recorded[HOOK_SOURCE_RUN_METADATA_KEY] = envelope.hook_source
    return recorded


def _reply_authorization_entity_names(
    config: Config,
    owner_entity_name: str,
    team_member_names: Sequence[str],
) -> tuple[str, ...]:
    """Return the policy entities governing one agent or team execution."""
    if owner_entity_name in config.teams:
        return (owner_entity_name,)
    return tuple(dict.fromkeys((owner_entity_name, *team_member_names)))


@dataclass(frozen=True)
class ResponseRequest:
    """Typed carrier for one response lifecycle request."""

    thread_history: Sequence[ResolvedVisibleMessage]
    prompt: str
    response_envelope: MessageEnvelope
    sources: ResponseSources
    participation: ParticipationConfig | None = None
    member_display_names: Mapping[str, str] = field(default_factory=dict)
    model_prompt: str | None = None
    # The event this response was dispatched to write over: the answer an edit
    # regenerates or an approval resumes, or a selection's acknowledgement.
    # Once claimed, the span's reply names the event instead.
    existing_event_id: str | None = None
    # Set when an edit regenerates the reply at ``existing_event_id``.
    edit_regeneration: bool = False
    # The span an interactive selection's acknowledgement created, which this answer adopts.
    interactive_span_id: str | None = None
    # Set for a follow-up: the conversation whose ended background jobs it reports in a new reply.
    follow_up_key: ConversationKey | None = None
    user_id: str | None = None
    media: MediaInputs | None = None
    attachment_ids: tuple[str, ...] | None = None
    correlation_id: str | None = None
    matrix_run_metadata: Mapping[str, Any] | None = None
    participating_agent_names: tuple[str, ...] = ()
    transient_enrichment_items: tuple[EnrichmentItem, ...] = ()
    system_enrichment_items: tuple[EnrichmentItem, ...] = ()
    requires_model_history_refresh: bool = False
    scheduled_history_budget: ScheduledHistoryBudget | None = None
    history_boundary_event_id: str | None = None
    scheduled_model: str | None = None
    payload_preparation: ResponsePayloadPreparation | None = None
    current_timestamp_ms: float | None = None
    current_prompt_is_structured: bool = False
    on_lifecycle_lock_acquired: Callable[[], None] | None = None
    prepare_source_turn: Callable[[Sequence[ResolvedVisibleMessage]], Coroutine[Any, Any, bool]] | None = None
    # Runs once the turn claimed its reply, before the response is prepared: an edit prunes the history it
    # replaces only then, so a Stop that refuses the claim leaves that history whole.
    on_reply_claimed: Callable[[], Awaitable[None]] | None = None
    # Settles the turn's sources when they became terminal before any reply existed to settle them.
    on_source_turn_suppressed: Callable[[], Awaitable[None]] | None = None
    pipeline_timing: DispatchPipelineTiming | None = None
    # Records and settles a turn that ended before any reply span existed to settle it.
    on_no_response_handled: Callable[[], Awaitable[None]] | None = None
    # Set only after another durable owner can finish the source.
    source_handoff: asyncio.Event | None = None

    def __post_init__(self) -> None:
        """Require the envelope to name the source driving this response."""
        if not isinstance(self.sources, ResponseSources):
            message = "ResponseRequest requires ResponseSources"
            raise TypeError(message)
        if self.sources.pending_event_ids[0] != self.response_envelope.source_event_id:
            message = "ResponseRequest first pending event must equal the envelope source event"
            raise ValueError(message)

    @property
    def room_id(self) -> str:
        """Return the canonical response room."""
        return self.response_envelope.target.room_id

    @property
    def reply_to_event_id(self) -> str | None:
        """Return the canonical event this response answers."""
        return self.response_envelope.target.reply_to_event_id

    @property
    def thread_id(self) -> str | None:
        """Return the canonical resolved response thread root."""
        return self.response_envelope.target.resolved_thread_id


def _request_has_media(request: ResponseRequest) -> bool:
    """Count deferred attachment registration as media too."""
    preparation = request.payload_preparation
    return bool(
        request.attachment_ids
        or (request.media is not None and request.media.has_any())
        or (
            preparation is not None
            and (
                preparation.payload_inputs.media_events
                or preparation.payload_inputs.message_attachment_ids
                or preparation.payload_inputs.trusted_attachment_ids
                or preparation.payload_inputs.raw_audio_fallback
            )
        ),
    )


def _mid_turn_for_request(
    request: ResponseRequest,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    on_defer: Callable[[str, str], Awaitable[None]],
) -> MidTurnGate | None:
    """Treat deferred attachment registration as incomplete judgment context too."""
    has_media = _request_has_media(request)
    gate = create_mid_turn_gate(
        config,
        runtime_paths,
        request.response_envelope,
        prompt=request.prompt,
        has_media=has_media,
        on_defer=on_defer,
    )
    if gate is not None:
        # The ingress history may be stale until this response acquires the lock.
        gate.bind_conversation_context(None)
    # A regeneration replaces an answer that may show, even one whose event Matrix has not acknowledged yet.
    if (
        gate is not None
        and (request.existing_event_id or request.edit_regeneration)
        and not _shows_placeholder(request)
    ):
        gate.visible_response_text = None
    return gate


def _participation_for_request(
    request: ResponseRequest,
    config: Config,
    runtime_paths: RuntimePaths,
) -> ParticipationGate | None:
    """Own one participation decision from locked preparation through delivery."""
    if request.participation is None:
        return None
    gate = ParticipationGate(instructions=request.participation.instructions)
    if request.existing_event_id is not None:
        gate.approve_existing_response()
    else:
        gate.decider = create_participation_decider(
            request.participation,
            config,
            runtime_paths,
            agent_name=request.response_envelope.agent_name,
        )
    return gate


def _approve_continued_reply(participation: ParticipationGate | None, request: ResponseRequest) -> None:
    """A claim that continued a reply already showing this turn's answer needs no new participation decision."""
    if participation is not None and _existing_event_id(request) is not None:
        participation.approve_existing_response()


def _skipped_participation_outcome() -> FinalDeliveryOutcome:
    """Describe quiet completion without claiming successful response generation."""
    return FinalDeliveryOutcome(
        terminal_status="completed",
        event_id=None,
        suppressed=True,
        failure_reason="participation_declined",
    )


def _skip_unapproved_response(participation: ParticipationGate | None) -> FinalDeliveryOutcome | None:
    """Settle a pre-approval failure before delivery."""
    if participation is None or not participation.decline("run_failed_before_decision"):
        return None
    return _skipped_participation_outcome()


def _is_silent_schedule_response(request: ResponseRequest) -> bool:
    """Return whether one response must avoid provisional Matrix activity."""
    return request.response_envelope.source_kind == SILENT_SCHEDULE_SOURCE_KIND


def _correlation_id_for_request(request: ResponseRequest) -> str:
    """Resolve the correlation id for one request."""
    return request.correlation_id or request.reply_to_event_id or request.response_envelope.source_event_id


def _response_typing_log_context(
    request: ResponseRequest,
    *,
    response_run_id: str | None,
) -> dict[str, object]:
    """Return the stable response attribution shared by one typing lease."""
    target = request.response_envelope.target
    return {
        **target.log_context,
        "agent_id": request.response_envelope.agent_name,
        "requester_id": request.response_envelope.requester_id,
        "session_id": target.session_id,
        "reply_to_event_id": target.reply_to_event_id,
        "correlation_id": _correlation_id_for_request(request),
        "response_run_id": response_run_id,
    }


@asynccontextmanager
async def _response_typing_indicator(
    client: nio.AsyncClient,
    request: ResponseRequest,
    *,
    response_run_id: str | None,
    participation: ParticipationGate | None = None,
) -> AsyncIterator[None]:
    """Expose typing only for response kinds whose progress may be visible."""
    if _is_silent_schedule_response(request):
        yield
        return

    @asynccontextmanager
    async def show_typing() -> AsyncIterator[None]:
        async with typing_indicator(
            client,
            request.room_id,
            log_context=_response_typing_log_context(request, response_run_id=response_run_id),
        ):
            yield

    if participation is None:
        async with show_typing():
            yield
        return
    finished = asyncio.Event()

    async def approved_typing() -> None:
        if await participation.wait_for_approval():
            async with show_typing():
                await finished.wait()

    task = asyncio.create_task(approved_typing())
    try:
        yield
    finally:
        finished.set()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def _response_thread_id(request: ResponseRequest, resolved_target: MessageTarget) -> str | None:
    """Return the thread root used for this response's model and delivery context."""
    if _existing_event_id(request) and not _shows_placeholder(request):
        return request.thread_id
    return resolved_target.resolved_thread_id


class PostLockRequestPreparationError(RuntimeError):
    """Raised when post-lock request preparation fails before generation starts.

    It only escapes a claimed reply, whose records then own the failure notice.
    """

    def __init__(self, message: str = "Post-lock request preparation failed") -> None:
        super().__init__(message)


def _paused_answer() -> PausedAnswer:
    """Return the answer the current approval resume continues, as its reply's records keep it."""
    handle = current_span()
    assert handle is not None, "every approval resume runs in its reply's resume span"
    segment = current_answer(handle.base, handle.span_id)
    if segment is None:
        return PausedAnswer()
    # The resume completes these entries in place; the span's base keeps what the pause showed.
    return PausedAnswer(text=segment.text, tool_trace=deepcopy(segment.tool_trace), team_state=segment.team_state)


def _paused_presentation(handle: SpanHandle, paused: PausedAttempt, *, waiting_text: str | None) -> Presentation:
    """Return what a span's reply shows while it waits for approval."""
    return handle.presentation(
        paused.response_text or "",
        tuple(paused.tool_trace),
        team_state=paused.response_presentation_state or None,
        trailing_note=None if waiting_text is None else note_segment(NoteKind.APPROVAL_WAIT, waiting_text),
    )


@dataclass(frozen=True)
class _ShownPause:
    """Where a pause shows before its continuation exists."""

    response_event_id: str | None
    delivery_kind: Literal["sent", "edited"] | None
    final_visible_body: str | None
    # The span, its paused presentation, and the stage of the row that shows
    # it with the continuation (``None`` when its create already showed it).
    handle: SpanHandle
    reply_pause: Presentation
    reply_stage: rl.WriteStage | None = None


@dataclass
class _EarlyPlaceholderState:
    """Track an early placeholder until normal response settlement takes ownership."""

    placeholder_sent: bool = False
    settlement_started: bool = False


@dataclass
class _DeliveryProgress:
    """Mutable pre/post-delivery state for one locked response turn."""

    stage_started: bool = False
    failure_reason: str | None = None
    cancelled: bool = False
    delivery_outcome: FinalDeliveryOutcome | None = None

    def note_delivery_started(self, _event_id: str | None = None) -> None:
        """Mark visible delivery as begun; the span's reply names the event carrying it."""
        self.stage_started = True

    def note_task_cancelled(self, failure_reason: str) -> None:
        """Record that the response task was cancelled before delivery settled."""
        self.failure_reason = failure_reason
        self.cancelled = True

    def settle(self, delivery_outcome: FinalDeliveryOutcome) -> None:
        """Record the turn's one canonical terminal delivery outcome."""
        if self.delivery_outcome is not None:
            msg = "Response delivery already settled"
            raise RuntimeError(msg)
        self.delivery_outcome = delivery_outcome


def _raise_if_process_shutdown() -> None:
    """Restore process cancellation when generation code consumed it."""
    if current_task_is_process_shutdown():
        raise asyncio.CancelledError


@dataclass(frozen=True)
class _TeamResponseRequest:
    """The team-specific inputs of one team response request."""

    member_names: tuple[str, ...]
    team_mode: str
    reason_prefix: str = "Team request"
    resolution_reason: str | None = None


def _post_lock_error(error: Exception, early_placeholder: _EarlyPlaceholderState) -> Exception:
    """Return the error a locked response failed with, as a dispatch failure once it showed an early placeholder.

    Retries and dispatch failures are returned as they are.
    """
    if (
        isinstance(
            error,
            (ReplyMembershipPendingError, RevisionSnapshotChangedError, PostLockRequestPreparationError),
        )
        or not early_placeholder.placeholder_sent
        or early_placeholder.settlement_started
    ):
        return error
    mapped = PostLockRequestPreparationError()
    mapped.__cause__ = error
    return mapped


@dataclass(frozen=True)
class ResponseRunnerDeps:
    """Explicit collaborators for the response lifecycle."""

    runtime: BotRuntimeView
    logger: structlog.stdlib.BoundLogger
    runtime_paths: RuntimePaths
    storage_path: Path
    agent_name: str
    matrix_full_id: str
    resolver: ConversationResolver
    tool_runtime: ToolRuntimeSupport
    knowledge_access: KnowledgeAccessSupport
    delivery_gateway: DeliveryGateway
    post_response_effects: PostResponseEffectsSupport
    state_writer: ConversationStateWriter
    request_preparer: ResponsePayloadPreparer
    approval_store: PrincipalStore
    retry_approval_sources: Callable[[str, tuple[str, ...]], None]
    approval_runtime_generation: str
    redacted_history_events: Callable[[MessageTarget, tuple[str, ...]], Awaitable[Mapping[str, str | None]]]
    # This bot instance's owner of durable reply records.
    replies: ReplyRuntime


@dataclass(frozen=True)
class _PreparedResponseRuntime:
    """Resolved runtime context shared by streaming and non-streaming responses."""

    resolved_target: MessageTarget
    media_inputs: MediaInputs
    session_id: str
    model_prompt: str
    active_model_name: str
    show_tool_calls: bool
    tool_dispatch: ToolDispatchContext
    participation: ParticipationGate | None = None
    skill_review_capture: SkillReviewCapture | None = None


@dataclass(frozen=True)
class _LockedTurn:
    """One admitted agent or team turn, prepared: everything its generation and settlement read.

    ``request`` is the turn's request as admitted, which its lifecycle, effects, and approval pause read;
    ``delivery_request`` is the same request with the model-facing history and prompt the stream answers.
    """

    request: ResponseRequest
    delivery_request: ResponseRequest
    # The conversation target the turn's reply answers in, which its lifecycle settles against.
    response_target: MessageTarget
    response_kind: Literal["ai", "team"]
    history_scope: HistoryScope
    execution_identity: ToolExecutionIdentity
    runtime: _PreparedResponseRuntime
    run_id: str
    streaming_cls: type[StreamingResponse]
    # The entity's response stream for one generation, given the room's live replies, its compaction notices, and
    # what the turn writes back.
    stream: Callable[[set[str], MatrixCompactionLifecycle | None, TurnSinks], AsyncIterator[StreamInputChunk]]
    post_response_deps: Callable[[], PostResponseEffectsDeps]
    team_member_names: tuple[str, ...] = ()
    team_mode: str | None = None


def _requested_by_a_person(origin: TurnOrigin) -> bool:
    """Whether a turn counts toward skill learning.

    Like Hermes skipping cron reviews, automated runs, job follow-ups, and replies to other agents never count toward a
    review; they have no human to learn from.
    """
    return (
        origin.requester_kind == SenderKind.USER
        and origin.intent is not TurnIntent.JOB_FOLLOW_UP
        and not is_automation_source_kind(origin.source_kind)
    )


# TODO(simplify): ResponseRunner mixes the locked turn with approval pause, resume, and recovery. Split approval
# handling into focused modules, as teams/ and reply_turn/ were. Contributors only; no behavior change (#3075).
@dataclass
class ResponseRunner:
    """Run one response lifecycle while keeping bot seams patchable."""

    deps: ResponseRunnerDeps
    # Own count, distinct from the shared gate's process-wide total: callers like
    # the todo-poke idle check ask whether *this* entity is busy.
    _in_flight_response_count: int = field(default=0, init=False)
    _lifecycle_coordinator: ResponseLifecycleCoordinator = field(
        default_factory=ResponseLifecycleCoordinator,
        init=False,
    )
    _admission_shutdown_requested: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)
    inbox: InboxResponses = field(init=False, repr=False)
    _approval_responses: ApprovalResponseCoordinator = field(init=False, repr=False)
    _approval_execution: AgentApprovalExecution = field(init=False, repr=False)
    _cli_approval_waits: CliApprovalWaits = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Bind inbox ownership, and response-side approval collaborators to the event journal."""
        self.inbox = InboxResponses(
            logger=self.deps.logger,
            replies=self.deps.replies,
            retry_approval_sources=self.deps.retry_approval_sources,
        )
        self._approval_responses = ApprovalResponseCoordinator(
            config=lambda: self.deps.runtime.config,
            runtime_paths=self.deps.runtime_paths,
            store=self.deps.approval_store,
            delivery_gateway=self.deps.delivery_gateway,
            retry_sources=self.deps.retry_approval_sources,
            finish_approval=self.deps.replies.finish_approval,
            release_approval=self.deps.replies.release_approval,
        )
        self._cli_approval_waits = CliApprovalWaits(
            store=self.deps.approval_store,
            responses=self._approval_responses,
            runtime_generation=self.deps.approval_runtime_generation,
            retry_sources=self.deps.retry_approval_sources,
            claim=self._claim_in_place_approval,
            advance=self._advance_in_place_approval,
        )
        self._approval_execution = AgentApprovalExecution(
            config=lambda: self.deps.runtime.config,
            runtime_paths=self.deps.runtime_paths,
            client=self._client,
            tool_runtime=self.deps.tool_runtime,
            knowledge_access=self.deps.knowledge_access,
            refresh_scheduler=self._knowledge_refresh_scheduler,
        )

    def _knowledge_refresh_scheduler(self) -> KnowledgeRefreshScheduler | None:
        """Return the current orchestrator scheduler when this runner is managed."""
        orchestrator = self.deps.runtime.orchestrator
        return orchestrator.knowledge_refresh_scheduler if orchestrator is not None else None

    def _post_response_deps(
        self,
        request: ResponseRequest,
        *,
        queue_memory_persistence: Callable[[], None] | None = None,
        queue_skill_review: Callable[[str], Awaitable[None]] | None = None,
        persist_response_event_id: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> PostResponseEffectsDeps:
        """Build post-response effect deps bound to one request's room."""
        return self.deps.post_response_effects.build_deps(
            room_id=request.room_id,
            membership_turn_id=request.response_envelope.source_event_id,
            queue_memory_persistence=queue_memory_persistence,
            queue_skill_review=queue_skill_review,
            notify_response_finished=self._response_finished_notifier(request.sources.logical_source_event_ids),
            persist_response_event_id=persist_response_event_id,
        )

    def _budgeted_model(self, request: ResponseRequest, model_name: str) -> str:
        """Return the model this request's requester may use under their budget."""
        orchestrator = self.deps.runtime.orchestrator
        return budget_model(
            self.deps.runtime.config,
            self.deps.runtime_paths,
            orchestrator.budgets if orchestrator is not None else None,
            request.response_envelope.requester_id,
            model_name,
        )

    def _response_finished_notifier(self, source_event_ids: Sequence[str]) -> Callable[[], None] | None:
        """Return the callback that lets automations verify their runs and budgets count the new spend."""
        orchestrator = self.deps.runtime.orchestrator
        if orchestrator is None:
            return None

        def notify() -> None:
            orchestrator.budgets.response_finished()
            orchestrator.automations.response_finished(source_event_ids)

        return notify

    def _client(self) -> nio.AsyncClient:
        """Return the current Matrix client required for response coordination."""
        client = self.deps.runtime.client
        if client is None:
            msg = "Matrix client is not ready for response coordination"
            raise RuntimeError(msg)
        return client

    def _log_delivery_failure(
        self,
        *,
        response_kind: str,
        error: Exception,
    ) -> None:
        """Log one response delivery failure with its raw error text."""
        self.deps.logger.error(
            "Error in response delivery",
            response_kind=response_kind,
            failure_reason=str(error),
            error_type=error.__class__.__name__,
        )

    @property
    def in_flight_response_count(self) -> int:
        """Return the number of active response lifecycles for this entity."""
        return self._in_flight_response_count

    @property
    def _admission_gate(self) -> ResponseAdmissionGate:
        """Return the orchestrator-owned gate deciding whether responses may start."""
        return self.deps.runtime.response_admission_gate

    def resume_pending_admissions(self) -> None:
        """Let a fresh sync-loop generation wait for config apply completion."""
        self.inbox.reopen_admission()
        self._admission_shutdown_requested.clear()

    def refuse_pending_admissions(self) -> None:
        """Wake pre-admission responses whose owning runtime is shutting down."""
        self._admission_shutdown_requested.set()

    async def wait_for_admission_or_shutdown(self) -> bool:
        """Return whether admission reopened before this runtime started shutdown."""
        if self._admission_shutdown_requested.is_set():
            return False
        admission_opened = asyncio.create_task(self._admission_gate.wait_until_open())
        shutdown_requested = asyncio.create_task(self._admission_shutdown_requested.wait())
        try:
            await asyncio.wait(
                (admission_opened, shutdown_requested),
                return_when=asyncio.FIRST_COMPLETED,
            )
            return not self._admission_shutdown_requested.is_set()
        finally:
            for task in (admission_opened, shutdown_requested):
                if not task.done():
                    task.cancel()
            await asyncio.gather(admission_opened, shutdown_requested, return_exceptions=True)

    def _show_tool_calls(self) -> bool:
        """Return tool-call visibility for this agent."""
        return show_tool_calls_for_agent(self.deps.runtime.config, self.deps.agent_name)

    def _turn_show_tool_calls(self) -> bool:
        """Return the tool-call visibility of the current turn: what its reply was claimed with, read once per turn."""
        handle = current_span()
        return handle.base.show_tool_calls if handle is not None else self._show_tool_calls()

    def _thread_summary_message_count_hint(self, request: ResponseRequest) -> int:
        """Return a lower bound on the thread's size after this reply, counted from the request's visible history."""
        return thread_summary_message_count_hint(
            request.thread_history,
            trusted_sender_ids=current_internal_sender_ids(self.deps.runtime.config, self.deps.runtime_paths),
        )

    async def _failed_approval_handoff(
        self,
        approval_id: str,
        *,
        error: BaseException,
    ) -> FinalDeliveryOutcome | None:
        """Return suspension ownership only after failure intent is durable."""
        reason = (
            "Tool approval suspension was cancelled."
            if isinstance(error, asyncio.CancelledError)
            else str(error) or "Tool approval suspension failed."
        )
        continuation = await self._approval_responses.fail_publication(approval_id, reason=reason)
        if continuation is None or continuation.state != "failing":
            return None
        response_event_id = continuation.response_event_id
        return FinalDeliveryOutcome(
            terminal_status="suspended",
            event_id=response_event_id,
            is_visible_response=response_event_id is not None,
            extra_content={STREAM_STATUS_KEY: STREAM_STATUS_APPROVAL_PENDING},
        )

    async def _suspend_for_approval(
        self,
        paused: PausedAttempt,
        *,
        request: ResponseRequest,
        target: MessageTarget,
        execution_identity: ToolExecutionIdentity,
        entity_kind: Literal["agent", "team"],
        history_scope: HistoryScope,
        show_tool_calls: bool,
        team_member_names: tuple[str, ...] = (),
        team_mode: str | None = None,
    ) -> FinalDeliveryOutcome:
        """Persist a paused run, expose its cards, and leave the response lock."""
        requester_id = request.user_id or execution_identity.requester_id
        if requester_id is None:
            msg = "Approval continuation requires the original requester identity"
            raise RuntimeError(msg)
        if paused.cli_call is not None:
            current = await self.deps.approval_store.approval_continuation_for_source(
                request.response_envelope.source_event_id,
            )
            if (
                current is not None
                and current.state in {"waiting", "ready"}
                and current.run_id == paused.run_id
                and current.session_id == paused.session_id
                and current.cli_call == paused.cli_call
            ):
                await self._end_expired_in_place_wait()
                return FinalDeliveryOutcome(
                    terminal_status="suspended",
                    event_id=current.response_event_id,
                    is_visible_response=True,
                    tool_trace=tuple(paused.tool_trace) if show_tool_calls else (),
                )
        require_ordered_pause_presentation(paused, show_tool_calls=show_tool_calls)
        identified_tools = identify_approval_tools(
            paused,
            default_agent_name=self.deps.agent_name,
        )
        approval_id = uuid4().hex
        try:
            plan = await plan_approval_calls(
                identified_tools,
                config=self.deps.runtime.config,
                runtime_paths=self.deps.runtime_paths,
                requester_id=requester_id,
                toolkit_owners=paused.toolkit_owners,
                room_id=target.room_id,
                thread_id=target.resolved_thread_id,
            )
            approval_pending = plan.waiting_text is not None
            visible_tool_trace = tuple(paused.tool_trace) if show_tool_calls else ()
            snapshot_text = paused.response_text
            visible_text = (
                paused.acknowledged_response_text
                if paused.acknowledged_response_text is not None
                else snapshot_text or plan.waiting_text or PROGRESS_PLACEHOLDER
            )
            stream_status = STREAM_STATUS_APPROVAL_PENDING if approval_pending else STREAM_STATUS_PENDING
            shown = await self._show_pause(
                paused,
                waiting_text=plan.waiting_text,
                target=target,
                visible_text=visible_text,
                stream_status=stream_status,
                visible_tool_trace=visible_tool_trace,
            )
            response_event_id = shown.response_event_id
            if response_event_id is None:
                msg = "Could not publish the suspended approval response"
                raise RuntimeError(msg)  # noqa: TRY301

            continuation_state: Literal["waiting", "ready"] = (
                "ready" if all(call.decision is not None for call in plan.calls) else "waiting"
            )
            draft = ApprovalContinuation(
                approval_id=approval_id,
                cli_call=deepcopy(paused.cli_call),
                run_id=paused.run_id,
                session_id=paused.session_id,
                entity_kind=entity_kind,
                entity_name=self.deps.agent_name,
                room_id=target.room_id,
                thread_id=target.resolved_thread_id,
                requester_id=requester_id,
                response_event_id=response_event_id,
                sources=request.sources,
                calls=plan.calls,
                state=continuation_state,
                delegation_storage_bindings=paused.delegation_storage_bindings,
                show_tool_calls=show_tool_calls,
                execution_identity=execution_identity,
                runtime_model_name=paused.runtime_model_name,
                continuation_count=paused.continuation_count,
                team_member_names=team_member_names,
                team_member_model_names=paused.team_member_model_names,
                team_mode=team_mode,
                request_body=request.response_envelope.body,
                attachment_ids=tuple(request.attachment_ids or ()),
                mentioned_agents=request.response_envelope.mentioned_agents,
                hook_source=request.response_envelope.hook_source,
                message_received_depth=request.response_envelope.message_received_depth,
                dispatch_policy_source_kind=request.response_envelope.dispatch_policy_source_kind,
                correlation_id=_correlation_id_for_request(request),
                history_scope=history_scope,
                origin=request.response_envelope.origin,
                memory_prompt=request.prompt,
                memory_thread_history=tuple(
                    ApprovalMemoryTurn(
                        sender=message.sender,
                        body=body_without_tool_markers(message.body, message.content),
                    )
                    for message in request.thread_history
                ),
                thread_summary_message_count_hint=self._thread_summary_message_count_hint(request),
                runtime_generation=(self.deps.approval_runtime_generation if continuation_state == "waiting" else None),
            )
            # Later messages of the conversation wait while the approval is pending, as behind a running reply.
            # It is held before the pause commits, so the approval's end, which releases it, cannot come first.
            self._lifecycle_coordinator.hold_for_approval(approval_id, target)
            if not await self._pause_reply(
                shown.handle,
                draft,
                shown=shown.reply_pause,
                stage=shown.reply_stage,
                # A response-local CLI wait keeps its span running while it waits.
                in_place=paused.cli_call is not None,
                target=target,
                text=visible_text,
                stream_status=stream_status,
                tool_trace=visible_tool_trace,
            ):
                msg = "Could not publish the suspended approval response"
                raise RuntimeError(msg)  # noqa: TRY301
            continuation = await self.deps.approval_store.approval_continuation(approval_id)
            if continuation is None or continuation.state != continuation_state:
                msg = "Approval continuation lost its journal source ownership"
                raise RuntimeError(msg)  # noqa: TRY301

            await self._approval_responses.publish_generation(
                continuation,
                plan,
                target=target,
                failure_reason="Approval card creation failed",
            )
            return FinalDeliveryOutcome(
                terminal_status="suspended",
                event_id=response_event_id,
                is_visible_response=True,
                final_visible_body=shown.final_visible_body,
                delivery_kind=shown.delivery_kind,
                tool_trace=visible_tool_trace,
                extra_content={STREAM_STATUS_KEY: stream_status},
            )
        except (asyncio.CancelledError, Exception) as error:
            handoff = await self._failed_approval_handoff(
                approval_id,
                error=error,
            )
            if await self.deps.approval_store.approval_continuation(approval_id) is None:
                # No approval is pending, or its end already ran: nothing holds the conversation.
                self._lifecycle_coordinator.release_approval_hold(approval_id)
            if handoff is not None:
                return handoff
            raise

    async def _show_pause(
        self,
        paused: PausedAttempt,
        *,
        waiting_text: str | None,
        target: MessageTarget,
        visible_text: str,
        stream_status: str,
        visible_tool_trace: tuple[ToolTraceEntry, ...],
    ) -> _ShownPause:
        """Show a pause before its continuation exists, or prepare the reply row that shows it."""
        shows_text = waiting_text is not None or bool(paused.response_text)
        initial = SendTextRequest(
            target=target,
            response_text=visible_text,
            extra_content={STREAM_STATUS_KEY: stream_status},
            tool_trace=list(visible_tool_trace) or None,
        )
        handle = current_span()
        assert handle is not None, "a pause runs in its reply's span"
        # The reply's records show the pause; its row is written with the continuation.
        reply_pause = _paused_presentation(handle, paused, waiting_text=waiting_text)
        reply = await handle.runtime.store.replies.load(handle.reply_id)
        assert reply is not None, "a span's reply exists while it pauses"
        if reply.event_id is None:
            # The pause is the reply's first visible message, so its create shows it.
            created = await self.deps.delivery_gateway.send_text(
                replace(initial, reply_write=initial_write(handle, reply_pause, placeholder_only=False)),
            )
            return _ShownPause(created, "sent", visible_text, handle=handle, reply_pause=reply_pause)
        if not shows_text:
            return _ShownPause(reply.event_id, None, None, handle=handle, reply_pause=reply_pause)
        return _ShownPause(
            reply.event_id,
            "edited",
            visible_text,
            handle=handle,
            reply_pause=reply_pause,
            reply_stage=rl.WriteStage.EDIT,
        )

    async def _advance_reply_pause(
        self,
        handle: SpanHandle,
        target: MessageTarget,
        advance: ApprovalAdvance,
        paused: PausedAttempt,
        *,
        visible_text: str,
        stream_status: str,
        tool_trace: tuple[ToolTraceEntry, ...],
        waiting_text: str | None,
        in_place: bool = False,
    ) -> tuple[ApprovalContinuation | None, bool]:
        """Advance the continuation and pause the span's reply with the pause row, in one transaction."""
        reply = await handle.runtime.store.replies.load(handle.reply_id)
        assert reply is not None
        assert reply.event_id is not None, "a resumed reply shows its paused event"
        try:
            shown = await self._edit_pause_row(
                handle,
                advance,
                target=target,
                event_id=reply.event_id,
                shown=_paused_presentation(handle, paused, waiting_text=waiting_text),
                in_place=in_place,
                text=visible_text,
                stream_status=stream_status,
                tool_trace=tool_trace,
            )
        except ReplyWriteRefusedError:
            # A Stop committed while the pause was prepared; neither was written.
            return None, False
        current = await self.deps.approval_store.approval_continuation(advance.approval_id)
        if current is None or current.generation != advance.claimant_generation + 1:
            return None, False
        # The pause row committed with the advance: recovery sends it if this send
        # did not land, and only a permanent refusal fails the approval.
        return current, shown or current.state == "waiting"

    async def _claim_in_place_approval(
        self,
        current: ApprovalContinuation,
    ) -> ApprovalContinuation | None:
        """Claim a ready continuation for the response waiting on it, resuming its reply in place."""
        handle = current_span()
        assert handle is not None, "a response-local wait runs in its reply's span"
        claimed, applied = await self.deps.approval_store.claim_approval_in_place(
            current.approval_id,
            runtime_generation=self.deps.approval_runtime_generation,
            reply_id=handle.reply_id,
            span_id=handle.span_id,
        )
        if applied is not None:
            await handle.runtime.committed(applied, handle)
        return claimed

    async def _advance_in_place_approval(
        self,
        current: ApprovalContinuation,
        paused: PausedAttempt,
        target: MessageTarget,
    ) -> None:
        """Record the next pause of a run that waits in place, with its reply's pause row."""
        handle = current_span()
        assert handle is not None, "a response-local wait runs in its reply's span"
        await self._approval_responses.advance_pause(
            current,
            paused,
            target=target,
            pending_text=PROGRESS_PLACEHOLDER,
            reply_pause=partial(self._advance_reply_pause, handle, target, in_place=True),
        )

    async def _end_expired_in_place_wait(self) -> None:
        """End the span of a response-local wait that expired; the reply waits for the decision."""
        handle = current_span()
        assert handle is not None, "a response-local wait runs in its reply's span"
        if handle.exited:
            return
        reply = await handle.runtime.store.replies.load(handle.reply_id)
        assert reply is not None
        handle.note_reply(reply)
        await self.deps.delivery_gateway.end_reply_span(
            handle,
            pause_decision(
                handle,
                decode_presentation(reply.presentation),
                in_place=False,
                stage=None,
            ),
        )

    async def _pause_reply(
        self,
        handle: SpanHandle,
        continuation: ApprovalContinuation,
        *,
        shown: Presentation,
        stage: rl.WriteStage | None,
        in_place: bool,
        target: MessageTarget,
        text: str,
        stream_status: str,
        tool_trace: tuple[ToolTraceEntry, ...],
    ) -> bool:
        """Pause the span's reply and create the continuation that holds its run, in one transaction.

        Returns whether the pause was recorded: its row commits with the
        continuation, so recovery sends a row this send left owed, and only a
        permanent refusal fails the approval. A rule that refuses the pause (a
        Stop committed while it was prepared) raises ``ReplyWriteRefusedError``.
        """
        if stage is None:
            return await self.deps.delivery_gateway.pause_shown_reply(
                handle,
                pause_decision(handle, shown, in_place=in_place, stage=None),
                enqueue=partial(self.deps.approval_store.pause_for_approval, continuation),
            )
        shown_now = await self._edit_pause_row(
            handle,
            continuation,
            target=target,
            event_id=continuation.response_event_id,
            shown=shown,
            in_place=in_place,
            text=text,
            stream_status=stream_status,
            tool_trace=tool_trace,
        )
        return shown_now or await self.deps.approval_store.approval_continuation(continuation.approval_id) is not None

    async def _edit_pause_row(
        self,
        handle: SpanHandle,
        hold: ApprovalContinuation | ApprovalAdvance,
        *,
        target: MessageTarget,
        event_id: str,
        shown: Presentation,
        in_place: bool,
        text: str,
        stream_status: str,
        tool_trace: tuple[ToolTraceEntry, ...],
    ) -> bool:
        """Edit the paused reply with its pause row, recorded with the continuation ``hold`` creates or advances.

        A rule that refuses the pause (a Stop committed while it was prepared) raises ``ReplyWriteRefusedError``.
        """
        return await self.deps.delivery_gateway.edit_text(
            EditTextRequest(
                target=target,
                event_id=event_id,
                new_text=text,
                extra_content={STREAM_STATUS_KEY: stream_status},
                tool_trace=list(tool_trace) or None,
                reply_write=pause_write(
                    handle,
                    shown,
                    in_place=in_place,
                    enqueue=partial(self.deps.approval_store.pause_for_approval, hold),
                ),
            ),
        )

    @asynccontextmanager
    async def _cli_approval_scope(
        self,
        runtime: _PreparedResponseRuntime,
        *,
        request: ResponseRequest,
        progress: _DeliveryProgress,
        history_scope: HistoryScope,
    ) -> AsyncIterator[_PreparedResponseRuntime]:
        """Attach approval publication to this response's real source and placeholder."""
        context = runtime_context_from_dispatch_context(runtime.tool_dispatch)
        if context is None:
            yield runtime
            return
        async with self._cli_approval_handler_scope(
            request=request,
            progress=progress,
            history_scope=history_scope,
            target=runtime.resolved_target,
            execution_identity=runtime.tool_dispatch.execution_identity,
            show_tool_calls=runtime.show_tool_calls,
            settle_terminal=True,
        ) as handler:
            yield replace(
                runtime,
                tool_dispatch=LiveToolDispatchContext.from_runtime_context(
                    replace(context, cli_approval_handler=handler),
                ),
            )

    def _cli_approval_handler_scope(
        self,
        *,
        request: ResponseRequest,
        progress: _DeliveryProgress,
        history_scope: HistoryScope,
        target: MessageTarget,
        execution_identity: ToolExecutionIdentity,
        show_tool_calls: bool,
        settle_terminal: bool,
    ) -> AbstractAsyncContextManager[Callable[[PausedAttempt], Awaitable[tuple[RunRequirement, ...]]]]:
        """Bind this request's approval publication and authorization to its CLI wait scope."""
        return self._cli_approval_waits.scope(
            source_event_ids=request.sources.pending_event_ids,
            progress=progress,
            target=target,
            publish=partial(
                self._suspend_for_approval,
                request=request,
                target=target,
                execution_identity=execution_identity,
                entity_kind="agent",
                history_scope=history_scope,
                show_tool_calls=show_tool_calls,
            ),
            authorize=lambda: self._request_remains_authorized(request),
            settle_terminal=settle_terminal,
        )

    async def _execute_claimed_approval(
        self,
        claimed: ApprovalContinuation,
        *,
        request: ResponseRequest,
        target: MessageTarget,
    ) -> tuple[FinalDeliveryOutcome, ApprovalContinuation]:
        """Run and classify one claimed continuation for either lifecycle entry path."""
        if claimed.cli_call is None:
            return await self._deliver_claimed_approval(claimed, request=request, target=target)
        progress = _DeliveryProgress()
        async with self._cli_approval_handler_scope(
            request=request,
            progress=progress,
            history_scope=claimed.history_scope or self.deps.state_writer.history_scope(),
            target=target,
            execution_identity=self.deps.tool_runtime.build_execution_identity(
                target=target,
                user_id=claimed.requester_id,
            ),
            show_tool_calls=claimed.show_tool_calls,
            settle_terminal=False,
        ) as handler:
            outcome, current = await self._deliver_claimed_approval(
                claimed,
                request=request,
                target=target,
                cli_approval_handler=handler,
            )
            progress.settle(outcome)
            return outcome, current

    async def _deliver_claimed_approval(
        self,
        claimed: ApprovalContinuation,
        *,
        request: ResponseRequest,
        target: MessageTarget,
        cli_approval_handler: Callable[[PausedAttempt], Awaitable[tuple[RunRequirement, ...]]] | None = None,
    ) -> tuple[FinalDeliveryOutcome, ApprovalContinuation]:
        """Deliver execution output against the latest native continuation generation."""
        tool_trace: list[ToolTraceEntry] = []
        identity = self._response_identity(
            request,
            response_kind="team" if claimed.entity_kind == "team" else "ai",
        )
        # Streaming requesters watch the resumed work in the reply it continues.
        # Progress closes before any terminal edit, which stays owned below.
        progress_scope: AbstractAsyncContextManager[ProgressPublisher | None] = (
            self.deps.delivery_gateway.stream_progress(
                target=target,
                event_id=claimed.response_event_id,
                identity=identity,
                show_tool_calls=claimed.show_tool_calls,
                extra_content=_merge_response_extra_content(None, claimed.attachment_ids),
                visible_progress_callback=self._lifecycle_coordinator.visible_progress_callback(target),
            )
            if await should_use_streaming(
                self._client(),
                claimed.room_id,
                requester_user_id=claimed.requester_id,
                enable_streaming=self.deps.runtime.enable_streaming,
            )
            else nullcontext()
        )
        async with progress_scope as progress:
            result = await self._continue_entity_call(
                claimed,
                request=request,
                target=target,
                tool_trace_collector=tool_trace,
                progress=progress,
                cli_approval_handler=cli_approval_handler,
            )
        if isinstance(result, CompletedApprovalRun):
            current = await self.deps.approval_store.approval_continuation(claimed.approval_id) or claimed
            show_tool_calls = claimed.show_tool_calls
            visible_tool_trace = tool_trace if show_tool_calls else []
            return (
                await self.deps.delivery_gateway.deliver_final(
                    FinalDeliveryRequest(
                        target=target,
                        existing_event_id=claimed.response_event_id,
                        response_text=result.response_text,
                        identity=identity,
                        tool_trace=visible_tool_trace if show_tool_calls else None,
                        extra_content=_merge_response_extra_content(result.metadata_content, claimed.attachment_ids),
                    ),
                ),
                current,
            )
        current = await self.deps.approval_store.approval_continuation(claimed.approval_id) or claimed
        if current.state in {"waiting", "ready"}:
            return (
                FinalDeliveryOutcome(
                    terminal_status="suspended",
                    event_id=current.response_event_id,
                    is_visible_response=True,
                    final_visible_body=result.response_text,
                    delivery_kind="edited",
                    extra_content={STREAM_STATUS_KEY: STREAM_STATUS_APPROVAL_PENDING},
                ),
                current,
            )
        handle = current_span()
        assert handle is not None, "an approval resume runs in its reply's resume span"
        presentation = await self._approval_responses.advance_pause(
            current,
            result,
            target=target,
            pending_text=PROGRESS_PLACEHOLDER,
            reply_pause=partial(self._advance_reply_pause, handle, target),
        )
        current = await self.deps.approval_store.approval_continuation(claimed.approval_id) or claimed
        return (
            FinalDeliveryOutcome(
                terminal_status="suspended",
                event_id=claimed.response_event_id,
                is_visible_response=True,
                final_visible_body=presentation.response_text,
                delivery_kind="edited",
                tool_trace=presentation.tool_trace,
                extra_content={
                    STREAM_STATUS_KEY: (
                        STREAM_STATUS_APPROVAL_PENDING if presentation.approval_pending else STREAM_STATUS_PENDING
                    ),
                },
            ),
            current,
        )

    async def _run_claimed_approval_lifecycle(
        self,
        claimed: ApprovalContinuation,
        *,
        target: MessageTarget,
    ) -> FinalDeliveryOutcome:
        """Run one claimed pause through the normal stoppable response lifecycle."""
        request = self._approval_response_request(claimed, target=target)
        await self._refresh_mid_turn_context_for_approval(request)
        progress = _DeliveryProgress()
        progress.note_delivery_started(claimed.response_event_id)
        lifecycle = self._build_lifecycle(
            identity=self._response_identity(
                request,
                response_kind="team" if claimed.entity_kind == "team" else "ai",
            ),
            request=request,
        )
        post_effect_continuation = claimed

        async def continue_response(_message_id: str | None) -> None:
            nonlocal post_effect_continuation
            try:
                outcome, post_effect_continuation = await self._execute_claimed_approval(
                    claimed,
                    request=request,
                    target=target,
                )
            except (asyncio.CancelledError, Exception):
                delivery = await run_coroutine_until_complete(
                    self._approval_responses.final_delivery(claimed, recover=True),
                )
                if delivery is None:
                    raise
                if delivery.acknowledged_event_id is None:
                    progress.settle(
                        FinalDeliveryOutcome(
                            terminal_status="suspended",
                            event_id=claimed.response_event_id,
                            is_visible_response=True,
                            extra_content={STREAM_STATUS_KEY: STREAM_STATUS_APPROVAL_PENDING},
                        ),
                    )
                    return
                progress.settle(await self._approval_outcome_from_delivery(delivery))
                return
            if (
                outcome.terminal_status not in {"completed", "suspended"}
                and await self._approval_responses.final_delivery(
                    claimed,
                )
                is not None
            ):
                outcome = FinalDeliveryOutcome(
                    terminal_status="suspended",
                    event_id=claimed.response_event_id,
                    is_visible_response=True,
                    final_visible_body=outcome.final_visible_body,
                    extra_content={STREAM_STATUS_KEY: STREAM_STATUS_APPROVAL_PENDING},
                )
            progress.settle(outcome)

        await self._run_and_settle_locked_response(
            request,
            target=target,
            lifecycle=lifecycle,
            progress=progress,
            response_function=continue_response,
            user_id=claimed.requester_id,
            build_post_response_outcome=lambda final: self._approval_post_response_outcome(
                post_effect_continuation,
                target=target,
                final=final,
            ),
            post_response_deps=lambda: self._approval_post_response_deps(claimed),
        )
        outcome = progress.delivery_outcome
        if outcome is None:
            msg = "Approval continuation ended without a terminal lifecycle outcome"
            raise RuntimeError(msg)
        if outcome.terminal_status == "completed":
            if not await self.deps.replies.finish_approval(claimed.approval_id):
                msg = "Approval continuation final delivery was not durably acknowledged"
                raise RuntimeError(msg)
        elif outcome.terminal_status != "suspended":
            await self._settle_failed_approval_outcome(post_effect_continuation, outcome)
        return outcome

    async def _settle_failed_approval_outcome(
        self,
        continuation: ApprovalContinuation,
        outcome: FinalDeliveryOutcome,
    ) -> None:
        """Preserve partial text for interrupted continuations, but not explicit stops."""
        reason = outcome.failure_reason or "Tool approval continuation failed safely."
        cancel_source = _approval_interruption_cancel_source(reason)
        if outcome.terminal_status == "cancelled" and cancel_source is not None:
            await self._settle_interrupted_approval_recovery(
                continuation,
                reason=cancel_failure_reason(cancel_source),
                cancel_source=cancel_source,
            )
        else:
            await self._approval_responses.settle_failure(continuation, reason)

    async def _approval_outcome_from_delivery(self, delivery: MatrixDelivery) -> FinalDeliveryOutcome:
        """Restore semantic lifecycle facts from one frozen approval FINAL and the reply it answered."""
        acknowledged_event_id = delivery.acknowledged_event_id
        if acknowledged_event_id is None:
            msg = "Approval final delivery is not acknowledged"
            raise RuntimeError(msg)
        event_id = delivery.edits_event_id or acknowledged_event_id
        payload = dict(delivery.payload)
        nested = payload.get("m.new_content")
        visible = cast("dict[str, Any]", nested) if isinstance(nested, dict) else payload
        semantic = cast("dict[str, object]", delivery.result) if isinstance(delivery.result, dict) else {}
        interactive_metadata = InteractiveMetadata.from_metadata(semantic.get("interactive"))
        reply = None if delivery.reply_id is None else await self.deps.replies.store.replies.load(delivery.reply_id)
        span = None if delivery.span_id is None else await self.deps.replies.store.replies.span(delivery.span_id)
        if reply is None or span is None:
            msg = "Approval final delivery has no reply records"
            raise RuntimeError(msg)
        # What the FINAL showed: the final transform's display when it reshaped the answer.
        body, _trace = render_body(decode_presentation(reply.possibly_shown or reply.presentation))
        return FinalDeliveryOutcome(
            # A frozen FINAL may be the failure or cancellation its span ended with.
            terminal_status=_terminal_status_of_span(span),
            event_id=event_id,
            is_visible_response=True,
            final_visible_body=body,
            delivery_kind="edited" if delivery.edits_event_id is not None else "sent",
            extra_content=visible,
            interactive_metadata=interactive_metadata,
        )

    async def _recover_claimed_approval_lifecycle(
        self,
        claimed: ApprovalContinuation,
        *,
        target: MessageTarget,
    ) -> str | None:
        """Recover frozen FINAL debt without resuming the run a second time."""
        owns_final, event_id = await self._recover_frozen_approval_final(claimed, target=target)
        if owns_final:
            return event_id
        final_delivery = await self._approval_responses.final_delivery(claimed)
        if final_delivery is not None and final_delivery.permanently_failed:
            settled = await self._approval_responses.settle_failure(
                claimed,
                INTERRUPTED_FAILURE_REASON,
            )
            return claimed.response_event_id if settled else None
        return await self._release_interrupted_approval(claimed, reason=INTERRUPTED_FAILURE_REASON)

    async def _release_interrupted_approval(
        self,
        continuation: ApprovalContinuation,
        *,
        reason: str,
    ) -> str | None:
        """Release an approved run a restart cut short, so replay continues its reply or its Stop ends it.

        Before a FINAL the reply is still the unfinished stream of one turn, so
        the replayed turn adopts it like any reply a restart left streaming; a
        Stop the run left unapplied ends it cancelled instead. A
        hand-back that cannot finish yet, such as cards that did not expire, is
        retried by the next recovery pass. A resume that already failed before
        its failure was fenced, or a FINAL already owed, settles the
        continuation as a failure instead.
        """
        claim_span = (
            None
            if continuation.claim_span_id is None
            else await self.deps.replies.store.replies.span(continuation.claim_span_id)
        )
        # A resume that already ended without its answer has nothing for a replay to continue.
        resume_ended = claim_span is not None and claim_span.outcome in _UNANSWERED_RESUME_OUTCOMES
        if resume_ended or await self._approval_responses.final_delivery(continuation) is not None:
            settled = await self._approval_responses.settle_failure(continuation, reason)
            return continuation.response_event_id if settled else None
        await self._approval_responses.release_to_replay(continuation, reason)
        return None

    async def _settle_interrupted_approval_recovery(
        self,
        continuation: ApprovalContinuation,
        *,
        reason: str,
        cancel_source: Literal["sync_restart", "interrupted"],
    ) -> bool:
        """Fence an uncertain stale claim and settle it with the interruption its reply shows."""
        failing = continuation
        if failing.state != "failing":
            requested = await self._approval_responses.request_failure(
                failing,
                reason,
            )
            if requested is None:
                return False
            failing = requested
        # The reply's records know what it showed; Matrix is not read back.
        return await self._approval_responses.settle_failure(
            failing,
            reason,
            interruption=interruption_note(cancel_source),
        )

    async def _recover_frozen_approval_final(
        self,
        claimed: ApprovalContinuation,
        *,
        target: MessageTarget,
    ) -> tuple[bool, str | None]:
        """Complete acknowledged FINAL debt, or retain unacknowledged delivery ownership."""
        delivery = await self._approval_responses.final_delivery(claimed, recover=True)
        if delivery is None:
            return False, None
        if delivery.permanently_failed:
            return False, None
        if delivery.acknowledged_event_id is None:
            return True, None
        recovered_outcome = await self._approval_outcome_from_delivery(delivery)
        request = self._approval_response_request(claimed, target=target)
        lifecycle = self._build_lifecycle(
            identity=self._response_identity(
                request,
                response_kind="team" if claimed.entity_kind == "team" else "ai",
            ),
            request=request,
        )
        await lifecycle.finalize(
            recovered_outcome,
            build_post_response_outcome=lambda final: self._approval_post_response_outcome(
                claimed,
                target=target,
                final=final,
            ),
            post_response_deps=lambda: self._approval_post_response_deps(claimed),
        )
        if not await self.deps.replies.finish_approval(claimed.approval_id):
            msg = "Recovered approval final delivery lost its journal ownership"
            raise RuntimeError(msg)
        return True, recovered_outcome.event_id

    async def _recover_or_request_claimed_failure(
        self,
        claimed: ApprovalContinuation,
        *,
        target: MessageTarget,
        reason: str,
    ) -> tuple[bool, str | None, ApprovalContinuation | None]:
        """Recover frozen success, or durably fence the observed claim after a recovery error."""
        try:
            owns_final, event_id = await self._recover_frozen_approval_final(claimed, target=target)
        except Exception:
            self.deps.logger.exception(
                "approval_final_recovery_failed_before_failure_fence",
                approval_id=claimed.approval_id,
            )
            owns_final, event_id = False, None
        failing = None if owns_final else await self._approval_responses.request_failure(claimed, reason)
        return owns_final, event_id, failing

    def _approval_response_request(
        self,
        continuation: ApprovalContinuation,
        *,
        target: MessageTarget,
    ) -> ResponseRequest:
        """Rebuild the original response identity for resumed hooks and post-effects."""
        envelope = MessageEnvelope(
            source_event_id=continuation.source_event_ids[0],
            target=target,
            body=continuation.request_body,
            attachment_ids=continuation.attachment_ids,
            mentioned_agents=continuation.mentioned_agents,
            agent_name=continuation.entity_name,
            origin=continuation.origin,
            hook_source=continuation.hook_source,
            message_received_depth=continuation.message_received_depth,
            dispatch_policy_source_kind=continuation.dispatch_policy_source_kind,
        )
        return ResponseRequest(
            thread_history=self._approval_memory_history(continuation),
            prompt=envelope.body,
            response_envelope=envelope,
            sources=continuation.sources,
            existing_event_id=continuation.response_event_id,
            user_id=continuation.requester_id,
            attachment_ids=continuation.attachment_ids,
            correlation_id=continuation.correlation_id,
            participating_agent_names=continuation.team_member_names or (continuation.entity_name,),
        )

    def _approval_post_response_outcome(
        self,
        continuation: ApprovalContinuation,
        *,
        target: MessageTarget,
        final: FinalDeliveryOutcome,
    ) -> ResponseOutcome:
        """Build normal post-response facts for one resumed native run."""
        return ResponseOutcome(
            response_run_id=final.response_run_id or continuation.run_id,
            run_succeeded=final.terminal_status == "completed",
            response_target=target,
            thread_summary_message_count_hint=continuation.thread_summary_message_count_hint,
            thread_summary_entity_name=continuation.entity_name,
        )

    def _approval_post_response_deps(self, continuation: ApprovalContinuation) -> PostResponseEffectsDeps:
        """Build normal post-response dependencies for a continued run."""
        return self.deps.post_response_effects.build_deps(
            room_id=continuation.room_id,
            membership_turn_id=continuation.source_event_ids[0],
            queue_memory_persistence=self._approval_memory_persistence(continuation),
            # A continuation keeps its turn's source events, so a run paused for approval is verified when it ends.
            notify_response_finished=self._response_finished_notifier(continuation.sources.logical_source_event_ids),
            persist_response_event_id=self._approval_response_event_persistence(continuation),
        )

    @staticmethod
    def _approval_memory_history(
        continuation: ApprovalContinuation,
    ) -> tuple[ResolvedVisibleMessage, ...]:
        """Rebuild the sender/body history consumed by conversation memory."""
        return tuple(
            ResolvedVisibleMessage.synthetic(
                sender=turn.sender,
                body=turn.body,
                event_id=f"$approval-memory-{continuation.approval_id}-{index}",
                thread_id=continuation.thread_id,
            )
            for index, turn in enumerate(continuation.memory_thread_history)
        )

    def _response_skill_review(
        self,
        request: ResponseRequest,
        runtime: _PreparedResponseRuntime,
        *,
        session_id: str,
        execution_identity: ToolExecutionIdentity | None,
    ) -> tuple[Callable[[str], Coroutine[Any, Any, None]] | None, _PreparedResponseRuntime]:
        """Stop the conversation's running review, and return a person's response's skill-review handoff.

        Like Hermes, a response starting in a conversation stops its running review. The returned runtime records the
        response's final request for the review to fork.
        """
        config = self.deps.runtime.config
        orchestrator = self.deps.runtime.orchestrator
        agent = config.agents.get(self.deps.agent_name)
        if orchestrator is None or agent is None or not agent.skill_learning.enabled:
            return None, runtime
        reviews = orchestrator.skill_reviews
        agent_name = self.deps.agent_name
        reviews.cancel(config, agent_name=agent_name, session_id=session_id, identity=execution_identity)
        if not _requested_by_a_person(request.response_envelope.origin):
            return None, runtime
        capture = SkillReviewCapture()

        async def count(run_id: str) -> None:
            await reviews.count(
                config,
                agent_name=agent_name,
                session_id=session_id,
                identity=execution_identity,
                run_id=run_id,
                captured=capture.latest,
                correlation_id=_correlation_id_for_request(request),
            )

        return count, replace(runtime, skill_review_capture=capture)

    def _approval_memory_persistence(self, continuation: ApprovalContinuation) -> Callable[[], None] | None:
        """Return the normal memory handoff for a completed continuation."""
        if continuation.entity_kind == "agent":
            agent_name: str | list[str] = continuation.entity_name
        elif continuation.entity_name in self.deps.runtime.config.teams:
            agent_name = list(continuation.team_member_names)
        else:
            return None
        return self._memory_persistence(
            agent_name=agent_name,
            session_id=continuation.session_id,
            execution_identity=continuation.execution_identity,
            prompt=(
                continuation.memory_prompt if continuation.memory_prompt is not None else continuation.request_body
            ),
            thread_history=self._approval_memory_history(continuation),
            user_id=continuation.requester_id,
            hook_source=continuation.hook_source,
            origin=continuation.origin,
        )

    def _memory_persistence(
        self,
        *,
        agent_name: str | list[str],
        session_id: str,
        execution_identity: ToolExecutionIdentity,
        prompt: str,
        thread_history: Sequence[ResolvedVisibleMessage],
        user_id: str | None,
        hook_source: str | None,
        origin: TurnOrigin,
    ) -> Callable[[], None] | None:
        """Build the completed-turn Mem0 or team memory handoff, or None for a turn whose prompt is not memory."""
        # An automation's maintenance turn, such as an unreviewed memory proposal, is not memory, and a job follow-up's
        # prompt is MindRoom's instruction to collect the results, not conversation to extract.
        if is_automation_hook_source(hook_source) or origin.intent is TurnIntent.JOB_FOLLOW_UP:
            return None
        # A file-memory agent's conversation is saved by auto-flush from stored sessions; a team's goes to its shared
        # memory.
        if isinstance(agent_name, str) and self.deps.runtime.config.resolve_entity(agent_name).memory_backend != "mem0":
            return None

        def queue() -> None:
            create_background_task(
                store_conversation_memory(
                    prompt,
                    agent_name,
                    self.deps.storage_path,
                    session_id,
                    self.deps.runtime.config,
                    self.deps.runtime_paths,
                    thread_history,
                    user_id,
                    execution_identity=execution_identity,
                ),
                name=f"memory_save_{agent_name if isinstance(agent_name, str) else 'team'}_{session_id}",
                owner=self.deps.runtime,
            )

        return queue

    def _approval_response_event_persistence(
        self,
        continuation: ApprovalContinuation,
    ) -> Callable[[str, str], Awaitable[None]] | None:
        """Return the normal run-to-Matrix event linkage for a resumed response."""
        history_scope = continuation.history_scope
        if history_scope is None:
            if continuation.entity_kind == "team":
                return None
            history_scope = self.deps.state_writer.history_scope()
        return self._build_persist_response_event_id_effect(
            session_id=continuation.session_id,
            scope=history_scope,
            execution_identity=continuation.execution_identity,
        )

    async def _continue_entity_call(
        self,
        continuation: ApprovalContinuation,
        *,
        request: ResponseRequest,
        target: MessageTarget,
        tool_trace_collector: list[ToolTraceEntry],
        progress: ProgressPublisher | None,
        cli_approval_handler: Callable[[PausedAttempt], Awaitable[tuple[RunRequirement, ...]]] | None,
    ) -> CompletedApprovalRun | PausedAttempt:
        execution_identity = continuation.execution_identity
        tool_dispatch = self.deps.tool_runtime.build_dispatch_context(
            target,
            user_id=continuation.requester_id,
            agent_name=continuation.entity_name,
            active_model_name=continuation.runtime_model_name,
            attachment_ids=continuation.attachment_ids,
            correlation_id=_correlation_id_for_request(request),
            source_envelope=request.response_envelope,
        )
        if tool_dispatch.execution_identity != execution_identity:
            msg = "Approval continuation execution identity no longer matches its target"
            raise RuntimeError(msg)
        if cli_approval_handler is not None:
            context = runtime_context_from_dispatch_context(tool_dispatch)
            if context is None:
                msg = "CLI approval recovery requires a live tool runtime"
                raise RuntimeError(msg)
            tool_dispatch = LiveToolDispatchContext.from_runtime_context(
                replace(context, cli_approval_handler=cli_approval_handler),
            )
        decisions = {call.tool_call_id: call.decision is ContinuationDecision.APPROVED for call in continuation.calls}
        denial_reasons = {call.tool_call_id: call.reason for call in continuation.calls}
        paused_answer = _paused_answer()
        with approval_receipt_context((*continuation.earlier_calls, *continuation.calls), run_id=continuation.run_id):
            if continuation.entity_kind == "team":

                async def continue_team() -> CompletedApprovalRun | PausedAttempt:
                    return await continue_paused_team_run(
                        member_names=continuation.team_member_names,
                        mode=TeamMode(continuation.team_mode or "coordinate"),
                        config=self.deps.runtime.config,
                        runtime_paths=self.deps.runtime_paths,
                        execution_identity=execution_identity,
                        session_id=continuation.session_id,
                        run_id=continuation.run_id,
                        user_id=continuation.requester_id,
                        configured_team_name=continuation.entity_name,
                        model_name=select_model_for_team(
                            continuation.entity_name,
                            continuation.room_id,
                            self.deps.runtime.config,
                            self.deps.runtime_paths,
                            thread_id=continuation.thread_id,
                        )
                        if continuation.runtime_model_name is None
                        else continuation.runtime_model_name,
                        decisions=decisions,
                        denial_reasons=denial_reasons,
                        refresh_scheduler=self._knowledge_refresh_scheduler(),
                        member_model_names=dict(continuation.team_member_model_names) or None,
                        approval_calls=continuation.calls,
                        history_scope=continuation.history_scope,
                        prior_tool_trace=paused_answer.tool_trace,
                        prior_presentation_state=paused_answer.team_state,
                        show_tool_calls=continuation.show_tool_calls,
                        tool_trace_collector=tool_trace_collector,
                        progress=progress,
                    )

                async with _response_typing_indicator(
                    self._client(),
                    request,
                    response_run_id=continuation.run_id,
                ):
                    response_text = await self._run_in_tool_context(
                        tool_dispatch=tool_dispatch,
                        operation=continue_team,
                    )
            else:
                response_text = await self._approval_execution.continue_run(
                    continuation,
                    paused_answer=paused_answer,
                    tool_dispatch=tool_dispatch,
                    decisions=decisions,
                    denial_reasons=denial_reasons,
                    tool_trace_collector=tool_trace_collector,
                    typing_log_context=_response_typing_log_context(
                        request,
                        response_run_id=continuation.run_id,
                    ),
                    progress=progress,
                )
        return response_text

    def _note_final_delivery_timing(self, request: ResponseRequest, delivery: FinalDeliveryOutcome) -> None:
        """Mark the terminal visible reply and response completion for one delivery."""
        if request.pipeline_timing is not None:
            request.pipeline_timing.mark_first_visible_reply(
                "final",
                substantive=delivery.delivered_substantive_content,
            )
            request.pipeline_timing.mark("response_complete")

    async def _settle_blocking_cancellation(
        self,
        exc: asyncio.CancelledError,
        *,
        message_id: str | None,
        restart_message: str,
        user_stop_message: str,
        interrupted_message: str,
    ) -> FinalDeliveryOutcome:
        """Settle one blocking-mode cancellation through the visible note or a no-event outcome."""
        cancel_source = classify_cancel_source(exc)
        log_cancelled_response(
            self.deps.logger,
            exc=exc,
            message_id=message_id,
            restart_message=restart_message,
            user_stop_message=user_stop_message,
            interrupted_message=interrupted_message,
        )
        if message_id:
            return self.deps.delivery_gateway.cancelled_outcome(message_id, cancel_source)
        return self.deps.delivery_gateway.terminal_outcome_without_visible_event(
            terminal_status="cancelled",
            failure_reason=cancel_failure_reason(cancel_source),
        )

    async def _finalize_streamed_turn(
        self,
        *,
        request: ResponseRequest,
        delivery_target: MessageTarget,
        transport_outcome: StreamTransportOutcome,
        delivery_kind: Literal["sent", "edited"],
        response_identity: ResponseIdentity,
        tool_trace: list[Any] | None,
        extra_content: dict[str, Any] | None,
    ) -> FinalDeliveryOutcome:
        """Finalize one streamed delivery and mark the terminal delivery timing."""
        delivery = await self.deps.delivery_gateway.finalize_streamed_response(
            FinalizeStreamedResponseRequest(
                target=delivery_target,
                stream_transport_outcome=transport_outcome,
                initial_delivery_kind=delivery_kind,
                identity=response_identity,
                tool_trace=tool_trace,
                extra_content=extra_content,
                existing_event_id=_existing_event_id(request),
                existing_event_is_placeholder=_replaceable_placeholder(request),
            ),
        )
        self._note_final_delivery_timing(request, delivery)
        return delivery

    def has_active_response_for_target(self, target: MessageTarget) -> bool:
        """Return whether one canonical conversation target already has an active turn."""
        return self._lifecycle_coordinator.has_active_response_for_target(target)

    def active_thread_ids_for_room(self, room_id: str) -> frozenset[str | None]:
        """Return canonical thread IDs with active response lifecycles in one room."""
        return self._lifecycle_coordinator.active_thread_ids_for_room(room_id)

    def hold_for_approval(self, continuation: ApprovalContinuation) -> None:
        """Keep a pending approval's conversation busy, as a restart finds it."""
        self._lifecycle_coordinator.hold_for_approval(continuation.approval_id, continuation_target(continuation))

    def release_approval_hold(self, approval_id: str) -> None:
        """Let the conversation of an approval that ended answer its waiting messages."""
        self._lifecycle_coordinator.release_approval_hold(approval_id)

    def release_approval_holds_in_room(self, room_id: str) -> None:
        """Release the approval holds of a room this bot left."""
        self._lifecycle_coordinator.release_approval_holds_in_room(room_id)

    def is_held_for_approval(self, target: MessageTarget) -> bool:
        """Return whether a pending approval keeps this conversation busy."""
        return bool(self._lifecycle_coordinator.approval_holds(target.room_id, target.resolved_thread_id))

    async def wait_for_thread_response_idle(self, room_id: str, thread_id: str | None) -> None:
        """Wait until one canonical room/thread has no active response turn.

        While it waits it rechecks that each approval holding the conversation
        still exists, so a hold whose end never reached this bot, as after a
        departure it did not see, cannot keep it waiting. A recheck that lands
        while a pause is still committing can take the new approval for an
        ended one and drop its hold, so messages that arrive while that
        approval waits are answered without waiting for it.
        """
        while True:
            try:
                await asyncio.wait_for(
                    self._lifecycle_coordinator.wait_for_thread_idle(room_id, thread_id),
                    timeout=_APPROVAL_HOLD_RECHECK_SECONDS,
                )
            except TimeoutError:
                await self._release_ended_approval_holds(room_id, thread_id)
                continue
            return

    async def _release_ended_approval_holds(self, room_id: str, thread_id: str | None) -> None:
        """Release the holds of a conversation whose approvals no longer exist."""
        for approval_id in self._lifecycle_coordinator.approval_holds(room_id, thread_id):
            try:
                ended = await self.deps.approval_store.approval_continuation(approval_id) is None
            except Exception:
                # The journal is unreachable: keep waiting, and recheck on the next interval.
                self.deps.logger.warning("approval_hold_recheck_failed", approval_id=approval_id, exc_info=True)
                return
            if ended:
                self._lifecycle_coordinator.release_approval_hold(approval_id)

    def reserve_waiting_human_message(
        self,
        *,
        target: MessageTarget,
        response_envelope: MessageEnvelope,
    ) -> QueuedHumanNoticeReservation | None:
        """Reserve a queued-human notice for an active response before dispatch owns ingress."""
        return self._lifecycle_coordinator.reserve_waiting_human_message(
            target=target,
            response_envelope=response_envelope,
        )

    async def reserve_response_lifecycle(
        self,
        response_envelope: MessageEnvelope,
    ) -> ResponseLifecycleReservation:
        """Reserve one canonical response lifecycle before detached preparation starts."""
        return await self._lifecycle_coordinator.reserve_response_lifecycle(response_envelope)

    async def _run_in_tool_context(
        self,
        *,
        tool_dispatch: ToolDispatchContext,
        operation: Callable[[], Awaitable[_ToolContextResult]],
    ) -> _ToolContextResult:
        """Execute one operation inside the response-owned execution and tool context."""
        return await self.deps.tool_runtime.run_in_context(
            tool_context=runtime_context_from_dispatch_context(tool_dispatch),
            operation=lambda: run_with_tool_execution_identity(
                tool_dispatch.execution_identity,
                operation=operation,
            ),
        )

    def _stream_in_tool_context(
        self,
        *,
        tool_dispatch: ToolDispatchContext,
        stream_factory: Callable[[], AsyncIterator[_ToolStreamChunk]],
    ) -> AsyncIterator[_ToolStreamChunk]:
        """Wrap one stream inside the response-owned execution and tool context."""
        return self.deps.tool_runtime.stream_in_context(
            tool_context=runtime_context_from_dispatch_context(tool_dispatch),
            stream_factory=lambda: stream_with_tool_execution_identity(
                tool_dispatch.execution_identity,
                stream_factory=stream_factory,
            ),
        )

    async def _active_response_event_ids(self, room_id: str) -> set[str]:
        """Return the events of one room's replies whose span runs in this bot instance."""
        return set(await self.deps.replies.live_event_ids(room_id))

    async def _run_locked_response_lifecycle(
        self,
        request: ResponseRequest,
        *,
        response_kind: str,
        locked_operation: Callable[[MessageTarget, _EarlyPlaceholderState], Awaitable[str | None]],
        signal_queued_message: bool = True,
    ) -> str | None:
        """Admit one response before lifecycle locking or visible placeholder work."""
        admission_deferred = False
        while not self._admission_gate.admit():
            if not admission_deferred:
                admission_deferred = True
                self.deps.logger.info(
                    "response_deferred_during_replacement",
                    response_kind=response_kind,
                    **request.response_envelope.target.log_context,
                )
            if not await self.wait_for_admission_or_shutdown():
                self.deps.logger.warning(
                    "response_refused_after_runtime_replacement",
                    response_kind=response_kind,
                    **request.response_envelope.target.log_context,
                )
                raise ResponseAdmissionRefusedError
        identity = ActiveResponseIdentity(
            responder=(
                f"team/{self.deps.agent_name}"
                if self.deps.agent_name in self.deps.runtime.config.teams
                else self.deps.agent_name
            ),
            requester_id=request.response_envelope.requester_id,
        )
        self._admission_gate.response_identities.add(identity)
        self._in_flight_response_count += 1

        async def acknowledge_deferred(key: str, event_id: str) -> None:
            await self.deps.delivery_gateway.send_judgment_reaction(
                turn_id=request.response_envelope.source_event_id,
                room_id=request.room_id,
                event_id=event_id,
                key=key,
                kind="mid_turn_defer",
            )

        try:
            resolved_target = request.response_envelope.target
            early_placeholder = _EarlyPlaceholderState()
            async with self.deps.replies.span_scope():
                return await self._run_locked_response_in_span(
                    request,
                    resolved_target=resolved_target,
                    early_placeholder=early_placeholder,
                    locked_operation=locked_operation,
                    signal_queued_message=signal_queued_message,
                    acknowledge_deferred=acknowledge_deferred,
                )
        finally:
            self._admission_gate.response_identities.remove(identity)
            self._in_flight_response_count -= 1
            self._admission_gate.release()

    async def _run_locked_response_in_span(
        self,
        request: ResponseRequest,
        *,
        resolved_target: MessageTarget,
        early_placeholder: _EarlyPlaceholderState,
        locked_operation: Callable[[MessageTarget, _EarlyPlaceholderState], Awaitable[str | None]],
        signal_queued_message: bool,
        acknowledge_deferred: Callable[[str, str], Awaitable[None]],
    ) -> str | None:
        """Run the locked response and end any span it claimed that has not ended itself.

        Early failures are mapped to their outcomes first.
        """
        try:
            result = await self._lifecycle_coordinator.run_locked_response(
                target=resolved_target,
                response_envelope=request.response_envelope,
                pipeline_timing=request.pipeline_timing,
                mid_turn_gate=_mid_turn_for_request(
                    request,
                    self.deps.runtime.config,
                    self.deps.runtime_paths,
                    on_defer=acknowledge_deferred,
                ),
                locked_operation=lambda target: self._run_locked_ending_span(
                    request,
                    target=target,
                    early_placeholder=early_placeholder,
                    locked_operation=locked_operation,
                ),
                signal_queued_message=signal_queued_message and not _is_silent_schedule_response(request),
            )
        except BaseException as error:
            handle = current_span()
            mapped = _post_lock_error(error, early_placeholder) if isinstance(error, Exception) else error
            # The locked operation ends its span before the lock is released;
            # this covers what raises outside it.
            await self._exit_unended_span(handle, mapped, target=resolved_target)
            if mapped is error:
                raise
            raise mapped from mapped.__cause__
        handle = current_span()
        if handle is not None and not handle.exited:
            # Every exit path is meant to end its span; one that did not is a bug,
            # and releasing keeps the reply's sources for a retry.
            self.deps.logger.error("reply_span_left_unended", span_id=handle.span_id, reply_id=handle.reply_id)
            await self.deps.delivery_gateway.end_reply_span(handle, release_decision(handle))
        return result

    async def _run_locked_ending_span(
        self,
        request: ResponseRequest,
        *,
        target: MessageTarget,
        early_placeholder: _EarlyPlaceholderState,
        locked_operation: Callable[[MessageTarget, _EarlyPlaceholderState], Awaitable[str | None]],
    ) -> str | None:
        """Run the locked response, ending a span it left running before the conversation lock is released.

        A claim waiting for the lock must not find that span still current.
        """
        try:
            return await self._run_owned_or_locked_response(
                request,
                target=target,
                early_placeholder=early_placeholder,
                locked_operation=locked_operation,
            )
        except BaseException as error:
            handle = current_span()
            mapped = _post_lock_error(error, early_placeholder) if isinstance(error, Exception) else error
            await self._exit_unended_span(handle, mapped, target=target)
            if mapped is error:
                raise
            raise mapped from mapped.__cause__

    async def _run_owned_or_locked_response(
        self,
        request: ResponseRequest,
        *,
        target: MessageTarget,
        early_placeholder: _EarlyPlaceholderState,
        locked_operation: Callable[[MessageTarget, _EarlyPlaceholderState], Awaitable[str | None]],
    ) -> str | None:
        """Keep executable approval generations inside their response's lifecycle."""
        owned = await self.deps.approval_store.approval_continuation_for_source(
            request.response_envelope.source_event_id,
        )
        if owned is None:
            event_id = await locked_operation(target, early_placeholder)
            owned = await self.deps.approval_store.approval_continuation_for_source(
                request.response_envelope.source_event_id,
            )
            if owned is None or owned.state != "ready":
                return event_id
        while True:
            self.deps.logger.info(
                "response_source_owned_by_approval_continuation",
                source_event_id=request.response_envelope.source_event_id,
                approval_id=owned.approval_id,
                approval_state=owned.state,
            )
            recovered, event_id = await self._recover_nonready_approval(owned, target=target)
            if recovered:
                return event_id
            if not is_sender_allowed_for_entity_replies_in_room(
                owned.requester_id,
                _reply_authorization_entity_names(
                    self.deps.runtime.config,
                    owned.entity_name,
                    owned.team_member_names,
                ),
                self.deps.runtime.config,
                owned.room_id,
                self.deps.runtime_paths,
                self.deps.runtime.agent_reply_memberships,
                require_resolved_membership=True,
            ):
                return await self._settle_unauthorized_approval_continuation(owned)
            # Each claimed generation runs in its own resume span.
            async with self.deps.replies.span_scope() as resume_slot:
                claimed = await self._claim_owned_approval(owned, slot=resume_slot)
                if claimed is None:
                    return None
                event_id = await self._run_owned_approval_continuation(claimed, target=target)
            owned = await self.deps.approval_store.approval_continuation_for_source(
                request.response_envelope.source_event_id,
            )
            if (
                owned is None
                or owned.approval_id != claimed.approval_id
                or owned.state != "ready"
                or owned.generation <= claimed.generation
            ):
                return event_id

    async def _exit_unended_span(
        self,
        handle: SpanHandle | None,
        error: BaseException,
        *,
        target: MessageTarget,
    ) -> None:
        """End a claimed span the error left running.

        A process stop leaves the span exactly as a crash would; the next bot
        instance ends it as lost and replays its sources.
        """
        if handle is not None and not handle.exited and not current_task_is_process_shutdown():
            await run_coroutine_until_complete(self._exit_span_on_error(handle, error, target=target))

    async def _exit_span_on_error(self, handle: SpanHandle, error: BaseException, *, target: MessageTarget) -> None:
        """End a span whose locked response raised before ending it."""
        gateway = self.deps.delivery_gateway
        confirms = handle.unconfirmed_progress
        now_ns = time.time_ns()
        if isinstance(error, asyncio.CancelledError):
            await gateway.end_interrupted_span(
                handle,
                target,
                cancel_source=classify_cancel_source(error),
                failure_reason=None,
                delivery_started=False,
            )
            return
        if isinstance(error, (ReplyMembershipPendingError, RevisionSnapshotChangedError)):
            await gateway.end_reply_span(handle, release_decision(handle, outcome=rl.SpanOutcome.SUPERSEDED))
            return
        if isinstance(error, PostLockRequestPreparationError):
            cause = error.__cause__ if isinstance(error.__cause__, Exception) else error
            error_text = get_user_friendly_error_message(
                cause,
                self.deps.agent_name,
                runtime_paths=self.deps.runtime_paths,
            )
            await gateway.end_reply_span(
                handle,
                lambda reply, span: rl.dispatch_failed(
                    rl.confirm_progress(reply, confirms),
                    span,
                    error_text=error_text,
                    now_ns=now_ns,
                ),
            )
            return
        await gateway.end_reply_span(handle, release_decision(handle))

    async def _claim_reply_span(
        self,
        request: ResponseRequest,
        *,
        history_scope: HistoryScope,
    ) -> ResponseRequest | None:
        """Claim the reply this request answers, after the first source gate passed.

        ``None`` means no span opened: the reply cannot be claimed yet, and the
        claim retries the sources once what blocks it resolves; or nothing runs
        for them, as when the reply already answered the edit or a Stop ended it.
        """
        slot = current_slot()
        if slot is None:
            return request
        if request.follow_up_key is not None and not await self._follow_up_due(request):
            return None
        handle = await self.deps.replies.claim(
            delivery_id=request.response_envelope.source_event_id,
            sources=request.sources,
            room_id=request.room_id,
            thread_id=request.thread_id,
            placeholder=TEAM_PLACEHOLDER if history_scope.kind == "team" else AGENT_PLACEHOLDER,
            show_tool_calls=self._show_tool_calls(),
            driving_edit_id=request.response_envelope.source_event_id if request.edit_regeneration else None,
            existing_event_id=request.existing_event_id,
            interactive_span_id=request.interactive_span_id,
        )
        if handle is ClaimRefused.NOTHING_TO_RUN:
            self.deps.logger.info(
                "reply_claim_nothing_to_run",
                source_event_id=request.response_envelope.source_event_id,
            )
            return None
        if isinstance(handle, ClaimRefused):
            # The sources stay pending: the instance that took the replies over
            # replays them, or the resolution of the reply's earlier writes retries them.
            if request.source_handoff is not None:
                request.source_handoff.set()
            return None
        slot.handle = handle
        return request

    async def _settle_unauthorized_approval_continuation(
        self,
        continuation: ApprovalContinuation,
    ) -> str | None:
        reason = "Current authorization no longer permits this tool approval continuation."
        failing = await self._approval_responses.request_failure(continuation, reason)
        if failing is None:
            return None
        settled = await self._approval_responses.settle_failure(failing, reason)
        return continuation.response_event_id if settled else None

    async def _run_owned_approval_continuation(
        self,
        claimed: ApprovalContinuation,
        *,
        target: MessageTarget,
    ) -> str | None:
        try:
            outcome = await self._run_claimed_approval_lifecycle(claimed, target=target)
        except asyncio.CancelledError as error:
            # A shutdown that hands this run to a successor runtime records it as
            # restart-interrupted and leaves the span as a crash would, for that
            # runtime to hand back to replay. Read here: the detached task that
            # ends the span is no shutdown task.
            process_shutdown = current_task_is_process_shutdown()
            reason = (
                INTERRUPTED_FAILURE_REASON if process_shutdown else cancel_failure_reason(classify_cancel_source(error))
            )
            if not process_shutdown:
                await run_coroutine_until_complete(self._end_live_resume_span())
            owns_final, event_id, _failing = await run_coroutine_until_complete(
                self._recover_or_request_claimed_failure(
                    claimed,
                    target=target,
                    reason=reason,
                ),
            )
            if owns_final:
                return event_id
            raise
        except Exception as error:
            reason = str(error) or "Tool approval continuation failed safely."
            if not current_task_is_process_shutdown():
                await self._end_live_resume_span()
            owns_final, event_id, failing = await self._recover_or_request_claimed_failure(
                claimed,
                target=target,
                reason=reason,
            )
            if owns_final:
                return event_id
            self.deps.logger.exception(
                "approval_continuation_failed",
                approval_id=claimed.approval_id,
            )
            if failing is not None:
                settled = await self._approval_responses.settle_failure(failing, reason)
                event_id = claimed.response_event_id if settled else None
            else:
                event_id = None
        else:
            retained = await self.deps.approval_store.approval_continuation(claimed.approval_id)
            event_id = outcome.event_id if outcome.terminal_status != "suspended" and retained is None else None
        return event_id

    async def _end_live_resume_span(self) -> None:
        """End a resume span its lifecycle left running, before the continuation's failure settles the reply."""
        handle = current_span()
        if handle is None or handle.exited or handle.span.kind is not rl.SpanKind.APPROVAL_RESUME:
            return
        now_ns = time.time_ns()
        # The rules leave a resume span's end to its approval, whether a Stop or a failure ended it.
        await self.deps.delivery_gateway.end_reply_span(
            handle,
            lambda reply, span: rl.fail(reply, span, None, now_ns=now_ns),
        )

    async def _claim_owned_approval(
        self,
        owned: ApprovalContinuation,
        *,
        slot: SpanSlot,
    ) -> ApprovalContinuation | None:
        """Claim a ready continuation together with its reply's resume span."""
        claimed, handle = await self.deps.replies.claim_approval_resume(
            owned,
            placeholder=TEAM_PLACEHOLDER if owned.entity_kind == "team" else AGENT_PLACEHOLDER,
        )
        slot.handle = handle
        return claimed

    async def _resume_approval_source(self, source_event_id: str) -> None:
        """Resume journal-owned approval work before normal ingress can reinterpret its source."""
        continuation = await self.deps.approval_store.approval_continuation_for_source(source_event_id)
        if continuation is None:
            return
        target = continuation_target(continuation, reply_to_event_id=source_event_id)
        request = self._approval_response_request(continuation, target=target)

        async def ownership_disappeared(
            _target: MessageTarget,
            _early_placeholder: _EarlyPlaceholderState,
        ) -> str | None:
            return None

        await self._run_locked_response_lifecycle(
            request,
            response_kind="team" if continuation.entity_kind == "team" else "ai",
            locked_operation=ownership_disappeared,
            signal_queued_message=False,
        )

    async def handoff_approval_source(self, source_event_id: str) -> bool | None:
        """Transfer one durable continuation out of the journal lane and into response ownership."""
        continuation = await self.deps.approval_store.approval_continuation_for_source(source_event_id)
        if continuation is None:
            return None

        if not self._cli_approval_waits.wake((source_event_id,)):
            return False

        resume = self._resume_approval_source(source_event_id)
        try:
            self.inbox.track_inbox_response(
                resume,
                name=f"approval_resume:{continuation.approval_id}:{continuation.generation}",
                recovery_proof_ready=lambda: True,
                room_id=continuation.room_id,
                source_event_ids=continuation.source_event_ids,
            )
        except BaseException:
            resume.close()
            raise
        return False

    async def handoff_job_follow_up(self, event: JournalEvent) -> None:
        """Hand a follow-up to a detached response owner; its reply, or that owner, settles it.

        The dispatcher hands over only a follow-up no live owner holds.
        """
        self.inbox.track_inbox_response(
            self._run_job_follow_up(event),
            name=f"job_follow_up:{event.event_id}",
            room_id=event.room_id,
            source_event_ids=(event.event_id,),
            recovery_proof_ready=lambda: True,
        )

    async def _run_job_follow_up(self, event: JournalEvent) -> None:
        """Run the turn a follow-up asks for, then settle a follow-up no reply took."""
        key = follow_up_key(event, self.deps.agent_name)
        claimed = asyncio.Event()
        handoff = asyncio.Event()
        await self._generate_job_follow_up(
            self._job_follow_up_request(key, event.event_id, claimed=claimed, handoff=handoff),
        )
        if claimed.is_set() or handoff.is_set():
            # The follow-up's reply settles its source, or keeps it pending for its retry.
            return
        await self.deps.approval_store.settle(event.event_id)

    async def _follow_up_due(self, request: ResponseRequest) -> bool:
        """Decide under the conversation lock whether a follow-up still has jobs to report.

        A reply may have collected them, and the reply to a message of their requester that already waits here reports
        them in its start note, so the follow-up gives way to that message.
        """
        key = request.follow_up_key
        assert key is not None
        waiting = self._lifecycle_coordinator.has_waiting_messages_from(
            request.response_envelope.target,
            key.requester_id,
        )
        runtime = get_background_runtime(self.deps.runtime_paths)
        if not waiting and runtime is not None and follow_up_jobs(await runtime.ended_jobs(key)):
            return True
        # A follow-up whose own reply a restart cut short answers again, though that reply collected the jobs.
        reply = await self.deps.replies.store.replies.for_sources((request.response_envelope.source_event_id,))
        return reply is not None and not reply.terminal

    def _job_follow_up_request(
        self,
        key: ConversationKey,
        follow_up_id: str,
        *,
        claimed: asyncio.Event,
        handoff: asyncio.Event,
    ) -> ResponseRequest:
        """Build the turn that reports a conversation's ended jobs in a new reply; its start note lists the jobs."""
        prompt = self.deps.runtime.config.get_prompt("BACKGROUND_JOBS_FOLLOW_UP_PROMPT")
        envelope = follow_up_envelope(key, follow_up_id=follow_up_id, sender_id=self.deps.matrix_full_id, prompt=prompt)

        async def mark_claimed() -> None:
            claimed.set()

        return ResponseRequest(
            thread_history=(),
            prompt=envelope.body,
            response_envelope=envelope,
            sources=ResponseSources((follow_up_id,), (follow_up_id,)),
            follow_up_key=key,
            user_id=key.requester_id,
            on_reply_claimed=mark_claimed,
            source_handoff=handoff,
        )

    async def _generate_job_follow_up(self, request: ResponseRequest) -> None:
        """Answer as this agent, or as this team, which owns the jobs its members started."""
        config = self.deps.runtime.config
        team = config.teams.get(self.deps.agent_name)
        if team is None:
            await self.generate_response(request)
            return
        await self.generate_team_response_helper(request, member_names=team.agents, team_mode=team.mode)

    def wake_cli_approval_sources(self, source_event_ids: tuple[str, ...]) -> tuple[str, ...]:
        """Wake response-local CLI owners and return sources still owned by the journal."""
        return self._cli_approval_waits.wake(source_event_ids)

    async def recover_approval_final(self, approval_id: str) -> bool:
        """Finalize one frozen FINAL under its original bot principal."""
        continuation = await self.deps.approval_store.approval_continuation(approval_id)
        if continuation is None:
            return True
        target = continuation_target(
            continuation,
            reply_to_event_id=continuation.source_event_ids[0],
        )
        request = self._approval_response_request(continuation, target=target)

        async def recover(_target: MessageTarget) -> bool:
            delivery = await self._approval_responses.final_delivery(continuation, recover=True)
            if delivery is None:
                return False
            if delivery.permanently_failed:
                reason = delivery.permanent_failure_reason or "Final Matrix delivery was permanently refused."
                return await self._approval_responses.settle_failure(continuation, reason)
            if delivery.acknowledged_event_id is None:
                return False
            if await self._approval_responses.successful_final_delivery(continuation) is None:
                return await self.deps.replies.finish_approval(continuation.approval_id)
            owns_final, event_id = await self._recover_frozen_approval_final(
                continuation,
                target=target,
            )
            return owns_final and event_id is not None

        return await self._lifecycle_coordinator.run_locked_response(
            target=target,
            response_envelope=request.response_envelope,
            pipeline_timing=None,
            locked_operation=recover,
            signal_queued_message=False,
        )

    async def _recover_nonready_approval(
        self,
        owned: ApprovalContinuation,
        *,
        target: MessageTarget,
    ) -> tuple[bool, str | None]:
        """Recover a non-ready owner, leaving ready execution to the caller."""
        if owned.state == "waiting":
            if owned.runtime_generation is not None:
                reason = "Tool approval card publication was interrupted and denied safely."
                failing = await self._approval_responses.request_failure(owned, reason)
                if failing is not None:
                    settled = await self._approval_responses.settle_failure(failing, reason)
                    return True, owned.response_event_id if settled else None
            return True, None
        if owned.state == "claimed":
            return True, await self._recover_claimed_approval_lifecycle(owned, target=target)
        if owned.state == "failing":
            return True, await self._recover_failing_approval(owned, target=target)
        return False, None

    async def _recover_failing_approval(
        self,
        failing: ApprovalContinuation,
        *,
        target: MessageTarget,
    ) -> str | None:
        """Finish a fenced continuation through its frozen FINAL, replay after a restart, or its failure note."""
        if await self._approval_responses.successful_final_delivery(failing, recover=True) is not None:
            owns_final, event_id = await self._recover_frozen_approval_final(failing, target=target)
            return event_id if owns_final else None
        reason = failing.failure_reason or "Tool approval continuation failed safely."
        if reason == INTERRUPTED_FAILURE_REASON:
            return await self._release_interrupted_approval(failing, reason=reason)
        cancel_source = _approval_interruption_cancel_source(reason)
        settled = (
            await self._settle_interrupted_approval_recovery(
                failing,
                reason=reason,
                cancel_source=cancel_source,
            )
            if cancel_source is not None
            else await self._approval_responses.settle_failure(failing, reason)
        )
        return failing.response_event_id if settled else None

    def _request_with_locked_target(
        self,
        request: ResponseRequest,
        resolved_target: MessageTarget,
    ) -> ResponseRequest:
        """Return a prepared request constrained to the target that owns the lock."""
        response_envelope = request.response_envelope
        if response_envelope.target != resolved_target:
            response_envelope = replace(response_envelope, target=resolved_target)
        return replace(
            request,
            response_envelope=response_envelope,
        )

    def _build_persist_response_event_id_effect(
        self,
        *,
        session_id: str,
        scope: HistoryScope,
        execution_identity: ToolExecutionIdentity | None,
    ) -> Callable[[str, str], Awaitable[None]]:
        """Build the response-event persistence callback for one session-backed response."""

        async def persist_response_event_id(run_id: str, response_event_id: str) -> None:
            await self.deps.state_writer.apersist_response_event_id_in_session_run(
                execution_identity=execution_identity,
                scope=scope,
                session_id=session_id,
                run_id=run_id,
                response_event_id=response_event_id,
            )

        return persist_response_event_id

    def _build_compaction_lifecycle(
        self,
        *,
        target: MessageTarget,
        request: ResponseRequest,
    ) -> MatrixCompactionLifecycle | None:
        """Build the ordered foreground compaction notice adapter for one response."""
        if _is_silent_schedule_response(request) or request.participation is not None:
            return None
        reply_to_event_id = (
            _existing_event_id(request)
            if _existing_event_id(request) is not None and _shows_placeholder(request)
            else request.reply_to_event_id
        )
        return MatrixCompactionLifecycle(
            delivery_gateway=self.deps.delivery_gateway,
            target=target,
            reply_to_event_id=reply_to_event_id,
        )

    async def _has_queued_forced_compaction(
        self,
        *,
        session_id: str,
        scope: HistoryScope,
        execution_identity: ToolExecutionIdentity | None,
    ) -> bool:
        """Return whether this scope should compact before creating a reply placeholder.

        Every turn pays for this before its placeholder is sent, so it reads
        one session row, without its runs, on a thread.
        """
        return await asyncio.to_thread(
            self._read_queued_forced_compaction,
            session_id=session_id,
            scope=scope,
            execution_identity=execution_identity,
        )

    def _read_queued_forced_compaction(
        self,
        *,
        session_id: str,
        scope: HistoryScope,
        execution_identity: ToolExecutionIdentity | None,
    ) -> bool:
        try:
            storage = self.deps.state_writer.create_storage(execution_identity, scope=scope)
            return has_queued_force_compaction(storage, session_id, scope)
        except Exception as error:
            self.deps.logger.warning(
                "forced_compaction_placeholder_check_failed",
                session_id=session_id,
                scope=scope.key,
                exception_type=error.__class__.__name__,
            )
            return False

    async def _refresh_model_history_after_lock(
        self,
        request: ResponseRequest,
        *,
        exclude_event_id: str | None = None,
    ) -> ResponseRequest:
        """Refresh model-facing thread history once this turn owns the lifecycle lock."""
        if request.thread_id is None:
            return request

        try:
            refreshed_history = await self.deps.resolver.fetch_thread_history(
                request.room_id,
                request.thread_id,
            )
        except Exception as exc:
            if request.requires_model_history_refresh:
                raise
            self.deps.logger.warning(
                "Failed to refresh thread history after lock; continuing with existing history",
                room_id=request.room_id,
                thread_id=request.thread_id,
                error=str(exc),
            )
            return replace(request, requires_model_history_refresh=True)
        if exclude_event_id is not None:
            filtered_history = [message for message in refreshed_history if message.event_id != exclude_event_id]
            if len(filtered_history) != len(refreshed_history):
                refreshed_history = replace(refreshed_history, messages=filtered_history)
        return replace(
            request,
            thread_history=refreshed_history,
            requires_model_history_refresh=False,
        )

    async def _refresh_mid_turn_context_for_approval(self, request: ResponseRequest) -> None:
        """Approval resumptions bypass ordinary payload preparation."""
        agent = self.deps.runtime.config.agents.get(request.response_envelope.agent_name)
        if agent is not None and agent.mid_turn is not None:
            # Refresh only the judge's public context; native continuation input stays unchanged.
            refreshed = await self._refresh_model_history_after_lock(request)
            self._bind_mid_turn_context(refreshed)

    def _bind_mid_turn_context(self, request: ResponseRequest) -> None:
        """Only successfully refreshed public history may authorize continuing tools."""
        bind_mid_turn_conversation_context(lambda: self._judgment_context(request))

    def _judgment_context(self, request: ResponseRequest) -> tuple[JudgmentMessage, ...] | None:
        """Return the public thread text judges read, or None until the history is refreshed."""
        if request.requires_model_history_refresh:
            return None
        return conversation_context_before_sources(
            request.thread_history,
            source_event_ids=request.sources.logical_source_event_ids,
            thread_id=request.thread_id,
            config=self.deps.runtime.config,
            runtime_paths=self.deps.runtime_paths,
        )

    async def _prepare_request_after_lock(
        self,
        request: ResponseRequest,
        *,
        exclude_history_event_id: str | None = None,
    ) -> ResponseRequest:
        """Refresh thread history and rebuild any history-derived payload once locked."""
        try:
            if request.pipeline_timing is not None:
                request.pipeline_timing.mark("thread_refresh_start")
            request = await self._refresh_model_history_after_lock(
                request,
                exclude_event_id=exclude_history_event_id,
            )
            self._bind_mid_turn_context(request)
            if request.pipeline_timing is not None:
                request.pipeline_timing.mark("thread_refresh_ready")
            request = replace(
                request,
                system_enrichment_items=with_silent_schedule_delivery(
                    request.system_enrichment_items,
                    request.response_envelope,
                ),
            )
            if request.payload_preparation is None:
                return request
            return await self.deps.request_preparer.prepare(request)
        except (ReplyMembershipPendingError, RevisionSnapshotChangedError):
            raise
        except Exception as exc:
            raise PostLockRequestPreparationError from exc

    def _note_pipeline_metadata(
        self,
        request: ResponseRequest,
        *,
        response_kind: str,
        used_streaming: bool,
    ) -> None:
        """Attach shared response metadata to one timing tracker."""
        if request.pipeline_timing is None:
            return
        request.pipeline_timing.note(
            response_kind=response_kind,
            used_streaming=used_streaming,
        )

    def _response_identity(self, request: ResponseRequest, *, response_kind: str) -> ResponseIdentity:
        """Build the per-turn identity carried by delivery requests and response hooks."""
        return ResponseIdentity(
            response_kind=response_kind,
            response_envelope=request.response_envelope,
            sources=request.sources,
            correlation_id=_correlation_id_for_request(request),
            participating_agent_names=request.participating_agent_names or (self.deps.agent_name,),
        )

    def _agent_turn_context(
        self,
        request: ResponseRequest,
        *,
        runtime: _PreparedResponseRuntime,
        run_id: str | None,
        active_event_ids: set[str],
        knowledge: AgentKnowledge | None,
        transient_enrichment_items: Sequence[EnrichmentItem],
        compaction_lifecycle: MatrixCompactionLifecycle | None,
    ) -> ResponseTurnContext:
        """Build every input of one agent response turn."""
        matrix_target_item = matrix_message_target_item(
            runtime.resolved_target,
            matrix_message_available=agent_has_matrix_messaging_tool(
                self.deps.runtime.config,
                self.deps.agent_name,
            ),
            runtime=self.deps.runtime,
        )
        state_root = resolve_agent_storage(
            self.deps.agent_name,
            self.deps.runtime.config,
            self.deps.runtime_paths,
            runtime.tool_dispatch.execution_identity,
        ).state_root
        agent_mode = resolve_agent_mode(self.deps.runtime_paths, state_root, self.deps.agent_name, runtime.session_id)
        return ResponseTurnContext(
            agent_mode=agent_mode,
            entity_label=self.deps.agent_name,
            session_id=runtime.session_id,
            run_id=run_id,
            correlation_id=_correlation_id_for_request(request),
            reply_to_event_id=request.reply_to_event_id,
            room_id=request.room_id,
            thread_id=runtime.resolved_target.resolved_thread_id,
            requester_id=request.user_id,
            matrix_run_metadata=_agent_run_metadata(request),
            current_sender_id=request.response_envelope.origin.acting_sender_id,
            history_boundary_event_id=request.history_boundary_event_id,
            member_display_names=request.member_display_names,
            active_model_name=runtime.active_model_name,
            active_event_ids=frozenset(active_event_ids),
            transient_enrichment_items=with_matrix_message_target(
                transient_enrichment_items,
                matrix_target_item,
            ),
            system_enrichment_items=tuple(request.system_enrichment_items),
            participation=runtime.participation,
            allow_no_report_response=_is_silent_schedule_response(request),
            scheduled_history_budget=request.scheduled_history_budget,
            skill_review_capture=runtime.skill_review_capture,
            redacted_history_events=partial(self.deps.redacted_history_events, runtime.resolved_target),
            prompt=request.prompt,
            model_prompt=runtime.model_prompt,
            current_timestamp_ms=request.current_timestamp_ms,
            current_prompt_is_structured=request.current_prompt_is_structured,
            thread_history=request.thread_history,
            media=runtime.media_inputs,
            show_tool_calls=runtime.show_tool_calls,
            execution_identity=runtime.tool_dispatch.execution_identity,
            compaction_lifecycle=compaction_lifecycle,
            pipeline_timing=request.pipeline_timing,
            refresh_scheduler=self._knowledge_refresh_scheduler(),
            knowledge=knowledge,
            supports_native_tool_approval=True,
        )

    async def _request_remains_authorized(
        self,
        request: ResponseRequest,
        *,
        reply_entity_names: tuple[str, ...] = (),
    ) -> bool:
        """Recheck one requester after serialized lifecycle admission."""
        requester_id = request.response_envelope.requester_id
        entity_names = _reply_authorization_entity_names(
            self.deps.runtime.config,
            self.deps.agent_name,
            reply_entity_names,
        )
        if is_sender_allowed_for_entity_replies_in_room(
            requester_id,
            entity_names,
            self.deps.runtime.config,
            request.room_id,
            self.deps.runtime_paths,
            self.deps.runtime.agent_reply_memberships,
            require_resolved_membership=True,
        ):
            return True
        self.deps.logger.info(
            "response_suppressed_after_authorization_revocation",
            source_event_id=request.response_envelope.source_event_id,
            requester_id=requester_id,
            room_id=request.room_id,
            entity_names=entity_names,
        )
        if request.on_source_turn_suppressed is not None:
            await request.on_source_turn_suppressed()
        return False

    async def _admit_locked_turn(
        self,
        request: ResponseRequest,
        *,
        resolved_target: MessageTarget,
        history_scope: HistoryScope,
        reply_entity_names: tuple[str, ...] = (),
    ) -> ResponseRequest | None:
        """Pass locked authorization and terminal-source gates, then claim the reply, before setup."""
        if not await self._request_remains_authorized(request, reply_entity_names=reply_entity_names):
            return None
        if request.on_lifecycle_lock_acquired is not None:
            request.on_lifecycle_lock_acquired()
        request = self._request_with_locked_target(request, resolved_target)
        prepared_request = await self._prepare_locked_source(request, resolved_target=resolved_target)
        if prepared_request is None:
            return None
        claimed_request = await self._claim_reply_span(prepared_request, history_scope=history_scope)
        if claimed_request is None:
            return None
        request = claimed_request
        if request.on_reply_claimed is not None:
            await request.on_reply_claimed()
        await record_silent_schedule_started_if_needed(
            entity_name=self.deps.agent_name,
            agent_names=request.participating_agent_names or (self.deps.agent_name,),
            envelope=request.response_envelope,
            config=self.deps.runtime.config,
            runtime_paths=self.deps.runtime_paths,
        )
        return request

    async def _with_interrupted_attempt(self, request: ResponseRequest) -> ResponseRequest:
        """Tell a replayed turn, or a retried regeneration, what its stopped attempts showed and ran.

        A restart, whether a crash, an orderly shutdown, an entity replacement
        or an approved run cut short, leaves the reply streaming and its sources
        pending, so replay claims that reply. Its records say what the stopped
        attempt may have shown, which stays in the message with the new attempt
        streaming below it, and every tool call it made, shown or not. The same
        account goes into the new attempt's prompt, where later turns keep it.
        """
        handle = current_span()
        if handle is None or handle.span.kind not in {rl.SpanKind.REPLAY, rl.SpanKind.REGENERATION}:
            return request
        recorded = await handle.runtime.interrupted_tool_calls(handle)
        return self._with_recorded_interrupted_attempt(request, handle, recorded)

    def _with_recorded_interrupted_attempt(
        self,
        request: ResponseRequest,
        handle: SpanHandle,
        recorded: tuple[ToolTraceEntry, ...],
    ) -> ResponseRequest:
        """Tell a new attempt what its stopped attempts showed and which tool calls they made, from the reply's records."""
        unfinished = handle.resumed if handle.span.kind is rl.SpanKind.REPLAY else None
        model_prompt = interrupted_attempt_model_prompt(
            request.model_prompt if request.model_prompt is not None else request.prompt,
            span_kind=handle.span.kind,
            unfinished=unfinished,
            recorded=recorded,
        )
        if model_prompt is None:
            return request
        self.deps.logger.info(
            "interrupted_attempt_resumed",
            response_event_id=_existing_event_id(request),
            attempt_shown=unfinished is not None,
            recorded_tool_calls=len(recorded),
        )
        return replace(request, model_prompt=model_prompt)

    async def _prepare_locked_source(
        self,
        request: ResponseRequest,
        *,
        resolved_target: MessageTarget,
    ) -> ResponseRequest | None:
        """Apply the owner gate to this exact request history, before and after refresh."""
        preparation = (
            await run_coroutine_until_complete(request.prepare_source_turn(request.thread_history))
            if request.prepare_source_turn is not None
            else False
        )
        handle = current_span()
        if preparation:
            self.deps.logger.info(
                "response_suppressed_for_terminal_source",
                source_event_id=request.response_envelope.source_event_id,
            )
            source_deleted = await self._sources_deleted(request, resolved_target)
            if handle is not None and not handle.exited:
                await self.deps.delivery_gateway.end_reply_span(
                    handle,
                    terminal_source_decision(source_deleted=source_deleted),
                )
            elif (
                # Before the claim, the reply an earlier attempt left ends through its records.
                not await self.deps.delivery_gateway.settle_unclaimed_reply(
                    (*request.sources.pending_event_ids, *request.sources.logical_source_event_ids),
                    source_deleted=source_deleted,
                )
                and request.on_source_turn_suppressed is not None
            ):
                # No reply exists to settle them.
                await request.on_source_turn_suppressed()
            return None
        return request

    async def _sources_deleted(self, request: ResponseRequest, target: MessageTarget) -> bool:
        """Return whether deletion made the request's sources terminal: all of them, or the edit driving it."""
        driving = request.response_envelope.source_event_id
        logical = request.sources.logical_source_event_ids
        redacted = await self.deps.replies.store.redacted_event_ids(target.room_id, (driving, *logical))
        return redacted.issuperset(logical) or (driving not in logical and driving in redacted)

    async def _prepare_admitted_locked_turn(
        self,
        request: ResponseRequest,
        *,
        resolved_target: MessageTarget,
        history_scope: HistoryScope,
        execution_identity: ToolExecutionIdentity,
        placeholder_message: str | None = None,
        early_placeholder_state: _EarlyPlaceholderState | None = None,
    ) -> ResponseRequest | None:
        """Run placeholder and request preparation for an already-admitted locked turn."""
        placeholder_state = early_placeholder_state or _EarlyPlaceholderState()
        placeholder_event_id = None
        if (
            placeholder_message is not None
            and _existing_event_id(request) is None
            and not await self._has_queued_forced_compaction(
                session_id=resolved_target.session_id,
                scope=history_scope,
                execution_identity=execution_identity,
            )
        ):
            handle = current_span()
            assert handle is not None, "a placeholder is its reply's create"
            placeholder_event_id = await self.deps.delivery_gateway.send_text(
                SendTextRequest(
                    target=resolved_target,
                    response_text=placeholder_message,
                    extra_content={STREAM_STATUS_KEY: STREAM_STATUS_PENDING},
                    # A streamed answer creates its visible message here and
                    # only edits it afterwards, so this send is the one a crash
                    # could duplicate into two answers in the room.
                    reply_write=initial_write(
                        handle,
                        Presentation(placeholder=placeholder_message, show_tool_calls=handle.base.show_tool_calls),
                        placeholder_only=True,
                    ),
                ),
            )
            if placeholder_event_id is not None:
                placeholder_state.placeholder_sent = True
                if request.pipeline_timing is not None:
                    request.pipeline_timing.mark("placeholder_sent")
                    request.pipeline_timing.mark_first_visible_reply("placeholder")
        request = await self._prepare_request_after_lock(
            request,
            exclude_history_event_id=placeholder_event_id,
        )
        request = self._request_with_locked_target(request, resolved_target)
        prepared_request = await self._prepare_locked_source(request, resolved_target=resolved_target)
        if prepared_request is None:
            return None
        return await self._with_interrupted_attempt(prepared_request)

    async def _begin_locked_turn(
        self,
        request: ResponseRequest,
        *,
        resolved_target: MessageTarget,
        history_scope: HistoryScope,
        execution_identity: ToolExecutionIdentity,
        placeholder_message: str | None = None,
        early_placeholder_state: _EarlyPlaceholderState | None = None,
        reply_entity_names: tuple[str, ...] = (),
    ) -> ResponseRequest | None:
        """Admit and prepare one turn after its lifecycle lock is acquired."""
        admitted_request = await self._admit_locked_turn(
            request,
            resolved_target=resolved_target,
            history_scope=history_scope,
            reply_entity_names=reply_entity_names,
        )
        if admitted_request is None:
            return None
        return await self._prepare_admitted_locked_turn(
            admitted_request,
            resolved_target=resolved_target,
            history_scope=history_scope,
            execution_identity=execution_identity,
            placeholder_message=placeholder_message,
            early_placeholder_state=early_placeholder_state,
        )

    def _settle_missing_delivery_outcome(
        self,
        progress: _DeliveryProgress,
        *,
        terminal_status: Literal["cancelled", "error"],
        failure_reason: str,
    ) -> None:
        """Settle a turn that ended before a delivery outcome; its span's exit decides the reply."""
        if progress.delivery_outcome is not None:
            return
        progress.settle(
            self.deps.delivery_gateway.terminal_outcome_without_visible_event(
                terminal_status=terminal_status,
                failure_reason=failure_reason,
            ),
        )

    async def _finalize_failed_approval_handoff(
        self,
        *,
        target: MessageTarget,
        request: ResponseRequest,
        failure_reason: str,
    ) -> FinalDeliveryOutcome:
        """Replace an unowned pause with durable failure, even after streaming began."""
        handle = current_span()
        assert handle is not None, "an approval handoff runs in its reply's span"
        if not handle.exited:
            # The pause never took: the span ends failed with the note below what it showed.
            noted = await self.deps.delivery_gateway.end_reply_span_with_note(
                handle,
                target,
                state=rl.ReplyState.FAILED,
                note=note_segment(NoteKind.APPROVAL_FAILED),
            )
            # A Stop recorded meanwhile ends it cancelled instead.
            return noted if noted.terminal_status == "cancelled" else replace(noted, failure_reason=failure_reason)
        # The pause committed: the reply is its approval's, whose failure
        # settlement writes what it shows.
        return FinalDeliveryOutcome(
            terminal_status="error",
            event_id=_existing_event_id(request),
            is_visible_response=False,
            failure_reason=failure_reason,
        )

    async def _finalize_locked_outcome(
        self,
        lifecycle: ResponseLifecycle,
        final_delivery_outcome: FinalDeliveryOutcome,
        *,
        post_response_outcome: ResponseOutcome,
        post_response_deps: PostResponseEffectsDeps | Callable[[], PostResponseEffectsDeps],
    ) -> FinalDeliveryOutcome:
        """Finalize the lifecycle, converting a late cancel into a terminal note first."""
        try:
            return await lifecycle.finalize(
                final_delivery_outcome,
                build_post_response_outcome=lambda _final_outcome: post_response_outcome,
                post_response_deps=post_response_deps,
            )
        except asyncio.CancelledError as exc:
            if current_task_is_process_shutdown():
                raise
            failure_reason = cancel_failure_reason(classify_cancel_source(exc))
            cancelled_outcome = self.deps.delivery_gateway.cancelled_terminal_outcome(
                final_delivery_outcome,
                failure_reason=failure_reason,
            )
            await lifecycle.finalize(
                cancelled_outcome,  # lifecycle.finalize cancelled terminal outcome before re-raising
                build_post_response_outcome=lambda _final_outcome: post_response_outcome,
                post_response_deps=post_response_deps,
            )
            raise

    async def _run_and_settle_locked_response(  # noqa: C901, PLR0912, PLR0915
        self,
        request: ResponseRequest,
        *,
        target: MessageTarget,
        lifecycle: ResponseLifecycle,
        progress: _DeliveryProgress,
        response_function: Callable[[str | None], Coroutine[Any, Any, None]],
        user_id: str | None,
        build_post_response_outcome: Callable[[FinalDeliveryOutcome], ResponseOutcome],
        post_response_deps: PostResponseEffectsDeps | Callable[[], PostResponseEffectsDeps],
        approval_suspension_handler: Callable[[PausedAttempt], Awaitable[FinalDeliveryOutcome]] | None = None,
        show_tool_calls: bool | None = None,
        participation: ParticipationGate | None = None,
        reply_session: ReplySession | None = None,
    ) -> str | None:
        """Run generation and settle its terminal lifecycle exactly once.

        ``reply_session`` names the stored session the reply writes; when one of its writes creates that session's
        row, ``session:started`` fires once the attempt settles, before the lifecycle's finalization.
        """
        deferred_error: BaseException | None = None
        async with session_started_on_creation(
            reply_session,
            correlation_id=lifecycle.identity.correlation_id,
        ):
            try:
                # The attempt runs against the event the turn already adopted, which
                # `progress` was seeded with, so it has no new event to report back.
                await self._run_cancellable_response(
                    target=target,
                    response_function=response_function,
                    existing_event_id=_existing_event_id(request),
                    user_id=user_id,
                    on_cancelled=progress.note_task_cancelled,
                    show_stop_button=not _is_silent_schedule_response(request),
                )
            except ResponsePausedForApproval as error:
                if approval_suspension_handler is None:
                    raise
                frozen_show_tool_calls = _require_frozen_tool_visibility(show_tool_calls)
                try:
                    progress.settle(
                        await approval_suspension_handler(
                            _paused_with_committed_presentation(
                                error,
                                show_tool_calls=frozen_show_tool_calls,
                            ),
                        ),
                    )
                except Exception as suspension_error:
                    self.deps.logger.exception("approval_suspension_failed", error=str(suspension_error))
                    progress.failure_reason = str(suspension_error) or "approval_suspension_failed"
                    progress.settle(
                        await self._finalize_failed_approval_handoff(
                            target=target,
                            request=request,
                            failure_reason=progress.failure_reason,
                        ),
                    )
            except asyncio.CancelledError as error:
                if current_task_is_process_shutdown():
                    raise
                progress.note_task_cancelled(cancel_failure_reason(classify_cancel_source(error)))
                self._settle_missing_delivery_outcome(
                    progress,
                    terminal_status="cancelled",
                    failure_reason=progress.failure_reason or "interrupted",
                )
                deferred_error = error
            except Exception as error:
                if current_task_is_process_shutdown():
                    raise
                if (
                    not progress.stage_started
                    and progress.delivery_outcome is None
                    and (skipped := _skip_unapproved_response(participation)) is not None
                ):
                    self.deps.logger.exception("Response skipped before participation", error=str(error))
                    progress.settle(skipped)
                elif progress.stage_started or progress.delivery_outcome is not None:
                    # Do not touch a tracked event after delivery starts: an adopted
                    # thinking-message stream can already hold the full reply.
                    self._log_delivery_failure(response_kind=lifecycle.identity.response_kind, error=error)
                    if progress.delivery_outcome is None:
                        progress.failure_reason = progress.failure_reason or str(error) or "late_delivery_failure"
                else:
                    progress.failure_reason = str(error) or "delivery_failed_before_start"
                    progress.settle(
                        self.deps.delivery_gateway.terminal_outcome_without_visible_event(
                            terminal_status="error",
                            failure_reason=progress.failure_reason,
                        ),
                    )
                    deferred_error = error

        if progress.delivery_outcome is None and (progress.cancelled or progress.failure_reason is not None):
            self._settle_missing_delivery_outcome(
                progress,
                terminal_status="cancelled" if progress.cancelled else "error",
                failure_reason=progress.failure_reason or "interrupted",
            )

        final_delivery_outcome = progress.delivery_outcome
        if final_delivery_outcome is None:
            msg = "Response generation did not settle a delivery outcome"
            raise RuntimeError(msg)
        handle = current_span()
        if handle is not None and not handle.exited:
            final_delivery_outcome = await self._end_span_after_outcome(
                handle,
                final_delivery_outcome,
                target=target,
                delivery_started=progress.stage_started,
            )
        final_outcome = await self._finalize_locked_outcome(
            lifecycle,
            final_delivery_outcome,
            post_response_outcome=build_post_response_outcome(final_delivery_outcome),
            post_response_deps=post_response_deps,
        )
        if final_outcome.terminal_status == "suspended" and request.source_handoff is not None:
            request.source_handoff.set()
        if deferred_error is not None:
            raise deferred_error
        cancel_source = final_outcome.resolved_cancel_source
        source_handled = final_outcome.mark_handled and (
            cancel_source is None or cancel_source == "user_stop" or _interruption_note_landed(current_span())
        )
        return final_outcome.final_visible_event_id if source_handled else None

    async def _end_span_after_outcome(
        self,
        handle: SpanHandle,
        outcome: FinalDeliveryOutcome,
        *,
        target: MessageTarget,
        delivery_started: bool,
    ) -> FinalDeliveryOutcome:
        """End a span whose settled outcome wrote no terminal row."""
        gateway = self.deps.delivery_gateway
        if outcome.terminal_status == "suspended":
            # The approval continuation owns the sources until its resume claims the reply.
            await gateway.end_reply_span(handle, release_decision(handle))
            return outcome
        if outcome.terminal_status == "completed" or outcome.suppressed:
            # Nothing was answered: a placeholder goes, substantive content stays.
            await gateway.end_reply_span(handle, suppress_decision(handle))
            return outcome
        noted = await gateway.end_interrupted_span(
            handle,
            target,
            cancel_source=outcome.resolved_cancel_source if outcome.terminal_status == "cancelled" else None,
            failure_reason=outcome.failure_reason,
            delivery_started=delivery_started,
        )
        if noted is None:
            return outcome
        return replace(
            noted,
            terminal_status=outcome.terminal_status,
            cancel_source=outcome.cancel_source,
            failure_reason=outcome.failure_reason,
        )

    async def _end_failed_stream(self, target: MessageTarget, *, failure_reason: str) -> FinalDeliveryOutcome:
        """End the reply of a stream that stopped on a failure: its span exit notes the failure below what it shows."""
        outcome = self.deps.delivery_gateway.terminal_outcome_without_visible_event(
            terminal_status="error",
            failure_reason=failure_reason,
        )
        handle = current_span()
        if handle is None or handle.exited:
            return outcome
        # A failure ends its reply failed whether or not delivery started; only an interruption tells them apart.
        return await self._end_span_after_outcome(handle, outcome, target=target, delivery_started=False)

    def _build_lifecycle(
        self,
        *,
        identity: ResponseIdentity,
        request: ResponseRequest,
    ) -> ResponseLifecycle:
        """Build one lifecycle helper with the resolved shared response context."""
        return ResponseLifecycle(
            ResponseLifecycleDeps(
                response_hooks=self.deps.delivery_gateway.deps.response_hooks,
                logger=self.deps.logger,
                process_shutdown_requested=current_task_is_process_shutdown,
            ),
            identity=identity,
            pipeline_timing=request.pipeline_timing,
        )

    async def _finalize_empty_prompt_locked(
        self,
        request: ResponseRequest,
        *,
        resolved_target: MessageTarget,
        response_kind: str,
        history_scope: HistoryScope | None = None,
        execution_identity: ToolExecutionIdentity | None = None,
        reply_entity_names: tuple[str, ...] = (),
    ) -> str | None:
        """Finalize one empty prompt through the canonical response lifecycle."""
        resolved_history_scope = history_scope or self.deps.state_writer.history_scope()
        resolved_execution_identity = execution_identity or self.deps.tool_runtime.build_execution_identity(
            target=resolved_target,
            user_id=request.user_id,
        )
        prepared_request = await self._begin_locked_turn(
            request,
            resolved_target=resolved_target,
            history_scope=resolved_history_scope,
            execution_identity=resolved_execution_identity,
            reply_entity_names=reply_entity_names,
        )
        if prepared_request is None:
            return None
        request = prepared_request
        handle = current_span()
        if handle is not None and not handle.exited:
            # Nothing to answer: a placeholder goes, as a suppressed answer's does.
            await self.deps.delivery_gateway.end_reply_span(handle, suppress_decision(handle))
        lifecycle = self._build_lifecycle(
            identity=self._response_identity(request, response_kind=response_kind),
            request=request,
        )
        final_outcome = await lifecycle.finalize(
            FinalDeliveryOutcome.cancelled_for_empty_prompt(),
            build_post_response_outcome=lambda _final_outcome: ResponseOutcome(),
            post_response_deps=lambda: self._post_response_deps(request),
        )
        return final_outcome.final_visible_event_id if final_outcome.mark_handled else None

    async def generate_team_response_helper(
        self,
        request: ResponseRequest,
        *,
        member_names: Sequence[str],
        team_mode: str,
        reason_prefix: str = "Team request",
        resolution_reason: str | None = None,
    ) -> str | None:
        """Generate a team response of ``member_names`` with lifecycle locking and queued-message state."""
        team_request = _TeamResponseRequest(
            member_names=tuple(member_names),
            team_mode=team_mode,
            reason_prefix=reason_prefix,
            resolution_reason=resolution_reason,
        )
        return await self._run_locked_response_lifecycle(
            request,
            response_kind="team",
            locked_operation=lambda resolved_target, early_placeholder_state: self._run_locked_turn(
                request,
                team=team_request,
                resolved_target=resolved_target,
                early_placeholder_state=early_placeholder_state,
                participation=None,
            ),
        )

    async def generate_response_for_empty_prompt(
        self,
        request: ResponseRequest,
        *,
        response_kind: str,
    ) -> str | None:
        """Finalize an empty prompt through the locked lifecycle before setup side effects."""
        return await self._run_locked_response_lifecycle(
            request,
            response_kind=response_kind,
            locked_operation=lambda resolved_target, _early_placeholder_state: self._finalize_empty_prompt_locked(
                request,
                resolved_target=resolved_target,
                response_kind=response_kind,
            ),
        )

    async def _run_locked_turn(
        self,
        request: ResponseRequest,
        *,
        team: _TeamResponseRequest | None,
        resolved_target: MessageTarget,
        early_placeholder_state: _EarlyPlaceholderState,
        participation: ParticipationGate | None,
    ) -> str | None:
        """Admit, prepare, generate, and settle one agent or team turn while the conversation's lock is held.

        ``team`` names the members a team turn runs; an agent turn has none, and only it may wait on a participation
        decision. What differs between the two is decided in ``_prepare_agent_turn`` and ``_prepare_team_turn``.
        """
        execution_identity = self.deps.tool_runtime.build_execution_identity(
            target=resolved_target,
            user_id=request.user_id,
        )
        member_names = () if team is None else team.member_names
        request = replace(request, participating_agent_names=member_names or (self.deps.agent_name,))
        history_scope = (
            self.deps.state_writer.history_scope()
            if team is None
            else self.deps.state_writer.team_history_scope(
                member_names,
                requester_user_id=execution_identity.requester_id,
            )
        )
        if not request.prompt.strip():
            return await self._finalize_empty_prompt_locked(
                request,
                resolved_target=resolved_target,
                response_kind="ai" if team is None else "team",
                history_scope=history_scope,
                execution_identity=execution_identity,
                reply_entity_names=member_names,
            )
        admitted_request = await self._admit_locked_turn(
            request,
            resolved_target=resolved_target,
            history_scope=history_scope,
            reply_entity_names=member_names,
        )
        if admitted_request is None:
            return None
        request = admitted_request
        if team is None:
            _approve_continued_reply(participation, request)
            runtime_model = self.deps.runtime.config.resolve_runtime_model(
                entity_name=self.deps.agent_name,
                active_model_name=request.scheduled_model,
                room_id=resolved_target.room_id,
                thread_id=_response_thread_id(request, resolved_target),
                runtime_paths=self.deps.runtime_paths,
            )
        else:
            team_models = None if team.resolution_reason is not None else self._budgeted_team_models(request, team)
        prepared_request = await self._prepare_admitted_locked_turn(
            request,
            resolved_target=resolved_target,
            history_scope=history_scope,
            execution_identity=execution_identity,
            placeholder_message=(
                None
                if _is_silent_schedule_response(request) or request.participation is not None
                else AGENT_PLACEHOLDER
                if team is None
                else TEAM_PLACEHOLDER
            ),
            early_placeholder_state=early_placeholder_state,
        )
        if prepared_request is None:
            return None
        request = prepared_request
        if team is None:
            turn = await self._prepare_agent_turn(
                request,
                resolved_target=resolved_target,
                runtime_model=runtime_model,
                history_scope=history_scope,
                execution_identity=execution_identity,
                participation=participation,
            )
        elif team.resolution_reason is not None:
            return await self._deliver_resolution_reason(request, resolved_target, team.resolution_reason)
        else:
            assert team_models is not None
            turn = await self._prepare_team_turn(
                request,
                team=team,
                team_models=team_models,
                resolved_target=resolved_target,
                history_scope=history_scope,
            )
        early_placeholder_state.settlement_started = True
        return await self._generate_and_settle(turn)

    def _budgeted_team_models(self, request: ResponseRequest, team: _TeamResponseRequest) -> TeamTurnModelSelection:
        """Return the models of a team turn, each under its requester's budget."""
        turn_models = resolve_team_turn_models(
            self.deps.agent_name,
            list(team.member_names),
            request.room_id,
            self.deps.runtime.config,
            self.deps.runtime_paths,
            thread_id=request.thread_id,
            active_model_name=request.scheduled_model,
        )
        budgeted = {
            model_name: self._budgeted_model(request, model_name)
            for model_name in {turn_models.team_model_name, *turn_models.member_model_names.values()}
        }
        return TeamTurnModelSelection(
            team_model_name=budgeted[turn_models.team_model_name],
            member_model_names={
                member: budgeted[model_name] for member, model_name in turn_models.member_model_names.items()
            },
        )

    async def _deliver_resolution_reason(
        self,
        request: ResponseRequest,
        resolved_target: MessageTarget,
        reason: str,
    ) -> str | None:
        """Answer a team turn with why it cannot run, as one final delivery."""
        response_identity = self._response_identity(request, response_kind="team")
        lifecycle = self._build_lifecycle(identity=response_identity, request=request)
        progress = _DeliveryProgress()

        async def deliver_resolution_reason(message_id: str | None) -> None:
            progress.settle(
                await self.deps.delivery_gateway.deliver_final(
                    FinalDeliveryRequest(
                        target=resolved_target,
                        existing_event_id=message_id,
                        response_text=reason,
                        identity=response_identity,
                        tool_trace=None,
                        extra_content=None,
                    ),
                ),
            )

        return await self._run_and_settle_locked_response(
            request,
            target=resolved_target,
            lifecycle=lifecycle,
            progress=progress,
            response_function=deliver_resolution_reason,
            user_id=request.user_id,
            build_post_response_outcome=lambda _final_outcome: ResponseOutcome(),
            post_response_deps=lambda: self._post_response_deps(request),
        )

    async def _prepare_team_turn(
        self,
        request: ResponseRequest,
        *,
        team: _TeamResponseRequest,
        team_models: TeamTurnModelSelection,
        resolved_target: MessageTarget,
        history_scope: HistoryScope,
    ) -> _LockedTurn:
        """Prepare an admitted team turn: its members' context, its leader's dispatch, and the stream that runs it."""
        agent_names = list(team.member_names)
        memory_prompt, memory_thread_history, prepared_prompt, model_thread_history = prepare_memory_and_model_context(
            request.prompt,
            request.thread_history,
            config=self.deps.runtime.config,
            runtime_paths=self.deps.runtime_paths,
            # A team turn is dated in the zone of the entity whose bot runs it, as its leader and prompt are.
            timezone=self.deps.runtime.config.get_entity_timezone(self.deps.agent_name),
            model_prompt=request.model_prompt,
        )
        delivery_request = self._request_with_locked_target(
            replace(request, thread_history=model_thread_history, media=request.media or MediaInputs()),
            resolved_target,
        )
        target = resolved_target.with_thread_root(_response_thread_id(request, resolved_target))
        tool_dispatch = self.deps.tool_runtime.build_dispatch_context(
            target,
            user_id=request.user_id or "",
            active_model_name=team_models.team_model_name,
            attachment_ids=request.attachment_ids,
            correlation_id=_correlation_id_for_request(request),
            source_envelope=request.response_envelope,
        )
        execution_identity = tool_dispatch.execution_identity
        configured_team_name = self.deps.runtime.config.configured_team_name(self.deps.agent_name)
        self.deps.runtime.config.assert_team_agents_supported(
            [agent_name for agent_name in agent_names if agent_name != ROUTER_AGENT_NAME],
            allow_direct_private_agents=(
                configured_team_name is None
                and execution_identity.channel == "matrix"
                and bool(execution_identity.requester_id)
            ),
        )
        orchestrator = self.deps.runtime.orchestrator
        if orchestrator is None:
            msg = "Orchestrator is not set"
            raise RuntimeError(msg)
        show_tool_calls = self._turn_show_tool_calls()
        runtime = _PreparedResponseRuntime(
            resolved_target=target,
            media_inputs=request.media or MediaInputs(),
            session_id=resolved_target.session_id,
            model_prompt=prepared_prompt,
            active_model_name=team_models.team_model_name,
            show_tool_calls=show_tool_calls,
            tool_dispatch=tool_dispatch,
        )
        run_id = str(uuid4())
        mode = TeamMode(team.team_mode)
        team_spec = TeamTurnSpec(
            agent_names=agent_names,
            mode=mode,
            orchestrator=orchestrator,
            model_name=team_models.team_model_name,
            member_model_names=team_models.member_model_names,
            configured_team_name=configured_team_name,
            response_sender_id=self.deps.matrix_full_id,
            reason_prefix=team.reason_prefix,
        )
        matrix_target_item = matrix_message_target_item(
            resolved_target,
            matrix_message_available=any(
                agent_has_matrix_messaging_tool(self.deps.runtime.config, name) for name in agent_names
            ),
            runtime=self.deps.runtime,
        )
        # Team entries refine entity_label to the materialized team label and
        # append the knowledge-availability enrichment before the turn runs.
        team_turn_ctx = ResponseTurnContext(
            entity_label=self.deps.agent_name,
            session_id=resolved_target.session_id,
            run_id=run_id,
            correlation_id=_correlation_id_for_request(request),
            reply_to_event_id=request.reply_to_event_id,
            room_id=request.room_id,
            thread_id=resolved_target.resolved_thread_id,
            requester_id=request.user_id or execution_identity.requester_id,
            matrix_run_metadata=None if request.matrix_run_metadata is None else dict(request.matrix_run_metadata),
            current_sender_id=request.response_envelope.origin.acting_sender_id,
            history_boundary_event_id=request.history_boundary_event_id,
            member_display_names=request.member_display_names,
            transient_enrichment_items=with_matrix_message_target(
                request.transient_enrichment_items,
                matrix_target_item,
            ),
            system_enrichment_items=request.system_enrichment_items,
            allow_no_report_response=_is_silent_schedule_response(request),
            scheduled_history_budget=request.scheduled_history_budget,
            redacted_history_events=partial(self.deps.redacted_history_events, resolved_target),
            prompt=prepared_prompt,
            current_timestamp_ms=request.current_timestamp_ms,
            current_prompt_is_structured=request.current_prompt_is_structured,
            thread_history=model_thread_history,
            media=request.media,
            show_tool_calls=show_tool_calls,
            execution_identity=execution_identity,
            pipeline_timing=request.pipeline_timing,
        )

        def stream(
            active_event_ids: set[str],
            compaction_lifecycle: MatrixCompactionLifecycle | None,
            sinks: TurnSinks,
        ) -> AsyncIterator[StreamInputChunk]:
            return team_response_stream(
                team_spec,
                replace(
                    team_turn_ctx,
                    active_event_ids=frozenset(active_event_ids),
                    compaction_lifecycle=compaction_lifecycle,
                ),
                sinks=sinks,
            )

        # A configured team keeps the conversations it answers in its members' shared memory.
        queue_memory_persistence = (
            self._memory_persistence(
                agent_name=agent_names,
                session_id=resolved_target.session_id,
                execution_identity=execution_identity,
                prompt=memory_prompt,
                thread_history=memory_thread_history,
                user_id=request.user_id,
                hook_source=request.response_envelope.hook_source,
                origin=request.response_envelope.origin,
            )
            if configured_team_name is not None
            else None
        )
        persist_response_event_id = self._build_persist_response_event_id_effect(
            session_id=resolved_target.session_id,
            scope=history_scope,
            execution_identity=execution_identity,
        )
        return _LockedTurn(
            request=request,
            delivery_request=delivery_request,
            response_target=resolved_target,
            response_kind="team",
            history_scope=history_scope,
            execution_identity=execution_identity,
            runtime=runtime,
            run_id=run_id,
            streaming_cls=ReplacementStreamingResponse,
            stream=stream,
            post_response_deps=lambda: self._post_response_deps(
                request,
                queue_memory_persistence=queue_memory_persistence,
                persist_response_event_id=persist_response_event_id,
            ),
            team_member_names=tuple(agent_names),
            team_mode=mode.value,
        )

    async def _generate_and_settle(self, turn: _LockedTurn) -> str | None:
        """Generate a prepared turn's reply, streamed or delivered whole, and settle its lifecycle once."""
        request = turn.request
        # A resumed reply is a stream, and a whole answer would replace what it already showed.
        use_streaming = _resumed_reply() is not None or (
            not _is_silent_schedule_response(request)
            and await should_use_streaming(
                self._client(),
                request.room_id,
                requester_user_id=request.user_id,
                enable_streaming=self.deps.runtime.enable_streaming,
            )
        )
        self._note_pipeline_metadata(
            request,
            response_kind="agent" if turn.response_kind == "ai" else "team",
            used_streaming=use_streaming,
        )
        # The turn driver writes the turn's record; this runner reads how it ended.
        recorder = TurnRecorder()
        progress = _DeliveryProgress()
        response_identity = self._response_identity(request, response_kind=turn.response_kind)
        lifecycle = self._build_lifecycle(identity=response_identity, request=request)

        def build_post_response_outcome(_delivery_outcome: FinalDeliveryOutcome) -> ResponseOutcome:
            return ResponseOutcome(
                response_run_id=recorder.run_id or turn.run_id,
                run_succeeded=recorder.outcome == "completed",
                response_target=turn.response_target,
                thread_summary_message_count_hint=self._thread_summary_message_count_hint(request),
                thread_summary_entity_name=self.deps.agent_name,
            )

        async with self._turn_dispatch_scope(turn, progress=progress) as dispatched:

            async def generate(_message_id: str | None) -> None:
                delivery = await self._deliver_turn(
                    dispatched,
                    use_streaming=use_streaming,
                    recorder=recorder,
                    response_identity=response_identity,
                    progress=progress,
                )
                participation = dispatched.runtime.participation
                if (
                    participation is not None
                    and participation.is_declined
                    and request.participation is not None
                    and request.participation.decline_reaction is not None
                    and delivery.failure_reason == "participation_declined"
                ):
                    await self.deps.delivery_gateway.send_judgment_reaction(
                        turn_id=response_identity.response_envelope.source_event_id,
                        room_id=request.room_id,
                        event_id=request.sources.logical_source_event_ids[-1],
                        key=request.participation.decline_reaction,
                        kind="participation_decline",
                    )
                progress.settle(delivery)

            return await self._run_and_settle_locked_response(
                request,
                target=dispatched.response_target,
                lifecycle=lifecycle,
                progress=progress,
                response_function=generate,
                user_id=request.user_id,
                build_post_response_outcome=build_post_response_outcome,
                post_response_deps=dispatched.post_response_deps,
                approval_suspension_handler=lambda paused: self._suspend_for_approval(
                    paused,
                    request=request,
                    target=dispatched.response_target,
                    execution_identity=dispatched.execution_identity,
                    entity_kind="agent" if dispatched.response_kind == "ai" else "team",
                    history_scope=dispatched.history_scope,
                    show_tool_calls=dispatched.runtime.show_tool_calls,
                    team_member_names=dispatched.team_member_names,
                    team_mode=dispatched.team_mode,
                ),
                show_tool_calls=dispatched.runtime.show_tool_calls,
                participation=dispatched.runtime.participation,
                reply_session=ReplySession(dispatched.history_scope, dispatched.runtime.tool_dispatch),
            )

    @asynccontextmanager
    async def _turn_dispatch_scope(
        self,
        turn: _LockedTurn,
        *,
        progress: _DeliveryProgress,
    ) -> AsyncIterator[_LockedTurn]:
        """Bind an agent turn's CLI approval publication to its reply; a team turn's members ask through its leader."""
        if turn.response_kind != "ai":
            yield turn
            return
        async with self._cli_approval_scope(
            turn.runtime,
            request=turn.request,
            progress=progress,
            history_scope=turn.history_scope,
        ) as runtime:
            yield replace(turn, runtime=runtime)

    async def _deliver_turn(
        self,
        turn: _LockedTurn,
        *,
        use_streaming: bool,
        recorder: TurnRecorder,
        response_identity: ResponseIdentity,
        progress: _DeliveryProgress,
    ) -> FinalDeliveryOutcome:
        """Run the turn's stream into its reply: streamed as it grows, or collected and delivered whole."""
        request = self._request_with_locked_target(turn.delivery_request, turn.runtime.resolved_target)
        active_event_ids = await self._active_response_event_ids(request.room_id)
        compaction_lifecycle = self._build_compaction_lifecycle(target=turn.runtime.resolved_target, request=request)
        # The caller-owned collectors stay live: the gateway snapshots the run metadata as the stream ends.
        run_metadata_content: dict[str, Any] = {}
        tool_trace: list[ToolTraceEntry] = []
        sinks = TurnSinks(
            turn_recorder=recorder,
            run_metadata_collector=run_metadata_content,
            response_event_id=_reply_event_id,
        )

        def response_stream() -> AsyncIterator[StreamInputChunk]:
            return turn.stream(active_event_ids, compaction_lifecycle, sinks)

        def final_extra_content() -> dict[str, Any] | None:
            # A turn that published no run metadata still names the run the record holds.
            fallback = ai_run_extra_content_from_metadata(recorder.run_metadata) or {}
            return _merge_response_extra_content({**fallback, **run_metadata_content} or None, request.attachment_ids)

        if use_streaming:
            return await self._deliver_streamed(
                turn,
                request,
                response_stream,
                response_identity=response_identity,
                run_metadata_content=run_metadata_content,
                tool_trace=tool_trace,
                final_extra_content=final_extra_content,
                progress=progress,
            )
        return await self._deliver_collected(
            turn,
            request,
            response_stream,
            response_identity=response_identity,
            tool_trace=tool_trace,
            final_extra_content=final_extra_content,
            progress=progress,
        )

    async def _deliver_streamed(
        self,
        turn: _LockedTurn,
        request: ResponseRequest,
        response_stream: Callable[[], AsyncIterator[StreamInputChunk]],
        *,
        response_identity: ResponseIdentity,
        run_metadata_content: dict[str, Any],
        tool_trace: list[ToolTraceEntry],
        final_extra_content: Callable[[], dict[str, Any] | None],
        progress: _DeliveryProgress,
    ) -> FinalDeliveryOutcome:
        """Stream the turn into its reply and finalize what the stream left."""
        runtime = turn.runtime
        label = "Bot" if turn.response_kind == "ai" else "Team"
        # What the reply showed before this stream, which its terminal handling continues from.
        existing_event_id = _existing_event_id(request)
        participation = runtime.participation
        try:
            async with _response_typing_indicator(
                self._client(),
                request,
                response_run_id=turn.run_id,
                participation=participation,
            ):
                transport_outcome = await self.deps.delivery_gateway.deliver_stream(
                    StreamingDeliveryRequest(
                        target=runtime.resolved_target,
                        identity=response_identity,
                        response_stream=self._stream_in_tool_context(
                            tool_dispatch=runtime.tool_dispatch,
                            stream_factory=response_stream,
                        ),
                        existing_event_id=existing_event_id,
                        adopt_existing_placeholder=bool(existing_event_id) and _shows_placeholder(request),
                        show_tool_calls=runtime.show_tool_calls,
                        # The live collector dict: the turn driver fills it at terminal settle, before the stream's
                        # final edit snapshots extra_content, so the ai_run metadata lands on the wire.
                        extra_content=_merge_response_extra_content(run_metadata_content, request.attachment_ids),
                        tool_trace_collector=tool_trace,
                        streaming_cls=turn.streaming_cls,
                        pipeline_timing=request.pipeline_timing,
                        visible_event_id_callback=progress.note_delivery_started,
                        visible_progress_callback=self._lifecycle_coordinator.visible_progress_callback(
                            runtime.resolved_target,
                        ),
                        allow_new_terminal_message=lambda: participation is None or participation.approved,
                        placeholder_ready=None if participation is None else participation.wait_for_approval,
                    ),
                )
            if request.pipeline_timing is not None:
                request.pipeline_timing.mark("streaming_complete")
            _raise_if_process_shutdown()
        except ResponsePausedForApproval:
            raise
        except asyncio.CancelledError as exc:
            log_cancelled_response(
                self.deps.logger,
                exc=exc,
                message_id=existing_event_id,
                restart_message=f"{label} streaming response interrupted by sync restart",
                user_stop_message=f"{label} streaming response cancelled by user",
                interrupted_message=f"{label} streaming response interrupted — traceback for diagnosis",
            )
            raise
        except Exception as raised:
            if current_task_is_process_shutdown():
                raise
            error = raised.error if isinstance(raised, StreamingDeliveryError) else raised
            self.deps.logger.exception(f"Error in {label.lower()} streaming response", error=str(error))
            # A failure before the participation decision stays quiet, wherever the stream failed.
            if skipped := _skip_unapproved_response(participation):
                return skipped
            return await self._end_failed_stream(runtime.resolved_target, failure_reason=str(error))

        if participation is not None and participation.is_silent:
            return _skipped_participation_outcome()
        progress.note_delivery_started()
        return await self._finalize_streamed_turn(
            request=request,
            delivery_target=runtime.resolved_target,
            transport_outcome=transport_outcome,
            delivery_kind="edited" if existing_event_id else "sent",
            response_identity=response_identity,
            tool_trace=tool_trace if runtime.show_tool_calls else None,
            extra_content=final_extra_content(),
        )

    async def _deliver_collected(
        self,
        turn: _LockedTurn,
        request: ResponseRequest,
        response_stream: Callable[[], AsyncIterator[StreamInputChunk]],
        *,
        response_identity: ResponseIdentity,
        tool_trace: list[ToolTraceEntry],
        final_extra_content: Callable[[], dict[str, Any] | None],
        progress: _DeliveryProgress,
    ) -> FinalDeliveryOutcome:
        """Collect the turn's stream into one reply, then deliver it whole."""
        runtime = turn.runtime
        label = "Non-streaming" if turn.response_kind == "ai" else "Team non-streaming"

        async def collect() -> str:
            body, collected_trace = await collect_streamed_response_content(
                response_stream(),
                # Hidden tool calls still separate the text around them, as a streamed reply shows it.
                presentation=CollectedStreamPresentation(
                    show_tool_calls=runtime.show_tool_calls,
                    track_hidden_tools=True,
                    replaces_text=turn.streaming_cls.replaces_text,
                ),
            )
            tool_trace.extend(collected_trace)
            return body

        try:
            async with _response_typing_indicator(
                self._client(),
                request,
                response_run_id=turn.run_id,
                participation=runtime.participation,
            ):
                response_text = await self._run_in_tool_context(tool_dispatch=runtime.tool_dispatch, operation=collect)
            _raise_if_process_shutdown()
        except ResponsePausedForApproval:
            raise
        except asyncio.CancelledError as exc:
            if current_task_is_process_shutdown():
                raise
            return await self._settle_blocking_cancellation(
                exc,
                message_id=_existing_event_id(request),
                restart_message=f"{label} response interrupted by sync restart",
                user_stop_message=f"{label} response cancelled by user",
                interrupted_message=f"{label} response interrupted — traceback for diagnosis",
            )
        except Exception as error:
            self.deps.logger.exception(f"Error in {label.lower()} response", error=str(error))
            if skipped := _skip_unapproved_response(runtime.participation):
                return skipped
            raise

        if runtime.participation is not None and runtime.participation.is_silent:
            return _skipped_participation_outcome()
        progress.note_delivery_started()
        delivery = await self.deps.delivery_gateway.deliver_final(
            FinalDeliveryRequest(
                target=runtime.resolved_target,
                existing_event_id=_existing_event_id(request),
                response_text=response_text,
                identity=response_identity,
                tool_trace=tool_trace if runtime.show_tool_calls else None,
                extra_content=final_extra_content(),
            ),
        )
        self._note_final_delivery_timing(request, delivery)
        return delivery

    async def _run_cancellable_response(
        self,
        *,
        target: MessageTarget,
        response_function: Callable[[str | None], Coroutine[Any, Any, None]],
        existing_event_id: str | None,
        user_id: str | None,
        on_cancelled: Callable[[str], None],
        show_stop_button: bool,
    ) -> None:
        """Run one response-generation attempt with cancellation support."""
        await ResponseAttemptRunner(
            ResponseAttemptDeps(
                client=self._client(),
                logger=self.deps.logger,
                show_stop_button=lambda: show_stop_button and self.deps.runtime.config.defaults.show_stop_button,
            ),
        ).run(
            ResponseAttemptRequest(
                target=target,
                response_function=response_function,
                existing_event_id=existing_event_id,
                user_id=user_id,
                on_cancelled=on_cancelled,
                span=self._span_attempt(),
            ),
        )

    def _span_attempt(self) -> SpanAttempt:
        """Return the current reply span's ownership of an attempt: its Stop and its Stop button."""
        handle = current_span()
        assert handle is not None, "every response attempt runs in a reply span"

        async def reply_event() -> str | None:
            await handle.reply_shown.wait()
            return handle.reply.event_id

        return SpanAttempt(
            register=partial(handle.runtime.spans.register, handle.span_id),
            add_stop_button=lambda message_id: self.deps.delivery_gateway.add_reply_stop_button(handle, message_id),
            reply_event=reply_event,
        )

    @timed("prepare_response_runtime")
    async def prepare_response_runtime(
        self,
        request: ResponseRequest,
        *,
        active_model_name: str | None = None,
        participation: ParticipationGate | None = None,
    ) -> _PreparedResponseRuntime:
        """Resolve shared runtime context for one streaming or non-streaming response."""
        resolved_target = request.response_envelope.target
        response_thread_id = _response_thread_id(request, resolved_target)
        resolved_target = resolved_target.with_thread_root(response_thread_id)
        media_inputs = request.media or MediaInputs()
        session_id = resolved_target.session_id
        resolved_model_prompt = request.model_prompt or request.prompt
        if active_model_name is None:
            active_model_name = self.deps.runtime.config.resolve_runtime_model(
                entity_name=self.deps.agent_name,
                active_model_name=request.scheduled_model,
                room_id=resolved_target.room_id,
                thread_id=response_thread_id,
                runtime_paths=self.deps.runtime_paths,
            ).model_name
        active_model_name = self._budgeted_model(request, active_model_name)
        tool_dispatch = self.deps.tool_runtime.build_dispatch_context(
            resolved_target,
            user_id=request.user_id,
            active_model_name=active_model_name,
            attachment_ids=request.attachment_ids,
            correlation_id=_correlation_id_for_request(request),
            source_envelope=request.response_envelope,
        )
        return _PreparedResponseRuntime(
            resolved_target=resolved_target,
            media_inputs=media_inputs,
            session_id=session_id,
            model_prompt=resolved_model_prompt,
            active_model_name=active_model_name,
            show_tool_calls=self._turn_show_tool_calls(),
            tool_dispatch=tool_dispatch,
            participation=participation
            if participation is not None
            else _participation_for_request(request, self.deps.runtime.config, self.deps.runtime_paths),
        )

    async def generate_response(self, request: ResponseRequest) -> str | None:
        """Generate and send/edit an agent response with lifecycle locking."""
        return await self._run_locked_response_lifecycle(
            request,
            response_kind="ai",
            locked_operation=lambda resolved_target, early_placeholder_state: self._generate_response_locked(
                request,
                resolved_target=resolved_target,
                early_placeholder_state=early_placeholder_state,
            ),
        )

    async def _generate_response_locked(
        self,
        request: ResponseRequest,
        *,
        resolved_target: MessageTarget,
        early_placeholder_state: _EarlyPlaceholderState,
    ) -> str | None:
        """Own participation before any fallible locked request preparation."""
        participation = _participation_for_request(request, self.deps.runtime.config, self.deps.runtime_paths)
        try:
            return await self._run_locked_turn(
                request,
                team=None,
                resolved_target=resolved_target,
                early_placeholder_state=early_placeholder_state,
                participation=participation,
            )
        except (ReplyMembershipPendingError, RevisionSnapshotChangedError, ResponseAdmissionRefusedError):
            raise
        except Exception as error:
            if (
                participation is None
                or participation.decision is not None
                or early_placeholder_state.settlement_started
            ):
                raise
            participation.decline("preparation_failed")
            self.deps.logger.exception("Response preparation skipped before participation", error=str(error))
            handle = current_span()
            settled_by_reply = False
            if handle is not None and not handle.exited:
                settled_by_reply = True
                # The declined turn shows nothing; its reply ends while the conversation is still held.
                await self.deps.delivery_gateway.end_reply_span(handle, suppress_decision(handle))
            lifecycle = self._build_lifecycle(
                identity=self._response_identity(request, response_kind="ai"),
                request=request,
            )
            await lifecycle.finalize(
                _skipped_participation_outcome(),
                build_post_response_outcome=lambda _outcome: ResponseOutcome(run_succeeded=False),
                post_response_deps=lambda: self._post_response_deps(request),
            )
            if not settled_by_reply and request.on_no_response_handled is not None:
                # No reply span exists to settle the sources.
                await request.on_no_response_handled()
            return None

    async def _cold_cache_model(
        self,
        request: ResponseRequest,
        *,
        runtime_model: ResolvedRuntimeModel,
        thread_id: str | None,
    ) -> str:
        """Let a judge move a thread whose prompt cache went cold to the agent's cheap model."""
        resolved_model = runtime_model.model_name
        config = self.deps.runtime.config
        agent = config.agents.get(self.deps.agent_name)
        cheap_model = config.get_agent_cheap_model(self.deps.agent_name) if agent is not None else None
        if (
            agent is None
            or agent.cold_cache is None
            or cheap_model is None
            # Scheduled, thread, and room choices are explicit, and an earlier switch is a thread choice.
            or runtime_model.overridden
            or resolved_model != agent.model
            # Only a thread can be switched for good.
            or thread_id is None
            # A regenerated or resumed answer follows one the agent model just gave.
            or request.edit_regeneration
            or _resumed_reply() is not None
            # The judge cannot see media, and the cheap model may not read it.
            or _request_has_media(request)
        ):
            return resolved_model
        selected = await select_cold_cache_model(
            agent.cold_cache,
            config=config,
            runtime_paths=self.deps.runtime_paths,
            agent_name=self.deps.agent_name,
            agent_model=resolved_model,
            cheap_model=cheap_model,
            idle_seconds=idle_seconds_before_turn(
                request.thread_history,
                agent_user_id=self.deps.matrix_full_id,
                source_event_ids=request.sources.logical_source_event_ids,
                now=time.time(),
            ),
            context=self._judgment_context(request),
            prompt=request.prompt,
        )
        if selected == resolved_model:
            return selected
        try:
            # The thread stays on the cheap model, like a `!model` choice, until someone changes it.
            set_thread_model_override(
                self.deps.runtime_paths,
                thread_id=thread_id,
                model_name=selected,
                entity_names=(self.deps.agent_name,),
                config=config,
            )
        except OSError:
            # The switch is an optimization, so a failed save must not fail the reply.
            self.deps.logger.warning("cold_cache_switch_save_failed", thread_id=thread_id, exc_info=True)
            return resolved_model
        return selected

    async def _prepare_agent_turn(
        self,
        request: ResponseRequest,
        *,
        resolved_target: MessageTarget,
        runtime_model: ResolvedRuntimeModel,
        history_scope: HistoryScope,
        execution_identity: ToolExecutionIdentity,
        participation: ParticipationGate | None,
    ) -> _LockedTurn:
        """Prepare an admitted agent turn: its model, runtime, effects, and the stream that runs it."""
        memory_prompt, memory_thread_history, model_prompt_text, model_thread_history = (
            prepare_memory_and_model_context(
                request.prompt,
                request.thread_history,
                config=self.deps.runtime.config,
                runtime_paths=self.deps.runtime_paths,
                timezone=self.deps.runtime.config.get_entity_timezone(self.deps.agent_name),
                model_prompt=request.model_prompt,
            )
        )
        normalized_request = replace(
            request,
            prompt=memory_prompt,
            model_prompt=model_prompt_text,
            thread_history=model_thread_history,
            media=request.media or MediaInputs(),
        )
        session_id = resolved_target.session_id
        active_model_name = await self._cold_cache_model(
            request,
            runtime_model=runtime_model,
            thread_id=_response_thread_id(request, resolved_target),
        )
        if request.pipeline_timing is not None:
            request.pipeline_timing.mark("response_runtime_start")
        runtime = await self.prepare_response_runtime(
            normalized_request,
            active_model_name=active_model_name,
            participation=participation,
        )
        if request.pipeline_timing is not None:
            request.pipeline_timing.mark("response_runtime_ready")
        queue_memory_persistence = self._memory_persistence(
            agent_name=self.deps.agent_name,
            session_id=session_id,
            execution_identity=execution_identity,
            prompt=memory_prompt,
            thread_history=memory_thread_history,
            user_id=request.user_id,
            hook_source=request.response_envelope.hook_source,
            origin=request.response_envelope.origin,
        )
        queue_skill_review, runtime = self._response_skill_review(
            request,
            runtime,
            session_id=session_id,
            execution_identity=execution_identity,
        )
        persist_response_event_id = self._build_persist_response_event_id_effect(
            session_id=session_id,
            scope=history_scope,
            execution_identity=execution_identity,
        )
        run_id = str(uuid4())
        turn_request = self._request_with_locked_target(normalized_request, runtime.resolved_target)

        async def stream(
            active_event_ids: set[str],
            compaction_lifecycle: MatrixCompactionLifecycle | None,
            sinks: TurnSinks,
        ) -> AsyncIterator[StreamInputChunk]:
            pipeline_timing = turn_request.pipeline_timing
            if pipeline_timing is not None:
                pipeline_timing.mark("knowledge_access_start")
            knowledge_resolution = await self.deps.knowledge_access.resolve_for_agent_async(
                self.deps.agent_name,
                execution_identity=runtime.tool_dispatch.execution_identity,
            )
            if pipeline_timing is not None:
                pipeline_timing.mark("knowledge_access_ready")
            ctx = self._agent_turn_context(
                turn_request,
                runtime=runtime,
                run_id=run_id,
                active_event_ids=active_event_ids,
                knowledge=knowledge_resolution.knowledge,
                transient_enrichment_items=append_knowledge_availability_enrichment(
                    turn_request.transient_enrichment_items,
                    knowledge_resolution.unavailable,
                ),
                compaction_lifecycle=compaction_lifecycle,
            )
            response_stream = stream_agent_response(ctx, self.deps.runtime_paths, self.deps.runtime.config, sinks=sinks)
            async with closing_async_stream(response_stream):
                async for chunk in response_stream:
                    yield chunk

        return _LockedTurn(
            request=request,
            delivery_request=turn_request,
            response_target=resolved_target,
            response_kind="ai",
            history_scope=history_scope,
            execution_identity=execution_identity,
            runtime=runtime,
            run_id=run_id,
            streaming_cls=StreamingResponse,
            stream=stream,
            post_response_deps=lambda: self._post_response_deps(
                request,
                queue_memory_persistence=queue_memory_persistence,
                queue_skill_review=queue_skill_review,
                persist_response_event_id=persist_response_event_id,
            ),
        )
