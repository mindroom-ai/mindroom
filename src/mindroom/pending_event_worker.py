"""Turns committed journal events into semantic work.

The journal decides what MindRoom owes. This decides when it runs. Nothing
here is durable: a crash leaves every unsettled event exactly as pending as it
was, which is why there is no ``running`` state to get stuck in.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mindroom.event_journal import SettlementOutcome
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.event_journal import JournalEvent, PrincipalStore

logger = get_logger(__name__)

_INITIAL_RETRY_DELAY_SECONDS = 1.0
_MAX_RETRY_DELAY_SECONDS = 30.0
_BATCH_SIZE = 128

# Returning ``None`` means the handler started work that outlives it — a turn
# that is still running — so the event stays pending and whoever owns that work
# settles it. Returning an outcome means the work is finished.
type EventHandler = Callable[[JournalEvent], Awaitable[SettlementOutcome | None]]


@dataclass
class PendingEventWorker:
    """Drain pending journal events, in receipt order within each room.

    Order is preserved per room rather than globally. A room's events are a
    conversation and must be answered in the order they were received; two
    different rooms are unrelated, and making one wait for the other would let
    a single slow turn stall every other conversation the bot is in.
    """

    store: PrincipalStore
    handle: EventHandler
    _lanes: dict[str, asyncio.Task[None]] = field(default_factory=dict, init=False, repr=False)
    _wake: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)
    _pump: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _retry_delay_seconds: float = field(default=_INITIAL_RETRY_DELAY_SECONDS, init=False, repr=False)
    _failed_rooms: set[str] = field(default_factory=set, init=False, repr=False)

    def start(self) -> None:
        """Begin draining, including anything a previous process left behind."""
        if self._pump is not None and not self._pump.done():
            return
        self._wake.set()
        self._pump = asyncio.create_task(self._run(), name="pending_event_worker")

    def wake(self) -> None:
        """Signal that new work was admitted."""
        self._wake.set()

    async def stop(self) -> None:
        """Stop draining, leaving unfinished events pending for the next start."""
        pump = self._pump
        self._pump = None
        if pump is not None:
            pump.cancel()
            try:  # noqa: SIM105 - the task may already have finished
                await pump
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

    async def drain_once(self) -> int:
        """Run every currently pending event to completion and return the count.

        Exists for tests and for startup recovery, where "the queue is empty"
        has to be observable rather than eventually true.
        """
        pending = await self.store.pending(limit=_BATCH_SIZE)
        by_room: dict[str, list[JournalEvent]] = {}
        for event in pending:
            by_room.setdefault(event.room_id, []).append(event)
        await asyncio.gather(*(self._run_lane(events) for events in by_room.values()))
        return len(pending)

    async def _run(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            try:
                dispatched = await self._dispatch_ready_rooms()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("pending_event_worker_dispatch_failed")
                dispatched = False
            if self._failed_rooms:
                # Nothing else will wake this: the events are already admitted,
                # so a retry has to be scheduled from here or they sit forever.
                await asyncio.sleep(self._retry_delay_seconds)
                self._retry_delay_seconds = min(self._retry_delay_seconds * 2, _MAX_RETRY_DELAY_SECONDS)
                self._wake.set()
            elif dispatched:
                self._retry_delay_seconds = _INITIAL_RETRY_DELAY_SECONDS

    async def _dispatch_ready_rooms(self) -> bool:
        pending = await self.store.pending(limit=_BATCH_SIZE)
        by_room: dict[str, list[JournalEvent]] = {}
        for event in pending:
            by_room.setdefault(event.room_id, []).append(event)
        dispatched = False
        for room_id, events in by_room.items():
            active = self._lanes.get(room_id)
            if active is not None and not active.done():
                continue
            dispatched = True
            self._lanes[room_id] = asyncio.create_task(
                self._run_lane(events),
                name=f"pending_event_lane_{room_id}",
            )
        return dispatched

    async def _run_lane(self, events: list[JournalEvent]) -> None:
        """Run one room's events in receipt order, stopping at the first failure.

        Stopping matters: if event two fails and event three still ran, the
        room's conversation would be answered out of order, and the retry of
        event two would then arrive after its own reply.
        """
        room_id = events[0].room_id if events else ""
        for event in events:
            if not await self.store.is_pending(event.event_id):
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
            if outcome is not None:
                await self.store.settle(event.event_id, outcome)
        self._failed_rooms.discard(room_id)
