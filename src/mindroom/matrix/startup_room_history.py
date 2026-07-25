"""Single-flight per-room thread-history acquisition for startup recovery.

Two startup systems need the same room history. Each bot warms recent thread roots after its first
sync, and stale-stream auto-resume must prove that every interrupted target is still the latest
human work before it resumes anything.

Auto-resume asks for all of its candidate roots at once, so one room walk replaces the per-thread
reconstruction a busy room used to pay. This coordinator removes the remaining duplicate: prewarm
and auto-resume starting on the same room at the same time. Callers join a running scan for that
``(startup generation, cache principal, room id)`` instead of starting a second one.

Ownership rules:

1. A flight runs in its own task, so cancelling a waiter never cancels shared room work.
2. Roots contributed before a flight starts scanning join that scan's scope.
3. Terminal per-root outcomes are remembered for the generation. A flight that fails or is cancelled
   records nothing and releases ownership, so a later attempt retries and no room stays permanently
   claimed but unfinished.
4. ``room_concurrency`` bounds how many rooms scan at once across every principal, and the slot is
   held by the flight rather than by its waiters.

Bookkeeping runs in synchronous critical sections. There is deliberately no ``asyncio.Lock``: every
state transition completes without awaiting, so the event loop already serializes them, and flight
finalization stays safe while a task is being cancelled.
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

# Join the running scan, then start at most one more for roots it did not cover.
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
type _RoomKey = tuple[int, str, str]


@dataclass(eq=False)
class _RoomHistoryFlight:
    """One room walk shared by every caller that joined it."""

    room_id: str
    scan: _StartupRoomScanner
    task_owner: object | None
    roots: set[str]
    frozen: bool = False
    outcomes: dict[str, StartupRootOutcome] = field(default_factory=dict)
    completed: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None


@dataclass
class _RoomHistoryState:
    """Shared startup history bookkeeping for one principal-owned room."""

    resolved: dict[str, StartupRootOutcome] = field(default_factory=dict)
    flight: _RoomHistoryFlight | None = None


@dataclass
class StartupRoomHistoryCoordinator:
    """Coalesce startup thread-history acquisition into one flight per principal-owned room."""

    room_concurrency: int = _STARTUP_ROOM_HISTORY_CONCURRENCY
    _generation: int = field(default=0, init=False)
    _states: dict[_RoomKey, _RoomHistoryState] = field(default_factory=dict, init=False)
    _room_slots: asyncio.Semaphore = field(init=False)

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
    ) -> Mapping[str, StartupRootOutcome]:
        """Certify thread roots for one room, joining a running scan instead of starting a second."""
        pending = {root_id.strip() for root_id in thread_root_ids if root_id and root_id.strip()}
        outcomes: dict[str, StartupRootOutcome] = {}
        if not pending:
            return outcomes

        key: _RoomKey = (self._generation, principal_id, room_id)
        for _attempt in range(_MAX_ACQUIRE_ATTEMPTS):
            known = self._states.get(key)
            if known is not None:
                self._harvest(known.resolved, pending, outcomes)
            if not pending:
                break
            state = self._states.setdefault(key, _RoomHistoryState())
            flight = self._flight_for_roots(key, state, pending, scan=scan, task_owner=task_owner)
            await flight.completed.wait()
            self._harvest(flight.outcomes, pending, outcomes)

        settled = self._states.get(key)
        if settled is not None:
            self._discard_state_if_idle(key, settled)
        # Roots the bounded attempt budget never resolved stay uncertified so callers fail closed,
        # and stay retryable so a later startup attempt can cover them.
        for root_id in pending:
            outcomes[root_id] = StartupRootOutcome.FAILED
        return outcomes

    async def aclose(self) -> None:
        """Cancel owned startup room work and release every waiter."""
        states = tuple(self._states.values())
        self._states.clear()
        cancelled_tasks: list[asyncio.Task[None]] = []
        for state in states:
            flight = state.flight
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
    ) -> _RoomHistoryFlight:
        """Return the flight this caller waits on, starting one only when no scan is running."""
        running = state.flight
        if running is not None:
            if not running.frozen:
                # The scan has not started yet, so these roots still fit inside its scope.
                running.roots |= pending
            # Roots a running scan does not cover are retried on the next attempt rather than
            # starting a second concurrent walk of the same room.
            return running
        flight = _RoomHistoryFlight(
            room_id=key[2],
            scan=scan,
            task_owner=task_owner,
            roots=set(pending),
        )
        state.flight = flight
        flight.task = create_background_task(
            self._run_flight(key, flight),
            name=f"startup_room_history:{flight.room_id}",
            owner=task_owner,
            log_exceptions=False,
        )
        return flight

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
            self._finish_flight(key, flight, None, "cancelled", queue_wait_ms, fetch_ms)
            raise
        except Exception as exc:
            status = "failed"
            logger.warning(
                "startup_room_history_scan_failed",
                startup_generation=key[0],
                room_id=flight.room_id,
                principal_id=key[1],
                candidate_root_count=len(flight.roots),
                error_type=type(exc).__name__,
                error=str(exc),
            )
        self._finish_flight(key, flight, scan_result, status, queue_wait_ms, fetch_ms)

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
        scan_result: StartupRoomScanResult | None,
        status: str,
        queue_wait_ms: float,
        fetch_ms: float,
    ) -> None:
        """Release ownership and wake every waiter."""
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
            self._discard_state_if_idle(key, state)
        flight.completed.set()
        counts: dict[StartupRootOutcome, int] = {}
        for outcome in flight.outcomes.values():
            counts[outcome] = counts.get(outcome, 0) + 1
        logger.info(
            "startup_room_history_completed",
            startup_generation=key[0],
            room_id=key[2],
            principal_id=key[1],
            status=status,
            candidate_root_count=len(flight.roots),
            roots_stored=counts.get(StartupRootOutcome.STORED, 0),
            roots_already_trusted=counts.get(StartupRootOutcome.ALREADY_TRUSTED, 0),
            roots_missing=counts.get(StartupRootOutcome.MISSING, 0),
            roots_truncated=counts.get(StartupRootOutcome.TRUNCATED, 0),
            roots_invalidated=counts.get(StartupRootOutcome.INVALIDATED, 0),
            roots_failed=counts.get(StartupRootOutcome.FAILED, 0),
            room_scan_pages=0 if scan_result is None else scan_result.pages,
            scanned_event_count=0 if scan_result is None else scan_result.scanned_events,
            scan_truncated=False if scan_result is None else scan_result.truncated,
            queue_wait_ms=queue_wait_ms,
            fetch_ms=fetch_ms,
        )

    def _discard_state_if_idle(self, key: _RoomKey, state: _RoomHistoryState) -> None:
        """Drop bookkeeping for rooms with nothing running and nothing worth remembering."""
        if state.flight is not None:
            return
        if key[0] == self._generation and state.resolved:
            return
        self._states.pop(key, None)
