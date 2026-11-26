"""Host/network connectivity state, coalesced reachability probes, and transient stream activity hooks."""

from __future__ import annotations

import asyncio
import ipaddress
import random
import ssl
import threading
import urllib.request
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from typing import TYPE_CHECKING
from urllib.parse import urlparse
from weakref import WeakKeyDictionary

import httpx

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Iterator

__all__ = [
    "corroborate_offline",
    "endpoint_host_port",
    "is_endpoint_known_reachable",
    "is_local_endpoint",
    "is_local_host",
    "matrix_offline",
    "note_endpoint_reachable",
    "note_matrix_sync_cleared",
    "note_matrix_sync_failed",
    "note_matrix_sync_started",
    "note_matrix_sync_succeeded",
    "register_matrix_homeserver",
    "reset_connectivity_state",
    "set_stream_activity",
    "stream_activity_scope",
    "wait_for_connectivity",
]

type StreamActivityCallback = Callable[[str, str | None], None]
type _HomeserverConfig = tuple[str, bool | ssl.SSLContext]

_MAX_KNOWN_ENDPOINTS = 256
_PROBE_TIMEOUT_SECONDS = 3.0

_lock = threading.Lock()
_failed_entities: set[str] = set()
_recovery_generation = 0
_waiters: set[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = set()
_homeserver: _HomeserverConfig | None = None
_known_endpoints: dict[tuple[str, int], None] = {}
_loop_probes: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[object, asyncio.Task[bool]]] = WeakKeyDictionary()
_STREAM_ACTIVITY_CALLBACK: ContextVar[StreamActivityCallback | None] = ContextVar(
    "mindroom_stream_activity_callback",
    default=None,
)


def is_local_host(host: str) -> bool:
    """Return whether ``host`` is local/private/Docker (single-label, .local/.internal, loopback, private IP)."""
    normalized = host.strip().lower().rstrip(".").removeprefix("[").removesuffix("]")
    if not normalized:
        return False
    try:
        address = ipaddress.ip_address(normalized.split("%", 1)[0])
    except ValueError:
        return "." not in normalized or normalized.endswith((".localhost", ".local", ".internal"))
    return address.is_loopback or address.is_private or address.is_link_local or address.is_unspecified


def _parse_endpoint(endpoint: str | None) -> tuple[str, str, int] | None:
    if not endpoint or not (raw := endpoint.split("|", 1)[0].strip()):
        return None
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    if not parsed.hostname:
        return None
    scheme = (parsed.scheme or "https").lower()
    try:
        port = parsed.port
    except ValueError:
        return None
    return scheme, parsed.hostname.lower(), port if port is not None else (80 if scheme == "http" else 443)


def is_local_endpoint(endpoint: str | None) -> bool:
    """Return whether ``endpoint`` parses to a local host."""
    return (parsed := _parse_endpoint(endpoint)) is not None and is_local_host(parsed[1])


def endpoint_host_port(endpoint: str | None) -> str | None:
    """Return a log-safe ``host:port`` for ``endpoint`` without paths, query strings, or credentials."""
    if (parsed := _parse_endpoint(endpoint)) is None:
        return None
    _, host, port = parsed
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def register_matrix_homeserver(url: str, *, ssl_verify: bool | ssl.SSLContext = True) -> None:
    """Record the active Matrix homeserver URL and TLS verification setting for connectivity probes."""
    global _homeserver
    cleaned = url.strip().rstrip("/")
    with _lock:
        _homeserver = (cleaned, ssl_verify) if cleaned else None


def note_matrix_sync_started(entity_name: str) -> None:
    """Retain existing sync failure state when ``entity_name`` starts or restarts its sync loop."""
    del entity_name


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


def note_endpoint_reachable(endpoint: str | None) -> None:
    """Record ``endpoint``'s ``(host, port)`` as previously reachable."""
    if (parsed := _parse_endpoint(endpoint)) is None:
        return
    key = (parsed[1], parsed[2])
    with _lock:
        _known_endpoints.pop(key, None)
        _known_endpoints[key] = None
        while len(_known_endpoints) > _MAX_KNOWN_ENDPOINTS:
            _known_endpoints.pop(next(iter(_known_endpoints)))


def is_endpoint_known_reachable(endpoint: str | None) -> bool:
    """Return whether ``endpoint``'s ``(host, port)`` has completed a request previously."""
    if (parsed := _parse_endpoint(endpoint)) is None:
        return False
    with _lock:
        return (parsed[1], parsed[2]) in _known_endpoints


def reset_connectivity_state() -> None:
    """Clear all process-global connectivity state."""
    global _recovery_generation, _homeserver
    with _lock:
        _failed_entities.clear()
        _recovery_generation = 0
        _waiters.clear()
        _homeserver = None
        _known_endpoints.clear()
    _loop_probes.clear()


def _remote_homeserver_config() -> _HomeserverConfig | None:
    with _lock:
        config = _homeserver
    if config is None or (parsed := _parse_endpoint(config[0])) is None or is_local_host(parsed[1]):
        return None
    return config


