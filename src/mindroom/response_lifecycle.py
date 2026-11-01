"""Shared response lifecycle helpers."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import Context, ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, TypeVar

from mindroom import constants
from mindroom.ai_runtime import queued_message_signal_context
from mindroom.background_tasks import create_background_task
from mindroom.cancellation import current_task_is_process_shutdown
from mindroom.dispatch_source import SILENT_SCHEDULE_SOURCE_KIND
from mindroom.final_delivery import AwaitingApproval, Owed, Suppression, hook_failure_reason, outcome_label
from mindroom.hooks import (
    EVENT_MESSAGE_AFTER_RESPONSE,
    EVENT_MESSAGE_BEFORE_RESPONSE,
    EVENT_MESSAGE_CANCELLED,
    EVENT_MESSAGE_FINAL_RESPONSE_TRANSFORM,
    AfterResponseContext,
    BeforeResponseContext,
    CancelledResponseContext,
    CancelledResponseInfo,
    FinalResponseDraft,
    FinalResponseTransformContext,
    ResponseDraft,
    ResponseResult,
    emit,
    emit_final_response_transform,
    emit_transform,
)
from mindroom.logging_config import get_logger
from mindroom.message_target import ResponseLifecycleKey
from mindroom.mid_turn import QueuedMessage, message_text_for_judgment
from mindroom.post_response_effects import apply_post_response_effects
from mindroom.scheduling.scheduled_run_records import record_silent_schedule_result_if_needed
from mindroom.streaming import strip_matching_visible_tool_markers
from mindroom.tool_jobs.control import pending_human_messages_context

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine, Iterator, Sequence

    from structlog.stdlib import BoundLogger

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.delivery_gateway import ResponseIdentity
    from mindroom.event_journal.approval_signals import ApprovalSignals, PausedReply
    from mindroom.final_delivery import FinalDeliveryOutcome
    from mindroom.hooks import HookContextSupport, MessageEnvelope
    from mindroom.message_target import MessageTarget
    from mindroom.mid_turn import MidTurnGate
    from mindroom.post_response_effects import PostResponseEffectsDeps, ResponseOutcome
    from mindroom.timing import DispatchPipelineTiming
    from mindroom.tool_system.events import ToolTraceEntry

logger = get_logger(__name__)

_LockedResponseResult = TypeVar("_LockedResponseResult")


@dataclass(slots=True)
class ResponseLifecycleReservation:
    """Own an early place in one coordinator's canonical lifecycle lock."""

    _coordinator: ResponseLifecycleCoordinator
    _lifecycle_key: ResponseLifecycleKey
    _lifecycle_lock: asyncio.Lock
    _queued_signal: _QueuedMessageState
    _notice: str | None
    _ready: asyncio.Event = field(default_factory=asyncio.Event)
    _acquire_task: asyncio.Task[None] | None = None
    _release_task: asyncio.Task[None] | None = None
    _owns_lock: bool = False
    _consumed: bool = False
    _response_turn_active: bool = True

    async def _acquire(self) -> None:
        try:
            if self._lifecycle_lock.locked():
                self._ready.set()
            await self._lifecycle_lock.acquire()
            self._owns_lock = True
        finally:
            self._ready.set()

    async def _wait_until_reserved(self) -> None:
        """Wait until this owner has acquired or queued on its lifecycle lock."""
        if self._acquire_task is None:
            self._acquire_task = asyncio.create_task(self._acquire())
        await self._ready.wait()

    def _consume(
        self,
        coordinator: ResponseLifecycleCoordinator,
        target: MessageTarget,
    ) -> asyncio.Lock:
        """Transfer this reservation to its exact response lifecycle."""
        if self._coordinator is not coordinator:
            msg = "Response lifecycle reservation belongs to a different coordinator"
            raise ValueError(msg)
        if self._lifecycle_key != target.lifecycle_key:
            msg = "Response lifecycle reservation target does not match the response target"
            raise ValueError(msg)
        if self._consumed:
            msg = "Response lifecycle reservation was already consumed"
            raise RuntimeError(msg)
        self._consumed = True
        return self._lifecycle_lock

    async def wait_until_acquired(self) -> None:
        """Wait until the reservation owns its lifecycle lock."""
        if self._acquire_task is None:
            msg = "Response lifecycle reservation has not started"
            raise RuntimeError(msg)
        await self._acquire_task

    def _consume_notice(self) -> None:
        if self._notice is None:
            return
        self._queued_signal.consume_waiting_human_message(self._notice)
        self._notice = None

    async def _release(self) -> None:
        acquire_task = self._acquire_task
        if acquire_task is not None and not acquire_task.done():
            acquire_task.cancel()
            await asyncio.gather(acquire_task, return_exceptions=True)
        if self._owns_lock:
            self._owns_lock = False
            self._lifecycle_lock.release()
        self._consume_notice()
        if self._response_turn_active:
            self._response_turn_active = False
            self._queued_signal.finish_response_turn()

    async def release(self) -> None:
        """Release or cancel this reservation exactly once."""
        if self._release_task is None:
            self._release_task = asyncio.create_task(self._release())
        await asyncio.shield(self._release_task)


