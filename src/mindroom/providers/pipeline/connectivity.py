"""Outer provider pipeline stage: wait and reissue uncommitted requests across corroborated host outages."""

from __future__ import annotations

import asyncio
import random
import ssl
import time
from contextlib import suppress
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING
from weakref import WeakKeyDictionary

import httpx

from mindroom.constants import runtime_matrix_homeserver, runtime_matrix_ssl_verify
from mindroom.llm.errors import CONNECTION_ERROR, STREAM_STALLED, TRANSPORT_TIMEOUT, ProviderUnavailable
from mindroom.llm.provider import ModelSelected, ReportedUsage
from mindroom.llm.usage import add_usage
from mindroom.logging_config import get_logger
from mindroom.providers.pipeline.wrapped import commits_attempt, with_carried_usage
from mindroom.tls_trust import verifying_ssl_context
from mindroom.tool_system.context_bound_streams import closing_async_stream

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from mindroom.constants import RuntimePaths
    from mindroom.llm.provider import ModelProvider, ModelRequest, StreamEvent
    from mindroom.llm.usage import MessageMetrics

__all__ = ["CONNECTIVITY_ERROR_CODES", "connectivity_retry_stream"]

logger = get_logger(__name__)

CONNECTIVITY_ERROR_CODES = frozenset({TRANSPORT_TIMEOUT, CONNECTION_ERROR, STREAM_STALLED})
_CONNECTIVITY_WAIT_LIMIT_SECONDS = 1800.0
_MAX_RECONNECT_ATTEMPTS = 3
_PROBE_TIMEOUT_SECONDS = 3.0
_PROBE_BACKOFF_SECONDS = (5.0, 10.0, 20.0, 40.0, 60.0)
_JITTER_RANGE_SECONDS = (0.5, 3.0)

_homeserver_probes: WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    dict[tuple[str, bool], asyncio.Task[bool]],
] = WeakKeyDictionary()


def _now() -> float:
    """Return monotonic seconds including host suspend time where supported."""
    if hasattr(time, "CLOCK_BOOTTIME"):
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    return time.monotonic()


