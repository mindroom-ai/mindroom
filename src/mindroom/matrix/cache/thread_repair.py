"""Single-flight ownership, fan-out bounding, and retained live deltas for thread-cache repair.

Repair admission has two tiers. An *interactive* repair backs a caller that is waiting for the
history right now, so it always runs, queueing only behind a global ceiling set well above any real
dispatch fan-out. A *speculative* repair is launched by a live append that found no cached snapshot;
nobody is waiting on its result, so it is dropped rather than queued whenever it would add load:

1. while any flight for the same thread is already scanning, whatever caller contract owns it;
2. while that thread is inside its post-repair cooldown;
3. while the speculative concurrency budget is spent or an interactive repair is waiting for a slot;
4. while a sync replay batch is being applied.

Dropping is safe because the thread stays marked stale, so the next read repairs it interactively.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Collection, Iterator

type _ThreadRepairFlightKey = tuple[str, str, str, bool, bool]
type _ThreadRepairDeltaKey = tuple[str, str, str]

# Retained deltas only cover the window where a homeserver scan can miss a just-certified event.
# Once a delta is older than this, any new scan already observes it, so keeping it only wastes memory.
_DELTA_RETENTION_SECONDS = 60.0

# Ceiling on scans in progress at once. This is a safety valve against a pathological storm, not a
# throttle: it sits well above the widest fan-out a real dispatch produces, because an interactive
# repair is a user-facing read and queueing one behind another is latency a caller pays for.
_MAX_CONCURRENT_THREAD_REPAIRS = 64

# The working bound. Every repair is a full history scan contending for the same serialized cache
# write path the Matrix sync callback is blocked on, and nobody is waiting on a speculative one,
# so only a couple run at a time however many threads are stale.
_MAX_CONCURRENT_SPECULATIVE_THREAD_REPAIRS = 2

# One speculative scan per thread per window. A thread that is still broken afterwards is repaired
# by the next read, which is interactive and exempt.
_SPECULATIVE_THREAD_REPAIR_COOLDOWN_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class _RetainedDelta:
    """One certified event source held until a scan or append is proven to include it."""

    event_source: dict[str, Any]
    retained_at: float


@dataclass(frozen=True, slots=True)
class _RepairFailureBackoff:
    """Current capped delay after consecutive repair failures."""

    delay_seconds: float
    retry_after: float


class ThreadRepairBackoffError(RuntimeError):
    """Raised when a failed repair is still inside its bounded retry delay."""

    def __init__(self, retry_after_seconds: float) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"thread cache repair backoff active for {retry_after_seconds:.3f}s")


class ThreadRepairSuppressedError(RuntimeError):
    """Raised when one speculative repair is dropped to bound global repair fan-out."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"speculative thread cache repair suppressed: {reason}")


