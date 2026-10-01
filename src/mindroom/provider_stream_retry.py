"""Bounded retries for transient errors in provider streaming requests.

SDK HTTP retries cannot recover errors delivered inside an established SSE
stream. Agno normalizes these failures to ModelProviderError before turning
them into untyped run-error events, so retry at the invocation boundary while
the response lifecycle still owns the turn. Reissue only the failed request;
earlier tool calls and their results remain in the existing run.

Attempts that yielded content, tool calls, or reasoning cannot be replayed
without duplicating output, so those errors propagate unchanged.

An optional idle limit also ends async attempts whose provider sends no stream
event for too long. HTTP read timeouts cannot catch this: proxies such as
OpenRouter send SSE keepalive comments while an upstream hangs, and the SDK
drops those comments before they become stream events.
"""

from __future__ import annotations

import asyncio
import random
import time
from functools import partial
from typing import TYPE_CHECKING

from agno.exceptions import ModelProviderError

from mindroom.agno_compat_model_hooks import install_stream_invocation_hooks
from mindroom.agno_compat_provider_errors import is_transient_stream_error
from mindroom.logging_config import get_logger
from mindroom.model_stream_output import has_meaningful_stream_output
from mindroom.redaction import redact_sensitive_text

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Callable, Generator, Iterator

    from agno.models.base import Model
    from agno.models.response import ModelResponse

logger = get_logger(__name__)

_STREAM_RETRY_HOOK_ATTR = "_mindroom_provider_stream_retry_hook_installed"

# One initial attempt plus this many re-issued requests. Provider overloads can
# outlive the SDK's short HTTP retry window, especially when they arrive as
# mid-stream SSE error events after the response has already started with 200.
_MAX_TRANSIENT_RETRIES = 4
_RETRY_BASE_DELAY_SECONDS = 1.0
# A silent attempt already waited out the idle limit, so one immediate fresh
# request is enough; a second silent attempt ends the turn instead.
_MAX_STALL_RETRIES = 1


class _ProviderStreamStalledError(ModelProviderError):
    """A streamed provider request sent no event within its idle limit."""

    def __init__(self, model: Model, idle_timeout_seconds: float) -> None:
        super().__init__(
            f"Model stream stalled: no provider event for {idle_timeout_seconds:g}s",
            status_code=504,
            model_name=model.name,
            model_id=model.id,
        )


def _should_reraise(error: ModelProviderError, *, yielded_meaningful_output: bool, attempt: int) -> bool:
    """Return whether one failed attempt must propagate instead of retrying."""
    return yielded_meaningful_output or attempt >= _MAX_TRANSIENT_RETRIES or not is_transient_stream_error(error)


def _retry_delay_seconds(attempt: int) -> float:
    # Jitter spreads retries from agents that failed on the same provider
    # incident, instead of re-hitting it in synchronized pulses.
    return _RETRY_BASE_DELAY_SECONDS * (2**attempt) * (1.0 + random.uniform(0.0, 0.25))  # noqa: S311


def _log_retry(model: Model, error: ModelProviderError, *, attempt: int, delay: float) -> None:
    logger.warning(
        "Retrying provider stream after transient model error",
        model_id=model.id,
        status_code=error.status_code,
        error=redact_sensitive_text(str(error.message), max_length=500),
        attempt=attempt + 1,
        max_retries=_MAX_TRANSIENT_RETRIES,
        delay_seconds=delay,
    )


def _invoke_stream_with_retry(
    model: Model,
    original_invoke_stream: Callable[..., Generator[ModelResponse, None, None]],
    *args: object,
    **kwargs: object,
) -> Iterator[ModelResponse]:
    """Replay one synchronous stream request after transient pre-output errors."""
    for attempt in range(_MAX_TRANSIENT_RETRIES + 1):
        yielded_meaningful_output = False
        stream = original_invoke_stream(*args, **kwargs)
        try:
            for response in stream:
                yielded_meaningful_output = yielded_meaningful_output or has_meaningful_stream_output(response)
                yield response
        except ModelProviderError as error:
            if _should_reraise(error, yielded_meaningful_output=yielded_meaningful_output, attempt=attempt):
                raise
            delay = _retry_delay_seconds(attempt)
            _log_retry(model, error, attempt=attempt, delay=delay)
            time.sleep(delay)
        else:
            return
        finally:
            # If the consumer closes us mid-stream (GeneratorExit at the yield
            # above), close the underlying request instead of leaving it to GC.
            stream.close()


async def _next_response(
    model: Model,
    stream: AsyncGenerator[ModelResponse, None],
    idle_timeout_seconds: float | None,
) -> ModelResponse:
    """Return the next provider event, raising once the provider stays silent too long."""
    if idle_timeout_seconds is None:
        return await anext(stream)
    deadline = asyncio.timeout(idle_timeout_seconds)
    try:
        async with deadline:
            return await anext(stream)
    except TimeoutError:
        if not deadline.expired():
            raise
        raise _ProviderStreamStalledError(model, idle_timeout_seconds) from None


async def _ainvoke_stream_with_retry(
    model: Model,
    original_ainvoke_stream: Callable[..., AsyncGenerator[ModelResponse, None]],
    idle_timeout_seconds: float | None,
    *args: object,
    **kwargs: object,
) -> AsyncIterator[ModelResponse]:
    """Replay one asynchronous stream request after transient or silent pre-output failures."""
    transient_retries = 0
    stall_retries = 0
    while True:
        yielded_meaningful_output = False
        stream = original_ainvoke_stream(*args, **kwargs)
        try:
            while True:
                try:
                    response = await _next_response(model, stream, idle_timeout_seconds)
                except StopAsyncIteration:
                    return
                yielded_meaningful_output = yielded_meaningful_output or has_meaningful_stream_output(response)
                yield response
        except _ProviderStreamStalledError as error:
            if yielded_meaningful_output or stall_retries >= _MAX_STALL_RETRIES:
                raise
            stall_retries += 1
            logger.warning("Retrying silent provider stream", model_id=model.id, error=error.message)
        except ModelProviderError as error:
            if _should_reraise(error, yielded_meaningful_output=yielded_meaningful_output, attempt=transient_retries):
                raise
            delay = _retry_delay_seconds(transient_retries)
            _log_retry(model, error, attempt=transient_retries, delay=delay)
            transient_retries += 1
            await asyncio.sleep(delay)
        finally:
            # Async generators abandoned mid-stream are only finalized by the
            # GC hook; close the underlying request deterministically when the
            # consumer cancels or closes us at the yield above.
            await stream.aclose()


def install_provider_stream_retry_hook(model: Model, *, idle_timeout_seconds: float | None = None) -> None:
    """Wrap a model's stream invocations with transient-error retries.

    Idempotent per model instance. Only attempts that have not yet yielded
    meaningful output are retried; anything else re-raises immediately so
    partially streamed responses are never duplicated. ``idle_timeout_seconds``
    bounds the silence between provider events in async streams, which every
    MindRoom turn uses; a blocking sync iteration cannot be interrupted.
    """
    install_stream_invocation_hooks(
        model,
        marker=_STREAM_RETRY_HOOK_ATTR,
        wrap_sync=lambda original: partial(_invoke_stream_with_retry, model, original),
        wrap_async=lambda original: partial(_ainvoke_stream_with_retry, model, original, idle_timeout_seconds),
    )
