"""Single-flight ownership and retained live deltas for thread-cache repair."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Collection

type _ThreadRepairKey = tuple[str, str, str]


def _delta_order(item: tuple[str, dict[str, Any]]) -> tuple[int, str]:
    """Return stable Matrix ordering fields for one retained delta."""
    event_id, event_source = item
    timestamp = event_source.get("origin_server_ts")
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
    clock: Callable[[], float] = time.monotonic
    _tasks: dict[_ThreadRepairKey, asyncio.Task[object]] = field(default_factory=dict, init=False)
    _retry_after: dict[_ThreadRepairKey, float] = field(default_factory=dict, init=False)
    _deltas: dict[_ThreadRepairKey, dict[str, dict[str, Any]]] = field(default_factory=dict, init=False)

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
        self._retry_after[key] = self.clock() + self.failure_backoff_seconds

    def _clear_failure(self, key: _ThreadRepairKey) -> None:
        self._retry_after.pop(key, None)

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
        result_is_usable: Callable[[T], bool],
        acknowledged_event_ids: Callable[[T], Collection[str]],
    ) -> ThreadRepairRunResult[T]:
        """Join or start one shielded repair and update backoff from its exact outcome."""
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
                self._record_failure(key)
                raise
            if result_is_usable(value):
                self._clear_failure(key)
                self.acknowledge_deltas(key, acknowledged_event_ids(value))
            else:
                self._record_failure(key)
            return value

        task = schedule(run_repair)
        self._tasks[key] = task
        task.add_done_callback(lambda done_task: self._clear_task(key, done_task))
        value = await asyncio.shield(task)
        return ThreadRepairRunResult(value=value, joined=False)

    def retain_delta(self, key: _ThreadRepairKey, event_source: dict[str, Any]) -> None:
        """Retain one certified thread event until append or repair durably includes it."""
        event_id = event_source.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            return
        self._deltas.setdefault(key, {})[event_id] = dict(event_source)

    def pending_deltas(self, key: _ThreadRepairKey) -> tuple[dict[str, Any], ...]:
        """Return retained deltas in deterministic event order."""
        deltas = self._deltas.get(key, {})
        return tuple(
            dict(event_source)
            for event_id, event_source in sorted(
                deltas.items(),
                key=_delta_order,
            )
        )

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
