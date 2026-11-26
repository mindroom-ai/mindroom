"""Shared inline image limits and bounded PNG/JPEG encoding."""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL.Image import Image as PillowImage

__all__ = [
    "MAX_IMAGE_DECODED_PIXELS",
    "MAX_IMAGE_EDGE",
    "MAX_IMAGE_SOURCE_BYTES",
    "MAX_INLINE_IMAGE_BYTES",
    "encode_png_or_jpeg",
]

MAX_IMAGE_SOURCE_BYTES = 20 * 1024 * 1024
MAX_INLINE_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_DECODED_PIXELS = 40_000_000
MAX_IMAGE_EDGE = 2048


def encode_png_or_jpeg(image: PillowImage, *, max_bytes: int = MAX_INLINE_IMAGE_BYTES) -> tuple[bytes, str]:
    """Encode ``image`` as PNG, falling back to RGB JPEG quality=85 when opaque and larger than ``max_bytes``."""
    if image.mode == "CMYK":
        image = image.convert("RGB")
    output = io.BytesIO()
    image.save(output, format="PNG")
    data = output.getvalue()
    if len(data) <= max_bytes or "A" in image.getbands() or "transparency" in image.info:
        return data, "image/png"
    output = io.BytesIO()
    image.convert("RGB").save(output, format="JPEG", quality=85)
    return output.getvalue(), "image/jpeg"