@dataclass
class ThreadRepairRegistry:
    """Own principal-scoped repair flights, failure backoff, and certified deltas."""

    failure_backoff_seconds: float = 1.0
    max_failure_backoff_seconds: float = 30.0
    delta_retention_seconds: float = _DELTA_RETENTION_SECONDS
    max_concurrent_repairs: int = _MAX_CONCURRENT_THREAD_REPAIRS
    max_concurrent_speculative_repairs: int = _MAX_CONCURRENT_SPECULATIVE_THREAD_REPAIRS
    speculative_cooldown_seconds: float = _SPECULATIVE_THREAD_REPAIR_COOLDOWN_SECONDS
    clock: Callable[[], float] = time.monotonic
    _tasks: dict[_ThreadRepairFlightKey, asyncio.Task[object]] = field(default_factory=dict, init=False)
    _failure_backoffs: dict[_ThreadRepairFlightKey, _RepairFailureBackoff] = field(default_factory=dict, init=False)
    _deltas: dict[_ThreadRepairDeltaKey, dict[str, _RetainedDelta]] = field(default_factory=dict, init=False)
    _speculative_cooldowns: dict[_ThreadRepairDeltaKey, float] = field(default_factory=dict, init=False)
    _interactive_joins: dict[_ThreadRepairFlightKey, int] = field(default_factory=dict, init=False)
    _speculative_flights: set[_ThreadRepairFlightKey] = field(default_factory=set, init=False)
    _slot_waiters: list[asyncio.Future[None]] = field(default_factory=list, init=False)
    _running_repairs: int = field(default=0, init=False)
    _running_speculative_repairs: int = field(default=0, init=False)
    _speculative_suppression_depth: int = field(default=0, init=False)

    @staticmethod
    def _thread_key(key: _ThreadRepairFlightKey) -> _ThreadRepairDeltaKey:
        """Return the thread one caller contract repairs, without the contract itself."""
        coordination_scope, room_id, thread_id, _hydrate_sidecars, _allow_stale_fallback = key
        return coordination_scope, room_id, thread_id

    def _active_task(self, key: _ThreadRepairFlightKey) -> asyncio.Task[object] | None:
        task = self._tasks.get(key)
        if task is None:
            return None
        if task.done():
            self._tasks.pop(key, None)
            return None
        return task

    def _clear_task(self, key: _ThreadRepairFlightKey, task: asyncio.Task[object]) -> None:
        if self._tasks.get(key) is task:
            self._tasks.pop(key, None)
            self._speculative_flights.discard(key)

    def _has_active_task(self, key: _ThreadRepairDeltaKey) -> bool:
        """Return whether any caller contract owns this thread's repair."""
        for flight_key in tuple(self._tasks):
            if flight_key[:3] == key and self._active_task(flight_key) is not None:
                return True
        return False

    def _drop_stale_failure_backoffs(self, now: float) -> None:
        stale_before = now - self.max_failure_backoff_seconds
        self._failure_backoffs = {
            key: backoff for key, backoff in self._failure_backoffs.items() if backoff.retry_after > stale_before
        }

    def _record_failure(self, key: _ThreadRepairFlightKey) -> None:
        now = self.clock()
        self._drop_stale_failure_backoffs(now)
        previous = self._failure_backoffs.get(key)
        delay_seconds = (
            self.failure_backoff_seconds
            if previous is None
            else min(previous.delay_seconds * 2, self.max_failure_backoff_seconds)
        )
        self._failure_backoffs[key] = _RepairFailureBackoff(
            delay_seconds=delay_seconds,
            retry_after=now + delay_seconds,
        )

    def retry_after_seconds(self, key: _ThreadRepairFlightKey) -> float:
        """Return remaining repair backoff for one key."""
        backoff = self._failure_backoffs.get(key)
        if backoff is None:
            return 0.0
        return max(0.0, backoff.retry_after - self.clock())

    @contextmanager
    def suppress_speculative_repairs(self) -> Iterator[None]:
        """Drop speculative repairs while one sync replay batch is being applied.

        Replay re-delivers events whose threads are being rewritten anyway, so speculative scans
        started from it are near-certain to lose the guarded replacement race and only add load to
        the write path the sync callback is already blocked on.
        """
        self._speculative_suppression_depth += 1
        try:
            yield
        finally:
            self._speculative_suppression_depth = max(0, self._speculative_suppression_depth - 1)

    def _speculative_cooldown_active(self, key: _ThreadRepairDeltaKey) -> bool:
        """Return whether this thread was scanned too recently.

        A pure read: expired entries are dropped when the next cooldown is armed, which keeps this
        O(1) on the hottest path and keeps the public suppression query free of side effects.
        """
        retry_after = self._speculative_cooldowns.get(key)
        return retry_after is not None and retry_after > self.clock()

    def speculative_suppression_reason(
        self,
        key: _ThreadRepairDeltaKey,
        *,
        ignore_active_flight: bool = False,
    ) -> str | None:
        """Return why one speculative repair must be dropped, or ``None`` when it may run.

        The key is the thread, not the caller contract, so a speculative trigger never opens a
        second scan of a thread another contract is already scanning. A flight re-checking itself
        just before it scans passes ``ignore_active_flight`` so it does not see its own ownership.
        """
        if self._speculative_suppression_depth > 0:
            return "sync_replay"
        if not ignore_active_flight and self._has_active_task(key):
            return "repair_in_flight"
        if self._speculative_cooldown_active(key):
            return "recently_repaired"
        if self._running_speculative_repairs >= self.max_concurrent_speculative_repairs:
            return "speculative_concurrency_limit"
        if self._slot_waiters or self._running_repairs >= self.max_concurrent_repairs:
            return "repair_concurrency_limit"
        return None

    @contextmanager
    def _joined_interactively(self, key: _ThreadRepairFlightKey) -> Iterator[None]:
        """Record that a waiting caller depends on this flight for as long as it is joined."""
        self._interactive_joins[key] = self._interactive_joins.get(key, 0) + 1
        try:
            yield
        finally:
            remaining = self._interactive_joins.get(key, 1) - 1
            if remaining > 0:
                self._interactive_joins[key] = remaining
            else:
                self._interactive_joins.pop(key, None)

    def _discard_slot_waiter(self, waiter: asyncio.Future[None]) -> None:
        self._slot_waiters = [existing for existing in self._slot_waiters if existing is not waiter]

    def _wake_next_slot_waiter(self) -> None:
        """Hand the just-released slot to the longest-waiting caller."""
        while self._slot_waiters:
            waiter = self._slot_waiters.pop(0)
            if not waiter.done():
                # Reserved here rather than by the waiter itself: a newcomer resuming first would
                # otherwise take the slot and push the waiter back to the end of the queue.
                self._running_repairs += 1
                waiter.set_result(None)
                return

    async def _acquire_repair_slot(self, *, speculative: bool) -> None:
        """Take one global repair slot, waiting only for callers someone is blocked on.

        The slot is taken immediately before the scan, never while the flight is still queued behind
        same-thread predecessors, so a slot always measures work actually in progress. Speculative
        callers re-check capacity without blocking first, so only interactive callers ever join the
        waiter queue, and they are served in arrival order.
        """
        if not self._slot_waiters and self._running_repairs < self.max_concurrent_repairs:
            self._running_repairs += 1
        else:
            waiter = asyncio.get_running_loop().create_future()
            self._slot_waiters.append(waiter)
            try:
                await waiter
            except asyncio.CancelledError:
                self._discard_slot_waiter(waiter)
                if waiter.done() and not waiter.cancelled():
                    # The slot was handed over before the cancellation landed; pass it along.
                    self._release_repair_slot(speculative=False)
                raise
        if speculative:
            self._running_speculative_repairs += 1

    def _release_repair_slot(self, *, speculative: bool) -> None:
        self._running_repairs = max(0, self._running_repairs - 1)
        if speculative:
            self._running_speculative_repairs = max(0, self._running_speculative_repairs - 1)
        self._wake_next_slot_waiter()

    def _arm_speculative_cooldown(self, key: _ThreadRepairFlightKey) -> None:
        """Hold off further speculative scans of this thread after one has just run.

        Expired entries are swept here rather than on the append path: a repair completing is rare
        next to an append, and a thread that is never speculatively re-checked would otherwise keep
        its entry for the life of the process.
        """
        now = self.clock()
        self._speculative_cooldowns = {
            cooled_key: retry_after
            for cooled_key, retry_after in self._speculative_cooldowns.items()
            if retry_after > now
        }
        self._speculative_cooldowns[self._thread_key(key)] = now + self.speculative_cooldown_seconds

    def _admission_error(
        self,
        key: _ThreadRepairFlightKey,
        *,
        speculative: bool,
        bypass_failure_backoff: bool,
    ) -> Exception | None:
        """Return why this caller may not start a new scan right now."""
        if speculative:
            suppression_reason = self.speculative_suppression_reason(self._thread_key(key))
            if suppression_reason is not None:
                return ThreadRepairSuppressedError(suppression_reason)
        retry_after_seconds = self.retry_after_seconds(key)
        if retry_after_seconds > 0 and not bypass_failure_backoff:
            return ThreadRepairBackoffError(retry_after_seconds)
        return None

    async def _run_in_repair_slot[T](
        self,
        key: _ThreadRepairFlightKey,
        run_repair: Callable[[], Awaitable[T]],
        *,
        speculative: bool,
    ) -> T:
        """Run one admitted repair while holding exactly one global slot.

        Reached only once same-thread predecessors have drained, so a held slot always measures a
        scan in progress. Capacity is re-checked for speculative work because that queue wait can be
        long enough for the runtime to have filled up, or for another flight to have fixed a thread.

        A flight an interactive caller has joined stops being speculative: declining it would raise
        into a read that is waiting on this exact result, and that caller is owed the scan.
        """
        if speculative and self._interactive_joins.get(key):
            speculative = False
        if speculative:
            deferred_reason = self.speculative_suppression_reason(
                self._thread_key(key),
                ignore_active_flight=True,
            )
            if deferred_reason is not None:
                raise ThreadRepairSuppressedError(deferred_reason)
        await self._acquire_repair_slot(speculative=speculative)
        try:
            return await run_repair()
        finally:
            self._release_repair_slot(speculative=speculative)
            self._arm_speculative_cooldown(key)

    async def _join_running_flight[T](
        self,
        key: _ThreadRepairFlightKey,
        active_task: asyncio.Task[object],
        *,
        speculative: bool,
        result_needs_own_flight: Callable[[T], bool] | None,
    ) -> tuple[bool, T]:
        """Await the flight this caller joins and report whether its result settles the call.

        The joined flight's tier is read at the instant of the join, so no flight can slip in
        between. An interactive caller that inherited a speculative flight's unusable result is
        owed its own scan, unless another flight already owns the thread and can be joined on
        equal terms.
        """
        if speculative:
            return True, cast("T", await asyncio.shield(active_task))
        joined_speculative_flight = key in self._speculative_flights
        with self._joined_interactively(key):
            value = cast("T", await asyncio.shield(active_task))
        if not joined_speculative_flight or result_needs_own_flight is None or not result_needs_own_flight(value):
            return True, value
        return self._active_task(key) is not None, value

    async def run[T](
        self,
        key: _ThreadRepairFlightKey,
        *,
        schedule: Callable[[Callable[[], Awaitable[T]]], asyncio.Task[T]],
        repair: Callable[[], Awaitable[T]],
        result_arms_backoff: Callable[[T], bool],
        result_needs_own_flight: Callable[[T], bool] | None = None,
        bypass_failure_backoff: bool = False,
        speculative: bool = False,
    ) -> T:
        """Join or start one shielded repair and update backoff from its outcome.

        Authoritative untimed reads may bypass an existing delay while preserving its failure count.
        A speculative caller raises ``ThreadRepairSuppressedError`` instead of adding a scan whenever
        the fan-out gate declines it.

        Joining a flight means inheriting its contract, and a speculative flight rescans a lost
        guarded replacement one fewer time than a waiting reader is owed. ``result_needs_own_flight``
        lets an interactive caller that inherited such a result repair again under its own contract.
        The tier is read at the instant of the join, so no flight can start in between.
        """

        async def run_repair() -> T:
            try:
                value = await repair()
            except Exception:
                if self._tasks.get(key) is asyncio.current_task():
                    self._record_failure(key)
                raise
            if self._tasks.get(key) is asyncio.current_task():
                if result_arms_backoff(value):
                    self._record_failure(key)
                else:
                    self._failure_backoffs.pop(key, None)
            return value

        active_task = self._active_task(key)
        if active_task is not None:
            settled, joined_value = await self._join_running_flight(
                key,
                active_task,
                speculative=speculative,
                result_needs_own_flight=result_needs_own_flight,
            )
            if settled:
                return joined_value
        admission_error = self._admission_error(
            key,
            speculative=speculative,
            bypass_failure_backoff=bypass_failure_backoff,
        )
        if admission_error is not None:
            raise admission_error

        task = schedule(lambda: self._run_in_repair_slot(key, run_repair, speculative=speculative))
        self._tasks[key] = task
        if speculative:
            self._speculative_flights.add(key)
        else:
            self._speculative_flights.discard(key)
        task.add_done_callback(lambda done_task: self._clear_task(key, done_task))
        return await asyncio.shield(task)

    def _drop_expired_deltas(self) -> None:
        cutoff = self.clock() - self.delta_retention_seconds
        for key, deltas in list(self._deltas.items()):
            if self._has_active_task(key):
                # Retention assumes a later scan already observes the event, which only holds for a
                # scan that starts after it. A running scan may have started earlier and paginate past
                # the window, so its deltas stay until that flight ends and a fresh scan can see them.
                continue
            for event_id, delta in list(deltas.items()):
                if delta.retained_at <= cutoff:
                    del deltas[event_id]
            if not deltas:
                self._deltas.pop(key, None)

    def retain_delta(self, key: _ThreadRepairDeltaKey, event_source: dict[str, Any]) -> None:
        """Retain one certified thread event until append or repair durably includes it."""
        event_id = event_source.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            return
        self._drop_expired_deltas()
        self._deltas.setdefault(key, {})[event_id] = _RetainedDelta(
            event_source=dict(event_source),
            retained_at=self.clock(),
        )

    def pending_deltas(self, key: _ThreadRepairDeltaKey) -> tuple[dict[str, Any], ...]:
        """Return retained deltas in deterministic retention order."""
        self._drop_expired_deltas()
        deltas = self._deltas.get(key, {})
        return tuple(dict(delta.event_source) for delta in deltas.values())

    def acknowledge_deltas(self, key: _ThreadRepairDeltaKey, event_ids: Collection[str]) -> None:
        """Forget retained deltas proven present in a usable snapshot."""
        deltas = self._deltas.get(key)
        if deltas is None:
            return
        for event_id in event_ids:
            deltas.pop(event_id, None)
        if not deltas:
            self._deltas.pop(key, None)

    def clear_room(self, coordination_scope: str, room_id: str) -> None:
        """Drop retained deltas and failure history at one membership boundary."""
        self._tasks = {key: task for key, task in self._tasks.items() if key[:2] != (coordination_scope, room_id)}
        self._speculative_flights = {
            key for key in self._speculative_flights if key[:2] != (coordination_scope, room_id)
        }
        self._deltas = {key: deltas for key, deltas in self._deltas.items() if key[:2] != (coordination_scope, room_id)}
        self._failure_backoffs = {
            key: backoff for key, backoff in self._failure_backoffs.items() if key[:2] != (coordination_scope, room_id)
        }
        self._speculative_cooldowns = {
            key: retry_after
            for key, retry_after in self._speculative_cooldowns.items()
            if key[:2] != (coordination_scope, room_id)
        }

    def clear(self) -> None:
        """Drop runtime-only ownership after all coordinator tasks drained."""
        self._tasks.clear()
        self._failure_backoffs.clear()
        self._deltas.clear()
        self._speculative_cooldowns.clear()
        self._interactive_joins.clear()
        self._speculative_flights.clear()
        self._slot_waiters.clear()
        self._running_repairs = 0
        self._running_speculative_repairs = 0
