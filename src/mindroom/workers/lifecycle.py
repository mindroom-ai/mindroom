"""Backend-neutral worker lifecycle helpers."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from mindroom.workers.models import WorkerHandle, WorkerStatus


def effective_idle_status(
    status: WorkerStatus,
    *,
    last_used_at: float,
    idle_timeout_seconds: float,
    now: float,
) -> WorkerStatus:
    """Return the externally visible worker status after applying idle timeout policy."""
    if status == "ready" and now - last_used_at >= idle_timeout_seconds:
        return "idle"
    return status


def filter_and_sort_worker_handles(
    handles: Iterable[WorkerHandle],
    *,
    include_idle: bool,
) -> list[WorkerHandle]:
    """Apply worker-list idle filtering and newest-first ordering."""
    filtered_handles = list(handles)
    if not include_idle:
        filtered_handles = [handle for handle in filtered_handles if handle.status != "idle"]
    return sorted(filtered_handles, key=lambda handle: handle.last_used_at, reverse=True)


@dataclass(slots=True)
class WorkerLockRegistry:
    """Thread-safe registry for stable per-worker locks."""

    _locks: dict[str, threading.Lock] = field(default_factory=dict)
    _guard: threading.Lock = field(default_factory=threading.Lock)

    def lock_for(self, worker_key: str) -> threading.Lock:
        """Return the stable lock for one worker key."""
        with self._guard:
            worker_lock = self._locks.get(worker_key)
            if worker_lock is None:
                worker_lock = threading.Lock()
                self._locks[worker_key] = worker_lock
            return worker_lock
