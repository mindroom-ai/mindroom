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
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mindroom.event_journal import SettlementOutcome
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Iterator

    from mindroom.event_journal import JournalEvent, ReplayView

logger = get_logger(__name__)

_INITIAL_RETRY_DELAY_SECONDS = 1.0
_MAX_RETRY_DELAY_SECONDS = 30.0
_BATCH_SIZE = 128
# How long a deferral may sit before the worker looks at its owner again.
#
# This is a cadence, not a deadline. Nothing is declared dead because this
# elapsed; the probe decides, and it is exact. So the value does not have to
# exceed the longest legitimate turn the way a death timeout would — it only
# bounds how long a lost owner goes unnoticed in a bot quiet enough that no
# admission wakes the pump on its own.
_DEFERRAL_SCAN_SECONDS = 30.0
# How far one pass will scan looking for events it can act on. Reached only
# when a very large backlog is in flight; the pass reports that more remains.
_MAX_SCAN_PAGES = 16

# Returning ``None`` means the handler started work that outlives it — a turn
# that is still running — so the event stays pending and whoever owns that work
# releases it. Returning an outcome means the work is finished.
type _EventHandler = Callable[[JournalEvent], Awaitable[SettlementOutcome | None]]

# Whether the owner a deferring handler handed one event to still exists.
type _DeferralLivenessProbe = Callable[[JournalEvent], bool]


def _assume_owner_is_live(event: JournalEvent) -> bool:
    """Treat every deferral as owned, which is all a worker alone can know.

    ``pending`` conflates "never started" with "started, someone else owns it",
    and only the caller that handed the event off can tell them apart. A worker
    built without a probe therefore has to believe the handoff, exactly as it
    did before probes existed. The opposite default would re-dispatch every
    deferral on every scan.
    """
    del event
    return True


