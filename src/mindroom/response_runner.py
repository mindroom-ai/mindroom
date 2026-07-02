"""Response lifecycle execution extracted from ``bot.py``."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal, TypeVar
from uuid import uuid4

from agno.db.base import SessionType
from agno.session.agent import AgentSession
from agno.session.team import TeamSession

from mindroom.agent_run_context import append_knowledge_availability_enrichment
from mindroom.agents import show_tool_calls_for_agent
from mindroom.ai import ai_response, build_matrix_run_metadata, stream_agent_response
from mindroom.ai_run_metadata import ai_run_extra_content_from_metadata
from mindroom.background_tasks import create_background_task
from mindroom.constants import ATTACHMENT_IDS_KEY, ORIGINAL_SENDER_KEY, ROUTER_AGENT_NAME
from mindroom.entity_resolution import entity_identity_registry
from mindroom.final_delivery import FinalDeliveryOutcome, StreamTransportOutcome
from mindroom.history import HistoryScope, has_pending_force_compaction_scope, read_scope_state
from mindroom.history.interrupted_replay import persist_interrupted_replay_snapshot
from mindroom.history.turn_recorder import TurnRecorder
from mindroom.matrix.client_visible_messages import replace_visible_message
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
    cancel_source_from_failure_reason,
    classify_cancel_source,
    log_cancelled_response,
    log_cancelled_response_source,
    request_task_cancel,
)
from mindroom.post_response_effects import PostResponseEffectsSupport, ResponseOutcome
from mindroom.response_attempt import ResponseAttemptDeps, ResponseAttemptRequest, ResponseAttemptRunner
from mindroom.response_terminal import (
    PendingVisibleResponse,
    build_placeholder_terminal_stream_transport_outcome,
    build_terminal_stream_transport_outcome,
)
from mindroom.runtime_shutdown import GENERIC_SHUTDOWN, RuntimeShutdownIntent
from mindroom.streaming import (
    PROGRESS_PLACEHOLDER,
    ReplacementStreamingResponse,
    StreamingDeliveryError,
    StreamingResponse,
    clean_partial_reply_text,
    strip_visible_tool_markers,
)
from mindroom.teams import TeamMode, select_model_for_team, team_response, team_response_stream
from mindroom.thread_summary import thread_summary_message_count_hint
from mindroom.timing import DispatchPipelineTiming, timed
from mindroom.tool_system.dynamic_toolkits import visible_tool_surface
from mindroom.tool_system.runtime_context import ToolDispatchContext, runtime_context_from_dispatch_context
from mindroom.tool_system.worker_routing import run_with_tool_execution_identity, stream_with_tool_execution_identity
from mindroom.user_turn_time import prefix_user_turn_time

from .delivery_gateway import (
    CancelledVisibleNoteRequest,
    DeliveryGateway,
    FinalDeliveryRequest,
    FinalizeStreamedResponseRequest,
    MatrixCompactionLifecycle,
    StreamingDeliveryRequest,
)
from .media_inputs import MediaInputs
from .response_lifecycle import (
    QueuedHumanNoticeReservation,
    ResponseLifecycle,
    ResponseLifecycleCoordinator,
    ResponseLifecycleDeps,
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
    from mindroom.history import CompactionOutcome, HistoryScope
    from mindroom.hooks import EnrichmentItem, MessageEnvelope
    from mindroom.knowledge import KnowledgeAccessSupport
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.matrix.identity import MatrixID
    from mindroom.message_target import MessageTarget
    from mindroom.response_payload_preparation import ResponsePayloadPreparation, ResponsePayloadPreparer
    from mindroom.stop import StopManager
    from mindroom.streaming import StreamInputChunk
    from mindroom.tool_system.events import ToolTraceEntry
    from mindroom.tool_system.runtime_context import ToolRuntimeSupport
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

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


def _append_matrix_prompt_context(
    prompt: str,
    *,
    target: MessageTarget,
    include_context: bool,
) -> str:
    """Append room/thread/event ids to the prompt when messaging tools are available."""
    if not include_context:
        return prompt
    if "[Matrix metadata for tool calls]" in prompt:
        return prompt

    metadata_block = "\n".join(
        (
            "[Matrix metadata for tool calls]",
            f"room_id: {target.room_id}",
            f"thread_id: {target.resolved_thread_id or 'none'}",
            f"reply_to_event_id: {target.reply_to_event_id or 'none'}",
            "Use these IDs when calling matrix_message.",
        ),
    )
    return f"{prompt.rstrip()}\n\n{metadata_block}"


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
    system_enrichment_items: tuple[EnrichmentItem, ...] = ()
    requires_model_history_refresh: bool = False
    payload_preparation: ResponsePayloadPreparation | None = None
    current_timestamp_ms: float | None = None
    current_prompt_is_structured: bool = False
    on_lifecycle_lock_acquired: Callable[[], None] | None = None
    pipeline_timing: DispatchPipelineTiming | None = None
    queued_notice_reservation: QueuedHumanNoticeReservation | None = None
    on_sync_restart_cancelled: Callable[[], None] | None = None

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


@dataclass(frozen=True)
class _ResponseGenerationOutcome:
    """What one locked response generation produced, returned instead of out-params."""

    delivery: FinalDeliveryOutcome
    run_succeeded: bool
    attempt_run_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class _TeamResponseRequest:
    """Typed carrier for one team response request plus team-specific inputs."""

    request: ResponseRequest
    team_agents: tuple[MatrixID, ...]
    team_mode: str
    reason_prefix: str = "Team request"


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


@dataclass(frozen=True)
class _PreparedResponseRuntime:
    """Resolved runtime context shared by streaming and non-streaming responses."""

    resolved_target: MessageTarget
    response_thread_id: str | None
    media_inputs: MediaInputs
    session_id: str
    model_prompt: str
    tool_dispatch: ToolDispatchContext
    room_mode: bool = False


@dataclass
class ResponseRunner:
    """Run one response lifecycle while keeping bot seams patchable."""

    deps: ResponseRunnerDeps
    _lifecycle_coordinator: ResponseLifecycleCoordinator = field(
        default_factory=ResponseLifecycleCoordinator,
        init=False,
    )
    _in_flight_response_count: int = field(default=0, init=False)
    _inbox_response_tasks: set[asyncio.Task[None]] = field(default_factory=set, init=False)

    def track_inbox_response(self, response: Coroutine[Any, Any, None], *, name: str) -> asyncio.Task[None]:
        """Own one detached inbox response until it completes or a drain settles it."""
        task = asyncio.create_task(response, name=name)
        self._inbox_response_tasks.add(task)
        task.add_done_callback(self._finish_inbox_response_task)
        return task

    def _finish_inbox_response_task(self, task: asyncio.Task[None]) -> None:
        self._inbox_response_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
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
        return False

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
        """Return the number of active response lifecycles."""
        return self._in_flight_response_count

    @in_flight_response_count.setter
    def in_flight_response_count(self, value: int) -> None:
        """Update the number of active response lifecycles."""
        self._in_flight_response_count = value

    def _show_tool_calls(self, agent_name: str | None = None) -> bool:
        """Return tool-call visibility for the current or target agent."""
        return show_tool_calls_for_agent(
            self.deps.runtime.config,
            agent_name or self.deps.agent_name,
        )

    def _build_turn_recorder(
        self,
        *,
        user_message: str,
        reply_to_event_id: str | None,
        matrix_run_metadata: dict[str, Any] | None,
    ) -> TurnRecorder:
        """Create one lifecycle-owned recorder seeded with canonical Matrix metadata."""
        recorder = TurnRecorder(user_message=user_message)
        recorder.set_run_metadata(
            build_matrix_run_metadata(
                reply_to_event_id,
                [],
                extra_metadata=matrix_run_metadata,
            ),
        )
        return recorder

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
        if recorder.outcome != "interrupted":
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
        await asyncio.shield(offload)

    def _record_stream_delivery_error(
        self,
        *,
        recorder: TurnRecorder,
        accumulated_text: str,
        tool_trace: Sequence[ToolTraceEntry],
    ) -> bool:
        """Capture canonical interrupted replay state from one failed stream delivery."""
        partial_text = clean_partial_reply_text(strip_visible_tool_markers(accumulated_text))
        completed_tools, interrupted_tools = _split_delivery_tool_trace(tool_trace)
        if not partial_text:
            partial_text = recorder.assistant_text
        if not completed_tools:
            completed_tools = list(recorder.completed_tools)
        if not interrupted_tools:
            interrupted_tools = list(recorder.interrupted_tools)
        if not partial_text and not completed_tools and not interrupted_tools:
            return False
        recorder.record_interrupted(
            run_metadata=recorder.run_metadata,
            assistant_text=partial_text,
            completed_tools=completed_tools,
            interrupted_tools=interrupted_tools,
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
        locked_operation: Callable[[MessageTarget], Awaitable[str | None]],
    ) -> str | None:
        """Run one locked response operation with shared queued-message bookkeeping."""
        resolved_target = request.response_envelope.target
        return await self._lifecycle_coordinator.run_locked_response(
            target=resolved_target,
            response_envelope=request.response_envelope,
            queued_notice_reservation=request.queued_notice_reservation,
            pipeline_timing=request.pipeline_timing,
            locked_operation=locked_operation,
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

    def _has_queued_forced_compaction(
        self,
        *,
        session_id: str,
        scope: HistoryScope,
        execution_identity: ToolExecutionIdentity | None,
    ) -> bool:
        """Return whether this scope should compact before creating a reply placeholder."""
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
    ) -> ResponseRequest:
        """Refresh model-facing thread history once this turn owns the lifecycle lock."""
        if request.thread_id is None:
            return request

        try:
            refreshed_history = await self.deps.resolver.fetch_thread_history(
                request.room_id,
                request.thread_id,
                caller_label="dispatch_post_lock_refresh",
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
        return replace(
            request,
            thread_history=refreshed_history,
            requires_model_history_refresh=False,
        )

    async def _prepare_request_after_lock(
        self,
        request: ResponseRequest,
    ) -> ResponseRequest:
        """Refresh thread history and rebuild any history-derived payload once locked."""
        try:
            request = await self._refresh_model_history_after_lock(request)
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

    def _notify_sync_restart_cancelled(
        self,
        request: ResponseRequest,
        final_outcome: FinalDeliveryOutcome,
        *,
        delivery_cancelled: bool,
        delivery_failure_reason: str | None,
    ) -> None:
        """Tell the dispatcher when stall recovery interrupted a marked-handled turn.

        Only turns that end as a visible interrupted note are reported: they get
        recorded in the handled-turn ledger, so the post-restart sync replay
        dedups them away and an explicit retry is their only recovery. Unmarked
        turns are recovered by that replay instead; retrying them too would
        answer twice.
        """
        delivery_cancelled = delivery_cancelled or final_outcome.terminal_status == "cancelled"
        delivery_failure_reason = delivery_failure_reason or final_outcome.failure_reason
        if request.on_sync_restart_cancelled is None or not delivery_cancelled:
            return
        if not final_outcome.mark_handled:
            return
        if cancel_source_from_failure_reason(delivery_failure_reason) != "sync_restart":
            return
        request.on_sync_restart_cancelled()

    def _build_lifecycle(
        self,
        *,
        response_kind: str,
        request: ResponseRequest,
        correlation_id: str | None = None,
    ) -> ResponseLifecycle:
        """Build one lifecycle helper with the resolved shared response context."""
        return ResponseLifecycle(
            ResponseLifecycleDeps(
                response_hooks=self.deps.delivery_gateway.deps.response_hooks,
                logger=self.deps.logger,
            ),
            response_kind=response_kind,
            pipeline_timing=request.pipeline_timing,
            response_envelope=request.response_envelope,
            correlation_id=correlation_id or self._correlation_id_for_request(request),
        )

    async def _finalize_empty_prompt_locked(
        self,
        request: ResponseRequest,
        *,
        resolved_target: MessageTarget,
        response_kind: str,
    ) -> str | None:
        """Finalize one empty prompt through the canonical response lifecycle."""
        if request.on_lifecycle_lock_acquired is not None:
            request.on_lifecycle_lock_acquired()
        request = await self._prepare_request_after_lock(request)
        request = self._request_with_locked_target(request, resolved_target)
        lifecycle = self._build_lifecycle(
            response_kind=response_kind,
            request=request,
            correlation_id=self._correlation_id_for_request(request),
        )
        final_outcome = await lifecycle.finalize(
            FinalDeliveryOutcome.cancelled_for_empty_prompt(),
            build_post_response_outcome=lambda _final_outcome: ResponseOutcome(),
            post_response_deps=lambda: self.deps.post_response_effects.build_deps(
                room_id=request.room_id,
                interactive_agent_name=self.deps.agent_name,
            ),
        )
        return final_outcome.final_visible_event_id if final_outcome.mark_handled else None

    async def generate_team_response_helper(
        self,
        request: ResponseRequest,
        *,
        team_agents: list[MatrixID],
        team_mode: str,
        reason_prefix: str = "Team request",
    ) -> str | None:
        """Generate a team response with lifecycle locking and queued-message state."""
        team_request = _TeamResponseRequest(
            request=request,
            team_agents=tuple(team_agents),
            team_mode=team_mode,
            reason_prefix=reason_prefix,
        )
        return await self._run_locked_response_lifecycle(
            request,
            locked_operation=lambda resolved_target: self.generate_team_response_helper_locked(
                team_request,
                resolved_target=resolved_target,
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
            locked_operation=lambda resolved_target: self._finalize_empty_prompt_locked(
                request,
                resolved_target=resolved_target,
                response_kind=response_kind,
            ),
        )

    async def generate_team_response_helper_locked(  # noqa: C901, PLR0912, PLR0915
        self,
        team_request: _TeamResponseRequest,
        *,
        resolved_target: MessageTarget,
    ) -> str | None:
        """Generate a team response once the per-thread lifecycle lock is held."""
        request = team_request.request
        if not request.prompt.strip():
            return await self._finalize_empty_prompt_locked(
                request,
                resolved_target=resolved_target,
                response_kind="team",
            )
        if request.on_lifecycle_lock_acquired is not None:
            request.on_lifecycle_lock_acquired()
        request = await self._prepare_request_after_lock(request)
        request = self._request_with_locked_target(request, resolved_target)
        if request.pipeline_timing is not None:
            request.pipeline_timing.mark("thread_refresh_ready")
        team_request = replace(team_request, request=request)
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
        include_matrix_prompt_context = any(
            _agent_has_matrix_messaging_tool(self.deps.runtime.config, name, resolved_target.session_id)
            for name in agent_names
        )
        model_message = _append_matrix_prompt_context(
            prepared_prompt,
            target=resolved_target,
            include_context=include_matrix_prompt_context,
        )
        resolved_request = self._request_with_locked_target(
            replace(
                request,
                thread_history=model_thread_history,
                media=request.media or MediaInputs(),
            ),
            resolved_target,
        )
        resolved_response_envelope = resolved_request.response_envelope
        resolved_correlation_id = self._correlation_id_for_request(request)
        lifecycle = self._build_lifecycle(
            response_kind="team",
            request=resolved_request,
            correlation_id=resolved_correlation_id,
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
            session_id=session_id,
            attachment_ids=request.attachment_ids,
            correlation_id=resolved_correlation_id,
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
        session_scope = self.deps.state_writer.team_history_scope(
            list(team_request.team_agents),
            requester_user_id=execution_identity.requester_id,
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
        final_delivery_outcome: FinalDeliveryOutcome | None = None
        compaction_outcomes: list[CompactionOutcome] = []
        team_run_metadata_content: dict[str, Any] = {}
        tracked_event_id: str | None = request.existing_event_id
        delivery_stage_started = False
        delivery_failure_reason: str | None = None
        delivery_cancelled = False
        matrix_run_metadata = _materialize_matrix_run_metadata(request.matrix_run_metadata)
        active_event_ids = self._active_response_event_ids(request.room_id)
        team_turn_recorder = self._build_turn_recorder(
            user_message=request.prompt,
            reply_to_event_id=request.reply_to_event_id,
            matrix_run_metadata=matrix_run_metadata,
        )

        persist_response_event_id = self._build_persist_response_event_id_effect(
            session_id=session_id,
            session_type=session_type,
            create_storage=team_storage_factory,
        )

        async def generate_team_response(message_id: str | None) -> None:  # noqa: C901, PLR0912, PLR0915
            nonlocal final_delivery_outcome, tracked_event_id, delivery_stage_started
            delivery_request = self._request_for_delivery(delivery_request_base, message_id=message_id)
            if message_id is not None:
                tracked_event_id = message_id
                team_turn_recorder.set_response_event_id(message_id)
            compaction_lifecycle = self._build_compaction_lifecycle(
                target=delivery_target,
                request=delivery_request,
            )

            def _note_attempt_run_id(current_run_id: str) -> None:
                self.deps.stop_manager.update_run_id(message_id, current_run_id)
                team_turn_recorder.set_run_id(current_run_id)

            def _note_visible_response_event_id(response_event_id: str) -> None:
                nonlocal tracked_event_id
                tracked_event_id = response_event_id
                team_turn_recorder.set_response_event_id(response_event_id)

            if use_streaming and (
                delivery_request.existing_event_id is None or delivery_request.existing_event_is_placeholder
            ):
                async with typing_indicator(self._client(), request.room_id):
                    event_id: str | None = None

                    def build_response_stream() -> AsyncIterator[StreamInputChunk]:
                        return team_response_stream(
                            agent_ids=list(team_request.team_agents),
                            message=model_message,
                            orchestrator=orchestrator,
                            execution_identity=tool_dispatch.execution_identity,
                            mode=mode,
                            thread_history=model_thread_history,
                            model_name=model_name,
                            media=resolved_request.media,
                            show_tool_calls=show_tool_calls,
                            session_id=session_id,
                            run_id=response_run_id,
                            run_id_callback=_note_attempt_run_id,
                            user_id=requester_user_id,
                            reply_to_event_id=request.reply_to_event_id,
                            current_timestamp_ms=request.current_timestamp_ms,
                            current_prompt_is_structured=request.current_prompt_is_structured,
                            correlation_id=resolved_correlation_id,
                            active_event_ids=active_event_ids,
                            response_sender_id=self.deps.matrix_full_id,
                            compaction_outcomes_collector=compaction_outcomes,
                            run_metadata_collector=team_run_metadata_content,
                            compaction_lifecycle=compaction_lifecycle,
                            configured_team_name=self.deps.agent_name
                            if self.deps.agent_name in self.deps.runtime.config.teams
                            else None,
                            system_enrichment_items=request.system_enrichment_items,
                            reason_prefix=team_request.reason_prefix,
                            matrix_run_metadata=matrix_run_metadata,
                            pipeline_timing=request.pipeline_timing,
                            turn_recorder=team_turn_recorder,
                        )

                    response_stream = self._stream_in_tool_context(
                        tool_dispatch=tool_dispatch,
                        stream_factory=build_response_stream,
                    )

                    try:
                        delivery_stage_started = True
                        transport_outcome = await self.deps.delivery_gateway.deliver_stream(
                            StreamingDeliveryRequest(
                                target=delivery_target,
                                response_stream=response_stream,
                                existing_event_id=delivery_request.existing_event_id,
                                adopt_existing_placeholder=bool(delivery_request.existing_event_id)
                                and delivery_request.existing_event_is_placeholder,
                                header=None,
                                show_tool_calls=show_tool_calls,
                                streaming_cls=ReplacementStreamingResponse,
                                pipeline_timing=request.pipeline_timing,
                                visible_event_id_callback=_note_visible_response_event_id,
                            ),
                        )
                        event_id = transport_outcome.last_physical_stream_event_id
                        if event_id:
                            tracked_event_id = event_id
                    except asyncio.CancelledError:
                        await self._persist_interrupted_recorder_off_loop(
                            recorder=team_turn_recorder,
                            session_scope=session_scope,
                            session_id=session_id,
                            execution_identity=tool_dispatch.execution_identity,
                            run_id=response_run_id,
                            is_team=True,
                            response_event_id=tracked_event_id,
                        )
                        raise
                    finally:
                        await lifecycle.emit_session_started(session_started_watch)
                if request.pipeline_timing is not None:
                    request.pipeline_timing.mark("streaming_complete")
                delivery_kind: Literal["sent", "edited"] = "edited" if message_id else "sent"
                finalize_request = FinalizeStreamedResponseRequest(
                    target=delivery_target,
                    stream_transport_outcome=transport_outcome,
                    initial_delivery_kind=delivery_kind,
                    response_kind="team",
                    response_envelope=resolved_response_envelope,
                    correlation_id=resolved_correlation_id,
                    tool_trace=None,
                    extra_content=_merge_response_extra_content(
                        team_run_metadata_content
                        or ai_run_extra_content_from_metadata(team_turn_recorder.run_metadata),
                        request.attachment_ids,
                    ),
                    existing_event_id=request.existing_event_id,
                    existing_event_is_placeholder=request.existing_event_is_placeholder,
                )
                final_delivery_outcome = await self.deps.delivery_gateway.finalize_streamed_response(
                    finalize_request,
                )
                if request.pipeline_timing is not None:
                    request.pipeline_timing.mark_first_visible_reply("final")
                    request.pipeline_timing.mark("response_complete")
            else:
                try:
                    try:
                        async with typing_indicator(self._client(), request.room_id):

                            async def build_response_text() -> str:
                                return await team_response(
                                    agent_names=agent_names,
                                    mode=mode,
                                    message=model_message,
                                    orchestrator=orchestrator,
                                    execution_identity=tool_dispatch.execution_identity,
                                    thread_history=model_thread_history,
                                    model_name=model_name,
                                    media=resolved_request.media,
                                    session_id=session_id,
                                    run_id=response_run_id,
                                    run_id_callback=_note_attempt_run_id,
                                    user_id=requester_user_id,
                                    reply_to_event_id=request.reply_to_event_id,
                                    current_timestamp_ms=request.current_timestamp_ms,
                                    current_prompt_is_structured=request.current_prompt_is_structured,
                                    correlation_id=resolved_correlation_id,
                                    active_event_ids=active_event_ids,
                                    response_sender_id=self.deps.matrix_full_id,
                                    compaction_outcomes_collector=compaction_outcomes,
                                    run_metadata_collector=team_run_metadata_content,
                                    compaction_lifecycle=compaction_lifecycle,
                                    configured_team_name=self.deps.agent_name
                                    if self.deps.agent_name in self.deps.runtime.config.teams
                                    else None,
                                    system_enrichment_items=request.system_enrichment_items,
                                    reason_prefix=team_request.reason_prefix,
                                    matrix_run_metadata=matrix_run_metadata,
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
                                    response_event_id=tracked_event_id,
                                )
                                raise
                    finally:
                        await lifecycle.emit_session_started(session_started_watch)
                except asyncio.CancelledError as exc:
                    log_cancelled_response(
                        self.deps.logger,
                        exc=exc,
                        message_id=message_id,
                        restart_message="Team non-streaming response interrupted by sync restart",
                        user_stop_message="Team non-streaming response cancelled by user",
                        interrupted_message="Team non-streaming response interrupted — traceback for diagnosis",
                    )
                    if message_id:
                        cancel_source = classify_cancel_source(exc)
                        final_delivery_outcome = await self.deps.delivery_gateway.deliver_cancelled_visible_note(
                            CancelledVisibleNoteRequest(
                                target=delivery_target,
                                event_id=message_id,
                                existing_event_is_placeholder=delivery_request.existing_event_is_placeholder,
                                cancel_source=cancel_source,
                                response_kind="team",
                                response_envelope=resolved_response_envelope,
                                correlation_id=resolved_correlation_id,
                            ),
                        )
                    else:
                        failure_reason = cancel_failure_reason(classify_cancel_source(exc))
                        final_delivery_outcome = FinalDeliveryOutcome(
                            terminal_status="cancelled",
                            event_id=None,
                            failure_reason=failure_reason,
                        )
                    return

                delivery_stage_started = True
                try:
                    final_delivery_outcome = await self.deps.delivery_gateway.deliver_final(
                        FinalDeliveryRequest(
                            target=delivery_target,
                            existing_event_id=message_id,
                            existing_event_is_placeholder=delivery_request.existing_event_is_placeholder,
                            response_text=response_text,
                            response_kind="team",
                            response_envelope=resolved_response_envelope,
                            correlation_id=resolved_correlation_id,
                            tool_trace=None,
                            extra_content=_merge_response_extra_content(
                                team_run_metadata_content
                                or ai_run_extra_content_from_metadata(team_turn_recorder.run_metadata),
                                request.attachment_ids,
                            ),
                        ),
                    )
                except asyncio.CancelledError:
                    await self._persist_interrupted_recorder_off_loop(
                        recorder=team_turn_recorder,
                        session_scope=session_scope,
                        session_id=session_id,
                        execution_identity=tool_dispatch.execution_identity,
                        run_id=response_run_id,
                        is_team=True,
                        response_event_id=tracked_event_id,
                    )
                    raise
                if request.pipeline_timing is not None:
                    request.pipeline_timing.mark_first_visible_reply("final")
                    request.pipeline_timing.mark("response_complete")

        thinking_msg = None
        if not request.existing_event_id and not self._has_queued_forced_compaction(
            session_id=session_id,
            scope=session_scope,
            execution_identity=tool_dispatch.execution_identity,
        ):
            thinking_msg = "🤝 Team Response: Thinking..."

        run_message_id: str | None = None
        stream_transport_outcome: StreamTransportOutcome | None = None

        def note_task_cancelled(failure_reason: str) -> None:
            nonlocal delivery_failure_reason, delivery_cancelled
            delivery_failure_reason = failure_reason
            delivery_cancelled = True

        try:
            run_message_id = await self.run_cancellable_response(
                target=delivery_target,
                response_function=generate_team_response,
                thinking_message=thinking_msg,
                existing_event_id=request.existing_event_id,
                user_id=requester_user_id,
                run_id=response_run_id,
                pipeline_timing=request.pipeline_timing,
                on_cancelled=note_task_cancelled,
            )
            if tracked_event_id is None:
                tracked_event_id = run_message_id
        except StreamingDeliveryError as error:
            stream_transport_outcome = error.transport_outcome
            if stream_transport_outcome.terminal_status == "cancelled":
                log_cancelled_response_source(
                    self.deps.logger,
                    cancel_source=cancel_source_from_failure_reason(stream_transport_outcome.failure_reason),
                    message_id=error.event_id,
                    restart_message="Team streaming response interrupted by sync restart",
                    user_stop_message="Team streaming response cancelled by user",
                    interrupted_message="Team streaming response interrupted — traceback for diagnosis",
                    exc_info=(type(error.error), error.error, error.error.__traceback__),
                )
            else:
                self.deps.logger.exception("Error in team streaming response", error=str(error.error))
            if error.event_id:
                tracked_event_id = error.event_id
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
                    response_event_id=tracked_event_id,
                )
            final_delivery_outcome = await self.deps.delivery_gateway.finalize_streamed_response(
                FinalizeStreamedResponseRequest(
                    target=delivery_target,
                    stream_transport_outcome=stream_transport_outcome,
                    initial_delivery_kind="edited" if request.existing_event_id else "sent",
                    response_kind="team",
                    response_envelope=resolved_response_envelope,
                    correlation_id=resolved_correlation_id,
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
        except Exception as error:
            if not delivery_stage_started:
                raise
            tracked_event_id = tracked_event_id or run_message_id
            delivery_failure_reason = str(error)
            self._log_delivery_failure(response_kind="team", error=error)
        if final_delivery_outcome is None and delivery_failure_reason is not None:
            final_delivery_outcome = await self.deps.delivery_gateway.finalize_streamed_response(
                FinalizeStreamedResponseRequest(
                    target=delivery_target,
                    stream_transport_outcome=build_terminal_stream_transport_outcome(
                        PendingVisibleResponse(
                            tracked_event_id=tracked_event_id,
                            run_message_id=run_message_id if request.existing_event_id is None else None,
                            existing_event_id=request.existing_event_id,
                            existing_event_is_placeholder=request.existing_event_is_placeholder,
                        ),
                        terminal_status="cancelled" if delivery_cancelled else "error",
                        failure_reason=delivery_failure_reason or "late_stream_delivery_failure",
                        placeholder_body=PROGRESS_PLACEHOLDER,
                    ),
                    initial_delivery_kind="edited" if request.existing_event_id else "sent",
                    response_kind="team",
                    response_envelope=resolved_response_envelope,
                    correlation_id=resolved_correlation_id,
                    tool_trace=None,
                    extra_content=_merge_response_extra_content(
                        team_run_metadata_content
                        or ai_run_extra_content_from_metadata(team_turn_recorder.run_metadata),
                        request.attachment_ids,
                    ),
                    existing_event_id=request.existing_event_id,
                    existing_event_is_placeholder=request.existing_event_is_placeholder,
                ),
            )
        assert final_delivery_outcome is not None
        final_outcome = await lifecycle.finalize(
            final_delivery_outcome,
            build_post_response_outcome=lambda _final_outcome: ResponseOutcome(
                response_run_id=team_turn_recorder.run_id or response_run_id,
                session_id=session_id,
                session_type=SessionType.TEAM,
                execution_identity=tool_dispatch.execution_identity,
                run_succeeded=team_turn_recorder.outcome == "completed",
                interactive_target=resolved_target,
                thread_summary_room_id=(request.room_id if resolved_target.resolved_thread_id is not None else None),
                thread_summary_thread_id=resolved_target.resolved_thread_id,
                thread_summary_message_count_hint=thread_summary_message_count_hint(request.thread_history),
                thread_summary_entity_name=self.deps.agent_name,
                memory_prompt=_memory_prompt,
                memory_thread_history=_memory_thread_history,
            ),
            post_response_deps=lambda: self.deps.post_response_effects.build_deps(
                room_id=request.room_id,
                interactive_agent_name=self.deps.agent_name,
                persist_response_event_id=persist_response_event_id,
            ),
        )
        self._notify_sync_restart_cancelled(
            request,
            final_outcome,
            delivery_cancelled=delivery_cancelled,
            delivery_failure_reason=delivery_failure_reason,
        )
        return final_outcome.final_visible_event_id if final_outcome.mark_handled else None

    async def run_cancellable_response(
        self,
        *,
        target: MessageTarget,
        response_function: Callable[[str | None], Coroutine[Any, Any, None]],
        thinking_message: str | None = None,
        existing_event_id: str | None = None,
        user_id: str | None = None,
        run_id: str | None = None,
        pipeline_timing: DispatchPipelineTiming | None = None,
        on_cancelled: Callable[[str], None] | None = None,
    ) -> _MatrixEventId | None:
        """Run one response generation function with cancellation support."""
        try:
            self.in_flight_response_count += 1
            return await ResponseAttemptRunner(
                ResponseAttemptDeps(
                    client=self._client(),
                    delivery_gateway=self.deps.delivery_gateway,
                    stop_manager=self.deps.stop_manager,
                    logger=self.deps.logger,
                    show_stop_button=lambda: self.deps.runtime.config.defaults.show_stop_button,
                    config=self.deps.runtime.config,
                    notify_outbound_event=self.deps.resolver.deps.conversation_cache.notify_outbound_event,
                    notify_outbound_redaction=(
                        self.deps.post_response_effects.conversation_cache.notify_outbound_redaction
                    ),
                ),
            ).run(
                ResponseAttemptRequest(
                    target=target,
                    response_function=response_function,
                    thinking_message=thinking_message,
                    existing_event_id=existing_event_id,
                    user_id=user_id,
                    run_id=run_id,
                    pipeline_timing=pipeline_timing,
                    on_cancelled=on_cancelled,
                ),
            )
        finally:
            self.in_flight_response_count -= 1

    async def _prepare_response_runtime_common(
        self,
        request: ResponseRequest,
        *,
        existing_event_uses_thread_id: bool,
        room_mode: bool,
    ) -> _PreparedResponseRuntime:
        resolved_target = request.response_envelope.target
        response_thread_id = (
            request.thread_id
            if request.existing_event_id and existing_event_uses_thread_id
            else request.response_envelope.target.resolved_thread_id
        )
        resolved_target = resolved_target.with_thread_root(response_thread_id)
        media_inputs = request.media or MediaInputs()
        session_id = resolved_target.session_id
        resolved_model_prompt = _append_matrix_prompt_context(
            request.model_prompt or request.prompt,
            target=resolved_target,
            include_context=_agent_has_matrix_messaging_tool(
                self.deps.runtime.config,
                self.deps.agent_name,
                session_id,
            ),
        )
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
            session_id=session_id,
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
            room_mode=room_mode,
        )

    @timed("prepare_non_streaming_runtime")
    async def prepare_non_streaming_runtime(
        self,
        request: ResponseRequest,
    ) -> _PreparedResponseRuntime:
        """Resolve non-streaming runtime context."""
        return await self._prepare_response_runtime_common(
            request,
            existing_event_uses_thread_id=not request.existing_event_is_placeholder,
            room_mode=False,
        )

    @timed("prepare_streaming_runtime")
    async def prepare_streaming_runtime(
        self,
        request: ResponseRequest,
    ) -> _PreparedResponseRuntime:
        """Resolve streaming runtime context."""
        room_mode = (
            self.deps.runtime.config.get_entity_thread_mode(
                self.deps.agent_name,
                self.deps.runtime_paths,
                room_id=request.room_id,
            )
            == "room"
        )
        return await self._prepare_response_runtime_common(
            request,
            existing_event_uses_thread_id=not request.existing_event_is_placeholder,
            room_mode=room_mode,
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
        tool_trace: list[Any],
        run_metadata_content: dict[str, Any],
        compaction_outcomes: list[CompactionOutcome],
        attempt_run_id_collector: list[str],
        pipeline_timing: DispatchPipelineTiming | None = None,
    ) -> str:
        """Run one non-streaming AI request."""
        compaction_lifecycle = self._build_compaction_lifecycle(
            target=runtime.resolved_target,
            request=request,
        )

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
            system_enrichment_items = append_knowledge_availability_enrichment(
                request.system_enrichment_items,
                knowledge_resolution.unavailable,
            )
            matrix_run_metadata = _materialize_matrix_run_metadata(request.matrix_run_metadata)
            return await ai_response(
                agent_name=self.deps.agent_name,
                prompt=request.prompt,
                session_id=runtime.session_id,
                runtime_paths=self.deps.runtime_paths,
                config=self.deps.runtime.config,
                thread_history=request.thread_history,
                model_prompt=runtime.model_prompt,
                current_timestamp_ms=request.current_timestamp_ms,
                current_prompt_is_structured=request.current_prompt_is_structured,
                thread_id=runtime.resolved_target.resolved_thread_id,
                room_id=request.room_id,
                knowledge=knowledge_resolution.knowledge,
                user_id=request.user_id,
                run_id=run_id,
                run_id_callback=note_attempt_run_id,
                media=runtime.media_inputs,
                reply_to_event_id=request.reply_to_event_id,
                correlation_id=self._correlation_id_for_request(request),
                active_event_ids=active_event_ids,
                show_tool_calls=show_tool_calls,
                collect_streamed_response=show_tool_calls,
                tool_trace_collector=tool_trace,
                run_metadata_collector=run_metadata_content,
                execution_identity=runtime.tool_dispatch.execution_identity,
                compaction_outcomes_collector=compaction_outcomes,
                compaction_lifecycle=compaction_lifecycle,
                refresh_scheduler=(
                    self.deps.runtime.orchestrator.knowledge_refresh_scheduler
                    if self.deps.runtime.orchestrator is not None
                    else None
                ),
                matrix_run_metadata=matrix_run_metadata,
                system_enrichment_items=system_enrichment_items,
                turn_recorder=turn_recorder,
                pipeline_timing=pipeline_timing,
            )

        try:
            async with typing_indicator(self._client(), request.room_id):
                return await self._run_in_tool_context(
                    tool_dispatch=runtime.tool_dispatch,
                    operation=build_response_text,
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
        compaction_outcomes: list[CompactionOutcome],
        attempt_run_id_collector: list[str],
        pipeline_timing: DispatchPipelineTiming | None = None,
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

        knowledge_resolution = self.deps.knowledge_access.resolve_for_agent(
            self.deps.agent_name,
            execution_identity=runtime.tool_dispatch.execution_identity,
        )
        system_enrichment_items = append_knowledge_availability_enrichment(
            request.system_enrichment_items,
            knowledge_resolution.unavailable,
        )
        matrix_run_metadata = _materialize_matrix_run_metadata(request.matrix_run_metadata)
        response_stream = stream_agent_response(
            agent_name=self.deps.agent_name,
            prompt=request.prompt,
            session_id=runtime.session_id,
            runtime_paths=self.deps.runtime_paths,
            config=self.deps.runtime.config,
            thread_history=request.thread_history,
            model_prompt=runtime.model_prompt,
            current_timestamp_ms=request.current_timestamp_ms,
            current_prompt_is_structured=request.current_prompt_is_structured,
            thread_id=runtime.resolved_target.resolved_thread_id,
            room_id=request.room_id,
            knowledge=knowledge_resolution.knowledge,
            user_id=request.user_id,
            run_id=run_id,
            run_id_callback=note_attempt_run_id,
            media=runtime.media_inputs,
            reply_to_event_id=request.reply_to_event_id,
            correlation_id=self._correlation_id_for_request(request),
            active_event_ids=active_event_ids,
            show_tool_calls=self._show_tool_calls(),
            run_metadata_collector=run_metadata_content,
            execution_identity=runtime.tool_dispatch.execution_identity,
            compaction_outcomes_collector=compaction_outcomes,
            compaction_lifecycle=compaction_lifecycle,
            refresh_scheduler=(
                self.deps.runtime.orchestrator.knowledge_refresh_scheduler
                if self.deps.runtime.orchestrator is not None
                else None
            ),
            matrix_run_metadata=matrix_run_metadata,
            system_enrichment_items=system_enrichment_items,
            turn_recorder=turn_recorder,
            pipeline_timing=pipeline_timing,
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

    async def process_and_respond(  # noqa: C901
        self,
        request: ResponseRequest,
        *,
        run_id: str | None = None,
        response_kind: str = "ai",
        on_delivery_started: Callable[[str | None], None] | None = None,
    ) -> _ResponseGenerationOutcome:
        """Process a message and send a response without streaming."""
        if request.pipeline_timing is not None:
            request.pipeline_timing.mark("response_runtime_start")
        runtime = await self.prepare_non_streaming_runtime(request)
        if request.pipeline_timing is not None:
            request.pipeline_timing.mark("response_runtime_ready")
        request = self._request_with_locked_target(request, runtime.resolved_target)
        response_envelope = request.response_envelope
        correlation_id = self._correlation_id_for_request(request)
        lifecycle = self._build_lifecycle(
            response_kind=response_kind,
            request=request,
            correlation_id=correlation_id,
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
        tool_trace: list[Any] = []
        compaction_outcomes: list[CompactionOutcome] = []
        run_metadata_content: dict[str, Any] = {}
        attempt_run_ids: list[str] = []
        active_event_ids = self._active_response_event_ids(request.room_id)
        turn_recorder = self._build_turn_recorder(
            user_message=request.prompt,
            reply_to_event_id=request.reply_to_event_id,
            matrix_run_metadata=_materialize_matrix_run_metadata(request.matrix_run_metadata),
        )

        def build_outcome(delivery: FinalDeliveryOutcome) -> _ResponseGenerationOutcome:
            return _ResponseGenerationOutcome(
                delivery=delivery,
                run_succeeded=turn_recorder.outcome == "completed",
                attempt_run_ids=tuple(attempt_run_ids),
            )

        try:
            try:
                response_text = await self.generate_non_streaming_ai_response(
                    request,
                    run_id=run_id,
                    runtime=runtime,
                    active_event_ids=active_event_ids,
                    turn_recorder=turn_recorder,
                    tool_trace=tool_trace,
                    run_metadata_content=run_metadata_content,
                    compaction_outcomes=compaction_outcomes,
                    attempt_run_id_collector=attempt_run_ids,
                    pipeline_timing=request.pipeline_timing,
                )
            finally:
                await lifecycle.emit_session_started(session_started_watch)
        except asyncio.CancelledError as exc:
            cancel_source = classify_cancel_source(exc)
            log_cancelled_response(
                self.deps.logger,
                exc=exc,
                message_id=request.existing_event_id,
                restart_message="Non-streaming response interrupted by sync restart",
                user_stop_message="Non-streaming response cancelled by user",
                interrupted_message="Non-streaming response interrupted — traceback for diagnosis",
            )
            if request.existing_event_id:
                return build_outcome(
                    await self.deps.delivery_gateway.deliver_cancelled_visible_note(
                        CancelledVisibleNoteRequest(
                            target=runtime.resolved_target,
                            event_id=request.existing_event_id,
                            existing_event_is_placeholder=request.existing_event_is_placeholder,
                            cancel_source=cancel_source,
                            response_kind=response_kind,
                            response_envelope=response_envelope,
                            correlation_id=correlation_id,
                        ),
                    ),
                )
            failure_reason = cancel_failure_reason(cancel_source)
            return build_outcome(
                FinalDeliveryOutcome(
                    terminal_status="cancelled",
                    event_id=None,
                    failure_reason=failure_reason,
                ),
            )
        except Exception as error:
            self.deps.logger.exception("Error in non-streaming response", error=str(error))
            raise

        response_extra_content = _merge_response_extra_content(
            run_metadata_content,
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
                    response_text=response_text,
                    response_kind=response_kind,
                    response_envelope=response_envelope,
                    correlation_id=correlation_id,
                    tool_trace=tool_trace if self._show_tool_calls() else None,
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
        if request.pipeline_timing is not None:
            request.pipeline_timing.mark_first_visible_reply("final")
            request.pipeline_timing.mark("response_complete")
        return build_outcome(delivery)

    async def process_and_respond_streaming(  # noqa: C901, PLR0915
        self,
        request: ResponseRequest,
        *,
        run_id: str | None = None,
        response_kind: str = "ai",
        on_delivery_started: Callable[[str | None], None] | None = None,
    ) -> _ResponseGenerationOutcome:
        """Process a message and send a streamed response."""
        if request.pipeline_timing is not None:
            request.pipeline_timing.mark("response_runtime_start")
        runtime = await self.prepare_streaming_runtime(request)
        if request.pipeline_timing is not None:
            request.pipeline_timing.mark("response_runtime_ready")
        request = self._request_with_locked_target(request, runtime.resolved_target)
        response_envelope = request.response_envelope
        correlation_id = self._correlation_id_for_request(request)
        lifecycle = self._build_lifecycle(
            response_kind=response_kind,
            request=request,
            correlation_id=correlation_id,
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
        compaction_outcomes: list[CompactionOutcome] = []
        run_metadata_content: dict[str, Any] = {}
        attempt_run_ids: list[str] = []
        active_event_ids = self._active_response_event_ids(request.room_id)
        tool_trace: list[Any] = []
        transport_outcome: StreamTransportOutcome | None = None
        turn_recorder = self._build_turn_recorder(
            user_message=request.prompt,
            reply_to_event_id=request.reply_to_event_id,
            matrix_run_metadata=_materialize_matrix_run_metadata(request.matrix_run_metadata),
        )

        def build_outcome(delivery: FinalDeliveryOutcome) -> _ResponseGenerationOutcome:
            return _ResponseGenerationOutcome(
                delivery=delivery,
                run_succeeded=turn_recorder.outcome == "completed",
                attempt_run_ids=tuple(attempt_run_ids),
            )

        try:
            try:
                transport_outcome = await self.generate_streaming_ai_response(
                    request,
                    run_id=run_id,
                    runtime=runtime,
                    active_event_ids=active_event_ids,
                    turn_recorder=turn_recorder,
                    tool_trace=tool_trace,
                    run_metadata_content=run_metadata_content,
                    compaction_outcomes=compaction_outcomes,
                    attempt_run_id_collector=attempt_run_ids,
                    pipeline_timing=request.pipeline_timing,
                )
            finally:
                await lifecycle.emit_session_started(session_started_watch)
        except StreamingDeliveryError as error:
            stream_transport_outcome = error.transport_outcome
            if stream_transport_outcome.terminal_status == "cancelled":
                log_cancelled_response_source(
                    self.deps.logger,
                    cancel_source=cancel_source_from_failure_reason(stream_transport_outcome.failure_reason),
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
                        response_kind=response_kind,
                        response_envelope=response_envelope,
                        correlation_id=correlation_id,
                        tool_trace=error.tool_trace if self._show_tool_calls() else None,
                        extra_content=response_extra_content,
                        existing_event_id=request.existing_event_id,
                        existing_event_is_placeholder=request.existing_event_is_placeholder,
                    ),
                ),
            )
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
                        response_kind=response_kind,
                        response_envelope=response_envelope,
                        correlation_id=correlation_id,
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
        delivery_kind: Literal["sent", "edited"] = "edited" if request.existing_event_id else "sent"
        if on_delivery_started is not None:
            on_delivery_started(transport_outcome.last_physical_stream_event_id)
        finalize_request = FinalizeStreamedResponseRequest(
            target=runtime.resolved_target,
            stream_transport_outcome=transport_outcome,
            initial_delivery_kind=delivery_kind,
            response_kind=response_kind,
            response_envelope=response_envelope,
            correlation_id=correlation_id,
            tool_trace=tool_trace if self._show_tool_calls() else None,
            extra_content=response_extra_content,
            existing_event_id=request.existing_event_id,
            existing_event_is_placeholder=request.existing_event_is_placeholder,
        )
        delivery = await self.deps.delivery_gateway.finalize_streamed_response(finalize_request)
        if request.pipeline_timing is not None:
            request.pipeline_timing.mark_first_visible_reply("final")
            request.pipeline_timing.mark("response_complete")
        return build_outcome(delivery)

    async def generate_response(self, request: ResponseRequest) -> str | None:
        """Generate and send/edit an agent response with lifecycle locking."""
        return await self._run_locked_response_lifecycle(
            request,
            locked_operation=lambda resolved_target: self.generate_response_locked(
                request,
                resolved_target=resolved_target,
            ),
        )

    async def generate_response_locked(  # noqa: C901, PLR0912, PLR0915
        self,
        request: ResponseRequest,
        *,
        resolved_target: MessageTarget,
    ) -> str | None:
        """Generate one agent response after acquiring the per-thread lock."""
        if not request.prompt.strip():
            return await self._finalize_empty_prompt_locked(
                request,
                resolved_target=resolved_target,
                response_kind="ai",
            )
        if request.on_lifecycle_lock_acquired is not None:
            request.on_lifecycle_lock_acquired()
        request = await self._prepare_request_after_lock(request)
        request = self._request_with_locked_target(request, resolved_target)
        if request.pipeline_timing is not None:
            request.pipeline_timing.mark("thread_refresh_ready")
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
        execution_identity = self.deps.tool_runtime.build_execution_identity(
            target=resolved_target,
            user_id=request.user_id,
            session_id=session_id,
        )
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
        final_delivery_outcome: FinalDeliveryOutcome | None = None
        generation: _ResponseGenerationOutcome | None = None
        response_run_id = str(uuid4())
        tracked_event_id: str | None = request.existing_event_id
        delivery_stage_started = False
        delivery_failure_reason: str | None = None
        delivery_cancelled = False
        early_delivery_error: BaseException | None = None
        request = self._request_with_locked_target(request, resolved_target)
        resolved_correlation_id = self._correlation_id_for_request(request)
        resolved_response_envelope = request.response_envelope
        lifecycle = self._build_lifecycle(
            response_kind="ai",
            request=request,
            correlation_id=resolved_correlation_id,
        )

        def queue_memory_persistence() -> None:
            mark_auto_flush_dirty_session(
                self.deps.storage_path,
                self.deps.runtime.config,
                agent_name=self.deps.agent_name,
                session_id=session_id,
                execution_identity=execution_identity,
            )
            if self.deps.runtime.config.resolve_entity(self.deps.agent_name).memory_backend == "mem0":
                create_background_task(
                    store_conversation_memory(
                        memory_prompt,
                        self.deps.agent_name,
                        self.deps.storage_path,
                        session_id,
                        self.deps.runtime.config,
                        self.deps.runtime_paths,
                        memory_thread_history,
                        request.user_id,
                        execution_identity=execution_identity,
                    ),
                    name=f"memory_save_{self.deps.agent_name}_{session_id}",
                    owner=self.deps.runtime,
                )

        persist_response_event_id = self._build_persist_response_event_id_effect(
            session_id=session_id,
            session_type=self.deps.state_writer.session_type_for_scope(self.deps.state_writer.history_scope()),
            create_storage=lambda: self.deps.state_writer.create_storage(execution_identity),
        )

        def note_delivery_started(event_id: str | None) -> None:
            nonlocal delivery_stage_started, tracked_event_id
            delivery_stage_started = True
            if event_id:
                tracked_event_id = event_id

        def note_task_cancelled(failure_reason: str) -> None:
            nonlocal delivery_failure_reason, delivery_cancelled
            delivery_failure_reason = failure_reason
            delivery_cancelled = True

        async def finalize_pre_delivery_terminal_response(
            event_id: str,
            *,
            terminal_status: Literal["cancelled", "error"],
            failure_reason: str,
        ) -> None:
            nonlocal final_delivery_outcome
            final_delivery_outcome = await self.deps.delivery_gateway.finalize_streamed_response(
                FinalizeStreamedResponseRequest(
                    target=resolved_target,
                    stream_transport_outcome=build_placeholder_terminal_stream_transport_outcome(
                        event_id,
                        terminal_status=terminal_status,
                        failure_reason=failure_reason,
                        placeholder_body=PROGRESS_PLACEHOLDER,
                    ),
                    initial_delivery_kind="edited" if request.existing_event_id else "sent",
                    response_kind="ai",
                    response_envelope=resolved_response_envelope,
                    correlation_id=resolved_correlation_id,
                    tool_trace=None,
                    extra_content=None,
                    existing_event_id=request.existing_event_id,
                    existing_event_is_placeholder=request.existing_event_is_placeholder,
                ),
            )

        async def generate(message_id: str | None) -> None:
            nonlocal final_delivery_outcome, generation, tracked_event_id
            if message_id is not None:
                tracked_event_id = message_id
            delivery_request = self._request_for_delivery(normalized_request, message_id=message_id)
            if use_streaming:
                generation = await self.process_and_respond_streaming(
                    delivery_request,
                    run_id=response_run_id,
                    on_delivery_started=note_delivery_started,
                )
            else:
                generation = await self.process_and_respond(
                    delivery_request,
                    run_id=response_run_id,
                    on_delivery_started=note_delivery_started,
                )
            final_delivery_outcome = generation.delivery

        thinking_msg = None
        if not request.existing_event_id and not self._has_queued_forced_compaction(
            session_id=session_id,
            scope=self.deps.state_writer.history_scope(),
            execution_identity=execution_identity,
        ):
            thinking_msg = "Thinking..."

        run_message_id: str | None = None
        try:
            run_message_id = await self.run_cancellable_response(
                target=resolved_target,
                response_function=generate,
                thinking_message=thinking_msg,
                existing_event_id=request.existing_event_id,
                user_id=request.user_id,
                run_id=response_run_id,
                pipeline_timing=request.pipeline_timing,
                on_cancelled=note_task_cancelled,
            )
            tracked_event_id = tracked_event_id or run_message_id
        except asyncio.CancelledError as error:
            if not delivery_stage_started:
                delivery_failure_reason = cancel_failure_reason(classify_cancel_source(error))
                delivery_cancelled = True
                tracked_event_id = tracked_event_id or run_message_id
                event_id = tracked_event_id or (
                    request.existing_event_id if request.existing_event_is_placeholder else None
                )
                if event_id is not None:
                    await finalize_pre_delivery_terminal_response(
                        event_id,
                        terminal_status="cancelled",
                        failure_reason=delivery_failure_reason,
                    )
                else:
                    final_delivery_outcome = FinalDeliveryOutcome(
                        terminal_status="cancelled",
                        event_id=None,
                        failure_reason=delivery_failure_reason,
                    )
                early_delivery_error = error
            else:
                raise
        except Exception as error:
            if not delivery_stage_started:
                delivery_failure_reason = str(error) or "delivery_failed_before_start"
                tracked_event_id = tracked_event_id or run_message_id
                event_id = tracked_event_id or (
                    request.existing_event_id if request.existing_event_is_placeholder else None
                )
                if event_id is not None:
                    await finalize_pre_delivery_terminal_response(
                        event_id,
                        terminal_status="error",
                        failure_reason=delivery_failure_reason,
                    )
                else:
                    final_delivery_outcome = FinalDeliveryOutcome(
                        terminal_status="error",
                        event_id=None,
                        failure_reason=delivery_failure_reason,
                    )
                early_delivery_error = error
            else:
                self._log_delivery_failure(response_kind="ai", error=error)
        if final_delivery_outcome is None and delivery_cancelled:
            if not delivery_stage_started:
                tracked_event_id = tracked_event_id or run_message_id
                event_id = tracked_event_id or (
                    request.existing_event_id if request.existing_event_is_placeholder else None
                )
                if event_id is not None:
                    await finalize_pre_delivery_terminal_response(
                        event_id,
                        terminal_status="cancelled",
                        failure_reason=delivery_failure_reason or "interrupted",
                    )
                else:
                    final_delivery_outcome = FinalDeliveryOutcome(
                        terminal_status="cancelled",
                        event_id=None,
                        failure_reason=delivery_failure_reason or "interrupted",
                    )
            else:
                final_delivery_outcome = FinalDeliveryOutcome(
                    terminal_status="cancelled",
                    event_id=None,
                    failure_reason=delivery_failure_reason or "interrupted",
                )
        assert final_delivery_outcome is not None
        post_response_outcome = ResponseOutcome(
            response_run_id=(
                generation.attempt_run_ids[-1]
                if generation is not None and generation.attempt_run_ids
                else response_run_id
            ),
            session_id=session_id,
            session_type=self.deps.state_writer.session_type_for_scope(self.deps.state_writer.history_scope()),
            execution_identity=execution_identity,
            run_succeeded=(
                generation.run_succeeded
                if generation is not None
                else final_delivery_outcome.terminal_status == "completed"
            ),
            interactive_target=resolved_target,
            thread_summary_room_id=(request.room_id if resolved_target.resolved_thread_id is not None else None),
            thread_summary_thread_id=resolved_target.resolved_thread_id,
            thread_summary_message_count_hint=thread_summary_message_count_hint(request.thread_history),
            thread_summary_entity_name=self.deps.agent_name,
            memory_prompt=memory_prompt,
            memory_thread_history=memory_thread_history,
        )
        post_response_deps = self.deps.post_response_effects.build_deps(
            room_id=request.room_id,
            interactive_agent_name=self.deps.agent_name,
            queue_memory_persistence=queue_memory_persistence,
            persist_response_event_id=persist_response_event_id,
        )
        try:
            final_outcome = await lifecycle.finalize(
                final_delivery_outcome,
                build_post_response_outcome=lambda _final_outcome: post_response_outcome,
                post_response_deps=post_response_deps,
            )
        except asyncio.CancelledError as exc:
            failure_reason = cancel_failure_reason(classify_cancel_source(exc))
            cancelled_outcome = FinalDeliveryOutcome(
                terminal_status="cancelled",
                event_id=final_delivery_outcome.final_visible_event_id,
                is_visible_response=final_delivery_outcome.final_visible_event_id is not None,
                final_visible_body=final_delivery_outcome.final_visible_body,
                failure_reason=failure_reason,
                tool_trace=final_delivery_outcome.tool_trace,
                extra_content=final_delivery_outcome.extra_content,
            )
            await lifecycle.finalize(
                cancelled_outcome,  # lifecycle.finalize cancelled terminal outcome before re-raising
                build_post_response_outcome=lambda _final_outcome: post_response_outcome,
                post_response_deps=post_response_deps,
            )
            raise
        if early_delivery_error is not None:
            raise early_delivery_error
        self._notify_sync_restart_cancelled(
            request,
            final_outcome,
            delivery_cancelled=delivery_cancelled,
            delivery_failure_reason=delivery_failure_reason,
        )
        return final_outcome.final_visible_event_id if final_outcome.mark_handled else None
