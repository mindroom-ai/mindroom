"""Shared inline-media fallback detection and prompt helpers."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mindroom.media_inputs import MediaInputs

_INLINE_MEDIA_FALLBACK_MARKER = "[Inline media unavailable for this model]"
_INLINE_MEDIA_FIELD_PATTERN = re.compile(r"(?:document|image|audio|video)\.source\.base64(?:\.media_type)?")
_INLINE_MEDIA_MIME_MISMATCH_PATTERN = re.compile(r"image was specified using the .* media type")
_INLINE_MEDIA_UNSUPPORTED_PATTERN = re.compile(r"(?:audio|image|video|file|document) input is not supported")
_INLINE_AUDIO_UNSUPPORTED_PATTERN = re.compile(r"audio input is not supported")
_UNSUPPORTED_INLINE_AUDIO_MODELS: set[tuple[str, str, str]] = set()


def _is_media_validation_error_text(error_text: str) -> bool:
    """Return whether provider error text indicates inline-media validation/capability failure."""
    lowered_error_text = error_text.lower()
    return bool(
        _INLINE_MEDIA_FIELD_PATTERN.search(lowered_error_text)
        or _INLINE_MEDIA_MIME_MISMATCH_PATTERN.search(lowered_error_text)
        or _INLINE_MEDIA_UNSUPPORTED_PATTERN.search(lowered_error_text),
    )


def should_retry_without_inline_media(error: Exception | str, media_inputs: MediaInputs) -> bool:
    """Return whether this run should retry once without inline media."""
    if not media_inputs.has_any():
        return False
    return _is_media_validation_error_text(str(error))


def append_inline_media_fallback_prompt(full_prompt: str) -> str:
    """Append one-time guidance when inline media had to be dropped."""
    if _INLINE_MEDIA_FALLBACK_MARKER in full_prompt:
        return full_prompt
    return (
        f"{full_prompt.rstrip()}\n\n"
        f"{_INLINE_MEDIA_FALLBACK_MARKER} "
        "The model rejected inline attachments for this turn. "
        "Use available attachment IDs and tools to inspect files instead."
    )


def _normalize_model_capability_key(
    *,
    provider: str | None,
    model_id: str | None,
    base_url: str | None,
) -> tuple[str, str, str] | None:
    normalized_provider = (provider or "").strip().lower().replace("-", "_")
    normalized_model_id = (model_id or "").strip().lower()
    if not normalized_provider or not normalized_model_id:
        return None
    normalized_base_url = (base_url or "").strip().lower()
    return (normalized_provider, normalized_model_id, normalized_base_url)


def remember_inline_audio_unsupported(
    *,
    provider: str | None,
    model_id: str | None,
    base_url: str | None,
) -> None:
    """Record that this model/backend rejected inline audio."""
    key = _normalize_model_capability_key(
        provider=provider,
        model_id=model_id,
        base_url=base_url,
    )
    if key is None:
        return
    _UNSUPPORTED_INLINE_AUDIO_MODELS.add(key)


def should_mark_inline_audio_unsupported(error: Exception | str, media_inputs: MediaInputs) -> bool:
    """Return whether this failure should teach the cache to preflight-drop audio."""
    if not media_inputs.audio:
        return False
    return bool(_INLINE_AUDIO_UNSUPPORTED_PATTERN.search(str(error).lower()))


def should_preflight_skip_inline_audio(
    media_inputs: MediaInputs,
    *,
    provider: str | None,
    model_id: str | None,
    base_url: str | None,
) -> bool:
    """Return whether inline audio should be dropped before the first model call.

    Uses a small learned cache populated from prior inline-audio rejections.
    """
    if not media_inputs.audio:
        return False

    key = _normalize_model_capability_key(
        provider=provider,
        model_id=model_id,
        base_url=base_url,
    )
    if key is None:
        return False
    return key in _UNSUPPORTED_INLINE_AUDIO_MODELS


def clear_inline_audio_capability_cache() -> None:
    """Clear learned inline-audio capability cache (mainly for tests)."""
    _UNSUPPORTED_INLINE_AUDIO_MODELS.clear()
