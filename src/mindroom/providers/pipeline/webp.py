"""Bounded off-loop WebP-to-PNG/JPEG transcoding and candidate matching for media fallback."""

from __future__ import annotations

import asyncio
import io
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from mindroom.llm.errors import InvalidRequest
from mindroom.llm.media import IMAGE_HEADER_BYTES, image_format

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.llm.errors import ProviderError
    from mindroom.llm.media import Image
    from mindroom.llm.messages import Message
    from mindroom.llm.provider import MediaKind, ModelProvider

__all__ = [
    "LEARNED_WEBP_UNSUPPORTED",
    "MediaRoute",
    "is_webp_transcode_candidate",
    "local_image_ids",
    "media_route",
    "should_learn_webp_unsupported",
    "transcode_webp_if",
    "transcode_webp_messages",
]

_PAYLOAD_TOO_LARGE_STATUS = 413
_UNSUPPORTED_MEDIA_TYPE_STATUS = 415
_MAX_SCANNED_ERROR_CHARS = 2000
_MAX_WEBP_SOURCE_BYTES = 20 * 1024 * 1024
_MAX_WEBP_DECODED_PIXELS = 40_000_000
_MAX_WEBP_EDGE = 2048
_MAX_WEBP_OUTPUT_BYTES = 5 * 1024 * 1024
_INVALID_IMAGE_URL_CODE = "invalid_image_url"
_WEBP_TRANSCODE_ERROR_CODES = frozenset({"invalid_image_format", "image_parse_error", "invalid_image"})
# Standalone format tokens (never .webp/.png/.jpg inside filenames/URLs, while still allowing image/<fmt>).
_STANDALONE_WEBP_FORMAT = re.compile(r"(?:(?<![./])|(?<=\bimage/))\bwebp\b")
_STANDALONE_IMAGE_FORMAT = re.compile(r"(?:(?<![./])|(?<=\bimage/))\b(?:png|jpe?g)\b")
_REJECTED_WEBP_ERROR = re.compile(
    r"(?:unsupported|invalid|not supported|disallowed|unrecognized|got|received)"
    r"[^.;\n]{0,60}(?:(?<![./])|(?<=\bimage/))\bwebp\b"
    r"|(?:(?<![./])|(?<=\bimage/))\bwebp\b[^.;\n]{0,40}"
    r"(?:is |are )?(?:not supported|unsupported|invalid|disallowed)",
)
_FORMAT_LIST_CONTEXT_MARKERS = (
    "supported",
    "only",
    "expected",
    "allowed",
    "format",
    "mime",
    "media_type",
    "media type",
)
_IMAGE_FORMAT_ERROR_MARKERS = (
    "mime",
    "media_type",
    "media type",
    "format",
    "unsupported image",
    "invalid image",
    "decode image",
    "parse image",
)


@dataclass(frozen=True, slots=True)
class MediaRoute:
    """Process-local key identifying one provider, model id, and endpoint for learned media rules."""

    provider: str
    model_id: str
    endpoint: str | None


# Learned WebP limits live for the process only; a provider may add support at any time.
LEARNED_WEBP_UNSUPPORTED: set[MediaRoute] = set()


def media_route(inner: ModelProvider) -> MediaRoute:
    """Return the process-local route key for ``inner``."""
    identity = inner.identity
    return MediaRoute(provider=identity.provider.lower(), model_id=identity.id, endpoint=identity.endpoint)


def local_image_ids(messages: Sequence[Message]) -> frozenset[str]:
    """Return the ids of local images (``url is None``) present in ``messages``."""
    return frozenset(image.id for message in messages for image in message.images or () if image.url is None)


def _read_local_webp_bytes(image: Image) -> bytes | None:
    """Return local WebP bytes within the source cap, else None for non-WebP, remote, or unreadable items."""
    if image.content is not None:
        return (
            image.content
            if 0 < len(image.content) <= _MAX_WEBP_SOURCE_BYTES and image_format(image.content) == "webp"
            else None
        )
    if not image.filepath:
        return None
    try:
        file_path = Path(image.filepath)
        if not file_path.is_file():
            return None
        with file_path.open("rb") as handle:
            header = handle.read(IMAGE_HEADER_BYTES)
            if image_format(header) != "webp" or os.fstat(handle.fileno()).st_size > _MAX_WEBP_SOURCE_BYTES:
                return None
            data = header + handle.read(_MAX_WEBP_SOURCE_BYTES - len(header) + 1)
    except OSError:
        return None
    return data if len(data) <= _MAX_WEBP_SOURCE_BYTES else None


