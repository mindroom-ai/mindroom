"""Workers with a tool call in flight, which idle cleanup refreshes so it never reaps one mid-call."""

from __future__ import annotations

import threading
from collections import Counter
from contextlib import contextmanager
from typing import TYPE_CHECKING

from mindroom.logging_config import get_logger
from mindroom.tool_system.worker_routing import worker_log_id
from mindroom.workers.backend import WorkerBackendError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from mindroom.workers.backend import WorkerBackend

logger = get_logger(__name__)

_lock = threading.Lock()
# Keyed by the backend's cleanup locator, so a backend touches only its own workers.
_in_flight: Counter[tuple[str | None, str]] = Counter()


@contextmanager
def worker_call_in_flight(backend: WorkerBackend, worker_key: str) -> Iterator[None]:
    """Count one call on a worker while it runs; a long call outlasts any idle timeout."""
    key = (backend.cleanup_locator, worker_key)
    with _lock:
        _in_flight[key] += 1
    try:
        yield
    finally:
        with _lock:
            _in_flight[key] -= 1
            if not _in_flight[key]:
                del _in_flight[key]


def touch_workers_in_flight(backend: WorkerBackend) -> None:
    """Refresh every worker of this backend with a call in flight, immediately before idle cleanup."""
    with _lock:
        worker_keys = sorted(key for locator, key in _in_flight if locator == backend.cleanup_locator)
    for worker_key in worker_keys:
        try:
            backend.touch_worker(worker_key)
        except WorkerBackendError:
            logger.warning("worker_call_touch_pending", worker=worker_log_id(worker_key), exc_info=True)
