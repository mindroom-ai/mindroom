"""Tests for learned model media capability fallback policy."""

from __future__ import annotations

from unittest.mock import MagicMock

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


def test_audio_unsupported_error_records_audio_only() -> None:
    """Audio unsupported errors should disable only audio for the route."""
    reset_model_media_capability_cache()
    media = _media_inputs()
    route = _route()

    decision = retry_media_inputs_after_failure(
        route,
        RuntimeError("audio input is not supported - hint: you may need to provide the mmproj"),
        media,
    )

    assert decision.should_retry is True
    assert decision.removed_kinds == frozenset({"audio"})
    assert decision.media_inputs.audio == ()
    assert decision.media_inputs.images == media.images
    assert decision.media_inputs.files == media.files
    assert decision.media_inputs.videos == media.videos

    filtered = filter_media_inputs_for_route(route, media)
    assert filtered.removed_kinds == frozenset({"audio"})
    assert filtered.media_inputs.audio == ()
    assert filtered.media_inputs.images == media.images


def test_image_remains_enabled_when_only_audio_failed() -> None:
    """Negative cache should track kinds independently."""
    reset_model_media_capability_cache()
    media = _media_inputs()
    route = _route()

    retry_media_inputs_after_failure(
        route,
        "Error code: 400 - at most 0 audio(s) may be provided",
        media,
    )

    filtered = filter_media_inputs_for_route(route, media)
    assert filtered.media_inputs.audio == ()
    assert filtered.media_inputs.images == media.images


def test_different_base_url_does_not_inherit_negative_cache() -> None:
    """Effective route should include endpoint, not just provider/model."""
    reset_model_media_capability_cache()
    media = _media_inputs()
    first_route = _route(base_url="http://localhost:9292/v1")
    second_route = _route(base_url="http://localhost:9293/v1")

    retry_media_inputs_after_failure(
        first_route,
        "audio input is not supported",
        media,
    )

    filtered = filter_media_inputs_for_route(second_route, media)
    assert filtered.removed_kinds == frozenset()
    assert filtered.media_inputs.audio == media.audio


def test_generic_errors_do_not_update_cache() -> None:
    """Transient or unrelated failures should not teach media capability."""
    reset_model_media_capability_cache()
    media = _media_inputs()
    route = _route()

    decision = retry_media_inputs_after_failure(route, "Rate limit exceeded", media)

    assert decision.should_retry is False
    filtered = filter_media_inputs_for_route(route, media)
    assert filtered.media_inputs == media


def test_generic_media_error_retries_without_caching() -> None:
    """Ambiguous media errors should preserve old drop-all retry without teaching cache."""
    reset_model_media_capability_cache()
    media = _media_inputs()
    route = _route()

    decision = retry_media_inputs_after_failure(route, "inline media input is not supported", media)

    assert decision.should_retry is True
    assert decision.removed_kinds == frozenset({"audio", "image", "file", "video"})
    assert decision.media_inputs == MediaInputs()

    filtered = filter_media_inputs_for_route(route, media)
    assert filtered.media_inputs == media


def test_openai_invalid_audio_format_error_retries_without_caching() -> None:
    """OpenAI audio format validation errors should drop audio for the retry only."""
    reset_model_media_capability_cache()
    media = _media_inputs()
    route = _route()
    error = (
        "litellm.BadRequestError: OpenAIException - Invalid value: 'bin'. "
        "Supported values are: 'wav' and 'mp3'.. Received Model Group=gpt-5.5 "
        "messages[3].content[1].input_audio.format"
    )

    decision = retry_media_inputs_after_failure(route, error, media)

    assert decision.should_retry is True
    assert decision.removed_kinds == frozenset({"audio"})
    assert decision.media_inputs.audio == ()
    assert decision.media_inputs.images == media.images
    assert decision.media_inputs.files == media.files
    assert decision.media_inputs.videos == media.videos

    filtered = filter_media_inputs_for_route(route, media)
    assert filtered.media_inputs == media


def test_context_media_kinds_enable_retry_without_current_turn_media() -> None:
    """Media pinned to history messages should still trigger retry and teach the cache."""
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
    assert unsupported_media_kinds_for_route(route) == frozenset({"image"})
    reset_model_media_capability_cache()


def test_unsupported_media_kinds_for_route_defaults_empty() -> None:
    """Unknown and None routes report no learned-unsupported kinds."""
    reset_model_media_capability_cache()
    assert unsupported_media_kinds_for_route(None) == frozenset()
    assert unsupported_media_kinds_for_route(_route()) == frozenset()


def test_cache_can_be_reset() -> None:
    """Tests need explicit access to clear process-local learned state."""
    media = _media_inputs()
    route = _route()

    retry_media_inputs_after_failure(route, "image input is not supported", media)
    assert filter_media_inputs_for_route(route, media).media_inputs.images == ()

    reset_model_media_capability_cache()

    assert filter_media_inputs_for_route(route, media).media_inputs.images == media.images


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