def _transcode_webp_bytes(data: bytes) -> tuple[bytes, str] | None:
    """Safely decode WebP bytes, downscale if needed, and encode bounded PNG/JPEG within the inline payload cap."""
    from PIL import Image as PILImage  # noqa: PLC0415 - keep Pillow out of module import time
    from PIL import ImageOps, UnidentifiedImageError  # noqa: PLC0415

    try:
        with PILImage.open(io.BytesIO(data)) as source:
            width, height = source.size
            if source.format != "WEBP" or width * height > _MAX_WEBP_DECODED_PIXELS:
                return None
            source.seek(0)
            oriented = ImageOps.exif_transpose(source)
            if oriented is None:
                return None
            if max(oriented.size) > _MAX_WEBP_EDGE:
                oriented.thumbnail((_MAX_WEBP_EDGE, _MAX_WEBP_EDGE), PILImage.Resampling.LANCZOS)
            if oriented.mode == "CMYK":
                oriented = oriented.convert("RGB")
            output = io.BytesIO()
            oriented.save(output, format="PNG")
            encoded = output.getvalue()
            if len(encoded) <= _MAX_WEBP_OUTPUT_BYTES:
                return encoded, "image/png"
            if "A" in oriented.getbands() or "transparency" in oriented.info:
                return None
            jpeg_output = io.BytesIO()
            oriented.convert("RGB").save(jpeg_output, format="JPEG", quality=85)
            jpeg_data = jpeg_output.getvalue()
            return (jpeg_data, "image/jpeg") if len(jpeg_data) <= _MAX_WEBP_OUTPUT_BYTES else None
    except (
        OSError,
        ValueError,
        SyntaxError,
        UnidentifiedImageError,
        PILImage.DecompressionBombError,
    ):
        return None


def transcode_webp_messages(messages: Sequence[Message]) -> list[Message] | None:
    """Transcode local WebP images in ``messages`` to PNG/JPEG; return None when no image was transcoded."""
    rewritten: list[Message] = []
    changed = False
    for message in messages:
        if not message.images:
            rewritten.append(message)
            continue
        replaced = [
            replace(image, content=encoded[0], filepath=None, url=None, mime_type=encoded[1], format=None)
            if (raw := _read_local_webp_bytes(image)) is not None
            and (encoded := _transcode_webp_bytes(raw)) is not None
            else image
            for image in message.images
        ]
        if any(new is not old for old, new in zip(message.images, replaced)):
            changed = True
            rewritten.append(replace(message, images=replaced))
        else:
            rewritten.append(message)
    return rewritten if changed else None


async def transcode_webp_if(should_check: bool, base: Sequence[Message]) -> tuple[Sequence[Message], bool]:
    """Transcode local WebP images off the event loop when ``should_check`` is True."""
    if not should_check or (transcoded := await asyncio.to_thread(transcode_webp_messages, base)) is None:
        return base, False
    return transcoded, True


def is_webp_transcode_candidate(
    error: ProviderError,
    remaining: frozenset[MediaKind],
    sent: dict[str, MediaKind],
    local_images: frozenset[str],
    *,
    is_image_capability_gap: bool,
    names_image_kind: bool,
) -> bool:
    """Return whether ``error`` looks like an image format/MIME rejection worth retrying with transcoded WebP."""
    if (
        "image" not in remaining
        or not local_images
        or not (isinstance(error, InvalidRequest) or error.status_code == _UNSUPPORTED_MEDIA_TYPE_STATUS)
        or error.status_code == _PAYLOAD_TOO_LARGE_STATUS
        or is_image_capability_gap
        or error.provider_code == _INVALID_IMAGE_URL_CODE
    ):
        return False
    named = error.rejected_media & sent.keys() if isinstance(error, InvalidRequest) else frozenset()
    if named and not (named & local_images):
        return False
    text = str(error).lower()[:_MAX_SCANNED_ERROR_CHARS]
    if "download" in text:
        return False
    if _STANDALONE_WEBP_FORMAT.search(text) or error.provider_code in _WEBP_TRANSCODE_ERROR_CODES:
        return True
    if _STANDALONE_IMAGE_FORMAT.search(text) and any(marker in text for marker in _FORMAT_LIST_CONTEXT_MARKERS):
        return True
    implicates_image = bool(named & local_images) or names_image_kind
    return implicates_image and any(marker in text for marker in _IMAGE_FORMAT_ERROR_MARKERS)


def should_learn_webp_unsupported(error: ProviderError) -> bool:
    """Return whether ``error`` proves the route rejects WebP, rather than one malformed or animated image."""
    text = str(error).lower()[:_MAX_SCANNED_ERROR_CHARS]
    if _REJECTED_WEBP_ERROR.search(text):
        return True
    return (
        not _STANDALONE_WEBP_FORMAT.search(text)
        and bool(_STANDALONE_IMAGE_FORMAT.search(text))
        and any(marker in text for marker in _FORMAT_LIST_CONTEXT_MARKERS)
    )
