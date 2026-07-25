"""Single-flight ownership and retained live deltas for thread-cache repair."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Collection

type _ThreadRepairKey = tuple[str, str, str]

# Retained deltas only cover the window where a homeserver scan can miss a just-certified event.
# Once a delta is older than this, any new scan already observes it, so keeping it only wastes memory.
_DELTA_RETENTION_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class _RetainedDelta:
    """One certified event source held until a scan or append is proven to include it."""

    event_source: dict[str, Any]
    retained_at: float

    @property
    def order(self) -> tuple[int, str]:
        """Return stable Matrix ordering fields for this delta."""
        timestamp = self.event_source.get("origin_server_ts")
        event_id = str(self.event_source["event_id"])
        return (timestamp if isinstance(timestamp, int) and not isinstance(timestamp, bool) else 0), event_id


class ThreadRepairBackoffError(RuntimeError):
    """Raised when a failed repair is still inside its bounded retry delay."""

    def __init__(self, retry_after_seconds: float) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"thread cache repair backoff active for {retry_after_seconds:.3f}s")


@dataclass(frozen=True, slots=True)
class ThreadRepairRunResult[T]:
    """One repair result plus whether this caller joined existing ownership."""

    value: T
    joined: bool


@dataclass
class ThreadRepairRegistry:
    """Own principal-scoped repair flights, failure backoff, and certified deltas."""

    failure_backoff_seconds: float = 1.0
    delta_retention_seconds: float = _DELTA_RETENTION_SECONDS
    clock: Callable[[], float] = time.monotonic
    _tasks: dict[_ThreadRepairKey, asyncio.Task[object]] = field(default_factory=dict, init=False)
    _retry_after: dict[_ThreadRepairKey, float] = field(default_factory=dict, init=False)
    _deltas: dict[_ThreadRepairKey, dict[str, _RetainedDelta]] = field(default_factory=dict, init=False)

    def _active_task(self, key: _ThreadRepairKey) -> asyncio.Task[object] | None:
        task = self._tasks.get(key)
        if task is None:
            return None
        if task.done():
            self._tasks.pop(key, None)
            return None
        return task

    def _clear_task(self, key: _ThreadRepairKey, task: asyncio.Task[object]) -> None:
        if self._tasks.get(key) is task:
            self._tasks.pop(key, None)

    def _record_failure(self, key: _ThreadRepairKey) -> None:
        now = self.clock()
        self._retry_after = {expired: until for expired, until in self._retry_after.items() if until > now}
        self._retry_after[key] = now + self.failure_backoff_seconds

    def retry_after_seconds(self, key: _ThreadRepairKey) -> float:
        """Return remaining repair backoff for one key."""
        retry_after = self._retry_after.get(key)
        if retry_after is None:
            return 0.0
        remaining = retry_after - self.clock()
        if remaining <= 0:
            self._retry_after.pop(key, None)
            return 0.0
        return remaining

    async def run[T](
        self,
        key: _ThreadRepairKey,
        *,
        schedule: Callable[[Callable[[], Awaitable[T]]], asyncio.Task[T]],
        repair: Callable[[], Awaitable[T]],
    ) -> ThreadRepairRunResult[T]:
        """Join or start one shielded repair, throttling only a repair that raised."""
        active = self._active_task(key)
        if active is not None:
            value = cast("T", await asyncio.shield(active))
            return ThreadRepairRunResult(value=value, joined=True)

        retry_after_seconds = self.retry_after_seconds(key)
        if retry_after_seconds > 0:
            raise ThreadRepairBackoffError(retry_after_seconds)

        async def run_repair() -> T:
            try:
                value = await repair()
            except asyncio.CancelledError:
                raise
            except BaseException:
                # A repair that completes without installing a snapshot still returns usable
                # history to its caller, so only a raising repair is worth throttling.
                self._record_failure(key)
                raise
            self._retry_after.pop(key, None)
            return value

        task = schedule(run_repair)
        self._tasks[key] = task
        task.add_done_callback(lambda done_task: self._clear_task(key, done_task))
        value = await asyncio.shield(task)
        return ThreadRepairRunResult(value=value, joined=False)

    def _drop_expired_deltas(self) -> None:
        cutoff = self.clock() - self.delta_retention_seconds
        for key, deltas in list(self._deltas.items()):
            if self._active_task(key) is not None:
                # Retention assumes a later scan already observes the event, which only holds for a
                # scan that starts after it. A running scan may have started earlier and paginate past
                # the window, so its deltas stay until that flight ends and a fresh scan can see them.
                continue
            for event_id, delta in list(deltas.items()):
                if delta.retained_at <= cutoff:
                    del deltas[event_id]
            if not deltas:
                self._deltas.pop(key, None)

    def retain_delta(self, key: _ThreadRepairKey, event_source: dict[str, Any]) -> None:
        """Retain one certified thread event until append or repair durably includes it."""
        event_id = event_source.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            return
        self._drop_expired_deltas()
        self._deltas.setdefault(key, {})[event_id] = _RetainedDelta(
            event_source=dict(event_source),
            retained_at=self.clock(),
        )

    def pending_deltas(self, key: _ThreadRepairKey) -> tuple[dict[str, Any], ...]:
        """Return retained deltas in deterministic event order."""
        self._drop_expired_deltas()
        deltas = self._deltas.get(key, {})
        return tuple(dict(delta.event_source) for delta in sorted(deltas.values(), key=lambda delta: delta.order))

    def acknowledge_deltas(self, key: _ThreadRepairKey, event_ids: Collection[str]) -> None:
        """Forget retained deltas proven present in a usable snapshot."""
        deltas = self._deltas.get(key)
        if deltas is None:
            return
        for event_id in event_ids:
            deltas.pop(event_id, None)
        if not deltas:
            self._deltas.pop(key, None)

    def clear(self) -> None:
        """Drop runtime-only ownership after all coordinator tasks drained."""
        self._tasks.clear()
        self._retry_after.clear()
        self._deltas.clear()
