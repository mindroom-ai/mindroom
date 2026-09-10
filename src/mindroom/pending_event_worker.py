"""Discover rooms globally; let each reserved room lane own ordered replay.

The journal owns durable work and replay eligibility. Discovery only finds
rooms: its rotating cursor never decides which event in a room runs next.
Each room keeps its own page position across bounded passes and cooldowns.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mindroom.cancellation import request_task_cancel
from mindroom.logging_config import get_logger
from mindroom.runtime_shutdown import GENERIC_SHUTDOWN, RuntimeShutdownIntent

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

    from mindroom.event_journal import JournalEvent, ReplayView

logger = get_logger(__name__)

_INITIAL_RETRY_DELAY_SECONDS = 1.0
_MAX_RETRY_DELAY_SECONDS = 30.0
_BATCH_SIZE = 128
_MAX_SCAN_PAGES = 16
_DEFERRAL_SCAN_SECONDS = 30.0

type _EventHandler = Callable[[JournalEvent], Awaitable[bool]]
type _DeferralLivenessProbe = Callable[[JournalEvent], bool]


def _assume_owner_is_live(event: JournalEvent) -> bool:
    """A worker without an owner probe must honor every deferred handoff."""
    del event
    return True


@dataclass
class _RoomProgress:
    """The lane advances its cursor; outside notifications only request rewinds."""

    cursor: int | None = None
    rewind_before: int | None = None


@dataclass
class _RoomRetry:
    """Cooldown history; the failed receipt controls reset, never admission."""

    delay_seconds: float
    task: asyncio.Task[None]
    failed_receipt_order: int | None


@dataclass(frozen=True)
class _RoomPass:
    """A drain observes this completed pass without borrowing its lane."""

    attempted: int
    more: bool = False
    failed: bool = False


@dataclass
class PendingEventWorker:
    """Run one ordered lane per room while keeping unsettled work durable."""

    store: ReplayView
    handle: _EventHandler
    runtime_generation: str = "unmanaged"
    deferral_is_live: _DeferralLivenessProbe = _assume_owner_is_live
    deferral_scan_seconds: float = _DEFERRAL_SCAN_SECONDS
    _lanes: dict[str, asyncio.Task[_RoomPass]] = field(default_factory=dict, init=False, repr=False)
    _rooms: dict[str, _RoomProgress] = field(default_factory=dict, init=False, repr=False)
    _ready_rooms: set[str] = field(default_factory=set, init=False, repr=False)
    _room_retries: dict[str, _RoomRetry] = field(default_factory=dict, init=False, repr=False)
    _deferred: dict[str, JournalEvent] = field(default_factory=dict, init=False, repr=False)
    _wake: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)
    _pump: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _retry: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _deferral_scan: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _retry_delay_seconds: float = field(default=_INITIAL_RETRY_DELAY_SECONDS, init=False, repr=False)
    _scan_cursor: int | None = field(default=None, init=False, repr=False)
    _stopped: bool = field(default=False, init=False, repr=False)
    _process_shutdown: bool = field(default=False, init=False, repr=False)
    _stop_generation: int = field(default=0, init=False, repr=False)

    def start(self) -> None:
        """Start discovery, including work left pending by a previous process."""
        if self._pump is not None and not self._pump.done():
            return
        self._stopped = False
        self._process_shutdown = False
        self._wake.set()
        self._pump = asyncio.create_task(self._run(), name="pending_event_worker")

    def wake(self, *, room_id: str | None = None) -> None:
        """Signal admission, or synchronously rewind a room after a retry handoff."""
        if self._stopped:
            return
        if room_id is not None:
            self._queue_room(room_id, rewind_before=0)
            if self._pump is not None and not self._pump.done():
                self._start_lane(room_id)
        self._wake.set()

    def release(self, event_ids: Iterable[str]) -> None:
        """Forget downstream ownership after its durable handoff.

        This remains an in-memory operation callable from terminal handoffs.
        A retry additionally wakes its room, invalidating the current page
        even after an approval source has left this cache.
        """
        for event_id in event_ids:
            self._deferred.pop(event_id, None)

    def _owned_tasks(self) -> tuple[asyncio.Task[object], ...]:
        return tuple(
            task
            for task in (
                self._pump,
                self._retry,
                self._deferral_scan,
                *self._lanes.values(),
                *(retry.task for retry in self._room_retries.values()),
            )
            if task is not None
        )

    @property
    def pending_task_count(self) -> int:
        """Count owners that still require runtime resources."""
        return sum(not task.done() for task in self._owned_tasks())

    def begin_shutdown(self, *, shutdown_intent: RuntimeShutdownIntent = GENERIC_SHUTDOWN) -> None:
        """Close admission and mark cancellations before teardown can yield."""
        process_shutdown = shutdown_intent.stop_reason == "shutdown"
        if self._stopped and (not process_shutdown or self._process_shutdown):
            return
        if not self._stopped:
            self._stop_generation += 1
        self._stopped = True
        self._process_shutdown = process_shutdown
        for task in self._owned_tasks():
            if not task.done():
                request_task_cancel(
                    task,
                    cancel_source=shutdown_intent.cancel_source,
                    process_shutdown=process_shutdown,
                )

    async def wait_stopped(self, *, timeout_seconds: float | None) -> bool:
        """Retain unfinished owners when the caller's shutdown budget expires."""
        tasks = self._owned_tasks()
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=timeout_seconds)
            if pending:
                return False
            for task in done:
                if not task.cancelled():
                    task.result()
        self._pump = None
        self._retry = None
        self._deferral_scan = None
        self._lanes.clear()
        self._rooms.clear()
        self._ready_rooms.clear()
        self._room_retries.clear()
        self._scan_cursor = None
        return True

    async def stop(self) -> None:
        """Stop owners without settling their unfinished journal work."""
        self.begin_shutdown()
        await self.wait_stopped(timeout_seconds=None)

    def _queue_room(self, room_id: str, *, rewind_before: int | None = None) -> None:
        progress = self._rooms.setdefault(room_id, _RoomProgress())
        if rewind_before is not None:
            previous = progress.rewind_before
            progress.rewind_before = rewind_before if previous is None else min(previous, rewind_before)
        self._ready_rooms.add(room_id)

    def _queue_lost_owners(self) -> None:
        """Probe globally, but leave reclamation to the room's reserved owner."""
        for event in tuple(self._deferred.values()):
            if not self.deferral_is_live(event):
                self._queue_room(event.room_id)

    async def _discover_rooms(self, *, whole_backlog: bool = False) -> tuple[set[str], bool]:
        """Find rooms without passing global scan slices to their lanes."""
        rooms: set[str] = set()
        origin = None if whole_backlog else self._scan_cursor
        cursor = origin
        wrapped = origin is None
        pages = 0
        while whole_backlog or pages < _MAX_SCAN_PAGES:
            page = await self.store.pending(
                limit=_BATCH_SIZE,
                after_receipt_order=cursor,
                runtime_generation=self.runtime_generation,
            )
            pages += 1
            reached_origin = False
            for event in page:
                if wrapped and origin is not None and event.receipt_order > origin:
                    reached_origin = True
                    break
                if event.event_id not in self._deferred:
                    rooms.add(event.room_id)
            if reached_origin or (page.reached_end and wrapped):
                if not whole_backlog:
                    self._scan_cursor = None
                return rooms, False
            if page.reached_end:
                cursor, wrapped = None, True
            else:
                cursor = page.resume_after
        self._scan_cursor = cursor
        return rooms, True

    async def drain_once(self) -> int:
        """Drain eligible room work through the same owners, without waiting out cooldowns."""
        generation = self._stop_generation
        if self._stopped:
            return 0
        rooms, _more = await self._discover_rooms(whole_backlog=True)
        if self._stopped or generation != self._stop_generation:
            return 0
        self._queue_lost_owners()
        rooms.update(self._ready_rooms)
        return sum(
            await asyncio.gather(
                *(self._drain_room(room_id, stop_generation=generation) for room_id in rooms),
            ),
        )

    async def _drain_room(self, room_id: str, *, stop_generation: int) -> int:
        attempted = 0
        while not self._stopped and stop_generation == self._stop_generation:
            active = self._lanes.get(room_id)
            if active is not None and not active.done():
                await asyncio.wait([active])
                continue
            if self._room_is_backing_off(room_id):
                return attempted
            self._queue_room(room_id)
            lane = self._start_lane(room_id)
            if lane is None:
                return attempted
            await asyncio.wait([lane])
            outcome = lane.result()
            attempted += outcome.attempted
            if outcome.failed or (not outcome.more and room_id not in self._ready_rooms):
                return attempted
        return attempted

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
        started = self._start_ready_rooms()
        rooms, more_remains = await self._discover_rooms()
        if self._stopped:
            return
        self._queue_lost_owners()
        for room_id in rooms:
            self._queue_room(room_id)
        started = self._start_ready_rooms() or started
        if more_remains:
            if started:
                self._wake.set()
            else:
                self._schedule_retry()
        else:
            self._retry_delay_seconds = _INITIAL_RETRY_DELAY_SECONDS
        self._schedule_deferral_scan()

    def _start_ready_rooms(self) -> bool:
        started = False
        for room_id in tuple(self._ready_rooms):
            if self._start_lane(room_id) is not None:
                started = True
        return started

    def _start_lane(self, room_id: str) -> asyncio.Task[_RoomPass] | None:
        """Reserve before reading; every caller shares this admission boundary."""
        active = self._lanes.get(room_id)
        if self._stopped or self._room_is_backing_off(room_id) or (active is not None and not active.done()):
            return None
        self._rooms.setdefault(room_id, _RoomProgress())
        self._ready_rooms.discard(room_id)
        lane = asyncio.create_task(self._run_room(room_id), name=f"pending_event_lane_{room_id}")
        self._lanes[room_id] = lane
        lane.add_done_callback(lambda task: self._lane_finished(room_id, task))
        return lane

    def _lane_finished(self, room_id: str, lane: asyncio.Task[_RoomPass]) -> None:
        if self._lanes.get(room_id) is not lane:
            return
        del self._lanes[room_id]
        if self._stopped or lane.cancelled():
            return
        outcome = lane.result()
        if outcome.more:
            self._ready_rooms.add(room_id)
        if room_id in self._ready_rooms:
            if self._pump is not None and not self._pump.done():
                self._start_lane(room_id)
        elif room_id not in self._room_retries:
            # An exhausted room starts a fresh scan on its next admission.
            self._rooms.pop(room_id, None)
        self._schedule_deferral_scan()

    def _room_is_backing_off(self, room_id: str) -> bool:
        retry = self._room_retries.get(room_id)
        return retry is not None and not retry.task.done()

    def _schedule_room_retry(self, room_id: str, event: JournalEvent | None) -> None:
        logger.exception(
            "pending_event_failed",
            room_id=room_id,
            event_id=None if event is None else event.event_id,
            kind=None if event is None else event.kind.value,
        )
        if self._stopped:
            return
        previous = self._room_retries.get(room_id)
        delay = (
            _INITIAL_RETRY_DELAY_SECONDS
            if previous is None
            else min(previous.delay_seconds * 2, _MAX_RETRY_DELAY_SECONDS)
        )
        failed_receipt = None if previous is None else previous.failed_receipt_order
        if event is not None:
            failed_receipt = event.receipt_order
        task = asyncio.create_task(
            asyncio.sleep(delay),
            name=f"pending_event_room_retry_{room_id}",
        )
        self._room_retries[room_id] = _RoomRetry(delay, task, failed_receipt)
        task.add_done_callback(lambda done: self._room_retry_finished(room_id, done))

    def _room_retry_finished(self, room_id: str, task: asyncio.Task[None]) -> None:
        retry = self._room_retries.get(room_id)
        if self._stopped or task.cancelled() or retry is None or retry.task is not task:
            return
        task.result()
        self._queue_room(room_id)
        if self._pump is not None and not self._pump.done():
            self._start_lane(room_id)

    def _record_room_progress(self, room_id: str, receipt_order: int | None) -> None:
        retry = self._room_retries.get(room_id)
        if retry is not None and (
            retry.failed_receipt_order is None or receipt_order is None or receipt_order >= retry.failed_receipt_order
        ):
            del self._room_retries[room_id]

    def _reclaim_room_deferrals(self, room_id: str, newly_unowned: set[str]) -> None:
        """Only the reserved lane can replace a dead owner with room replay."""
        progress = self._rooms[room_id]
        for event in tuple(self._deferred.values()):
            if event.room_id != room_id or event.event_id in newly_unowned or self.deferral_is_live(event):
                continue
            rewind = event.receipt_order - 1
            previous = progress.rewind_before
            progress.rewind_before = rewind if previous is None else min(previous, rewind)
            self._deferred.pop(event.event_id, None)
            logger.warning(
                "pending_event_deferral_owner_lost",
                event_id=event.event_id,
                kind=event.kind.value,
                room_id=room_id,
            )

    @staticmethod
    def _apply_rewind(progress: _RoomProgress) -> bool:
        requested = progress.rewind_before
        progress.rewind_before = None
        if requested is None:
            return False
        if progress.cursor is not None:
            progress.cursor = min(progress.cursor, requested)
        # A request during an awaited read invalidates its snapshot even when
        # the cursor was already before the newly eligible source.
        return True

    async def _run_room_event(self, event: JournalEvent, seen: set[str], newly_unowned: set[str]) -> bool:
        """Admit against current ownership; False stops the current page."""
        if self._stopped:
            return False
        room_id = event.room_id
        progress = self._rooms[room_id]
        self._reclaim_room_deferrals(room_id, newly_unowned)
        if self._apply_rewind(progress):
            return False
        if event.event_id in self._deferred:
            progress.cursor = event.receipt_order
            return True
        self._record_room_progress(room_id, event.receipt_order - 1)
        seen.add(event.event_id)
        pending = await self.store.is_pending(event.event_id)
        if self._stopped:
            return False
        self._reclaim_room_deferrals(room_id, newly_unowned)
        if self._apply_rewind(progress):
            return False
        if not pending:
            self._deferred.pop(event.event_id, None)
        elif not await self.handle(event):
            self._deferred[event.event_id] = event
            if not self.deferral_is_live(event):
                # The downstream owner can finish before the callback returns.
                # Leave an already ownerless handoff to the next probe, avoiding
                # repeated dispatch within this pass.
                newly_unowned.add(event.event_id)
        else:
            self._deferred.pop(event.event_id, None)
            await self.store.settle(event.event_id)
        progress.cursor = event.receipt_order
        self._record_room_progress(room_id, event.receipt_order)
        return True

    async def _run_room(self, room_id: str) -> _RoomPass:
        """Own selection, callback order, and the continuation of one room."""
        progress = self._rooms[room_id]
        seen: set[str] = set()
        newly_unowned: set[str] = set()
        event: JournalEvent | None = None
        try:
            for _ in range(_MAX_SCAN_PAGES):
                event = None
                self._reclaim_room_deferrals(room_id, newly_unowned)
                self._apply_rewind(progress)
                page = await self.store.pending(
                    room_id=room_id,
                    limit=_BATCH_SIZE,
                    after_receipt_order=progress.cursor,
                    runtime_generation=self.runtime_generation,
                )
                if self._stopped:
                    return _RoomPass(len(seen))
                self._reclaim_room_deferrals(room_id, newly_unowned)
                if self._apply_rewind(progress):
                    continue
                for event in page:
                    if not await self._run_room_event(event, seen, newly_unowned):
                        return _RoomPass(len(seen), more=not self._stopped)
                progress.cursor = page.resume_after
                self._reclaim_room_deferrals(room_id, newly_unowned)
                if self._apply_rewind(progress):
                    continue
                if page.reached_end:
                    progress.cursor = None
                    self._record_room_progress(room_id, None)
                    return _RoomPass(len(seen))
            return _RoomPass(len(seen), more=True)
        except Exception:
            if event is not None:
                self._deferred.pop(event.event_id, None)
                progress.cursor = event.receipt_order - 1
            self._schedule_room_retry(room_id, event)
            return _RoomPass(len(seen), failed=True)

    def _schedule_deferral_scan(self) -> None:
        if self._stopped or not self._deferred or (self._deferral_scan is not None and not self._deferral_scan.done()):
            return
        self._deferral_scan = asyncio.create_task(
            self._scan_after_deferral_delay(),
            name="pending_event_deferral_scan",
        )

    async def _scan_after_deferral_delay(self) -> None:
        await asyncio.sleep(self.deferral_scan_seconds)
        self._wake.set()

    def _schedule_retry(self) -> None:
        if self._stopped or (self._retry is not None and not self._retry.done()):
            return
        self._retry = asyncio.create_task(self._retry_after_delay(), name="pending_event_worker_retry")

    async def _retry_after_delay(self) -> None:
        await asyncio.sleep(self._retry_delay_seconds)
        self._retry_delay_seconds = min(self._retry_delay_seconds * 2, _MAX_RETRY_DELAY_SECONDS)
        self._wake.set()
