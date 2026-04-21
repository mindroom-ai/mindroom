"""Response lifecycle execution extracted from ``bot.py``."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeVar
from uuid import uuid4
from zoneinfo import ZoneInfo

from agno.db.base import SessionType

from mindroom import interactive
from mindroom.agents import get_agent_session, get_team_session, show_tool_calls_for_agent
from mindroom.ai import (
    ai_response,
    build_matrix_run_metadata,
    queued_message_signal_context,
    stream_agent_response,
)
from mindroom.background_tasks import create_background_task
from mindroom.constants import (
    ATTACHMENT_IDS_KEY,
    ORIGINAL_SENDER_KEY,
    ROUTER_AGENT_NAME,
    STREAM_STATUS_CANCELLED,
    STREAM_STATUS_ERROR,
    STREAM_STATUS_KEY,
    STREAM_STATUS_PENDING,
)
from mindroom.history.interrupted_replay import persist_interrupted_replay_snapshot
from mindroom.history.turn_recorder import TurnRecorder
from mindroom.history.types import HistoryScope
from mindroom.hooks import (
    EnrichmentItem,
    MessageEnvelope,
    SessionHookContext,
    emit,
)
from mindroom.hooks.ingress import is_automation_source_kind
from mindroom.hooks.types import EVENT_SESSION_STARTED
from mindroom.knowledge import KnowledgeAccessSupport, ensure_request_knowledge_managers
from mindroom.logging_config import bound_log_context
from mindroom.matrix.client_visible_messages import replace_visible_message
from mindroom.matrix.identity import is_agent_id
from mindroom.matrix.presence import is_user_online, should_use_streaming
from mindroom.matrix.typing import typing_indicator
from mindroom.memory import (
    mark_auto_flush_dirty_session,
    reprioritize_auto_flush_sessions,
    store_conversation_memory,
    strip_user_turn_time_prefix,
)
from mindroom.orchestration.runtime import is_sync_restart_cancel
from mindroom.post_response_effects import (
    PostResponseEffectsSupport,
    ResponseOutcome,
)
from mindroom.streaming import (
    ReplacementStreamingResponse,
    StreamingDeliveryError,
    StreamingResponse,
    build_restart_interrupted_body,
    clean_partial_reply_text,
)
from mindroom.teams import TeamMode, select_model_for_team, team_response, team_response_stream
from mindroom.thread_summary import thread_summary_message_count_hint
from mindroom.timing import DispatchPipelineTiming, timed
from mindroom.tool_system.runtime_context import (
    ToolDispatchContext,
    resolve_tool_runtime_hook_bindings,
    runtime_context_from_dispatch_context,
)
from mindroom.tool_system.worker_routing import (
    run_with_tool_execution_identity,
    stream_with_tool_execution_identity,
)

from .delivery_gateway import (
    DeliveryGateway,
    DeliveryResult,
    EditTextRequest,
    FinalDeliveryRequest,
    FinalizeStreamedResponseRequest,
    SendTextRequest,
    StreamingDeliveryRequest,
)
from .media_inputs import MediaInputs
from .response_lifecycle import DeliveryOutcome, ResponseLifecycle

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Mapping, Sequence
    from pathlib import Path

    import nio
    import structlog
    from agno.db.sqlite import SqliteDb

    from mindroom.bot_runtime_view import BotRuntimeView
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.conversation_resolver import ConversationResolver
    from mindroom.conversation_state_writer import ConversationStateWriter
    from mindroom.history.types import CompactionOutcome
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.matrix.identity import MatrixID
    from mindroom.message_target import MessageTarget
    from mindroom.stop import StopManager
    from mindroom.tool_system.events import ToolTraceEntry
    from mindroom.tool_system.runtime_context import (
        ToolRuntimeContext,
        ToolRuntimeSupport,
    )
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

_CANCELLED_RESPONSE_TEXT = "**[Response cancelled by user]**"
_ToolContextResult = TypeVar("_ToolContextResult")
_ToolStreamChunk = TypeVar("_ToolStreamChunk")
_VISIBLE_TOOL_MARKER_LINE_PATTERN = re.compile(r"^\s*🔧 `[^`]+` \[\d+\](?: ⏳)?\s*$")


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


def _strip_visible_tool_markers(text: str) -> str:
    """Remove Matrix-visible tool marker lines from streamed text before replay persistence."""
    filtered_lines = [line for line in text.splitlines() if not _VISIBLE_TOOL_MARKER_LINE_PATTERN.fullmatch(line)]
    return "\n".join(filtered_lines).rstrip()


def _materialize_matrix_run_metadata(
    matrix_run_metadata: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Return a concrete metadata dict for downstream APIs that require one."""
    if matrix_run_metadata is None:
        return None
    return dict(matrix_run_metadata)


def _agent_has_matrix_messaging_tool(config: Config, agent_name: str) -> bool:
    """Return whether one agent can issue Matrix message actions."""
    try:
        tool_names = config.get_agent_tools(agent_name)
    except ValueError:
        return False
    return "matrix_message" in tool_names


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


def _prefix_user_turn_time(
    prompt: str,
    *,
    timezone: str,
    timestamp_ms: float | None = None,
) -> str:
    """Prefix one user-authored turn with local date and time."""
    if not prompt.strip() or strip_user_turn_time_prefix(prompt) != prompt:
        return prompt
    tz = ZoneInfo(timezone)
    current = datetime.now(tz) if timestamp_ms is None else datetime.fromtimestamp(timestamp_ms / 1000, tz)
    timezone_abbrev = current.tzname() or timezone
    return f"[{current.strftime('%Y-%m-%d %H:%M')} {timezone_abbrev}] {prompt}"


