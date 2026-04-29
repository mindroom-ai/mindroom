"""Shared response lifecycle helpers."""

from __future__ import annotations

import asyncio
import enum
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypeVar, cast

from agno.db.base import SessionType

from mindroom.agent_storage import get_agent_session, get_team_session
from mindroom.ai_runtime import queued_message_signal_context
from mindroom.dispatch_source import is_automation_source_kind
from mindroom.hooks import (
    EVENT_SESSION_STARTED,
    SessionHookContext,
    emit,
)
from mindroom.post_response_effects import apply_post_response_effects
from mindroom.tool_system.runtime_context import resolve_tool_runtime_hook_bindings

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from agno.db.base import BaseDb
    from structlog.stdlib import BoundLogger

    from mindroom.delivery_gateway import ResponseHookService
    from mindroom.final_delivery import FinalDeliveryOutcome
    from mindroom.history import HistoryScope
    from mindroom.hooks import MessageEnvelope
    from mindroom.message_target import MessageTarget
    from mindroom.post_response_effects import PostResponseEffectsDeps, ResponseOutcome
    from mindroom.timing import DispatchPipelineTiming
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

_LockedResponseResult = TypeVar("_LockedResponseResult")


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


class _QueuedHumanNotice(enum.Enum):
    NONE = "none"
    WAITING = "waiting"


@dataclass(slots=True)
class QueuedHumanNoticeReservation:
    """Owned reservation for a queued-human notice created before dispatch starts."""

    _state: _QueuedMessageState
    _active: bool = True

    def _release_waiting_human_message(self) -> None:
        if not self._active:
            return
        self._state.consume_waiting_human_message()
        self._active = False

    def consume(self) -> None:
        """Mark the reservation as owned by the response lifecycle."""
        self._release_waiting_human_message()

    def cancel(self) -> None:
        """Release a reservation that will not reach response lifecycle ownership."""
        self._release_waiting_human_message()