_current_response_lifecycle_reservation: ContextVar[ResponseLifecycleReservation | None] = ContextVar(
    "current_response_lifecycle_reservation",
    default=None,
)


@contextmanager
def response_lifecycle_reservation_context(
    reservation: ResponseLifecycleReservation,
) -> Iterator[None]:
    """Carry one detached response's lifecycle reservation into execution."""
    token = _current_response_lifecycle_reservation.set(reservation)
    try:
        yield
    finally:
        _current_response_lifecycle_reservation.reset(token)


@dataclass
class _QueuedMessageState:
    """Track queued human ingress while one response lifecycle holds the lock."""

    _pending_messages: dict[str, QueuedMessage] = field(default_factory=dict)
    mid_turn_gate: MidTurnGate | None = None
    _active_response_turns: int = 0
    # Replaced on every change of the queue, so each waiter wakes once per change.
    _changed: asyncio.Event = field(default_factory=asyncio.Event)
    _idle_event: asyncio.Event = field(default_factory=asyncio.Event)

    def __post_init__(self) -> None:
        self._idle_event.set()

    def pending_message_snapshot(self) -> tuple[QueuedMessage, ...]:
        """Snapshot queued text in admission order without consuming any event."""
        return tuple(self._pending_messages.values())

    @property
    def pending_human_messages(self) -> int:
        """Return the number of distinct queued human events."""
        return len(self._pending_messages)

    def begin_response_turn(self) -> bool:
        existing_turn = self._active_response_turns > 0
        self._active_response_turns += 1
        self._idle_event.clear()
        return existing_turn

    def finish_response_turn(self) -> None:
        if self._active_response_turns == 0:
            return
        self._active_response_turns -= 1
        if self._active_response_turns == 0:
            self._idle_event.set()

    def add_waiting_human_message(
        self,
        source_event_id: str,
        *,
        text: str | None = None,
        requester_id: str | None = None,
    ) -> bool:
        if source_event_id in self._pending_messages:
            return False
        progress = self.mid_turn_gate.visible_response_text if self.mid_turn_gate is not None else ""
        self._pending_messages[source_event_id] = QueuedMessage(source_event_id, text, progress, requester_id)
        self._notify_changed()
        return True

    def consume_waiting_human_message(self, source_event_id: str) -> None:
        if source_event_id not in self._pending_messages:
            return
        del self._pending_messages[source_event_id]
        self._notify_changed()

    def _notify_changed(self) -> None:
        self._changed.set()
        self._changed = asyncio.Event()

    def has_pending_human_messages(self) -> bool:
        return self.pending_human_messages > 0

    def has_active_response_turn(self) -> bool:
        return self._active_response_turns > 0

    async def wait(self) -> None:
        """Return once a queued message needs the reply's attention; the mid-turn judge can let messages wait."""
        while True:
            changed = self._changed
            if await self._interrupts():
                return
            # A queue that changed while it was judged or acknowledged is judged again at once.
            await changed.wait()

    async def _interrupts(self) -> bool:
        pending = self.pending_message_snapshot()
        if not pending:
            return False
        gate = self.mid_turn_gate
        if gate is None:
            return True

        def still_queued() -> bool:
            return pending == self.pending_message_snapshot()

        # A decision counts only for the queue it judged.
        return not await gate.defers(pending, current=still_queued) and still_queued()

    async def wait_until_idle(self) -> None:
        await self._idle_event.wait()


