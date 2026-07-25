"""Single-flight per-room thread-history acquisition for startup recovery.

Two independent startup systems need the same room history. Each bot warms recent thread roots
after its first sync, and stale-stream auto-resume must prove that every interrupted target is
still the latest human work before it resumes anything. Both used to reconstruct one thread at a
time, so a busy room with many interrupted threads paid one homeserver ``/messages`` walk per
thread while that room's bulk prewarm was still queued behind them.

This module owns one shared room-history operation keyed by
``(startup generation, cache principal, room id)``. Callers contribute the thread roots they need
and either start the operation or join a running one, so one room walk serves every startup
consumer of that room.

Ownership rules:

1. A flight runs in its own task. Cancelling a waiter never cancels shared room work, and a waiter
   that joins late never restarts work that is already running.
2. Roots contributed before a flight starts scanning join that scan's scope. Roots arriving after
   the scope freezes are collected into exactly one follow-up batch that starts when the running
   flight finishes, so a late candidate can never trigger its own full room scan.
3. Terminal per-root outcomes are remembered for the generation, so a later caller reuses them
   instead of rescanning. A flight that fails or is cancelled records nothing and releases
   ownership, so a later attempt retries and no room stays permanently claimed but unfinished.
4. ``room_concurrency`` bounds how many rooms scan at once across every principal, and the slot is
   held by the flight rather than by its waiters.

All coordinator bookkeeping runs in synchronous critical sections. There is deliberately no
``asyncio.Lock``: every state transition completes without awaiting, so the single-threaded event
loop already serializes them, and flight finalization stays safe while a task is being cancelled.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from mindroom.background_tasks import create_background_task
from mindroom.logging_config import get_logger
from mindroom.timing import elapsed_ms_between, elapsed_ms_since

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Collection, Mapping

logger = get_logger(__name__)

# Two concurrent rooms keep startup progress moving without letting background history work
# saturate the homeserver or the event loop that live dispatch shares.
_STARTUP_ROOM_HISTORY_CONCURRENCY = 2

# One running flight plus the single follow-up batch that collects post-freeze roots.
_MAX_ACQUIRE_ATTEMPTS = 2


class StartupRootOutcome(StrEnum):
    """Per-root result of one shared startup room-history acquisition."""

    STORED = "stored"
    ALREADY_TRUSTED = "already_trusted"
    MISSING = "missing"
    TRUNCATED = "truncated"
    INVALIDATED = "invalidated"
    FAILED = "failed"

    @property
    def certified(self) -> bool:
        """Return whether a trusted durable snapshot now backs this root."""
        return self in {StartupRootOutcome.STORED, StartupRootOutcome.ALREADY_TRUSTED}

    @property
    def retryable(self) -> bool:
        """Return whether a later startup attempt should scan this root again."""
        return self is StartupRootOutcome.FAILED


@dataclass(frozen=True, slots=True)
class StartupRoomScanResult:
    """One executed room scan reported back to the coordinator."""

    outcomes: Mapping[str, StartupRootOutcome]
    pages: int = 0
    scanned_events: int = 0
    truncated: bool = False


type _StartupRoomScanner = Callable[[str, frozenset[str]], Awaitable[StartupRoomScanResult]]


@dataclass(frozen=True, slots=True)
class _StartupRoomHistoryResult:
    """Per-root outcomes one caller observed from shared startup room work."""

    outcomes: Mapping[str, StartupRootOutcome]
    flights_awaited: int = 0
    flights_created: int = 0


@dataclass(eq=False)
class _RoomHistoryFlight:
    """One room walk shared by every caller that joined it."""

    room_id: str
    scan: _StartupRoomScanner
    task_owner: object | None
    roots: set[str]
    scan_index: int
    is_follow_up: bool
    frozen: bool = False
    waiter_count: int = 0
    peak_waiter_count: int = 0
    outcomes: dict[str, StartupRootOutcome] = field(default_factory=dict)
    completed: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None

    def add_waiter(self) -> None:
        """Record one caller waiting on this shared flight."""
        self.waiter_count += 1
        self.peak_waiter_count = max(self.peak_waiter_count, self.waiter_count)

    def remove_waiter(self) -> None:
        """Record that one caller stopped waiting, including after cancellation."""
        self.waiter_count -= 1


@dataclass
class _RoomHistoryState:
    """Shared startup history bookkeeping for one principal-owned room."""

    resolved: dict[str, StartupRootOutcome] = field(default_factory=dict)
    flight: _RoomHistoryFlight | None = None
    follow_up: _RoomHistoryFlight | None = None


type _RoomKey = tuple[int, str, str]


@dataclass
class StartupRoomHistoryCoordinator:
    """Coalesce startup thread-history acquisition into one flight per principal-owned room."""

    room_concurrency: int = _STARTUP_ROOM_HISTORY_CONCURRENCY
    _generation: int = field(default=0, init=False)
    _states: dict[_RoomKey, _RoomHistoryState] = field(default_factory=dict, init=False)
    _room_slots: asyncio.Semaphore = field(init=False)
    _scan_count: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        """Bound how many rooms may scan concurrently across every principal."""
        self._room_slots = asyncio.Semaphore(max(1, self.room_concurrency))

    @property
    def generation(self) -> int:
        """Return the current startup generation used to scope shared room work."""
        return self._generation

    def advance_generation(self) -> None:
        """Start a new startup wave so a later restart may re-certify the same rooms."""
        self._generation += 1
        for key, state in tuple(self._states.items()):
            self._discard_state_if_idle(key, state)

    async def acquire(
        self,
        *,
        principal_id: str,
        room_id: str,
        thread_root_ids: Collection[str],
        scan: _StartupRoomScanner,
        task_owner: object | None = None,
    ) -> _StartupRoomHistoryResult:
        """Certify thread roots for one room through at most one shared scan plus one follow-up."""
        pending = {root_id.strip() for root_id in thread_root_ids if root_id and root_id.strip()}
        outcomes: dict[str, StartupRootOutcome] = {}
        if not pending:
            return _StartupRoomHistoryResult(outcomes=outcomes)

        key: _RoomKey = (self._generation, principal_id, room_id)
        flights_awaited = 0
        flights_created = 0
        for _attempt in range(_MAX_ACQUIRE_ATTEMPTS):
            known = self._states.get(key)
            if known is not None:
                self._harvest(known.resolved, pending, outcomes)
            if not pending:
                break
            state = self._states.setdefault(key, _RoomHistoryState())
            flight, created = self._flight_for_roots(
                key,
                state,
                pending,
                scan=scan,
                task_owner=task_owner,
            )
            flights_awaited += 1
            flights_created += int(created)
            self._log_join(key, flight, joined_root_count=len(pending & flight.roots), created=created)
            flight.add_waiter()
            try:
                await flight.completed.wait()
            finally:
                flight.remove_waiter()
            self._harvest(flight.outcomes, pending, outcomes)

        settled = self._states.get(key)
        if settled is not None:
            self._discard_state_if_idle(key, settled)
        # Roots the bounded attempt budget never resolved stay uncertified so callers fail closed,
        # and stay retryable so a later startup attempt can cover them.
        for root_id in pending:
            outcomes[root_id] = StartupRootOutcome.FAILED
        return _StartupRoomHistoryResult(
            outcomes=outcomes,
            flights_awaited=flights_awaited,
            flights_created=flights_created,
        )

    async def aclose(self) -> None:
        """Cancel owned startup room work and release every waiter."""
        states = tuple(self._states.values())
        self._states.clear()
        cancelled_tasks: list[asyncio.Task[None]] = []
        for state in states:
            for flight in (state.flight, state.follow_up):
                if flight is None:
                    continue
                task = flight.task
                if task is not None and not task.done():
                    task.cancel()
                    cancelled_tasks.append(task)
                    continue
                self._publish_flight_outcomes(flight, scan_result=None)
                flight.completed.set()
        if cancelled_tasks:
            await asyncio.gather(*cancelled_tasks, return_exceptions=True)

    @staticmethod
    def _harvest(
        source: Mapping[str, StartupRootOutcome],
        pending: set[str],
        outcomes: dict[str, StartupRootOutcome],
    ) -> None:
        """Move every already-known root outcome out of the pending set."""
        for root_id in tuple(pending):
            outcome = source.get(root_id)
            if outcome is not None:
                outcomes[root_id] = outcome
                pending.discard(root_id)

    def _flight_for_roots(
        self,
        key: _RoomKey,
        state: _RoomHistoryState,
        pending: set[str],
        *,
        scan: _StartupRoomScanner,
        task_owner: object | None,
    ) -> tuple[_RoomHistoryFlight, bool]:
        """Return the flight this caller waits on and whether that exact flight is new to it.

        The boolean describes the returned flight only. Scheduling a follow-up while returning the
        running flight is a join, not a start; that follow-up has its own scheduled event.
        """
        running = state.flight
        if running is None:
            flight = self._new_flight(key, set(pending), scan=scan, task_owner=task_owner, is_follow_up=False)
            state.flight = flight
            self._start_flight(key, flight)
            return flight, True

        if not running.frozen:
            # The scan has not started yet, so late roots still fit inside its scope.
            running.roots |= pending
            return running, False

        late_roots = pending - running.roots
        if not late_roots:
            return running, False

        follow_up = state.follow_up
        follow_up_created = follow_up is None
        if follow_up is None:
            follow_up = self._new_flight(key, set(late_roots), scan=scan, task_owner=task_owner, is_follow_up=True)
            state.follow_up = follow_up
            logger.info(
                "startup_room_history_follow_up_scheduled",
                startup_generation=key[0],
                room_id=key[2],
                principal_id=key[1],
                root_count=len(late_roots),
                running_scan_index=running.scan_index,
            )
        else:
            follow_up.roots |= late_roots
        if pending & running.roots:
            # Make progress on the running scan first; the follow-up is awaited on the next attempt.
            return running, False
        return follow_up, follow_up_created

    def _new_flight(
        self,
        key: _RoomKey,
        roots: set[str],
        *,
        scan: _StartupRoomScanner,
        task_owner: object | None,
        is_follow_up: bool,
    ) -> _RoomHistoryFlight:
        """Build one unstarted flight for a room."""
        self._scan_count += 1
        return _RoomHistoryFlight(
            room_id=key[2],
            scan=scan,
            task_owner=task_owner,
            roots=roots,
            scan_index=self._scan_count,
            is_follow_up=is_follow_up,
        )

    def _start_flight(self, key: _RoomKey, flight: _RoomHistoryFlight) -> None:
        """Detach one flight so waiter cancellation cannot cancel shared room work."""
        flight.task = create_background_task(
            self._run_flight(key, flight),
            name=f"startup_room_history:{flight.room_id}",
            owner=flight.task_owner,
            log_exceptions=False,
        )

    async def _run_flight(self, key: _RoomKey, flight: _RoomHistoryFlight) -> None:
        """Run one bounded room scan and publish its per-root outcomes exactly once."""
        queued_at = time.perf_counter()
        queue_wait_ms = 0.0
        fetch_ms = 0.0
        scan_result: StartupRoomScanResult | None = None
        status = "completed"
        try:
            async with self._room_slots:
                flight.frozen = True
                scan_started = time.perf_counter()
                queue_wait_ms = elapsed_ms_between(queued_at, scan_started)
                try:
                    scan_result = await flight.scan(flight.room_id, frozenset(flight.roots))
                finally:
                    fetch_ms = elapsed_ms_since(scan_started, clock=time.perf_counter)
        except asyncio.CancelledError:
            self._finish_flight(
                key,
                flight,
                scan_result=None,
                status="cancelled",
                queue_wait_ms=queue_wait_ms,
                fetch_ms=fetch_ms,
            )
            raise
        except Exception as exc:
            status = "failed"
            logger.warning(
                "startup_room_history_scan_failed",
                startup_generation=key[0],
                room_id=flight.room_id,
                principal_id=key[1],
                scan_index=flight.scan_index,
                candidate_root_count=len(flight.roots),
                error_type=type(exc).__name__,
                error=str(exc),
            )
        self._finish_flight(
            key,
            flight,
            scan_result=scan_result,
            status=status,
            queue_wait_ms=queue_wait_ms,
            fetch_ms=fetch_ms,
        )

    @staticmethod
    def _publish_flight_outcomes(
        flight: _RoomHistoryFlight,
        *,
        scan_result: StartupRoomScanResult | None,
    ) -> None:
        """Give every requested root exactly one terminal outcome for this flight."""
        outcomes = dict(scan_result.outcomes) if scan_result is not None else {}
        default_outcome = StartupRootOutcome.MISSING if scan_result is not None else StartupRootOutcome.FAILED
        for root_id in flight.roots:
            outcomes.setdefault(root_id, default_outcome)
        flight.outcomes = outcomes

    def _finish_flight(
        self,
        key: _RoomKey,
        flight: _RoomHistoryFlight,
        *,
        scan_result: StartupRoomScanResult | None,
        status: str,
        queue_wait_ms: float,
        fetch_ms: float,
    ) -> None:
        """Release ownership, promote any follow-up batch, and wake every waiter."""
        self._publish_flight_outcomes(flight, scan_result=scan_result)
        state = self._states.get(key)
        if state is not None:
            if state.flight is flight:
                state.flight = None
            # Retryable outcomes are deliberately not remembered: a failed or cancelled room must
            # stay claimable so a later startup attempt can scan it again.
            state.resolved.update(
                {root_id: outcome for root_id, outcome in flight.outcomes.items() if not outcome.retryable},
            )
            follow_up = state.follow_up
            if follow_up is not None and state.flight is None:
                state.follow_up = None
                state.flight = follow_up
                self._start_flight(key, follow_up)
            self._discard_state_if_idle(key, state)
        flight.completed.set()
        self._log_completion(
            key,
            flight,
            status=status,
            queue_wait_ms=queue_wait_ms,
            fetch_ms=fetch_ms,
            scan_result=scan_result,
        )

    def _discard_state_if_idle(self, key: _RoomKey, state: _RoomHistoryState) -> None:
        """Drop bookkeeping for rooms with nothing running and nothing worth remembering."""
        if state.flight is not None or state.follow_up is not None:
            return
        if key[0] == self._generation and state.resolved:
            return
        self._states.pop(key, None)

    @staticmethod
    def _log_join(
        key: _RoomKey,
        flight: _RoomHistoryFlight,
        *,
        joined_root_count: int,
        created: bool,
    ) -> None:
        """Record whether one caller started shared room work or joined existing work."""
        logger.info(
            "startup_room_history_started" if created else "startup_room_history_joined",
            startup_generation=key[0],
            room_id=key[2],
            principal_id=key[1],
            scan_index=flight.scan_index,
            candidate_root_count=len(flight.roots),
            joined_root_count=joined_root_count,
            follow_up=flight.is_follow_up,
        )

    @staticmethod
    def _log_completion(
        key: _RoomKey,
        flight: _RoomHistoryFlight,
        *,
        status: str,
        queue_wait_ms: float,
        fetch_ms: float,
        scan_result: StartupRoomScanResult | None,
    ) -> None:
        """Record one shared room-history outcome with safe counts and timings only."""
        counts: dict[StartupRootOutcome, int] = {}
        for outcome in flight.outcomes.values():
            counts[outcome] = counts.get(outcome, 0) + 1
        logger.info(
            "startup_room_history_completed",
            startup_generation=key[0],
            room_id=key[2],
            principal_id=key[1],
            scan_index=flight.scan_index,
            follow_up=flight.is_follow_up,
            status=status,
            candidate_root_count=len(flight.roots),
            roots_stored=counts.get(StartupRootOutcome.STORED, 0),
            roots_already_trusted=counts.get(StartupRootOutcome.ALREADY_TRUSTED, 0),
            roots_missing=counts.get(StartupRootOutcome.MISSING, 0),
            roots_truncated=counts.get(StartupRootOutcome.TRUNCATED, 0),
            roots_invalidated=counts.get(StartupRootOutcome.INVALIDATED, 0),
            roots_failed=counts.get(StartupRootOutcome.FAILED, 0),
            waiter_count=flight.peak_waiter_count,
            room_scan_pages=0 if scan_result is None else scan_result.pages,
            scanned_event_count=0 if scan_result is None else scan_result.scanned_events,
            scan_truncated=False if scan_result is None else scan_result.truncated,
            queue_wait_ms=queue_wait_ms,
            fetch_ms=fetch_ms,
        )
