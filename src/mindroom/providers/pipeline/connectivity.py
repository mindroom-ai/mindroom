"""Outer provider pipeline stage: wait and reissue uncommitted requests across corroborated host outages."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING
from uuid import uuid4

from mindroom.connectivity import corroborate_offline, set_stream_activity, wait_for_connectivity
from mindroom.llm.errors import ProviderUnavailable
from mindroom.llm.provider import ModelSelected, ReportedUsage
from mindroom.llm.usage import add_usage
from mindroom.logging_config import get_logger
from mindroom.providers.pipeline.fallback import CONNECTIVITY_ERROR_CODES, rollback_offline_cooldowns
from mindroom.providers.pipeline.wrapped import commits_attempt, with_carried_usage
from mindroom.tool_system.context_bound_streams import closing_async_stream

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from mindroom.llm.provider import ModelProvider, ModelRequest, StreamEvent
    from mindroom.llm.usage import MessageMetrics

__all__ = ["CONNECTIVITY_ERROR_CODES", "connectivity_retry_stream"]

logger = get_logger(__name__)

_CONNECTIVITY_WAIT_LIMIT_SECONDS = 1800.0
_MAX_RECONNECT_ATTEMPTS = 3
_WAITING_STATUS_TEXT = "⏳ Waiting for connection — will retry automatically."


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
    *,
    error: ProviderUnavailable,
    wait_id: str,
    deadline: float,
    reconnect_attempts: int,
) -> bool:
    """Roll back outage cooldowns, publish waiting activity, and wait until recovery or deadline."""
    log_fields = {
        "provider": attempt.provider,
        "model_id": attempt.model_id,
        "error_code": error.code,
        "attempt": reconnect_attempts + 1,
        "max_attempts": _MAX_RECONNECT_ATTEMPTS,
    }
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        logger.warning("provider_connectivity_wait_expired", **log_fields)
        return False
    rollback_offline_cooldowns(error)
    logger.warning("provider_connectivity_wait", **log_fields, remaining_seconds=round(remaining, 1))
    set_stream_activity(wait_id, _WAITING_STATUS_TEXT)
    try:
        recovered = await wait_for_connectivity(deadline=deadline)
    finally:
        set_stream_activity(wait_id, None)
    if not recovered:
        logger.warning("provider_connectivity_wait_expired", **log_fields)
        return False
    logger.info("provider_connectivity_resumed", **log_fields)
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
                or not await corroborate_offline()
            ):
                raise
            if (report := attempt.usage.settle()) is not None:
                yield report if attempt.selected_count else with_carried_usage(report, carried)
            if not attempt.selected_count:
                carried = add_usage(carried, attempt.usage.settled)
            if deadline is None:
                deadline = loop.time() + _CONNECTIVITY_WAIT_LIMIT_SECONDS
            if not await _wait_for_reconnect(
                attempt,
                error=error,
                wait_id=wait_id,
                deadline=deadline,
                reconnect_attempts=reconnect_attempts,
            ):
                raise
            reconnect_attempts += 1
            strip_replay = request.strip_encrypted_replay or (error.replay_stripped and attempt.selected_count <= 1)
            request = replace(request, retry_budget=None, strip_encrypted_replay=strip_replay)
        else:
            return
