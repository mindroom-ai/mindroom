"""Tests for learned model media capability fallback policy."""

from __future__ import annotations

from unittest.mock import MagicMock

from agno.models.openai import OpenAIChat

from mindroom.media_fallback import (
    ModelMediaRoute,
    build_model_media_route,
    filter_media_inputs_for_route,
    reset_model_media_capability_cache,
    retry_media_inputs_after_failure,
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
