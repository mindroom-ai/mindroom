"""Tests for learned model media capability fallback policy."""

from __future__ import annotations

from unittest.mock import MagicMock

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


def test_generic_errors_retry_but_never_teach() -> None:
    """Transient failures retry without media but must not teach capability, even on success."""
    reset_model_media_capability_cache()
    media = _media_inputs()
    route = _route()

    decision = retry_media_inputs_after_failure(route, "Rate limit exceeded", media)

    assert decision.should_retry is True
    assert decision.media_inputs == MediaInputs()
    assert decision.teach_route_on_success is None

    decision.record_retry_success()
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


def test_text_only_content_error_teaches_all_present_kinds() -> None:
    """Z.ai text-only models reject every non-text part; all present kinds are learned."""
    reset_model_media_capability_cache()
    media = _media_inputs()
    route = _route()

    decision = retry_media_inputs_after_failure(
        route,
        "Error code: 400 - messages.content.type is invalid, allowed values: ['text']",
        media,
    )

    assert decision.should_retry is True
    assert decision.removed_kinds == frozenset({"audio", "image", "file", "video"})
    assert decision.media_inputs == MediaInputs()

    filtered = filter_media_inputs_for_route(route, media)
    assert filtered.removed_kinds == frozenset({"audio", "image", "file", "video"})
    assert filtered.media_inputs == MediaInputs()
    reset_model_media_capability_cache()


def test_text_only_content_error_teaches_only_present_kinds() -> None:
    """Text-only errors must not disable media kinds that were never sent."""
    reset_model_media_capability_cache()
    media = MediaInputs(images=(MagicMock(name="image"),))
    route = _route()

    decision = retry_media_inputs_after_failure(
        route,
        "messages.content.type is invalid, allowed values: ['text']",
        media,
    )

    assert decision.should_retry is True
    assert decision.removed_kinds == frozenset({"image"})
    assert unsupported_media_kinds_for_route(route) == frozenset({"image"})
    reset_model_media_capability_cache()


def test_content_part_type_error_retries_via_generic_gate() -> None:
    """Z.ai bare content-part type errors carry the 400 marker the generic gate matches."""
    reset_model_media_capability_cache()
    media = _media_inputs()
    route = _route()

    decision = retry_media_inputs_after_failure(
        route,
        "Error code: 400 - messages[12].content[0].type type error",
        media,
    )

    assert decision.should_retry is True
    assert decision.removed_kinds == frozenset({"audio", "image", "file", "video"})
    assert decision.media_inputs == MediaInputs()

    filtered = filter_media_inputs_for_route(route, media)
    assert filtered.media_inputs == media
    reset_model_media_capability_cache()


def test_bare_content_part_type_error_from_streamed_run_retries() -> None:
    """Streamed RunErrorEvents carry only Z.ai's bare 1214 message, without the 400 marker.

    Agno converts the provider 400 into a RunErrorEvent whose content is just
    str(ModelProviderError) — the status code and "Error code: 400" prefix are
    lost — so the bare message shape must be recognized on its own.
    """
    reset_model_media_capability_cache()
    media = _media_inputs()
    route = _route()

    decision = retry_media_inputs_after_failure(
        route,
        "messages[30].content[0].type type error",
        media,
    )

    assert decision.should_retry is True
    assert decision.removed_kinds == frozenset({"audio", "image", "file", "video"})
    assert decision.media_inputs == MediaInputs()
    assert decision.teach_route_on_success == route
    # Nothing is taught until the without-media retry actually succeeds.
    assert filter_media_inputs_for_route(route, media).media_inputs == media

    decision.record_retry_success()
    assert unsupported_media_kinds_for_route(route) == frozenset({"audio", "image", "file", "video"})
    reset_model_media_capability_cache()


def test_invalid_request_status_retries_and_teaches_only_on_success() -> None:
    """Any 400-class provider exception retries without media; the cache learns on retry success."""
    reset_model_media_capability_cache()
    media = _media_inputs()
    route = _route()
    error = ModelProviderError(message="Some brand new provider wording about content", status_code=400)

    decision = retry_media_inputs_after_failure(route, error, media)

    assert decision.should_retry is True
    assert decision.removed_kinds == frozenset({"audio", "image", "file", "video"})
    assert decision.media_inputs == MediaInputs()
    assert filter_media_inputs_for_route(route, media).media_inputs == media

    decision.record_retry_success()

    assert unsupported_media_kinds_for_route(route) == frozenset({"audio", "image", "file", "video"})
    reset_model_media_capability_cache()


def test_invalid_request_text_marker_retries_and_teaches_only_on_success() -> None:
    """Errored-run text with a generic 400 marker retries even for unknown provider prose."""
    reset_model_media_capability_cache()
    media = _media_inputs()
    route = _route()

    decision = retry_media_inputs_after_failure(route, "Error code: 422 - unknown validation prose", media)

    assert decision.should_retry is True
    assert decision.removed_kinds == frozenset({"audio", "image", "file", "video"})
    assert filter_media_inputs_for_route(route, media).media_inputs == media

    decision.record_retry_success()

    assert unsupported_media_kinds_for_route(route) == frozenset({"audio", "image", "file", "video"})
    reset_model_media_capability_cache()