@dataclass(slots=True)
class QueuedHumanNoticeReservation:
    """Owned reservation for a queued-human notice created before dispatch starts."""

    _state: _QueuedMessageState
    _source_event_id: str
    _active: bool = True

    def release(self) -> None:
        """Stop counting this message as waiting for the running response; idempotent."""
        if not self._active:
            return
        self._state.consume_waiting_human_message(self._source_event_id)
        self._active = False


@dataclass
class ResponseLifecycleCoordinator:
    """Serialize response turns and signal active turns about queued human ingress."""

    _response_lifecycle_locks: dict[ResponseLifecycleKey, asyncio.Lock] = field(default_factory=dict)
    _thread_queued_signals: dict[ResponseLifecycleKey, _QueuedMessageState] = field(default_factory=dict)
    # A pending approval counts as an active response turn of its conversation, by approval,
    # for each paused reply the journal's approval signals report.
    _approval_holds: dict[str, ResponseLifecycleKey] = field(default_factory=dict)
    _stop_following_approvals: Callable[[], None] | None = None

    def follow_approval_holds(self, signals: ApprovalSignals, principal_id: str) -> None:
        """Hold the conversation of each paused reply of ``principal_id``, from the ones paused now, until it ends.

        Later messages wait as they wait behind a running response. The hold
        does not take the lifecycle lock, so the approval's own resume and
        settlement still run.
        """
        self.stop_following_approval_holds()
        self._stop_following_approvals = signals.follow_paused_replies(
            principal_id,
            paused=self._hold_for_approval,
            ended=self._release_approval_hold,
        )

    def stop_following_approval_holds(self) -> None:
        """Stop learning of paused replies, as a stopping bot does before its replacement follows them."""
        stop, self._stop_following_approvals = self._stop_following_approvals, None
        if stop is not None:
            stop()

    def _hold_for_approval(self, paused: PausedReply) -> None:
        if paused.approval_id in self._approval_holds:
            return
        lifecycle_key = ResponseLifecycleKey(room_id=paused.room_id, thread_id=paused.thread_id)
        self._approval_holds[paused.approval_id] = lifecycle_key
        self._queued_signal_for_key(lifecycle_key).begin_response_turn()

    def _release_approval_hold(self, approval_id: str) -> None:
        lifecycle_key = self._approval_holds.pop(approval_id, None)
        if lifecycle_key is not None:
            self._thread_queued_signals[lifecycle_key].finish_response_turn()

    def approval_holds(self, room_id: str, thread_id: str | None) -> tuple[str, ...]:
        """Return the approvals that hold one conversation."""
        lifecycle_key = ResponseLifecycleKey(room_id=room_id, thread_id=thread_id)
        return tuple(approval_id for approval_id, key in self._approval_holds.items() if key == lifecycle_key)

    def _has_active_response_for_thread_key(self, lifecycle_key: ResponseLifecycleKey) -> bool:
        queued_signal = self._thread_queued_signals.get(lifecycle_key)
        if queued_signal is not None and queued_signal.has_active_response_turn():
            return True
        lifecycle_lock = self._response_lifecycle_locks.get(lifecycle_key)
        return lifecycle_lock.locked() if lifecycle_lock is not None else False

    def has_active_response_for_target(self, target: MessageTarget) -> bool:
        """Return whether one canonical conversation target already has an active turn."""
        return self._has_active_response_for_thread_key(target.lifecycle_key)

    def has_waiting_messages_from(self, target: MessageTarget, requester_id: str) -> bool:
        """Return whether one requester's messages wait for one canonical conversation's next turn."""
        queued_signal = self._thread_queued_signals.get(target.lifecycle_key)
        return queued_signal is not None and any(
            message.requester_id == requester_id for message in queued_signal.pending_message_snapshot()
        )

    def active_thread_ids_for_room(self, room_id: str) -> frozenset[str | None]:
        """Return canonical thread IDs with active response lifecycles in one room."""
        known_lifecycle_keys = set(self._thread_queued_signals) | set(self._response_lifecycle_locks)
        return frozenset(
            lifecycle_key.thread_id
            for lifecycle_key in known_lifecycle_keys
            if lifecycle_key.room_id == room_id and self._has_active_response_for_thread_key(lifecycle_key)
        )

    async def wait_for_thread_idle(self, room_id: str, thread_id: str | None) -> None:
        """Wait until a response lifecycle lock is idle for one room/thread key."""
        lifecycle_key = ResponseLifecycleKey(room_id=room_id, thread_id=thread_id)
        while self._has_active_response_for_thread_key(lifecycle_key):
            queued_signal = self._thread_queued_signals.get(lifecycle_key)
            if queued_signal is not None and queued_signal.has_active_response_turn():
                await queued_signal.wait_until_idle()
                continue
            lifecycle_lock = self._response_lifecycle_locks.get(lifecycle_key)
            if lifecycle_lock is not None and lifecycle_lock.locked():
                async with lifecycle_lock:
                    pass
                continue
            return

    def _response_lifecycle_lock(self, target: MessageTarget) -> asyncio.Lock:
        """Return the per-target lock that serializes one response lifecycle."""
        lock_key = target.lifecycle_key
        lock = self._response_lifecycle_locks.get(lock_key)
        if lock is not None:
            return lock
        if len(self._response_lifecycle_locks) >= 100:
            for candidate, candidate_lock in list(self._response_lifecycle_locks.items()):
                if len(self._response_lifecycle_locks) < 100:
                    break
                if candidate_lock.locked():
                    continue
                # An unlocked entry is not necessarily an idle one. Queued human
                # ingress, and a turn that has begun but not yet taken the lock,
                # both live in the signal rather than the lock, so evicting on
                # lock state alone silently drops user input.
                candidate_signal = self._thread_queued_signals.get(candidate)
                if candidate_signal is not None and (
                    candidate_signal.has_pending_human_messages() or candidate_signal.has_active_response_turn()
                ):
                    continue
                self._response_lifecycle_locks.pop(candidate, None)
                self._thread_queued_signals.pop(candidate, None)
        lock = asyncio.Lock()
        self._response_lifecycle_locks[lock_key] = lock
        return lock

    async def reserve_response_lifecycle(
        self,
        response_envelope: MessageEnvelope,
    ) -> ResponseLifecycleReservation:
        """Reserve FIFO admission on one canonical lifecycle before response preparation."""
        target = response_envelope.target
        lifecycle_lock = self._response_lifecycle_lock(target)
        queued_signal = self._get_or_create_queued_signal(target)
        reservation = ResponseLifecycleReservation(
            _coordinator=self,
            _lifecycle_key=target.lifecycle_key,
            _lifecycle_lock=lifecycle_lock,
            _queued_signal=queued_signal,
            _notice=self._begin_response_turn_notice(
                lifecycle_lock=lifecycle_lock,
                queued_signal=queued_signal,
                response_envelope=response_envelope,
                signal_queued_message=True,
            ),
        )
        try:
            await reservation._wait_until_reserved()
        except BaseException:
            await reservation.release()
            raise
        return reservation

    def _get_or_create_queued_signal(self, target: MessageTarget) -> _QueuedMessageState:
        """Return the queued-message signal for one canonical conversation thread."""
        return self._queued_signal_for_key(target.lifecycle_key)

    def _queued_signal_for_key(self, lifecycle_key: ResponseLifecycleKey) -> _QueuedMessageState:
        signal = self._thread_queued_signals.get(lifecycle_key)
        if signal is not None:
            return signal
        signal = _QueuedMessageState()
        self._thread_queued_signals[lifecycle_key] = signal
        return signal

    def visible_progress_callback(
        self,
        target: MessageTarget,
    ) -> Callable[[str, Sequence[ToolTraceEntry]], None] | None:
        """Bind acknowledged Matrix text to the active response's opted-in judge."""
        signal = self._thread_queued_signals.get(target.lifecycle_key)
        if signal is None or signal.mid_turn_gate is None:
            return None
        return signal.mid_turn_gate.record_visible_response

    @staticmethod
    def _should_signal_queued_message(
        response_envelope: MessageEnvelope,
    ) -> bool:
        """Return whether one queued ingress should interrupt the active turn."""
        return response_envelope.origin.may_answer_interactive_prompt

    def _assert_target_matches_envelope(self, target: MessageTarget, response_envelope: MessageEnvelope) -> None:
        """Require lifecycle callers to use the envelope's canonical response target."""
        if target.lifecycle_key == response_envelope.target.lifecycle_key:
            return
        msg = "Response lifecycle target must match MessageEnvelope.target"
        raise ValueError(msg)

    def reserve_waiting_human_message(
        self,
        *,
        target: MessageTarget,
        response_envelope: MessageEnvelope,
    ) -> QueuedHumanNoticeReservation | None:
        """Reserve an active-turn notice before queued dispatch owns the follow-up."""
        self._assert_target_matches_envelope(target, response_envelope)
        if not self._should_signal_queued_message(response_envelope):
            return None
        if not self._has_active_response_for_thread_key(target.lifecycle_key):
            return None
        queued_signal = self._get_or_create_queued_signal(target)
        if not queued_signal.add_waiting_human_message(
            response_envelope.source_event_id,
            text=message_text_for_judgment(response_envelope),
            requester_id=response_envelope.requester_id,
        ):
            return None
        return QueuedHumanNoticeReservation(queued_signal, response_envelope.source_event_id)

    def _begin_response_turn_notice(
        self,
        *,
        lifecycle_lock: asyncio.Lock,
        queued_signal: _QueuedMessageState,
        response_envelope: MessageEnvelope,
        signal_queued_message: bool,
    ) -> str | None:
        existing_turn = queued_signal.begin_response_turn()
        if not signal_queued_message:
            return None
        if not (existing_turn or lifecycle_lock.locked()):
            return None
        if not self._should_signal_queued_message(response_envelope):
            return None
        if not queued_signal.add_waiting_human_message(
            response_envelope.source_event_id,
            text=message_text_for_judgment(response_envelope),
            requester_id=response_envelope.requester_id,
        ):
            return None
        return response_envelope.source_event_id

    def _consume_queued_human_notice(
        self,
        *,
        notice: str | None,
        queued_signal: _QueuedMessageState,
    ) -> None:
        if notice is None:
            return
        queued_signal.consume_waiting_human_message(notice)

    def _start_response_turn(
        self,
        *,
        target: MessageTarget,
        response_envelope: MessageEnvelope,
        signal_queued_message: bool,
        reservation: ResponseLifecycleReservation | None,
    ) -> tuple[asyncio.Lock, _QueuedMessageState, str | None]:
        if reservation is not None:
            return reservation._consume(self, target), reservation._queued_signal, None
        lifecycle_lock = self._response_lifecycle_lock(target)
        queued_signal = self._get_or_create_queued_signal(target)
        notice = self._begin_response_turn_notice(
            lifecycle_lock=lifecycle_lock,
            queued_signal=queued_signal,
            response_envelope=response_envelope,
            signal_queued_message=signal_queued_message,
        )
        return lifecycle_lock, queued_signal, notice

    @staticmethod
    async def _acquire_response_turn_lock(
        lifecycle_lock: asyncio.Lock,
        reservation: ResponseLifecycleReservation | None,
    ) -> None:
        if reservation is not None:
            await reservation.wait_until_acquired()
            return
        await lifecycle_lock.acquire()

    def _consume_response_turn_notice(
        self,
        *,
        reservation: ResponseLifecycleReservation | None,
        notice: str | None,
        queued_signal: _QueuedMessageState,
    ) -> None:
        if reservation is not None:
            reservation._consume_notice()
            return
        self._consume_queued_human_notice(notice=notice, queued_signal=queued_signal)

    @staticmethod
    async def _release_response_turn_lock(
        lifecycle_lock: asyncio.Lock,
        reservation: ResponseLifecycleReservation | None,
    ) -> None:
        if reservation is not None:
            await reservation.release()
            return
        lifecycle_lock.release()

    async def _finish_response_turn(
        self,
        *,
        reservation: ResponseLifecycleReservation | None,
        notice: str | None,
        queued_signal: _QueuedMessageState,
    ) -> None:
        if reservation is not None:
            await reservation.release()
            return
        self._consume_queued_human_notice(notice=notice, queued_signal=queued_signal)
        queued_signal.finish_response_turn()

    async def run_locked_response(
        self,
        *,
        target: MessageTarget,
        response_envelope: MessageEnvelope,
        pipeline_timing: DispatchPipelineTiming | None,
        locked_operation: Callable[[MessageTarget], Awaitable[_LockedResponseResult]],
        signal_queued_message: bool = True,
        mid_turn_gate: MidTurnGate | None = None,
    ) -> _LockedResponseResult:
        """Run one locked response operation with shared queued-message bookkeeping."""
        self._assert_target_matches_envelope(target, response_envelope)
        reservation = _current_response_lifecycle_reservation.get()
        lifecycle_lock, queued_signal, notice = self._start_response_turn(
            target=target,
            response_envelope=response_envelope,
            signal_queued_message=signal_queued_message,
            reservation=reservation,
        )
        if reservation is not None:
            # This response now owns the reservation locally. Descendant tasks
            # must acquire their own lifecycle instead of inheriting this one-shot claim.
            _current_response_lifecycle_reservation.set(None)
        try:
            if pipeline_timing is not None:
                pipeline_timing.mark("lock_wait_start")
            await self._acquire_response_turn_lock(lifecycle_lock, reservation)
            if pipeline_timing is not None:
                pipeline_timing.mark("lock_acquired")
            try:
                self._consume_response_turn_notice(
                    reservation=reservation,
                    notice=notice,
                    queued_signal=queued_signal,
                )
                queued_signal.mid_turn_gate = mid_turn_gate
                with (
                    # A newer human message also releases the reply's tool waits; other queued turns wait for it.
                    pending_human_messages_context(queued_signal),
                    queued_message_signal_context(queued_signal, mid_turn_gate=mid_turn_gate),
                ):
                    try:
                        return await locked_operation(target)
                    finally:
                        queued_signal.mid_turn_gate = None
            finally:
                await self._release_response_turn_lock(lifecycle_lock, reservation)
        finally:
            await self._finish_response_turn(
                reservation=reservation,
                notice=notice,
                queued_signal=queued_signal,
            )


