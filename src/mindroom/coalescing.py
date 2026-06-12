"""Live message coalescing gate."""

from __future__ import annotations

import asyncio
import enum
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .coalescing_batch import (
    CoalescedBatch,
    CoalescingKey,
    PendingEvent,
    active_follow_up_coalescing_key,
    build_coalesced_batch,
    is_active_follow_up_coalescing_key,
)
from .coalescing_cleanup import (
    ClaimedSegmentOwner,
    ReadyPendingEvent,
    close_pending_event_metadata_once,
    close_ready_task_result_metadata,
)
from .coalescing_policy import (
    QueueKind,
    is_coalescing_exempt_source_kind,
    pending_events_require_solo_batch,
    pending_has_only_text,
    pending_has_room_scope_source,
    queue_kind,
    source_or_event_allows_room_scope_batching,
)
from .dispatch_handoff import is_media_dispatch_event
from .dispatch_source import ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND
from .ingress_lanes import IngressAdmissionClosedError, IngressLanes, LaneSlot
from .logging_config import get_logger
from .timing import elapsed_ms_since, emit_elapsed_timing, event_timing_scope

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from .ingress_lanes import LaneDelivery

__all__ = [
    "CoalescingDrainResult",
    "CoalescingGate",
    "GatePhase",
    "IngressAdmissionClosedError",
    "LaneSlot",
    "ReadyPendingEvent",
    "close_ready_task_result_metadata",
    "is_coalescing_exempt_source_kind",
]

_UPLOAD_GRACE_HARD_CAP_MULTIPLIER = 4.0
_UPLOAD_GRACE_MAX_HARD_CAP_SECONDS = 2.0
_COALESCING_FLUSH_WARNING_SECONDS = 5.0
logger = get_logger(__name__)


async def _allow_dispatch(_key: CoalescingKey) -> None:
    return


class GatePhase(enum.Enum):
    """Lifecycle phases for one coalescing gate."""

    DEBOUNCE = "debounce"
    GRACE = "grace"
    IN_FLIGHT = "in_flight"


@dataclass
class _QueuedEvent:
    received_at: float
    receipt_time: float
    source_event_id: str | None
    source_kind: str
    ready_result: ReadyPendingEvent

    @property
    def pending_event(self) -> PendingEvent:
        """Return the resolved pending event."""
        return self.ready_result.pending_event


@dataclass(frozen=True)
class CoalescingDrainResult:
    """Outcome from flushing coalescing work during shutdown."""

    completed: bool
    released_reservation_count: int = 0
    cancelled_unready_count: int = 0
    failed_ready_count: int = 0
    dropped_ready_count: int = 0
    dispatch_failure_count: int = 0
    dispatch_cancelled_count: int = 0


@dataclass
class _MutableDrainResult:
    released_reservation_count: int = 0
    cancelled_unready_count: int = 0
    failed_ready_count: int = 0
    dropped_ready_count: int = 0
    dispatch_failure_count: int = 0
    dispatch_cancelled_count: int = 0

    def freeze(self) -> CoalescingDrainResult:
        completed = not any(
            (
                self.released_reservation_count,
                self.cancelled_unready_count,
                self.failed_ready_count,
                self.dropped_ready_count,
                self.dispatch_failure_count,
                self.dispatch_cancelled_count,
            ),
        )
        return CoalescingDrainResult(
            completed=completed,
            released_reservation_count=self.released_reservation_count,
            cancelled_unready_count=self.cancelled_unready_count,
            failed_ready_count=self.failed_ready_count,
            dropped_ready_count=self.dropped_ready_count,
            dispatch_failure_count=self.dispatch_failure_count,
            dispatch_cancelled_count=self.dispatch_cancelled_count,
        )


@dataclass
class _DrainContext:
    ready_timeout_seconds: float | None
    result: _MutableDrainResult
    cancelled_initial_drain_tasks: bool = False


@dataclass
class _GateEntry:
    phase: GatePhase = GatePhase.DEBOUNCE
    queue: deque[_QueuedEvent] = field(default_factory=deque)
    claimed_admissions: list[_QueuedEvent] = field(default_factory=list)
    drain_task: asyncio.Task[None] | None = None
    wake_event: asyncio.Event = field(default_factory=asyncio.Event)
    wake_generation: int = 0
    deadline: float | None = None
    grace_deadline: float | None = None
    drain_all_requested: bool = False
    drain_context: _DrainContext | None = None


@dataclass(frozen=True)
class _FlushDiagnostics:
    """Stable metadata for one flush attempt."""

    batch: CoalescedBatch
    pending_count: int
    timing_scope: str
    log_context: dict[str, object]


@dataclass(frozen=True)
class _DebounceWaitResult:
    """Boundary discovered during one debounce wait."""

    quiet_deadline: float


@dataclass(frozen=True)
class _UploadGraceWaitResult:
    """Boundary discovered during upload grace."""

    candidate_count: int
    quiet_deadline: float