def test_context_window_error_retries_but_never_teaches() -> None:
    """Dropping media can shrink an overflowing prompt, so success must not teach capability."""
    reset_model_media_capability_cache()
    media = _media_inputs()
    route = _route()
    error = ContextWindowExceededError(message="prompt is too long: 250000 tokens > 200000 maximum")

    decision = retry_media_inputs_after_failure(route, error, media)

    assert decision.should_retry is True
    assert decision.teach_route_on_success is None

    decision.record_retry_success()

    assert unsupported_media_kinds_for_route(route) == frozenset()


def test_context_window_text_never_teaches() -> None:
    """Context-overflow vocabulary in errored-run text blocks teaching, not the retry."""
    reset_model_media_capability_cache()
    media = _media_inputs()
    route = _route()

    decision = retry_media_inputs_after_failure(
        route,
        "Error code: 400 - maximum context length is 128000 tokens",
        media,
    )

    assert decision.should_retry is True
    assert decision.teach_route_on_success is None


def test_payload_too_large_retries_but_never_teaches() -> None:
    """Oversized-payload rejections are about size, not media capability."""
    reset_model_media_capability_cache()
    media = _media_inputs()
    route = _route()
    error = ModelProviderError(message="Request Entity Too Large", status_code=413)

    decision = retry_media_inputs_after_failure(route, error, media)

    assert decision.should_retry is True
    assert decision.teach_route_on_success is None

    decision.record_retry_success()

    assert unsupported_media_kinds_for_route(route) == frozenset()


def test_auth_worded_invalid_request_retries_but_never_teaches() -> None:
    """Credential failures phrased as 400s retry (and fail again) but are not capability evidence."""
    reset_model_media_capability_cache()
    media = _media_inputs()

    text_decision = retry_media_inputs_after_failure(
        _route(),
        "Error code: 400 - invalid api key provided",
        media,
    )
    # Google rejects bad API keys with HTTP 400, so the status-code path needs the same exclusion.
    exception_decision = retry_media_inputs_after_failure(
        _route(),
        ModelProviderError(message="API key not valid. Please pass a valid API key.", status_code=400),
        media,
    )

    assert text_decision.should_retry is True
    assert text_decision.teach_route_on_success is None
    assert exception_decision.should_retry is True
    assert exception_decision.teach_route_on_success is None


def test_non_request_status_exception_retries_but_never_teaches() -> None:
    """Server-side failures retry without media but are not capability evidence."""
    reset_model_media_capability_cache()
    media = _media_inputs()
    error = ModelProviderError(message="upstream connect error", status_code=502)

    decision = retry_media_inputs_after_failure(_route(), error, media)

    assert decision.should_retry is True
    assert decision.teach_route_on_success is None


def test_kind_specific_error_takes_precedence_over_generic_gate() -> None:
    """A named kind on a 400 drops and teaches only that kind, not everything present."""
    reset_model_media_capability_cache()
    media = _media_inputs()
    route = _route()
    error = ModelProviderError(message="audio input is not supported", status_code=400)

    decision = retry_media_inputs_after_failure(route, error, media)

    assert decision.should_retry is True
    assert decision.removed_kinds == frozenset({"audio"})
    assert decision.media_inputs.images == media.images
    assert unsupported_media_kinds_for_route(route) == frozenset({"audio"})
    reset_model_media_capability_cache()


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


def test_openai_invalid_audio_format_message_without_field_retries_without_caching() -> None:
    """LiteLLM can omit OpenAI's input_audio.format field from the surfaced message."""
    reset_model_media_capability_cache()
    media = _media_inputs()
    route = _route()
    error = (
        "litellm.BadRequestError: OpenAIException - Invalid value: 'bin'. "
        "Supported values are: 'wav' and 'mp3'.. Received Model Group=gpt-5.5"
    )

    decision = retry_media_inputs_after_failure(route, error, media)

    assert decision.should_retry is True
    assert decision.removed_kinds == frozenset({"audio"})
    assert decision.media_inputs.audio == ()
    assert decision.media_inputs.images == media.images
    assert filter_media_inputs_for_route(route, media).media_inputs == media


def test_openai_supported_audio_values_without_audio_field_is_not_audio_attribution() -> None:
    """Wav/mp3 validation text alone must not single out audio; the generic drop-all retry applies."""
    reset_model_media_capability_cache()
    media = _media_inputs()
    route = _route()
    error = "Invalid value: 'bin'. Supported values are: 'wav' and 'mp3'. parameter=output_format"

    decision = retry_media_inputs_after_failure(route, error, media)

    assert decision.should_retry is True
    assert decision.removed_kinds == frozenset({"audio", "image", "file", "video"})
    assert decision.teach_route_on_success is None
    assert filter_media_inputs_for_route(route, media).media_inputs == media


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
