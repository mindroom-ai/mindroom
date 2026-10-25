"""Bounded retries for transient provider failures in streamed requests, and the stream idle limit.

SDK HTTP retries cannot recover errors delivered inside an established SSE stream, so a failed request
is reissued here until the attempt committed: produced output the caller could have shown, or started a hosted
tool that acts or costs, which a retry would run again. A stall is decided the same way. Earlier tool calls and
their results stay in the conversation; only the failed request repeats.

The idle limit ends attempts whose provider sends no event for too long. Adapters report provider data
without output as ``StreamProgress``, so long tool-call arguments or hosted-tool work keep an attempt alive.
HTTP read timeouts cannot catch a stall: proxies such as OpenRouter send SSE keepalive comments while an
upstream hangs, and the SDK drops those comments before they become events.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

from mindroom.llm.errors import ProviderError, ProviderUnavailable
from mindroom.logging_config import get_logger
from mindroom.providers.pipeline.wrapped import Retry, attempts
from mindroom.redaction import redact_sensitive_text

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from mindroom.llm.provider import ModelProvider, ModelRequest, StreamEvent

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
    *,
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


def _reissue(error: ProviderError, *, request: ModelRequest, budget: _RetryBudget, model_id: str) -> Retry | None:
    """Return how to reissue ``request`` after ``error``, or None when the error must propagate."""
    if not error.retryable:
        return None
    if error.code == STREAM_STALLED:
        if budget.stalls >= _MAX_STALL_RETRIES:
            return None
        budget.stalls += 1
        logger.warning("Retrying silent provider stream", model_id=model_id, error=str(error))
        return Retry(request)
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
    return Retry(request, delay)


def retrying_stream(
    inner: ModelProvider,
    request: ModelRequest,
    *,
    idle_timeout_seconds: float | None,
) -> AsyncIterator[StreamEvent]:
    """Stream ``request``, reissuing it after a retryable failure that came before the attempt committed.

    A request whose caller retries it itself passes through unchanged. So does a non-streaming request, which the
    SDK's HTTP retries cover, except on a route that only streams: it streams every request, so each one gets the
    stream's retries and idle limit.
    """
    if request.overrides.caller_retries or (not request.stream and not inner.capabilities.streams_only):
        return inner.stream(request)
    reissue = partial(_reissue, request=request, budget=_RetryBudget(), model_id=inner.identity.id)
    next_event = partial(_next_event, idle_timeout_seconds=idle_timeout_seconds, provider=inner.identity.provider_name)
    return attempts(inner, request, reissue, next_event=next_event)
