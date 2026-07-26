"""Background worker that drains durable pending terminal Matrix deliveries.

The worker owns scheduling only. Deciding what one attempt means - inspecting
the visible target, sending or editing, and classifying the failure - belongs to
the delivery boundary that injects :attr:`TerminalDeliveryWorkerDeps.attempt`.

Wakeups are advisory. Every correctness guarantee comes from the durable store
plus the bounded periodic scan, so a missed notification only delays delivery.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mindroom.logging_config import get_logger
from mindroom.terminal_delivery import TerminalDeliveryAttempt

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import structlog

    from mindroom.terminal_delivery import PendingTerminalDelivery, TerminalDeliveryStore

logger = get_logger(__name__)

DEFAULT_POLL_INTERVAL_SECONDS = 15.0
DEFAULT_MAX_CONCURRENCY = 4
_DEFAULT_INITIAL_BACKOFF_SECONDS = 1.0
_DEFAULT_MAX_BACKOFF_SECONDS = 300.0
# Claim in bounded rounds so a large backlog cannot pin one lease batch open.
_CLAIM_BATCH_MULTIPLIER = 4
# One drain never loops forever: anything still due afterwards waits for the
# next wakeup or poll instead of spinning.
_MAX_DRAIN_ROUNDS = 64


@dataclass
class _RoomOrderingLock:
    """One room's serialization lock plus its live holder count."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    holders: int = 0


@dataclass(frozen=True)
class TerminalDeliveryWorkerDeps:
    """Collaborators the durable terminal delivery worker needs."""

    store: TerminalDeliveryStore
    attempt: Callable[[PendingTerminalDelivery], Awaitable[TerminalDeliveryAttempt]]
    complete_lifecycle: Callable[[PendingTerminalDelivery], Awaitable[None]]
    is_ready: Callable[[], bool]
    logger: structlog.stdlib.BoundLogger = field(default_factory=lambda: logger)
    wall_clock: Callable[[], float] = time.time
    jitter: Callable[[], float] = random.random
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY
    initial_backoff_seconds: float = _DEFAULT_INITIAL_BACKOFF_SECONDS
    max_backoff_seconds: float = _DEFAULT_MAX_BACKOFF_SECONDS


