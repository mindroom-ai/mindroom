"""Provider-boundary recovery for rejected inline media."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

import pytest
from agno.exceptions import ModelProviderError, ModelRateLimitError
from agno.media import Audio, File, Image, Video
from agno.models.message import Message
from agno.models.response import ModelResponse

from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.error_handling import MODEL_SAFEGUARD_REFUSAL_MESSAGE, ModelSafeguardRefusalError
from mindroom.model_loading import get_model_instance
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from agno.models.base import Model


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


def _messages_from_call(args: tuple[object, ...], kwargs: dict[str, object]) -> list[Message]:
    candidate = kwargs.get("messages") if "messages" in kwargs else args[0]
    assert isinstance(candidate, list)
    assert all(isinstance(message, Message) for message in candidate)
    return cast("list[Message]", candidate)


def _provider_error(status_code: int = 400) -> ModelProviderError:
    return ModelProviderError(message="inline media is unsupported", status_code=status_code)


def _media_message() -> Message:
    return Message(
        role="user",
        content='Please inspect this.\n[attachments: att_123 (image, "diagram.png")]',
        audio=[Audio(content=b"audio", mime_type="audio/ogg")],
        images=[Image(content=b"image")],
        files=[File(content=b"file")],
        videos=[Video(content=b"video")],
    )


def _load(model: _FakeModel, tmp_path: Path) -> _FakeModel:
    config = bind_runtime_paths(
        Config(
            models={
                "default": ModelConfig(provider="synthetic", id="local-test-model"),
            },
        ),
        test_runtime_paths(tmp_path),
    )
    with patch("mindroom.model_loading._create_model_for_provider", return_value=cast("Model", model)):
        loaded = get_model_instance(config, runtime_paths_for(config))
    return cast("_FakeModel", loaded)


async def _consume_into(chunks: list[ModelResponse], stream: AsyncIterator[ModelResponse]) -> None:
    async for chunk in stream:
        chunks.append(chunk)  # noqa: PERF401 - Preserve chunks emitted before the failure.


@pytest.mark.asyncio
async def test_loaded_model_retries_once_without_inline_media_and_preserves_attachment_id(tmp_path: Path) -> None:
    """A typed provider rejection retries only the final request with attachment text intact."""
    original = _media_message()
    original_snapshot = original.model_copy(deep=True)
    model = _load(
        _FakeModel(blocking_outcomes=[_provider_error(), ModelResponse(content="recovered")]),
        tmp_path,
    )

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
    assert "Inline media unavailable for this model" in str(retry_messages[-1].content)
    assert original == original_snapshot


@pytest.mark.asyncio
async def test_loaded_model_retries_a_stream_only_before_output(tmp_path: Path) -> None:
    """A pre-output stream rejection retries once and closes both provider streams."""
    original = _media_message()
    model = _load(
        _FakeModel(
            streaming_outcomes=[
                _provider_error(),
                [ModelResponse(content="recovered")],
            ],
        ),
        tmp_path,
    )

    chunks = [chunk async for chunk in model.ainvoke_stream([original])]

    assert [chunk.content for chunk in chunks] == ["recovered"]
    assert len(model.streaming_calls) == 2
    assert model.streaming_calls[1][0].images is None
    assert "att_123" in str(model.streaming_calls[1][0].content)
    assert model.closed_streams == 2
    assert original.images


@pytest.mark.asyncio
async def test_loaded_model_does_not_replay_a_stream_after_output(tmp_path: Path) -> None:
    """Once output escapes, a later provider failure propagates without replay."""
    error = _provider_error()
    model = _load(
        _FakeModel(
            streaming_outcomes=[
                [ModelResponse(content="prefix"), error],
                [ModelResponse(content="must not run")],
            ],
        ),
        tmp_path,
    )

    chunks: list[ModelResponse] = []
    with pytest.raises(ModelProviderError):
        await _consume_into(chunks, model.ainvoke_stream(messages=[_media_message()]))

    assert [chunk.content for chunk in chunks] == ["prefix"]
    assert len(model.streaming_calls) == 1
    assert model.closed_streams == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        ModelProviderError(message="unauthorized", status_code=401),
        ModelProviderError(message="forbidden", status_code=403),
        ModelRateLimitError(message="rate limited", status_code=429),
        ModelProviderError(message="server unavailable", status_code=503),
        ModelSafeguardRefusalError(message=MODEL_SAFEGUARD_REFUSAL_MESSAGE, status_code=400),
        RuntimeError("not reported by a provider"),
    ],
)
async def test_loaded_model_leaves_non_media_provider_failures_to_existing_policy(
    tmp_path: Path,
    error: Exception,
) -> None:
    """Authentication, transient, safeguard, and untyped failures keep their existing owners."""
    model = _load(
        _FakeModel(blocking_outcomes=[error, ModelResponse(content="must not run")]),
        tmp_path,
    )

    with pytest.raises(type(error)):
        await model.ainvoke(messages=[_media_message()])

    assert len(model.blocking_calls) == 1
