"""Transient-error retry for Claude streaming model requests.

The Anthropic SDK retries HTTP-level failures (429/5xx, connection errors)
before a stream is established, but once the response is committed as 200 the
API reports server faults as an SSE ``error`` event instead. The SDK raises
that event as an ``APIStatusError`` with status code 200 and never retries it,
Agno converts it into a generic ``ModelProviderError``, and without this hook
the whole agent run fails on a documented-retryable fault (observed in
production as ``{'type': 'api_error', 'message': 'Internal server error'}``
from Vertex).

This hook wraps ``invoke_stream``/``ainvoke_stream`` on Anthropic-family Claude
models (direct API, Vertex, and Bedrock share the Agno model class) and
re-issues the request when an attempt fails with a transient error before
producing any meaningful output. Attempts that already yielded content, tool
calls, or reasoning cannot be replayed without duplicating what downstream
consumers streamed to the user, so those errors propagate unchanged.
"""

from __future__ import annotations

import asyncio
import random
import time
from functools import partial
from typing import TYPE_CHECKING, cast

from agno.exceptions import ContextWindowExceededError, ModelProviderError

from mindroom.claude_prompt_cache import as_anthropic_claude
from mindroom.error_handling import TRANSIENT_PROVIDER_STATUS_CODES, ModelSafeguardRefusalError
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Callable, Generator, Iterator
    from typing import Any

    from agno.models.anthropic import Claude as AnthropicClaude
    from agno.models.response import ModelResponse

logger = get_logger(__name__)

_STREAM_RETRY_HOOK_ATTR = "_mindroom_claude_stream_retry_hook_installed"

# One initial attempt plus this many re-issued requests. Provider overloads can
# outlive the SDK's short HTTP retry window, especially when they arrive as
# mid-stream SSE error events after the response has already started with 200.
_MAX_TRANSIENT_RETRIES = 4
_RETRY_BASE_DELAY_SECONDS = 1.0


def _is_transient_model_error(error: BaseException) -> bool:
    """Return whether one model-call failure is worth re-issuing the request."""
    if isinstance(error, (ContextWindowExceededError, ModelSafeguardRefusalError)):
        return False
    return isinstance(error, ModelProviderError) and error.status_code in TRANSIENT_PROVIDER_STATUS_CODES


def _has_meaningful_output(response: ModelResponse) -> bool:
    """Return whether one streamed delta already reached downstream consumers.

    Anything beyond role/event bookkeeping counts: downstream consumers
    accumulate these fields (Agno extends provider-data lists, MindRoom's
    collector appends content), so replaying an attempt that set any of them
    would duplicate state.
    """
    return bool(
        response.content
        or response.parsed
        or response.audio
        or response.images
        or response.videos
        or response.audios
        or response.files
        or response.tool_calls
        or response.tool_executions
        or response.provider_data
        or response.reasoning_content
        or response.redacted_reasoning_content
        or response.citations
        or response.response_usage
        or response.extra
        or response.updated_session_state,
    )


def _should_reraise(error: ModelProviderError, *, yielded_meaningful_output: bool, attempt: int) -> bool:
    """Return whether one failed attempt must propagate instead of retrying."""
    return yielded_meaningful_output or attempt >= _MAX_TRANSIENT_RETRIES or not _is_transient_model_error(error)


def _retry_delay_seconds(attempt: int) -> float:
    # Jitter spreads retries from agents that failed on the same provider
    # incident, instead of re-hitting it in synchronized pulses.
    return _RETRY_BASE_DELAY_SECONDS * (2**attempt) * (1.0 + random.uniform(0.0, 0.25))  # noqa: S311


def _log_retry(model: AnthropicClaude, error: ModelProviderError, *, attempt: int, delay: float) -> None:
    logger.warning(
        "Retrying Claude stream after transient model error",
        model_id=model.id,
        status_code=error.status_code,
        error=str(error.message),
        attempt=attempt + 1,
        max_retries=_MAX_TRANSIENT_RETRIES,
        delay_seconds=delay,
    )


def _invoke_stream_with_retry(
    model: AnthropicClaude,
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
                yielded_meaningful_output = yielded_meaningful_output or _has_meaningful_output(response)
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


async def _ainvoke_stream_with_retry(
    model: AnthropicClaude,
    original_ainvoke_stream: Callable[..., AsyncGenerator[ModelResponse, None]],
    *args: object,
    **kwargs: object,
) -> AsyncIterator[ModelResponse]:
    """Replay one asynchronous stream request after transient pre-output errors."""
    for attempt in range(_MAX_TRANSIENT_RETRIES + 1):
        yielded_meaningful_output = False
        stream = original_ainvoke_stream(*args, **kwargs)
        try:
            async for response in stream:
                yielded_meaningful_output = yielded_meaningful_output or _has_meaningful_output(response)
                yield response
        except ModelProviderError as error:
            if _should_reraise(error, yielded_meaningful_output=yielded_meaningful_output, attempt=attempt):
                raise
            delay = _retry_delay_seconds(attempt)
            _log_retry(model, error, attempt=attempt, delay=delay)
            await asyncio.sleep(delay)
        else:
            return
        finally:
            # Async generators abandoned mid-stream are only finalized by the
            # GC hook; close the underlying request deterministically when the
            # consumer cancels or closes us at the yield above.
            await stream.aclose()


def install_claude_stream_retry_hook(model: object) -> None:
    """Wrap a Claude model's stream invocations with transient-error retries.

    Idempotent per model instance. Only attempts that have not yet yielded
    meaningful output are retried; anything else re-raises immediately so
    partially streamed responses are never duplicated.
    """
    claude_model = as_anthropic_claude(model)
    if claude_model is None:
        return
    model_dict = vars(claude_model)
    if model_dict.get(_STREAM_RETRY_HOOK_ATTR) is True:
        return
    original_invoke_stream = claude_model.invoke_stream
    original_ainvoke_stream = claude_model.ainvoke_stream
    model_dict[_STREAM_RETRY_HOOK_ATTR] = True
    model_dict["invoke_stream"] = partial(_invoke_stream_with_retry, claude_model, original_invoke_stream)
    model_dict["ainvoke_stream"] = cast(
        "Any",
        partial(_ainvoke_stream_with_retry, claude_model, original_ainvoke_stream),
    )
