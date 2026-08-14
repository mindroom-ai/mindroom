"""Response lifecycle execution extracted from ``bot.py``."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast
from uuid import uuid4

from agno.db.base import SessionType
from agno.run.base import RunStatus
from agno.session.agent import AgentSession
from agno.session.team import TeamSession

from mindroom.agent_run_context import append_knowledge_availability_enrichment
from mindroom.agents import show_tool_calls_for_agent
from mindroom.ai import ResponseTurnContext, ai_response, build_matrix_run_metadata, stream_agent_response
from mindroom.ai_run_metadata import ai_run_extra_content_from_metadata
from mindroom.approval_execution import AgentApprovalExecution
from mindroom.approval_response import (
    ApprovalResponseCoordinator,
    continuation_target,
    identify_approval_tools,
)
from mindroom.background_tasks import create_background_task, run_coroutine_until_complete
from mindroom.constants import (
    ATTACHMENT_IDS_KEY,
    DURABLE_FINAL_OUTCOME_KEY,
    MATRIX_MESSAGE_TARGET_ENRICHMENT_KEY,
    MATRIX_SOURCE_EVENT_IDS_METADATA_KEY,
    ORIGINAL_SENDER_KEY,
    ROUTER_AGENT_NAME,
    STREAM_STATUS_APPROVAL_PENDING,
    STREAM_STATUS_COMPLETED,
    STREAM_STATUS_KEY,
    STREAM_STATUS_PENDING,
)
from mindroom.entity_resolution import current_internal_sender_ids, entity_identity_registry
from mindroom.event_journal import (
    ApprovalContinuation,
    ApprovalMemoryTurn,
    MatrixDelivery,
)
from mindroom.event_journal import (
    ApprovalDecision as ContinuationDecision,
)
from mindroom.final_delivery import FinalDeliveryOutcome, StreamTransportOutcome
from mindroom.history.interrupted_replay import persist_interrupted_replay_snapshot
from mindroom.history.storage import has_pending_force_compaction_scope, read_scope_state
from mindroom.history.turn_recorder import TurnRecorder
from mindroom.hooks import EnrichmentItem, MessageEnvelope
from mindroom.interactive import InteractiveMetadata
from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage, replace_visible_message
from mindroom.matrix.presence import should_use_streaming
from mindroom.matrix.typing import typing_indicator
from mindroom.memory import (
    mark_auto_flush_dirty_session,
    reprioritize_auto_flush_sessions,
    store_conversation_memory,
    strip_user_turn_time_prefix,
)
from mindroom.orchestration.runtime import (
    cancel_failure_reason,
    classify_cancel_source,
    log_cancelled_response,
    log_cancelled_response_source,
    request_task_cancel,
)
from mindroom.post_response_effects import PostResponseEffectsSupport, ResponseOutcome
from mindroom.response_attempt import ResponseAttemptDeps, ResponseAttemptRequest, ResponseAttemptRunner
from mindroom.response_terminal import (
    PendingVisibleResponse,
    TerminalFailureStatus,
    build_terminal_stream_transport_outcome,
)
from mindroom.response_turn import CompletedApprovalRun, PausedAttempt, ResponsePausedForApproval
from mindroom.runtime_shutdown import GENERIC_SHUTDOWN, RuntimeShutdownIntent
from mindroom.streaming import (
    INTERRUPTED_RESPONSE_NOTE,
    PROGRESS_PLACEHOLDER,
    RESTART_INTERRUPTED_RESPONSE_NOTE,
    ReplacementStreamingResponse,
    StreamingDeliveryError,
    StreamingResponse,
    clean_partial_reply_text,
    strip_visible_tool_markers,
)
from mindroom.sync_restart_retry import interrupted_source_needs_retry
from mindroom.teams import (
    TeamMode,
    continue_paused_team_run,
    select_model_for_team,
    team_response,
    team_response_stream,
)
from mindroom.thread_summary import thread_summary_message_count_hint
from mindroom.timing import DispatchPipelineTiming, timed
from mindroom.tool_system.dynamic_toolkits import visible_tool_surface
from mindroom.tool_system.runtime_context import ToolDispatchContext, runtime_context_from_dispatch_context
from mindroom.tool_system.worker_routing import (
    parse_tool_execution_identity_payload,
    run_with_tool_execution_identity,
    serialize_tool_execution_identity,
    stream_with_tool_execution_identity,
)
from mindroom.turn_origin import SenderKind, TurnIntent, TurnOrigin, TurnTrust
from mindroom.user_turn_time import prefix_user_turn_time

from .delivery_gateway import (
    CancelledVisibleNoteRequest,
    DeliveryGateway,
    DeliveryStage,
    EditTextRequest,
    FinalDeliveryRequest,
    FinalizeStreamedResponseRequest,
    MatrixCompactionLifecycle,
    ResponseIdentity,
    SendTextRequest,
    StreamingDeliveryRequest,
)
from .media_inputs import MediaInputs
from .response_admission import ResponseAdmissionRefusedError
from .response_lifecycle import (
    QueuedHumanNoticeReservation,
    ResponseLifecycle,
    ResponseLifecycleCoordinator,
    ResponseLifecycleDeps,
    ResponseLifecycleReservation,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Mapping, Sequence
    from pathlib import Path

    import nio
    import structlog
    from agno.db.base import BaseDb

    from mindroom.bot_runtime_view import BotRuntimeView
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.conversation_resolver import ConversationResolver
    from mindroom.conversation_state_writer import ConversationStateWriter
    from mindroom.dispatch_source import ScheduledHistoryBudget
    from mindroom.event_journal import PrincipalStore
    from mindroom.history.types import HistoryScope
    from mindroom.knowledge import KnowledgeAccessSupport
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.matrix.identity import MatrixID
    from mindroom.message_target import MessageTarget
    from mindroom.post_response_effects import PostResponseEffectsDeps
    from mindroom.response_payload_preparation import ResponsePayloadPreparation, ResponsePayloadPreparer
    from mindroom.stop import StopManager
    from mindroom.streaming import StreamInputChunk
    from mindroom.tool_system.events import ToolTraceEntry
    from mindroom.tool_system.runtime_context import ToolRuntimeSupport
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

    from .response_admission import ResponseAdmissionGate

type _MatrixEventId = str
_ToolContextResult = TypeVar("_ToolContextResult")
_ToolStreamChunk = TypeVar("_ToolStreamChunk")


def _merge_response_extra_content(
    extra_content: dict[str, Any] | None,
    attachment_ids: Sequence[str] | None,
) -> dict[str, Any] | None:
    """Merge optional attachment IDs into response metadata."""
    merged_extra_content = extra_content if extra_content is not None else {}
    if attachment_ids:
        merged_extra_content[ATTACHMENT_IDS_KEY] = list(attachment_ids)
    return merged_extra_content if extra_content is not None or attachment_ids else None


def _split_delivery_tool_trace(
    tool_trace: Sequence[ToolTraceEntry],
) -> tuple[list[ToolTraceEntry], list[ToolTraceEntry]]:
    """Split visible stream trace state into completed and still-interrupted tools."""
    completed: list[ToolTraceEntry] = []
    interrupted: list[ToolTraceEntry] = []
    for trace_entry in tool_trace:
        if trace_entry.type == "tool_call_completed":
            completed.append(trace_entry)
        else:
            interrupted.append(trace_entry)
    return completed, interrupted


def _materialize_matrix_run_metadata(
    matrix_run_metadata: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Return a concrete metadata dict for downstream APIs that require one."""
    if matrix_run_metadata is None:
        return None
    return dict(matrix_run_metadata)


def _agent_has_matrix_messaging_tool(config: Config, agent_name: str, session_id: str | None) -> bool:
    """Return whether one agent can issue Matrix message actions."""
    try:
        surface = visible_tool_surface(
            agent_name=agent_name,
            config=config,
            session_id=session_id,
            enable_dynamic_tools_manager=False,
        )
    except ValueError:
        return False
    return "matrix_message" in {entry.name for entry in surface.runtime_tool_configs}


def _cached_room_display_name(runtime: BotRuntimeView, room_id: str) -> str | None:
    """Return the room name already present in the synced Matrix cache."""
    client = runtime.client
    if client is None:
        return None
    room = client.rooms.get(room_id)
    if room is None or not room.display_name:
        return None
    return room.display_name


def _matrix_message_target_item(
    target: MessageTarget,
    *,
    matrix_message_available: bool,
    runtime: BotRuntimeView,
) -> EnrichmentItem | None:
    """Build transient Matrix targeting context when the tool is available."""
    if not matrix_message_available:
        return None
    room_display_name = _cached_room_display_name(runtime, target.room_id)
    room_label = f"{room_display_name!r} (room ID {target.room_id})" if room_display_name else target.room_id
    thread_id = target.resolved_thread_id
    if thread_id is None:
        text = (
            f"You are responding in Matrix room {room_label}, outside any thread. "
            "When calling matrix_message here, use this room_id and do not pass thread_id."
        )
    else:
        text = (
            f"You are responding in Matrix room {room_label}, in thread {thread_id}. "
            "When calling matrix_message here, use this room_id and thread_id."
        )
    text += " Use a current or selected <msg event_id> as target for reactions and edits."
    return EnrichmentItem(
        key=MATRIX_MESSAGE_TARGET_ENRICHMENT_KEY,
        text=text,
        cache_policy="stable",
        persist=False,
    )


def _with_matrix_message_target(
    items: Sequence[EnrichmentItem],
    target_item: EnrichmentItem | None,
) -> tuple[EnrichmentItem, ...]:
    """Replace any hook-provided Matrix target with runtime-owned context."""
    filtered_items = tuple(item for item in items if item.key != MATRIX_MESSAGE_TARGET_ENRICHMENT_KEY)
    if target_item is None:
        return filtered_items
    return (*filtered_items, target_item)


def _timestamp_thread_history_user_turns(
    thread_history: Sequence[ResolvedVisibleMessage],
    *,
    config: Config,
    runtime_paths: RuntimePaths,
) -> list[ResolvedVisibleMessage]:
    """Add local timestamps to user-authored thread history entries."""
    timestamped_history: list[ResolvedVisibleMessage] = []
    registry = entity_identity_registry(config, runtime_paths)
    for message in thread_history:
        is_user_turn = (
            isinstance(message.content.get(ORIGINAL_SENDER_KEY), str)
            or registry.current_entity_name_for_user_id(message.sender) is None
        )
        if not is_user_turn:
            timestamped_history.append(message)
            continue

        timestamped_body = prefix_user_turn_time(
            message.body,
            timezone=config.timezone,
            timestamp_ms=message.timestamp,
        )
        timestamped_history.append(replace_visible_message(message, body=timestamped_body))
    return timestamped_history


def prepare_memory_and_model_context(
    prompt: str,
    thread_history: Sequence[ResolvedVisibleMessage],
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    model_prompt: str | None = None,
) -> tuple[str, Sequence[ResolvedVisibleMessage], str, list[ResolvedVisibleMessage]]:
    """Return raw memory inputs alongside timestamped model-facing context."""
    model_prompt_content = model_prompt or prompt
    if model_prompt is not None and prompt:
        normalized_model_prompt = model_prompt.strip()
        normalized_prompt = prompt.strip()
        normalized_model_prompt_without_time = strip_user_turn_time_prefix(normalized_model_prompt)
        if (
            normalized_model_prompt == normalized_prompt
            or normalized_model_prompt.startswith(f"{normalized_prompt}\n\n")
            or normalized_model_prompt_without_time == normalized_prompt
            or normalized_model_prompt_without_time.startswith(f"{normalized_prompt}\n\n")
        ):
            model_prompt_content = model_prompt
        else:
            model_prompt_content = f"{prompt}\n\n{model_prompt}"
    model_thread_history = _timestamp_thread_history_user_turns(
        thread_history,
        config=config,
        runtime_paths=runtime_paths,
    )
    return prompt, thread_history, model_prompt_content, model_thread_history


@dataclass(frozen=True)
class ResponseRequest:
    """Typed carrier for one response lifecycle request."""

    thread_history: Sequence[ResolvedVisibleMessage]
    prompt: str
    response_envelope: MessageEnvelope
    model_prompt: str | None = None
    existing_event_id: str | None = None
    existing_event_is_placeholder: bool = False
    user_id: str | None = None
    media: MediaInputs | None = None
    attachment_ids: tuple[str, ...] | None = None
    correlation_id: str | None = None
    matrix_run_metadata: Mapping[str, Any] | None = None
    transient_enrichment_items: tuple[EnrichmentItem, ...] = ()
    system_enrichment_items: tuple[EnrichmentItem, ...] = ()
    requires_model_history_refresh: bool = False
    scheduled_history_budget: ScheduledHistoryBudget | None = None
    payload_preparation: ResponsePayloadPreparation | None = None
    current_timestamp_ms: float | None = None
    current_prompt_is_structured: bool = False
    on_lifecycle_lock_acquired: Callable[[], None] | None = None
    prepare_source_turn: Callable[[], Coroutine[Any, Any, bool]] | None = None
    on_source_turn_suppressed: Callable[[], Awaitable[None]] | None = None
    pipeline_timing: DispatchPipelineTiming | None = None
    on_interrupted_response_recoverable: Callable[[], None] | None = None
    sync_restart_retry_source_event_id: str | None = None
    on_deferred_outcome_handled: Callable[[str], Awaitable[None]] | None = None
    on_user_stop_handled: Callable[[str, int], Awaitable[None]] | None = None
    on_visible_response: Callable[[str], Awaitable[None]] | None = None
    # Set only after another durable owner can finish the source.
    source_handoff: asyncio.Event | None = None

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


