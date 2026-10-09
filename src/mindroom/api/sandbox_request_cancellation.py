"""Stop one in-flight sandbox runner request when the primary stops waiting for it."""

from __future__ import annotations

import threading
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

__all__ = ["bind_request_stop", "cancel_request", "request_cancelled", "track_request"]

# A cancel can overtake its own request; remember a bounded number of those.
_MAX_EARLY_CANCELS = 1024

_lock = threading.Lock()
_running: dict[str, _RunningRequest] = {}
_early_cancels: OrderedDict[str, None] = OrderedDict()
_current: ContextVar[_RunningRequest | None] = ContextVar("sandbox_running_request", default=None)


@dataclass
class _RunningRequest:
    cancelled: bool = False
    stop: Callable[[], object] | None = None


@contextmanager
def track_request(request_id: str | None) -> Iterator[None]:
    """Make the request running in this context cancellable by its ID."""
    if request_id is None:
        yield
        return
    running = _RunningRequest()
    with _lock:
        if request_id in _early_cancels:
            del _early_cancels[request_id]
            running.cancelled = True
        _running[request_id] = running
    token = _current.set(running)
    try:
        yield
    finally:
        _current.reset(token)
        with _lock:
            _running.pop(request_id, None)


def bind_request_stop(stop: Callable[[], object]) -> None:
    """Arm the current request's stop hook once its process or task exists; stop at once if already cancelled."""
    running = _current.get()
    if running is None:
        return
    with _lock:
        running.stop = stop
        cancelled = running.cancelled
    if cancelled:
        stop()


def request_cancelled() -> bool:
    """Return whether the current request was cancelled, so its failure is not blamed on the worker."""
    running = _current.get()
    return running is not None and running.cancelled


def cancel_request(request_id: str) -> bool:
    """Stop the running request with this ID, or stop it on arrival; return whether it was running."""
    with _lock:
        running = _running.get(request_id)
        if running is None:
            _early_cancels[request_id] = None
            while len(_early_cancels) > _MAX_EARLY_CANCELS:
                _early_cancels.popitem(last=False)
            return False
        running.cancelled = True
        stop = running.stop
    if stop is not None:
        stop()
    return True
