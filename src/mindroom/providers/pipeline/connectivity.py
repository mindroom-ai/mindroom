"""Outer provider pipeline stage: wait and reissue uncommitted requests across corroborated host outages."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING
from uuid import uuid4

from mindroom.connectivity import (
    corroborate_offline,
    is_endpoint_known_reachable,
    note_endpoint_reachable,
    set_stream_activity,
    wait_for_connectivity,
)
from mindroom.llm.errors import CONNECTION_ERROR, STREAM_STALLED, TRANSPORT_TIMEOUT, ProviderUnavailable
from mindroom.llm.provider import ModelSelected, ReportedUsage, TurnCompleted
from mindroom.llm.usage import add_usage
from mindroom.logging_config import get_logger
from mindroom.providers.pipeline.wrapped import commits_attempt, with_carried_usage
from mindroom.tool_system.context_bound_streams import closing_async_stream

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from mindroom.llm.provider import ModelProvider, ModelRequest, StreamEvent
    from mindroom.llm.usage import MessageMetrics

__all__ = ["CONNECTIVITY_ERROR_CODES", "connectivity_retry_stream"]

logger = get_logger(__name__)

CONNECTIVITY_ERROR_CODES = frozenset({TRANSPORT_TIMEOUT, CONNECTION_ERROR, STREAM_STALLED})
_CONNECTIVITY_WAIT_LIMIT_SECONDS = 1800.0
_MAX_RECONNECT_ATTEMPTS = 3
_WAITING_STATUS_TEXT = "⏳ Waiting for connection — will retry automatically."


@dataclass(slots=True)
class _AttemptState:
    endpoint: str | None
    model_id: str
    provider: str
    committed: bool = False
    saw_model_selected: bool = False
    attempted_endpoints: list[str] = field(default_factory=list)
    usage: ReportedUsage = field(default_factory=ReportedUsage)

    def observe(self, event: StreamEvent) -> None:
        if isinstance(event, ModelSelected):
            self.saw_model_selected = True
            self.endpoint = event.identity.endpoint
            self.model_id = event.identity.id
            self.provider = event.identity.provider
            if self.endpoint and self.endpoint not in self.attempted_endpoints:
                self.attempted_endpoints.append(self.endpoint)
            self.usage = ReportedUsage()
        self.usage.observe(event)
        self.committed = self.committed or commits_attempt(event)
        if isinstance(event, TurnCompleted):
            note_endpoint_reachable(self.endpoint)

    def probe_endpoint(self) -> str | None:
        for ep in self.attempted_endpoints:
            if is_endpoint_known_reachable(ep):
                return ep
        return self.attempted_endpoints[0] if self.attempted_endpoints else self.endpoint


async def _wait_for_reconnect(
    attempt: _AttemptState,
    *,
    error: ProviderUnavailable,
    probe_endpoint: str | None,
    wait_id: str,
    deadline: float,
    reconnect_attempts: int,
) -> bool:
    """Publish waiting activity, wait until connectivity returns or ``deadline`` expires, and log transitions."""
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        logger.warning(
            "provider_connectivity_wait_expired",
            provider=attempt.provider,
            model_id=attempt.model_id,
            endpoint=probe_endpoint,
            error_code=error.code,
            reconnect_attempts=reconnect_attempts,
        )
        return False
    logger.warning(
        "provider_connectivity_wait",
        provider=attempt.provider,
        model_id=attempt.model_id,
        endpoint=probe_endpoint,
        error_code=error.code,
        reconnect_attempt=reconnect_attempts + 1,
        max_reconnect_attempts=_MAX_RECONNECT_ATTEMPTS,
        remaining_seconds=round(remaining, 1),
    )
    set_stream_activity(wait_id, _WAITING_STATUS_TEXT)
    try:
        recovered = await wait_for_connectivity(probe_endpoint, deadline=deadline)
    finally:
        set_stream_activity(wait_id, None)
    if not recovered:
        logger.warning(
            "provider_connectivity_wait_expired",
            provider=attempt.provider,
            model_id=attempt.model_id,
            endpoint=probe_endpoint,
            error_code=error.code,
            reconnect_attempts=reconnect_attempts,
        )
        return False
    logger.info(
        "provider_connectivity_resumed",
        provider=attempt.provider,
        model_id=attempt.model_id,
        endpoint=probe_endpoint,
        reconnect_attempt=reconnect_attempts + 1,
    )
    return True


def connectivity_retry_stream(inner: ModelProvider, request: ModelRequest) -> AsyncIterator[StreamEvent]:
    """Wait and reissue an uncommitted request after a corroborated host/network outage."""
    if request.overrides.transient_retries != "pipeline" or not request.overrides.wait_for_connectivity:
        return inner.stream(request)
    return _connectivity_retry_stream(inner, request)


async def _connectivity_retry_stream(inner: ModelProvider, request: ModelRequest) -> AsyncIterator[StreamEvent]:
    loop = asyncio.get_running_loop()
    deadline: float | None = None
    reconnect_attempts = 0
    carried: MessageMetrics | None = None
    wait_id = uuid4().hex

    while True:
        attempt = _AttemptState(
            endpoint=inner.identity.endpoint,
            model_id=inner.identity.id,
            provider=inner.identity.provider,
        )
        try:
            events = inner.stream(request)
            async with closing_async_stream(events):
                async for event in events:
                    attempt.observe(event)
                    yield event if attempt.saw_model_selected else with_carried_usage(event, carried)
        except ProviderUnavailable as error:
            probe_endpoint = attempt.probe_endpoint()
            if (
                attempt.committed
                or error.code not in CONNECTIVITY_ERROR_CODES
                or reconnect_attempts >= _MAX_RECONNECT_ATTEMPTS
                or not await corroborate_offline(probe_endpoint, is_stream_stalled=(error.code == STREAM_STALLED))
            ):
                raise
            if (report := attempt.usage.settle()) is not None:
                yield report if attempt.saw_model_selected else with_carried_usage(report, carried)
            if not attempt.saw_model_selected:
                carried = add_usage(carried, attempt.usage.settled)
            if deadline is None:
                deadline = loop.time() + _CONNECTIVITY_WAIT_LIMIT_SECONDS
            if not await _wait_for_reconnect(
                attempt,
                error=error,
                probe_endpoint=probe_endpoint,
                wait_id=wait_id,
                deadline=deadline,
                reconnect_attempts=reconnect_attempts,
            ):
                raise
            reconnect_attempts += 1
            request = replace(
                request,
                retry_budget=None,
                strip_encrypted_replay=request.strip_encrypted_replay or error.replay_stripped,
            )
        else:
            return