class PostLockRequestPreparationError(RuntimeError):
    """Raised when post-lock request preparation fails before generation starts."""

    def __init__(
        self,
        message: str = "Post-lock request preparation failed",
        *,
        placeholder_event_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.placeholder_event_id = placeholder_event_id


@dataclass
class _EarlyPlaceholderState:
    """Track an early placeholder until normal response settlement takes ownership."""

    placeholder_event_id: str | None = None
    request: ResponseRequest | None = None
    settlement_started: bool = False


@dataclass
class _DeliveryProgress:
    """Mutable pre/post-delivery state for one locked response turn."""

    tracked_event_id: str | None = None
    stage_started: bool = False
    failure_reason: str | None = None
    cancelled: bool = False
    delivery_outcome: FinalDeliveryOutcome | None = None

    def note_delivery_started(self, event_id: str | None) -> None:
        """Mark visible delivery as begun, tracking the event carrying it."""
        self.stage_started = True
        self.track_event(event_id)

    def note_task_cancelled(self, failure_reason: str) -> None:
        """Record that the response task was cancelled before delivery settled."""
        self.failure_reason = failure_reason
        self.cancelled = True

    def track_event(self, event_id: str | None) -> None:
        """Remember the latest Matrix event a terminal note could edit."""
        if event_id:
            self.tracked_event_id = event_id

    def settle(self, delivery_outcome: FinalDeliveryOutcome) -> None:
        """Record the turn's one canonical terminal delivery outcome."""
        if self.delivery_outcome is not None:
            msg = "Response delivery already settled"
            raise RuntimeError(msg)
        self.delivery_outcome = delivery_outcome


@dataclass(frozen=True)
class _ResponseGenerationOutcome:
    """What one locked response generation produced, returned instead of out-params."""

    delivery: FinalDeliveryOutcome
    run_succeeded: bool


@dataclass(frozen=True)
class _NonStreamingGeneration:
    """One non-streaming AI generation's artifacts, returned instead of out-params.

    The streaming path keeps caller-owned collectors instead: its run-metadata
    dict must be live while the delivery gateway snapshots extra_content, and
    its artifacts must survive the raising StreamingDeliveryError exit.
    """

    response_text: str
    tool_trace: list[ToolTraceEntry]
    run_metadata_content: dict[str, Any]


def _generation_outcome(
    delivery: FinalDeliveryOutcome,
    turn_recorder: TurnRecorder,
) -> _ResponseGenerationOutcome:
    """Assemble one generation outcome from the turn's recorder."""
    return _ResponseGenerationOutcome(
        delivery=delivery,
        run_succeeded=turn_recorder.outcome == "completed",
    )


@dataclass(frozen=True)
class _TeamResponseRequest:
    """Typed carrier for one team response request plus team-specific inputs."""

    request: ResponseRequest
    team_agents: tuple[MatrixID, ...]
    team_mode: str
    reason_prefix: str = "Team request"
    resolution_reason: str | None = None


@dataclass(frozen=True)
class ResponseRunnerDeps:
    """Explicit collaborators for the response lifecycle."""

    runtime: BotRuntimeView
    logger: structlog.stdlib.BoundLogger
    stop_manager: StopManager
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
    retry_approval_sources: Callable[[tuple[str, ...]], None]
    approval_runtime_generation: str


@dataclass(frozen=True)
class _PreparedResponseRuntime:
    """Resolved runtime context shared by streaming and non-streaming responses."""

    resolved_target: MessageTarget
    response_thread_id: str | None
    media_inputs: MediaInputs
    session_id: str
    model_prompt: str
    tool_dispatch: ToolDispatchContext


@dataclass(frozen=True)
class _InboxResponseOwnership:
    """Recovery callbacks retained with one detached inbox response."""

    recovery_proof_ready: Callable[[], bool]
    on_failure: Callable[[], None] | None
    source_event_ids: frozenset[str]


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
    _inbox_response_tasks: dict[asyncio.Task[None], _InboxResponseOwnership] = field(default_factory=dict, init=False)
    _incomplete_inbox_responses_recoverable: bool = field(default=True, init=False)
    _admission_shutdown_requested: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)
    _user_stop_receipt_orders: dict[str, set[int]] = field(default_factory=dict, init=False, repr=False)
    _approval_responses: ApprovalResponseCoordinator = field(init=False, repr=False)
    _approval_execution: AgentApprovalExecution = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Bind response-side approval collaborators to the event journal."""
        self._approval_responses = ApprovalResponseCoordinator(
            config=lambda: self.deps.runtime.config,
            runtime_paths=self.deps.runtime_paths,
            store=self.deps.approval_store,
            delivery_gateway=self.deps.delivery_gateway,
            retry_sources=self.deps.retry_approval_sources,
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

    def track_inbox_response(
        self,
        response: Coroutine[Any, Any, None],
        *,
        name: str,
        recovery_proof_ready: Callable[[], bool],
        on_failure: Callable[[], None] | None = None,
        source_event_ids: tuple[str, ...] = (),
    ) -> asyncio.Task[None]:
        """Own one detached inbox response until it completes or a drain settles it."""
        task = asyncio.create_task(response, name=name)
        self._inbox_response_tasks[task] = _InboxResponseOwnership(
            recovery_proof_ready=recovery_proof_ready,
            on_failure=on_failure,
            source_event_ids=frozenset(source_event_ids),
        )
        task.add_done_callback(self._finish_inbox_response_task)
        return task

    def has_live_inbox_response(self, source_event_id: str) -> bool:
        """Return whether a managed response task still owns one journal source."""
        return any(source_event_id in ownership.source_event_ids for ownership in self._inbox_response_tasks.values())

    @property
    def pending_inbox_response_count(self) -> int:
        """Return an event-loop-local snapshot of runner-owned unsettled responses."""
        return sum(not task.done() for task in self._inbox_response_tasks)

    @property
    def incomplete_inbox_responses_recoverable(self) -> bool:
        """Return whether every timed-out response has finished cleanup with recovery proof."""
        return self._incomplete_inbox_responses_recoverable

    def _finish_inbox_response_task(self, task: asyncio.Task[None]) -> None:
        ownership = self._inbox_response_tasks.pop(task, None)
        if ownership is not None and ownership.source_event_ids:
            self.deps.retry_approval_sources(tuple(ownership.source_event_ids))
        if task.cancelled():
            return
        error = task.exception()
        if isinstance(error, ResponseAdmissionRefusedError):
            # The Matrix callback awaiting this pre-lock task surfaces the
            # refusal into sync-checkpoint failure accounting.
            return
        if error is not None:
            if ownership is not None and ownership.on_failure is not None:
                ownership.on_failure()
            self.deps.logger.error(
                "inbox_response_task_failed",
                task_name=task.get_name(),
                exception_type=error.__class__.__name__,
                error=str(error),
            )

    async def drain_inbox_responses(
        self,
        *,
        cancel_after_seconds: float | None = None,
        shutdown_intent: RuntimeShutdownIntent = GENERIC_SHUTDOWN,
    ) -> bool:
        """Settle detached inbox responses: graceful drains await, bounded drains cancel.

        Returns False when a bounded drain had to cancel or abandon running work.
        A bounded drain may take up to two cancel_after_seconds windows: one
        waiting for completion and one letting cancelled tasks run cleanup.
        """
        tasks = [task for task in self._inbox_response_tasks if not task.done()]
        # Done callbacks pop tasks, so snapshot proofs before an await can run them.
        recovery_checks = {
            task: (
                self._inbox_response_tasks[task].recovery_proof_ready
                if task in self._inbox_response_tasks
                else lambda: True
            )
            for task in tasks
        }
        if not tasks:
            return True
        if cancel_after_seconds is None:
            await asyncio.gather(*tasks, return_exceptions=True)
            return True
        _done, pending = await asyncio.wait(tasks, timeout=cancel_after_seconds)
        if not pending:
            return True
        for task in pending:
            request_task_cancel(task, cancel_source=shutdown_intent.cancel_source)
        await asyncio.wait(pending, timeout=cancel_after_seconds)
        cancelled_responses_recoverable = all(task.done() and recovery_checks[task]() for task in pending)
        self._incomplete_inbox_responses_recoverable &= cancelled_responses_recoverable
        return False

    async def wait_for_source_owned_inbox_responses(self) -> None:
        """Wait for detached responses that still own durable journal sources."""
        tasks = [
            task
            for task, ownership in self._inbox_response_tasks.items()
            if not task.done() and ownership.source_event_ids
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _post_response_deps(
        self,
        request: ResponseRequest,
        *,
        queue_memory_persistence: Callable[[], None] | None = None,
        persist_response_event_id: Callable[[str, str], None] | None = None,
    ) -> PostResponseEffectsDeps:
        """Build post-response effect deps bound to one request's room."""
        return self.deps.post_response_effects.build_deps(
            room_id=request.room_id,
            membership_turn_id=request.response_envelope.source_event_id,
            queue_memory_persistence=queue_memory_persistence,
            persist_response_event_id=persist_response_event_id,
        )

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

    def _show_tool_calls(self, agent_name: str | None = None) -> bool:
        """Return tool-call visibility for the current or target agent."""
        return show_tool_calls_for_agent(
            self.deps.runtime.config,
            agent_name or self.deps.agent_name,
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
        progress: _DeliveryProgress,
        execution_identity: ToolExecutionIdentity,
        entity_kind: Literal["agent", "team"],
        history_scope: HistoryScope,
        team_member_names: tuple[str, ...] = (),
        team_mode: str | None = None,
    ) -> FinalDeliveryOutcome:
        """Persist a paused Agno run, expose its cards, and leave the response lock."""
        requester_id = request.user_id or execution_identity.requester_id
        if requester_id is None:
            msg = "Approval continuation requires the original requester identity"
            raise RuntimeError(msg)
        identified_tools = identify_approval_tools(
            paused,
            default_agent_name=self.deps.agent_name,
        )
        approval_id = uuid4().hex
        raw_source_event_ids = (
            request.matrix_run_metadata.get(MATRIX_SOURCE_EVENT_IDS_METADATA_KEY)
            if request.matrix_run_metadata is not None
            else None
        )
        source_event_ids = (
            tuple(
                dict.fromkeys(
                    (
                        request.response_envelope.source_event_id,
                        *(value for value in raw_source_event_ids if isinstance(value, str)),
                    ),
                ),
            )
            if isinstance(raw_source_event_ids, list)
            else (request.response_envelope.source_event_id,)
        )
        try:
            plan = await self._approval_responses.plan_pause(identified_tools, requester_id=requester_id)
            response_event_id = progress.tracked_event_id
            delivery_kind: Literal["sent", "edited"] = "edited"
            if response_event_id is None:
                response_event_id = await self.deps.delivery_gateway.send_text(
                    SendTextRequest(
                        target=target,
                        response_text=plan.waiting_text,
                        extra_content={STREAM_STATUS_KEY: STREAM_STATUS_APPROVAL_PENDING},
                        delivery_turn_id=request.response_envelope.source_event_id,
                        delivery_stage=DeliveryStage.INITIAL,
                    ),
                )
                delivery_kind = "sent"
            elif not await self.deps.delivery_gateway.edit_text(
                EditTextRequest(
                    target=target,
                    event_id=response_event_id,
                    new_text=plan.waiting_text,
                    extra_content={STREAM_STATUS_KEY: STREAM_STATUS_APPROVAL_PENDING},
                ),
            ):
                response_event_id = None
            if response_event_id is None:
                msg = "Could not publish the suspended approval response"
                raise RuntimeError(msg)  # noqa: TRY301

            continuation_state: Literal["waiting", "ready"] = (
                "ready" if all(call.decision is not None for call in plan.calls) else "waiting"
            )
            continuation = await self._approval_responses.create(
                ApprovalContinuation(
                    approval_id=approval_id,
                    run_id=paused.run_id,
                    session_id=paused.session_id,
                    entity_kind=entity_kind,
                    entity_name=self.deps.agent_name,
                    room_id=target.room_id,
                    thread_id=target.resolved_thread_id,
                    requester_id=requester_id,
                    response_event_id=response_event_id,
                    source_event_ids=source_event_ids,
                    calls=plan.calls,
                    state=continuation_state,
                    execution_identity=serialize_tool_execution_identity(execution_identity),
                    runtime_model_name=paused.runtime_model_name,
                    team_member_names=team_member_names,
                    team_mode=team_mode,
                    request_body=request.response_envelope.body,
                    transport_sender_id=request.response_envelope.sender_id,
                    source_kind=request.response_envelope.source_kind,
                    attachment_ids=tuple(request.attachment_ids or ()),
                    mentioned_agents=request.response_envelope.mentioned_agents,
                    hook_source=request.response_envelope.hook_source,
                    message_received_depth=request.response_envelope.message_received_depth,
                    dispatch_policy_source_kind=request.response_envelope.dispatch_policy_source_kind,
                    correlation_id=request.correlation_id,
                    history_scope=history_scope,
                    origin=request.response_envelope.origin,
                    memory_prompt=request.prompt,
                    memory_thread_history=tuple(
                        ApprovalMemoryTurn(sender=message.sender, body=message.body)
                        for message in request.thread_history
                    ),
                    thread_summary_message_count_hint=thread_summary_message_count_hint(
                        request.thread_history,
                        trusted_sender_ids=current_internal_sender_ids(
                            self.deps.runtime.config,
                            self.deps.runtime_paths,
                        ),
                    ),
                    runtime_generation=(
                        self.deps.approval_runtime_generation if continuation_state == "waiting" else None
                    ),
                ),
            )
            if continuation is None or continuation.state != continuation_state:
                msg = "Approval continuation lost its journal source ownership"
                raise RuntimeError(msg)  # noqa: TRY301
            progress.track_event(response_event_id)
            if delivery_kind == "sent" and request.on_visible_response is not None:
                await request.on_visible_response(response_event_id)

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
                final_visible_body=plan.waiting_text,
                delivery_kind=delivery_kind,
                extra_content={STREAM_STATUS_KEY: STREAM_STATUS_APPROVAL_PENDING},
            )
        except (asyncio.CancelledError, Exception) as error:
            handoff = await self._failed_approval_handoff(
                approval_id,
                error=error,
            )
            if handoff is not None:
                return handoff
            raise

    async def _execute_claimed_approval(
        self,
        claimed: ApprovalContinuation,
        *,
        request: ResponseRequest,
        target: MessageTarget,
    ) -> tuple[FinalDeliveryOutcome, ApprovalContinuation]:
        """Run and classify one claimed continuation for either lifecycle entry path."""
        tool_trace: list[ToolTraceEntry] = []
        result = await self._continue_entity_call(
            claimed,
            request=request,
            target=target,
            tool_trace_collector=tool_trace,
        )
        if isinstance(result, CompletedApprovalRun):
            current = await self.deps.approval_store.approval_continuation(claimed.approval_id) or claimed
            return (
                await self.deps.delivery_gateway.deliver_final(
                    FinalDeliveryRequest(
                        target=target,
                        existing_event_id=claimed.response_event_id,
                        existing_event_is_placeholder=False,
                        response_text=result.response_text,
                        identity=self._response_identity(
                            request,
                            response_kind="team" if claimed.entity_kind == "team" else "ai",
                        ),
                        tool_trace=tool_trace if self._show_tool_calls() else None,
                        extra_content=_merge_response_extra_content(
                            {**result.metadata_content, STREAM_STATUS_KEY: STREAM_STATUS_COMPLETED},
                            claimed.attachment_ids,
                        ),
                        defer_source_handoff=True,
                    ),
                ),
                current,
            )
        waiting_text = await self._approval_responses.advance_pause(
            claimed,
            result,
            target=target,
            tool_trace=tool_trace if self._show_tool_calls() else [],
        )
        current = await self.deps.approval_store.approval_continuation(claimed.approval_id) or claimed
        return (
            FinalDeliveryOutcome(
                terminal_status="suspended",
                event_id=claimed.response_event_id,
                is_visible_response=True,
                final_visible_body=waiting_text,
                delivery_kind="edited",
                tool_trace=tuple(tool_trace) if self._show_tool_calls() else (),
                extra_content={STREAM_STATUS_KEY: STREAM_STATUS_APPROVAL_PENDING},
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
        progress = _DeliveryProgress(tracked_event_id=claimed.response_event_id)
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
                progress.settle(self._approval_outcome_from_delivery(delivery))
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
            run_id=claimed.run_id,
            build_post_response_outcome=lambda final: self._approval_post_response_outcome(
                post_effect_continuation,
                target=target,
                run_succeeded=final.terminal_status == "completed",
            ),
            post_response_deps=lambda: self._approval_post_response_deps(claimed),
        )
        outcome = progress.delivery_outcome
        if outcome is None:
            msg = "Approval continuation ended without a terminal lifecycle outcome"
            raise RuntimeError(msg)
        if outcome.terminal_status == "completed":
            if not await self.deps.approval_store.finish_approval_continuation(claimed.approval_id):
                msg = "Approval continuation final delivery was not durably acknowledged"
                raise RuntimeError(msg)
        elif outcome.terminal_status != "suspended":
            reason = outcome.failure_reason or "Tool approval continuation failed safely."
            await self._approval_responses.settle_failure(post_effect_continuation, reason)
        return outcome

    @staticmethod
    def _approval_outcome_from_delivery(delivery: MatrixDelivery) -> FinalDeliveryOutcome:
        """Restore semantic lifecycle facts from one frozen approval FINAL."""
        acknowledged_event_id = delivery.acknowledged_event_id
        if acknowledged_event_id is None:
            msg = "Approval final delivery is not acknowledged"
            raise RuntimeError(msg)
        event_id = delivery.edits_event_id or acknowledged_event_id
        payload = dict(delivery.payload)
        nested = payload.get("m.new_content")
        visible = cast("dict[str, Any]", nested) if isinstance(nested, dict) else payload
        semantic = visible.get(DURABLE_FINAL_OUTCOME_KEY)
        semantic_body: object = None
        interactive_metadata = None
        if isinstance(semantic, dict):
            stored_semantic = cast("dict[str, object]", semantic)
            semantic_body = stored_semantic.get("body")
            interactive_metadata = InteractiveMetadata.from_metadata(stored_semantic.get("interactive"))
        body = semantic_body if isinstance(semantic_body, str) else visible.get("body")
        if not isinstance(body, str):
            body = "Tool approval continuation completed"
        return FinalDeliveryOutcome(
            terminal_status="completed",
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
        """Recover frozen FINAL debt without invoking Agno a second time."""
        owns_final, event_id = await self._recover_frozen_approval_final(claimed, target=target)
        if owns_final:
            return event_id
        reason = "Tool approval continuation was interrupted before final delivery and denied safely."
        settled = await self._approval_responses.settle_failure(claimed, reason)
        return claimed.response_event_id if settled else None

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
        if delivery.acknowledged_event_id is None:
            return True, None
        recovered_outcome = self._approval_outcome_from_delivery(delivery)
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
            build_post_response_outcome=lambda _final: self._approval_post_response_outcome(
                claimed,
                target=target,
                run_succeeded=True,
            ),
            post_response_deps=lambda: self._approval_post_response_deps(claimed),
        )
        if not await self.deps.approval_store.finish_approval_continuation(claimed.approval_id):
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
        transport_sender_id = continuation.transport_sender_id or continuation.requester_id
        relayed = transport_sender_id != continuation.requester_id
        origin = continuation.origin or TurnOrigin(
            transport_sender_id=transport_sender_id,
            requester_id=continuation.requester_id,
            sender_entity_name=ROUTER_AGENT_NAME if relayed else None,
            requester_entity_name=None,
            sender_kind=SenderKind.MANAGED_ENTITY if relayed else SenderKind.USER,
            requester_kind=SenderKind.USER,
            intent=TurnIntent.ROUTER_HANDOFF if relayed else TurnIntent.USER_MESSAGE,
            source_kind=continuation.source_kind,
            trust=TurnTrust.TRUSTED_INTERNAL if relayed else TurnTrust.EXTERNAL,
        )
        envelope = MessageEnvelope(
            source_event_id=continuation.source_event_ids[0],
            target=target,
            body=continuation.request_body,
            attachment_ids=continuation.attachment_ids,
            mentioned_agents=continuation.mentioned_agents,
            agent_name=continuation.entity_name,
            origin=origin,
            hook_source=continuation.hook_source,
            message_received_depth=continuation.message_received_depth,
            dispatch_policy_source_kind=continuation.dispatch_policy_source_kind,
        )
        return ResponseRequest(
            thread_history=self._approval_memory_history(continuation),
            prompt=envelope.body,
            response_envelope=envelope,
            existing_event_id=continuation.response_event_id,
            user_id=continuation.requester_id,
            attachment_ids=continuation.attachment_ids,
            correlation_id=continuation.correlation_id,
        )

    def _approval_post_response_outcome(
        self,
        continuation: ApprovalContinuation,
        *,
        target: MessageTarget,
        run_succeeded: bool,
    ) -> ResponseOutcome:
        """Build normal post-response facts for one resumed native run."""
        execution_identity = parse_tool_execution_identity_payload(
            continuation.execution_identity,
            error_prefix="Approval continuation execution_identity",
        )
        return ResponseOutcome(
            response_run_id=continuation.run_id,
            session_id=continuation.session_id,
            session_type=SessionType.TEAM if continuation.entity_kind == "team" else SessionType.AGENT,
            execution_identity=execution_identity,
            run_succeeded=run_succeeded,
            response_target=target,
            thread_summary_room_id=continuation.room_id if target.resolved_thread_id is not None else None,
            thread_summary_thread_id=target.resolved_thread_id,
            thread_summary_message_count_hint=continuation.thread_summary_message_count_hint,
            thread_summary_entity_name=continuation.entity_name,
            memory_prompt=(
                continuation.memory_prompt
                if continuation.memory_prompt is not None
                else continuation.request_body or None
            ),
            memory_thread_history=self._approval_memory_history(continuation),
        )

    def _approval_post_response_deps(self, continuation: ApprovalContinuation) -> PostResponseEffectsDeps:
        """Build normal post-response dependencies for a continued run."""
        return self.deps.post_response_effects.build_deps(
            room_id=continuation.room_id,
            membership_turn_id=continuation.source_event_ids[0],
            queue_memory_persistence=self._approval_memory_persistence(continuation),
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

    def _approval_memory_persistence(self, continuation: ApprovalContinuation) -> Callable[[], None] | None:
        """Return the normal agent-memory handoff for a completed continuation."""
        if continuation.entity_kind != "agent":
            return None
        execution_identity = parse_tool_execution_identity_payload(
            continuation.execution_identity,
            error_prefix="Approval continuation execution_identity",
        )
        if execution_identity is None:
            return None
        return self._memory_persistence(
            agent_name=continuation.entity_name,
            session_id=continuation.session_id,
            execution_identity=execution_identity,
            prompt=(
                continuation.memory_prompt if continuation.memory_prompt is not None else continuation.request_body
            ),
            thread_history=self._approval_memory_history(continuation),
            user_id=continuation.requester_id,
        )

    def _memory_persistence(
        self,
        *,
        agent_name: str,
        session_id: str,
        execution_identity: ToolExecutionIdentity,
        prompt: str,
        thread_history: Sequence[ResolvedVisibleMessage],
        user_id: str | None,
    ) -> Callable[[], None]:
        """Build the shared completed-agent memory handoff."""

        def queue() -> None:
            mark_auto_flush_dirty_session(
                self.deps.storage_path,
                self.deps.runtime.config,
                agent_name=agent_name,
                session_id=session_id,
                execution_identity=execution_identity,
            )
            if self.deps.runtime.config.resolve_entity(agent_name).memory_backend == "mem0":
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
                    name=f"memory_save_{agent_name}_{session_id}",
                    owner=self.deps.runtime,
                )

        return queue

    def _approval_response_event_persistence(
        self,
        continuation: ApprovalContinuation,
    ) -> Callable[[str, str], None] | None:
        """Return the normal run-to-Matrix event linkage for a resumed response."""
        execution_identity = parse_tool_execution_identity_payload(
            continuation.execution_identity,
            error_prefix="Approval continuation execution_identity",
        )
        if execution_identity is None:
            return None
        history_scope = continuation.history_scope
        if history_scope is None:
            if continuation.entity_kind == "team":
                return None
            history_scope = self.deps.state_writer.history_scope()
        return self._build_persist_response_event_id_effect(
            session_id=continuation.session_id,
            session_type=self.deps.state_writer.session_type_for_scope(history_scope),
            create_storage=lambda: self.deps.state_writer.create_storage(
                execution_identity,
                scope=history_scope,
            ),
        )

    async def _continue_entity_call(
        self,
        continuation: ApprovalContinuation,
        *,
        request: ResponseRequest,
        target: MessageTarget,
        tool_trace_collector: list[ToolTraceEntry],
    ) -> CompletedApprovalRun | PausedAttempt:
        execution_identity = parse_tool_execution_identity_payload(
            continuation.execution_identity,
            error_prefix="Approval continuation execution_identity",
        )
        if execution_identity is None:
            msg = f"Approval continuation {continuation.approval_id!r} has no execution identity"
            raise RuntimeError(msg)
        tool_dispatch = self.deps.tool_runtime.build_dispatch_context(
            target,
            user_id=continuation.requester_id,
            agent_name=continuation.entity_name,
            active_model_name=continuation.runtime_model_name,
            attachment_ids=continuation.attachment_ids,
            correlation_id=self._correlation_id_for_request(request),
            source_envelope=request.response_envelope,
        )
        if tool_dispatch.execution_identity != execution_identity:
            msg = "Approval continuation execution identity no longer matches its target"
            raise RuntimeError(msg)
        decisions = {call.tool_call_id: call.decision is ContinuationDecision.APPROVED for call in continuation.calls}
        denial_reasons = {call.tool_call_id: call.reason for call in continuation.calls}
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
                    history_scope=continuation.history_scope,
                    tool_trace_collector=tool_trace_collector,
                )

            async with typing_indicator(self._client(), continuation.room_id):
                response_text = await self._run_in_tool_context(tool_dispatch=tool_dispatch, operation=continue_team)
        else:
            response_text = await self._approval_execution.continue_run(
                continuation,
                execution_identity=execution_identity,
                tool_dispatch=tool_dispatch,
                decisions=decisions,
                denial_reasons=denial_reasons,
                tool_trace_collector=tool_trace_collector,
            )
        return response_text

    def _build_turn_recorder(
        self,
        *,
        user_message: str,
        user_message_is_structured: bool,
        reply_to_event_id: str | None,
        requester_id: str | None,
        matrix_run_metadata: dict[str, Any] | None,
    ) -> TurnRecorder:
        """Create one lifecycle-owned recorder seeded with canonical Matrix metadata."""
        recorder = TurnRecorder(
            user_message=user_message,
            user_message_is_structured=user_message_is_structured,
        )
        recorder.set_run_metadata(
            build_matrix_run_metadata(
                reply_to_event_id,
                [],
                requester_id=requester_id,
                extra_metadata=matrix_run_metadata,
            ),
        )
        return recorder

    def _note_final_delivery_timing(self, request: ResponseRequest, delivery: FinalDeliveryOutcome) -> None:
        """Mark the terminal visible reply and response completion for one delivery."""
        if request.pipeline_timing is not None:
            request.pipeline_timing.mark_first_visible_reply(
                "final",
                substantive=delivery.delivered_substantive_content,
            )
            request.pipeline_timing.mark("response_complete")

    async def _persist_failed_turn(
        self,
        recorder: TurnRecorder,
        *,
        is_team: bool,
        session_scope: HistoryScope,
        session_id: str,
        execution_identity: ToolExecutionIdentity | None,
        run_id: str | None,
        response_event_id: str | None,
    ) -> None:
        """Persist one failed or interrupted turn that never completed."""
        if recorder.outcome in {"completed", "suspended"} or recorder.original_status is RunStatus.cancelled:
            return
        if recorder.outcome == "pending":
            recorder.mark_interrupted(RunStatus.error)
        await self._persist_interrupted_recorder_off_loop(
            recorder=recorder,
            session_scope=session_scope,
            session_id=session_id,
            execution_identity=execution_identity,
            run_id=run_id,
            is_team=is_team,
            response_event_id=response_event_id,
        )

    async def _settle_blocking_cancellation(
        self,
        exc: asyncio.CancelledError,
        *,
        message_id: str | None,
        delivery_target: MessageTarget,
        existing_event_is_placeholder: bool,
        response_identity: ResponseIdentity,
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
            return await self.deps.delivery_gateway.deliver_cancelled_visible_note(
                CancelledVisibleNoteRequest(
                    target=delivery_target,
                    event_id=message_id,
                    existing_event_is_placeholder=existing_event_is_placeholder,
                    cancel_source=cancel_source,
                    identity=response_identity,
                ),
            )
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
                existing_event_id=request.existing_event_id,
                existing_event_is_placeholder=request.existing_event_is_placeholder,
            ),
        )
        self._note_final_delivery_timing(request, delivery)
        return delivery

    def _persist_interrupted_turn(
        self,
        *,
        recorder: TurnRecorder,
        session_scope: HistoryScope,
        session_id: str,
        execution_identity: ToolExecutionIdentity | None,
        run_id: str | None,
        is_team: bool,
        response_event_id: str | None = None,
    ) -> None:
        """Persist one interrupted recorder snapshot exactly once."""
        if not recorder.claim_interrupted_persistence():
            return
        if response_event_id:
            recorder.set_response_event_id(response_event_id)
        storage = self.deps.state_writer.create_storage(execution_identity, scope=session_scope)
        try:
            persist_interrupted_replay_snapshot(
                storage=storage,
                session=None,
                session_id=session_id,
                scope_id=session_scope.scope_id,
                run_id=recorder.run_id or run_id or str(uuid4()),
                snapshot=recorder.interrupted_snapshot(),
                is_team=is_team,
            )
        finally:
            storage.close()

    def _ensure_recorder_interrupted(self, recorder: TurnRecorder) -> None:
        """Mark one recorder interrupted unless lower layers already captured richer state."""
        if recorder.outcome == "pending":
            recorder.mark_interrupted()

    def _persist_interrupted_recorder(
        self,
        *,
        recorder: TurnRecorder,
        session_scope: HistoryScope,
        session_id: str,
        execution_identity: ToolExecutionIdentity | None,
        run_id: str | None,
        is_team: bool,
        response_event_id: str | None = None,
    ) -> None:
        """Persist one interrupted recorder snapshot after marking it interrupted."""
        self._ensure_recorder_interrupted(recorder)
        self._persist_interrupted_turn(
            recorder=recorder,
            session_scope=session_scope,
            session_id=session_id,
            execution_identity=execution_identity,
            run_id=run_id,
            is_team=is_team,
            response_event_id=response_event_id,
        )

    async def _persist_interrupted_recorder_off_loop(
        self,
        *,
        recorder: TurnRecorder,
        session_scope: HistoryScope,
        session_id: str,
        execution_identity: ToolExecutionIdentity | None,
        run_id: str | None,
        is_team: bool,
        response_event_id: str | None = None,
    ) -> None:
        """Persist interrupted replay state without blocking the event loop."""
        offload = create_background_task(
            asyncio.to_thread(
                self._persist_interrupted_recorder,
                recorder=recorder,
                session_scope=session_scope,
                session_id=session_id,
                execution_identity=execution_identity,
                run_id=run_id,
                is_team=is_team,
                response_event_id=response_event_id,
            ),
            name="persist_interrupted_recorder",
            owner=self.deps.runtime,
        )
        try:
            await asyncio.shield(offload)
        except Exception:
            # A snapshot-persist failure (e.g. sqlite locked) must not escape into
            # the streaming error arms: they classify the adopted placeholder as
            # pristine and would redact an already-delivered visible reply. Losing
            # the replay snapshot is the lesser harm.
            self.deps.logger.exception("Failed to persist interrupted replay state")

    def _record_stream_delivery_error(
        self,
        *,
        recorder: TurnRecorder,
        accumulated_text: str,
        tool_trace: Sequence[ToolTraceEntry],
    ) -> bool:
        """Capture canonical interrupted replay state from one failed stream delivery."""
        if recorder.outcome != "pending":
            return recorder.outcome == "interrupted"
        partial_text = clean_partial_reply_text(strip_visible_tool_markers(accumulated_text))
        completed_tools, interrupted_tools = _split_delivery_tool_trace(tool_trace)
        if not partial_text:
            partial_text = recorder.assistant_text
        if not completed_tools:
            completed_tools = list(recorder.completed_tools)
        if not interrupted_tools:
            interrupted_tools = list(recorder.interrupted_tools)
        recorder.record_interrupted(
            run_metadata=recorder.run_metadata,
            assistant_text=partial_text,
            completed_tools=completed_tools,
            interrupted_tools=interrupted_tools,
            original_status=RunStatus.error,
        )
        return True

    def has_active_response_for_target(self, target: MessageTarget) -> bool:
        """Return whether one canonical conversation target already has an active turn."""
        return self._lifecycle_coordinator.has_active_response_for_target(target)

    def active_thread_ids_for_room(self, room_id: str) -> frozenset[str | None]:
        """Return canonical thread IDs with active response lifecycles in one room."""
        return self._lifecycle_coordinator.active_thread_ids_for_room(room_id)

    async def wait_for_thread_response_idle(self, room_id: str, thread_id: str | None) -> None:
        """Wait until one canonical room/thread has no active response turn."""
        await self._lifecycle_coordinator.wait_for_thread_idle(room_id, thread_id)

    async def _settle_user_stopped_approval(
        self,
        *,
        response_event_id: str,
        source_event_id: str,
        target: MessageTarget,
    ) -> bool | None:
        """Settle a stopped approval, returning ``None`` while frozen success remains unresolved."""
        continuation = await self.deps.approval_store.approval_continuation_for_source(source_event_id)
        if continuation is None:
            final_delivery = await self.deps.approval_store.load_matrix_delivery(
                delivery_id=source_event_id,
                stage=DeliveryStage.FINAL,
            )
            return bool(
                final_delivery is not None
                and final_delivery.acknowledged_event_id is not None
                and response_event_id in {final_delivery.edits_event_id, final_delivery.acknowledged_event_id},
            )
        if continuation.state == "claimed":
            owns_final, event_id = await self._recover_frozen_approval_final(
                continuation,
                target=target,
            )
            if owns_final:
                return True if event_id is not None else None
        failing = await self._approval_responses.request_failure(
            continuation,
            "cancelled_by_user",
        )
        if failing is None:
            failing = await self.deps.approval_store.approval_continuation(continuation.approval_id)
        if failing is None:
            return False
        return True if await self._approval_responses.settle_failure(failing, "cancelled_by_user") else None

    async def finalize_user_stop(
        self,
        message_id: str,
        source_event_id: str,
        target: MessageTarget,
        stop_receipt_order: int,
        should_cancel: Callable[[], bool],
        finalize: Callable[[bool], Awaitable[bool]],
    ) -> bool:
        """Cancel the live response, then durably finalize its turn under the same lock."""
        cancellation_requested = False
        self._user_stop_receipt_orders.setdefault(message_id, set()).add(stop_receipt_order)

        async def cancel_live_response() -> None:
            nonlocal cancellation_requested
            if cancellation_requested:
                return
            cancellation_requested = self.deps.stop_manager.request_stop_if(message_id, should_cancel)

        async def finalize_locked() -> bool:
            approval_settled = await self._settle_user_stopped_approval(
                response_event_id=message_id,
                source_event_id=source_event_id,
                target=target,
            )
            if approval_settled is None:
                return False
            return await finalize(approval_settled)

        try:
            return await self._lifecycle_coordinator.run_locked_target_operation(
                target=target,
                while_waiting=cancel_live_response,
                locked_operation=finalize_locked,
            )
        finally:
            receipt_orders = self._user_stop_receipt_orders.get(message_id)
            if receipt_orders is not None:
                receipt_orders.discard(stop_receipt_order)
            if not receipt_orders:
                self._user_stop_receipt_orders.pop(message_id, None)

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

    def _active_response_event_ids(self, room_id: str) -> set[str]:
        """Return still-running response event IDs for one room."""
        return {
            event_id
            for event_id, tracked in self.deps.stop_manager.tracked_messages.items()
            if tracked.target.room_id == room_id and not tracked.task.done()
        }

    async def _run_locked_response_lifecycle(
        self,
        request: ResponseRequest,
        *,
        response_kind: str,
        locked_operation: Callable[[MessageTarget, _EarlyPlaceholderState], Awaitable[str | None]],
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
        self._in_flight_response_count += 1
        try:
            resolved_target = request.response_envelope.target
            early_placeholder = _EarlyPlaceholderState()
            try:
                return await self._lifecycle_coordinator.run_locked_response(
                    target=resolved_target,
                    response_envelope=request.response_envelope,
                    pipeline_timing=request.pipeline_timing,
                    locked_operation=lambda target: self._run_owned_or_locked_response(
                        request,
                        target=target,
                        early_placeholder=early_placeholder,
                        locked_operation=locked_operation,
                    ),
                    signal_queued_message=request.sync_restart_retry_source_event_id is None,
                )
            except asyncio.CancelledError as error:
                if early_placeholder.placeholder_event_id is not None and not early_placeholder.settlement_started:
                    await self._finalize_early_placeholder_cancellation(
                        early_placeholder,
                        error,
                        response_kind=response_kind,
                    )
                raise
            except Exception as error:
                already_linked = (
                    isinstance(error, PostLockRequestPreparationError) and error.placeholder_event_id is not None
                )
                if (
                    early_placeholder.placeholder_event_id is None
                    or early_placeholder.settlement_started
                    or already_linked
                ):
                    raise
                cause = (
                    error.__cause__
                    if isinstance(error, PostLockRequestPreparationError) and isinstance(error.__cause__, Exception)
                    else error
                )
                raise PostLockRequestPreparationError(
                    placeholder_event_id=early_placeholder.placeholder_event_id,
                ) from cause
        finally:
            self._in_flight_response_count -= 1
            self._admission_gate.release()

    async def _run_owned_or_locked_response(
        self,
        request: ResponseRequest,
        *,
        target: MessageTarget,
        early_placeholder: _EarlyPlaceholderState,
        locked_operation: Callable[[MessageTarget, _EarlyPlaceholderState], Awaitable[str | None]],
    ) -> str | None:
        """Dispatch journal-owned approval work through normal turn serialization."""
        owned = await self.deps.approval_store.approval_continuation_for_source(
            request.response_envelope.source_event_id,
        )
        if owned is None:
            return await locked_operation(target, early_placeholder)
        self.deps.logger.info(
            "response_source_owned_by_approval_continuation",
            source_event_id=request.response_envelope.source_event_id,
            approval_id=owned.approval_id,
            approval_state=owned.state,
        )
        recovered, event_id = await self._recover_nonready_approval(owned, target=target)
        if recovered:
            return event_id
        claimed = await self.deps.approval_store.claim_approval_continuation(
            owned.approval_id,
            runtime_generation=self.deps.approval_runtime_generation,
        )
        if claimed is None:
            return None
        try:
            outcome = await self._run_claimed_approval_lifecycle(claimed, target=target)
        except asyncio.CancelledError:
            owns_final, event_id, _failing = await run_coroutine_until_complete(
                self._recover_or_request_claimed_failure(
                    claimed,
                    target=target,
                    reason="Tool approval continuation was interrupted and denied safely.",
                ),
            )
            if owns_final:
                return event_id
            raise
        except Exception as error:
            reason = str(error) or "Tool approval continuation failed safely."
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
        )

    async def handoff_approval_source(self, source_event_id: str) -> bool | None:
        """Transfer one durable continuation out of the journal lane and into response ownership."""
        continuation = await self.deps.approval_store.approval_continuation_for_source(source_event_id)
        if continuation is None:
            return None

        resume = self._resume_approval_source(source_event_id)
        try:
            self.track_inbox_response(
                resume,
                name=f"approval_resume:{continuation.approval_id}:{continuation.generation}",
                recovery_proof_ready=lambda: True,
                source_event_ids=continuation.source_event_ids,
            )
        except BaseException:
            resume.close()
            raise
        return False

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
            if delivery is None or delivery.acknowledged_event_id is None:
                return False
            if await self._approval_responses.successful_final_delivery(continuation) is None:
                return await self.deps.approval_store.finish_approval_continuation(continuation.approval_id)
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
            if await self._approval_responses.successful_final_delivery(owned, recover=True) is not None:
                owns_final, event_id = await self._recover_frozen_approval_final(owned, target=target)
                return True, event_id if owns_final else None
            reason = owned.failure_reason or "Tool approval continuation was interrupted and denied safely."
            settled = await self._approval_responses.settle_failure(owned, reason)
            return True, owned.response_event_id if settled else None
        return False, None

    async def _finalize_early_placeholder_cancellation(
        self,
        state: _EarlyPlaceholderState,
        error: asyncio.CancelledError,
        *,
        response_kind: str,
    ) -> None:
        """Best-effort terminalize an early placeholder before attempt settlement starts."""
        request = state.request
        event_id = state.placeholder_event_id
        assert request is not None
        assert event_id is not None
        try:
            await self.deps.delivery_gateway.deliver_cancelled_visible_note(
                CancelledVisibleNoteRequest(
                    target=request.response_envelope.target,
                    event_id=event_id,
                    existing_event_is_placeholder=True,
                    cancel_source=classify_cancel_source(error),
                    identity=self._response_identity(request, response_kind=response_kind),
                ),
            )
        except Exception:
            self.deps.logger.exception(
                "Failed to finalize early placeholder after cancellation",
                event_id=event_id,
                response_kind=response_kind,
            )

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
        session_type: SessionType,
        create_storage: Callable[[], BaseDb],
    ) -> Callable[[str, str], None]:
        """Build the response-event persistence callback for one session-backed response."""

        def persist_response_event_id(run_id: str, response_event_id: str) -> None:
            storage = create_storage()
            try:
                self.deps.state_writer.persist_response_event_id_in_session_run(
                    storage=storage,
                    session_id=session_id,
                    session_type=session_type,
                    run_id=run_id,
                    response_event_id=response_event_id,
                )
            finally:
                storage.close()

        return persist_response_event_id

    def _request_for_delivery(
        self,
        request: ResponseRequest,
        *,
        message_id: str | None,
    ) -> ResponseRequest:
        """Attach the current visible event id to one delivery request."""
        if message_id is None:
            return request
        if request.existing_event_id is None:
            return replace(request, existing_event_id=message_id, existing_event_is_placeholder=True)
        return replace(request, existing_event_id=message_id)

    def _build_compaction_lifecycle(
        self,
        *,
        target: MessageTarget,
        request: ResponseRequest,
    ) -> MatrixCompactionLifecycle:
        """Build the ordered foreground compaction notice adapter for one response."""
        reply_to_event_id = (
            request.existing_event_id
            if request.existing_event_id is not None and request.existing_event_is_placeholder
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

        Read on a thread. This runs before a placeholder is sent, so every
        turn pays for it, and the statement underneath is a row out of a
        session database large enough to have been measured in seconds.
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
        storage = None
        try:
            storage = self.deps.state_writer.create_storage(execution_identity, scope=scope)
            session = storage.get_session(session_id, self.deps.state_writer.session_type_for_scope(scope))
            if not isinstance(session, AgentSession | TeamSession):
                return False
            state = read_scope_state(session, scope)
            return state.force_compact_before_next_run or has_pending_force_compaction_scope(session, scope)
        except Exception as error:
            self.deps.logger.warning(
                "forced_compaction_placeholder_check_failed",
                session_id=session_id,
                scope=scope.key,
                exception_type=error.__class__.__name__,
            )
            return False
        finally:
            if storage is not None:
                try:
                    storage.close()
                except Exception as error:
                    self.deps.logger.warning(
                        "forced_compaction_placeholder_storage_close_failed",
                        session_id=session_id,
                        scope=scope.key,
                        exception_type=error.__class__.__name__,
                    )

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
            return request
        if exclude_event_id is not None:
            filtered_history = [message for message in refreshed_history if message.event_id != exclude_event_id]
            if len(filtered_history) != len(refreshed_history):
                refreshed_history = replace(refreshed_history, messages=filtered_history)
        return replace(
            request,
            thread_history=refreshed_history,
            requires_model_history_refresh=False,
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
            if request.pipeline_timing is not None:
                request.pipeline_timing.mark("thread_refresh_ready")
            if request.payload_preparation is None:
                return request
            return await self.deps.request_preparer.prepare(request)
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

    def _correlation_id_for_request(self, request: ResponseRequest) -> str:
        """Resolve the correlation id for one request."""
        return request.correlation_id or request.reply_to_event_id or request.response_envelope.source_event_id

    def _response_identity(self, request: ResponseRequest, *, response_kind: str) -> ResponseIdentity:
        """Build the per-turn identity carried by delivery requests and response hooks."""
        return ResponseIdentity(
            response_kind=response_kind,
            response_envelope=request.response_envelope,
            correlation_id=self._correlation_id_for_request(request),
        )

    def _agent_turn_context(
        self,
        request: ResponseRequest,
        *,
        runtime: _PreparedResponseRuntime,
        run_id: str | None,
        active_event_ids: set[str],
        transient_enrichment_items: Sequence[EnrichmentItem],
        system_enrichment_items: Sequence[EnrichmentItem],
    ) -> ResponseTurnContext:
        """Build the per-turn identity context for one agent response."""
        matrix_target_item = _matrix_message_target_item(
            runtime.resolved_target,
            matrix_message_available=_agent_has_matrix_messaging_tool(
                self.deps.runtime.config,
                self.deps.agent_name,
                runtime.session_id,
            ),
            runtime=self.deps.runtime,
        )
        return ResponseTurnContext(
            entity_label=self.deps.agent_name,
            session_id=runtime.session_id,
            run_id=run_id,
            correlation_id=self._correlation_id_for_request(request),
            reply_to_event_id=request.reply_to_event_id,
            room_id=request.room_id,
            thread_id=runtime.resolved_target.resolved_thread_id,
            requester_id=request.user_id,
            matrix_run_metadata=_materialize_matrix_run_metadata(request.matrix_run_metadata),
            active_event_ids=frozenset(active_event_ids),
            transient_enrichment_items=_with_matrix_message_target(
                transient_enrichment_items,
                matrix_target_item,
            ),
            system_enrichment_items=tuple(system_enrichment_items),
            scheduled_history_budget=request.scheduled_history_budget,
        )

    def _notify_interrupted_response_recoverable(
        self,
        request: ResponseRequest,
        final_outcome: FinalDeliveryOutcome,
    ) -> bool:
        """Tell the dispatcher when a marked-handled interrupted turn is recoverable.

        Only turns whose terminal interruption update reached Matrix are
        reported: restart cleanup can discover that note, while the handled-turn
        ledger prevents source replay from answering it twice. Explicit user
        stops are terminal user intent and must never schedule recovery.
        """
        if request.on_interrupted_response_recoverable is None or final_outcome.terminal_status != "cancelled":
            return False
        if (
            not final_outcome.mark_handled
            or final_outcome.delivery_kind is None
            or request.response_envelope.target.resolved_thread_id is None
        ):
            return False
        cancel_source = final_outcome.resolved_cancel_source
        if cancel_source == "user_stop":
            return False
        expected_note = (
            RESTART_INTERRUPTED_RESPONSE_NOTE if cancel_source == "sync_restart" else INTERRUPTED_RESPONSE_NOTE
        )
        if final_outcome.final_visible_body is None or not final_outcome.final_visible_body.rstrip().endswith(
            expected_note,
        ):
            return False
        request.on_interrupted_response_recoverable()
        return True

    async def _record_user_stop_handled(
        self,
        request: ResponseRequest,
        final_outcome: FinalDeliveryOutcome,
        *,
        cancel_source: str | None,
        source_handled: bool,
    ) -> None:
        """Make explicit user-stop settlement durable before releasing the response lock."""
        on_user_stop_handled = request.on_user_stop_handled
        if not source_handled or cancel_source != "user_stop" or on_user_stop_handled is None:
            return
        response_event_id = final_outcome.final_visible_event_id
        assert response_event_id is not None
        stop_receipt_orders = self._user_stop_receipt_orders.get(response_event_id)
        if not stop_receipt_orders:
            return
        stop_receipt_order = max(stop_receipt_orders)
        await on_user_stop_handled(response_event_id, stop_receipt_order)

    async def _begin_locked_turn(
        self,
        request: ResponseRequest,
        *,
        resolved_target: MessageTarget,
        history_scope: HistoryScope,
        execution_identity: ToolExecutionIdentity,
        placeholder_message: str | None = None,
        early_placeholder_state: _EarlyPlaceholderState | None = None,
    ) -> ResponseRequest | None:
        """Expose a locked turn before running its potentially slow preparation."""
        placeholder_state = early_placeholder_state or _EarlyPlaceholderState()
        if not self._sync_restart_retry_is_current(
            request,
            history_scope=history_scope,
            execution_identity=execution_identity,
        ):
            return None
        if request.on_lifecycle_lock_acquired is not None:
            request.on_lifecycle_lock_acquired()
        request = self._request_with_locked_target(request, resolved_target)
        if request.prepare_source_turn is not None and await run_coroutine_until_complete(
            request.prepare_source_turn(),
        ):
            self.deps.logger.info(
                "response_suppressed_for_terminal_source",
                source_event_id=request.response_envelope.source_event_id,
            )
            if request.existing_event_id is not None and request.existing_event_is_placeholder:
                await self.deps.delivery_gateway.deliver_cancelled_visible_note(
                    CancelledVisibleNoteRequest(
                        target=resolved_target,
                        event_id=request.existing_event_id,
                        existing_event_is_placeholder=True,
                        cancel_source="interrupted",
                        identity=self._response_identity(
                            request,
                            response_kind="team" if history_scope.kind == "team" else "agent",
                        ),
                    ),
                )
            if request.on_source_turn_suppressed is not None:
                await request.on_source_turn_suppressed()
            return None
        placeholder_event_id = None
        if (
            placeholder_message is not None
            and request.existing_event_id is None
            and not await self._has_queued_forced_compaction(
                session_id=resolved_target.session_id,
                scope=history_scope,
                execution_identity=execution_identity,
            )
        ):
            placeholder_event_id = await self.deps.delivery_gateway.send_text(
                SendTextRequest(
                    target=resolved_target,
                    response_text=placeholder_message,
                    extra_content={STREAM_STATUS_KEY: STREAM_STATUS_PENDING},
                    # A streamed answer creates its visible message here and
                    # only edits it afterwards, so this send is the one a crash
                    # could duplicate into two answers in the room.
                    delivery_turn_id=request.response_envelope.source_event_id,
                    delivery_stage=DeliveryStage.INITIAL,
                ),
            )
            if placeholder_event_id is not None:
                placeholder_state.placeholder_event_id = placeholder_event_id
                placeholder_state.request = request
                request = replace(
                    request,
                    existing_event_id=placeholder_event_id,
                    existing_event_is_placeholder=True,
                )
                if request.pipeline_timing is not None:
                    request.pipeline_timing.mark("placeholder_sent")
                    request.pipeline_timing.mark_first_visible_reply("placeholder")
                if request.on_visible_response is not None:
                    await request.on_visible_response(placeholder_event_id)
        request = await self._prepare_request_after_lock(
            request,
            exclude_history_event_id=placeholder_event_id,
        )
        return self._request_with_locked_target(request, resolved_target)

    def _sync_restart_retry_is_current(
        self,
        request: ResponseRequest,
        *,
        history_scope: HistoryScope,
        execution_identity: ToolExecutionIdentity,
    ) -> bool:
        """Fail closed unless persisted history still ends in this retry's interrupted source."""
        source_event_id = request.sync_restart_retry_source_event_id
        if source_event_id is None:
            return True

        try:
            storage = self.deps.state_writer.create_storage(execution_identity, scope=history_scope)
            try:
                session = storage.get_session(
                    request.response_envelope.target.session_id,
                    self.deps.state_writer.session_type_for_scope(history_scope),
                )
                should_retry = isinstance(session, AgentSession | TeamSession) and interrupted_source_needs_retry(
                    session.runs or (),
                    scope=history_scope,
                    source_event_id=source_event_id,
                )
            finally:
                storage.close()
        except Exception as error:
            self.deps.logger.warning(
                "sync_restart_retry_history_check_failed",
                source_event_id=source_event_id,
                scope=history_scope.key,
                exception_type=error.__class__.__name__,
            )
            return False
        if not should_retry:
            self.deps.logger.info("sync_restart_retry_skipped", source_event_id=source_event_id)
        return should_retry

    async def _finalize_pre_delivery_terminal(
        self,
        *,
        target: MessageTarget,
        request: ResponseRequest,
        identity: ResponseIdentity,
        progress: _DeliveryProgress,
        terminal_status: TerminalFailureStatus,
        failure_reason: str,
    ) -> FinalDeliveryOutcome:
        """Finalize one turn that terminated before a delivery outcome settled.

        The real pending-visible shape decides what the gateway may touch: a
        non-placeholder existing event (for example a prior answer being
        regenerated) must never be treated as a redactable placeholder.
        """
        # Pre-delivery, a tracked event with no adopted existing event is a
        # message this turn created on its own, so classify it as the run
        # message for placeholder cleanup instead of leaving it dangling.
        placeholder_run_message_id = progress.tracked_event_id if request.existing_event_id is None else None
        pending = PendingVisibleResponse(
            tracked_event_id=progress.tracked_event_id,
            run_message_id=placeholder_run_message_id,
            existing_event_id=request.existing_event_id,
            existing_event_is_placeholder=request.existing_event_is_placeholder,
        )
        if pending.terminal_event_id is None:
            return self.deps.delivery_gateway.terminal_outcome_without_visible_event(
                terminal_status=terminal_status,
                failure_reason=failure_reason,
            )
        return await self.deps.delivery_gateway.finalize_streamed_response(
            FinalizeStreamedResponseRequest(
                target=target,
                stream_transport_outcome=build_terminal_stream_transport_outcome(
                    pending,
                    terminal_status=terminal_status,
                    failure_reason=failure_reason,
                    placeholder_body=PROGRESS_PLACEHOLDER,
                ),
                initial_delivery_kind="edited" if request.existing_event_id else "sent",
                identity=identity,
                tool_trace=None,
                extra_content=None,
                existing_event_id=request.existing_event_id,
                existing_event_is_placeholder=request.existing_event_is_placeholder,
            ),
        )

    async def _settle_missing_delivery_outcome(
        self,
        *,
        target: MessageTarget,
        request: ResponseRequest,
        identity: ResponseIdentity,
        progress: _DeliveryProgress,
        terminal_status: TerminalFailureStatus,
        failure_reason: str,
    ) -> None:
        """Settle a missing outcome without touching content after delivery starts."""
        if progress.delivery_outcome is not None:
            return
        if progress.stage_started:
            delivery_outcome = self.deps.delivery_gateway.terminal_outcome_without_visible_event(
                terminal_status=terminal_status,
                failure_reason=failure_reason,
            )
        else:
            delivery_outcome = await self._finalize_pre_delivery_terminal(
                target=target,
                request=request,
                identity=identity,
                progress=progress,
                terminal_status=terminal_status,
                failure_reason=failure_reason,
            )
        progress.settle(delivery_outcome)

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

    async def _run_and_settle_locked_response(  # noqa: C901, PLR0912
        self,
        request: ResponseRequest,
        *,
        target: MessageTarget,
        lifecycle: ResponseLifecycle,
        progress: _DeliveryProgress,
        response_function: Callable[[str | None], Coroutine[Any, Any, None]],
        user_id: str | None,
        run_id: str,
        build_post_response_outcome: Callable[[FinalDeliveryOutcome], ResponseOutcome],
        post_response_deps: PostResponseEffectsDeps | Callable[[], PostResponseEffectsDeps],
        streaming_delivery_error_handler: Callable[
            [StreamingDeliveryError],
            Awaitable[FinalDeliveryOutcome],
        ]
        | None = None,
        approval_suspension_handler: Callable[[PausedAttempt], Awaitable[FinalDeliveryOutcome]] | None = None,
    ) -> str | None:
        """Run generation and settle its terminal lifecycle exactly once."""
        deferred_error: BaseException | None = None
        try:
            # The attempt runs against the event the turn already adopted, which
            # `progress` was seeded with, so it has no new event to report back.
            await self._run_cancellable_response(
                target=target,
                response_function=response_function,
                existing_event_id=request.existing_event_id,
                user_id=user_id,
                run_id=run_id,
                on_cancelled=progress.note_task_cancelled,
            )
        except ResponsePausedForApproval as error:
            if approval_suspension_handler is None:
                raise
            try:
                progress.settle(await approval_suspension_handler(error.paused))
            except Exception as suspension_error:
                self.deps.logger.exception("approval_suspension_failed", error=str(suspension_error))
                progress.failure_reason = str(suspension_error) or "approval_suspension_failed"
                await self._settle_missing_delivery_outcome(
                    target=target,
                    request=request,
                    identity=lifecycle.identity,
                    progress=progress,
                    terminal_status="error",
                    failure_reason=progress.failure_reason,
                )
        except asyncio.CancelledError as error:
            progress.note_task_cancelled(cancel_failure_reason(classify_cancel_source(error)))
            await self._settle_missing_delivery_outcome(
                target=target,
                request=request,
                identity=lifecycle.identity,
                progress=progress,
                terminal_status="cancelled",
                failure_reason=progress.failure_reason or "interrupted",
            )
            deferred_error = error
        except Exception as error:
            if isinstance(error, StreamingDeliveryError) and streaming_delivery_error_handler is not None:
                progress.settle(await streaming_delivery_error_handler(error))
            elif progress.stage_started or progress.delivery_outcome is not None:
                # Do not touch a tracked event after delivery starts: an adopted
                # thinking-message stream can already hold the full reply.
                self._log_delivery_failure(response_kind=lifecycle.identity.response_kind, error=error)
                if progress.delivery_outcome is None:
                    progress.failure_reason = progress.failure_reason or str(error) or "late_delivery_failure"
            else:
                progress.failure_reason = str(error) or "delivery_failed_before_start"
                progress.settle(
                    await self._finalize_pre_delivery_terminal(
                        target=target,
                        request=request,
                        identity=lifecycle.identity,
                        progress=progress,
                        terminal_status="error",
                        failure_reason=progress.failure_reason,
                    ),
                )
                deferred_error = error

        if progress.delivery_outcome is None and (progress.cancelled or progress.failure_reason is not None):
            await self._settle_missing_delivery_outcome(
                target=target,
                request=request,
                identity=lifecycle.identity,
                progress=progress,
                terminal_status="cancelled" if progress.cancelled else "error",
                failure_reason=progress.failure_reason or "interrupted",
            )

        final_delivery_outcome = progress.delivery_outcome
        if final_delivery_outcome is None:
            msg = "Response generation did not settle a delivery outcome"
            raise RuntimeError(msg)
        final_outcome = await self._finalize_locked_outcome(
            lifecycle,
            final_delivery_outcome,
            post_response_outcome=build_post_response_outcome(final_delivery_outcome),
            post_response_deps=post_response_deps,
        )
        if final_outcome.terminal_status == "suspended" and request.source_handoff is not None:
            request.source_handoff.set()
        interruption_recovery_registered = self._notify_interrupted_response_recoverable(request, final_outcome)
        cancel_source = final_outcome.resolved_cancel_source
        source_handled = final_outcome.mark_handled and (
            request.on_deferred_outcome_handled is None
            or cancel_source is None
            or cancel_source == "user_stop"
            or interruption_recovery_registered
        )
        await self._record_user_stop_handled(
            request,
            final_outcome,
            cancel_source=cancel_source,
            source_handled=source_handled,
        )
        if deferred_error is not None:
            if source_handled and request.on_deferred_outcome_handled is not None:
                response_event_id = final_outcome.final_visible_event_id
                assert response_event_id is not None
                await request.on_deferred_outcome_handled(response_event_id)
            raise deferred_error
        return final_outcome.final_visible_event_id if source_handled else None

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
        )
        if prepared_request is None:
            return None
        request = prepared_request
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
        team_agents: list[MatrixID],
        team_mode: str,
        reason_prefix: str = "Team request",
        resolution_reason: str | None = None,
    ) -> str | None:
        """Generate a team response with lifecycle locking and queued-message state."""
        team_request = _TeamResponseRequest(
            request=request,
            team_agents=tuple(team_agents),
            team_mode=team_mode,
            reason_prefix=reason_prefix,
            resolution_reason=resolution_reason,
        )
        return await self._run_locked_response_lifecycle(
            request,
            response_kind="team",
            locked_operation=lambda resolved_target, early_placeholder_state: (
                self._generate_team_response_helper_locked(
                    team_request,
                    resolved_target=resolved_target,
                    early_placeholder_state=early_placeholder_state,
                )
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

    async def _generate_team_response_helper_locked(  # noqa: C901, PLR0915
        self,
        team_request: _TeamResponseRequest,
        *,
        resolved_target: MessageTarget,
        early_placeholder_state: _EarlyPlaceholderState | None = None,
    ) -> str | None:
        """Generate a team response once the per-thread lifecycle lock is held."""
        placeholder_state = early_placeholder_state or _EarlyPlaceholderState()
        request = team_request.request
        retry_execution_identity = self.deps.tool_runtime.build_execution_identity(
            target=resolved_target,
            user_id=request.user_id,
        )
        session_scope = self.deps.state_writer.team_history_scope(
            list(team_request.team_agents),
            requester_user_id=retry_execution_identity.requester_id,
        )
        if not request.prompt.strip():
            return await self._finalize_empty_prompt_locked(
                request,
                resolved_target=resolved_target,
                response_kind="team",
                history_scope=session_scope,
                execution_identity=retry_execution_identity,
            )
        prepared_request = await self._begin_locked_turn(
            request,
            resolved_target=resolved_target,
            history_scope=session_scope,
            execution_identity=retry_execution_identity,
            placeholder_message="🤝 Team Response: Thinking...",
            early_placeholder_state=placeholder_state,
        )
        if prepared_request is None:
            return None
        request = prepared_request
        team_request = replace(team_request, request=request)
        reason = team_request.resolution_reason
        if reason is not None:
            response_identity = self._response_identity(request, response_kind="team")
            lifecycle = self._build_lifecycle(identity=response_identity, request=request)
            progress = _DeliveryProgress(tracked_event_id=request.existing_event_id)

            async def deliver_resolution_reason(message_id: str | None) -> None:
                progress.settle(
                    await self.deps.delivery_gateway.deliver_final(
                        FinalDeliveryRequest(
                            target=resolved_target,
                            existing_event_id=message_id,
                            existing_event_is_placeholder=request.existing_event_is_placeholder,
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
                run_id=str(uuid4()),
                build_post_response_outcome=lambda _final_outcome: ResponseOutcome(),
                post_response_deps=lambda: self._post_response_deps(request),
            )
        requester_user_id = request.user_id or ""
        _memory_prompt, _memory_thread_history, prepared_prompt, model_thread_history = (
            prepare_memory_and_model_context(
                request.prompt,
                request.thread_history,
                config=self.deps.runtime.config,
                runtime_paths=self.deps.runtime_paths,
                model_prompt=request.model_prompt,
            )
        )
        model_name = select_model_for_team(
            self.deps.agent_name,
            request.room_id,
            self.deps.runtime.config,
            self.deps.runtime_paths,
            thread_id=resolved_target.resolved_thread_id,
        )
        use_streaming = await should_use_streaming(
            self._client(),
            request.room_id,
            requester_user_id=requester_user_id,
            enable_streaming=self.deps.runtime.enable_streaming,
        )
        self._note_pipeline_metadata(request, response_kind="team", used_streaming=use_streaming)
        show_tool_calls = self._show_tool_calls()
        mode = TeamMode.COORDINATE if team_request.team_mode == "coordinate" else TeamMode.COLLABORATE
        registry = entity_identity_registry(self.deps.runtime.config, self.deps.runtime_paths)
        agent_names = [
            registry.current_entity_name_for_user_id(mid.full_id) or mid.username for mid in team_request.team_agents
        ]
        matrix_target_item = _matrix_message_target_item(
            resolved_target,
            matrix_message_available=any(
                _agent_has_matrix_messaging_tool(self.deps.runtime.config, name, resolved_target.session_id)
                for name in agent_names
            ),
            runtime=self.deps.runtime,
        )
        resolved_request = self._request_with_locked_target(
            replace(
                request,
                thread_history=model_thread_history,
                media=request.media or MediaInputs(),
            ),
            resolved_target,
        )
        response_identity = self._response_identity(resolved_request, response_kind="team")
        lifecycle = self._build_lifecycle(
            identity=response_identity,
            request=resolved_request,
        )
        delivery_target = (
            resolved_target
            if request.existing_event_id is None or request.existing_event_is_placeholder
            else resolved_target.with_thread_root(request.thread_id)
        )
        delivery_request_base = resolved_request
        session_id = resolved_target.session_id
        tool_dispatch = self.deps.tool_runtime.build_dispatch_context(
            resolved_target,
            user_id=requester_user_id,
            active_model_name=model_name,
            attachment_ids=request.attachment_ids,
            correlation_id=response_identity.correlation_id,
            source_envelope=request.response_envelope,
        )
        execution_identity = tool_dispatch.execution_identity
        allow_direct_private_agents = (
            self.deps.agent_name not in self.deps.runtime.config.teams
            and execution_identity.channel == "matrix"
            and bool(execution_identity.requester_id)
        )
        self.deps.runtime.config.assert_team_agents_supported(
            [agent_name for agent_name in agent_names if agent_name != ROUTER_AGENT_NAME],
            allow_direct_private_agents=allow_direct_private_agents,
        )
        session_type = self.deps.state_writer.session_type_for_scope(session_scope)

        def team_storage_factory() -> BaseDb:
            return self.deps.state_writer.create_storage(execution_identity, scope=session_scope)

        session_started_watch = lifecycle.setup_session_watch(
            tool_context=runtime_context_from_dispatch_context(tool_dispatch),
            session_id=session_id,
            session_type=session_type,
            scope=session_scope,
            room_id=request.room_id,
            thread_id=resolved_target.resolved_thread_id,
            create_storage=team_storage_factory,
        )
        orchestrator = self.deps.runtime.orchestrator
        if orchestrator is None:
            msg = "Orchestrator is not set"
            raise RuntimeError(msg)
        response_run_id = str(uuid4())
        team_run_metadata_content: dict[str, Any] = {}
        progress = _DeliveryProgress(tracked_event_id=request.existing_event_id)
        matrix_run_metadata = _materialize_matrix_run_metadata(request.matrix_run_metadata)
        active_event_ids = self._active_response_event_ids(request.room_id)
        # Team entries refine entity_label to the materialized team label and
        # append the knowledge-availability enrichment before the turn runs.
        team_turn_ctx = ResponseTurnContext(
            entity_label=self.deps.agent_name,
            session_id=session_id,
            run_id=response_run_id,
            correlation_id=response_identity.correlation_id,
            reply_to_event_id=request.reply_to_event_id,
            room_id=request.room_id,
            thread_id=resolved_target.resolved_thread_id,
            requester_id=requester_user_id or execution_identity.requester_id,
            matrix_run_metadata=matrix_run_metadata,
            active_event_ids=frozenset(active_event_ids),
            transient_enrichment_items=_with_matrix_message_target(
                request.transient_enrichment_items,
                matrix_target_item,
            ),
            system_enrichment_items=request.system_enrichment_items,
            scheduled_history_budget=request.scheduled_history_budget,
        )
        team_turn_recorder = self._build_turn_recorder(
            user_message=prepared_prompt,
            user_message_is_structured=request.current_prompt_is_structured,
            reply_to_event_id=request.reply_to_event_id,
            requester_id=requester_user_id or execution_identity.requester_id,
            matrix_run_metadata=matrix_run_metadata,
        )

        async def persist_failed_team_turn() -> None:
            await self._persist_failed_turn(
                team_turn_recorder,
                is_team=True,
                session_scope=session_scope,
                session_id=session_id,
                execution_identity=tool_dispatch.execution_identity,
                run_id=response_run_id,
                response_event_id=progress.tracked_event_id,
            )

        persist_response_event_id = self._build_persist_response_event_id_effect(
            session_id=session_id,
            session_type=session_type,
            create_storage=team_storage_factory,
        )

        async def generate_team_response(message_id: str | None) -> None:  # noqa: C901, PLR0915
            delivery_request = self._request_for_delivery(delivery_request_base, message_id=message_id)
            if message_id is not None:
                progress.track_event(message_id)
                team_turn_recorder.set_response_event_id(message_id)
            compaction_lifecycle = self._build_compaction_lifecycle(
                target=delivery_target,
                request=delivery_request,
            )

            def _note_attempt_run_id(current_run_id: str) -> None:
                self.deps.stop_manager.update_run_id(message_id, current_run_id)
                team_turn_recorder.set_run_id(current_run_id)

            def _note_visible_response_event_id(response_event_id: str) -> None:
                progress.track_event(response_event_id)
                team_turn_recorder.set_response_event_id(response_event_id)

            if use_streaming and (
                delivery_request.existing_event_id is None or delivery_request.existing_event_is_placeholder
            ):
                async with typing_indicator(self._client(), request.room_id):
                    event_id: str | None = None

                    def build_response_stream() -> AsyncIterator[StreamInputChunk]:
                        return team_response_stream(
                            agent_ids=list(team_request.team_agents),
                            message=prepared_prompt,
                            orchestrator=orchestrator,
                            execution_identity=tool_dispatch.execution_identity,
                            ctx=team_turn_ctx,
                            mode=mode,
                            thread_history=model_thread_history,
                            model_name=model_name,
                            media=resolved_request.media,
                            show_tool_calls=show_tool_calls,
                            run_id_callback=_note_attempt_run_id,
                            user_id=requester_user_id,
                            current_timestamp_ms=request.current_timestamp_ms,
                            current_prompt_is_structured=request.current_prompt_is_structured,
                            response_sender_id=self.deps.matrix_full_id,
                            run_metadata_collector=team_run_metadata_content,
                            compaction_lifecycle=compaction_lifecycle,
                            configured_team_name=self.deps.agent_name
                            if self.deps.agent_name in self.deps.runtime.config.teams
                            else None,
                            reason_prefix=team_request.reason_prefix,
                            pipeline_timing=request.pipeline_timing,
                            turn_recorder=team_turn_recorder,
                        )

                    response_stream = self._stream_in_tool_context(
                        tool_dispatch=tool_dispatch,
                        stream_factory=build_response_stream,
                    )

                    try:
                        progress.note_delivery_started(None)
                        transport_outcome = await self.deps.delivery_gateway.deliver_stream(
                            StreamingDeliveryRequest(
                                target=delivery_target,
                                identity=response_identity,
                                response_stream=response_stream,
                                existing_event_id=delivery_request.existing_event_id,
                                adopt_existing_placeholder=bool(delivery_request.existing_event_id)
                                and delivery_request.existing_event_is_placeholder,
                                header=None,
                                show_tool_calls=show_tool_calls,
                                # The live collector dict: the turn driver fills it
                                # at terminal settle, before the stream's final
                                # edit snapshots extra_content, so the ai_run
                                # metadata lands on the wire (mirrors the agent
                                # streaming path).
                                extra_content=_merge_response_extra_content(
                                    team_run_metadata_content,
                                    request.attachment_ids,
                                ),
                                streaming_cls=ReplacementStreamingResponse,
                                pipeline_timing=request.pipeline_timing,
                                visible_event_id_callback=_note_visible_response_event_id,
                            ),
                        )
                        event_id = transport_outcome.last_physical_stream_event_id
                        progress.track_event(event_id)
                    except asyncio.CancelledError:
                        await self._persist_interrupted_recorder_off_loop(
                            recorder=team_turn_recorder,
                            session_scope=session_scope,
                            session_id=session_id,
                            execution_identity=tool_dispatch.execution_identity,
                            run_id=response_run_id,
                            is_team=True,
                            response_event_id=progress.tracked_event_id,
                        )
                        raise
                    finally:
                        await lifecycle.emit_session_started(session_started_watch)
                if request.pipeline_timing is not None:
                    request.pipeline_timing.mark("streaming_complete")
                await persist_failed_team_turn()
                delivery = await self._finalize_streamed_turn(
                    request=request,
                    delivery_target=delivery_target,
                    transport_outcome=transport_outcome,
                    delivery_kind="edited" if message_id else "sent",
                    response_identity=response_identity,
                    tool_trace=None,
                    extra_content=_merge_response_extra_content(
                        team_run_metadata_content
                        or ai_run_extra_content_from_metadata(team_turn_recorder.run_metadata),
                        request.attachment_ids,
                    ),
                )
                progress.settle(delivery)
            else:
                try:
                    try:
                        async with typing_indicator(self._client(), request.room_id):

                            async def build_response_text() -> str:
                                return await team_response(
                                    agent_names=agent_names,
                                    mode=mode,
                                    message=prepared_prompt,
                                    orchestrator=orchestrator,
                                    execution_identity=tool_dispatch.execution_identity,
                                    ctx=team_turn_ctx,
                                    thread_history=model_thread_history,
                                    model_name=model_name,
                                    media=resolved_request.media,
                                    run_id_callback=_note_attempt_run_id,
                                    user_id=requester_user_id,
                                    current_timestamp_ms=request.current_timestamp_ms,
                                    current_prompt_is_structured=request.current_prompt_is_structured,
                                    response_sender_id=self.deps.matrix_full_id,
                                    run_metadata_collector=team_run_metadata_content,
                                    compaction_lifecycle=compaction_lifecycle,
                                    configured_team_name=self.deps.agent_name
                                    if self.deps.agent_name in self.deps.runtime.config.teams
                                    else None,
                                    reason_prefix=team_request.reason_prefix,
                                    pipeline_timing=request.pipeline_timing,
                                    turn_recorder=team_turn_recorder,
                                )

                            try:
                                response_text = await self._run_in_tool_context(
                                    tool_dispatch=tool_dispatch,
                                    operation=build_response_text,
                                )
                            except asyncio.CancelledError:
                                await self._persist_interrupted_recorder_off_loop(
                                    recorder=team_turn_recorder,
                                    session_scope=session_scope,
                                    session_id=session_id,
                                    execution_identity=tool_dispatch.execution_identity,
                                    run_id=response_run_id,
                                    is_team=True,
                                    response_event_id=progress.tracked_event_id,
                                )
                                raise
                    finally:
                        await lifecycle.emit_session_started(session_started_watch)
                        await persist_failed_team_turn()
                except asyncio.CancelledError as exc:
                    progress.settle(
                        await self._settle_blocking_cancellation(
                            exc,
                            message_id=message_id,
                            delivery_target=delivery_target,
                            existing_event_is_placeholder=delivery_request.existing_event_is_placeholder,
                            response_identity=response_identity,
                            restart_message="Team non-streaming response interrupted by sync restart",
                            user_stop_message="Team non-streaming response cancelled by user",
                            interrupted_message="Team non-streaming response interrupted — traceback for diagnosis",
                        ),
                    )
                    return

                progress.note_delivery_started(None)
                try:
                    delivery = await self.deps.delivery_gateway.deliver_final(
                        FinalDeliveryRequest(
                            target=delivery_target,
                            existing_event_id=message_id,
                            existing_event_is_placeholder=delivery_request.existing_event_is_placeholder,
                            response_text=response_text,
                            identity=response_identity,
                            tool_trace=None,
                            extra_content=_merge_response_extra_content(
                                team_run_metadata_content
                                or ai_run_extra_content_from_metadata(team_turn_recorder.run_metadata),
                                request.attachment_ids,
                            ),
                        ),
                    )
                    progress.settle(delivery)
                except asyncio.CancelledError:
                    await self._persist_interrupted_recorder_off_loop(
                        recorder=team_turn_recorder,
                        session_scope=session_scope,
                        session_id=session_id,
                        execution_identity=tool_dispatch.execution_identity,
                        run_id=response_run_id,
                        is_team=True,
                        response_event_id=progress.tracked_event_id,
                    )
                    raise
                self._note_final_delivery_timing(request, delivery)

        async def settle_team_streaming_delivery_error(error: StreamingDeliveryError) -> FinalDeliveryOutcome:
            transport_outcome = error.transport_outcome
            if transport_outcome.terminal_status == "cancelled":
                log_cancelled_response_source(
                    self.deps.logger,
                    cancel_source=transport_outcome.resolved_cancel_source or "interrupted",
                    message_id=error.event_id,
                    restart_message="Team streaming response interrupted by sync restart",
                    user_stop_message="Team streaming response cancelled by user",
                    interrupted_message="Team streaming response interrupted — traceback for diagnosis",
                    exc_info=(type(error.error), error.error, error.error.__traceback__),
                )
            else:
                self.deps.logger.exception("Error in team streaming response", error=str(error.error))
            progress.track_event(error.event_id)
            if self._record_stream_delivery_error(
                recorder=team_turn_recorder,
                accumulated_text=error.accumulated_text,
                tool_trace=error.tool_trace,
            ):
                await self._persist_interrupted_recorder_off_loop(
                    recorder=team_turn_recorder,
                    session_scope=session_scope,
                    session_id=session_id,
                    execution_identity=tool_dispatch.execution_identity,
                    run_id=response_run_id,
                    is_team=True,
                    response_event_id=progress.tracked_event_id,
                )
            return await self.deps.delivery_gateway.finalize_streamed_response(
                FinalizeStreamedResponseRequest(
                    target=delivery_target,
                    stream_transport_outcome=transport_outcome,
                    initial_delivery_kind="edited" if request.existing_event_id else "sent",
                    identity=response_identity,
                    tool_trace=error.tool_trace if show_tool_calls else None,
                    extra_content=_merge_response_extra_content(
                        team_run_metadata_content
                        or ai_run_extra_content_from_metadata(team_turn_recorder.run_metadata),
                        request.attachment_ids,
                    ),
                    existing_event_id=request.existing_event_id,
                    existing_event_is_placeholder=request.existing_event_is_placeholder,
                ),
            )

        def build_team_post_response_outcome(_delivery_outcome: FinalDeliveryOutcome) -> ResponseOutcome:
            return ResponseOutcome(
                response_run_id=team_turn_recorder.run_id or response_run_id,
                session_id=session_id,
                session_type=SessionType.TEAM,
                execution_identity=tool_dispatch.execution_identity,
                run_succeeded=team_turn_recorder.outcome == "completed",
                response_target=resolved_target,
                thread_summary_room_id=(request.room_id if resolved_target.resolved_thread_id is not None else None),
                thread_summary_thread_id=resolved_target.resolved_thread_id,
                thread_summary_message_count_hint=thread_summary_message_count_hint(
                    request.thread_history,
                    trusted_sender_ids=current_internal_sender_ids(
                        self.deps.runtime.config,
                        self.deps.runtime_paths,
                    ),
                ),
                thread_summary_entity_name=self.deps.agent_name,
                memory_prompt=_memory_prompt,
                memory_thread_history=_memory_thread_history,
            )

        placeholder_state.settlement_started = True
        return await self._run_and_settle_locked_response(
            request,
            target=delivery_target,
            lifecycle=lifecycle,
            progress=progress,
            response_function=generate_team_response,
            user_id=requester_user_id,
            run_id=response_run_id,
            build_post_response_outcome=build_team_post_response_outcome,
            post_response_deps=lambda: self._post_response_deps(
                request,
                persist_response_event_id=persist_response_event_id,
            ),
            streaming_delivery_error_handler=settle_team_streaming_delivery_error,
            approval_suspension_handler=lambda paused: self._suspend_for_approval(
                paused,
                request=request,
                target=delivery_target,
                progress=progress,
                execution_identity=tool_dispatch.execution_identity,
                entity_kind="team",
                history_scope=session_scope,
                team_member_names=tuple(agent_names),
                team_mode=mode.value,
            ),
        )

    async def _run_cancellable_response(
        self,
        *,
        target: MessageTarget,
        response_function: Callable[[str | None], Coroutine[Any, Any, None]],
        existing_event_id: str | None = None,
        user_id: str | None = None,
        run_id: str | None = None,
        on_cancelled: Callable[[str], None] | None = None,
    ) -> _MatrixEventId | None:
        """Run one response-generation attempt with cancellation support."""
        return await ResponseAttemptRunner(
            ResponseAttemptDeps(
                client=self._client(),
                stop_manager=self.deps.stop_manager,
                logger=self.deps.logger,
                show_stop_button=lambda: self.deps.runtime.config.defaults.show_stop_button,
                config=self.deps.runtime.config,
            ),
        ).run(
            ResponseAttemptRequest(
                target=target,
                response_function=response_function,
                existing_event_id=existing_event_id,
                user_id=user_id,
                run_id=run_id,
                on_cancelled=on_cancelled,
            ),
        )

    @timed("prepare_response_runtime")
    async def prepare_response_runtime(
        self,
        request: ResponseRequest,
    ) -> _PreparedResponseRuntime:
        """Resolve shared runtime context for one streaming or non-streaming response."""
        resolved_target = request.response_envelope.target
        response_thread_id = (
            request.thread_id
            if request.existing_event_id and not request.existing_event_is_placeholder
            else request.response_envelope.target.resolved_thread_id
        )
        resolved_target = resolved_target.with_thread_root(response_thread_id)
        media_inputs = request.media or MediaInputs()
        session_id = resolved_target.session_id
        resolved_model_prompt = request.model_prompt or request.prompt
        runtime_model = self.deps.runtime.config.resolve_runtime_model(
            entity_name=self.deps.agent_name,
            room_id=resolved_target.room_id,
            thread_id=response_thread_id,
            runtime_paths=self.deps.runtime_paths,
        )
        tool_dispatch = self.deps.tool_runtime.build_dispatch_context(
            resolved_target,
            user_id=request.user_id,
            active_model_name=runtime_model.model_name,
            attachment_ids=request.attachment_ids,
            correlation_id=request.correlation_id,
            source_envelope=request.response_envelope,
        )
        return _PreparedResponseRuntime(
            resolved_target=resolved_target,
            response_thread_id=response_thread_id,
            media_inputs=media_inputs,
            session_id=session_id,
            model_prompt=resolved_model_prompt,
            tool_dispatch=tool_dispatch,
        )

    @timed("non_streaming_response_generation")
    async def generate_non_streaming_ai_response(
        self,
        request: ResponseRequest,
        *,
        run_id: str | None,
        runtime: _PreparedResponseRuntime,
        active_event_ids: set[str],
        turn_recorder: TurnRecorder,
        attempt_run_id_collector: list[str],
        pipeline_timing: DispatchPipelineTiming | None = None,
    ) -> _NonStreamingGeneration:
        """Run one non-streaming AI request and return its artifacts by value."""
        compaction_lifecycle = self._build_compaction_lifecycle(
            target=runtime.resolved_target,
            request=request,
        )
        tool_trace: list[ToolTraceEntry] = []
        run_metadata_content: dict[str, Any] = {}

        def note_attempt_run_id(current_run_id: str) -> None:
            self.deps.stop_manager.update_run_id(request.existing_event_id, current_run_id)
            turn_recorder.set_run_id(current_run_id)
            attempt_run_id_collector.append(current_run_id)

        show_tool_calls = self._show_tool_calls()

        async def build_response_text() -> str:
            knowledge_resolution = self.deps.knowledge_access.resolve_for_agent(
                self.deps.agent_name,
                execution_identity=runtime.tool_dispatch.execution_identity,
            )
            transient_enrichment_items = append_knowledge_availability_enrichment(
                request.transient_enrichment_items,
                knowledge_resolution.unavailable,
            )
            return await ai_response(
                self._agent_turn_context(
                    request,
                    runtime=runtime,
                    run_id=run_id,
                    active_event_ids=active_event_ids,
                    transient_enrichment_items=transient_enrichment_items,
                    system_enrichment_items=request.system_enrichment_items,
                ),
                prompt=request.prompt,
                runtime_paths=self.deps.runtime_paths,
                config=self.deps.runtime.config,
                thread_history=request.thread_history,
                model_prompt=runtime.model_prompt,
                current_timestamp_ms=request.current_timestamp_ms,
                current_prompt_is_structured=request.current_prompt_is_structured,
                knowledge=knowledge_resolution.knowledge,
                run_id_callback=note_attempt_run_id,
                media=runtime.media_inputs,
                show_tool_calls=show_tool_calls,
                collect_streamed_response=show_tool_calls,
                tool_trace_collector=tool_trace,
                run_metadata_collector=run_metadata_content,
                execution_identity=runtime.tool_dispatch.execution_identity,
                compaction_lifecycle=compaction_lifecycle,
                refresh_scheduler=self._knowledge_refresh_scheduler(),
                turn_recorder=turn_recorder,
                pipeline_timing=pipeline_timing,
                supports_native_tool_approval=True,
            )

        try:
            async with typing_indicator(self._client(), request.room_id):
                response_text = await self._run_in_tool_context(
                    tool_dispatch=runtime.tool_dispatch,
                    operation=build_response_text,
                )
                return _NonStreamingGeneration(
                    response_text=response_text,
                    tool_trace=tool_trace,
                    run_metadata_content=run_metadata_content,
                )
        except asyncio.CancelledError:
            await self._persist_interrupted_recorder_off_loop(
                recorder=turn_recorder,
                session_scope=self.deps.state_writer.history_scope(),
                session_id=runtime.session_id,
                execution_identity=runtime.tool_dispatch.execution_identity,
                run_id=run_id,
                is_team=False,
                response_event_id=request.existing_event_id,
            )
            raise

    @timed("streaming_response_generation")
    async def generate_streaming_ai_response(
        self,
        request: ResponseRequest,
        *,
        run_id: str | None,
        runtime: _PreparedResponseRuntime,
        active_event_ids: set[str],
        turn_recorder: TurnRecorder,
        tool_trace: list[Any],
        run_metadata_content: dict[str, Any],
        attempt_run_id_collector: list[str],
        identity: ResponseIdentity,
        pipeline_timing: DispatchPipelineTiming | None = None,
        visible_event_id_callback: Callable[[str], None] | None = None,
    ) -> StreamTransportOutcome:
        """Run one streaming AI request and send the streamed Matrix response."""
        compaction_lifecycle = self._build_compaction_lifecycle(
            target=runtime.resolved_target,
            request=request,
        )

        def note_attempt_run_id(current_run_id: str) -> None:
            self.deps.stop_manager.update_run_id(request.existing_event_id, current_run_id)
            turn_recorder.set_run_id(current_run_id)
            attempt_run_id_collector.append(current_run_id)

        def note_visible_response_event_id(response_event_id: str) -> None:
            turn_recorder.set_response_event_id(response_event_id)
            if visible_event_id_callback is not None:
                visible_event_id_callback(response_event_id)

        knowledge_resolution = self.deps.knowledge_access.resolve_for_agent(
            self.deps.agent_name,
            execution_identity=runtime.tool_dispatch.execution_identity,
        )
        transient_enrichment_items = append_knowledge_availability_enrichment(
            request.transient_enrichment_items,
            knowledge_resolution.unavailable,
        )
        response_stream = stream_agent_response(
            self._agent_turn_context(
                request,
                runtime=runtime,
                run_id=run_id,
                active_event_ids=active_event_ids,
                transient_enrichment_items=transient_enrichment_items,
                system_enrichment_items=request.system_enrichment_items,
            ),
            prompt=request.prompt,
            runtime_paths=self.deps.runtime_paths,
            config=self.deps.runtime.config,
            thread_history=request.thread_history,
            model_prompt=runtime.model_prompt,
            current_timestamp_ms=request.current_timestamp_ms,
            current_prompt_is_structured=request.current_prompt_is_structured,
            knowledge=knowledge_resolution.knowledge,
            run_id_callback=note_attempt_run_id,
            media=runtime.media_inputs,
            show_tool_calls=self._show_tool_calls(),
            run_metadata_collector=run_metadata_content,
            execution_identity=runtime.tool_dispatch.execution_identity,
            compaction_lifecycle=compaction_lifecycle,
            refresh_scheduler=self._knowledge_refresh_scheduler(),
            turn_recorder=turn_recorder,
            pipeline_timing=pipeline_timing,
            supports_native_tool_approval=True,
        )

        try:
            async with typing_indicator(self._client(), request.room_id):
                wrapped_response_stream = self._stream_in_tool_context(
                    tool_dispatch=runtime.tool_dispatch,
                    stream_factory=lambda: response_stream,
                )
                response_extra_content = _merge_response_extra_content(
                    run_metadata_content,
                    request.attachment_ids,
                )
                transport_outcome = await self.deps.delivery_gateway.deliver_stream(
                    StreamingDeliveryRequest(
                        target=runtime.resolved_target,
                        identity=identity,
                        response_stream=wrapped_response_stream,
                        existing_event_id=request.existing_event_id,
                        adopt_existing_placeholder=bool(request.existing_event_id)
                        and request.existing_event_is_placeholder,
                        show_tool_calls=self._show_tool_calls(),
                        extra_content=response_extra_content,
                        tool_trace_collector=tool_trace,
                        streaming_cls=StreamingResponse,
                        pipeline_timing=request.pipeline_timing,
                        visible_event_id_callback=note_visible_response_event_id,
                    ),
                )
                if request.pipeline_timing is not None:
                    request.pipeline_timing.mark("streaming_complete")
                if turn_recorder.outcome == "interrupted":
                    await self._persist_interrupted_recorder_off_loop(
                        recorder=turn_recorder,
                        session_scope=self.deps.state_writer.history_scope(),
                        session_id=runtime.session_id,
                        execution_identity=runtime.tool_dispatch.execution_identity,
                        run_id=run_id,
                        is_team=False,
                        response_event_id=request.existing_event_id,
                    )
                return transport_outcome
        except asyncio.CancelledError:
            await self._persist_interrupted_recorder_off_loop(
                recorder=turn_recorder,
                session_scope=self.deps.state_writer.history_scope(),
                session_id=runtime.session_id,
                execution_identity=runtime.tool_dispatch.execution_identity,
                run_id=run_id,
                is_team=False,
                response_event_id=request.existing_event_id,
            )
            raise

    async def _process_and_respond(
        self,
        request: ResponseRequest,
        *,
        run_id: str | None = None,
        response_kind: str = "ai",
        on_delivery_started: Callable[[str | None], None] | None = None,
        attempt_run_id_collector: list[str] | None = None,
    ) -> _ResponseGenerationOutcome:
        """Process a message and send a response without streaming."""
        if request.pipeline_timing is not None:
            request.pipeline_timing.mark("response_runtime_start")
        runtime = await self.prepare_response_runtime(request)
        if request.pipeline_timing is not None:
            request.pipeline_timing.mark("response_runtime_ready")
        request = self._request_with_locked_target(request, runtime.resolved_target)
        response_identity = self._response_identity(request, response_kind=response_kind)
        lifecycle = self._build_lifecycle(
            identity=response_identity,
            request=request,
        )
        session_scope = self.deps.state_writer.history_scope()
        session_type = self.deps.state_writer.session_type_for_scope(session_scope)

        def history_storage_factory() -> BaseDb:
            return self.deps.state_writer.create_storage(runtime.tool_dispatch.execution_identity, scope=session_scope)

        session_started_watch = lifecycle.setup_session_watch(
            tool_context=runtime_context_from_dispatch_context(runtime.tool_dispatch),
            session_id=runtime.session_id,
            session_type=session_type,
            scope=session_scope,
            room_id=request.room_id,
            thread_id=runtime.resolved_target.resolved_thread_id,
            create_storage=history_storage_factory,
        )
        # The caller's list survives raising exit paths (cancellation, stream
        # re-raises), unlike the returned outcome.
        attempt_run_ids = attempt_run_id_collector if attempt_run_id_collector is not None else []
        active_event_ids = self._active_response_event_ids(request.room_id)
        turn_recorder = self._build_turn_recorder(
            user_message=runtime.model_prompt,
            user_message_is_structured=request.current_prompt_is_structured,
            reply_to_event_id=request.reply_to_event_id,
            requester_id=request.user_id,
            matrix_run_metadata=_materialize_matrix_run_metadata(request.matrix_run_metadata),
        )

        def build_outcome(delivery: FinalDeliveryOutcome) -> _ResponseGenerationOutcome:
            return _generation_outcome(delivery, turn_recorder)

        try:
            try:
                generation = await self.generate_non_streaming_ai_response(
                    request,
                    run_id=run_id,
                    runtime=runtime,
                    active_event_ids=active_event_ids,
                    turn_recorder=turn_recorder,
                    attempt_run_id_collector=attempt_run_ids,
                    pipeline_timing=request.pipeline_timing,
                )
            finally:
                await lifecycle.emit_session_started(session_started_watch)
                await self._persist_failed_turn(
                    turn_recorder,
                    is_team=False,
                    session_scope=session_scope,
                    session_id=runtime.session_id,
                    execution_identity=runtime.tool_dispatch.execution_identity,
                    run_id=run_id,
                    response_event_id=request.existing_event_id,
                )
        except asyncio.CancelledError as exc:
            return build_outcome(
                await self._settle_blocking_cancellation(
                    exc,
                    message_id=request.existing_event_id,
                    delivery_target=runtime.resolved_target,
                    existing_event_is_placeholder=request.existing_event_is_placeholder,
                    response_identity=response_identity,
                    restart_message="Non-streaming response interrupted by sync restart",
                    user_stop_message="Non-streaming response cancelled by user",
                    interrupted_message="Non-streaming response interrupted — traceback for diagnosis",
                ),
            )
        except Exception as error:
            self.deps.logger.exception("Error in non-streaming response", error=str(error))
            raise

        response_extra_content = _merge_response_extra_content(
            generation.run_metadata_content,
            request.attachment_ids,
        )
        if on_delivery_started is not None:
            on_delivery_started(request.existing_event_id)
        try:
            delivery = await self.deps.delivery_gateway.deliver_final(
                FinalDeliveryRequest(
                    target=runtime.resolved_target,
                    existing_event_id=request.existing_event_id,
                    existing_event_is_placeholder=request.existing_event_is_placeholder,
                    response_text=generation.response_text,
                    identity=response_identity,
                    tool_trace=generation.tool_trace if self._show_tool_calls() else None,
                    extra_content=response_extra_content or None,
                ),
            )
        except asyncio.CancelledError:
            await self._persist_interrupted_recorder_off_loop(
                recorder=turn_recorder,
                session_scope=session_scope,
                session_id=runtime.session_id,
                execution_identity=runtime.tool_dispatch.execution_identity,
                run_id=run_id,
                is_team=False,
                response_event_id=request.existing_event_id,
            )
            raise
        self._note_final_delivery_timing(request, delivery)
        return build_outcome(delivery)

    async def _process_and_respond_streaming(  # noqa: C901
        self,
        request: ResponseRequest,
        *,
        run_id: str | None = None,
        response_kind: str = "ai",
        on_delivery_started: Callable[[str | None], None] | None = None,
        attempt_run_id_collector: list[str] | None = None,
    ) -> _ResponseGenerationOutcome:
        """Process a message and send a streamed response."""
        if request.pipeline_timing is not None:
            request.pipeline_timing.mark("response_runtime_start")
        runtime = await self.prepare_response_runtime(request)
        if request.pipeline_timing is not None:
            request.pipeline_timing.mark("response_runtime_ready")
        request = self._request_with_locked_target(request, runtime.resolved_target)
        response_identity = self._response_identity(request, response_kind=response_kind)
        lifecycle = self._build_lifecycle(
            identity=response_identity,
            request=request,
        )
        session_scope = self.deps.state_writer.history_scope()
        session_type = self.deps.state_writer.session_type_for_scope(session_scope)

        def history_storage_factory() -> BaseDb:
            return self.deps.state_writer.create_storage(runtime.tool_dispatch.execution_identity, scope=session_scope)

        session_started_watch = lifecycle.setup_session_watch(
            tool_context=runtime_context_from_dispatch_context(runtime.tool_dispatch),
            session_id=runtime.session_id,
            session_type=session_type,
            scope=session_scope,
            room_id=request.room_id,
            thread_id=runtime.resolved_target.resolved_thread_id,
            create_storage=history_storage_factory,
        )
        # The streaming path keeps these caller-owned (unlike the non-streaming
        # path's returned _NonStreamingGeneration): the metadata dict must stay
        # live while the delivery gateway snapshots extra_content, and both
        # must survive the raising StreamingDeliveryError exit below.
        run_metadata_content: dict[str, Any] = {}
        # The caller's list survives raising exit paths (cancellation, stream
        # re-raises), unlike the returned outcome.
        attempt_run_ids = attempt_run_id_collector if attempt_run_id_collector is not None else []
        active_event_ids = self._active_response_event_ids(request.room_id)
        tool_trace: list[Any] = []
        transport_outcome: StreamTransportOutcome | None = None
        turn_recorder = self._build_turn_recorder(
            user_message=runtime.model_prompt,
            user_message_is_structured=request.current_prompt_is_structured,
            reply_to_event_id=request.reply_to_event_id,
            requester_id=request.user_id,
            matrix_run_metadata=_materialize_matrix_run_metadata(request.matrix_run_metadata),
        )

        def build_outcome(delivery: FinalDeliveryOutcome) -> _ResponseGenerationOutcome:
            return _generation_outcome(delivery, turn_recorder)

        try:
            try:
                transport_outcome = await self.generate_streaming_ai_response(
                    request,
                    identity=response_identity,
                    run_id=run_id,
                    runtime=runtime,
                    active_event_ids=active_event_ids,
                    turn_recorder=turn_recorder,
                    tool_trace=tool_trace,
                    run_metadata_content=run_metadata_content,
                    attempt_run_id_collector=attempt_run_ids,
                    pipeline_timing=request.pipeline_timing,
                    visible_event_id_callback=on_delivery_started,
                )
            finally:
                await lifecycle.emit_session_started(session_started_watch)
        except StreamingDeliveryError as error:
            stream_transport_outcome = error.transport_outcome
            if stream_transport_outcome.terminal_status == "cancelled":
                log_cancelled_response_source(
                    self.deps.logger,
                    cancel_source=stream_transport_outcome.resolved_cancel_source or "interrupted",
                    message_id=error.event_id,
                    restart_message="Bot streaming response interrupted by sync restart",
                    user_stop_message="Bot streaming response cancelled by user",
                    interrupted_message="Bot streaming response interrupted — traceback for diagnosis",
                    exc_info=(type(error.error), error.error, error.error.__traceback__),
                )
            else:
                self.deps.logger.exception("Error in streaming response", error=str(error.error))
            tool_trace[:] = error.tool_trace
            if self._record_stream_delivery_error(
                recorder=turn_recorder,
                accumulated_text=error.accumulated_text,
                tool_trace=error.tool_trace,
            ):
                await self._persist_interrupted_recorder_off_loop(
                    recorder=turn_recorder,
                    session_scope=session_scope,
                    session_id=runtime.session_id,
                    execution_identity=runtime.tool_dispatch.execution_identity,
                    run_id=run_id,
                    is_team=False,
                    response_event_id=error.event_id,
                )
            response_extra_content = _merge_response_extra_content(
                run_metadata_content,
                request.attachment_ids,
            )
            return build_outcome(
                await self.deps.delivery_gateway.finalize_streamed_response(
                    FinalizeStreamedResponseRequest(
                        target=runtime.resolved_target,
                        stream_transport_outcome=stream_transport_outcome,
                        initial_delivery_kind="edited" if request.existing_event_id else "sent",
                        identity=response_identity,
                        tool_trace=error.tool_trace if self._show_tool_calls() else None,
                        extra_content=response_extra_content,
                        existing_event_id=request.existing_event_id,
                        existing_event_is_placeholder=request.existing_event_is_placeholder,
                    ),
                ),
            )
        except ResponsePausedForApproval:
            raise
        except asyncio.CancelledError as exc:
            log_cancelled_response(
                self.deps.logger,
                exc=exc,
                message_id=request.existing_event_id,
                restart_message="Bot streaming response interrupted by sync restart",
                user_stop_message="Bot streaming response cancelled by user",
                interrupted_message="Bot streaming response interrupted — traceback for diagnosis",
            )
            raise
        except Exception as error:
            self.deps.logger.exception("Error in streaming response", error=str(error))
            return build_outcome(
                await self.deps.delivery_gateway.finalize_streamed_response(
                    FinalizeStreamedResponseRequest(
                        target=runtime.resolved_target,
                        stream_transport_outcome=build_terminal_stream_transport_outcome(
                            PendingVisibleResponse(
                                tracked_event_id=request.existing_event_id,
                                run_message_id=None,
                                existing_event_id=request.existing_event_id,
                                existing_event_is_placeholder=request.existing_event_is_placeholder,
                            ),
                            terminal_status="error",
                            failure_reason=str(error),
                            placeholder_body=PROGRESS_PLACEHOLDER,
                        ),
                        initial_delivery_kind="edited" if request.existing_event_id else "sent",
                        identity=response_identity,
                        tool_trace=list(tool_trace) if self._show_tool_calls() else None,
                        extra_content=_merge_response_extra_content(
                            run_metadata_content,
                            request.attachment_ids,
                        ),
                        existing_event_id=request.existing_event_id,
                        existing_event_is_placeholder=request.existing_event_is_placeholder,
                    ),
                ),
            )

        response_extra_content = _merge_response_extra_content(
            run_metadata_content,
            request.attachment_ids,
        )
        if on_delivery_started is not None:
            on_delivery_started(transport_outcome.last_physical_stream_event_id)
        delivery = await self._finalize_streamed_turn(
            request=request,
            delivery_target=runtime.resolved_target,
            transport_outcome=transport_outcome,
            delivery_kind="edited" if request.existing_event_id else "sent",
            response_identity=response_identity,
            tool_trace=tool_trace if self._show_tool_calls() else None,
            extra_content=response_extra_content,
        )
        return build_outcome(delivery)

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
        early_placeholder_state: _EarlyPlaceholderState | None = None,
    ) -> str | None:
        """Generate one agent response after acquiring the per-thread lock."""
        placeholder_state = early_placeholder_state or _EarlyPlaceholderState()
        history_scope = self.deps.state_writer.history_scope()
        execution_identity = self.deps.tool_runtime.build_execution_identity(
            target=resolved_target,
            user_id=request.user_id,
        )
        if not request.prompt.strip():
            return await self._finalize_empty_prompt_locked(
                request,
                resolved_target=resolved_target,
                response_kind="ai",
                history_scope=history_scope,
                execution_identity=execution_identity,
            )
        prepared_request = await self._begin_locked_turn(
            request,
            resolved_target=resolved_target,
            history_scope=history_scope,
            execution_identity=execution_identity,
            placeholder_message="Thinking...",
            early_placeholder_state=placeholder_state,
        )
        if prepared_request is None:
            return None
        request = prepared_request
        memory_prompt, memory_thread_history, model_prompt_text, model_thread_history = (
            prepare_memory_and_model_context(
                request.prompt,
                request.thread_history,
                config=self.deps.runtime.config,
                runtime_paths=self.deps.runtime_paths,
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
        reprioritize_auto_flush_sessions(
            self.deps.storage_path,
            self.deps.runtime.config,
            agent_name=self.deps.agent_name,
            active_session_id=session_id,
            execution_identity=execution_identity,
        )

        use_streaming = await should_use_streaming(
            self._client(),
            request.room_id,
            requester_user_id=request.user_id,
            enable_streaming=self.deps.runtime.enable_streaming,
        )
        self._note_pipeline_metadata(request, response_kind="agent", used_streaming=use_streaming)
        generation: _ResponseGenerationOutcome | None = None
        attempt_run_ids: list[str] = []
        response_run_id = str(uuid4())
        progress = _DeliveryProgress(tracked_event_id=request.existing_event_id)
        response_identity = self._response_identity(request, response_kind="ai")
        lifecycle = self._build_lifecycle(
            identity=response_identity,
            request=request,
        )

        queue_memory_persistence = self._memory_persistence(
            agent_name=self.deps.agent_name,
            session_id=session_id,
            execution_identity=execution_identity,
            prompt=memory_prompt,
            thread_history=memory_thread_history,
            user_id=request.user_id,
        )

        persist_response_event_id = self._build_persist_response_event_id_effect(
            session_id=session_id,
            session_type=self.deps.state_writer.session_type_for_scope(history_scope),
            create_storage=lambda: self.deps.state_writer.create_storage(execution_identity),
        )

        async def generate(message_id: str | None) -> None:
            nonlocal generation
            progress.track_event(message_id)
            delivery_request = self._request_for_delivery(normalized_request, message_id=message_id)
            if use_streaming:
                generation = await self._process_and_respond_streaming(
                    delivery_request,
                    run_id=response_run_id,
                    on_delivery_started=progress.note_delivery_started,
                    attempt_run_id_collector=attempt_run_ids,
                )
            else:
                generation = await self._process_and_respond(
                    delivery_request,
                    run_id=response_run_id,
                    on_delivery_started=progress.note_delivery_started,
                    attempt_run_id_collector=attempt_run_ids,
                )
            progress.settle(generation.delivery)

        def build_post_response_outcome(final_delivery_outcome: FinalDeliveryOutcome) -> ResponseOutcome:
            return ResponseOutcome(
                # The live collector list also covers raising exit paths, where the
                # returned generation outcome never materialized.
                response_run_id=attempt_run_ids[-1] if attempt_run_ids else response_run_id,
                session_id=session_id,
                session_type=self.deps.state_writer.session_type_for_scope(self.deps.state_writer.history_scope()),
                execution_identity=execution_identity,
                run_succeeded=(
                    generation.run_succeeded
                    if generation is not None
                    else final_delivery_outcome.terminal_status == "completed"
                ),
                response_target=resolved_target,
                thread_summary_room_id=(request.room_id if resolved_target.resolved_thread_id is not None else None),
                thread_summary_thread_id=resolved_target.resolved_thread_id,
                thread_summary_message_count_hint=thread_summary_message_count_hint(
                    request.thread_history,
                    trusted_sender_ids=current_internal_sender_ids(
                        self.deps.runtime.config,
                        self.deps.runtime_paths,
                    ),
                ),
                thread_summary_entity_name=self.deps.agent_name,
                memory_prompt=memory_prompt,
                memory_thread_history=memory_thread_history,
            )

        placeholder_state.settlement_started = True
        return await self._run_and_settle_locked_response(
            request,
            target=resolved_target,
            lifecycle=lifecycle,
            progress=progress,
            response_function=generate,
            user_id=request.user_id,
            run_id=response_run_id,
            build_post_response_outcome=build_post_response_outcome,
            post_response_deps=lambda: self._post_response_deps(
                request,
                queue_memory_persistence=queue_memory_persistence,
                persist_response_event_id=persist_response_event_id,
            ),
            approval_suspension_handler=lambda paused: self._suspend_for_approval(
                paused,
                request=request,
                target=resolved_target,
                progress=progress,
                execution_identity=execution_identity,
                entity_kind="agent",
                history_scope=history_scope,
            ),
        )
