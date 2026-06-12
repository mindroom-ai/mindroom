"""Per-(room, sender) ingress lanes delivering resolving ingress in receipt order."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .coalescing_cleanup import ReadyPendingEvent, close_ready_task_result_metadata
from .logging_config import get_logger
from .timing import elapsed_ms_since

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from .coalescing_batch import CoalescingKey

logger = get_logger(__name__)

type _LaneKey = tuple[str, str]


class IngressAdmissionClosedError(RuntimeError):
    """Raised when ingress tries to admit through a released or closed lane slot."""


@dataclass
class LaneDelivery:
    """Conversation-assigned payload waiting for its lane turn."""

    key: CoalescingKey
    source_event_id: str | None
    source_kind: str
    ready_result: ReadyPendingEvent | None
    ready_task: asyncio.Task[ReadyPendingEvent | None] | None
    received_at: float


@dataclass
class LaneSlot:
    """One receipt-order position in a (room, sender) ingress lane."""

    room_id: str
    sender_id: str
    receipt_time: float
    closed: bool = False
    released: bool = False
    delivery: LaneDelivery | None = None
    loaded: asyncio.Event = field(default_factory=asyncio.Event, repr=False, compare=False)
    settled: asyncio.Event = field(default_factory=asyncio.Event, repr=False, compare=False)


@dataclass(frozen=True)
class _LaneAbandonOutcome:
    """Counts from abandoning one undelivered lane slot."""

    cancelled_unready_count: int = 0
    dropped_ready_count: int = 0


class IngressLanes:
    """Own receipt-order delivery of resolving ingress per (room, sender) lane.

    Each lane is a plain FIFO: a slot enters at receipt time, is later loaded
    with its canonical conversation key plus a ready event (or a readiness
    task such as voice STT), and a per-lane worker delivers loaded slots to
    the conversation gate strictly in receipt order.
    """

    def __init__(
        self,
        *,
        deliver: Callable[[LaneSlot, LaneDelivery, ReadyPendingEvent], Awaitable[None]],
    ) -> None:
        self._deliver = deliver
        self._lanes: dict[_LaneKey, deque[LaneSlot]] = {}
        self._workers: dict[_LaneKey, asyncio.Task[None]] = {}

    @staticmethod
    def closed_slot(*, room_id: str, sender_id: str, receipt_time: float | None = None) -> LaneSlot:
        """Return a pre-closed slot for ingress arriving during a bounded drain."""
        slot = LaneSlot(
            room_id=room_id,
            sender_id=sender_id,
            receipt_time=receipt_time if receipt_time is not None else time.monotonic(),
            closed=True,
            released=True,
        )
        slot.loaded.set()
        slot.settled.set()
        return slot

    def enter(self, *, room_id: str, sender_id: str, receipt_time: float | None = None) -> LaneSlot:
        """Reserve the next receipt-order position in one (room, sender) lane."""
        slot = LaneSlot(
            room_id=room_id,
            sender_id=sender_id,
            receipt_time=receipt_time if receipt_time is not None else time.monotonic(),
        )
        lane_key = (room_id, sender_id)
        self._lanes.setdefault(lane_key, deque()).append(slot)
        self._ensure_worker(lane_key)
        return slot

    def submit(
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
        """Load one slot with its conversation key and ready payload."""
        if slot.released or slot.closed:
            msg = "Cannot admit through a released ingress lane slot"
            raise IngressAdmissionClosedError(msg)
        if ready_result is None and ready_task is None:
            msg = "ready_task is required when ready_result is not provided"
            raise ValueError(msg)
        slot.delivery = LaneDelivery(
            key=key,
            source_event_id=source_event_id,
            source_kind=source_kind,
            ready_result=ready_result,
            ready_task=ready_task,
            received_at=received_at if received_at is not None else time.time(),
        )
        slot.loaded.set()
        self._ensure_worker((slot.room_id, slot.sender_id))

    def release(self, slot: LaneSlot) -> None:
        """Release one slot that will not deliver; its lane worker settles it."""
        if slot.released or slot.settled.is_set():
            return
        slot.released = True
        slot.loaded.set()

    def undelivered_in_window(
        self,
        room_id: str,
        sender_id: str,
        *,
        before_or_at_receipt_time: float,
    ) -> list[LaneSlot]:
        """Return undelivered slots for one sender received inside an open burst window."""
        lane = self._lanes.get((room_id, sender_id), ())
        return [
            slot
            for slot in lane
            if not slot.released and not slot.settled.is_set() and slot.receipt_time <= before_or_at_receipt_time
        ]

    def unsettled_slots(self) -> list[LaneSlot]:
        """Return every slot that has not delivered or released yet."""
        return [slot for lane in self._lanes.values() for slot in lane if not slot.settled.is_set()]

    def all_settled(self) -> bool:
        """Return whether no lane holds undelivered ingress."""
        return not self.unsettled_slots()

    async def abandon_slot(self, slot: LaneSlot, *, ready_timeout_seconds: float | None) -> _LaneAbandonOutcome:
        """Release one slot for a bounded drain, cancelling and closing its payload."""
        self.release(slot)
        slot.settled.set()
        delivery = slot.delivery
        if delivery is None:
            return _LaneAbandonOutcome()
        if delivery.ready_result is not None:
            return _LaneAbandonOutcome(dropped_ready_count=close_ready_task_result_metadata(delivery.ready_result))
        ready_task = delivery.ready_task
        if ready_task is None:
            return _LaneAbandonOutcome()
        ready_task.cancel()
        done, pending = await asyncio.wait({ready_task}, timeout=ready_timeout_seconds)
        if pending:
            ready_task.add_done_callback(_close_late_ready_task_result)
            return _LaneAbandonOutcome(cancelled_unready_count=1)
        result = await asyncio.gather(*done, return_exceptions=True)
        return _LaneAbandonOutcome(
            cancelled_unready_count=1,
            dropped_ready_count=close_ready_task_result_metadata(result[0]),
        )

    def _ensure_worker(self, lane_key: _LaneKey) -> None:
        worker = self._workers.get(lane_key)
        if worker is not None and not worker.done():
            return
        lane = self._lanes.get(lane_key)
        if not lane:
            return
        self._workers[lane_key] = asyncio.create_task(
            self._run_lane(lane_key),
            name=f"ingress_lane:{lane_key[0]}:{lane_key[1]}",
        )

    async def _run_lane(self, lane_key: _LaneKey) -> None:
        lane = self._lanes.get(lane_key)
        if lane is None:
            return
        try:
            while lane:
                slot = lane[0]
                await slot.loaded.wait()
                try:
                    if not slot.released:
                        await self._deliver_slot(slot)
                finally:
                    if lane and lane[0] is slot:
                        lane.popleft()
                    slot.settled.set()
        finally:
            self._workers.pop(lane_key, None)
            if not self._lanes.get(lane_key):
                self._lanes.pop(lane_key, None)

    async def _deliver_slot(self, slot: LaneSlot) -> None:
        delivery = slot.delivery
        if delivery is None:
            return
        ready = delivery.ready_result
        if ready is None:
            assert delivery.ready_task is not None
            try:
                ready = await asyncio.shield(delivery.ready_task)
            except asyncio.CancelledError:
                if delivery.ready_task.cancelled():
                    logger.warning(
                        "ingress_lane_ready_task_cancelled",
                        source_event_id=delivery.source_event_id,
                        room_id=slot.room_id,
                        sender_id=slot.sender_id,
                        age_ms=elapsed_ms_since(delivery.received_at, clock=time.time),
                    )
                    return
                raise
            except Exception as error:
                logger.exception(
                    "ingress_lane_ready_task_failed",
                    source_event_id=delivery.source_event_id,
                    room_id=slot.room_id,
                    sender_id=slot.sender_id,
                    age_ms=elapsed_ms_since(delivery.received_at, clock=time.time),
                    exception_type=error.__class__.__name__,
                    error_message=str(error),
                )
                return
        if ready is None:
            return
        ready.pending_event.enqueue_time = delivery.received_at
        try:
            await self._deliver(slot, delivery, ready)
        except Exception:
            close_ready_task_result_metadata(ready)
            logger.exception(
                "ingress_lane_delivery_failed",
                source_event_id=delivery.source_event_id,
                room_id=slot.room_id,
                sender_id=slot.sender_id,
            )


def _close_late_ready_task_result(task: asyncio.Task[ReadyPendingEvent | None]) -> None:
    try:
        result = task.result()
    except BaseException:
        return
    close_ready_task_result_metadata(result)
