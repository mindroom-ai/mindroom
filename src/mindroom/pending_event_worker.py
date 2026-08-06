"""Turns committed journal events into semantic work.

The journal decides what MindRoom owes. This decides when it runs. Nothing
here is durable: a crash leaves every unsettled event exactly as pending as it
was, which is why there is no ``running`` state to get stuck in.

Every bound here is paired with a signal that more work remains. A pass that
stops early — because a room is busy, because the scan hit its page budget, or
because a lane failed — arranges to be woken again. A bound that silently drops
the remainder is how durable work ends up abandoned while the process looks
healthy.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mindroom.event_journal import SettlementOutcome
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

    from mindroom.event_journal import JournalEvent, PrincipalStore

logger = get_logger(__name__)

_INITIAL_RETRY_DELAY_SECONDS = 1.0
_MAX_RETRY_DELAY_SECONDS = 30.0
_BATCH_SIZE = 128
# How far one pass will scan looking for events it can act on. Reached only
# when a very large backlog is in flight; the pass reports that more remains.
_MAX_SCAN_PAGES = 16

# Returning ``None`` means the handler started work that outlives it — a turn
# that is still running — so the event stays pending and whoever owns that work
# releases it. Returning an outcome means the work is finished.
type _EventHandler = Callable[[JournalEvent], Awaitable[SettlementOutcome | None]]


@dataclass
class PendingEventWorker:
    """Drain pending journal events, in receipt order within each room.

    Order is preserved per room rather than globally. A room's events are a
    conversation and must be answered in the order they were received; two
    different rooms are unrelated, and making one wait for the other would let
    a single slow turn stall every other conversation the bot is in.
    """

    store: PrincipalStore
    handle: _EventHandler
    _lanes: dict[str, asyncio.Task[None]] = field(default_factory=dict, init=False, repr=False)
    _wake: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)
    _pump: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _retry: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _retry_delay_seconds: float = field(default=_INITIAL_RETRY_DELAY_SECONDS, init=False, repr=False)
    _failed_rooms: set[str] = field(default_factory=set, init=False, repr=False)
    # Events handed to a turn that is still running. They stay pending durably
    # so a crash replays them, but dispatching them again while their turn is
    # alive would answer the same message twice.
    _deferred: set[str] = field(default_factory=set, init=False, repr=False)
    # Rooms a pass found work for but could not dispatch, because their lane
    # was still busy. Their lane wakes the pump when it finishes.
    _rooms_with_more: set[str] = field(default_factory=set, init=False, repr=False)

    def start(self) -> None:
        """Begin draining, including anything a previous process left behind."""
        if self._pump is not None and not self._pump.done():
            return
        self._wake.set()
        self._pump = asyncio.create_task(self._run(), name="pending_event_worker")

    def wake(self) -> None:
        """Signal that new work was admitted."""
        self._wake.set()

    def release(self, event_ids: Iterable[str]) -> None:
        """Let events be dispatched again: their turn ended or is being retried.

        Callable from any thread, because a turn can become terminal on one.
        It only mutates memory; the pump does the I/O on its own loop.
        """
        self._deferred.difference_update(event_ids)

    def forget_all_deferrals(self) -> None:
        """Treat nothing as in flight, as a recovery pass must."""
        self._deferred.clear()

    async def stop(self) -> None:
        """Stop draining, leaving unfinished events pending for the next start."""
        pump = self._pump
        self._pump = None
        retry = self._retry
        self._retry = None
        for task in (pump, retry):
            if task is not None:
                task.cancel()
                try:  # noqa: SIM105 - the task may already have finished
                    await task
                except asyncio.CancelledError:
                    pass
        lanes = tuple(self._lanes.values())
        for lane in lanes:
            lane.cancel()
        for lane in lanes:
            try:  # noqa: SIM105 - cancellation is the expected outcome
                await lane
            except asyncio.CancelledError:
                pass
        self._lanes.clear()
        self._rooms_with_more.clear()

    async def drain_once(self) -> int:
        """Run every currently pending event to completion and return the count.

        Exists for startup recovery and for tests, where "the queue is empty"
        has to be observable rather than eventually true. Unlike a pump pass,
        this keeps scanning until nothing dispatchable is left, so its return
        value is the whole backlog rather than one bounded slice of it.
        """
        drained = 0
        attempted: frozenset[str] = frozenset()
        while True:
            by_room, _ = await self._collect_dispatchable()
            if not by_room:
                return drained
            ids = frozenset(event.event_id for events in by_room.values() for event in events)
            if ids == attempted:
                # Nothing moved: every remaining event failed or was refused.
                # Looping again would only repeat the same failures forever.
                return drained
            attempted = ids
            drained += len(ids)
            await asyncio.gather(*(self._run_lane(events) for events in by_room.values()))

    async def _run(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            try:
                await self._dispatch_ready_rooms()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("pending_event_worker_dispatch_failed")
                self._schedule_retry()

    async def _dispatch_ready_rooms(self) -> None:
        by_room, more_remains = await self._collect_dispatchable()
        started = False
        for room_id, events in by_room.items():
            active = self._lanes.get(room_id)
            if active is not None and not active.done():
                # Nothing else will look at this room again on its own, so its
                # lane has to wake the pump when it finishes.
                self._rooms_with_more.add(room_id)
                continue
            started = True
            self._start_lane(room_id, events, more_remains=more_remains)
        if started:
            self._retry_delay_seconds = _INITIAL_RETRY_DELAY_SECONDS

    def _start_lane(self, room_id: str, events: list[JournalEvent], *, more_remains: bool) -> None:
        self._rooms_with_more.discard(room_id)
        lane = asyncio.create_task(self._run_lane(events), name=f"pending_event_lane_{room_id}")
        self._lanes[room_id] = lane
        if more_remains:
            # This pass could not see the whole backlog, so the room may still
            # owe work that no later admission would reveal.
            self._rooms_with_more.add(room_id)
        lane.add_done_callback(lambda task: self._lane_finished(room_id, task))

    def _lane_finished(self, room_id: str, lane: asyncio.Task[None]) -> None:
        if self._lanes.get(room_id) is lane:
            del self._lanes[room_id]
        if lane.cancelled():
            return
        if room_id in self._failed_rooms:
            self._schedule_retry()
        elif room_id in self._rooms_with_more:
            self._wake.set()

    def _schedule_retry(self) -> None:
        """Re-run a failed pass later, since nothing else will trigger one."""
        if self._retry is not None and not self._retry.done():
            return
        self._retry = asyncio.create_task(self._retry_after_delay(), name="pending_event_worker_retry")

    async def _retry_after_delay(self) -> None:
        await asyncio.sleep(self._retry_delay_seconds)
        self._retry_delay_seconds = min(self._retry_delay_seconds * 2, _MAX_RETRY_DELAY_SECONDS)
        self._wake.set()

    async def _collect_dispatchable(self) -> tuple[dict[str, list[JournalEvent]], bool]:
        """Group pending events this worker may act on now, by room.

        Returns the grouping and whether the scan stopped before the end of the
        backlog. Events whose turn is still running are skipped rather than
        stopping the scan, so a room full of in-flight turns cannot hide the
        events queued behind it.
        """
        by_room: dict[str, list[JournalEvent]] = {}
        cursor: int | None = None
        for _ in range(_MAX_SCAN_PAGES):
            page = await self.store.pending(limit=_BATCH_SIZE, after_receipt_order=cursor)
            if not page:
                return by_room, False
            cursor = page[-1].receipt_order
            truncated = False
            for event in page:
                if event.event_id in self._deferred:
                    continue
                lane = by_room.setdefault(event.room_id, [])
                if len(lane) >= _BATCH_SIZE:
                    truncated = True
                    continue
                lane.append(event)
            if truncated:
                return by_room, True
            if len(page) < _BATCH_SIZE:
                return by_room, False
        return by_room, True

    async def _run_lane(self, events: list[JournalEvent]) -> None:
        """Run one room's events in receipt order, stopping at the first failure.

        Stopping matters: if event two fails and event three still ran, the
        room's conversation would be answered out of order, and the retry of
        event two would then arrive after its own reply.
        """
        room_id = events[0].room_id if events else ""
        for event in events:
            if not await self.store.is_pending(event.event_id):
                self._deferred.discard(event.event_id)
                continue
            try:
                outcome = await self.handle(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "pending_event_failed",
                    event_id=event.event_id,
                    kind=event.kind.value,
                    room_id=event.room_id,
                )
                self._failed_rooms.add(room_id)
                return
            if outcome is None:
                self._deferred.add(event.event_id)
                continue
            self._deferred.discard(event.event_id)
            await self.store.settle(event.event_id, outcome)
        self._failed_rooms.discard(room_id)