def _is_cert_error(error: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        if isinstance(current, ssl.SSLCertVerificationError):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


async def _http_probe_homeserver(url: str, verify: bool | ssl.SSLContext, timeout_seconds: float) -> bool:
    try:
        async with (
            asyncio.timeout(timeout_seconds),
            httpx.AsyncClient(timeout=timeout_seconds, verify=verify, follow_redirects=False) as client,
            client.stream("GET", f"{url}/_matrix/client/versions"),
        ):
            return True
    except (httpx.TransportError, OSError, TimeoutError) as exc:
        return _is_cert_error(exc)


async def _probe_homeserver_reachable(
    runtime_paths: RuntimePaths,
    *,
    timeout: float = _PROBE_TIMEOUT_SECONDS,  # noqa: ASYNC109
) -> bool | None:
    url = runtime_matrix_homeserver(runtime_paths).strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        return None
    verify_flag = runtime_matrix_ssl_verify(runtime_paths=runtime_paths)
    key = (url, verify_flag)
    loop = asyncio.get_running_loop()
    loop_probes = _homeserver_probes.setdefault(loop, {})
    task = loop_probes.get(key)
    if task is None or task.done():
        # httpcore mutates ALPN on the SSLContext, so never pass a shared context to httpx.
        verify: bool | ssl.SSLContext = (
            verifying_ssl_context(runtime_paths.process_env, shared=False)
            if url.startswith("https://") and verify_flag
            else verify_flag
        )
        task = loop.create_task(_http_probe_homeserver(url, verify, _PROBE_TIMEOUT_SECONDS))
        loop_probes[key] = task

        def _cleanup(done: asyncio.Task[bool]) -> None:
            if loop_probes.get(key) is done:
                loop_probes.pop(key, None)
            if not done.cancelled():
                with suppress(Exception):
                    done.exception()

        task.add_done_callback(_cleanup)
    try:
        async with asyncio.timeout(timeout):
            return await asyncio.shield(task)
    except TimeoutError:
        return False


@dataclass(slots=True)
class _AttemptState:
    model_id: str
    provider: str
    committed: bool = False
    selected_count: int = 0
    usage: ReportedUsage = field(default_factory=ReportedUsage)

    def observe(self, event: StreamEvent) -> None:
        if isinstance(event, ModelSelected):
            self.selected_count += 1
            self.model_id = event.identity.id
            self.provider = event.identity.provider
            self.usage = ReportedUsage()
        self.usage.observe(event)
        self.committed = self.committed or commits_attempt(event)


async def _wait_for_reconnect(
    attempt: _AttemptState,
    runtime_paths: RuntimePaths,
    *,
    error: ProviderUnavailable,
    deadline: float,
    reconnect_attempts: int,
) -> bool:
    log_fields = {
        "provider": attempt.provider,
        "model_id": attempt.model_id,
        "error_code": error.code,
        "attempt": reconnect_attempts + 1,
        "max_attempts": _MAX_RECONNECT_ATTEMPTS,
    }
    if (remaining := deadline - _now()) <= 0:
        logger.warning("provider_connectivity_wait_expired", **log_fields)
        return False
    logger.warning("provider_connectivity_wait", **log_fields, remaining_seconds=round(remaining, 1))
    probe_attempt = 0
    while (remaining := deadline - _now()) > 0:
        delay = _PROBE_BACKOFF_SECONDS[min(probe_attempt, len(_PROBE_BACKOFF_SECONDS) - 1)]
        await asyncio.sleep(min(delay, remaining))
        probe_attempt += 1
        if (remaining := deadline - _now()) <= 0:
            break
        if await _probe_homeserver_reachable(runtime_paths, timeout=min(_PROBE_TIMEOUT_SECONDS, remaining)) is True:
            if (jitter := min(random.uniform(*_JITTER_RANGE_SECONDS), max(0.0, deadline - _now()))) > 0:  # noqa: S311
                await asyncio.sleep(jitter)
            if _now() >= deadline:
                break
            logger.info("provider_connectivity_resumed", **log_fields)
            return True
    logger.warning("provider_connectivity_wait_expired", **log_fields)
    return False


def connectivity_retry_stream(
    inner: ModelProvider,
    request: ModelRequest,
    *,
    runtime_paths: RuntimePaths | None = None,
) -> AsyncIterator[StreamEvent]:
    """Wait and reissue an uncommitted request after a corroborated host/network outage."""
    if (
        runtime_paths is None
        or request.overrides.transient_retries != "pipeline"
        or not request.overrides.wait_for_connectivity
    ):
        return inner.stream(request)
    return _connectivity_retry_stream(inner, request, runtime_paths)


async def _connectivity_retry_stream(
    inner: ModelProvider,
    request: ModelRequest,
    runtime_paths: RuntimePaths,
) -> AsyncIterator[StreamEvent]:
    deadline: float | None = None
    reconnect_attempts = 0
    carried: MessageMetrics | None = None

    while True:
        attempt = _AttemptState(model_id=inner.identity.id, provider=inner.identity.provider)
        try:
            events = inner.stream(request)
            async with closing_async_stream(events):
                async for event in events:
                    attempt.observe(event)
                    yield event if attempt.selected_count else with_carried_usage(event, carried)
        except ProviderUnavailable as error:
            if (
                attempt.committed
                or error.code not in CONNECTIVITY_ERROR_CODES
                or reconnect_attempts >= _MAX_RECONNECT_ATTEMPTS
                or await _probe_homeserver_reachable(runtime_paths) is not False
            ):
                raise
            if (report := attempt.usage.settle()) is not None:
                yield report if attempt.selected_count else with_carried_usage(report, carried)
            if not attempt.selected_count:
                carried = add_usage(carried, attempt.usage.settled)
            if deadline is None:
                deadline = _now() + _CONNECTIVITY_WAIT_LIMIT_SECONDS
            if not await _wait_for_reconnect(
                attempt,
                runtime_paths,
                error=error,
                deadline=deadline,
                reconnect_attempts=reconnect_attempts,
            ):
                raise
            reconnect_attempts += 1
            strip_replay = request.strip_encrypted_replay or (error.replay_stripped and attempt.selected_count <= 1)
            request = replace(request, retry_budget=None, strip_encrypted_replay=strip_replay)
        else:
            return
