"""Provider-boundary recovery for rejected inline media."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

import pytest
from agno.exceptions import ModelProviderError, ModelRateLimitError
from agno.media import Audio, File, Image, Video
from agno.models.anthropic import Claude
from agno.models.message import Message
from agno.models.response import ModelResponse

from mindroom import claude_stream_retry
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.error_handling import MODEL_SAFEGUARD_REFUSAL_MESSAGE, ModelSafeguardRefusalError
from mindroom.model_loading import get_model_instance
from mindroom.provider_media_fallback import reset_model_media_capability_cache
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from agno.models.base import Model


@dataclass
class _FakeModel:
    id: str = "text-only"
    provider: str = "test"
    base_url: str | None = None
    client_params: dict[str, object] | None = None
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


def _load[LoadedModel](model: LoadedModel, tmp_path: Path) -> LoadedModel:
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
    return cast("LoadedModel", loaded)


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
async def test_loaded_model_omits_media_kinds_learned_unsupported_for_its_route(tmp_path: Path) -> None:
    """A successful stripped retry prevents another probe for the same model route."""
    model = _load(
        _FakeModel(
            blocking_outcomes=[
                _provider_error(),
                ModelResponse(content="first recovered"),
                ModelResponse(content="second recovered"),
            ],
        ),
        tmp_path,
    )

    first = await model.ainvoke(messages=[_media_message()])
    second = await model.ainvoke(messages=[_media_message()])

    assert first.content == "first recovered"
    assert second.content == "second recovered"
    assert len(model.blocking_calls) == 3
    learned_call = model.blocking_calls[2]
    assert learned_call[0].audio is None
    assert learned_call[0].images is None
    assert learned_call[0].files is None
    assert learned_call[0].videos is None
    assert "att_123" in str(learned_call[0].content)
    assert learned_call[-1].temporary is True


@pytest.mark.asyncio
async def test_loaded_model_learns_only_the_media_kinds_present_in_the_failed_request(tmp_path: Path) -> None:
    """Learning that audio is unsupported does not suppress an image sent later on the same route."""
    audio_only = Message(
        role="user",
        content='Listen to att_audio.\n[attachments: att_audio (audio, "clip.ogg")]',
        audio=[Audio(content=b"audio", mime_type="audio/ogg")],
    )
    model = _load(
        _FakeModel(
            blocking_outcomes=[
                _provider_error(),
                ModelResponse(content="audio recovered"),
                ModelResponse(content="image accepted"),
            ],
        ),
        tmp_path,
    )

    await model.ainvoke(messages=[audio_only])
    await model.ainvoke(messages=[_media_message()])

    learned_call = model.blocking_calls[2]
    assert learned_call[0].audio is None
    assert learned_call[0].images
    assert learned_call[0].files
    assert learned_call[0].videos


@pytest.mark.asyncio
async def test_loaded_model_keeps_capability_learning_isolated_by_endpoint(tmp_path: Path) -> None:
    """The same provider and model ID at another endpoint gets its own first probe."""
    first = _load(
        _FakeModel(
            base_url="http://localhost:9292/v1/",
            blocking_outcomes=[_provider_error(), ModelResponse(content="recovered")],
        ),
        tmp_path,
    )
    second = _load(
        _FakeModel(
            base_url="http://localhost:9293/v1",
            blocking_outcomes=[ModelResponse(content="accepted")],
        ),
        tmp_path,
    )

    await first.ainvoke(messages=[_media_message()])
    await second.ainvoke(messages=[_media_message()])

    assert first.blocking_calls[1][0].images is None
    assert second.blocking_calls[0][0].images


@pytest.mark.asyncio
async def test_failed_media_free_retry_does_not_teach_the_route(tmp_path: Path) -> None:
    """A negative capability is remembered only after the stripped request succeeds."""
    first = _load(
        _FakeModel(blocking_outcomes=[_provider_error(), _provider_error()]),
        tmp_path,
    )
    second = _load(
        _FakeModel(blocking_outcomes=[ModelResponse(content="accepted")]),
        tmp_path,
    )

    with pytest.raises(ModelProviderError):
        await first.ainvoke(messages=[_media_message()])
    await second.ainvoke(messages=[_media_message()])

    assert second.blocking_calls[0][0].images


@pytest.mark.asyncio
async def test_model_media_capability_cache_can_be_reset(tmp_path: Path) -> None:
    """Resetting the process cache makes the same route probe inline media again."""
    first = _load(
        _FakeModel(blocking_outcomes=[_provider_error(), ModelResponse(content="recovered")]),
        tmp_path,
    )
    second = _load(
        _FakeModel(blocking_outcomes=[ModelResponse(content="accepted")]),
        tmp_path,
    )

    await first.ainvoke(messages=[_media_message()])
    reset_model_media_capability_cache()
    await second.ainvoke(messages=[_media_message()])

    assert second.blocking_calls[0][0].images


@pytest.mark.asyncio
async def test_loaded_model_retries_a_stream_only_before_output(tmp_path: Path) -> None:
    """A pre-output stream rejection retries once and closes both provider streams."""
    original = _media_message()
    model = _load(
        _FakeModel(
            streaming_outcomes=[
                _provider_error(),
                [ModelResponse(content="recovered")],
                [ModelResponse(content="cached")],
            ],
        ),
        tmp_path,
    )

    chunks = [chunk async for chunk in model.ainvoke_stream([original])]
    cached_chunks = [chunk async for chunk in model.ainvoke_stream([_media_message()])]

    assert [chunk.content for chunk in chunks] == ["recovered"]
    assert [chunk.content for chunk in cached_chunks] == ["cached"]
    assert len(model.streaming_calls) == 3
    assert model.streaming_calls[1][0].images is None
    assert model.streaming_calls[2][0].images is None
    assert "att_123" in str(model.streaming_calls[1][0].content)
    assert model.closed_streams == 3
    assert original.images


@pytest.mark.asyncio
async def test_loaded_claude_keeps_media_removed_during_transient_stream_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient retry nested inside fallback must reuse the media-free request."""
    attempts: list[list[ModelResponse | Exception]] = [
        [_provider_error()],
        [_provider_error(503)],
        [ModelResponse(content="recovered")],
    ]
    provider_calls: list[list[Message]] = []
    model = Claude(id="claude-sonnet-5")

    async def fake_ainvoke_stream(*args: object, **kwargs: object) -> AsyncIterator[ModelResponse]:
        provider_calls.append(list(_messages_from_call(args, kwargs)))
        for item in attempts[len(provider_calls) - 1]:
            if isinstance(item, Exception):
                raise item
            yield item

    vars(model)["ainvoke_stream"] = fake_ainvoke_stream
    monkeypatch.setattr(claude_stream_retry, "_RETRY_BASE_DELAY_SECONDS", 0.0)
    loaded = _load(model, tmp_path)

    chunks = [
        chunk
        async for chunk in loaded.ainvoke_stream(
            messages=[_media_message()],
            assistant_message=Message(role="assistant"),
        )
    ]

    assert [chunk.content for chunk in chunks] == ["recovered"]
    assert len(provider_calls) == 3
    assert provider_calls[0][0].images
    assert provider_calls[1][0].images is None
    assert provider_calls[2][0].images is None


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
        ModelRateLimitError(message="rate limited", status_code=429),
        ModelProviderError(message="payload too large", status_code=413),
        ModelProviderError(message="server unavailable", status_code=503),
    ],
)
async def test_successful_retry_after_non_capability_failure_does_not_teach_route(
    tmp_path: Path,
    error: Exception,
) -> None:
    """Size and transient failures retry once but cannot prove media is unsupported."""
    model = _load(
        _FakeModel(
            blocking_outcomes=[
                error,
                ModelResponse(content="recovered"),
                ModelResponse(content="next"),
            ],
        ),
        tmp_path,
    )

    await model.ainvoke(messages=[_media_message()])
    await model.ainvoke(messages=[_media_message()])

    assert len(model.blocking_calls) == 3
    assert model.blocking_calls[1][0].images is None
    assert model.blocking_calls[2][0].images