@dataclass
class ResponseLifecycleCoordinator:
    """Serialize response turns and signal active turns about queued human ingress."""

    _response_lifecycle_locks: dict[tuple[str, str | None], asyncio.Lock] = field(default_factory=dict)
    _thread_queued_signals: dict[tuple[str, str | None], _QueuedMessageState] = field(default_factory=dict)

    @staticmethod
    def _thread_key(target: MessageTarget) -> tuple[str, str | None]:
        return (target.room_id, target.resolved_thread_id)

    def _has_active_response_for_thread_key(self, thread_key: tuple[str, str | None]) -> bool:
        queued_signal = self._thread_queued_signals.get(thread_key)
        if queued_signal is not None and queued_signal.has_active_response_turn():
            return True
        lifecycle_lock = self._response_lifecycle_locks.get(thread_key)
        return lifecycle_lock.locked() if lifecycle_lock is not None else False

    def has_active_response_for_target(self, target: MessageTarget) -> bool:
        """Return whether one canonical conversation target already has an active turn."""
        return self._has_active_response_for_thread_key(self._thread_key(target))

    def _response_lifecycle_lock(self, target: MessageTarget) -> asyncio.Lock:
        """Return the per-target lock that serializes one response lifecycle."""
        lock_key = self._thread_key(target)
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
        thread_key = self._thread_key(target)
        signal = self._thread_queued_signals.get(thread_key)
        if signal is not None:
            return signal
        signal = _QueuedMessageState()
        self._thread_queued_signals[thread_key] = signal
        return signal

    @staticmethod
    def _should_signal_queued_message(
        response_envelope: MessageEnvelope | None,
    ) -> bool:
        """Return whether one queued ingress should interrupt the active turn."""
        return response_envelope is not None and not is_automation_source_kind(response_envelope.source_kind)

    def reserve_waiting_human_message(
        self,
        *,
        target: MessageTarget,
        response_envelope: MessageEnvelope | None,
    ) -> QueuedHumanNoticeReservation | None:
        """Reserve an active-turn notice before queued dispatch owns the follow-up."""
        if response_envelope is None or not self._should_signal_queued_message(response_envelope):
            return None
        thread_key = self._thread_key(target)
        if not self._has_active_response_for_thread_key(thread_key):
            return None
        queued_signal = self._get_or_create_queued_signal(target)
        queued_signal.add_waiting_human_message()
        return QueuedHumanNoticeReservation(queued_signal)

    def _begin_response_turn_notice(
        self,
        *,
        lifecycle_lock: asyncio.Lock,
        queued_signal: _QueuedMessageState,
        response_envelope: MessageEnvelope | None,
        queued_notice_reservation: QueuedHumanNoticeReservation | None,
    ) -> _QueuedHumanNotice:
        existing_turn = queued_signal.begin_response_turn()
        if queued_notice_reservation is not None:
            return _QueuedHumanNotice.NONE
        if not (existing_turn or lifecycle_lock.locked()):
            return _QueuedHumanNotice.NONE
        if not self._should_signal_queued_message(response_envelope):
            return _QueuedHumanNotice.NONE
        queued_signal.add_waiting_human_message()
        return _QueuedHumanNotice.WAITING

    def _consume_queued_human_notice(
        self,
        *,
        notice: _QueuedHumanNotice,
        queued_signal: _QueuedMessageState,
    ) -> _QueuedHumanNotice:
        if notice is _QueuedHumanNotice.NONE:
            return notice
        queued_signal.consume_waiting_human_message()
        return _QueuedHumanNotice.NONE

    async def run_locked_response(
        self,
        *,
        target: MessageTarget,
        response_envelope: MessageEnvelope | None,
        queued_notice_reservation: QueuedHumanNoticeReservation | None,
        pipeline_timing: DispatchPipelineTiming | None,
        locked_operation: Callable[[MessageTarget], Awaitable[_LockedResponseResult]],
    ) -> _LockedResponseResult:
        """Run one locked response operation with shared queued-message bookkeeping."""
        lifecycle_lock = self._response_lifecycle_lock(target)
        queued_signal = self._get_or_create_queued_signal(target)
        notice = self._begin_response_turn_notice(
            lifecycle_lock=lifecycle_lock,
            queued_signal=queued_signal,
            response_envelope=response_envelope,
            queued_notice_reservation=queued_notice_reservation,
        )
        lock_acquired = False
        try:
            if pipeline_timing is not None:
                pipeline_timing.mark("lock_wait_start")
            await lifecycle_lock.acquire()
            lock_acquired = True
            if pipeline_timing is not None:
                pipeline_timing.mark("lock_acquired")
            try:
                if queued_notice_reservation is not None:
                    queued_notice_reservation.consume()
                notice = self._consume_queued_human_notice(
                    notice=notice,
                    queued_signal=queued_signal,
                )
                with queued_message_signal_context(queued_signal):
                    return await locked_operation(target)
            finally:
                if lock_acquired:
                    lifecycle_lock.release()
        finally:
            self._consume_queued_human_notice(
                notice=notice,
                queued_signal=queued_signal,
            )
            queued_signal.finish_response_turn()


@dataclass(frozen=True)
class SessionStartedWatch:
    """Pre-computed session:started eligibility and emission arguments."""

    should_watch: bool
    tool_context: ToolRuntimeContext | None
    scope: HistoryScope
    session_id: str
    room_id: str
    thread_id: str | None
    session_type: SessionType
    correlation_id: str
    create_storage: Callable[[], BaseDb]


@dataclass(frozen=True)
class ResponseLifecycleDeps:
    """Dependencies owned by the response lifecycle boundary."""

    response_hooks: ResponseHookService
    logger: BoundLogger


def _session_exists(
    *,
    storage: BaseDb,
    session_id: str,
    session_type: SessionType,
) -> bool:
    if session_type is SessionType.TEAM:
        return get_team_session(storage, session_id) is not None
    return get_agent_session(storage, session_id) is not None


