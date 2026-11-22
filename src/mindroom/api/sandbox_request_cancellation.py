"""Stop one in-flight sandbox runner request when the primary stops waiting for it."""

from __future__ import annotations

import asyncio
import inspect
import threading
from collections import OrderedDict
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator

__all__ = [
    "bind_request_stop",
    "bind_request_stop_confirm",
    "cancel_request",
    "cancel_request_and_wait",
    "current_request_id",
    "request_cancelled",
    "track_request",
]

# A cancel can overtake its own request; remember a bounded number of those.
_MAX_EARLY_CANCELS = 1024
_CANCEL_CONFIRM_WAIT_SECONDS = 3.0

_lock = threading.Lock()
_running: dict[str, _RunningRequest] = {}
_early_cancels: OrderedDict[str, None] = OrderedDict()
_current: ContextVar[_RunningRequest | None] = ContextVar("sandbox_running_request", default=None)


@dataclass
class _RunningRequest:
    request_id: str
    cancelled: bool = False
    stop: Callable[[], object] | None = None
    confirm: Callable[[], bool | Awaitable[bool]] | None = None


@contextmanager
def track_request(request_id: str | None) -> Iterator[None]:
    """Make the request running in this context cancellable by its ID."""
    if request_id is None:
        yield
        return
    running = _RunningRequest(request_id=request_id)
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


def current_request_id() -> str | None:
    """Return the active tracked request ID, if any."""
    running = _current.get()
    return running.request_id if running is not None else None


def bind_request_stop(
    stop: Callable[[], object],
    *,
    confirm: Callable[[], bool | Awaitable[bool]] | None = None,
) -> None:
    """Arm the current request's stop hook once its process or task exists; stop at once if already cancelled."""
    running = _current.get()
    if running is None:
        return
    with _lock:
        running.stop = stop
        if confirm is not None and running.confirm is None:
            running.confirm = confirm
        cancelled = running.cancelled
    if cancelled:
        stop()


def bind_request_stop_confirm(confirm: Callable[[], bool | Awaitable[bool]]) -> None:
    """Bind a confirmation hook that verifies the request's command process group has actually stopped."""
    running = _current.get()
    if running is None:
        return
    with _lock:
        running.confirm = confirm


def request_cancelled() -> bool:
    """Return whether the current request was cancelled, so its failure is not blamed on the worker."""
    running = _current.get()
    return running is not None and running.cancelled


def _begin_cancel(request_id: str) -> tuple[bool, Callable[[], object] | None, Callable[[], bool | Awaitable[bool]] | None]:
    with _lock:
        running = _running.get(request_id)
        if running is None:
            _early_cancels[request_id] = None
            while len(_early_cancels) > _MAX_EARLY_CANCELS:
                _early_cancels.popitem(last=False)
            return False, None, None
        running.cancelled = True
        return True, running.stop, running.confirm


def cancel_request(request_id: str) -> bool:
    """Stop the running request with this ID, or stop it on arrival; return whether it was running."""
    found, stop, _confirm = _begin_cancel(request_id)
    if stop is not None:
        stop()
    return found


async def cancel_request_and_wait(
    request_id: str,
    *,
    wait_seconds: float = _CANCEL_CONFIRM_WAIT_SECONDS,
) -> tuple[bool, bool]:
    """Stop one request and wait up to *wait_seconds* on the event loop to confirm its process group stopped."""
    found, stop, confirm = _begin_cancel(request_id)
    if not found or stop is None:
        return found, False
    if confirm is not None and inspect.iscoroutinefunction(confirm):
        stop()
        try:
            return True, bool(await asyncio.wait_for(confirm(), timeout=wait_seconds))
        except (TimeoutError, Exception):
            return True, False
    loop = asyncio.get_running_loop()
    done: asyncio.Future[bool] = loop.create_future()

    def _resolve(value: bool) -> None:
        if not done.done():
            done.set_result(value)

    def _run() -> None:
        ok = False
        try:
            res = stop()
            ok = bool(confirm()) if confirm is not None else res is not False
        except Exception:
            ok = False
        finally:
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(_resolve, ok)

    threading.Thread(target=_run, name="mindroom-runner-cancel", daemon=True).start()
    try:
        return True, await asyncio.wait_for(done, timeout=wait_seconds)
    except TimeoutError:
        return True, False