@dataclass
class TerminalDeliveryWorker:
    """Drain durable terminal deliveries with bounded concurrency and per-room order."""

    deps: TerminalDeliveryWorkerDeps
    _task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _wake: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)
    _room_locks: dict[str, _RoomOrderingLock] = field(default_factory=dict, init=False, repr=False)
    _leased_revisions: dict[str, int] = field(default_factory=dict, init=False, repr=False)

    @property
    def running(self) -> bool:
        """Return whether the worker loop is currently scheduled."""
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Start the worker loop; safe to call when it is already running."""
        if self.running:
            return
        self._wake.set()
        self._task = asyncio.create_task(self._run(), name="terminal_delivery_worker")

    async def stop(self) -> None:
        """Stop the worker and return every leased record to the retry queue."""
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await self._release_leases(reason="worker_shutdown")

    def wake(self, *, reason: str = "recovery_ready") -> None:
        """Signal that transport conditions may have changed for pending work."""
        if not self.running:
            return
        self.deps.logger.debug("terminal_delivery_worker_wake", wake_reason=reason)
        self._wake.set()

    async def _run(self) -> None:
        """Own the durable retry loop until cancelled.

        Cancellation deliberately leaves leased records in ``attempting``:
        :meth:`stop` releases them from an uncancelled context, and a hard crash
        is repaired by lease expiry during the next :meth:`TerminalDeliveryStore.warm`.
        """
        while True:
            await self._wait_for_work()
            if not self.deps.is_ready():
                continue
            try:
                await self.drain_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.deps.logger.exception("terminal_delivery_drain_failed")

    async def _wait_for_work(self) -> None:
        """Wait for a wakeup or the next scheduled attempt, whichever comes first.

        A due row only shortens the wait once delivery can actually be attempted.
        While the runtime is not ready - the whole of bot startup, where warmed
        rows are already due - the schedule is ignored and the loop parks on the
        wake event, so it cannot spin against work it is not allowed to run yet.
        """
        # The schedule read takes the store lock, which writer threads hold across
        # fsync, so it must not run on the event loop.
        timeout = (
            await asyncio.to_thread(self._seconds_until_next_attempt)
            if self.deps.is_ready()
            else self.deps.poll_interval_seconds
        )
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout)
        self._wake.clear()

    def _seconds_until_next_attempt(self) -> float:
        """Return the bounded sleep before the next scheduled attempt."""
        unsettled = self.deps.store.unsettled_items()
        if not unsettled:
            return self.deps.poll_interval_seconds
        now = self.deps.wall_clock()
        earliest = min(item.next_attempt_at for item in unsettled)
        return max(0.0, min(self.deps.poll_interval_seconds, earliest - now))

    async def drain_once(self) -> int:
        """Attempt every currently due record, in bounded concurrent rounds."""
        attempted = 0
        semaphore = asyncio.Semaphore(self.deps.max_concurrency)
        for _round in range(_MAX_DRAIN_ROUNDS):
            batch = await asyncio.to_thread(
                self.deps.store.claim_due,
                limit=self.deps.max_concurrency * _CLAIM_BATCH_MULTIPLIER,
            )
            if not batch:
                break
            self._leased_revisions.update((item.delivery_id, item.revision) for item in batch)
            await asyncio.gather(
                *(self._attempt_with_limits(item, semaphore) for item in batch),
                return_exceptions=False,
            )
            attempted += len(batch)
        if attempted:
            unsettled = await asyncio.to_thread(self.deps.store.unsettled_items)
            self.deps.logger.info(
                "terminal_delivery_drain_completed",
                attempted_count=attempted,
                unsettled_count=len(unsettled),
                unsettled_room_count=len({item.target.room_id for item in unsettled}),
                oldest_unsettled_age_seconds=round(
                    max((self.deps.wall_clock() - item.created_at for item in unsettled), default=0.0),
                    3,
                ),
                max_attempts=max((item.attempts for item in unsettled), default=0),
            )
        return attempted

    async def _attempt_with_limits(self, item: PendingTerminalDelivery, semaphore: asyncio.Semaphore) -> None:
        """Run one attempt under the global concurrency cap and its room's ordering lock."""
        room_id = item.target.room_id
        room_lock = self._room_locks.get(room_id)
        if room_lock is None:
            room_lock = _RoomOrderingLock()
            self._room_locks[room_id] = room_lock
        room_lock.holders += 1
        try:
            async with semaphore, room_lock.lock:
                await self._attempt_once(item)
        finally:
            room_lock.holders -= 1
            if room_lock.holders <= 0 and self._room_locks.get(room_id) is room_lock:
                del self._room_locks[room_id]

    async def _attempt_once(self, item: PendingTerminalDelivery) -> None:
        """Run and settle exactly one durable delivery attempt.

        The lease is only dropped once this attempt reached a durable decision;
        a cancelled attempt keeps it so :meth:`stop` can release it explicitly.
        """
        try:
            attempt = await self.deps.attempt(item)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.deps.logger.exception("terminal_delivery_attempt_raised", **item.log_context)
            attempt = TerminalDeliveryAttempt.transient("attempt_exception")
        # Shutdown must not cancel the durable write of an outcome that already
        # happened; a settled row would otherwise be released back to retry_wait.
        await asyncio.shield(self._settle_attempt(item, attempt))
        self._leased_revisions.pop(item.delivery_id, None)

    async def _settle_attempt(self, item: PendingTerminalDelivery, attempt: TerminalDeliveryAttempt) -> None:
        """Apply one attempt outcome to durable state."""
        store = self.deps.store
        if attempt.result == "delivered":
            await self.deps.complete_lifecycle(item)
            await asyncio.to_thread(
                store.mark_delivered,
                item.delivery_id,
                revision=item.revision,
                reason=attempt.reason,
            )
            self.deps.logger.info(
                "terminal_delivery_recovered",
                delivery_reason=attempt.reason,
                **item.log_context,
            )
            return
        if attempt.result == "superseded":
            await asyncio.to_thread(
                store.mark_superseded,
                item.delivery_id,
                revision=item.revision,
                reason=attempt.reason,
            )
            self.deps.logger.info(
                "terminal_delivery_superseded",
                delivery_reason=attempt.reason,
                **item.log_context,
            )
            return
        # A committed answer is never abandoned for taking too long: the visible
        # placeholder already carries a delivery-failure note, so retrying at the
        # capped backoff forever is strictly better than dropping the outcome.
        delay = self._backoff_seconds(item.attempts + 1)
        await asyncio.to_thread(
            store.defer,
            item.delivery_id,
            revision=item.revision,
            reason=attempt.reason,
            next_attempt_at=self.deps.wall_clock() + delay,
        )
        self.deps.logger.info(
            "terminal_delivery_deferred",
            delivery_reason=attempt.reason,
            retry_in_seconds=round(delay, 3),
            **item.log_context,
        )

    def _backoff_seconds(self, attempts: int) -> float:
        """Return exponential backoff with jitter, bounded above."""
        exponential = self.deps.initial_backoff_seconds * (2 ** max(0, attempts - 1))
        bounded = min(exponential, self.deps.max_backoff_seconds)
        return bounded * (0.5 + 0.5 * self.deps.jitter())

    async def _release_leases(self, *, reason: str) -> None:
        """Return every record this worker still holds to the retry queue."""
        leased = tuple(self._leased_revisions.items())
        self._leased_revisions.clear()
        for delivery_id, revision in leased:
            await asyncio.to_thread(self.deps.store.release, delivery_id, revision=revision, reason=reason)


__all__ = [
    "DEFAULT_MAX_CONCURRENCY",
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "TerminalDeliveryWorker",
    "TerminalDeliveryWorkerDeps",
]
