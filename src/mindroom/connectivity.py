"""Matrix host/network connectivity state, coalesced homeserver probes, and transient stream activity hooks."""

from __future__ import annotations

import asyncio
import random
import ssl
import threading
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from typing import TYPE_CHECKING
from weakref import WeakKeyDictionary

import httpx

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

__all__ = [
    "corroborate_offline",
    "matrix_offline",
    "note_matrix_sync_cleared",
    "note_matrix_sync_failed",
    "note_matrix_sync_succeeded",
    "register_matrix_homeserver",
    "reset_connectivity_state",
    "set_stream_activity",
    "stream_activity_scope",
    "wait_for_connectivity",
]

type StreamActivityCallback = Callable[[str, str | None], None]
type _HomeserverConfig = tuple[str, bool | ssl.SSLContext]

_PROBE_TIMEOUT_SECONDS = 3.0

_lock = threading.Lock()
_failed_entities: set[str] = set()
_recovery_generation = 0
_waiters: set[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = set()
_homeserver: _HomeserverConfig | None = None
_homeserver_probes: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Task[bool]] = WeakKeyDictionary()
_STREAM_ACTIVITY_CALLBACK: ContextVar[StreamActivityCallback | None] = ContextVar(
    "mindroom_stream_activity_callback",
    default=None,
)


def register_matrix_homeserver(url: str, *, ssl_verify: bool | ssl.SSLContext = True) -> None:
    """Record the active Matrix homeserver URL and TLS verification setting for connectivity probes."""
    global _homeserver
    cleaned = url.strip().rstrip("/")
    with _lock:
        _homeserver = (cleaned, ssl_verify) if cleaned else None


def note_matrix_sync_failed(entity_name: str) -> None:
    """Mark ``entity_name`` offline immediately after a Matrix sync transport failure or stall."""
    with _lock:
        _failed_entities.add(entity_name)


def note_matrix_sync_succeeded(entity_name: str) -> None:
    """Clear offline state, advance the recovery generation, and wake all waiting coroutines."""
    global _recovery_generation
    del entity_name
    with _lock:
        _failed_entities.clear()
        _recovery_generation += 1
        waiters = tuple(_waiters)
    for loop, event in waiters:
        with suppress(RuntimeError):
            loop.call_soon_threadsafe(event.set)


def note_matrix_sync_cleared(entity_name: str) -> None:
    """Remove ``entity_name`` from failed Matrix sync entities."""
    with _lock:
        _failed_entities.discard(entity_name)


def matrix_offline() -> bool:
    """Return True iff at least one active entity failed and no entity succeeded since."""
    with _lock:
        return bool(_failed_entities)


def reset_connectivity_state() -> None:
    """Clear all process-global connectivity state."""
    global _recovery_generation, _homeserver
    with _lock:
        _failed_entities.clear()
        _recovery_generation = 0
        _waiters.clear()
        _homeserver = None
    _homeserver_probes.clear()


def _has_ssl_cert_verification_error(error: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        if isinstance(current, ssl.SSLCertVerificationError):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


async def _http_probe_homeserver(config: _HomeserverConfig, timeout_seconds: float) -> bool:
    url, ssl_verify = config
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds, verify=ssl_verify, follow_redirects=False) as client:
            await client.get(f"{url}/_matrix/client/versions")
    except (httpx.TransportError, OSError, TimeoutError) as exc:
        return _has_ssl_cert_verification_error(exc)
    return True


async def _probe_homeserver_reachable(
    timeout: float = _PROBE_TIMEOUT_SECONDS,  # noqa: ASYNC109
) -> bool | None:
    with _lock:
        config = _homeserver
    if config is None:
        return None
    loop = asyncio.get_running_loop()
    task = _homeserver_probes.get(loop)
    if task is None or task.done():
        task = loop.create_task(_http_probe_homeserver(config, _PROBE_TIMEOUT_SECONDS))
        _homeserver_probes[loop] = task

        def _cleanup(done: asyncio.Task[bool]) -> None:
            if _homeserver_probes.get(loop) is done:
                _homeserver_probes.pop(loop, None)
            if not done.cancelled():
                with suppress(Exception):
                    done.exception()

        task.add_done_callback(_cleanup)
    try:
        async with asyncio.timeout(timeout):
            return await asyncio.shield(task)
    except TimeoutError:
        return False


async def corroborate_offline() -> bool:
    """Return whether a provider transport/stall failure is corroborated as a host/network outage."""
    if matrix_offline():
        return True
    return await _probe_homeserver_reachable() is False


async def _wait_for_sync_or_sleep(
    loop: asyncio.AbstractEventLoop,
    sleep_for: float,
    seen_generation: int,
) -> tuple[bool, int]:
    wake_event = asyncio.Event()
    waiter = (loop, wake_event)
    with _lock:
        if _recovery_generation != seen_generation:
            return True, _recovery_generation
        _waiters.add(waiter)
    try:
        async with asyncio.timeout(sleep_for):
            await wake_event.wait()
        woken = True
    except TimeoutError:
        woken = False
    finally:
        with _lock:
            _waiters.discard(waiter)
            current_generation = _recovery_generation
    return woken, current_generation


async def _connectivity_restored(*, deadline: float, loop: asyncio.AbstractEventLoop) -> bool:
    if matrix_offline() or (remaining := deadline - loop.time()) <= 0:
        return False
    return await _probe_homeserver_reachable(timeout=min(_PROBE_TIMEOUT_SECONDS, remaining)) is not False


async def _sleep_jitter(jitter_range: tuple[float, float], deadline: float, loop: asyncio.AbstractEventLoop) -> None:
    if (jitter := min(random.uniform(*jitter_range), max(0.0, deadline - loop.time()))) > 0:  # noqa: S311
        await asyncio.sleep(jitter)


async def wait_for_connectivity(
    *,
    deadline: float,
    probe_backoff: tuple[float, ...] = (5.0, 10.0, 20.0, 40.0, 60.0),
    jitter_range: tuple[float, float] = (0.5, 3.0),
) -> bool:
    """Wait until Matrix connectivity recovers (sync wakeup or coalesced probe) or ``deadline`` expires."""
    loop = asyncio.get_running_loop()
    with _lock:
        seen_generation = _recovery_generation
    attempt = 0
    while (remaining := deadline - loop.time()) > 0:
        sleep_for = min(probe_backoff[min(attempt, len(probe_backoff) - 1)], remaining)
        woken, seen_generation = await _wait_for_sync_or_sleep(loop, sleep_for, seen_generation)
        if not woken:
            attempt += 1
        if await _connectivity_restored(deadline=deadline, loop=loop):
            await _sleep_jitter(jitter_range, deadline, loop)
            return True
    return False


@contextmanager
def stream_activity_scope(on_change: StreamActivityCallback) -> Iterator[None]:
    """Bind a task-scoped callback for ephemeral stream activity updates."""
    token = _STREAM_ACTIVITY_CALLBACK.set(on_change)
    try:
        yield
    finally:
        _STREAM_ACTIVITY_CALLBACK.reset(token)


def set_stream_activity(wait_id: str, text: str | None) -> None:
    """Set or clear an ephemeral stream activity line if a stream scope is active."""
    if (callback := _STREAM_ACTIVITY_CALLBACK.get()) is not None:
        callback(wait_id, text)