@dataclass
class PendingEventWorker:
    """Drain pending journal events, in receipt order within each room.

    Order is preserved per room rather than globally. A room's events are a
    conversation and must be answered in the order they were received; two
    different rooms are unrelated, and making one wait for the other would let
    a single slow turn stall every other conversation the bot is in.
    """

    store: ReplayView
    handle: _EventHandler
    # Asked about every deferred event on every scan. A deferral whose owner is
    # gone is durable work nobody is left to release, so the scan takes it back
    # rather than waiting for a restart to notice.
    deferral_is_live: _DeferralLivenessProbe = _assume_owner_is_live
    deferral_scan_seconds: float = _DEFERRAL_SCAN_SECONDS
    _lanes: dict[str, asyncio.Task[None]] = field(default_factory=dict, init=False, repr=False)
    _wake: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)
    _pump: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _retry: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _deferral_scan: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _retry_delay_seconds: float = field(default=_INITIAL_RETRY_DELAY_SECONDS, init=False, repr=False)
    _failed_rooms: set[str] = field(default_factory=set, init=False, repr=False)
    # Events handed to a turn that is still running. They stay pending durably
    # so a crash replays them, but dispatching them again while their turn is
    # alive would answer the same message twice.
    _deferred: set[str] = field(default_factory=set, init=False, repr=False)
    # Rooms a pass found work for but could not dispatch, because their lane
    # was still busy. Their lane wakes the pump when it finishes.
    _rooms_with_more: set[str] = field(default_factory=set, init=False, repr=False)
    # Events a caller is running itself, off the lanes. An event is pending in
    # the store for the whole time its handler runs, so a scan that could not
    # see these would collect one and put a second handler inside it.
    _running_off_lane: set[str] = field(default_factory=set, init=False, repr=False)

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

    @contextmanager
    def sole_handler(self, event_id: str) -> Iterator[None]:
        """Hold one event against lane dispatch while its caller runs it itself.

        Some events are ordered against the response that produced them, so
        their caller has to see the handler finish rather than hand it to the
        pump. That does not exempt the event from having one handler: it stays
        pending for its handler's whole duration, and nothing else here treats
        a running handler as in flight.

        Enter this before admitting, not after. A scan can only collect a
        committed row, so a claim taken first cannot be missed; taken
        afterwards it leaves a window in which the pump starts the very
        handler the caller is about to start.

        Releasing wakes the pump, because a handler that deferred leaves its
        event pending and the admission that would have revealed it has
        already been spent on a scan that skipped it.
        """
        self._running_off_lane.add(event_id)
        try:
            yield
        finally:
            self._running_off_lane.discard(event_id)
            self._wake.set()

    async def stop(self) -> None:
        """Stop draining, leaving unfinished events pending for the next start."""
        pump = self._pump
        self._pump = None
        retry = self._retry
        self._retry = None
        deferral_scan = self._deferral_scan
        self._deferral_scan = None
        for task in (pump, retry, deferral_scan):
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

        Recovery runs while the pump is live rather than only before it starts,
        so this drains through the room's lane instead of beside it. A second
        lane over one room is not a faster drain: it puts two handlers inside
        one event and lets event three overtake event two, which are the two
        things a lane exists to make impossible.
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
            await asyncio.gather(*(self._drain_room(room_id, events) for room_id, events in by_room.items()))

    async def _drain_room(self, room_id: str, events: list[JournalEvent]) -> None:
        """Run one room's events, once whatever lane owns that room is done.

        Rechecked after each wait because the pump wakes on the same lane
        completion, so the room can be claimed again before this resumes.
        Someone else's lane is waited on rather than awaited: whether that one
        was cancelled is the pump's business, and a drain must not inherit it.
        """
        while (active := self._lanes.get(room_id)) is not None and not active.done():
            await asyncio.wait([active])
        lane = self._start_lane(room_id, events, more_remains=False)
        await asyncio.wait([lane])
        # This lane is the drain's own, so whatever ended it is the drain's to
        # report. A cancelled turn is not a failed one: it leaves its event
        # pending and hands the cancellation to whoever asked for the drain.
        lane.result()

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
        # A pass that found every deferral still owned starts no lane, so the
        # lane-finished path cannot be the only thing that arms the next look.
        self._schedule_deferral_scan()

    def _start_lane(self, room_id: str, events: list[JournalEvent], *, more_remains: bool) -> asyncio.Task[None]:
        """Make one room's lane, which is the only one that room may have."""
        self._rooms_with_more.discard(room_id)
        lane = asyncio.create_task(self._run_lane(events), name=f"pending_event_lane_{room_id}")
        self._lanes[room_id] = lane
        if more_remains:
            # This pass could not see the whole backlog, so the room may still
            # owe work that no later admission would reveal.
            self._rooms_with_more.add(room_id)
        lane.add_done_callback(lambda task: self._lane_finished(room_id, task))
        return lane

    def _lane_finished(self, room_id: str, lane: asyncio.Task[None]) -> None:
        if self._lanes.get(room_id) is lane:
            del self._lanes[room_id]
        if lane.cancelled():
            return
        if room_id in self._failed_rooms:
            self._schedule_retry()
        elif room_id in self._rooms_with_more:
            self._wake.set()
        self._schedule_deferral_scan()

    def _schedule_deferral_scan(self) -> None:
        """Arrange one later look while anything is deferred.

        Every other wakeup here is caused by something: an admission, a lane
        finishing, a failure backing off. An owner dying causes none of them,
        so without this the reclaim would only run when unrelated traffic
        happened to wake the pump — which is no bound at all in a quiet room.
        The timer exists only while a deferral does.
        """
        if not self._deferred or (self._deferral_scan is not None and not self._deferral_scan.done()):
            return
        self._deferral_scan = asyncio.create_task(
            self._scan_after_deferral_delay(),
            name="pending_event_deferral_scan",
        )

    async def _scan_after_deferral_delay(self) -> None:
        await asyncio.sleep(self.deferral_scan_seconds)
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
                if event.event_id in self._running_off_lane:
                    # Its caller is inside the handler right now, and releases
                    # the claim with a wake so a later pass reconsiders it.
                    continue
                if event.event_id in self._deferred and not self._reclaim_lost_deferral(event):
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

    def _reclaim_lost_deferral(self, event: JournalEvent) -> bool:
        """Return whether one deferred event is dispatchable again already.

        Deferral is a promise that some owner will call ``release``. Nothing
        makes that owner keep the promise, and the event is durably pending the
        whole time, so an owner that dies quietly leaves work that no later
        admission and no retry will ever reveal. Asking whether the owner still
        exists turns that into a bounded outage instead of one that lasts until
        the process restarts.
        """
        if self.deferral_is_live(event):
            return False
        self._deferred.discard(event.event_id)
        logger.warning(
            "pending_event_deferral_owner_lost",
            event_id=event.event_id,
            kind=event.kind.value,
            room_id=event.room_id,
        )
        return True

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
