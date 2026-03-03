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
