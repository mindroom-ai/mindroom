"""Bounded retries for transient provider failures, and the stream idle limit.

This stage retries a failed model request; MindRoom turns off the SDK clients' configurable retries, though google-genai
still resends on its own in a few places it hardcodes, such as a Gemini file upload's transfer and a dropped connection.
A failed request is reissued here until the attempt committed: produced output the caller could have shown, or started a
hosted tool that acts or costs, which a retry would run again. A stall is decided the same way. Earlier tool calls and
their results stay in the conversation; only the
failed request repeats.

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

from mindroom.llm.errors import ProviderError, ProviderUnavailable, incomplete_turn_error
from mindroom.llm.provider import TurnCompleted
from mindroom.logging_config import get_logger
from mindroom.providers.pipeline.wrapped import Retry, WrappedProvider, attempts
from mindroom.redaction import redact_sensitive_text
from mindroom.tool_system.context_bound_streams import closing_async_stream

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from mindroom.llm.provider import ModelProvider, ModelRequest, StreamEvent

__all__ = ["STREAM_STALLED", "retrying_stream"]

logger = get_logger(__name__)

STREAM_STALLED = "stream_stalled"
# One initial attempt plus this many reissued requests, enough to outlast a short provider overload, which also
# arrives as an SSE error event after the response already started with 200.
_MAX_TRANSIENT_RETRIES = 4
_RETRY_BASE_DELAY_SECONDS = 1.0
# A provider's ``Retry-After`` hint up to this long replaces the backoff, as the SDKs honored it; a longer one is
# treated as no hint, so one header cannot hold a reply for minutes.
_MAX_RETRY_AFTER_SECONDS = 60.0
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
        _log_retry(
            error,
            model_id=model_id,
            reason=STREAM_STALLED,
            attempt=budget.stalls,
            max_retries=_MAX_STALL_RETRIES,
            delay_seconds=0.0,
        )
        return Retry(request)
    if budget.transient >= _MAX_TRANSIENT_RETRIES:
        return None
    hint = error.retry_after_seconds
    delay = (
        hint if hint is not None and 0 < hint <= _MAX_RETRY_AFTER_SECONDS else _retry_delay_seconds(budget.transient)
    )
    budget.transient += 1
    _log_retry(
        error,
        model_id=model_id,
        reason="transient_error",
        attempt=budget.transient,
        max_retries=_MAX_TRANSIENT_RETRIES,
        delay_seconds=delay,
    )
    return Retry(request, delay)


def _log_retry(
    error: ProviderError,
    *,
    model_id: str,
    reason: str,
    attempt: int,
    max_retries: int,
    delay_seconds: float,
) -> None:
    """Log one reissued request as the documented operator event, the same fields for either reason."""
    logger.warning(
        "provider_request_retried",
        model_id=model_id,
        reason=reason,
        error_type=type(error).__name__,
        status_code=error.status_code,
        error=redact_sensitive_text(str(error), max_length=500),
        attempt=attempt,
        max_retries=max_retries,
        delay_seconds=delay_seconds,
    )


async def _fail_empty_incomplete(inner: ModelProvider, request: ModelRequest) -> AsyncIterator[StreamEvent]:
    """Raise ``ProviderUnavailable`` for an empty ``incomplete`` turn so ``attempts`` retries it when uncommitted."""
    events = inner.stream(request)
    async with closing_async_stream(events):
        async for event in events:
            if (
                isinstance(event, TurnCompleted)
                and event.stop_reason == "incomplete"
                and not event.message.tool_calls
                and not event.message.get_content_string().strip()
            ):
                raise incomplete_turn_error(
                    provider=inner.identity.provider,
                    raw_stop_reason=event.raw_stop_reason,
                )
            yield event


def retrying_stream(
    inner: ModelProvider,
    request: ModelRequest,
    *,
    idle_timeout_seconds: float | None,
) -> AsyncIterator[StreamEvent]:
    """Stream ``request``, reissuing it after a retryable failure that came before the attempt committed.

    A request whose caller retries it itself passes through unchanged, without the idle limit. On a route with
    ``sticky_replay_recovery``, a failure the transient retries did not resolve is resent without provider-encrypted
    replay state, once: a failure of that resend, which ``attempts`` marks and keeps without that state, gets only
    the transient retries the request has left. So a rate limit that used them all up gets the one resend, and a
    rejection on the first attempt leaves its resend every retry. The budget goes with the failure, so a stage that
    rewrites a request already sent without that state, such as the media fallback, reissues it with what is left.
    """
    if request.overrides.caller_retries:
        return inner.stream(request)
    identity, recover = inner.identity, inner.capabilities.sticky_replay_recovery
    budget = request.retry_budget if isinstance(request.retry_budget, _RetryBudget) else _RetryBudget()
    transient = partial(_reissue, request=request, budget=budget, model_id=identity.id)

    def reissue(error: ProviderError) -> Retry | None:
        error.retry_budget = budget
        retry = transient(error)
        if retry is not None or error.replay_stripped or recover is None:
            return retry
        if (recovered := recover(request, error)) is None:
            return None
        logger.warning(
            "Retrying model request without provider-encrypted replay state",
            provider=identity.provider,
            model_id=identity.id,
            error_type=type(error).__name__,
            status_code=error.status_code,
            error=redact_sensitive_text(str(error), max_length=500),
        )
        return Retry(recovered)

    next_event = partial(_next_event, idle_timeout_seconds=idle_timeout_seconds, provider=identity.provider)
    return attempts(WrappedProvider(inner, _fail_empty_incomplete), request, reissue, next_event=next_event)