def response_outcome_label(final_delivery_outcome: FinalDeliveryOutcome | None) -> str:
    """Return one pipeline outcome label for the canonical final delivery outcome."""
    if final_delivery_outcome is not None and final_delivery_outcome.suppressed:
        return "suppressed"
    if final_delivery_outcome is not None and final_delivery_outcome.terminal_status == "cancelled":
        return "cancelled"
    if final_delivery_outcome is not None and final_delivery_outcome.terminal_status == "error":
        return "error"
    if final_delivery_outcome is not None and final_delivery_outcome.delivery_kind is not None:
        return final_delivery_outcome.delivery_kind
    if (
        final_delivery_outcome is not None
        and final_delivery_outcome.event_id is not None
        and final_delivery_outcome.is_visible_response
    ):
        return "visible_response_preserved"
    return "no_visible_response"


class ResponseLifecycle:
    """Consolidate lifecycle helpers shared across response paths."""

    def __init__(
        self,
        deps: ResponseLifecycleDeps,
        *,
        response_kind: str,
        pipeline_timing: DispatchPipelineTiming | None,
        response_envelope: MessageEnvelope,
        correlation_id: str,
    ) -> None:
        self.deps = deps
        self.response_kind = response_kind
        self.pipeline_timing = pipeline_timing
        self.response_envelope = response_envelope
        self.correlation_id = correlation_id

    def _log_effects_failure_after_visible_delivery(
        self,
        *,
        response_event_id: str,
        error: BaseException,
    ) -> None:
        """Log one non-fatal post-response failure after visible delivery succeeded."""
        self.deps.logger.error(
            "Post-response effects failed after visible delivery",
            response_kind=self.response_kind,
            response_event_id=response_event_id,
            failure_reason=str(error),
            error_type=error.__class__.__name__,
        )

    def _session_started_watch_is_needed(
        self,
        *,
        tool_context: ToolRuntimeContext | None,
        session_id: str,
        session_type: SessionType,
        create_storage: Callable[[], BaseDb],
    ) -> bool:
        if tool_context is None or not tool_context.hook_registry.has_hooks(EVENT_SESSION_STARTED):
            return False
        try:
            storage = create_storage()
            try:
                return not _session_exists(
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

    def setup_session_watch(
        self,
        *,
        tool_context: ToolRuntimeContext | None,
        session_id: str,
        session_type: SessionType,
        scope: HistoryScope,
        room_id: str,
        thread_id: str | None,
        create_storage: Callable[[], BaseDb],
    ) -> SessionStartedWatch:
        """Pre-compute session:started eligibility for one response path."""
        return SessionStartedWatch(
            should_watch=self._session_started_watch_is_needed(
                tool_context=tool_context,
                session_id=session_id,
                session_type=session_type,
                create_storage=create_storage,
            ),
            tool_context=tool_context,
            scope=scope,
            session_id=session_id,
            room_id=room_id,
            thread_id=thread_id,
            session_type=session_type,
            correlation_id=self.correlation_id,
            create_storage=create_storage,
        )

    async def _maybe_emit_session_started(self, watch: SessionStartedWatch) -> None:
        if watch.tool_context is None or not watch.should_watch:
            return
        storage = watch.create_storage()
        try:
            if not _session_exists(storage=storage, session_id=watch.session_id, session_type=watch.session_type):
                return
        finally:
            storage.close()

        bindings = resolve_tool_runtime_hook_bindings(watch.tool_context)
        context = SessionHookContext(
            event_name=EVENT_SESSION_STARTED,
            plugin_name="",
            settings={},
            config=watch.tool_context.config,
            runtime_paths=watch.tool_context.runtime_paths,
            logger=self.deps.logger.bind(event_name=EVENT_SESSION_STARTED, session_id=watch.session_id),
            correlation_id=watch.correlation_id,
            message_sender=bindings.message_sender,
            matrix_admin=bindings.matrix_admin,
            room_state_querier=bindings.room_state_querier,
            room_state_putter=bindings.room_state_putter,
            agent_name=watch.scope.scope_id if watch.scope.kind == "team" else watch.tool_context.agent_name,
            scope=watch.scope,
            session_id=watch.session_id,
            room_id=watch.room_id,
            thread_id=watch.thread_id,
        )
        await emit(watch.tool_context.hook_registry, EVENT_SESSION_STARTED, context)

    async def emit_session_started(self, watch: SessionStartedWatch) -> None:
        """Emit session:started without aborting delivery on ordinary failures."""
        try:
            await self._maybe_emit_session_started(watch)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.deps.logger.exception(
                "Failed to emit session:started",
                session_id=watch.session_id,
                room_id=watch.room_id,
                thread_id=watch.thread_id,
                failure_reason=str(error),
            )

    async def finalize(
        self,
        final_delivery_outcome: FinalDeliveryOutcome,
        *,
        build_post_response_outcome: Callable[[FinalDeliveryOutcome], ResponseOutcome],
        post_response_deps: PostResponseEffectsDeps | Callable[[], PostResponseEffectsDeps],
    ) -> FinalDeliveryOutcome:
        """Run outer lifecycle finalization and return the canonical terminal outcome."""
        response_event_id = final_delivery_outcome.final_visible_event_id
        try:
            if final_delivery_outcome.terminal_status == "completed":
                if (
                    response_event_id is not None
                    and final_delivery_outcome.final_visible_body is not None
                    and final_delivery_outcome.delivery_kind is not None
                ):
                    await self.deps.response_hooks.emit_after_response(
                        correlation_id=self.correlation_id,
                        envelope=self.response_envelope,
                        response_text=final_delivery_outcome.final_visible_body,
                        response_event_id=response_event_id,
                        delivery_kind=final_delivery_outcome.delivery_kind,
                        response_kind=self.response_kind,
                        continue_on_cancelled=True,
                    )
            else:
                await self.deps.response_hooks.emit_cancelled_response(
                    correlation_id=self.correlation_id,
                    envelope=self.response_envelope,
                    visible_response_event_id=response_event_id,
                    response_kind=self.response_kind,
                    failure_reason=final_delivery_outcome.failure_reason,
                )
        except asyncio.CancelledError as error:
            if response_event_id is None:
                raise
            self._log_effects_failure_after_visible_delivery(
                response_event_id=response_event_id,
                error=error,
            )
        except Exception as error:
            if response_event_id is None:
                raise
            self._log_effects_failure_after_visible_delivery(
                response_event_id=response_event_id,
                error=error,
            )
        await self.apply_effects_safely(
            final_delivery_outcome=final_delivery_outcome,
            post_response_outcome=lambda: build_post_response_outcome(final_delivery_outcome),
            post_response_deps=post_response_deps,
        )
        if self.pipeline_timing is not None:
            self.pipeline_timing.emit_summary(self.deps.logger, outcome=response_outcome_label(final_delivery_outcome))
        return final_delivery_outcome

    async def apply_effects_safely(
        self,
        *,
        final_delivery_outcome: FinalDeliveryOutcome,
        post_response_outcome: ResponseOutcome | Callable[[], ResponseOutcome],
        post_response_deps: PostResponseEffectsDeps | Callable[[], PostResponseEffectsDeps],
    ) -> None:
        """Apply post-response effects without masking failures before visible delivery."""
        response_event_id = final_delivery_outcome.final_visible_event_id
        try:
            if callable(post_response_outcome):
                post_response_outcome = cast("Callable[[], ResponseOutcome]", post_response_outcome)()
            if callable(post_response_deps):
                post_response_deps = cast("Callable[[], PostResponseEffectsDeps]", post_response_deps)()
            await apply_post_response_effects(
                final_delivery_outcome,
                post_response_outcome,
                post_response_deps,
            )
        except asyncio.CancelledError as error:
            if response_event_id is None:
                raise
            self._log_effects_failure_after_visible_delivery(
                response_event_id=response_event_id,
                error=error,
            )
        except Exception as error:
            if response_event_id is None:
                raise
            self._log_effects_failure_after_visible_delivery(
                response_event_id=response_event_id,
                error=error,
            )
