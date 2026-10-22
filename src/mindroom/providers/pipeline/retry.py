"""Bounded retries for transient provider failures in streamed requests, and the stream idle limit.

SDK HTTP retries cannot recover errors delivered inside an established SSE stream, so a failed request
is reissued here while it has produced nothing the caller could have shown. Earlier tool calls and their
results stay in the conversation; only the failed request repeats.

The idle limit ends attempts whose provider sends no event for too long. Adapters report provider data
without output as ``StreamProgress``, so long tool-call arguments or hosted-tool work keep an attempt alive.
HTTP read timeouts cannot catch a stall: proxies such as OpenRouter send SSE keepalive comments while an
upstream hangs, and the SDK drops those comments before they become events.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.llm.errors import ProviderError, ProviderUnavailable
from mindroom.logging_config import get_logger
from mindroom.providers.pipeline.wrapped import AttemptUsage, add_usage, is_visible_output, with_carried_usage
from mindroom.redaction import redact_sensitive_text
from mindroom.tool_system.context_bound_streams import closing_async_stream

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from mindroom.llm.provider import ModelProvider, ModelRequest, StreamEvent
    from mindroom.llm.usage import MessageMetrics

__all__ = ["STREAM_STALLED", "retrying_stream"]

logger = get_logger(__name__)

STREAM_STALLED = "stream_stalled"
# One initial attempt plus this many reissued requests. Provider overloads can outlive the SDK's short HTTP
# retry window, especially as mid-stream SSE error events after the response already started with 200.
_MAX_TRANSIENT_RETRIES = 4
_RETRY_BASE_DELAY_SECONDS = 1.0
# A silent attempt already waited out the idle limit, so one immediate fresh request is enough.
_MAX_STALL_RETRIES = 1


def _retry_delay_seconds(attempt: int) -> float:
    # Jitter spreads retries from agents that failed on the same incident instead of re-hitting it together.
    return _RETRY_BASE_DELAY_SECONDS * (2**attempt) * (1.0 + random.uniform(0.0, 0.25))  # noqa: S311


async def _next_event(
    stream: AsyncIterator[StreamEvent],
    idle_timeout_seconds: float | None,
    provider: str,
) -> StreamEvent:
    """Return the next event, raising once the provider stays silent past the idle limit."""
    if idle_timeout_seconds is None:
        return await anext(stream)
    deadline = asyncio.timeout(idle_timeout_seconds)
    try:
        async with deadline:
            return await anext(stream)
    except TimeoutError:
        if not deadline.expired():
            raise
        msg = f"Model stream stalled: no provider event for {idle_timeout_seconds:g}s"
        raise ProviderUnavailable(msg, provider=provider, status_code=504, code=STREAM_STALLED) from None


@dataclass(slots=True)
class _RetryBudget:
    transient: int = 0
    stalls: int = 0


def _retry_delay(error: ProviderError, budget: _RetryBudget, model_id: str) -> float | None:
    """Return the wait before reissuing the request, or None when the error must propagate."""
    if not error.retryable:
        return None
    if error.code == STREAM_STALLED:
        if budget.stalls >= _MAX_STALL_RETRIES:
            return None
        budget.stalls += 1
        logger.warning("Retrying silent provider stream", model_id=model_id, error=str(error))
        return 0.0
    if budget.transient >= _MAX_TRANSIENT_RETRIES:
        return None
    delay = _retry_delay_seconds(budget.transient)
    budget.transient += 1
    logger.warning(
        "Retrying provider stream after transient model error",
        model_id=model_id,
        status_code=error.status_code,
        error=redact_sensitive_text(str(error), max_length=500),
        attempt=budget.transient,
        max_retries=_MAX_TRANSIENT_RETRIES,
        delay_seconds=delay,
    )
    return delay


def retrying_stream(
    inner: ModelProvider,
    request: ModelRequest,
    *,
    idle_timeout_seconds: float | None,
) -> AsyncIterator[StreamEvent]:
    """Stream ``request``, reissuing it after a retryable failure that came before any visible output.

    Non-streaming requests pass through unchanged: their callers own deadlines and retry policy.
    """
    if not request.stream:
        return inner.stream(request)
    return _retried(inner, request, idle_timeout_seconds)


async def _retried(
    inner: ModelProvider,
    request: ModelRequest,
    idle_timeout_seconds: float | None,
) -> AsyncIterator[StreamEvent]:
    identity = inner.identity
    budget = _RetryBudget()
    carried: MessageMetrics | None = None
    while True:
        visible = False
        usage = AttemptUsage()
        try:
            events = inner.stream(request)
            async with closing_async_stream(events):
                while True:
                    try:
                        event = await _next_event(events, idle_timeout_seconds, identity.provider_name)
                    except StopAsyncIteration:
                        return
                    usage.observe(event)
                    visible = visible or is_visible_output(event)
                    yield with_carried_usage(event, carried)
        except ProviderError as error:
            if visible or (delay := _retry_delay(error, budget, identity.id)) is None:
                raise
            if (report := usage.settle()) is not None:
                yield report
            if delay:
                await asyncio.sleep(delay)
        carried = add_usage(carried, usage.settled)
