"""Tests for the provider-boundary inline-media fallback."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

import pytest
from agno.exceptions import ModelProviderError, ModelRateLimitError
from agno.media import Audio, File, Image, Video
from agno.models.message import Message
from agno.models.response import ModelResponse

from mindroom.config.models import DebugConfig
from mindroom.error_handling import MODEL_SAFEGUARD_REFUSAL_MESSAGE, ModelSafeguardRefusalError
from mindroom.llm_request_logging import install_llm_request_logging
from mindroom.media_fallback import install_model_media_fallback

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from agno.models.base import Model


_FALLBACK_PROMPT = "Use available attachment IDs and tools to inspect files instead."


@dataclass
class _FakeModel:
    id: str = "text-only"
    provider: str = "test"
    blocking_outcomes: list[ModelResponse | Exception] = field(default_factory=list)
    streaming_outcomes: list[list[ModelResponse | Exception] | Exception] = field(default_factory=list)
    blocking_calls: list[list[Message]] = field(default_factory=list)
    streaming_calls: list[list[Message]] = field(default_factory=list)
    closed_streams: int = 0

    async def ainvoke(self, *args: object, **kwargs: object) -> ModelResponse:
        self.blocking_calls.append(list(_messages_from_call(args, kwargs)))
        outcome = self.blocking_outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def ainvoke_stream(self, *args: object, **kwargs: object) -> AsyncIterator[ModelResponse]:
        self.streaming_calls.append(list(_messages_from_call(args, kwargs)))
        outcome = self.streaming_outcomes.pop(0)
        try:
            if isinstance(outcome, Exception):
                raise outcome
            for item in outcome:
                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            self.closed_streams += 1


@dataclass
class _StreamDelegatesToBlockingModel(_FakeModel):
    async def ainvoke_stream(self, *args: object, **kwargs: object) -> AsyncIterator[ModelResponse]:
        self.streaming_calls.append(list(_messages_from_call(args, kwargs)))
        try:
            yield await self.ainvoke(*args, **kwargs)
        finally:
            self.closed_streams += 1


def _messages_from_call(args: tuple[object, ...], kwargs: dict[str, object]) -> list[Message]:
    candidate = kwargs.get("messages") if "messages" in kwargs else args[0]
    assert isinstance(candidate, list)
    assert all(isinstance(message, Message) for message in candidate)
    return cast("list[Message]", candidate)


def _provider_error(status_code: int = 400, message: str = "inline media is unsupported") -> ModelProviderError:
    return ModelProviderError(message=message, status_code=status_code)


def _media_message() -> Message:
    return Message(
        role="user",
        content='Please inspect this.\n[attachments: att_123 (image, "diagram.png")]',
        audio=[Audio(content=b"audio", mime_type="audio/ogg")],
        images=[Image(content=b"image")],
        files=[File(content=b"file")],
        videos=[Video(content=b"video")],
    )


def _install(model: _FakeModel) -> None:
    install_model_media_fallback(cast("Model", model), fallback_prompt=_FALLBACK_PROMPT)


async def _consume_into(chunks: list[ModelResponse], stream: AsyncIterator[ModelResponse]) -> None:
    async for chunk in stream:
        chunks.append(chunk)  # noqa: PERF401 - Preserve chunks yielded before a failure.


@pytest.mark.asyncio
async def test_blocking_retries_once_without_inline_media_and_preserves_attachment_id() -> None:
    """A provider rejection gets one media-free retry with attachment text intact."""
    original = _media_message()
    model = _FakeModel(blocking_outcomes=[_provider_error(), ModelResponse(content="recovered")])
    _install(model)

    response = await model.ainvoke(messages=[original])

    assert response.content == "recovered"
    assert len(model.blocking_calls) == 2
    assert model.blocking_calls[0] == [original]
    retry_messages = model.blocking_calls[1]
    assert "att_123" in str(retry_messages[0].content)
    assert retry_messages[0].audio is None
    assert retry_messages[0].images is None
    assert retry_messages[0].files is None
    assert retry_messages[0].videos is None
    assert retry_messages[-1].temporary is True
    assert "[Inline media unavailable for this model]" in str(retry_messages[-1].content)
    assert _FALLBACK_PROMPT in str(retry_messages[-1].content)
    assert original.audio
    assert original.images
    assert original.files
    assert original.videos


@pytest.mark.asyncio
async def test_blocking_retry_does_not_mutate_the_callers_list_or_messages() -> None:
    """Building the retry request leaves caller-owned messages unchanged."""
    original = _media_message()
    messages = [Message(role="system", content="system"), original]
    original_snapshot = [message.model_copy(deep=True) for message in messages]
    model = _FakeModel(blocking_outcomes=[_provider_error(), ModelResponse(content="ok")])
    _install(model)

    await model.ainvoke(messages=messages)

    assert messages == original_snapshot
    assert messages[0] is not model.blocking_calls[1][0]
    assert messages[1] is not model.blocking_calls[1][1]
    assert len(messages) == 2


@pytest.mark.asyncio
async def test_blocking_supports_positional_messages() -> None:
    """The wrapper handles the model API's positional messages argument."""
    model = _FakeModel(blocking_outcomes=[_provider_error(), ModelResponse(content="ok")])
    _install(model)

    response = await model.ainvoke([_media_message()])

    assert response.content == "ok"
    assert len(model.blocking_calls) == 2
    assert model.blocking_calls[1][0].images is None


