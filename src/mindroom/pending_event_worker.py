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
# How many pages one pass will read looking for events it can act on. Each is
# bounded in rows by the store, so this bounds the pass in rows too. Reached
# only when a very large backlog is in flight, or when a long stretch of it
# cannot be read; either way the pass reports that more remains.
_MAX_SCAN_PAGES = 16

# Returning ``False`` means the handler started work that outlives it — a turn
# that is still running — so the event stays pending and whoever owns that work
# releases it. Returning ``True`` means the work is finished.
type _EventHandler = Callable[[JournalEvent], Awaitable[bool]]

# Whether the owner a deferring handler handed one event to still exists.
type _DeferralLivenessProbe = Callable[[JournalEvent], bool]

# The events whose parsed Matrix objects a caller is holding for a run that has
# not happened yet, and the way to tell it a run is never coming.
type _RetainedEventIds = Callable[[], frozenset[str]]
type _ReleaseRetained = Callable[[frozenset[str]], None]


def _nothing_is_retained() -> frozenset[str]:
    """Hold nothing, for a worker whose caller keeps no parsed objects."""
    return frozenset()


def _release_nothing(event_ids: frozenset[str]) -> None:
    """Discard a release for a caller that was holding nothing to begin with."""
    del event_ids


def _in_receipt_order(by_room: dict[str, list[JournalEvent]]) -> dict[str, list[JournalEvent]]:
    """Put each room's collected events back into receipt order.

    A lane runs its list verbatim, so the list is where a room's order is
    decided -- and a collected list is segments concatenated, none of which is
    placed by receipt order. Reclaimed deferrals are seeded before any page is
    read, whatever their position in the backlog, and a wrapped pass reads the
    events after its resume point before the ones in front of it. Either one
    hands a lane a later message to answer before an earlier one, which is the
    single thing a lane exists to prevent.

    Sorted rather than merged, because there are not two ordered runs to merge.
    Only the pages carry the store's ``ORDER BY``; the reclaim walks the
    deferral map in insertion order, and an event released and deferred again
    moves to the back of it, behind one that never moved. Sorting also costs
    nothing to state: these lists are near-sorted and bounded by the page
    budget, which is the case a sort is cheapest on.
    """
    for events in by_room.values():
        events.sort(key=lambda event: event.receipt_order)
    return by_room


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
    # A parsed Matrix object is given back by the run that uses it, and a row
    # can stop being pending without any run: a membership fence settles the
    # turn-backed work it has just made unanswerable. Only a pass that saw the
    # whole pending set can prove a run is never coming, so only this worker is
    # in a position to say so.
    retained_event_ids: _RetainedEventIds = _nothing_is_retained
    release_retained: _ReleaseRetained = _release_nothing
    deferral_scan_seconds: float = _DEFERRAL_SCAN_SECONDS
    _lanes: dict[str, asyncio.Task[None]] = field(default_factory=dict, init=False, repr=False)
    _wake: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)
    _pump: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _retry: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _deferral_scan: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _retry_delay_seconds: float = field(default=_INITIAL_RETRY_DELAY_SECONDS, init=False, repr=False)
    _failed_rooms: set[str] = field(default_factory=set, init=False, repr=False)
    # Events handed to a turn that is still running, kept whole rather than by
    # id. They stay pending durably so a crash replays them, but dispatching
    # one again while its turn is alive would answer the same message twice --
    # and asking whether that turn still exists is a question about this set,
    # not about wherever the scan's window currently sits.
    _deferred: dict[str, JournalEvent] = field(default_factory=dict, init=False, repr=False)
    # Rooms a pass found work for but could not dispatch, because their lane
    # was still busy. Their lane wakes the pump when it finishes.
    _rooms_with_more: set[str] = field(default_factory=set, init=False, repr=False)
    # Events a caller is running itself, off the lanes. An event is pending in
    # the store for the whole time its handler runs, so a scan that could not
    # see these would collect one and put a second handler inside it.
    _running_off_lane: set[str] = field(default_factory=set, init=False, repr=False)
    # Where the next bounded scan resumes, so a prefix of events this worker
    # cannot act on cannot spend the whole page budget on every pass.
    _scan_cursor: int | None = field(default=None, init=False, repr=False)

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
        for event_id in event_ids:
            self._deferred.pop(event_id, None)

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

        It scans the whole backlog from the front and owns no resume point, and
        both halves of that matter. Borrowing the pump's bounded, rotating
        window gave consecutive passes different slices, so the comparison that
        ends this loop -- the same events came back untouched -- could not
        converge on a backlog larger than one pass with anything in it that
        keeps failing: the drain never returned, and every later recovery for
        every bot queued behind the one that was still going. Borrowing the
        pump's cursor also moved it, so a drain rewound the pump's position or
        skipped it forward past work the pump had not looked at yet.
        """
        drained = 0
        attempted: frozenset[str] = frozenset()
        while True:
            by_room = await self._collect_whole_backlog()
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
        lane = self._start_lane(room_id, events)
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
            self._start_lane(room_id, events)
        if started:
            self._retry_delay_seconds = _INITIAL_RETRY_DELAY_SECONDS
        if more_remains:
            self._continue_scanning(dispatched=started)
        # A pass that found every deferral still owned starts no lane, so the
        # lane-finished path cannot be the only thing that arms the next look.
        self._schedule_deferral_scan()

    def _continue_scanning(self, *, dispatched: bool) -> None:
        """Arm the pass that resumes where this one stopped short.

        Where a pass stopped is the scan's own position, so the scan is what
        has to carry it. Hung off the rooms a pass dispatched to instead, it
        was simply lost whenever a pass dispatched to none -- and a window of
        nothing but rows the store could not decode is exactly that. Those rows
        yield no event and belong to no owner, so nothing would wake the pump
        on their behalf and everything queued behind them stayed pending until
        unrelated traffic happened to arrive, which in a quiet room is never.

        A pass that dispatched something left the cursor past what it
        dispatched, so the next one reads a different window and may run at
        once. A pass that dispatched nothing would read the same window
        immediately and spin on it, so that one waits out the backoff instead.
        """
        if dispatched:
            self._wake.set()
        else:
            self._schedule_retry()

    def _start_lane(self, room_id: str, events: list[JournalEvent]) -> asyncio.Task[None]:
        """Make one room's lane, which is the only one that room may have."""
        self._rooms_with_more.discard(room_id)
        lane = asyncio.create_task(self._run_lane(events), name=f"pending_event_lane_{room_id}")
        self._lanes[room_id] = lane
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

        A pass that runs out of page budget leaves its position behind for the
        next one, and a pass that reaches the end of the backlog starts again
        from the front. Restarting at receipt order zero every time is what
        made the budget a ceiling rather than a bound: enough events the worker
        cannot act on -- a busy bot's in-flight turns, a room whose lane keeps
        failing -- and every pass spends the whole budget on the same prefix
        while the dispatchable events behind it are never reached at all.

        Rows the store could not read are the same kind of prefix and get the
        same treatment. They yield no event, so the resume point comes from the
        page rather than from the last event on it: taken from the events, a
        page that decoded nothing would leave the cursor where it was and every
        later pass would spend its whole budget re-reading the same corruption.

        Wrapping to the front is a revolution, not a fresh start, so the pass
        ends where it began. Without that stop it kept spending budget past its
        own origin and collected the events it had already collected a few
        pages earlier -- and a room's lane is handed that list verbatim, so a
        handler that defers rather than settling runs twice on one source.
        """
        by_room = self._reclaim_lost_deferrals()
        reclaimed = frozenset(event.event_id for events in by_room.values() for event in events)
        retained = self.retained_event_ids()
        still_pending: set[str] = set(reclaimed)
        origin = self._scan_cursor
        cursor = origin
        wrapped = origin is None
        for _ in range(_MAX_SCAN_PAGES):
            page = await self.store.pending(limit=_BATCH_SIZE, after_receipt_order=cursor)
            reached_origin = self._collect_page(
                page,
                by_room,
                still_pending,
                already_taken=reclaimed,
                stop_after=origin if wrapped else None,
            )
            if reached_origin or (page.reached_end and wrapped):
                self._scan_cursor = None
                self._release_events_no_run_can_reach(retained, still_pending)
                return _in_receipt_order(by_room), False
            if not page.reached_end:
                cursor = page.resume_after
                continue
            # The end of the backlog, reached from part way through it. What
            # came before the resume point has not been looked at, so the rest
            # of the budget goes on it rather than on another pass.
            cursor, wrapped = None, True
        self._scan_cursor = cursor
        return _in_receipt_order(by_room), True

    async def _collect_whole_backlog(self) -> dict[str, list[JournalEvent]]:
        """Group every pending event this worker may act on, front to back.

        What a drain asks for, and the reason it cannot borrow the pump's scan.
        A page budget exists to bound how long one pump pass keeps the loop,
        and a drain has no such need -- it already loops until the backlog
        stops moving, and it is that loop, not one pass of it, that has to see
        every event.
        """
        by_room = self._reclaim_lost_deferrals()
        reclaimed = frozenset(event.event_id for events in by_room.values() for event in events)
        retained = self.retained_event_ids()
        still_pending: set[str] = set(reclaimed)
        cursor: int | None = None
        while True:
            page = await self.store.pending(limit=_BATCH_SIZE, after_receipt_order=cursor)
            self._collect_page(page, by_room, still_pending, already_taken=reclaimed, stop_after=None)
            if page.reached_end:
                self._release_events_no_run_can_reach(retained, still_pending)
                return _in_receipt_order(by_room)
            cursor = page.resume_after

    def _collect_page(
        self,
        page: Iterable[JournalEvent],
        by_room: dict[str, list[JournalEvent]],
        still_pending: set[str],
        *,
        already_taken: frozenset[str],
        stop_after: int | None,
    ) -> bool:
        """Group one page's dispatchable events by room, stopping at ``stop_after``.

        Returns whether the page ran into that stop, which is how a wrapped
        pass recognises the position it set out from. Every event on the page
        is noted as pending regardless of whether this pass may act on it: a
        turn still running is not a run that will never come.
        """
        for event in page:
            if stop_after is not None and event.receipt_order > stop_after:
                return True
            still_pending.add(event.event_id)
            if event.event_id in already_taken:
                # The reclaim at the top of this pass already took it back.
                continue
            if event.event_id in self._running_off_lane:
                # Its caller is inside the handler right now, and releases the
                # claim with a wake so a later pass reconsiders it.
                continue
            if event.event_id in self._deferred:
                # Handed to an owner the reclaim found still alive.
                continue
            by_room.setdefault(event.room_id, []).append(event)
        return False

    def _release_events_no_run_can_reach(self, retained: frozenset[str], still_pending: set[str]) -> None:
        """Give back the parsed objects held for rows that are no longer pending.

        The parsed object nio produced is handed back by the run that uses it,
        and settlement is not always preceded by a run: a membership fence
        settles the turn-backed work it has just made unanswerable, and the row
        simply stops being returned. Nothing then asks about that event again,
        so what stays behind is a room, an event, and the message text inside
        it, for the life of the process.

        Only a pass that reached the end of the backlog may say this, because
        only it can distinguish "not pending" from "not in this window". And
        only for objects already held when the pass began: one handed over
        while the scan was already past its row is late, not absent.
        """
        unreachable = retained - still_pending
        if not unreachable:
            return
        self.release_retained(unreachable)
        logger.info("pending_event_live_objects_released", count=len(unreachable))

    def _reclaim_lost_deferrals(self) -> dict[str, list[JournalEvent]]:
        """Take back every deferral whose owner is gone, wherever it sits.

        Deferral is a promise that some owner will call ``release``. Nothing
        makes that owner keep the promise, and the event is durably pending the
        whole time, so an owner that dies quietly leaves work that no later
        admission and no retry will ever reveal. Asking whether the owner still
        exists turns that into a bounded outage instead of one that lasts until
        the process restarts.

        Asked of the deferrals themselves rather than of whatever the scan's
        window happens to cover, because those are different sets. A backlog
        too large for one pass keeps the window ahead of a deferral sitting
        behind it for as long as the overload lasts, and the outage this is
        supposed to bound then lasts exactly as long as the one it replaced.
        """
        by_room: dict[str, list[JournalEvent]] = {}
        for event in tuple(self._deferred.values()):
            if self.deferral_is_live(event):
                continue
            self._deferred.pop(event.event_id, None)
            logger.warning(
                "pending_event_deferral_owner_lost",
                event_id=event.event_id,
                kind=event.kind.value,
                room_id=event.room_id,
            )
            by_room.setdefault(event.room_id, []).append(event)
        return by_room

    async def _run_lane(self, events: list[JournalEvent]) -> None:
        """Run one room's events in receipt order, stopping at the first failure.

        Stopping matters: if event two fails and event three still ran, the
        room's conversation would be answered out of order, and the retry of
        event two would then arrive after its own reply.

        The store reads and writes around the handler are inside the same
        failure, because they fail for the same reasons it does and leave the
        same work owed. Left outside, they would instead fault the lane task
        itself: nothing retrieves that exception, no room is recorded as
        failed, and no retry is scheduled -- so a failed settlement would sit
        until some unrelated event woke the pump, and then run its handler a
        second time.
        """
        room_id = events[0].room_id if events else ""
        for event in events:
            try:
                if not await self.store.is_pending(event.event_id):
                    self._deferred.pop(event.event_id, None)
                    continue
                if not await self.handle(event):
                    self._deferred[event.event_id] = event
                    continue
                self._deferred.pop(event.event_id, None)
                await self.store.settle(event.event_id)
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
        self._failed_rooms.discard(room_id)
