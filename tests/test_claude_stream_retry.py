"""Transient-error retry behavior for Claude streaming invocations."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from agno.exceptions import ContextWindowExceededError, ModelProviderError, ModelRateLimitError
from agno.models.anthropic import Claude
from agno.models.response import ModelResponse

from mindroom import claude_stream_retry
from mindroom.claude_stream_retry import install_claude_stream_retry_hook

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator


@pytest.fixture(autouse=True)
def _no_retry_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_stream_retry, "_RETRY_BASE_DELAY_SECONDS", 0.0)


def _hooked_model_with_async_attempts(attempts: list[list[ModelResponse | Exception]]) -> tuple[Claude, list[int]]:
    """Return a hooked model whose ainvoke_stream replays scripted attempts."""
    model = Claude(id="claude-sonnet-5")
    calls: list[int] = []

    async def fake_ainvoke_stream(*_args: object, **_kwargs: object) -> AsyncIterator[ModelResponse]:
        calls.append(len(calls))
        for item in attempts[len(calls) - 1]:
            if isinstance(item, Exception):
                raise item
            yield item

    vars(model)["ainvoke_stream"] = fake_ainvoke_stream
    install_claude_stream_retry_hook(model)
    return model, calls


def _hooked_model_with_sync_attempts(attempts: list[list[ModelResponse | Exception]]) -> tuple[Claude, list[int]]:
    model = Claude(id="claude-sonnet-5")
    calls: list[int] = []

    def fake_invoke_stream(*_args: object, **_kwargs: object) -> Iterator[ModelResponse]:
        calls.append(len(calls))
        for item in attempts[len(calls) - 1]:
            if isinstance(item, Exception):
                raise item
            yield item

    vars(model)["invoke_stream"] = fake_invoke_stream
    install_claude_stream_retry_hook(model)
    return model, calls


def _mid_stream_api_error() -> ModelProviderError:
    return ModelProviderError(
        message="{'type': 'error', 'error': {'type': 'api_error', 'message': 'Internal server error'}}",
        status_code=200,
        model_id="claude-sonnet-5",
    )


async def _collect(stream: AsyncIterator[ModelResponse]) -> list[ModelResponse]:
    return [response async for response in stream]


async def _collect_into(stream: AsyncIterator[ModelResponse], collected: list[ModelResponse]) -> None:
    # Append as we go: the stream is expected to raise mid-iteration and the
    # partially collected responses are the assertion target.
    async for response in stream:
        collected.append(response)  # noqa: PERF401


@pytest.mark.asyncio
async def test_retries_mid_stream_api_error_before_output() -> None:
    """A mid-stream api_error before any output re-issues the request."""
    model, calls = _hooked_model_with_async_attempts(
        [
            [_mid_stream_api_error()],
            [ModelResponse(content="hello"), ModelResponse(content=" world")],
        ],
    )

    responses = await _collect(model.ainvoke_stream([], object()))

    assert [response.content for response in responses] == ["hello", " world"]
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_retries_after_bookkeeping_only_responses() -> None:
    """Role-only bookkeeping deltas do not block a retry."""
    model, calls = _hooked_model_with_async_attempts(
        [
            [ModelResponse(role="assistant"), _mid_stream_api_error()],
            [ModelResponse(content="recovered")],
        ],
    )

    responses = await _collect(model.ainvoke_stream([], object()))

    assert responses[-1].content == "recovered"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_does_not_retry_after_meaningful_output() -> None:
    """Errors after streamed content propagate so text is never duplicated."""
    model, calls = _hooked_model_with_async_attempts(
        [
            [ModelResponse(content="partial"), _mid_stream_api_error()],
            [ModelResponse(content="never reached")],
        ],
    )

    collected: list[ModelResponse] = []
    with pytest.raises(ModelProviderError):
        await _collect_into(model.ainvoke_stream([], object()), collected)

    assert [response.content for response in collected] == ["partial"]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_does_not_retry_after_provider_data_delta() -> None:
    """Provider-data deltas (e.g. thinking signatures) count as meaningful output."""
    model, calls = _hooked_model_with_async_attempts(
        [
            [ModelResponse(provider_data={"signature": "sig"}), _mid_stream_api_error()],
            [ModelResponse(content="never reached")],
        ],
    )

    with pytest.raises(ModelProviderError):
        await _collect(model.ainvoke_stream([], object()))

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_does_not_retry_non_transient_error() -> None:
    """Client errors such as HTTP 400 are not retried."""
    model, calls = _hooked_model_with_async_attempts(
        [[ModelProviderError(message="invalid request", status_code=400)]],
    )

    with pytest.raises(ModelProviderError):
        await _collect(model.ainvoke_stream([], object()))

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_does_not_retry_context_window_error() -> None:
    """Context-window overflows are permanent and never retried."""
    model, calls = _hooked_model_with_async_attempts(
        [[ContextWindowExceededError(message="prompt is too long")]],
    )

    with pytest.raises(ContextWindowExceededError):
        await _collect(model.ainvoke_stream([], object()))

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_retries_rate_limit_then_gives_up() -> None:
    """Rate limits retry up to the cap, then the last error propagates."""
    attempts: list[list[ModelResponse | Exception]] = [
        [ModelRateLimitError(message="overloaded_error", status_code=529)]
        for _ in range(claude_stream_retry._MAX_TRANSIENT_RETRIES + 1)
    ]
    model, calls = _hooked_model_with_async_attempts(attempts)

    with pytest.raises(ModelRateLimitError):
        await _collect(model.ainvoke_stream([], object()))

    assert len(calls) == claude_stream_retry._MAX_TRANSIENT_RETRIES + 1


@pytest.mark.asyncio
async def test_retries_sustained_overload_before_success() -> None:
    """A provider overload lasting beyond the old retry window can recover."""
    model, calls = _hooked_model_with_async_attempts(
        [
            [ModelRateLimitError(message="overloaded_error", status_code=529)],
            [ModelRateLimitError(message="overloaded_error", status_code=529)],
            [ModelRateLimitError(message="overloaded_error", status_code=529)],
            [ModelRateLimitError(message="overloaded_error", status_code=529)],
            [ModelResponse(content="recovered")],
        ],
    )

    responses = await _collect(model.ainvoke_stream([], object()))

    assert [response.content for response in responses] == ["recovered"]
    assert len(calls) == 5


def test_sync_stream_retries_transient_error() -> None:
    """The synchronous stream path retries transient errors too."""
    model, calls = _hooked_model_with_sync_attempts(
        [
            [_mid_stream_api_error()],
            [ModelResponse(content="recovered")],
        ],
    )

    responses = list(model.invoke_stream([], object()))

    assert [response.content for response in responses] == ["recovered"]
    assert len(calls) == 2


def test_install_is_idempotent() -> None:
    """A second install must not stack another retry layer on top of the first.

    The script exhausts the retry budget and only then offers a success: a
    single hook raises after the final retry, while a stacked double hook
    would keep going and reach the success attempt.
    """
    attempts: list[list[ModelResponse | Exception]] = [
        [_mid_stream_api_error()] for _ in range(claude_stream_retry._MAX_TRANSIENT_RETRIES + 1)
    ]
    attempts.append([ModelResponse(content="only reachable when double-wrapped")])
    model, calls = _hooked_model_with_sync_attempts(attempts)
    install_claude_stream_retry_hook(model)

    with pytest.raises(ModelProviderError):
        list(model.invoke_stream([], object()))

    assert len(calls) == claude_stream_retry._MAX_TRANSIENT_RETRIES + 1


def test_early_close_closes_underlying_sync_stream() -> None:
    """Closing the wrapper mid-stream closes the in-flight request too."""
    model = Claude(id="claude-sonnet-5")
    finalized: list[bool] = []

    def fake_invoke_stream(*_args: object, **_kwargs: object) -> Iterator[ModelResponse]:
        try:
            yield ModelResponse(content="first")
            yield ModelResponse(content="second")
        finally:
            finalized.append(True)

    vars(model)["invoke_stream"] = fake_invoke_stream
    install_claude_stream_retry_hook(model)

    stream = model.invoke_stream([], object())
    assert next(stream).content == "first"
    stream.close()

    assert finalized == [True]


@pytest.mark.asyncio
async def test_early_aclose_closes_underlying_async_stream() -> None:
    """Aclosing the wrapper mid-stream finalizes the in-flight request too."""
    model = Claude(id="claude-sonnet-5")
    finalized: list[bool] = []

    async def fake_ainvoke_stream(*_args: object, **_kwargs: object) -> AsyncIterator[ModelResponse]:
        try:
            yield ModelResponse(content="first")
            yield ModelResponse(content="second")
        finally:
            finalized.append(True)

    vars(model)["ainvoke_stream"] = fake_ainvoke_stream
    install_claude_stream_retry_hook(model)

    stream = model.ainvoke_stream([], object())
    assert (await anext(stream)).content == "first"
    await stream.aclose()

    assert finalized == [True]


def test_install_skips_non_claude_models() -> None:
    """Non-Claude models are left untouched."""
    sentinel = object()
    install_claude_stream_retry_hook(sentinel)