@pytest.mark.asyncio
async def test_blocking_propagates_the_retry_failure_after_two_calls() -> None:
    """A failed media-free attempt escapes without a third call."""
    retry_error = _provider_error(message="still rejected")
    model = _FakeModel(blocking_outcomes=[_provider_error(), retry_error])
    _install(model)

    with pytest.raises(ModelProviderError) as raised:
        await model.ainvoke(messages=[_media_message()])

    assert raised.value is retry_error
    assert len(model.blocking_calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        _provider_error(401, "bad key"),
        _provider_error(403, "forbidden"),
        _provider_error(408, "timeout"),
        _provider_error(429, "rate limited"),
        _provider_error(500, "server error"),
        ModelRateLimitError(message="rate limited", status_code=429),
        ModelSafeguardRefusalError(message=MODEL_SAFEGUARD_REFUSAL_MESSAGE),
        RuntimeError("application bug"),
    ],
)
async def test_blocking_does_not_retry_ineligible_failures(error: Exception) -> None:
    """Failures unrelated to supported client rejections remain untouched."""
    model = _FakeModel(blocking_outcomes=[error])
    _install(model)

    with pytest.raises(type(error)) as raised:
        await model.ainvoke(messages=[_media_message()])

    assert raised.value is error
    assert len(model.blocking_calls) == 1


@pytest.mark.asyncio
async def test_blocking_does_not_retry_when_the_request_has_no_media() -> None:
    """A text-only request never enters media fallback."""
    error = _provider_error()
    model = _FakeModel(blocking_outcomes=[error])
    _install(model)

    with pytest.raises(ModelProviderError):
        await model.ainvoke(messages=[Message(role="user", content="text only")])

    assert len(model.blocking_calls) == 1


@pytest.mark.asyncio
async def test_streaming_retries_when_the_first_stream_fails_before_a_chunk() -> None:
    """A stream may restart media-free before any output escapes."""
    model = _FakeModel(
        streaming_outcomes=[
            _provider_error(),
            [ModelResponse(content="recovered"), ModelResponse(content=" response")],
        ],
    )
    _install(model)

    chunks = [chunk async for chunk in model.ainvoke_stream(messages=[_media_message()])]

    assert [chunk.content for chunk in chunks] == ["recovered", " response"]
    assert len(model.streaming_calls) == 2
    assert model.streaming_calls[1][0].images is None
    assert model.closed_streams == 2


@pytest.mark.asyncio
async def test_streaming_does_not_retry_after_any_chunk_escaped() -> None:
    """A visible partial stream is never replayed from the beginning."""
    error = _provider_error()
    model = _FakeModel(streaming_outcomes=[[ModelResponse(content="prefix"), error]])
    _install(model)
    chunks: list[ModelResponse] = []

    with pytest.raises(ModelProviderError) as raised:
        await _consume_into(chunks, model.ainvoke_stream(messages=[_media_message()]))

    assert raised.value is error
    assert [chunk.content for chunk in chunks] == ["prefix"]
    assert len(model.streaming_calls) == 1
    assert model.closed_streams == 1


@pytest.mark.asyncio
async def test_streaming_propagates_the_retry_failure() -> None:
    """A failed media-free stream escapes after exactly two streams."""
    retry_error = _provider_error(message="still rejected")
    model = _FakeModel(streaming_outcomes=[_provider_error(), retry_error])
    _install(model)

    with pytest.raises(ModelProviderError) as raised:
        _ = [chunk async for chunk in model.ainvoke_stream(messages=[_media_message()])]

    assert raised.value is retry_error
    assert len(model.streaming_calls) == 2
    assert model.closed_streams == 2


@pytest.mark.asyncio
async def test_streaming_nested_model_call_has_only_one_retry_owner() -> None:
    """Nested adapter methods share one retry owner for the provider request."""
    initial_error = _provider_error(message="initial rejection")
    retry_error = _provider_error(message="retry rejection")
    unexpected_error = _provider_error(message="unexpected third attempt")
    model = _StreamDelegatesToBlockingModel(
        blocking_outcomes=[initial_error, retry_error, unexpected_error],
    )
    _install(model)

    with pytest.raises(ModelProviderError) as raised:
        _ = [chunk async for chunk in model.ainvoke_stream(messages=[_media_message()])]

    assert raised.value is retry_error
    assert len(model.blocking_calls) == 2
    assert model.closed_streams == 2


@pytest.mark.asyncio
async def test_installation_is_idempotent() -> None:
    """Installing the wrapper twice does not stack fallback attempts."""
    model = _FakeModel(blocking_outcomes=[_provider_error(), ModelResponse(content="ok")])
    _install(model)
    _install(model)

    await model.ainvoke(messages=[_media_message()])

    assert len(model.blocking_calls) == 2


@pytest.mark.asyncio
async def test_deep_copied_model_retries_on_the_copy(tmp_path: Path) -> None:
    """Stacked wrappers remain bound to a copied model instance."""
    original = _FakeModel()
    install_llm_request_logging(
        cast("Model", original),
        agent_name="test",
        debug_config=DebugConfig(),
        default_log_dir=tmp_path,
    )
    _install(original)
    copied = deepcopy(original)
    copied.blocking_outcomes = [_provider_error(), ModelResponse(content="copy")]

    response = await copied.ainvoke(messages=[_media_message()])

    assert response.content == "copy"
    assert len(copied.blocking_calls) == 2
    assert original.blocking_calls == []