class CoalescingGate:
    """Debounce/grace state machine for live inbound message batching.

    Ingress enters a per-(room, sender) lane in receipt order; lanes deliver
    only ready, conversation-assigned events to this gate. State machine per
    (room, thread, sender) key:
    IDLE (absent) -> DEBOUNCE -> GRACE (optional, wait for images) ->
    flush -> IN_FLIGHT, while all undispatched work remains in one FIFO queue.
    """

    def __init__(
        self,
        *,
        dispatch_batch: Callable[[CoalescedBatch], Awaitable[None]],
        debounce_seconds: Callable[[], float],
        upload_grace_seconds: Callable[[], float],
        is_shutting_down: Callable[[], bool],
        wait_until_dispatch_allowed: Callable[[CoalescingKey], Awaitable[None]] | None = None,
        room_scope_is_single_conversation: Callable[[str], bool] | None = None,
        dispatch_allowed_now: Callable[[CoalescingKey], bool] | None = None,
    ) -> None:
        self._dispatch_batch = dispatch_batch
        self._debounce_seconds = debounce_seconds
        self._upload_grace_seconds = upload_grace_seconds
        self._is_shutting_down = is_shutting_down
        self._wait_until_dispatch_allowed = wait_until_dispatch_allowed or _allow_dispatch
        self._room_scope_is_single_conversation = room_scope_is_single_conversation
        self._dispatch_allowed_now = dispatch_allowed_now
        self._gates: dict[CoalescingKey, _GateEntry] = {}
        self._lanes = IngressLanes(deliver=self._admit_from_lane)
        self._active_drain_context: _DrainContext | None = None

    @property
    def lanes(self) -> IngressLanes:
        """Return the per-(room, sender) ingress lanes feeding this gate."""
        return self._lanes

    def enter_lane(
        self,
        *,
        room_id: str,
        sender_id: str,
        receipt_time: float | None = None,
    ) -> LaneSlot:
        """Reserve receipt order in one sender lane before resolution can finish."""
        drain_context = self._active_drain_context
        if self._is_bounded_drain(drain_context):
            assert drain_context is not None
            drain_context.result.released_reservation_count += 1
            return IngressLanes.closed_slot(room_id=room_id, sender_id=sender_id, receipt_time=receipt_time)
        return self._lanes.enter(room_id=room_id, sender_id=sender_id, receipt_time=receipt_time)

    def submit_lane_slot(
        self,
        slot: LaneSlot,
        *,
        key: CoalescingKey,
        source_event_id: str | None,
        source_kind: str,
        ready_result: ReadyPendingEvent | None = None,
        ready_task: asyncio.Task[ReadyPendingEvent | None] | None = None,
        received_at: float | None = None,
    ) -> None:
        """Load one lane slot with its resolved conversation key and ready payload."""
        self._lanes.submit(
            slot,
            key=key,
            source_event_id=source_event_id,
            source_kind=source_kind,
            ready_result=ready_result,
            ready_task=ready_task,
            received_at=received_at,
            busy_at_submit=self._conversation_is_busy(key),
        )

    def release_lane_slot(self, slot: LaneSlot) -> None:
        """Release one lane slot that will not be admitted."""
        self._lanes.release(slot)

    def _conversation_is_busy(self, key: CoalescingKey) -> bool:
        return self._dispatch_allowed_now is not None and not self._dispatch_allowed_now(key)

    async def _admit_from_lane(
        self,
        slot: LaneSlot,
        delivery: LaneDelivery,
        ready: ReadyPendingEvent,
    ) -> None:
        if delivery.busy_at_submit and not self._conversation_is_busy(delivery.key):
            logger.info(
                "follow_up_missed_combined_turn",
                room_id=delivery.key.room_id,
                thread_id=delivery.key.thread_id,
                source_event_id=delivery.source_event_id,
            )
        await self.admit(
            delivery.key,
            ready_result=ready,
            received_at=delivery.received_at,
            receipt_time=slot.receipt_time,
            source_event_id=delivery.source_event_id,
            source_kind=delivery.source_kind,
        )

    def _remove_gate(self, key: CoalescingKey) -> None:
        self._gates.pop(key, None)

    def _get_or_create_gate(self, key: CoalescingKey) -> _GateEntry:
        gate = self._gates.get(key)
        if gate is None:
            gate = _GateEntry()
            gate.drain_context = self._active_drain_context
            self._gates[key] = gate
        return gate

    def _current_drain_context(self, gate: _GateEntry | None = None) -> _DrainContext | None:
        if gate is not None and gate.drain_context is not None:
            return gate.drain_context
        return self._active_drain_context

    @staticmethod
    def _is_bounded_drain(context: _DrainContext | None) -> bool:
        return context is not None and context.ready_timeout_seconds is not None

    @staticmethod
    def _gate_work_count(gate: _GateEntry) -> int:
        return len(gate.queue) + len(gate.claimed_admissions)

    async def _wait_for_lane_slots(self, gate: _GateEntry, slots: list[LaneSlot]) -> None:
        """Wait for undelivered same-sender ingress, releasing it on bounded drains."""
        while True:
            unsettled = [slot for slot in slots if not slot.settled.is_set()]
            if not unsettled:
                return
            drain_context = self._current_drain_context(gate)
            if not self._is_bounded_drain(drain_context):
                await asyncio.gather(*(slot.settled.wait() for slot in unsettled))
                continue
            assert drain_context is not None
            try:
                await asyncio.wait_for(
                    asyncio.gather(*(slot.settled.wait() for slot in unsettled)),
                    timeout=drain_context.ready_timeout_seconds,
                )
            except TimeoutError:
                await self._abandon_lane_slots(unsettled, drain_context)
                return

    async def _abandon_lane_slots(self, slots: list[LaneSlot], drain_context: _DrainContext) -> None:
        for slot in slots:
            if slot.settled.is_set():
                continue
            outcome = await self._lanes.abandon_slot(slot, ready_timeout_seconds=drain_context.ready_timeout_seconds)
            drain_context.result.released_reservation_count += 1
            drain_context.result.cancelled_unready_count += outcome.cancelled_unready_count
            drain_context.result.dropped_ready_count += outcome.dropped_ready_count

    @staticmethod
    def _oldest_pending_age_ms(gate: _GateEntry) -> float | None:
        pending = [*gate.claimed_admissions, *gate.queue]
        if not pending:
            return None
        oldest_enqueue_time = min(queued.received_at for queued in pending)
        return elapsed_ms_since(oldest_enqueue_time, clock=time.time)

    @staticmethod
    def _oldest_pending_events_age_ms(pending_events: list[PendingEvent]) -> float:
        oldest_enqueue_time = min(pending_event.enqueue_time for pending_event in pending_events)
        return elapsed_ms_since(oldest_enqueue_time, clock=time.time)

    @staticmethod
    def _source_event_ids(pending_events: list[PendingEvent]) -> list[str]:
        return [pending_event.event.event_id for pending_event in pending_events]

    @staticmethod
    def _queued_kind(queued: _QueuedEvent) -> QueueKind:
        return queue_kind(queued.ready_result.pending_event)

    @staticmethod
    def _claim_front_events(gate: _GateEntry, count: int) -> list[_QueuedEvent]:
        gate.claimed_admissions = [gate.queue.popleft() for _ in range(count)]
        return gate.claimed_admissions

    @staticmethod
    def _clear_claimed_admissions(gate: _GateEntry, admissions: list[_QueuedEvent]) -> None:
        if gate.claimed_admissions is admissions:
            gate.claimed_admissions = []

    @staticmethod
    def _requeue_claimed_admissions(gate: _GateEntry, admissions: list[_QueuedEvent]) -> None:
        if not admissions:
            return
        gate.queue.extendleft(reversed(admissions))
        CoalescingGate._clear_claimed_admissions(gate, admissions)

    @staticmethod
    def _insert_queued_event(gate: _GateEntry, admission: _QueuedEvent) -> None:
        for index, queued in enumerate(gate.queue):
            if admission.receipt_time < queued.receipt_time:
                gate.queue.insert(index, admission)
                return
        gate.queue.append(admission)

    @staticmethod
    def _front_normal_run_length(
        gate: _GateEntry,
        *,
        coalesce_normal_events: bool,
        max_receipt_time: float | None = None,
    ) -> int:
        count = 0
        for queued in gate.queue:
            if max_receipt_time is not None and queued.receipt_time > max_receipt_time:
                break
            if CoalescingGate._queued_kind(queued) is not QueueKind.NORMAL:
                break
            if count > 0 and not coalesce_normal_events:
                break
            count += 1
        return count

    @staticmethod
    def _extend_candidate_with_grace_media(gate: _GateEntry, candidate_count: int) -> int:
        count = candidate_count
        while count < len(gate.queue):
            queued = gate.queue[count]
            if CoalescingGate._queued_kind(queued) is not QueueKind.NORMAL or not is_media_dispatch_event(
                queued.pending_event.event,
            ):
                break
            count += 1
        return count

    @staticmethod
    def _has_barrier_after_front_normal_run(
        gate: _GateEntry,
        *,
        coalesce_normal_events: bool,
    ) -> bool:
        normal_count = CoalescingGate._front_normal_run_length(
            gate,
            coalesce_normal_events=coalesce_normal_events,
        )
        return normal_count < len(gate.queue)

    @staticmethod
    def _has_item_after_candidate(gate: _GateEntry, candidate_count: int) -> bool:
        return candidate_count < len(gate.queue)

    @staticmethod
    def _front_normal_run_latest_receipt_time(
        gate: _GateEntry,
        *,
        coalesce_normal_events: bool,
        debounce_seconds: float,
    ) -> float:
        if not gate.queue:
            return time.monotonic()
        latest_receipt_time = gate.queue[0].receipt_time
        if not coalesce_normal_events:
            return latest_receipt_time
        for queued in list(gate.queue)[1:]:
            if CoalescingGate._queued_kind(queued) is not QueueKind.NORMAL:
                break
            if queued.receipt_time > latest_receipt_time + debounce_seconds:
                break
            latest_receipt_time = queued.receipt_time
        return latest_receipt_time

    def _enqueue_path(self, kind: QueueKind) -> str:
        if kind is QueueKind.BYPASS:
            return "bypass"
        if self._debounce_seconds() <= 0:
            return "zero_debounce"
        return "debounce_schedule"

    def _log_enqueue(
        self,
        key: CoalescingKey,
        gate: _GateEntry,
        *,
        enqueue_start: float,
        path: str,
        source_kind: str,
    ) -> None:
        logger.debug(
            "coalescing_gate_enqueue",
            room_id=key.room_id,
            thread_id=key.thread_id,
            requester_user_id=key.requester_user_id,
            path=path,
            source_kind=source_kind,
            pending_count=self._gate_work_count(gate),
            oldest_pending_age_ms=self._oldest_pending_age_ms(gate),
            duration_ms=elapsed_ms_since(enqueue_start),
        )

    def _log_enqueued_event(
        self,
        key: CoalescingKey,
        pending_event: PendingEvent,
        *,
        pending_count: int,
    ) -> None:
        logger.info(
            "coalescing_gate_message_enqueued",
            room_id=key.room_id,
            thread_id=key.thread_id,
            requester_user_id=key.requester_user_id,
            event_id=pending_event.event.event_id,
            pending_count=pending_count,
            source_kind=pending_event.source_kind,
            timing_scope=event_timing_scope(pending_event.event.event_id),
        )

    def _flush_diagnostics(
        self,
        key: CoalescingKey,
        pending_events: list[PendingEvent],
        *,
        bypass_grace: bool,
    ) -> _FlushDiagnostics:
        batch = build_coalesced_batch(key, pending_events)
        pending_count = len(pending_events)
        timing_scope = event_timing_scope(batch.primary_event.event_id)
        return _FlushDiagnostics(
            batch=batch,
            pending_count=pending_count,
            timing_scope=timing_scope,
            log_context={
                "room_id": key.room_id,
                "thread_id": key.thread_id,
                "requester_user_id": key.requester_user_id,
                "pending_count": pending_count,
                "oldest_pending_age_ms": self._oldest_pending_events_age_ms(pending_events),
                "bypass_grace": bypass_grace,
                "source_event_ids": self._source_event_ids(pending_events),
                "timing_scope": timing_scope,
            },
        )

    @staticmethod
    def _log_flush_finished(
        flush_context: dict[str, object],
        *,
        flush_start: float,
        outcome: str,
    ) -> None:
        duration_ms = elapsed_ms_since(flush_start)
        log_context = {
            **flush_context,
            "duration_ms": duration_ms,
            "outcome": outcome,
        }
        if duration_ms >= _COALESCING_FLUSH_WARNING_SECONDS * 1000:
            logger.warning("coalescing_gate_flush_slow", **log_context)
            return
        logger.info("coalescing_gate_flush_finished", **log_context)

    def _ensure_drain_task(self, key: CoalescingKey, gate: _GateEntry) -> None:
        if gate.drain_task is not None and not gate.drain_task.done():
            return
        gate.drain_task = asyncio.create_task(
            self._drain_gate(key, gate),
            name=f"coalescing_drain:{key.room_id}:{key.thread_id or 'room'}:{key.requester_user_id}",
        )

    def _schedule_drain(self, key: CoalescingKey, gate: _GateEntry) -> None:
        self._ensure_drain_task(key, gate)
        self._wake(gate)

    @staticmethod
    def _wake(gate: _GateEntry) -> None:
        gate.wake_generation += 1
        gate.wake_event.set()

    def _record_enqueue(
        self,
        key: CoalescingKey,
        gate: _GateEntry,
        pending_event: PendingEvent,
        enqueue_start: float,
        *,
        path: str,
        flush_outcome: str | None = None,
    ) -> None:
        self._log_enqueued_event(
            key,
            pending_event,
            pending_count=self._gate_work_count(gate),
        )
        self._log_enqueue(
            key,
            gate,
            enqueue_start=enqueue_start,
            path=path,
            source_kind=pending_event.source_kind,
        )
        emit_elapsed_timing(
            "coalescing_gate.enqueue",
            enqueue_start,
            path=path,
            source_kind=pending_event.source_kind,
            pending_count=self._gate_work_count(gate),
            flush_outcome=flush_outcome,
            oldest_pending_age_ms=self._oldest_pending_age_ms(gate),
            timing_scope=event_timing_scope(pending_event.event.event_id),
        )

    def _busy_conversation_key(self, key: CoalescingKey, ready_result: ReadyPendingEvent) -> CoalescingKey:
        """Reroute one admission to its conversation's follow-up queue while a response runs."""
        if is_active_follow_up_coalescing_key(key) or not self._conversation_is_busy(key):
            return key
        pending_event = ready_result.pending_event
        if pending_event.dispatch_policy_source_kind is None and not is_coalescing_exempt_source_kind(
            pending_event.event,
            pending_event.source_kind,
        ):
            pending_event.dispatch_policy_source_kind = ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND
        return active_follow_up_coalescing_key(key.room_id, key.thread_id)

    async def admit(
        self,
        key: CoalescingKey,
        *,
        ready_result: ReadyPendingEvent,
        received_at: float | None = None,
        receipt_time: float | None = None,
        source_event_id: str | None = None,
        source_kind: str = "pending",
    ) -> None:
        """Admit one ready, conversation-assigned event under its coalescing key.

        The busy-conversation check and the enqueue happen synchronously on the
        event loop, so an admission can never race a response start or finish.
        """
        enqueue_start = time.monotonic()
        key = self._busy_conversation_key(key, ready_result)
        gate = self._get_or_create_gate(key)
        admission = _QueuedEvent(
            received_at=received_at if received_at is not None else time.time(),
            receipt_time=receipt_time if receipt_time is not None else time.monotonic(),
            source_event_id=source_event_id,
            source_kind=source_kind,
            ready_result=ready_result,
        )
        self._insert_queued_event(gate, admission)
        self._schedule_drain(key, gate)
        kind = self._queued_kind(admission)
        path = self._enqueue_path(kind)
        self._record_enqueue(
            key,
            gate,
            ready_result.pending_event,
            enqueue_start,
            path=path,
            flush_outcome="scheduled_drain" if path == "zero_debounce" else None,
        )

    async def drain_all(self, *, ready_timeout_seconds: float | None = None) -> CoalescingDrainResult:
        """Flush every active gate and await owned drain tasks."""
        drain_context = _DrainContext(
            ready_timeout_seconds=ready_timeout_seconds,
            result=_MutableDrainResult(),
        )
        return await _CoalescingDrainCoordinator(self, drain_context).run()

    def _upload_grace_hard_cap_seconds(self) -> float:
        grace_seconds = max(self._upload_grace_seconds(), 0.0)
        return max(
            grace_seconds,
            min(grace_seconds * _UPLOAD_GRACE_HARD_CAP_MULTIPLIER, _UPLOAD_GRACE_MAX_HARD_CAP_SECONDS),
        )

    async def _wait_for_deadline(self, gate: _GateEntry, deadline: float) -> bool:
        """Return True when ingress woke the drain before the deadline."""
        while True:
            delay = deadline - time.monotonic()
            if delay <= 0:
                return False
            wake_generation = gate.wake_generation
            gate.wake_event.clear()
            if gate.deadline != deadline or gate.wake_generation != wake_generation:
                return True
            try:
                await asyncio.wait_for(gate.wake_event.wait(), timeout=delay)
            except TimeoutError:
                return False
            else:
                return True

    async def _wait_for_debounce(
        self,
        gate: _GateEntry,
        *,
        coalesce_normal_events: Callable[[], bool],
    ) -> _DebounceWaitResult:
        """Wait for the normal debounce window, returning early when a barrier appears."""
        gate.phase = GatePhase.DEBOUNCE
        gate.grace_deadline = None
        if not gate.queue:
            gate.deadline = time.monotonic()
            return _DebounceWaitResult(quiet_deadline=gate.deadline)
        debounce_seconds = max(self._debounce_seconds(), 0.0)
        if debounce_seconds <= 0 or self._is_shutting_down() or gate.drain_all_requested:
            gate.deadline = time.monotonic()
            return _DebounceWaitResult(quiet_deadline=gate.deadline)
        quiet_deadline = (
            self._front_normal_run_latest_receipt_time(
                gate,
                coalesce_normal_events=coalesce_normal_events(),
                debounce_seconds=debounce_seconds,
            )
            + debounce_seconds
        )
        if self._has_barrier_after_front_normal_run(
            gate,
            coalesce_normal_events=coalesce_normal_events(),
        ):
            gate.deadline = time.monotonic()
            return _DebounceWaitResult(quiet_deadline=quiet_deadline)
        gate.deadline = quiet_deadline
        while True:
            deadline = gate.deadline or time.monotonic()
            if not await self._wait_for_deadline(gate, deadline):
                return _DebounceWaitResult(quiet_deadline=quiet_deadline)
            if (
                self._is_shutting_down()
                or gate.drain_all_requested
                or self._has_barrier_after_front_normal_run(
                    gate,
                    coalesce_normal_events=coalesce_normal_events(),
                )
            ):
                return _DebounceWaitResult(quiet_deadline=quiet_deadline)
            quiet_deadline = (
                self._front_normal_run_latest_receipt_time(
                    gate,
                    coalesce_normal_events=coalesce_normal_events(),
                    debounce_seconds=debounce_seconds,
                )
                + debounce_seconds
            )
            gate.deadline = quiet_deadline

    async def _wait_for_upload_grace(
        self,
        gate: _GateEntry,
        candidate_count: int,
        *,
        timing_scope: str,
    ) -> _UploadGraceWaitResult:
        """Wait for late media without removing the candidate batch from the queue."""
        grace_seconds = max(self._upload_grace_seconds(), 0.0)
        if grace_seconds <= 0 or self._is_shutting_down() or gate.drain_all_requested:
            return _UploadGraceWaitResult(candidate_count=candidate_count, quiet_deadline=time.monotonic())
        gate.phase = GatePhase.GRACE
        gate.grace_deadline = time.monotonic() + self._upload_grace_hard_cap_seconds()
        gate.deadline = time.monotonic() + min(grace_seconds, self._upload_grace_hard_cap_seconds())
        candidate_count = self._extend_candidate_with_grace_media(gate, candidate_count)
        if self._has_item_after_candidate(gate, candidate_count):
            return _UploadGraceWaitResult(
                candidate_count=candidate_count,
                quiet_deadline=gate.deadline,
            )
        grace_start = time.monotonic()
        emit_elapsed_timing(
            "coalescing_gate.flush",
            grace_start,
            outcome="scheduled_grace",
            pending_count=candidate_count,
            oldest_pending_age_ms=self._oldest_pending_age_ms(gate),
            timing_scope=timing_scope,
        )
        while True:
            deadline = gate.deadline or time.monotonic()
            woke = await self._wait_for_deadline(gate, deadline)
            candidate_count = self._extend_candidate_with_grace_media(gate, candidate_count)
            if (
                self._is_shutting_down()
                or gate.drain_all_requested
                or self._has_item_after_candidate(gate, candidate_count)
                or not woke
            ):
                return _UploadGraceWaitResult(
                    candidate_count=candidate_count,
                    quiet_deadline=deadline,
                )
            remaining_seconds = max((gate.grace_deadline or time.monotonic()) - time.monotonic(), 0.0)
            if remaining_seconds <= 0:
                return _UploadGraceWaitResult(
                    candidate_count=candidate_count,
                    quiet_deadline=deadline,
                )
            gate.deadline = time.monotonic() + min(grace_seconds, remaining_seconds)

    @staticmethod
    def _front_admissions_allow_room_scope_coalescing(gate: _GateEntry) -> bool:
        """Return whether front normal admissions allow room-level batching policy."""
        for queued in gate.queue:
            if CoalescingGate._queued_kind(queued) is not QueueKind.NORMAL:
                return False
            if source_or_event_allows_room_scope_batching(queued.source_kind):
                return True
            if source_or_event_allows_room_scope_batching(
                queued.pending_event.source_kind,
                queued.pending_event.event,
            ):
                return True
        return False

    def _should_coalesce_normal_events(self, key: CoalescingKey, gate: _GateEntry) -> bool:
        if key.thread_id is not None:
            return True
        if self._room_scope_is_single_conversation is not None and self._room_scope_is_single_conversation(
            key.room_id,
        ):
            return True
        return self._front_admissions_allow_room_scope_coalescing(gate)

    @staticmethod
    def _should_wait_for_upload_grace(candidate_events: list[PendingEvent]) -> bool:
        return pending_has_only_text(candidate_events) and not pending_has_room_scope_source(candidate_events)

    def _log_dispatch_failure(
        self,
        key: CoalescingKey,
        gate: _GateEntry,
        error: Exception,
    ) -> None:
        logger.exception(
            "Coalescing drain failed",
            room_id=key.room_id,
            thread_id=key.thread_id,
            requester_user_id=key.requester_user_id,
            pending_count=self._gate_work_count(gate),
            oldest_pending_age_ms=self._oldest_pending_age_ms(gate),
            exception_type=error.__class__.__name__,
            error_message="Coalesced dispatch failed.",
        )

    async def _dispatch_events(
        self,
        key: CoalescingKey,
        gate: _GateEntry,
        pending_events: list[PendingEvent],
        *,
        bypass_grace: bool,
    ) -> str:
        """Dispatch a claimed batch."""
        flush_start = time.monotonic()
        gate.phase = GatePhase.IN_FLIGHT
        gate.deadline = None
        gate.grace_deadline = None
        pending_count = len(pending_events)
        timing_scope = event_timing_scope(pending_events[-1].event.event_id)
        log_context: dict[str, object] = {
            "room_id": key.room_id,
            "thread_id": key.thread_id,
            "requester_user_id": key.requester_user_id,
            "pending_count": pending_count,
            "oldest_pending_age_ms": self._oldest_pending_events_age_ms(pending_events),
            "bypass_grace": bypass_grace,
            "source_event_ids": self._source_event_ids(pending_events),
            "timing_scope": timing_scope,
        }
        dispatched = False
        try:
            diagnostics = self._flush_diagnostics(key, pending_events, bypass_grace=bypass_grace)
            pending_count = diagnostics.pending_count
            timing_scope = diagnostics.timing_scope
            log_context = diagnostics.log_context
            logger.info("coalescing_gate_flush_started", **log_context)
            dispatch_batch_start = time.monotonic()
            await self._dispatch_batch(diagnostics.batch)
            dispatched = True
            emit_elapsed_timing(
                "coalescing_gate.flush.dispatch_batch",
                dispatch_batch_start,
                pending_count=pending_count,
                bypass_grace=bypass_grace,
                timing_scope=timing_scope,
            )
            return "dispatched"
        finally:
            outcome = "dispatched" if dispatched else "failed"
            emit_elapsed_timing(
                "coalescing_gate.flush",
                flush_start,
                outcome=outcome,
                pending_count=pending_count,
                bypass_grace=bypass_grace,
                timing_scope=timing_scope,
            )
            self._log_flush_finished(
                log_context,
                flush_start=flush_start,
                outcome=outcome,
            )
            gate.phase = GatePhase.DEBOUNCE
            gate.grace_deadline = None
            gate.deadline = None

    async def _dispatch_claimed_events(
        self,
        key: CoalescingKey,
        gate: _GateEntry,
        segment_owner: ClaimedSegmentOwner,
        *,
        bypass_grace: bool,
    ) -> None:
        try:
            await self._dispatch_events(key, gate, segment_owner.pending_events, bypass_grace=bypass_grace)
        except asyncio.CancelledError:
            segment_owner.close_metadata_once()
            if (drain_context := self._current_drain_context(gate)) is not None:
                drain_context.result.dispatch_cancelled_count += 1
            raise
        except Exception as error:
            segment_owner.close_metadata_once()
            if (drain_context := self._current_drain_context(gate)) is not None:
                drain_context.result.dispatch_failure_count += 1
            self._log_dispatch_failure(key, gate, error)

    async def _dispatch_after_upload_grace(
        self,
        key: CoalescingKey,
        gate: _GateEntry,
        grace_result: _UploadGraceWaitResult,
    ) -> None:
        """Restart a claim after upload grace, including same-window unresolved ingress."""
        if not gate.queue:
            return

        if not is_active_follow_up_coalescing_key(key):
            window_slots = self._lanes.undelivered_in_window(
                key.room_id,
                key.requester_user_id,
                before_or_at_receipt_time=grace_result.quiet_deadline,
            )
            if window_slots:
                await self._wait_for_lane_slots(gate, window_slots)
                return

        claimable_count = self._front_normal_run_length(
            gate,
            coalesce_normal_events=self._should_coalesce_normal_events(key, gate),
        )
        candidate_count = min(grace_result.candidate_count, claimable_count)
        if candidate_count <= 0:
            return

        next_admissions = self._claim_front_events(gate, candidate_count)
        await self._dispatch_claim(
            key,
            gate,
            next_admissions,
            bypass_grace=True,
            allow_upload_grace=False,
        )

    async def _dispatch_claim(
        self,
        key: CoalescingKey,
        gate: _GateEntry,
        admissions: list[_QueuedEvent],
        *,
        bypass_grace: bool,
        allow_upload_grace: bool,
    ) -> None:
        """Dispatch one claimed admission set with one cleanup owner."""
        pending_events = [admission.pending_event for admission in admissions]
        segment_owner: ClaimedSegmentOwner | None = None
        try:
            if (
                allow_upload_grace
                and pending_events
                and not pending_events_require_solo_batch(pending_events)
                and self._should_wait_for_upload_grace(pending_events)
            ):
                timing_scope = event_timing_scope(pending_events[-1].event.event_id)
                self._requeue_claimed_admissions(gate, admissions)
                grace_result = await self._wait_for_upload_grace(
                    gate,
                    len(admissions),
                    timing_scope=timing_scope,
                )
                await self._dispatch_after_upload_grace(key, gate, grace_result)
                return
            segment_owner = ClaimedSegmentOwner(pending_events=pending_events)
            await self._dispatch_claimed_events(
                key,
                gate,
                segment_owner,
                bypass_grace=bypass_grace,
            )
        except BaseException:
            if segment_owner is not None:
                closed_before = segment_owner.metadata_closed
                segment_owner.close_metadata_once()
                if not closed_before and (drain_context := self._current_drain_context(gate)) is not None:
                    drain_context.result.dropped_ready_count += len(segment_owner.pending_events)
            raise
        finally:
            self._clear_claimed_admissions(gate, admissions)

    async def _dispatch_front_barrier(
        self,
        key: CoalescingKey,
        gate: _GateEntry,
        front_kind: QueueKind,
    ) -> bool:
        if front_kind is not QueueKind.BYPASS:
            return False
        claimed_admissions = self._claim_front_events(gate, 1)
        await self._dispatch_claim(
            key,
            gate,
            claimed_admissions,
            bypass_grace=True,
            allow_upload_grace=False,
        )
        return True

    async def _dispatch_normal_after_debounce(
        self,
        key: CoalescingKey,
        gate: _GateEntry,
        debounce_result: _DebounceWaitResult,
    ) -> None:
        if not is_active_follow_up_coalescing_key(key):
            window_slots = self._lanes.undelivered_in_window(
                key.room_id,
                key.requester_user_id,
                before_or_at_receipt_time=debounce_result.quiet_deadline,
            )
            if window_slots:
                await self._wait_for_lane_slots(gate, window_slots)
                return

        bypass_grace = self._is_shutting_down() or gate.drain_all_requested
        candidate_count = self._front_normal_run_length(
            gate,
            coalesce_normal_events=self._should_coalesce_normal_events(key, gate),
            max_receipt_time=debounce_result.quiet_deadline,
        )
        if candidate_count == 0:
            return

        claimed_admissions = self._claim_front_events(gate, candidate_count)
        await self._dispatch_claim(
            key,
            gate,
            claimed_admissions,
            bypass_grace=bypass_grace,
            allow_upload_grace=not bypass_grace and self._upload_grace_seconds() > 0,
        )
        if not gate.queue:
            gate.drain_all_requested = False

    async def _dispatch_active_follow_up_backlog(self, key: CoalescingKey, gate: _GateEntry) -> bool:
        """Dispatch the post-idle active-response backlog as one receive-ordered batch."""
        if not is_active_follow_up_coalescing_key(key):
            return False
        front = gate.queue[0]
        if self._queued_kind(front) is not QueueKind.NORMAL:
            return False

        candidate_count = self._front_normal_run_length(
            gate,
            coalesce_normal_events=True,
        )
        if candidate_count == 0:
            return False

        claimed_admissions = self._claim_front_events(gate, candidate_count)
        await self._dispatch_claim(
            key,
            gate,
            claimed_admissions,
            bypass_grace=True,
            allow_upload_grace=False,
        )
        return True

    async def _drain_gate_iteration(self, key: CoalescingKey, gate: _GateEntry) -> None:
        drain_context = self._current_drain_context(gate)
        if not (self._is_shutting_down() or self._is_bounded_drain(drain_context)):
            await self._wait_until_dispatch_allowed(key)
        if not gate.queue:
            return

        front = gate.queue[0]
        if await self._dispatch_front_barrier(key, gate, self._queued_kind(front)):
            return

        if await self._dispatch_active_follow_up_backlog(key, gate):
            return

        debounce_result = await self._wait_for_debounce(
            gate,
            coalesce_normal_events=lambda key=key, entry=gate: self._should_coalesce_normal_events(key, entry),
        )
        if not gate.queue:
            return
        await self._dispatch_normal_after_debounce(key, gate, debounce_result)

    async def _drain_gate_loop(self, key: CoalescingKey, gate: _GateEntry) -> None:
        while True:
            if not gate.queue:
                self._remove_gate(key)
                return
            await self._drain_gate_iteration(key, gate)

    def _finish_gate_drain(
        self,
        key: CoalescingKey,
        *,
        outcome: str,
        drain_start: float,
    ) -> None:
        current_gate = self._gates.get(key)
        if current_gate is not None:
            try:
                current_task = asyncio.current_task()
            except RuntimeError:
                current_task = None
            if current_gate.drain_task is current_task:
                current_gate.drain_task = None
            if self._gate_work_count(current_gate) == 0:
                self._remove_gate(key)
            elif outcome in {"failed", "cancelled"} and not self._is_shutting_down():
                self._ensure_drain_task(key, current_gate)
                self._wake(current_gate)
        logger.debug(
            "coalescing_drain_finish",
            room_id=key.room_id,
            thread_id=key.thread_id,
            requester_user_id=key.requester_user_id,
            outcome=outcome,
            pending_count=self._gate_work_count(current_gate) if current_gate is not None else 0,
            oldest_pending_age_ms=self._oldest_pending_age_ms(current_gate) if current_gate is not None else None,
            duration_ms=elapsed_ms_since(drain_start),
        )

    async def _drain_gate(self, key: CoalescingKey, gate: _GateEntry) -> None:
        """Own debounce, grace, and dispatch for one coalescing key."""
        drain_start = time.monotonic()
        outcome = "finished"
        logger.debug(
            "coalescing_drain_start",
            room_id=key.room_id,
            thread_id=key.thread_id,
            requester_user_id=key.requester_user_id,
            pending_count=self._gate_work_count(gate),
            oldest_pending_age_ms=self._oldest_pending_age_ms(gate),
        )
        try:
            await self._drain_gate_loop(key, gate)
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception as error:
            outcome = "failed"
            if (drain_context := self._current_drain_context(gate)) is not None:
                drain_context.result.dispatch_failure_count += 1
            self._log_dispatch_failure(key, gate, error)
        finally:
            self._finish_gate_drain(key, outcome=outcome, drain_start=drain_start)


