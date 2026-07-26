"""Single-flight ownership and retained live deltas for thread-cache repair."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Collection

type _ThreadRepairFlightKey = tuple[str, str, str, bool, bool]
type _ThreadRepairDeltaKey = tuple[str, str, str]

# Retained deltas only cover the window where a homeserver scan can miss a just-certified event.
# Once a delta is older than this, any new scan already observes it, so keeping it only wastes memory.
_DELTA_RETENTION_SECONDS = 60.0


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


@dataclass
class ThreadRepairRegistry:
    """Own principal-scoped repair flights, failure backoff, and certified deltas."""

    failure_backoff_seconds: float = 1.0
    max_failure_backoff_seconds: float = 30.0
    delta_retention_seconds: float = _DELTA_RETENTION_SECONDS
    clock: Callable[[], float] = time.monotonic
    _tasks: dict[_ThreadRepairFlightKey, asyncio.Task[object]] = field(default_factory=dict, init=False)
    _failure_backoffs: dict[_ThreadRepairFlightKey, _RepairFailureBackoff] = field(default_factory=dict, init=False)
    _deltas: dict[_ThreadRepairDeltaKey, dict[str, _RetainedDelta]] = field(default_factory=dict, init=False)

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

    async def run[T](
        self,
        key: _ThreadRepairFlightKey,
        *,
        schedule: Callable[[Callable[[], Awaitable[T]]], asyncio.Task[T]],
        repair: Callable[[], Awaitable[T]],
        result_arms_backoff: Callable[[T], bool],
        bypass_failure_backoff: bool = False,
    ) -> T:
        """Join or start one shielded repair and update backoff from its outcome.

        Authoritative untimed reads may bypass an existing delay while preserving its failure count.
        """
        active = self._active_task(key)
        if active is not None:
            return cast("T", await asyncio.shield(active))

        retry_after_seconds = self.retry_after_seconds(key)
        if retry_after_seconds > 0 and not bypass_failure_backoff:
            raise ThreadRepairBackoffError(retry_after_seconds)

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

        task = schedule(run_repair)
        self._tasks[key] = task
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
        self._deltas = {key: deltas for key, deltas in self._deltas.items() if key[:2] != (coordination_scope, room_id)}
        self._failure_backoffs = {
            key: backoff for key, backoff in self._failure_backoffs.items() if key[:2] != (coordination_scope, room_id)
        }

    def clear(self) -> None:
        """Drop runtime-only ownership after all coordinator tasks drained."""
        self._tasks.clear()
        self._failure_backoffs.clear()
        self._deltas.clear()
