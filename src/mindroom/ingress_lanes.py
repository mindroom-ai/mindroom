"""Per-(room, sender) ingress lanes delivering resolving ingress in receipt order."""

from __future__ import annotations

import asyncio
import enum
import time
from collections import deque
from dataclasses import dataclass, field
from itertools import chain
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
    callback_source_kind: str | None = None
    busy_at_submit: bool = False


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


class _LaneDeliveryOutcome(enum.Enum):
    """Terminal ownership result for one resolved lane slot."""

    DELIVERED = "delivered"
    RETRY = "retry"
    INTENTIONALLY_IGNORED = "intentionally_ignored"


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
        on_undelivered_source: Callable[[str, str], None] | None = None,
        on_intentionally_ignored_source: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> None:
        self._deliver = deliver
        self._on_undelivered_source = on_undelivered_source
        self._on_intentionally_ignored_source = on_intentionally_ignored_source
        self._lanes: dict[_LaneKey, deque[LaneSlot]] = {}
        self._workers: dict[_LaneKey, asyncio.Task[None]] = {}
        self._settling_slots: dict[int, LaneSlot] = {}

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
        callback_source_kind: str | None = None,
        ready_result: ReadyPendingEvent | None = None,
        ready_task: asyncio.Task[ReadyPendingEvent | None] | None = None,
        received_at: float | None = None,
        busy_at_submit: bool = False,
    ) -> None:
        """Load one slot with its conversation key and ready payload."""
        if slot.released or slot.closed:
            msg = "Cannot admit through a released ingress lane slot"
            raise IngressAdmissionClosedError(msg)
        if (ready_result is None) == (ready_task is None):
            msg = "Provide exactly one of ready_result or ready_task"
            raise ValueError(msg)
        slot.delivery = LaneDelivery(
            key=key,
            source_event_id=source_event_id,
            source_kind=source_kind,
            ready_result=ready_result,
            ready_task=ready_task,
            received_at=received_at if received_at is not None else time.time(),
            callback_source_kind=callback_source_kind,
            busy_at_submit=busy_at_submit,
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
        exclude_slot_ids: set[int] | None = None,
    ) -> list[LaneSlot]:
        """Return undelivered slots for one sender received inside an open burst window."""
        lane = self._lanes.get((room_id, sender_id), ())
        return [
            slot
            for slot in lane
            if not slot.released
            and not slot.settled.is_set()
            and slot.receipt_time <= before_or_at_receipt_time
            and (exclude_slot_ids is None or id(slot) not in exclude_slot_ids)
        ]

    def unsettled_slots(self) -> list[LaneSlot]:
        """Return every slot that has not delivered or released yet."""
        lane_slots = [slot for lane in self._lanes.values() for slot in lane if not slot.settled.is_set()]
        return [*lane_slots, *self._settling_slots.values()]

    def has_pending_source_event(self, source_event_id: str) -> bool:
        """Return whether an active lane still owns one exact source before settlement."""
        return self._has_pending_source_event(source_event_id)

    def _has_pending_source_event(
        self,
        source_event_id: str,
        *,
        exclude_slot: LaneSlot | None = None,
    ) -> bool:
        """Return whether any live lane owner except one optional slot owns a source."""
        return any(
            slot is not exclude_slot and slot.delivery is not None and slot.delivery.source_event_id == source_event_id
            for slot in chain(
                chain.from_iterable(self._lanes.values()),
                self._settling_slots.values(),
            )
        )

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
        worker = asyncio.create_task(
            self._run_lane(lane_key),
            name=f"ingress_lane:{lane_key[0]}:{lane_key[1]}",
        )
        # Lane cleanup lives in a done callback, not the coroutine's finally:
        # a worker cancelled before its first scheduling never enters its own
        # function body, and unsettled slots would hang graceful drains.
        worker.add_done_callback(lambda task, lane_key=lane_key: self._finish_worker(lane_key, task))
        self._workers[lane_key] = worker

    def _finish_worker(self, lane_key: _LaneKey, task: asyncio.Task[None]) -> None:
        current = self._workers.get(lane_key)
        if current is task:
            self._workers.pop(lane_key, None)
        elif current is not None:
            # A replacement worker already owns this lane.
            return
        if task.cancelled() or task.exception() is not None:
            self._settle_abandoned_lane(lane_key)
            return
        if not self._lanes.get(lane_key):
            self._lanes.pop(lane_key, None)

    def _settle_abandoned_lane(self, lane_key: _LaneKey) -> None:
        """Release and settle every slot of a lane whose worker died abnormally."""
        remaining = self._lanes.pop(lane_key, None)
        for slot in remaining or ():
            if slot.settled.is_set():
                continue
            # A late submit into an abandoned slot must raise instead of
            # loading work that no worker will ever deliver.
            slot.released = True
            slot.loaded.set()
            slot.settled.set()

    async def _run_lane(self, lane_key: _LaneKey) -> None:
        lane = self._lanes.get(lane_key)
        if lane is None:
            return
        while lane:
            slot = lane[0]
            # The finally must cover the whole slot lifecycle, including the
            # loaded wait: a head slot that never settles poisons its sender's
            # lane and hangs unbounded drains.
            completed = False
            undelivered: LaneDelivery | None = None
            intentionally_ignored: LaneDelivery | None = None
            try:
                await slot.loaded.wait()
                if not slot.released:
                    delivery_outcome = await self._deliver_slot(slot)
                    undelivered, intentionally_ignored = self._completion_sources(
                        delivery_outcome,
                        slot.delivery,
                    )
                completed = True
            except asyncio.CancelledError:
                raise
            except Exception:
                undelivered = slot.delivery
                logger.exception(
                    "ingress_lane_slot_failed",
                    room_id=slot.room_id,
                    sender_id=slot.sender_id,
                )
            finally:
                if not completed:
                    slot.released = True
                if lane and lane[0] is slot:
                    lane.popleft()
                if intentionally_ignored is None:
                    slot.settled.set()
                else:
                    self._settling_slots[id(slot)] = slot
            await self._finish_delivery_notifications(
                slot,
                undelivered=undelivered,
                intentionally_ignored=intentionally_ignored,
            )

    async def _finish_delivery_notifications(
        self,
        slot: LaneSlot,
        *,
        undelivered: LaneDelivery | None,
        intentionally_ignored: LaneDelivery | None,
    ) -> None:
        if undelivered is not None:
            self._notify_undelivered_source(undelivered)
        if intentionally_ignored is not None:
            try:
                await self._notify_intentionally_ignored_source(slot, intentionally_ignored)
            finally:
                self._settling_slots.pop(id(slot), None)
                slot.settled.set()

    @staticmethod
    def _completion_sources(
        outcome: _LaneDeliveryOutcome,
        delivery: LaneDelivery | None,
    ) -> tuple[LaneDelivery | None, LaneDelivery | None]:
        if outcome is _LaneDeliveryOutcome.RETRY:
            return delivery, None
        if outcome is _LaneDeliveryOutcome.INTENTIONALLY_IGNORED:
            return None, delivery
        return None, None

    def _notify_undelivered_source(self, delivery: LaneDelivery) -> None:
        callback = self._on_undelivered_source
        if callback is None or delivery.source_event_id is None:
            return
        try:
            callback(delivery.source_event_id, delivery.callback_source_kind or delivery.source_kind)
        except Exception:
            logger.exception(
                "ingress_lane_undelivered_source_notification_failed",
                source_event_id=delivery.source_event_id,
                room_id=delivery.key.room_id,
            )

    async def _notify_intentionally_ignored_source(self, slot: LaneSlot, delivery: LaneDelivery) -> None:
        callback = self._on_intentionally_ignored_source
        if callback is None or delivery.source_event_id is None:
            return
        if self._has_pending_source_event(delivery.source_event_id, exclude_slot=slot):
            return
        try:
            await callback(
                delivery.source_event_id,
                delivery.callback_source_kind or delivery.source_kind,
            )
        except Exception:
            logger.exception(
                "ingress_lane_ignored_source_settlement_failed",
                source_event_id=delivery.source_event_id,
                room_id=delivery.key.room_id,
            )
            # The fallback retry is synchronous, so drop only this owner before
            # its ordinary duplicate-suppression check runs.
            self._settling_slots.pop(id(slot), None)
            self._notify_undelivered_source(delivery)

    async def _deliver_slot(self, slot: LaneSlot) -> _LaneDeliveryOutcome:
        delivery = slot.delivery
        assert delivery is not None
        ready = delivery.ready_result
        if ready is None:
            assert delivery.ready_task is not None
            # Shield the readiness task so cancelling this lane worker does not
            # cancel STT work another drain may still want to settle. A raised
            # CancelledError is therefore ambiguous and must be split by source
            # BEFORE the ready-is-None check below: the task cancelling itself
            # skips this slot, while a cancelled worker re-raises to its owner.
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
                    return _LaneDeliveryOutcome.RETRY
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
                return _LaneDeliveryOutcome.RETRY
        # Only after the exception split: a successful None result intentionally
        # consumed this source without entering the gate.
        if ready is None:
            return _LaneDeliveryOutcome.INTENTIONALLY_IGNORED
        if slot.released:
            # A bounded drain abandoned this slot while its readiness resolved;
            # the drain already counted it dropped and closed its metadata, so
            # delivering now would dispatch work the drain reported as dropped.
            close_ready_task_result_metadata(ready)
            return _LaneDeliveryOutcome.DELIVERED
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
            return _LaneDeliveryOutcome.RETRY
        return _LaneDeliveryOutcome.DELIVERED


def _close_late_ready_task_result(task: asyncio.Task[ReadyPendingEvent | None]) -> None:
    try:
        result = task.result()
    except BaseException:
        return
    close_ready_task_result_metadata(result)
