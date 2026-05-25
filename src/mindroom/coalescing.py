"""Live message coalescing gate."""

from __future__ import annotations

import asyncio
import enum
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import nio

from .coalescing_batch import (
    CoalescedBatch,
    CoalescingKey,
    PendingEvent,
    build_coalesced_batch,
    close_pending_event_metadata,
)
from .commands.parsing import command_parser
from .dispatch_handoff import DispatchEvent, PreparedTextEvent, is_media_dispatch_event
from .dispatch_source import (
    ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
    HOOK_DISPATCH_SOURCE_KIND,
    HOOK_SOURCE_KIND,
    IMAGE_SOURCE_KIND,
    MEDIA_SOURCE_KIND,
    SCHEDULED_SOURCE_KIND,
    TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
    VOICE_SOURCE_KIND,
    is_voice_event,
)
from .logging_config import get_logger
from .timing import elapsed_ms_since, emit_elapsed_timing, event_timing_scope

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

__all__ = [
    "CoalescingGate",
    "GatePhase",
    "IngressAdmissionClosedError",
    "IngressOrderReservation",
    "ReadyPendingEvent",
    "close_ready_task_result_metadata",
    "is_coalescing_exempt_source_kind",
]

_UPLOAD_GRACE_HARD_CAP_MULTIPLIER = 4.0
_UPLOAD_GRACE_MAX_HARD_CAP_SECONDS = 2.0
_COALESCING_FLUSH_WARNING_SECONDS = 5.0
_COALESCING_EXEMPT_SOURCE_KINDS: frozenset[str] = frozenset(
    {
        HOOK_SOURCE_KIND,
        HOOK_DISPATCH_SOURCE_KIND,
        SCHEDULED_SOURCE_KIND,
        ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
        TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
    },
)
logger = get_logger(__name__)


class GatePhase(enum.Enum):
    """Lifecycle phases for one coalescing gate."""

    DEBOUNCE = "debounce"
    GRACE = "grace"
    IN_FLIGHT = "in_flight"


class _QueueKind(enum.Enum):
    """Dispatch behavior for one queued event."""

    NORMAL = "normal"
    COMMAND = "command"
    BYPASS = "bypass"


class IngressAdmissionClosedError(RuntimeError):
    """Raised when ingress tries to admit a released reservation."""


@dataclass
class _QueuedEvent:
    admission_key: CoalescingKey
    received_order: int
    received_at: float
    source_event_id: str | None
    source_kind: str
    ready_task: asyncio.Task[ReadyPendingEvent | None] | None
    ready_result: ReadyPendingEvent | None = None

    @property
    def pending_event(self) -> PendingEvent:
        """Return the resolved pending event for ready-only test introspection."""
        if self.ready_result is None:
            msg = "Queued admission has not resolved to a pending event"
            raise RuntimeError(msg)
        return self.ready_result.pending_event


@dataclass
class IngressOrderReservation:
    """Receive-order placeholder for ingress that must resolve its canonical key first."""

    room_id: str
    requester_user_id: str
    received_order: int
    receipt_time: float
    released: bool = False
    settled: asyncio.Event = field(default_factory=asyncio.Event, repr=False, compare=False)
    _release: Callable[[IngressOrderReservation], None] | None = field(default=None, repr=False, compare=False)

    def release(self) -> None:
        """Release this reservation if it will not be admitted."""
        if self._release is None:
            if self.released:
                return
            self.released = True
            self.settled.set()
            return
        self._release(self)


@dataclass(frozen=True)
class ReadyPendingEvent:
    """Resolved event returned by async ingress normalization."""

    pending_event: PendingEvent


def close_ready_task_result_metadata(result: object) -> int:
    """Close dispatch metadata for a ready-task result and report closures."""
    if isinstance(result, ReadyPendingEvent):
        close_pending_event_metadata([result.pending_event])
        return 1
    return 0


@dataclass(frozen=True)
class _ReadyAdmission:
    """One receive-time admission resolved to a dispatchable pending event."""

    admission_key: CoalescingKey
    ready_event: ReadyPendingEvent

    @property
    def key(self) -> CoalescingKey:
        return self.admission_key

    @property
    def pending_event(self) -> PendingEvent:
        return self.ready_event.pending_event


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
    before_order: int | None = None


def _effective_source_kind(
    event: DispatchEvent,
    fallback_source_kind: str | None = None,
) -> str | None:
    if fallback_source_kind is not None:
        return fallback_source_kind
    if isinstance(event, PreparedTextEvent) and event.source_kind_override is not None:
        return event.source_kind_override
    return None