@dataclass
class _CoalescingDrainCoordinator:
    """Own one drain_all run across lanes and active gate drains."""

    gate: CoalescingGate
    context: _DrainContext

    def _prepare_gate(self, gate: _GateEntry) -> None:
        gate.drain_context = self.context
        gate.drain_all_requested = True
        gate.deadline = time.monotonic()
        gate.grace_deadline = None

    def _cancel_non_in_flight_drain_tasks(self) -> list[asyncio.Task[None]]:
        if not self.gate._is_bounded_drain(self.context):
            return []
        cancelled_tasks: list[asyncio.Task[None]] = []
        for gate in self.gate._gates.values():
            task = gate.drain_task
            if task is None or task.done() or gate.phase is GatePhase.IN_FLIGHT:
                continue
            task.cancel()
            cancelled_tasks.append(task)
        return cancelled_tasks

    def _active_drain_tasks(self) -> list[asyncio.Task[None]]:
        return [
            gate.drain_task
            for gate in self.gate._gates.values()
            if gate.drain_task is not None and not gate.drain_task.done()
        ]

    async def _abandon_gate_work_for_bounded_shutdown(self) -> None:
        if not self.gate._is_bounded_drain(self.context):
            return
        dropped_ready_count = 0
        for gate in self.gate._gates.values():
            if gate.drain_task is not None and not gate.drain_task.done():
                gate.drain_task.cancel()
                self.context.result.dispatch_cancelled_count += 1
                gate.drain_task = None
            admissions = [*gate.claimed_admissions, *gate.queue]
            for queued in admissions:
                close_pending_event_metadata_once([queued.pending_event])
                dropped_ready_count += 1
            gate.claimed_admissions = []
            gate.queue.clear()
            gate.drain_all_requested = False
        if dropped_ready_count:
            self.context.result.dropped_ready_count += dropped_ready_count

    async def _drain_lanes(self) -> None:
        while True:
            slots = self.gate.lanes.unsettled_slots()
            if not slots:
                return
            if not self.gate._is_bounded_drain(self.context):
                await asyncio.gather(*(slot.settled.wait() for slot in slots))
                continue
            try:
                await asyncio.wait_for(
                    asyncio.gather(*(slot.settled.wait() for slot in slots)),
                    timeout=self.context.ready_timeout_seconds,
                )
            except TimeoutError:
                await self.gate._abandon_lane_slots(slots, self.context)
                return

    async def _await_active_drain_tasks(self) -> tuple[bool, bool]:
        """Await active drains; return (abandoned, still_pending)."""
        tasks_to_await = self._active_drain_tasks()
        if not tasks_to_await:
            return False, False
        if not self.gate._is_bounded_drain(self.context):
            await asyncio.gather(*tasks_to_await, return_exceptions=True)
            return False, False
        done, pending = await asyncio.wait(tasks_to_await, timeout=self.context.ready_timeout_seconds)
        if done:
            await asyncio.gather(*done, return_exceptions=True)
        if not pending:
            return False, False
        if not any(
            gate.drain_task in pending and gate.phase is GatePhase.IN_FLIGHT for gate in self.gate._gates.values()
        ):
            return False, True
        await self._abandon_gate_work_for_bounded_shutdown()
        return True, False

    async def _drain_once(self) -> bool:
        await self._drain_lanes()
        for gate in list(self.gate._gates.values()):
            self._prepare_gate(gate)

        cancelled_tasks = [] if self.context.cancelled_initial_drain_tasks else self._cancel_non_in_flight_drain_tasks()
        self.context.cancelled_initial_drain_tasks = True
        if cancelled_tasks:
            await asyncio.gather(*cancelled_tasks, return_exceptions=True)

        for key, gate in list(self.gate._gates.items()):
            self._prepare_gate(gate)
            self.gate._ensure_drain_task(key, gate)
            self.gate._wake(gate)

        abandoned, active_pending = await self._await_active_drain_tasks()
        if abandoned:
            return True
        if active_pending:
            return False
        return self.gate.lanes.all_settled()

    def _clear_context(self) -> None:
        for gate in self.gate._gates.values():
            if gate.drain_context is self.context:
                gate.drain_context = None
        if self.gate._active_drain_context is self.context:
            self.gate._active_drain_context = None

    async def run(self) -> CoalescingDrainResult:
        self.gate._active_drain_context = self.context
        drain_completed = False
        try:
            while True:
                if await self._drain_once():
                    break
            await self._abandon_gate_work_for_bounded_shutdown()
            drain_completed = True
            return self.context.result.freeze()
        finally:
            self._clear_context()
            if drain_completed:
                self.gate._gates.clear()