@dataclass
class ResponseHookService:
    """Own response hook execution around final delivery."""

    hook_context: HookContextSupport
    # Passes each final agent reply on to another surface, or returns None when the reply goes nowhere else, so no
    # task starts for it. It runs in the background, so a slow surface never holds the response lock that
    # `_emit_after_response` runs under.
    reply_mirror: Callable[[MessageEnvelope, str], Coroutine[Any, Any, None] | None] | None = None

    async def apply_before_response(
        self,
        *,
        identity: ResponseIdentity,
        response_text: str,
        tool_trace: list[ToolTraceEntry] | None,
        extra_content: dict[str, Any] | None,
    ) -> ResponseDraft:
        """Return a whole answer as the ``message:before_response`` hooks leave it; raises what a hook raises."""
        draft = ResponseDraft(
            response_text=response_text,
            response_kind=identity.response_kind,
            tool_trace=deepcopy(tool_trace) if tool_trace is not None else None,
            extra_content=deepcopy(extra_content) if extra_content is not None else None,
            envelope=identity.response_envelope,
        )
        if not self.hook_context.registry.has_hooks(EVENT_MESSAGE_BEFORE_RESPONSE):
            return draft
        context = BeforeResponseContext(
            **self.hook_context.base_kwargs(EVENT_MESSAGE_BEFORE_RESPONSE, identity.correlation_id),
            draft=draft,
        )
        return await emit_transform(self.hook_context.registry, EVENT_MESSAGE_BEFORE_RESPONSE, context)

    async def apply_final_response_transform(
        self,
        *,
        identity: ResponseIdentity,
        response_text: str,
    ) -> FinalResponseDraft:
        """Return a streamed answer's final text as the ``message:final_response_transform`` hooks leave it."""
        if current_task_is_process_shutdown():
            raise asyncio.CancelledError
        draft = FinalResponseDraft(
            response_text=response_text,
            response_kind=identity.response_kind,
            envelope=identity.response_envelope,
        )
        if not self.hook_context.registry.has_hooks(EVENT_MESSAGE_FINAL_RESPONSE_TRANSFORM):
            return draft
        context = FinalResponseTransformContext(
            **self.hook_context.base_kwargs(EVENT_MESSAGE_FINAL_RESPONSE_TRANSFORM, identity.correlation_id),
            draft=draft,
        )
        draft = await emit_final_response_transform(
            self.hook_context.registry,
            EVENT_MESSAGE_FINAL_RESPONSE_TRANSFORM,
            context,
        )
        if current_task_is_process_shutdown():
            raise asyncio.CancelledError
        return draft

    async def _emit_after_response(
        self,
        *,
        identity: ResponseIdentity,
        response_text: str,
        response_event_id: str,
        delivery_kind: Literal["sent", "edited"],
    ) -> None:
        if identity.response_kind == "ai" and self.reply_mirror is not None:
            try:
                mirror = self.reply_mirror(identity.response_envelope, response_text)
                if mirror is not None:
                    create_background_task(
                        mirror,
                        name="reply_mirror",
                        owner=self.hook_context.runtime,
                        # Outside the reply's span and context: the mirror is no part of the reply.
                        context=Context(),
                    )
            except Exception:
                logger.exception(
                    "Reply mirror failed",
                    agent=self.hook_context.agent_name,
                    correlation_id=identity.correlation_id,
                )
        if not self.hook_context.registry.has_hooks(EVENT_MESSAGE_AFTER_RESPONSE):
            return
        context = AfterResponseContext(
            **self.hook_context.base_kwargs(EVENT_MESSAGE_AFTER_RESPONSE, identity.correlation_id),
            result=ResponseResult(
                response_text=response_text,
                response_event_id=response_event_id,
                delivery_kind=delivery_kind,
                response_kind=identity.response_kind,
                envelope=identity.response_envelope,
            ),
        )
        await emit(
            self.hook_context.registry,
            EVENT_MESSAGE_AFTER_RESPONSE,
            context,
            # The answer is delivered: one cancelled observer does not keep the rest from seeing it.
            continue_on_cancelled=True,
        )

    async def _emit_cancelled_response(
        self,
        *,
        identity: ResponseIdentity,
        visible_response_event_id: str | None = None,
        failure_reason: str | None = None,
    ) -> None:
        if not self.hook_context.registry.has_hooks(EVENT_MESSAGE_CANCELLED):
            return
        context = CancelledResponseContext(
            **self.hook_context.base_kwargs(EVENT_MESSAGE_CANCELLED, identity.correlation_id),
            info=CancelledResponseInfo(
                envelope=identity.response_envelope,
                visible_response_event_id=visible_response_event_id,
                response_kind=identity.response_kind,
                failure_reason=failure_reason,
            ),
        )
        await emit(self.hook_context.registry, EVENT_MESSAGE_CANCELLED, context)