def is_coalescing_exempt_source_kind(
    event: DispatchEvent,
    fallback_source_kind: str | None = None,
) -> bool:
    """Return True when coalescing should be skipped for this event."""
    return _effective_source_kind(event, fallback_source_kind) in _COALESCING_EXEMPT_SOURCE_KINDS


def _is_command_event(
    event: DispatchEvent,
    *,
    fallback_source_kind: str | None = None,
) -> bool:
    """Return whether a dispatch event should bypass coalescing as a command."""
    if not isinstance(event, nio.RoomMessageText | PreparedTextEvent):
        return False
    if fallback_source_kind == VOICE_SOURCE_KIND or is_voice_event(event):
        return False
    if _effective_source_kind(event, fallback_source_kind) in {IMAGE_SOURCE_KIND, MEDIA_SOURCE_KIND}:
        return False
    return command_parser.parse(event.body) is not None


def _pending_has_only_text(pending_events: list[PendingEvent]) -> bool:
    return bool(pending_events) and all(
        isinstance(pending_event.event, nio.RoomMessageText | PreparedTextEvent) for pending_event in pending_events
    )


def _pending_has_voice_source(pending_events: list[PendingEvent]) -> bool:
    return any(
        pending_event.source_kind == VOICE_SOURCE_KIND or is_voice_event(pending_event.event)
        for pending_event in pending_events
    )


