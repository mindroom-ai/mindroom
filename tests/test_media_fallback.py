"""Tests for learned model media capability fallback policy."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from agno.exceptions import ContextWindowExceededError, ModelProviderError
from agno.media import Audio, Image
from agno.models.message import Message
from agno.models.openai import OpenAIChat

from mindroom import ai_runtime
from mindroom.media_fallback import (
    ModelMediaRoute,
    build_model_media_route,
    filter_media_inputs_for_route,
    reset_model_media_capability_cache,
    retry_media_inputs_after_failure,
    unsupported_media_kinds_for_route,
)
from mindroom.media_inputs import MediaInputs


def test_unknown_model_route_sends_all_media() -> None:
    """Unknown route should optimistically keep every supplied media kind."""
    reset_model_media_capability_cache()
    media = _media_inputs()

    filtered = filter_media_inputs_for_route(_route(), media)

    assert filtered.media_inputs == media
    assert filtered.removed_kinds == frozenset()


def test_model_route_includes_provider_model_and_base_url() -> None:
    """Route construction should key learned support by concrete model endpoint."""
    model = OpenAIChat(id="qwen-local", base_url="http://localhost:9292/v1/")

    assert build_model_media_route(model) == ModelMediaRoute(
        provider="openai",
        model_id="qwen-local",
        base_url="http://localhost:9292/v1",
    )


@pytest.mark.parametrize(
    "error",
    [
        # Z.ai code 1214 as it reaches the streamed run-error path: bare message,
        # no exception object, no status code, no "Error code: 400" marker.
        "messages[30].content[0].type type error",
        "Error code: 400 - messages.content.type is invalid, allowed values: ['text']",
        "audio input is not supported - hint: you may need to provide the mmproj",
        "Rate limit exceeded",
        "Error code: 400 - invalid api key provided",
        ModelProviderError(message="Some brand new provider wording about content", status_code=400),
    ],
)
def test_any_failure_retries_without_media_and_teaches_on_success(error: Exception | str) -> None:
    """No error wording decides the retry: every failure drops all media once.

    The route capability cache learns the dropped kinds only when the retry
    actually succeeds, which never happens for failures unrelated to media
    (auth, rate limits, outages) because their retry fails identically.
    """
    reset_model_media_capability_cache()
    media = _media_inputs()
    route = _route()

    decision = retry_media_inputs_after_failure(route, error, media)

    assert decision.should_retry is True
    assert decision.removed_kinds == frozenset({"audio", "image", "file", "video"})
    assert decision.media_inputs == MediaInputs()
    assert decision.teach_route_on_success == route
    # Nothing is taught until the without-media retry actually succeeds.
    assert filter_media_inputs_for_route(route, media).media_inputs == media

    decision.record_retry_success()

    assert unsupported_media_kinds_for_route(route) == frozenset({"audio", "image", "file", "video"})
    filtered = filter_media_inputs_for_route(route, media)
    assert filtered.removed_kinds == frozenset({"audio", "image", "file", "video"})
    assert filtered.media_inputs == MediaInputs()
    reset_model_media_capability_cache()


def test_no_media_present_never_retries() -> None:
    """Media-shaped errors without any media sent are not retried."""
    decision = retry_media_inputs_after_failure(_route(), "audio input is not supported", MediaInputs())

    assert decision.should_retry is False
    assert decision.removed_kinds == frozenset()


@pytest.mark.parametrize(
    "error",
    [
        ContextWindowExceededError(message="prompt is too long: 250000 tokens > 200000 maximum"),
        "Error code: 400 - maximum context length is 128000 tokens",
        "Request too large",
        "Request Entity Too Large",
        ModelProviderError(message="Request Entity Too Large", status_code=413),
        # Transient failures can pass on the retry because the blip passed, so
        # a lucky retry success must not disable media for the route.
        ModelProviderError(message="upstream connect error", status_code=502),
        ModelProviderError(message="model overloaded", status_code=503),
        ModelProviderError(message="Too Many Requests", status_code=429),
    ],
)
def test_size_context_and_transient_failures_retry_but_never_teach(error: Exception | str) -> None:
    """Oversized requests and transient failures must not teach capability on retry success."""
    reset_model_media_capability_cache()
    media = _media_inputs()
    route = _route()

    decision = retry_media_inputs_after_failure(route, error, media)

    assert decision.should_retry is True
    assert decision.teach_route_on_success is None

    decision.record_retry_success()

    assert unsupported_media_kinds_for_route(route) == frozenset()


def test_different_base_url_does_not_inherit_negative_cache() -> None:
    """Effective route should include endpoint, not just provider/model."""
    reset_model_media_capability_cache()
    media = _media_inputs()
    first_route = _route(base_url="http://localhost:9292/v1")
    second_route = _route(base_url="http://localhost:9293/v1")

    retry_media_inputs_after_failure(first_route, "audio input is not supported", media).record_retry_success()

    filtered = filter_media_inputs_for_route(second_route, media)
    assert filtered.removed_kinds == frozenset()
    assert filtered.media_inputs == media
    reset_model_media_capability_cache()


def test_context_media_kinds_enable_retry_without_current_turn_media() -> None:
    """Media pinned to history messages should still trigger the retry and teach on success."""
    reset_model_media_capability_cache()
    route = _route()

    decision = retry_media_inputs_after_failure(
        route,
        "image input is not supported",
        MediaInputs(),
        extra_present_kinds=frozenset({"image"}),
    )

    assert decision.should_retry is True
    assert decision.removed_kinds == frozenset({"image"})

    decision.record_retry_success()

    assert unsupported_media_kinds_for_route(route) == frozenset({"image"})
    reset_model_media_capability_cache()


def test_unsupported_media_kinds_for_route_defaults_empty() -> None:
    """Unknown and None routes report no learned-unsupported kinds."""
    reset_model_media_capability_cache()
    assert unsupported_media_kinds_for_route(None) == frozenset()
    assert unsupported_media_kinds_for_route(_route()) == frozenset()


def test_cache_can_be_reset() -> None:
    """Tests need explicit access to clear process-local learned state."""
    reset_model_media_capability_cache()
    media = _media_inputs()
    route = _route()

    retry_media_inputs_after_failure(route, "image input is not supported", media).record_retry_success()
    assert filter_media_inputs_for_route(route, media).media_inputs == MediaInputs()

    reset_model_media_capability_cache()

    assert filter_media_inputs_for_route(route, media).media_inputs == media


def _route(base_url: str = "http://localhost:9292/v1") -> ModelMediaRoute:
    return ModelMediaRoute(provider="openai", model_id="qwen-local", base_url=base_url)


def _media_inputs() -> MediaInputs:
    return MediaInputs(
        audio=(MagicMock(name="audio"),),
        images=(MagicMock(name="image"),),
        files=(MagicMock(name="file"),),
        videos=(MagicMock(name="video"),),
    )


def test_run_input_media_helpers_cover_pinned_history_media() -> None:
    """Run-input helpers report, collect, and strip media pinned to history messages."""
    image = Image(content=b"\x89PNG\r\n\x1a\npayload")
    audio = Audio(content=b"audio-bytes", mime_type="audio/ogg")
    history = Message(role="user", content="earlier", images=[image], audio=[audio])
    current = Message(role="user", content="now")
    run_input = [history, current]

    collected = ai_runtime.media_inputs_from_run_input(run_input)
    assert collected.kinds() == frozenset({"image", "audio"})
    assert ai_runtime.media_inputs_from_run_input("plain prompt").kinds() == frozenset()
    assert list(collected.images) == [image]
    assert list(collected.audio) == [audio]

    stripped = ai_runtime.append_inline_media_fallback_to_run_input(
        run_input,
        fallback_prompt="Use attachment tools instead.",
        removed_kinds=frozenset({"image"}),
    )
    assert stripped[0].images is None
    assert [item.content for item in (stripped[0].audio or [])] == [audio.content]
    assert "[Inline media unavailable for this model]" in str(stripped[-1].content)
    # The original run input stays untouched for later retries.
    assert history.images == [image]


def test_media_inputs_merge_concatenates_preserving_order() -> None:
    """Merging media inputs keeps left-then-right (chronological) order per kind."""
    history_image = Image(content=b"\x89PNG\r\n\x1a\nhistory")
    current_image = Image(content=b"\x89PNG\r\n\x1a\ncurrent")
    history = MediaInputs(images=(history_image,))
    current = MediaInputs(images=(current_image,), audio=(Audio(content=b"audio-bytes", mime_type="audio/ogg"),))

    merged = history.merge(current)

    assert list(merged.images) == [history_image, current_image]
    assert merged.kinds() == frozenset({"image", "audio"})
    assert MediaInputs().merge(current) == current
    assert history.merge(MediaInputs()) == history