def _has_ssl_cert_verification_error(error: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        if isinstance(current, ssl.SSLCertVerificationError):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


async def _coalesced_probe(key: object, factory: Callable[[], Coroutine[object, object, bool]]) -> bool:
    loop = asyncio.get_running_loop()
    probes = _loop_probes.setdefault(loop, {})
    task = probes.get(key)
    if task is None or task.done():
        task = loop.create_task(factory())
        probes[key] = task

        def _cleanup(done: asyncio.Task[bool]) -> None:
            if probes.get(key) is done:
                probes.pop(key, None)
            if not done.cancelled():
                with suppress(Exception):
                    done.exception()

        task.add_done_callback(_cleanup)
    return await asyncio.shield(task)


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
    if (config := _remote_homeserver_config()) is None:
        return None
    return await _coalesced_probe("homeserver", lambda: _http_probe_homeserver(config, timeout))


def _tcp_probe_target(endpoint: str) -> tuple[str, int] | None:
    if (parsed := _parse_endpoint(endpoint)) is None:
        return None
    scheme, host, port = parsed
    if not urllib.request.proxy_bypass(host):
        proxies = urllib.request.getproxies()
        if (proxy_url := proxies.get(scheme) or proxies.get("all")) and (
            proxy_parsed := _parse_endpoint(proxy_url)
        ) is not None:
            return proxy_parsed[1], proxy_parsed[2]
    return host, port


async def _tcp_connect(host: str, port: int, timeout_seconds: float) -> bool:
    try:
        async with asyncio.timeout(timeout_seconds):
            _reader, writer = await asyncio.open_connection(host, port)
    except (OSError, TimeoutError):
        return False
    writer.close()
    with suppress(OSError, TimeoutError):
        async with asyncio.timeout(0.5):
            await writer.wait_closed()
    return True


async def _probe_tcp_reachable(
    endpoint: str,
    timeout: float = _PROBE_TIMEOUT_SECONDS,  # noqa: ASYNC109
) -> bool:
    if (target := _tcp_probe_target(endpoint)) is None:
        return False
    return await _coalesced_probe(target, lambda: _tcp_connect(target[0], target[1], timeout))


async def corroborate_offline(endpoint: str | None) -> bool:
    """Return whether a provider transport/stall failure is corroborated as a host/network outage."""
    if is_local_endpoint(endpoint):
        return False
    if _remote_homeserver_config() is not None:
        return matrix_offline() or await _probe_homeserver_reachable(timeout=_PROBE_TIMEOUT_SECONDS) is False
    if not endpoint or not is_endpoint_known_reachable(endpoint):
        return False
    return not await _probe_tcp_reachable(endpoint, timeout=_PROBE_TIMEOUT_SECONDS)


async def _wait_for_sync_or_sleep(
    loop: asyncio.AbstractEventLoop,
    sleep_for: float,
    seen_generation: int,
    *,
    remote_hs: bool,
) -> tuple[bool, int]:
    if not remote_hs:
        await asyncio.sleep(sleep_for)
        return False, seen_generation
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


async def _connectivity_restored(
    endpoint: str | None,
    *,
    remote_hs: bool,
    woken_by_sync: bool,
    deadline: float,
    loop: asyncio.AbstractEventLoop,
) -> bool:
    if remote_hs:
        if woken_by_sync:
            if matrix_offline():
                return False
        elif (remaining := deadline - loop.time()) <= 0 or (
            await _probe_homeserver_reachable(timeout=min(_PROBE_TIMEOUT_SECONDS, remaining)) is not True
        ):
            return False
        if endpoint is None:
            return True
    elif endpoint is None:
        return False
    if (remaining := deadline - loop.time()) <= 0:
        return False
    return await _probe_tcp_reachable(endpoint, timeout=min(_PROBE_TIMEOUT_SECONDS, remaining))


async def wait_for_connectivity(
    endpoint: str | None,
    *,
    deadline: float,
    probe_backoff: tuple[float, ...] = (5.0, 10.0, 20.0, 40.0, 60.0),
    jitter_range: tuple[float, float] = (0.5, 3.0),
) -> bool:
    """Wait until connectivity recovers (via Matrix sync wakeup or coalesced probes) or ``deadline`` expires."""
    loop = asyncio.get_running_loop()
    with _lock:
        seen_generation = _recovery_generation
    attempt = 0
    while (remaining := deadline - loop.time()) > 0:
        sleep_for = min(probe_backoff[min(attempt, len(probe_backoff) - 1)], remaining)
        remote_hs = _remote_homeserver_config() is not None
        woken, seen_generation = await _wait_for_sync_or_sleep(loop, sleep_for, seen_generation, remote_hs=remote_hs)
        if woken and (jitter := min(random.uniform(*jitter_range), max(0.0, deadline - loop.time()))) > 0:  # noqa: S311
            await asyncio.sleep(jitter)
        elif not woken:
            attempt += 1
        if await _connectivity_restored(
            endpoint,
            remote_hs=remote_hs,
            woken_by_sync=woken,
            deadline=deadline,
            loop=loop,
        ):
            if not woken and (jitter := min(random.uniform(*jitter_range), max(0.0, deadline - loop.time()))) > 0:  # noqa: S311
                await asyncio.sleep(jitter)
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