class CoalescingGate:
    """Debounce/grace state machine for live inbound message batching.

    State machine per (room, thread, sender) key:
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
    ) -> None:
        self._dispatch_batch = dispatch_batch
        self._debounce_seconds = debounce_seconds
        self._upload_grace_seconds = upload_grace_seconds
        self._is_shutting_down = is_shutting_down
        self._gates: dict[CoalescingKey, _GateEntry] = {}
        self._order_reservations: list[IngressOrderReservation] = []
        self._next_received_order = 0

    def _remove_gate(self, key: CoalescingKey) -> None:
        self._gates.pop(key, None)
        self._wake_owner_gates(key)

    def _get_or_create_gate(self, key: CoalescingKey) -> _GateEntry:
        gate = self._gates.get(key)
        if gate is None:
            gate = _GateEntry()
            self._gates[key] = gate
        return gate

    @staticmethod
    def _gate_work_count(gate: _GateEntry) -> int:
        return len(gate.queue) + len(gate.claimed_admissions)

    @staticmethod
    def _same_owner(left: CoalescingKey, right: CoalescingKey) -> bool:
        return left.room_id == right.room_id and left.requester_user_id == right.requester_user_id

    @staticmethod
    def _reservation_matches_key(reservation: IngressOrderReservation, key: CoalescingKey) -> bool:
        return reservation.room_id == key.room_id and reservation.requester_user_id == key.requester_user_id

    def _next_order(self) -> int:
        self._next_received_order += 1
        return self._next_received_order

    def _wake_owner(self, room_id: str, requester_user_id: str) -> None:
        for gate_key, gate in self._gates.items():
            if gate_key.room_id == room_id and gate_key.requester_user_id == requester_user_id:
                self._wake(gate)

    def _wake_owner_gates(self, key: CoalescingKey) -> None:
        for other_key, other_gate in self._gates.items():
            if other_key != key and self._same_owner(other_key, key):
                self._wake(other_gate)

    def _older_owner_reservations(
        self,
        key: CoalescingKey,
        *,
        before_order: int,
    ) -> list[IngressOrderReservation]:
        return [
            reservation
            for reservation in self._order_reservations
            if not reservation.released
            and self._reservation_matches_key(reservation, key)
            and reservation.received_order < before_order
        ]

    async def _wait_until_front_claimable(
        self,
        key: CoalescingKey,
        gate: _GateEntry,
        *,
        front_order: int,
    ) -> None:
        await self._wait_for_reservations(
            gate,
            self._older_owner_reservations(key, before_order=front_order),
        )

    async def _wait_for_reservations(
        self,
        gate: _GateEntry,
        reservations: list[IngressOrderReservation],
    ) -> None:
        _ = gate
        unsettled = [reservation for reservation in reservations if not reservation.released]
        if not unsettled:
            return
        await asyncio.gather(*(reservation.settled.wait() for reservation in unsettled))

    def _unsettled_owner_reservations_in_window(
        self,
        key: CoalescingKey,
        *,
        after_order: int,
        before_order: int | None,
        before_or_at_receipt_time: float,
    ) -> list[IngressOrderReservation]:
        return [
            reservation
            for reservation in self._order_reservations
            if not reservation.released
            and self._reservation_matches_key(reservation, key)
            and reservation.received_order > after_order
            and (before_order is None or reservation.received_order < before_order)
            and reservation.receipt_time <= before_or_at_receipt_time
        ]

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
    def _queue_kind(pending_event: PendingEvent) -> _QueueKind:
        if any(item.requires_solo_batch for item in pending_event.dispatch_metadata):
            return _QueueKind.BYPASS
        if is_coalescing_exempt_source_kind(pending_event.event, pending_event.source_kind):
            return _QueueKind.BYPASS
        if _is_command_event(pending_event.event, fallback_source_kind=pending_event.source_kind):
            return _QueueKind.COMMAND
        return _QueueKind.NORMAL

    @staticmethod
    def _queued_kind(queued: _QueuedEvent) -> _QueueKind:
        if queued.ready_result is None:
            return _QueueKind.NORMAL
        return CoalescingGate._queue_kind(queued.ready_result.pending_event)

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
            if admission.received_order < queued.received_order:
                gate.queue.insert(index, admission)
                return
        gate.queue.append(admission)

    def reserve_order(
        self,
        *,
        room_id: str,
        requester_user_id: str,
        receipt_time: float | None = None,
        received_at: float | None = None,
    ) -> IngressOrderReservation:
        """Reserve receive order before async work can resolve the final coalescing key."""
        _ = received_at
        reservation = IngressOrderReservation(
            room_id=room_id,
            requester_user_id=requester_user_id,
            received_order=self._next_order(),
            receipt_time=receipt_time if receipt_time is not None else time.monotonic(),
            _release=self.release_order_reservation,
        )
        self._order_reservations.append(reservation)
        self._wake_owner(room_id, requester_user_id)
        return reservation

    def release_order_reservation(self, reservation: IngressOrderReservation) -> None:
        """Release a receive-order reservation that will not become a queued admission."""
        self._release_order_reservation(reservation, wake=True)

    def _release_order_reservation(self, reservation: IngressOrderReservation, *, wake: bool) -> None:
        if reservation.released:
            return
        reservation.released = True
        for index, current_reservation in enumerate(self._order_reservations):
            if current_reservation is reservation:
                del self._order_reservations[index]
                break
        reservation.settled.set()
        if wake:
            self._wake_owner(reservation.room_id, reservation.requester_user_id)

    async def _wait_for_order_reservations(self) -> None:
        while True:
            reservations = [reservation for reservation in self._order_reservations if not reservation.released]
            if not reservations:
                return
            await asyncio.gather(*(reservation.settled.wait() for reservation in reservations))

    @staticmethod
    def _front_normal_run_length(
        gate: _GateEntry,
        *,
        coalesce_normal_events: bool,
        max_received_order: int | None = None,
    ) -> int:
        count = 0
        for queued in gate.queue:
            if max_received_order is not None and queued.received_order > max_received_order:
                break
            if CoalescingGate._queued_kind(queued) is not _QueueKind.NORMAL:
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
            if (
                CoalescingGate._queued_kind(queued) is not _QueueKind.NORMAL
                or queued.ready_result is None
                or not is_media_dispatch_event(queued.pending_event.event)
            ):
                break
            count += 1
        return count

    @staticmethod
    def _first_barrier_after_front_normal_run_order(
        gate: _GateEntry,
        *,
        coalesce_normal_events: bool,
    ) -> int | None:
        normal_count = CoalescingGate._front_normal_run_length(
            gate,
            coalesce_normal_events=coalesce_normal_events,
        )
        if normal_count < len(gate.queue):
            return gate.queue[normal_count].received_order
        return None

    @staticmethod
    def _has_item_after_candidate(gate: _GateEntry, candidate_count: int) -> bool:
        return candidate_count < len(gate.queue)

    def _enqueue_path(self, kind: _QueueKind) -> str:
        if kind is _QueueKind.BYPASS:
            return "bypass"
        if kind is _QueueKind.COMMAND:
            return "command_interrupt"
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

    async def admit(
        self,
        key: CoalescingKey,
        *,
        ready_task: asyncio.Task[ReadyPendingEvent | None] | None = None,
        received_at: float | None = None,
        source_event_id: str | None = None,
        source_kind: str = "pending",
        ready_result: ReadyPendingEvent | None = None,
        order_reservation: IngressOrderReservation | None = None,
    ) -> None:
        """Admit one Matrix ingress item under its stable coalescing key."""
        if ready_task is None and ready_result is None:
            msg = "ready_task is required when ready_result is not provided"
            raise ValueError(msg)
        if order_reservation is not None and order_reservation.released:
            msg = "Cannot admit a released ingress reservation"
            raise IngressAdmissionClosedError(msg)
        enqueue_start = time.monotonic()
        gate = self._get_or_create_gate(key)
        if order_reservation is None:
            received_order = self._next_order()
            resolved_received_at = received_at if received_at is not None else time.time()
        else:
            if not self._reservation_matches_key(order_reservation, key):
                msg = "Ingress order reservation owner must match admitted coalescing key"
                raise ValueError(msg)
            received_order = order_reservation.received_order
            resolved_received_at = received_at if received_at is not None else time.time()
            self._release_order_reservation(order_reservation, wake=False)
        admission = _QueuedEvent(
            admission_key=key,
            received_order=received_order,
            received_at=resolved_received_at,
            source_event_id=source_event_id,
            source_kind=source_kind,
            ready_task=ready_task,
            ready_result=ready_result,
        )
        self._insert_queued_event(gate, admission)
        self._schedule_drain(key, gate)
        self._wake_owner_gates(key)
        kind = self._queued_kind(admission)
        path = self._enqueue_path(kind)
        if ready_result is not None:
            self._record_enqueue(
                key,
                gate,
                ready_result.pending_event,
                enqueue_start,
                path=path,
                flush_outcome="scheduled_drain" if path == "zero_debounce" else None,
            )
            return
        self._log_enqueue(
            key,
            gate,
            enqueue_start=enqueue_start,
            path=path,
            source_kind=source_kind,
        )

    async def drain_all(self) -> None:
        """Flush every active gate and await owned drain tasks."""
        while True:
            await self._wait_for_order_reservations()
            for key, gate in list(self._gates.items()):
                gate.drain_all_requested = True
                gate.deadline = time.monotonic()
                gate.grace_deadline = None
                self._ensure_drain_task(key, gate)
                self._wake(gate)
            tasks_to_await = [
                gate.drain_task
                for gate in self._gates.values()
                if gate.drain_task is not None and not gate.drain_task.done()
            ]
            if tasks_to_await:
                await asyncio.gather(*tasks_to_await, return_exceptions=True)
            if not any(not reservation.released for reservation in self._order_reservations):
                break
        self._gates.clear()

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
        debounce_seconds = max(self._debounce_seconds(), 0.0)
        if debounce_seconds <= 0 or self._is_shutting_down() or gate.drain_all_requested:
            gate.deadline = time.monotonic()
            return _DebounceWaitResult(quiet_deadline=gate.deadline)
        barrier_order = self._first_barrier_after_front_normal_run_order(
            gate,
            coalesce_normal_events=coalesce_normal_events(),
        )
        if barrier_order is not None:
            gate.deadline = time.monotonic()
            return _DebounceWaitResult(quiet_deadline=gate.deadline, before_order=barrier_order)
        gate.deadline = time.monotonic() + debounce_seconds
        while True:
            deadline = gate.deadline or time.monotonic()
            if not await self._wait_for_deadline(gate, deadline):
                return _DebounceWaitResult(quiet_deadline=deadline)
            barrier_order = self._first_barrier_after_front_normal_run_order(
                gate,
                coalesce_normal_events=coalesce_normal_events(),
            )
            if self._is_shutting_down() or gate.drain_all_requested or barrier_order is not None:
                return _DebounceWaitResult(quiet_deadline=time.monotonic(), before_order=barrier_order)
            gate.deadline = time.monotonic() + debounce_seconds

    async def _wait_for_upload_grace(
        self,
        gate: _GateEntry,
        candidate_count: int,
        *,
        timing_scope: str,
    ) -> int:
        """Wait for late media without removing the candidate batch from the queue."""
        grace_seconds = max(self._upload_grace_seconds(), 0.0)
        if grace_seconds <= 0 or self._is_shutting_down() or gate.drain_all_requested:
            return candidate_count
        gate.phase = GatePhase.GRACE
        gate.grace_deadline = time.monotonic() + self._upload_grace_hard_cap_seconds()
        gate.deadline = time.monotonic() + min(grace_seconds, self._upload_grace_hard_cap_seconds())
        candidate_count = self._extend_candidate_with_grace_media(gate, candidate_count)
        if self._has_item_after_candidate(gate, candidate_count):
            return candidate_count
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
                return candidate_count
            remaining_seconds = max((gate.grace_deadline or time.monotonic()) - time.monotonic(), 0.0)
            if remaining_seconds <= 0:
                return candidate_count
            gate.deadline = time.monotonic() + min(grace_seconds, remaining_seconds)

    async def _resolve_queued_event(self, queued: _QueuedEvent) -> ReadyPendingEvent | None:
        """Return one ready result, logging normalization failures as skipped ingress."""
        if queued.ready_result is not None:
            return queued.ready_result
        if queued.ready_task is None:
            msg = "Queued admission has neither ready_result nor ready_task"
            raise RuntimeError(msg)
        try:
            queued.ready_result = await asyncio.shield(queued.ready_task)
        except asyncio.CancelledError:
            if queued.ready_task.cancelled():
                logger.warning(
                    "coalescing_gate_ready_task_cancelled",
                    source_event_id=queued.source_event_id,
                    received_order=queued.received_order,
                    age_ms=elapsed_ms_since(queued.received_at, clock=time.time),
                )
                return None
            raise
        except Exception as error:
            logger.exception(
                "coalescing_gate_ready_task_failed",
                source_event_id=queued.source_event_id,
                received_order=queued.received_order,
                age_ms=elapsed_ms_since(queued.received_at, clock=time.time),
                exception_type=error.__class__.__name__,
                error_message=str(error),
            )
            return None
        if queued.ready_result is not None:
            queued.ready_result.pending_event.enqueue_time = queued.received_at
        return queued.ready_result

    @staticmethod
    def _front_admissions_have_voice(gate: _GateEntry) -> bool:
        for queued in gate.queue:
            if CoalescingGate._queued_kind(queued) is not _QueueKind.NORMAL:
                break
            if queued.source_kind == VOICE_SOURCE_KIND:
                return True
            if queued.ready_result is not None and queued.ready_result.pending_event.source_kind == VOICE_SOURCE_KIND:
                return True
        return False

    @staticmethod
    def _should_coalesce_normal_events(key: CoalescingKey, gate: _GateEntry) -> bool:
        return key.thread_id is not None or CoalescingGate._front_admissions_have_voice(gate)

    async def _resolve_claimed_admissions(
        self,
        admissions: list[_QueuedEvent],
    ) -> list[_ReadyAdmission]:
        results = await asyncio.gather(*(self._resolve_queued_event(admission) for admission in admissions))
        return [
            _ReadyAdmission(admission_key=admission.admission_key, ready_event=result)
            for admission, result in zip(admissions, results, strict=True)
            if result is not None
        ]

    async def _resolve_claimed_admissions_or_requeue(
        self,
        key: CoalescingKey,
        gate: _GateEntry,
        admissions: list[_QueuedEvent],
    ) -> list[_ReadyAdmission]:
        try:
            ready_admissions = await self._resolve_claimed_admissions(admissions)
        except BaseException:
            self._requeue_claimed_admissions(gate, admissions)
            raise
        self._clear_claimed_admissions(gate, admissions)
        if len(ready_admissions) != len(admissions):
            self._wake_owner_gates(key)
        return ready_admissions

    @staticmethod
    def _key_for_ready_admission(
        ready_admissions: list[_ReadyAdmission],
        index: int,
    ) -> CoalescingKey:
        return ready_admissions[index].key

    @staticmethod
    def _can_merge_ready_segment(
        key: CoalescingKey,
        pending_events: list[PendingEvent],
        next_event: PendingEvent,
    ) -> bool:
        if key.thread_id is not None:
            return True
        if _pending_has_voice_source([*pending_events, next_event]):
            return True
        return any(is_media_dispatch_event(pending_event.event) for pending_event in [*pending_events, next_event])

    def _ready_admission_segments(
        self,
        ready_admissions: list[_ReadyAdmission],
    ) -> list[tuple[CoalescingKey, list[PendingEvent]]]:
        segments: list[tuple[CoalescingKey, list[PendingEvent]]] = []
        for index, ready_admission in enumerate(ready_admissions):
            key = self._key_for_ready_admission(ready_admissions, index)
            if (
                segments
                and segments[-1][0] == key
                and CoalescingGate._can_merge_ready_segment(key, segments[-1][1], ready_admission.pending_event)
            ):
                segments[-1][1].append(ready_admission.pending_event)
            else:
                segments.append((key, [ready_admission.pending_event]))
        return segments

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
        """Dispatch a claimed batch while buffering new ingress on the same gate."""
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
            self._wake_owner_gates(key)

    async def _dispatch_claimed_events(
        self,
        key: CoalescingKey,
        gate: _GateEntry,
        pending_events: list[PendingEvent],
        *,
        bypass_grace: bool,
    ) -> None:
        try:
            await self._dispatch_events(key, gate, pending_events, bypass_grace=bypass_grace)
        except asyncio.CancelledError:
            close_pending_event_metadata(pending_events)
            raise
        except Exception as error:
            close_pending_event_metadata(pending_events)
            self._log_dispatch_failure(key, gate, error)

    async def _drain_gate(self, key: CoalescingKey, gate: _GateEntry) -> None:  # noqa: C901, PLR0912, PLR0915
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
            while True:
                if not gate.queue:
                    self._remove_gate(key)
                    return

                front = gate.queue[0]
                await self._wait_until_front_claimable(key, gate, front_order=front.received_order)
                if not gate.queue:
                    continue
                front = gate.queue[0]
                front_kind = self._queued_kind(front)
                if front_kind in {_QueueKind.BYPASS, _QueueKind.COMMAND}:
                    claimed_admissions = self._claim_front_events(gate, 1)
                    ready_admissions = await self._resolve_claimed_admissions_or_requeue(key, gate, claimed_admissions)
                    if not ready_admissions:
                        continue
                    await self._dispatch_claimed_events(
                        self._key_for_ready_admission(ready_admissions, 0),
                        gate,
                        [ready_admissions[0].pending_event],
                        bypass_grace=True,
                    )
                    continue

                debounce_result = await self._wait_for_debounce(
                    gate,
                    coalesce_normal_events=lambda key=key, entry=gate: self._should_coalesce_normal_events(key, entry),
                )
                if not gate.queue:
                    continue
                front = gate.queue[0]
                same_window_reservations = self._unsettled_owner_reservations_in_window(
                    key,
                    after_order=front.received_order,
                    before_order=debounce_result.before_order,
                    before_or_at_receipt_time=debounce_result.quiet_deadline,
                )
                if same_window_reservations:
                    await self._wait_for_reservations(gate, same_window_reservations)
                    continue
                claim_max_received_order = self._next_received_order
                bypass_grace = self._is_shutting_down() or gate.drain_all_requested
                use_upload_grace = not bypass_grace and self._upload_grace_seconds() > 0
                coalesce_normal_events = self._should_coalesce_normal_events(key, gate)
                candidate_count = self._front_normal_run_length(
                    gate,
                    coalesce_normal_events=coalesce_normal_events,
                    max_received_order=claim_max_received_order,
                )
                if candidate_count == 0:
                    continue
                claimed_admissions = self._claim_front_events(gate, candidate_count)
                ready_admissions = await self._resolve_claimed_admissions_or_requeue(key, gate, claimed_admissions)
                if not ready_admissions:
                    continue
                segments = self._ready_admission_segments(ready_admissions)
                candidate_events = [event for _key, pending_events in segments for event in pending_events]
                if (
                    use_upload_grace
                    and _pending_has_only_text(candidate_events)
                    and not _pending_has_voice_source(candidate_events)
                ):
                    timing_scope = event_timing_scope(
                        build_coalesced_batch(key, candidate_events).primary_event.event_id,
                    )
                    gate.queue.extendleft(reversed(claimed_admissions))
                    candidate_count = await self._wait_for_upload_grace(
                        gate,
                        candidate_count,
                        timing_scope=timing_scope,
                    )
                    claimed_admissions = self._claim_front_events(gate, candidate_count)
                    ready_admissions = await self._resolve_claimed_admissions_or_requeue(key, gate, claimed_admissions)
                    segments = self._ready_admission_segments(ready_admissions)
                    bypass_grace = True
                if not gate.queue:
                    gate.drain_all_requested = False
                for segment_key, pending_events in segments:
                    await self._dispatch_claimed_events(
                        segment_key,
                        gate,
                        pending_events,
                        bypass_grace=bypass_grace,
                    )
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception as error:
            outcome = "failed"
            self._log_dispatch_failure(key, gate, error)
        finally:
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