@pytest.mark.asyncio
async def test_untyped_failure_retries_once_and_teaches_after_success(tmp_path: Path) -> None:
    """Fallback does not depend on provider-specific error wording or exception type."""
    model = _load(
        _FakeModel(
            blocking_outcomes=[
                RuntimeError("unknown provider failure"),
                ModelResponse(content="recovered"),
                ModelResponse(content="next"),
            ],
        ),
        tmp_path,
    )

    await model.ainvoke(messages=[_media_message()])
    await model.ainvoke(messages=[_media_message()])

    assert len(model.blocking_calls) == 3
    assert model.blocking_calls[1][0].images is None
    assert model.blocking_calls[2][0].images is None


@pytest.mark.asyncio
async def test_safeguard_refusal_is_not_retried_without_media(tmp_path: Path) -> None:
    """A deterministic safeguard refusal must remain a refusal."""
    error = ModelSafeguardRefusalError(message=MODEL_SAFEGUARD_REFUSAL_MESSAGE, status_code=400)
    model = _load(
        _FakeModel(blocking_outcomes=[error, ModelResponse(content="must not run")]),
        tmp_path,
    )

    with pytest.raises(ModelSafeguardRefusalError):
        await model.ainvoke(messages=[_media_message()])

    assert len(model.blocking_calls) == 1