def _timestamp_thread_history_user_turns(
    thread_history: Sequence[ResolvedVisibleMessage],
    *,
    config: Config,
    runtime_paths: RuntimePaths,
) -> list[ResolvedVisibleMessage]:
    """Add local timestamps to user-authored thread history entries."""
    timestamped_history: list[ResolvedVisibleMessage] = []
    for message in thread_history:
        is_user_turn = isinstance(message.content.get(ORIGINAL_SENDER_KEY), str) or not is_agent_id(
            message.sender,
            config,
            runtime_paths,
        )
        if not is_user_turn:
            timestamped_history.append(message)
            continue

        timestamped_body = _prefix_user_turn_time(
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
    model_prompt_text = _prefix_user_turn_time(
        model_prompt_content,
        timezone=config.timezone,
    )
    model_thread_history = _timestamp_thread_history_user_turns(
        thread_history,
        config=config,
        runtime_paths=runtime_paths,
    )
    return prompt, thread_history, model_prompt_text, model_thread_history


class _ReplyEventWithSource(Protocol):
    """Minimal reply event surface needed for skill command responses."""

    source: dict[str, Any]


@dataclass
class _QueuedMessageState:
    """Track queued human ingress while one response lifecycle holds the lock."""

    pending_human_messages: int = 0
    _active_response_turns: int = 0
    _event: asyncio.Event = field(default_factory=asyncio.Event)

    def begin_response_turn(self) -> bool:
        existing_turn = self._active_response_turns > 0
        self._active_response_turns += 1
        return existing_turn

    def finish_response_turn(self) -> None:
        if self._active_response_turns == 0:
            return
        self._active_response_turns -= 1

    def add_waiting_human_message(self) -> None:
        self.pending_human_messages += 1
        self._event.set()

    def consume_waiting_human_message(self) -> None:
        if self.pending_human_messages == 0:
            return
        self.pending_human_messages -= 1
        if self.pending_human_messages == 0:
            self._event.clear()

    def has_pending_human_messages(self) -> bool:
        return self.pending_human_messages > 0

    def has_active_response_turn(self) -> bool:
        return self._active_response_turns > 0

    async def wait(self) -> None:
        await self._event.wait()

    def is_set(self) -> bool:
        return self._event.is_set()


@dataclass(frozen=True)
class ResponseRequest:
    """Typed carrier for one response lifecycle request."""

    room_id: str
    reply_to_event_id: str
    thread_id: str | None
    thread_history: Sequence[ResolvedVisibleMessage]
    prompt: str
    model_prompt: str | None = None
    existing_event_id: str | None = None
    existing_event_is_placeholder: bool = False
    user_id: str | None = None
    media: MediaInputs | None = None
    attachment_ids: tuple[str, ...] | None = None
    response_envelope: MessageEnvelope | None = None
    correlation_id: str | None = None
    target: MessageTarget | None = None
    matrix_run_metadata: Mapping[str, Any] | None = None
    system_enrichment_items: tuple[EnrichmentItem, ...] = ()
    requires_full_thread_history: bool = False
    prepare_after_lock: Callable[[ResponseRequest], Awaitable[ResponseRequest]] | None = None
    on_lifecycle_lock_acquired: Callable[[], None] | None = None
    pipeline_timing: DispatchPipelineTiming | None = None


class PostLockRequestPreparationError(RuntimeError):
    """Raised when post-lock request preparation fails before generation starts."""


@dataclass(frozen=True)
class TeamResponseRequest:
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


@dataclass(frozen=True)
class _PreparedResponseRuntime:
    """Resolved runtime context shared by streaming and non-streaming responses."""

    resolved_target: MessageTarget
    response_thread_id: str | None
    media_inputs: MediaInputs
    session_id: str
    model_prompt: str
    tool_dispatch: ToolDispatchContext
    request_knowledge_managers: dict[str, Any]
    room_mode: bool = False


@dataclass
class ResponseRunner:
    """Run one response lifecycle while keeping bot seams patchable."""

    deps: ResponseRunnerDeps
    _response_lifecycle_locks: dict[tuple[str, str | None], asyncio.Lock] = field(default_factory=dict, init=False)
    _thread_queued_signals: dict[tuple[str, str | None], _QueuedMessageState] = field(default_factory=dict, init=False)
    _in_flight_response_count: int = field(default=0, init=False)

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

    def _log_post_response_effects_failure(
        self,
        *,
        response_kind: str,
        response_event_id: str,
        error: BaseException,
    ) -> None:
        """Log one non-fatal post-response failure after visible delivery succeeded."""
        self.deps.logger.error(
            "Post-response effects failed after visible delivery",
            response_kind=response_kind,
            response_event_id=response_event_id,
            failure_reason=str(error),
            error_type=error.__class__.__name__,
        )

    def _cancelled_response_update(self, *, restart: bool) -> tuple[str, dict[str, str]]:
        """Return the visible note and terminal status for one cancelled response."""
        if restart:
            return build_restart_interrupted_body(""), {STREAM_STATUS_KEY: STREAM_STATUS_ERROR}
        return _CANCELLED_RESPONSE_TEXT, {STREAM_STATUS_KEY: STREAM_STATUS_CANCELLED}

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
        reply_to_event_id: str,
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
        if response_event_id is not None:
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

    def _record_stream_delivery_error(
        self,
        *,
        recorder: TurnRecorder,
        accumulated_text: str,
        tool_trace: Sequence[ToolTraceEntry],
    ) -> bool:
        """Capture canonical interrupted replay state from one failed stream delivery."""
        partial_text = clean_partial_reply_text(_strip_visible_tool_markers(accumulated_text))
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
        thread_key = (target.room_id, target.resolved_thread_id)
        queued_signal = self._thread_queued_signals.get(thread_key)
        if queued_signal is not None and queued_signal.has_active_response_turn():
            return True
        lifecycle_lock = self._response_lifecycle_locks.get(thread_key)
        return lifecycle_lock.locked() if lifecycle_lock is not None else False

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

    def _resolve_request_target(self, request: ResponseRequest) -> MessageTarget:
        """Resolve the canonical response target for one request."""
        return request.target or (
            request.response_envelope.target
            if request.response_envelope is not None
            else self.deps.resolver.build_message_target(
                room_id=request.room_id,
                thread_id=request.thread_id,
                reply_to_event_id=request.reply_to_event_id,
            )
        )

    def _response_lifecycle_lock(self, target: MessageTarget) -> asyncio.Lock:
        """Return the per-target lock that serializes one response lifecycle."""
        lock_key = (target.room_id, target.resolved_thread_id)
        lock = self._response_lifecycle_locks.get(lock_key)
        if lock is not None:
            return lock
        if len(self._response_lifecycle_locks) >= 100:
            for candidate, candidate_lock in list(self._response_lifecycle_locks.items()):
                if len(self._response_lifecycle_locks) < 100:
                    break
                if candidate_lock.locked():
                    continue
                self._response_lifecycle_locks.pop(candidate, None)
                self._thread_queued_signals.pop(candidate, None)
        lock = asyncio.Lock()
        self._response_lifecycle_locks[lock_key] = lock
        return lock

    def _get_or_create_queued_signal(self, target: MessageTarget) -> _QueuedMessageState:
        """Return the queued-message signal for one canonical conversation thread."""
        thread_key = (target.room_id, target.resolved_thread_id)
        signal = self._thread_queued_signals.get(thread_key)
        if signal is not None:
            return signal
        signal = _QueuedMessageState()
        self._thread_queued_signals[thread_key] = signal
        return signal

    @staticmethod
    def _should_signal_queued_message(response_envelope: MessageEnvelope | None) -> bool:
        """Return whether one queued ingress should interrupt the active turn."""
        return response_envelope is not None and not is_automation_source_kind(response_envelope.source_kind)

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
        resolved_target = self._resolve_request_target(request)
        lifecycle_lock = self._response_lifecycle_lock(resolved_target)
        queued_signal = self._get_or_create_queued_signal(resolved_target)
        existing_turn = queued_signal.begin_response_turn()
        queued_human_message = (existing_turn or lifecycle_lock.locked()) and self._should_signal_queued_message(
            request.response_envelope,
        )
        if queued_human_message:
            queued_signal.add_waiting_human_message()
        lock_acquired = False
        try:
            if request.pipeline_timing is not None:
                request.pipeline_timing.mark("lock_wait_start")
            await lifecycle_lock.acquire()
            lock_acquired = True
            if request.pipeline_timing is not None:
                request.pipeline_timing.mark("lock_acquired")
            try:
                if queued_human_message:
                    queued_signal.consume_waiting_human_message()
                    queued_human_message = False
                with queued_message_signal_context(queued_signal):
                    return await locked_operation(resolved_target)
            finally:
                if lock_acquired:
                    lifecycle_lock.release()
        finally:
            if queued_human_message:
                queued_signal.consume_waiting_human_message()
            queued_signal.finish_response_turn()

    def _build_persist_response_event_id_effect(
        self,
        *,
        session_id: str,
        session_type: SessionType,
        create_storage: Callable[[], SqliteDb],
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

    def _session_exists(
        self,
        *,
        storage: SqliteDb,
        session_id: str,
        session_type: SessionType,
    ) -> bool:
        if session_type is SessionType.TEAM:
            return get_team_session(storage, session_id) is not None
        return get_agent_session(storage, session_id) is not None

    def _should_watch_session_started(
        self,
        *,
        tool_context: ToolRuntimeContext | None,
        session_id: str,
        session_type: SessionType,
        create_storage: Callable[[], SqliteDb],
    ) -> bool:
        if tool_context is None or not tool_context.hook_registry.has_hooks(EVENT_SESSION_STARTED):
            return False
        try:
            storage = create_storage()
            try:
                return not self._session_exists(
                    storage=storage,
                    session_id=session_id,
                    session_type=session_type,
                )
            finally:
                storage.close()
        except Exception as error:
            self.deps.logger.exception(
                "Failed to probe session storage for session:started eligibility",
                session_id=session_id,
                session_type=str(session_type),
                failure_reason=str(error),
            )
            return False

    async def _maybe_emit_session_started(
        self,
        *,
        tool_context: ToolRuntimeContext | None,
        should_watch_session_started: bool,
        scope: HistoryScope,
        session_id: str,
        room_id: str,
        thread_id: str | None,
        session_type: SessionType,
        correlation_id: str,
        create_storage: Callable[[], SqliteDb],
    ) -> None:
        if tool_context is None or not should_watch_session_started:
            return
        storage = create_storage()
        try:
            if not self._session_exists(storage=storage, session_id=session_id, session_type=session_type):
                return
        finally:
            storage.close()

        bindings = resolve_tool_runtime_hook_bindings(tool_context)
        context = SessionHookContext(
            event_name=EVENT_SESSION_STARTED,
            plugin_name="",
            settings={},
            config=tool_context.config,
            runtime_paths=tool_context.runtime_paths,
            logger=self.deps.logger.bind(event_name=EVENT_SESSION_STARTED, session_id=session_id),
            correlation_id=correlation_id,
            message_sender=bindings.message_sender,
            matrix_admin=bindings.matrix_admin,
            room_state_querier=bindings.room_state_querier,
            room_state_putter=bindings.room_state_putter,
            agent_name=scope.scope_id if scope.kind == "team" else tool_context.agent_name,
            scope=scope,
            session_id=session_id,
            room_id=room_id,
            thread_id=thread_id,
        )
        await emit(tool_context.hook_registry, EVENT_SESSION_STARTED, context)

    async def _emit_session_started_safely(
        self,
        *,
        tool_context: ToolRuntimeContext | None,
        should_watch_session_started: bool,
        scope: HistoryScope,
        session_id: str,
        room_id: str,
        thread_id: str | None,
        session_type: SessionType,
        correlation_id: str,
        create_storage: Callable[[], SqliteDb],
    ) -> None:
        """Emit session:started without aborting delivery on ordinary failures."""
        try:
            await self._maybe_emit_session_started(
                tool_context=tool_context,
                should_watch_session_started=should_watch_session_started,
                scope=scope,
                session_id=session_id,
                room_id=room_id,
                thread_id=thread_id,
                session_type=session_type,
                correlation_id=correlation_id,
                create_storage=create_storage,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.deps.logger.exception(
                "Failed to emit session:started",
                session_id=session_id,
                room_id=room_id,
                thread_id=thread_id,
                failure_reason=str(error),
            )

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

    async def _refresh_thread_history_after_lock(
        self,
        request: ResponseRequest,
    ) -> ResponseRequest:
        """Refresh thread history once this turn owns the lifecycle lock."""
        if request.thread_id is None:
            return request

        try:
            refreshed_history = await self.deps.resolver.fetch_thread_history(
                self._client(),
                request.room_id,
                request.thread_id,
            )
        except Exception as exc:
            if request.requires_full_thread_history:
                raise
            self.deps.logger.warning(
                "Failed to refresh thread history after lock; continuing with existing history",
                room_id=request.room_id,
                thread_id=request.thread_id,
                error=str(exc),
            )
            return request
        return replace(request, thread_history=refreshed_history, requires_full_thread_history=False)

    async def _prepare_request_after_lock(
        self,
        request: ResponseRequest,
    ) -> ResponseRequest:
        """Refresh thread history and rebuild any history-derived payload once locked."""
        try:
            request = await self._refresh_thread_history_after_lock(request)
            if request.prepare_after_lock is None:
                return request
            return await request.prepare_after_lock(request)
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

    def _emit_pipeline_timing_summary(
        self,
        request: ResponseRequest,
        *,
        outcome: str,
    ) -> None:
        """Emit one structured end-to-end timing summary when available."""
        if request.pipeline_timing is None:
            return
        request.pipeline_timing.emit_summary(self.deps.logger, outcome=outcome)

    @staticmethod
    def _response_outcome(delivery_result: DeliveryResult | None) -> str:
        """Return one pipeline outcome label for the final delivery result."""
        if delivery_result is not None and delivery_result.suppressed:
            return "suppressed"
        if delivery_result is not None and delivery_result.delivery_kind is not None:
            return delivery_result.delivery_kind
        return "no_visible_response"

    def _response_envelope_for_request(
        self,
        request: ResponseRequest,
        *,
        resolved_target: MessageTarget,
        requester_id: str | None = None,
        sender_id: str | None = None,
    ) -> MessageEnvelope:
        """Resolve the hook envelope for one response request."""
        if request.response_envelope is not None:
            return request.response_envelope
        resolved_requester_id = (
            requester_id if requester_id is not None else request.user_id or self.deps.matrix_full_id
        )
        resolved_sender_id = sender_id if sender_id is not None else request.user_id or self.deps.matrix_full_id
        return MessageEnvelope(
            source_event_id=request.reply_to_event_id,
            room_id=request.room_id,
            target=resolved_target,
            requester_id=resolved_requester_id,
            sender_id=resolved_sender_id,
            body=request.prompt,
            attachment_ids=tuple(request.attachment_ids or ()),
            mentioned_agents=(),
            agent_name=self.deps.agent_name,
            source_kind="message",
        )

    def _correlation_id_for_request(self, request: ResponseRequest) -> str:
        """Resolve the correlation id for one request."""
        return request.correlation_id or request.reply_to_event_id

    def _build_lifecycle(
        self,
        *,
        response_kind: str,
        request: ResponseRequest,
        response_envelope: MessageEnvelope | None = None,
        correlation_id: str | None = None,
    ) -> ResponseLifecycle:
        """Build one lifecycle helper with the resolved shared response context."""
        resolved_response_envelope = response_envelope
        if resolved_response_envelope is None:
            assert request.target is not None
            resolved_response_envelope = self._response_envelope_for_request(
                request,
                resolved_target=request.target,
            )
        return ResponseLifecycle(
            self,
            response_kind=response_kind,
            request=request,
            response_envelope=resolved_response_envelope,
            correlation_id=correlation_id or self._correlation_id_for_request(request),
        )

    def _is_cancelled_delivery_result(self, delivery_result: DeliveryResult | None) -> bool:
        """Return whether one terminal delivery outcome never produced a final visible response."""
        if delivery_result is None:
            return True
        return (
            not delivery_result.suppressed
            and delivery_result.event_id is None
            and delivery_result.delivery_kind is None
        )

    async def _ensure_request_knowledge_managers(
        self,
        agent_names: list[str],
        execution_identity: ToolExecutionIdentity | None,
    ) -> dict[str, Any]:
        """Ensure request-scoped knowledge managers for one response execution."""
        try:
            return await ensure_request_knowledge_managers(
                agent_names,
                config=self.deps.runtime.config,
                runtime_paths=self.deps.runtime_paths,
                execution_identity=execution_identity,
            )
        except Exception:
            self.deps.logger.exception(
                "Failed to initialize request-scoped knowledge managers",
                agent_names=agent_names,
            )
            return {}

    async def generate_team_response_helper(
        self,
        request: ResponseRequest,
        *,
        team_agents: list[MatrixID],
        team_mode: str,
        reason_prefix: str = "Team request",
    ) -> str | None:
        """Generate a team response with lifecycle locking and queued-message state."""
        team_request = TeamResponseRequest(
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

    async def generate_team_response_helper_locked(  # noqa: C901, PLR0915
        self,
        team_request: TeamResponseRequest,
        *,
        resolved_target: MessageTarget,
    ) -> str | None:
        """Generate a team response once the per-thread lifecycle lock is held."""
        request = team_request.request
        if request.on_lifecycle_lock_acquired is not None:
            request.on_lifecycle_lock_acquired()
        request = await self._prepare_request_after_lock(request)
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
        agent_names = [
            mid.agent_name(self.deps.runtime.config, self.deps.runtime_paths) or mid.username
            for mid in team_request.team_agents
        ]
        self.deps.runtime.config.assert_team_agents_supported(
            [agent_name for agent_name in agent_names if agent_name != ROUTER_AGENT_NAME],
        )
        include_matrix_prompt_context = any(
            _agent_has_matrix_messaging_tool(self.deps.runtime.config, name) for name in agent_names
        )
        response_thread_id = resolved_target.resolved_thread_id
        resolved_target = resolved_target.with_thread_root(response_thread_id)
        model_message = _append_matrix_prompt_context(
            prepared_prompt,
            target=resolved_target,
            include_context=include_matrix_prompt_context,
        )
        resolved_request = replace(
            request,
            target=resolved_target,
            thread_history=model_thread_history,
            media=request.media or MediaInputs(),
        )
        resolved_response_envelope = self._response_envelope_for_request(
            request,
            resolved_target=resolved_target,
            requester_id=requester_user_id,
            sender_id=requester_user_id,
        )
        resolved_correlation_id = self._correlation_id_for_request(request)
        lifecycle = self._build_lifecycle(
            response_kind="team",
            request=request,
            response_envelope=resolved_response_envelope,
            correlation_id=resolved_correlation_id,
        )
        delivery_target = (
            resolved_target
            if request.existing_event_id is None or request.existing_event_is_placeholder
            else resolved_target.with_thread_root(request.thread_id)
        )
        delivery_request_base = replace(resolved_request, target=delivery_target)
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
        session_scope = self.deps.state_writer.team_history_scope(list(team_request.team_agents))
        session_type = self.deps.state_writer.session_type_for_scope(session_scope)

        def team_storage_factory() -> SqliteDb:
            return self.deps.state_writer.create_storage(tool_dispatch.execution_identity, scope=session_scope)

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
        delivery_result: DeliveryResult | None = None
        compaction_outcomes: list[CompactionOutcome] = []
        tracked_event_id: str | None = request.existing_event_id
        delivery_stage_started = False
        delivery_failure_reason: str | None = None
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
            nonlocal delivery_result, tracked_event_id, delivery_stage_started
            delivery_request = self._request_for_delivery(delivery_request_base, message_id=message_id)
            delivery_target = delivery_request.target
            if delivery_target is None:
                msg = "Team response delivery target was not resolved"
                raise RuntimeError(msg)
            if message_id is not None:
                tracked_event_id = message_id
                team_turn_recorder.set_response_event_id(message_id)

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

                    def build_response_stream() -> AsyncIterator[object]:
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
                            active_event_ids=active_event_ids,
                            response_sender_id=self.deps.matrix_full_id,
                            compaction_outcomes_collector=compaction_outcomes,
                            configured_team_name=self.deps.agent_name
                            if self.deps.agent_name in self.deps.runtime.config.teams
                            else None,
                            system_enrichment_items=request.system_enrichment_items,
                            reason_prefix=team_request.reason_prefix,
                            matrix_run_metadata=matrix_run_metadata,
                            turn_recorder=team_turn_recorder,
                        )

                    response_stream = self._stream_in_tool_context(
                        tool_dispatch=tool_dispatch,
                        stream_factory=build_response_stream,
                    )

                    try:
                        delivery_stage_started = True
                        event_id, accumulated = await self.deps.delivery_gateway.deliver_stream(
                            StreamingDeliveryRequest(
                                target=delivery_target,
                                response_stream=response_stream,
                                existing_event_id=delivery_request.existing_event_id,
                                adopt_existing_placeholder=delivery_request.existing_event_id is not None
                                and delivery_request.existing_event_is_placeholder,
                                header=None,
                                show_tool_calls=show_tool_calls,
                                streaming_cls=ReplacementStreamingResponse,
                                pipeline_timing=request.pipeline_timing,
                                visible_event_id_callback=_note_visible_response_event_id,
                            ),
                        )
                        if event_id is not None:
                            tracked_event_id = event_id
                    except asyncio.CancelledError:
                        self._persist_interrupted_recorder(
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
                if event_id is None:
                    delivery_result = DeliveryResult(
                        event_id=None,
                        response_text=accumulated,
                        delivery_kind=None,
                    )
                    return

                delivery_kind: Literal["sent", "edited"] = "edited" if message_id else "sent"
                try:
                    delivery_result = await self.deps.delivery_gateway.finalize_streamed_response(
                        FinalizeStreamedResponseRequest(
                            target=delivery_target,
                            streamed_event_id=event_id,
                            streamed_text=accumulated,
                            delivery_kind=delivery_kind,
                            response_kind="team",
                            response_envelope=resolved_response_envelope,
                            correlation_id=resolved_correlation_id,
                            tool_trace=None,
                            extra_content=None,
                            cleanup_suppressed_streamed_event=(
                                delivery_request.existing_event_is_placeholder
                                or delivery_request.existing_event_id is None
                            ),
                        ),
                    )
                except asyncio.CancelledError:
                    self._record_stream_delivery_error(
                        recorder=team_turn_recorder,
                        accumulated_text=accumulated,
                        tool_trace=[],
                    )
                    self._persist_interrupted_recorder(
                        recorder=team_turn_recorder,
                        session_scope=session_scope,
                        session_id=session_id,
                        execution_identity=tool_dispatch.execution_identity,
                        run_id=response_run_id,
                        is_team=True,
                        response_event_id=event_id or tracked_event_id,
                    )
                    raise
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
                                    active_event_ids=active_event_ids,
                                    response_sender_id=self.deps.matrix_full_id,
                                    compaction_outcomes_collector=compaction_outcomes,
                                    configured_team_name=self.deps.agent_name
                                    if self.deps.agent_name in self.deps.runtime.config.teams
                                    else None,
                                    system_enrichment_items=request.system_enrichment_items,
                                    reason_prefix=team_request.reason_prefix,
                                    matrix_run_metadata=matrix_run_metadata,
                                    turn_recorder=team_turn_recorder,
                                )

                            try:
                                response_text = await self._run_in_tool_context(
                                    tool_dispatch=tool_dispatch,
                                    operation=build_response_text,
                                )
                            except asyncio.CancelledError:
                                self._persist_interrupted_recorder(
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
                    restart = is_sync_restart_cancel(exc)
                    if restart:
                        self.deps.logger.info(
                            "Team non-streaming response interrupted by sync restart",
                            message_id=message_id,
                        )
                    else:
                        self.deps.logger.warning(
                            "Team non-streaming response cancelled — traceback for diagnosis",
                            message_id=message_id,
                            exc_info=True,
                        )
                    if message_id:
                        cancelled_text, extra_content = self._cancelled_response_update(restart=restart)
                        await self.deps.delivery_gateway.edit_text(
                            EditTextRequest(
                                target=delivery_target,
                                event_id=message_id,
                                new_text=cancelled_text,
                                extra_content=extra_content,
                            ),
                        )
                    raise

                delivery_stage_started = True
                try:
                    delivery_result = await self.deps.delivery_gateway.deliver_final(
                        FinalDeliveryRequest(
                            target=delivery_target,
                            existing_event_id=message_id,
                            existing_event_is_placeholder=delivery_request.existing_event_is_placeholder,
                            response_text=response_text,
                            response_kind="team",
                            response_envelope=resolved_response_envelope,
                            correlation_id=resolved_correlation_id,
                            tool_trace=None,
                            extra_content=None,
                        ),
                    )
                except asyncio.CancelledError:
                    self._persist_interrupted_recorder(
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
        if not request.existing_event_id:
            thinking_msg = "🤝 Team Response: Thinking..."

        try:
            run_message_id = await self.run_cancellable_response(
                room_id=request.room_id,
                reply_to_event_id=request.reply_to_event_id,
                thread_id=request.thread_id,
                target=delivery_target,
                response_function=generate_team_response,
                thinking_message=thinking_msg,
                existing_event_id=request.existing_event_id,
                user_id=requester_user_id,
                run_id=response_run_id,
                pipeline_timing=request.pipeline_timing,
            )
            if tracked_event_id is None:
                tracked_event_id = run_message_id
        except StreamingDeliveryError as error:
            self.deps.logger.exception("Error in team streaming response", error=str(error.error))
            delivery_failure_reason = str(error.error)
            if error.event_id is not None:
                tracked_event_id = error.event_id
            if self._record_stream_delivery_error(
                recorder=team_turn_recorder,
                accumulated_text=error.accumulated_text,
                tool_trace=error.tool_trace,
            ):
                self._persist_interrupted_recorder(
                    recorder=team_turn_recorder,
                    session_scope=session_scope,
                    session_id=session_id,
                    execution_identity=tool_dispatch.execution_identity,
                    run_id=response_run_id,
                    is_team=True,
                    response_event_id=tracked_event_id,
                )
            delivery_kind: Literal["sent", "edited"] | None = None
            if error.event_id is not None:
                delivery_kind = "edited" if request.existing_event_id else "sent"
            delivery_result = DeliveryResult(
                event_id=error.event_id,
                response_text=error.accumulated_text,
                delivery_kind=delivery_kind,
                failure_reason=str(error.error),
            )
        except Exception as error:
            if not delivery_stage_started:
                raise
            delivery_failure_reason = str(error)
            self._log_delivery_failure(response_kind="team", error=error)
        return await lifecycle.finalize(
            DeliveryOutcome(
                delivery_result=delivery_result,
                delivery_failure_reason=delivery_failure_reason,
                tracked_event_id=tracked_event_id,
            ),
            build_post_response_outcome=lambda resolved_event_id: ResponseOutcome(
                resolved_event_id=resolved_event_id,
                delivery_result=delivery_result,
                response_run_id=response_run_id,
                session_id=session_id,
                session_type=SessionType.TEAM,
                execution_identity=tool_dispatch.execution_identity,
                compaction_outcomes=tuple(compaction_outcomes),
                interactive_target=resolved_target,
                thread_summary_room_id=(request.room_id if resolved_target.resolved_thread_id is not None else None),
                thread_summary_thread_id=resolved_target.resolved_thread_id,
                thread_summary_message_count_hint=thread_summary_message_count_hint(request.thread_history),
                dispatch_compaction_when_suppressed=True,
            ),
            post_response_deps=lambda: self.deps.post_response_effects.build_deps(
                room_id=request.room_id,
                reply_to_event_id=request.reply_to_event_id,
                thread_id=resolved_target.resolved_thread_id,
                interactive_agent_name=self.deps.agent_name,
                persist_response_event_id=persist_response_event_id,
            ),
        )

    async def run_cancellable_response(
        self,
        *,
        room_id: str,
        reply_to_event_id: str,
        thread_id: str | None,
        response_function: Callable[[str | None], Coroutine[Any, Any, None]],
        thinking_message: str | None = None,
        existing_event_id: str | None = None,
        user_id: str | None = None,
        run_id: str | None = None,
        target: MessageTarget | None = None,
        pipeline_timing: DispatchPipelineTiming | None = None,
    ) -> str | None:
        """Run one response generation function with cancellation support."""
        resolved_target = target or self.deps.resolver.build_message_target(
            room_id=room_id,
            thread_id=thread_id,
            reply_to_event_id=reply_to_event_id,
        )

        assert not (thinking_message and existing_event_id), (
            "thinking_message and existing_event_id are mutually exclusive"
        )

        try:
            self.in_flight_response_count += 1
            with bound_log_context(**resolved_target.log_context):
                initial_message_id = None
                if thinking_message:
                    assert not existing_event_id
                    initial_message_id = await self.deps.delivery_gateway.send_text(
                        SendTextRequest(
                            target=resolved_target,
                            response_text=thinking_message,
                            extra_content={STREAM_STATUS_KEY: STREAM_STATUS_PENDING},
                        ),
                    )
                    if initial_message_id is not None and pipeline_timing is not None:
                        pipeline_timing.mark("placeholder_sent")
                        pipeline_timing.mark_first_visible_reply("placeholder")

                message_id = existing_event_id or initial_message_id
                task: asyncio.Task[None] = asyncio.create_task(response_function(message_id))

                message_to_track = existing_event_id or initial_message_id
                tracked_message_id = message_to_track or f"__pending_response__:{id(task)}"
                show_stop_button = False

                self.deps.stop_manager.set_current(
                    tracked_message_id,
                    resolved_target,
                    task,
                    None,
                    run_id=run_id,
                )

                if message_to_track:
                    show_stop_button = self.deps.runtime.config.defaults.show_stop_button
                    if show_stop_button and user_id:
                        user_is_online = await is_user_online(
                            self._client(),
                            user_id,
                            room_id=room_id,
                        )
                        show_stop_button = user_is_online
                        self.deps.logger.info(
                            "Stop button decision",
                            message_id=message_to_track,
                            user_online=user_is_online,
                            show_button=show_stop_button,
                        )

                    if show_stop_button:
                        self.deps.logger.info("Adding stop button", message_id=message_to_track)
                        await self.deps.stop_manager.add_stop_button(
                            self._client(),
                            message_to_track,
                            notify_outbound_event=self.deps.resolver.deps.conversation_cache.notify_outbound_event,
                        )

                try:
                    await task
                except asyncio.CancelledError as exc:
                    if is_sync_restart_cancel(exc):
                        self.deps.logger.info(
                            "Response interrupted by sync restart",
                            message_id=message_to_track or tracked_message_id,
                        )
                    else:
                        self.deps.logger.warning(
                            "Response cancelled — traceback for diagnosis",
                            message_id=message_to_track or tracked_message_id,
                            exc_info=True,
                        )
                except Exception as error:
                    self.deps.logger.exception("Error during response generation", error=str(error))
                    raise
                finally:
                    tracked = self.deps.stop_manager.tracked_messages.get(tracked_message_id)
                    button_already_removed = tracked is None or tracked.reaction_event_id is None
                    self.deps.stop_manager.clear_message(
                        tracked_message_id,
                        client=self._client(),
                        remove_button=show_stop_button and not button_already_removed,
                        notify_outbound_redaction=(
                            self.deps.post_response_effects.conversation_cache.notify_outbound_redaction
                        ),
                    )

                return message_id
        finally:
            self.in_flight_response_count -= 1

    async def _prepare_response_runtime_common(
        self,
        request: ResponseRequest,
        *,
        existing_event_uses_thread_id: bool,
        room_mode: bool,
    ) -> _PreparedResponseRuntime:
        resolved_target = self._resolve_request_target(request)
        response_thread_id = (
            resolved_target.resolved_thread_id
            if request.target is not None
            else request.thread_id
            if request.existing_event_id is not None and existing_event_uses_thread_id
            else self.deps.resolver.resolve_response_thread_root(
                request.thread_id,
                request.reply_to_event_id,
                room_id=request.room_id,
                response_envelope=request.response_envelope,
            )
        )
        resolved_target = resolved_target.with_thread_root(response_thread_id)
        media_inputs = request.media or MediaInputs()
        session_id = resolved_target.session_id
        resolved_model_prompt = _append_matrix_prompt_context(
            request.model_prompt or request.prompt,
            target=resolved_target,
            include_context=_agent_has_matrix_messaging_tool(self.deps.runtime.config, self.deps.agent_name),
        )
        tool_dispatch = self.deps.tool_runtime.build_dispatch_context(
            resolved_target,
            user_id=request.user_id,
            session_id=session_id,
            attachment_ids=request.attachment_ids,
            correlation_id=request.correlation_id,
            source_envelope=request.response_envelope,
        )
        request_knowledge_managers = await self._ensure_request_knowledge_managers(
            [self.deps.agent_name],
            tool_dispatch.execution_identity,
        )
        return _PreparedResponseRuntime(
            resolved_target=resolved_target,
            response_thread_id=response_thread_id,
            media_inputs=media_inputs,
            session_id=session_id,
            model_prompt=resolved_model_prompt,
            tool_dispatch=tool_dispatch,
            request_knowledge_managers=request_knowledge_managers,
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
        pipeline_timing: DispatchPipelineTiming | None = None,
    ) -> str:
        """Run one non-streaming AI request."""

        def note_attempt_run_id(current_run_id: str) -> None:
            self.deps.stop_manager.update_run_id(request.existing_event_id, current_run_id)
            turn_recorder.set_run_id(current_run_id)

        async def build_response_text() -> str:
            knowledge = self.deps.knowledge_access.for_agent(
                self.deps.agent_name,
                request_knowledge_managers=runtime.request_knowledge_managers,
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
                thread_id=runtime.resolved_target.resolved_thread_id,
                room_id=request.room_id,
                knowledge=knowledge,
                user_id=request.user_id,
                run_id=run_id,
                run_id_callback=note_attempt_run_id,
                media=runtime.media_inputs,
                reply_to_event_id=request.reply_to_event_id,
                active_event_ids=active_event_ids,
                show_tool_calls=self._show_tool_calls(),
                tool_trace_collector=tool_trace,
                run_metadata_collector=run_metadata_content,
                execution_identity=runtime.tool_dispatch.execution_identity,
                compaction_outcomes_collector=compaction_outcomes,
                matrix_run_metadata=matrix_run_metadata,
                system_enrichment_items=request.system_enrichment_items,
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
            self._persist_interrupted_recorder(
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
        pipeline_timing: DispatchPipelineTiming | None = None,
    ) -> tuple[str | None, str]:
        """Run one streaming AI request and send the streamed Matrix response."""

        def note_attempt_run_id(current_run_id: str) -> None:
            self.deps.stop_manager.update_run_id(request.existing_event_id, current_run_id)
            turn_recorder.set_run_id(current_run_id)

        def note_visible_response_event_id(response_event_id: str) -> None:
            turn_recorder.set_response_event_id(response_event_id)

        knowledge = self.deps.knowledge_access.for_agent(
            self.deps.agent_name,
            request_knowledge_managers=runtime.request_knowledge_managers,
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
            thread_id=runtime.resolved_target.resolved_thread_id,
            room_id=request.room_id,
            knowledge=knowledge,
            user_id=request.user_id,
            run_id=run_id,
            run_id_callback=note_attempt_run_id,
            media=runtime.media_inputs,
            reply_to_event_id=request.reply_to_event_id,
            active_event_ids=active_event_ids,
            show_tool_calls=self._show_tool_calls(),
            run_metadata_collector=run_metadata_content,
            execution_identity=runtime.tool_dispatch.execution_identity,
            compaction_outcomes_collector=compaction_outcomes,
            matrix_run_metadata=matrix_run_metadata,
            system_enrichment_items=request.system_enrichment_items,
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
                event_id, accumulated = await self.deps.delivery_gateway.deliver_stream(
                    StreamingDeliveryRequest(
                        target=runtime.resolved_target,
                        response_stream=wrapped_response_stream,
                        existing_event_id=request.existing_event_id,
                        adopt_existing_placeholder=request.existing_event_id is not None
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
                return event_id, accumulated
        except asyncio.CancelledError:
            self._persist_interrupted_recorder(
                recorder=turn_recorder,
                session_scope=self.deps.state_writer.history_scope(),
                session_id=runtime.session_id,
                execution_identity=runtime.tool_dispatch.execution_identity,
                run_id=run_id,
                is_team=False,
                response_event_id=request.existing_event_id,
            )
            raise

    async def process_and_respond(  # noqa: C901, PLR0912, PLR0915
        self,
        request: ResponseRequest,
        *,
        run_id: str | None = None,
        response_kind: str = "ai",
        compaction_outcomes_collector: list[CompactionOutcome] | None = None,
        on_delivery_started: Callable[[str | None], None] | None = None,
    ) -> DeliveryResult:
        """Process a message and send a response without streaming."""
        if not request.prompt.strip():
            return DeliveryResult(event_id=request.existing_event_id, response_text="", delivery_kind=None)

        if request.pipeline_timing is not None:
            request.pipeline_timing.mark("response_runtime_start")
        runtime = await self.prepare_non_streaming_runtime(request)
        if request.pipeline_timing is not None:
            request.pipeline_timing.mark("response_runtime_ready")
        response_envelope = self._response_envelope_for_request(
            request,
            resolved_target=runtime.resolved_target,
        )
        lifecycle = self._build_lifecycle(
            response_kind=response_kind,
            request=request,
            response_envelope=response_envelope,
        )
        session_scope = self.deps.state_writer.history_scope()
        session_type = self.deps.state_writer.session_type_for_scope(session_scope)

        def history_storage_factory() -> SqliteDb:
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
        active_event_ids = self._active_response_event_ids(request.room_id)
        turn_recorder = self._build_turn_recorder(
            user_message=request.prompt,
            reply_to_event_id=request.reply_to_event_id,
            matrix_run_metadata=_materialize_matrix_run_metadata(request.matrix_run_metadata),
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
                    pipeline_timing=request.pipeline_timing,
                )
            finally:
                await lifecycle.emit_session_started(session_started_watch)
        except asyncio.CancelledError as exc:
            restart = is_sync_restart_cancel(exc)
            if restart:
                self.deps.logger.info(
                    "Non-streaming response interrupted by sync restart",
                    message_id=request.existing_event_id,
                )
            else:
                self.deps.logger.warning(
                    "Non-streaming response cancelled — traceback for diagnosis",
                    message_id=request.existing_event_id,
                    exc_info=True,
                )
            if request.existing_event_id:
                cancelled_text, extra_content = self._cancelled_response_update(restart=restart)
                await self.deps.delivery_gateway.edit_text(
                    EditTextRequest(
                        target=runtime.resolved_target,
                        event_id=request.existing_event_id,
                        new_text=cancelled_text,
                        extra_content=extra_content,
                    ),
                )
            raise
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
                    response_envelope=self._response_envelope_for_request(
                        request,
                        resolved_target=runtime.resolved_target,
                    ),
                    correlation_id=self._correlation_id_for_request(request),
                    tool_trace=tool_trace if self._show_tool_calls() else None,
                    extra_content=response_extra_content or None,
                ),
            )
        except asyncio.CancelledError:
            self._persist_interrupted_recorder(
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
        if compaction_outcomes_collector is not None:
            compaction_outcomes_collector.extend(compaction_outcomes)
        return delivery

    async def process_and_respond_streaming(  # noqa: C901, PLR0912, PLR0915
        self,
        request: ResponseRequest,
        *,
        run_id: str | None = None,
        response_kind: str = "ai",
        compaction_outcomes_collector: list[CompactionOutcome] | None = None,
        on_delivery_started: Callable[[str | None], None] | None = None,
    ) -> DeliveryResult:
        """Process a message and send a streamed response."""
        if not request.prompt.strip():
            return DeliveryResult(event_id=request.existing_event_id, response_text="", delivery_kind=None)

        if request.pipeline_timing is not None:
            request.pipeline_timing.mark("response_runtime_start")
        runtime = await self.prepare_streaming_runtime(request)
        if request.pipeline_timing is not None:
            request.pipeline_timing.mark("response_runtime_ready")
        response_envelope = self._response_envelope_for_request(
            request,
            resolved_target=runtime.resolved_target,
        )
        lifecycle = self._build_lifecycle(
            response_kind=response_kind,
            request=request,
            response_envelope=response_envelope,
        )
        session_scope = self.deps.state_writer.history_scope()
        session_type = self.deps.state_writer.session_type_for_scope(session_scope)

        def history_storage_factory() -> SqliteDb:
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
        active_event_ids = self._active_response_event_ids(request.room_id)
        tool_trace: list[Any] = []
        turn_recorder = self._build_turn_recorder(
            user_message=request.prompt,
            reply_to_event_id=request.reply_to_event_id,
            matrix_run_metadata=_materialize_matrix_run_metadata(request.matrix_run_metadata),
        )

        try:
            try:
                event_id, accumulated = await self.generate_streaming_ai_response(
                    request,
                    run_id=run_id,
                    runtime=runtime,
                    active_event_ids=active_event_ids,
                    turn_recorder=turn_recorder,
                    tool_trace=tool_trace,
                    run_metadata_content=run_metadata_content,
                    compaction_outcomes=compaction_outcomes,
                    pipeline_timing=request.pipeline_timing,
                )
            finally:
                await lifecycle.emit_session_started(session_started_watch)
        except StreamingDeliveryError as error:
            self.deps.logger.exception("Error in streaming response", error=str(error.error))
            tool_trace[:] = error.tool_trace
            if self._record_stream_delivery_error(
                recorder=turn_recorder,
                accumulated_text=error.accumulated_text,
                tool_trace=error.tool_trace,
            ):
                self._persist_interrupted_recorder(
                    recorder=turn_recorder,
                    session_scope=session_scope,
                    session_id=runtime.session_id,
                    execution_identity=runtime.tool_dispatch.execution_identity,
                    run_id=run_id,
                    is_team=False,
                    response_event_id=error.event_id,
                )
            if compaction_outcomes_collector is not None:
                compaction_outcomes_collector.extend(compaction_outcomes)
            delivery_kind: Literal["sent", "edited"] | None = None
            if error.event_id is not None:
                delivery_kind = "edited" if request.existing_event_id else "sent"
            return DeliveryResult(
                event_id=error.event_id,
                response_text=error.accumulated_text,
                delivery_kind=delivery_kind,
                failure_reason=str(error.error),
            )
        except asyncio.CancelledError as exc:
            if is_sync_restart_cancel(exc):
                self.deps.logger.info(
                    "Bot streaming response interrupted by sync restart",
                    message_id=request.existing_event_id,
                )
            else:
                self.deps.logger.warning(
                    "Bot streaming response cancelled — traceback for diagnosis",
                    message_id=request.existing_event_id,
                    exc_info=True,
                )
            raise
        except Exception as error:
            self.deps.logger.exception("Error in streaming response", error=str(error))
            return DeliveryResult(
                event_id=None,
                response_text="",
                delivery_kind=None,
                failure_reason=str(error),
            )

        if event_id is None:
            if compaction_outcomes_collector is not None:
                compaction_outcomes_collector.extend(compaction_outcomes)
            return DeliveryResult(event_id=None, response_text=accumulated, delivery_kind=None)

        response_extra_content = _merge_response_extra_content(
            run_metadata_content,
            request.attachment_ids,
        )
        delivery_kind: Literal["sent", "edited"] = "edited" if request.existing_event_id else "sent"
        if request.response_envelope is None or request.correlation_id is None:
            interactive_response = interactive.parse_and_format_interactive(
                accumulated,
                extract_mapping=True,
            )
            if compaction_outcomes_collector is not None:
                compaction_outcomes_collector.extend(compaction_outcomes)
            if request.pipeline_timing is not None:
                request.pipeline_timing.mark_first_visible_reply("final")
                request.pipeline_timing.mark("response_complete")
            return DeliveryResult(
                event_id=event_id,
                response_text=interactive_response.formatted_text,
                delivery_kind=delivery_kind,
                option_map=interactive_response.option_map,
                options_list=interactive_response.options_list,
            )

        if on_delivery_started is not None:
            on_delivery_started(event_id)
        try:
            delivery = await self.deps.delivery_gateway.finalize_streamed_response(
                FinalizeStreamedResponseRequest(
                    target=runtime.resolved_target,
                    streamed_event_id=event_id,
                    streamed_text=accumulated,
                    delivery_kind=delivery_kind,
                    response_kind=response_kind,
                    response_envelope=request.response_envelope,
                    correlation_id=request.correlation_id,
                    tool_trace=tool_trace if self._show_tool_calls() else None,
                    extra_content=response_extra_content,
                    cleanup_suppressed_streamed_event=(
                        request.existing_event_is_placeholder or request.existing_event_id is None
                    ),
                ),
            )
        except asyncio.CancelledError:
            self._record_stream_delivery_error(
                recorder=turn_recorder,
                accumulated_text=accumulated,
                tool_trace=tool_trace,
            )
            self._persist_interrupted_recorder(
                recorder=turn_recorder,
                session_scope=session_scope,
                session_id=runtime.session_id,
                execution_identity=runtime.tool_dispatch.execution_identity,
                run_id=run_id,
                is_team=False,
                response_event_id=event_id,
            )
            raise
        if request.pipeline_timing is not None:
            request.pipeline_timing.mark_first_visible_reply("final")
            request.pipeline_timing.mark("response_complete")

        if compaction_outcomes_collector is not None:
            compaction_outcomes_collector.extend(compaction_outcomes)
        return delivery

    async def send_skill_command_response(
        self,
        *,
        room_id: str,
        reply_to_event_id: str,
        thread_id: str | None,
        thread_history: Sequence[ResolvedVisibleMessage],
        prompt: str,
        agent_name: str,
        user_id: str | None,
        reply_to_event: _ReplyEventWithSource | None = None,
        source_envelope: MessageEnvelope | None = None,
    ) -> str | None:
        """Send a skill command response using a specific agent."""
        target = self.deps.resolver.build_message_target(
            room_id=room_id,
            thread_id=thread_id,
            reply_to_event_id=reply_to_event_id,
        )
        lifecycle_lock = self._response_lifecycle_lock(target)
        async with lifecycle_lock:
            return await self.send_skill_command_response_locked(
                room_id=room_id,
                reply_to_event_id=reply_to_event_id,
                thread_id=thread_id,
                thread_history=thread_history,
                prompt=prompt,
                agent_name=agent_name,
                user_id=user_id,
                reply_to_event=reply_to_event,
                source_envelope=source_envelope,
            )

    async def send_skill_command_response_locked(
        self,
        *,
        room_id: str,
        reply_to_event_id: str,
        thread_id: str | None,
        thread_history: Sequence[ResolvedVisibleMessage],
        prompt: str,
        agent_name: str,
        user_id: str | None,
        reply_to_event: _ReplyEventWithSource | None = None,
        source_envelope: MessageEnvelope | None = None,
    ) -> str | None:
        """Send a skill command response after acquiring the per-thread lock."""
        if not prompt.strip():
            return None
        memory_prompt, memory_thread_history, model_prompt, thread_history = prepare_memory_and_model_context(
            prompt,
            thread_history,
            config=self.deps.runtime.config,
            runtime_paths=self.deps.runtime_paths,
        )

        resolved_target = self.deps.resolver.build_message_target(
            room_id=room_id,
            thread_id=thread_id,
            reply_to_event_id=reply_to_event_id,
            event_source=reply_to_event.source if reply_to_event is not None else None,
        )
        session_id = resolved_target.session_id
        model_prompt = _append_matrix_prompt_context(
            model_prompt,
            target=resolved_target,
            include_context=_agent_has_matrix_messaging_tool(self.deps.runtime.config, agent_name),
        )
        tool_dispatch = self.deps.tool_runtime.build_dispatch_context(
            resolved_target,
            user_id=user_id,
            session_id=session_id,
            agent_name=agent_name,
            source_envelope=source_envelope,
        )
        skill_request = ResponseRequest(
            room_id=room_id,
            reply_to_event_id=reply_to_event_id,
            thread_id=thread_id,
            thread_history=memory_thread_history,
            prompt=memory_prompt,
            model_prompt=model_prompt,
            user_id=user_id,
            response_envelope=source_envelope,
            correlation_id=reply_to_event_id,
            target=resolved_target,
        )
        lifecycle = self._build_lifecycle(
            response_kind="skill_command",
            request=skill_request,
        )
        session_scope = HistoryScope(kind="agent", scope_id=agent_name)

        def history_storage_factory() -> SqliteDb:
            return self.deps.state_writer.create_storage(tool_dispatch.execution_identity, scope=session_scope)

        session_started_watch = lifecycle.setup_session_watch(
            tool_context=runtime_context_from_dispatch_context(tool_dispatch),
            session_id=session_id,
            session_type=self.deps.state_writer.session_type_for_scope(session_scope),
            scope=session_scope,
            room_id=room_id,
            thread_id=resolved_target.resolved_thread_id,
            create_storage=history_storage_factory,
        )
        request_knowledge_managers = await self._ensure_request_knowledge_managers(
            [agent_name],
            tool_dispatch.execution_identity,
        )
        reprioritize_auto_flush_sessions(
            self.deps.storage_path,
            self.deps.runtime.config,
            agent_name=agent_name,
            active_session_id=session_id,
            execution_identity=tool_dispatch.execution_identity,
        )
        show_tool_calls = self._show_tool_calls(agent_name)
        tool_trace: list[Any] = []
        run_metadata_content: dict[str, Any] = {}
        active_event_ids = self._active_response_event_ids(room_id)
        async with typing_indicator(self._client(), room_id):

            async def build_response_text() -> str:
                knowledge = self.deps.knowledge_access.for_agent(
                    agent_name,
                    request_knowledge_managers=request_knowledge_managers,
                )
                return await ai_response(
                    agent_name=agent_name,
                    prompt=memory_prompt,
                    session_id=session_id,
                    runtime_paths=self.deps.runtime_paths,
                    config=self.deps.runtime.config,
                    thread_history=thread_history,
                    model_prompt=model_prompt,
                    thread_id=resolved_target.resolved_thread_id,
                    room_id=room_id,
                    knowledge=knowledge,
                    user_id=user_id,
                    reply_to_event_id=reply_to_event_id,
                    active_event_ids=active_event_ids,
                    show_tool_calls=show_tool_calls,
                    tool_trace_collector=tool_trace,
                    run_metadata_collector=run_metadata_content,
                    execution_identity=tool_dispatch.execution_identity,
                )

            try:
                response_text = await self._run_in_tool_context(
                    tool_dispatch=tool_dispatch,
                    operation=build_response_text,
                )
            finally:
                await lifecycle.emit_session_started(session_started_watch)

        response = interactive.parse_and_format_interactive(response_text, extract_mapping=True)
        event_id = await self.deps.delivery_gateway.send_text(
            SendTextRequest(
                target=resolved_target,
                response_text=response.formatted_text,
                skip_mentions=True,
                tool_trace=tool_trace if show_tool_calls else None,
                extra_content=run_metadata_content or None,
            ),
        )

        def queue_memory_persistence() -> None:
            try:
                mark_auto_flush_dirty_session(
                    self.deps.storage_path,
                    self.deps.runtime.config,
                    agent_name=agent_name,
                    session_id=session_id,
                    execution_identity=tool_dispatch.execution_identity,
                )
                if self.deps.runtime.config.get_agent_memory_backend(agent_name) == "mem0":
                    create_background_task(
                        store_conversation_memory(
                            memory_prompt,
                            agent_name,
                            self.deps.storage_path,
                            session_id,
                            self.deps.runtime.config,
                            self.deps.runtime_paths,
                            memory_thread_history,
                            user_id,
                            execution_identity=tool_dispatch.execution_identity,
                        ),
                        name=f"memory_save_{agent_name}_{session_id}",
                        owner=self.deps.runtime,
                    )
            except Exception:  # pragma: no cover
                self.deps.logger.debug("Skipping memory storage due to configuration error")

        return await lifecycle.apply_effects_safely(
            resolved_event_id=event_id,
            post_response_outcome=lambda: ResponseOutcome(
                resolved_event_id=event_id,
                delivery_result=DeliveryResult(
                    event_id=event_id,
                    response_text=response.formatted_text,
                    delivery_kind="sent" if event_id is not None else None,
                    option_map=response.option_map,
                    options_list=response.options_list,
                ),
                session_id=session_id,
                session_type=SessionType.AGENT,
                execution_identity=tool_dispatch.execution_identity,
                interactive_target=resolved_target,
                memory_prompt=memory_prompt,
                memory_thread_history=memory_thread_history,
            ),
            post_response_deps=lambda: self.deps.post_response_effects.build_deps(
                room_id=room_id,
                reply_to_event_id=reply_to_event_id,
                thread_id=thread_id,
                interactive_agent_name=agent_name,
                queue_memory_persistence=queue_memory_persistence,
            ),
        )

    def resolve_response_event_id(
        self,
        *,
        delivery_result: DeliveryResult | None,
        tracked_event_id: str | None,
        existing_event_id: str | None,
        existing_event_is_placeholder: bool = False,
    ) -> str | None:
        """Resolve the final response event id across send, edit, and placeholder reuse."""
        if self._is_cancelled_delivery_result(delivery_result):
            return None
        assert delivery_result is not None
        if delivery_result.event_id is not None:
            return delivery_result.event_id
        if delivery_result.suppressed or existing_event_is_placeholder:
            return None
        return existing_event_id or tracked_event_id

    async def generate_response(self, request: ResponseRequest) -> str | None:
        """Generate and send/edit an agent response with lifecycle locking."""
        return await self._run_locked_response_lifecycle(
            request,
            locked_operation=lambda resolved_target: self.generate_response_locked(
                request,
                resolved_target=resolved_target,
            ),
        )

    async def generate_response_locked(  # noqa: C901, PLR0915
        self,
        request: ResponseRequest,
        *,
        resolved_target: MessageTarget,
    ) -> str | None:
        """Generate one agent response after acquiring the per-thread lock."""
        delivery_thread_id = resolved_target.resolved_thread_id
        resolved_target = resolved_target.with_thread_root(delivery_thread_id)
        if request.on_lifecycle_lock_acquired is not None:
            request.on_lifecycle_lock_acquired()
        request = await self._prepare_request_after_lock(request)
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
            target=resolved_target,
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
        delivery_result: DeliveryResult | None = None
        compaction_outcomes: list[CompactionOutcome] = []
        response_run_id = str(uuid4())
        tracked_event_id: str | None = request.existing_event_id
        delivery_stage_started = False
        delivery_failure_reason: str | None = None
        resolved_correlation_id = self._correlation_id_for_request(request)
        resolved_response_envelope = self._response_envelope_for_request(
            request,
            resolved_target=resolved_target,
        )
        lifecycle = self._build_lifecycle(
            response_kind="ai",
            request=request,
            response_envelope=resolved_response_envelope,
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
            if self.deps.runtime.config.get_agent_memory_backend(self.deps.agent_name) == "mem0":
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
            if event_id is not None:
                tracked_event_id = event_id

        async def generate(message_id: str | None) -> None:
            nonlocal delivery_result, tracked_event_id
            if message_id is not None:
                tracked_event_id = message_id
            delivery_request = self._request_for_delivery(normalized_request, message_id=message_id)
            if use_streaming:
                delivery_result = await self.process_and_respond_streaming(
                    delivery_request,
                    run_id=response_run_id,
                    compaction_outcomes_collector=compaction_outcomes,
                    on_delivery_started=note_delivery_started,
                )
            else:
                delivery_result = await self.process_and_respond(
                    delivery_request,
                    run_id=response_run_id,
                    compaction_outcomes_collector=compaction_outcomes,
                    on_delivery_started=note_delivery_started,
                )

        thinking_msg = None
        if not request.existing_event_id:
            thinking_msg = "Thinking..."

        try:
            run_message_id = await self.run_cancellable_response(
                room_id=request.room_id,
                reply_to_event_id=request.reply_to_event_id,
                thread_id=request.thread_id,
                target=resolved_target,
                response_function=generate,
                thinking_message=thinking_msg,
                existing_event_id=request.existing_event_id,
                user_id=request.user_id,
                run_id=response_run_id,
                pipeline_timing=request.pipeline_timing,
            )
            if tracked_event_id is None:
                tracked_event_id = run_message_id
        except Exception as error:
            if not delivery_stage_started:
                raise
            delivery_failure_reason = str(error)
            self._log_delivery_failure(response_kind="ai", error=error)
        return await lifecycle.finalize(
            DeliveryOutcome(
                delivery_result=delivery_result,
                delivery_failure_reason=delivery_failure_reason,
                tracked_event_id=tracked_event_id,
            ),
            build_post_response_outcome=lambda resolved_event_id: ResponseOutcome(
                resolved_event_id=resolved_event_id,
                delivery_result=delivery_result,
                response_run_id=response_run_id,
                session_id=session_id,
                session_type=self.deps.state_writer.session_type_for_scope(self.deps.state_writer.history_scope()),
                execution_identity=execution_identity,
                compaction_outcomes=tuple(compaction_outcomes),
                interactive_target=resolved_target,
                thread_summary_room_id=(request.room_id if resolved_target.resolved_thread_id is not None else None),
                thread_summary_thread_id=resolved_target.resolved_thread_id,
                thread_summary_message_count_hint=thread_summary_message_count_hint(request.thread_history),
                memory_prompt=memory_prompt,
                memory_thread_history=memory_thread_history,
            ),
            post_response_deps=lambda: self.deps.post_response_effects.build_deps(
                room_id=request.room_id,
                reply_to_event_id=request.reply_to_event_id,
                thread_id=resolved_target.resolved_thread_id,
                interactive_agent_name=self.deps.agent_name,
                queue_memory_persistence=queue_memory_persistence,
                persist_response_event_id=persist_response_event_id,
            ),
        )