async def answer_suppression(
    draft: ResponseDraft,
    identity: ResponseIdentity,
    *,
    entity_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> Suppression | None:
    """Return why a whole answer the hooks left must not be shown, and record a silent schedule's result.

    A hook can suppress it, and neither a silent schedule's no-report answer nor a contact turn's answer is ever shown.
    """
    suppression = Suppression.HOOK if draft.suppress else None
    if suppression is None and draft.envelope.source_kind == SILENT_SCHEDULE_SOURCE_KIND:
        no_report_text = draft.response_text
        if draft.tool_trace:
            no_report_text = strip_matching_visible_tool_markers(no_report_text, draft.tool_trace)
        if constants.is_silent_schedule_no_report_response(no_report_text):
            suppression = Suppression.SILENT_NO_REPORT
    if suppression is None and identity.contact_turn:
        suppression = Suppression.CONTACT_TURN
    await record_silent_schedule_result_if_needed(
        entity_name=entity_name,
        agent_names=identity.participating_agent_names or (entity_name,),
        envelope=identity.response_envelope,
        config=config,
        runtime_paths=runtime_paths,
        suppression_reason=None if suppression is None else suppression.value,
        response_text=draft.response_text,
    )
    return suppression


@dataclass(frozen=True)
class ResponseLifecycleDeps:
    """Dependencies owned by the response lifecycle boundary."""

    response_hooks: ResponseHookService
    logger: BoundLogger
    process_shutdown_requested: Callable[[], bool]


class ResponseLifecycle:
    """Consolidate lifecycle helpers shared across response paths."""

    def __init__(
        self,
        deps: ResponseLifecycleDeps,
        *,
        identity: ResponseIdentity,
        pipeline_timing: DispatchPipelineTiming | None,
    ) -> None:
        self.deps = deps
        self.identity = identity
        self.pipeline_timing = pipeline_timing

    def _raise_if_process_shutdown(self) -> None:
        """Stop best-effort lifecycle work once orderly process shutdown owns cancellation."""
        if self.deps.process_shutdown_requested():
            raise asyncio.CancelledError

    def _log_effects_failure_after_visible_delivery(
        self,
        *,
        response_event_id: str,
        error: BaseException,
    ) -> None:
        """Log one non-fatal post-response failure after visible delivery succeeded."""
        self.deps.logger.error(
            "Post-response effects failed after visible delivery",
            response_kind=self.identity.response_kind,
            response_event_id=response_event_id,
            failure_reason=str(error),
            error_type=error.__class__.__name__,
        )

    async def finalize(
        self,
        final_delivery_outcome: FinalDeliveryOutcome,
        *,
        build_post_response_outcome: Callable[[FinalDeliveryOutcome], ResponseOutcome],
        post_response_deps: Callable[[], PostResponseEffectsDeps],
    ) -> FinalDeliveryOutcome:
        """Run outer lifecycle finalization and return the canonical terminal outcome."""
        if isinstance(final_delivery_outcome.ending, AwaitingApproval | Owed):
            await self._finalize_suspended(
                final_delivery_outcome,
                post_response_outcome=build_post_response_outcome(final_delivery_outcome),
                post_response_deps=post_response_deps,
            )
            return final_delivery_outcome
        self._raise_if_process_shutdown()
        response_event_id = final_delivery_outcome.final_visible_event_id
        with self._tolerated_after_visible_delivery(response_event_id):
            if final_delivery_outcome.completed:
                if (
                    response_event_id is not None
                    and final_delivery_outcome.final_visible_body is not None
                    and final_delivery_outcome.delivery_kind is not None
                ):
                    await self.deps.response_hooks._emit_after_response(
                        identity=self.identity,
                        response_text=final_delivery_outcome.final_visible_body,
                        response_event_id=response_event_id,
                        delivery_kind=final_delivery_outcome.delivery_kind,
                    )
            elif (failure_reason := hook_failure_reason(final_delivery_outcome.ending)) is not None:
                await self.deps.response_hooks._emit_cancelled_response(
                    identity=self.identity,
                    visible_response_event_id=response_event_id,
                    failure_reason=failure_reason,
                )
            self._raise_if_process_shutdown()
        await self._apply_effects_safely(
            final_delivery_outcome=final_delivery_outcome,
            post_response_outcome=lambda: build_post_response_outcome(final_delivery_outcome),
            post_response_deps=post_response_deps,
        )
        if self.pipeline_timing is not None:
            self.pipeline_timing.emit_summary(self.deps.logger, outcome=outcome_label(final_delivery_outcome))
        return final_delivery_outcome

    async def _finalize_suspended(
        self,
        final_delivery_outcome: FinalDeliveryOutcome,
        *,
        post_response_outcome: ResponseOutcome,
        post_response_deps: Callable[[], PostResponseEffectsDeps],
    ) -> None:
        """Finalize a reply whose run waits for approval, or whose answer is recorded but not yet acknowledged.

        A run paused for approval has not finished, so nothing applies. An owed answer's run finished, so its run's
        effects apply unless the process is stopping; effects and hooks that need a delivered completed answer skip.
        The outbox owns the answer by then, so an effect that fails or is cancelled must not settle it again.
        """
        if post_response_outcome.run_succeeded and not self.deps.process_shutdown_requested():
            try:
                await apply_post_response_effects(final_delivery_outcome, post_response_outcome, post_response_deps())
            except asyncio.CancelledError:
                if self.deps.process_shutdown_requested():
                    raise
                self.deps.logger.warning("Post-response effects cancelled for an answer the outbox owes")
            except Exception:
                self.deps.logger.exception("Post-response effects failed for an answer the outbox owes")
        if self.pipeline_timing is not None:
            self.pipeline_timing.emit_summary(self.deps.logger, outcome=outcome_label(final_delivery_outcome))

    async def _apply_effects_safely(
        self,
        *,
        final_delivery_outcome: FinalDeliveryOutcome,
        post_response_outcome: Callable[[], ResponseOutcome],
        post_response_deps: Callable[[], PostResponseEffectsDeps],
    ) -> None:
        """Apply post-response effects without masking failures before visible delivery."""
        self._raise_if_process_shutdown()
        with self._tolerated_after_visible_delivery(final_delivery_outcome.final_visible_event_id):
            await apply_post_response_effects(final_delivery_outcome, post_response_outcome(), post_response_deps())
            self._raise_if_process_shutdown()

    @contextmanager
    def _tolerated_after_visible_delivery(self, response_event_id: str | None) -> Iterator[None]:
        """Log a hook or effect failure once the answer is visible; before that, or at process shutdown, it raises."""
        try:
            yield
        except asyncio.CancelledError as error:
            if self.deps.process_shutdown_requested() or response_event_id is None:
                raise
            self._log_effects_failure_after_visible_delivery(response_event_id=response_event_id, error=error)
        except Exception as error:
            if response_event_id is None:
                raise
            self._log_effects_failure_after_visible_delivery(response_event_id=response_event_id, error=error)
