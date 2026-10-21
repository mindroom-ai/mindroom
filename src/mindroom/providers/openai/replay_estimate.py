# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""Size the history a Responses request replays explicitly, as the request sends it.

History budgets take the larger of this estimate and the canonical one. A typed ``input_image`` block counts by
the visual patches OpenAI bills for it, read from the image header when the bytes are inline, never by its encoded
transport bytes.
"""

from __future__ import annotations

import base64
import io
import math
import struct
from typing import TYPE_CHECKING, Any

from mindroom.history.message_content import image_content_for_token_estimation
from mindroom.model_defaults import OPENAI_IMAGE_ORIGINAL_NO_PATCH_BUDGET_PREFIXES, OPENAI_IMAGE_PATCH_MODEL_PREFIXES
from mindroom.providers.openai.responses_wire import responses_input
from mindroom.token_budget import approximate_o200k_tokens, stable_serialize

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.llm.messages import Message

__all__ = ["estimate_responses_replay_tokens", "responses_replay_uses_visual_tokens"]

# Header bytes read from an inline image; longer metadata before the frame falls back to the patch ceiling.
_HEADER_SCAN_BYTES = 64 * 1024


def responses_replay_uses_visual_tokens(model_id: str) -> bool:
    """Return whether images are priced by their visual patches, which only models with known patch budgets allow."""
    return model_id.startswith(OPENAI_IMAGE_PATCH_MODEL_PREFIXES)


def estimate_responses_replay_tokens(
    messages: Sequence[Message],
    *,
    model_id: str,
    cache_system_prompt: bool,
    explicit_breakpoint: bool,
) -> int:
    """Count the ``input`` an explicitly replayed Responses request sends for ``messages``."""
    formatted = responses_input(
        messages,
        store=False,
        portable_replay=True,
        native_compaction=None,
        cache_system_prompt=cache_system_prompt,
        explicit_breakpoint=explicit_breakpoint,
    )
    if not responses_replay_uses_visual_tokens(model_id):
        return approximate_o200k_tokens(stable_serialize(formatted))
    estimated_input = []
    image_tokens = 0
    for item in formatted:
        projected = dict(item)
        for field_name in ("content", "output"):
            content = item.get(field_name)
            if isinstance(content, list):
                projected[field_name] = [image_content_for_token_estimation(block) for block in content]
                image_tokens += sum(
                    _image_tokens(block, model_id)
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "input_image"
                )
        estimated_input.append(projected)
    return approximate_o200k_tokens(stable_serialize(estimated_input)) + image_tokens


def _image_tokens(block: dict[str, Any], model_id: str) -> int:
    """Estimate one image's visual patches; unknown dimensions use the model and detail ceiling.

    Current patch-model sizing: https://developers.openai.com/api/docs/guides/images-vision
    """
    detail = block.get("detail", "auto")
    recent = model_id.startswith(OPENAI_IMAGE_ORIGINAL_NO_PATCH_BUDGET_PREFIXES)
    if detail == "auto":
        detail = "high" if model_id.startswith("gpt-5.4") else "original"
    max_dimension, patch_limit = 65_535, 30_000
    if detail == "low":
        max_dimension, patch_limit = (2048, 6144) if model_id.startswith("gpt-5.4") else (512, 256)
    elif detail == "high":
        max_dimension, patch_limit = (65_535 if model_id.startswith("gpt-6") else 2048), 2500
    elif not recent:
        max_dimension, patch_limit = 6000, 10_000

    dimensions = _embedded_image_dimensions(block.get("image_url"))
    if dimensions is None:
        return math.ceil(patch_limit * 1.2)
    width, height = dimensions
    scale = min(1, max_dimension / max(width, height))
    patches = math.ceil(math.ceil(width * scale) / 32) * math.ceil(math.ceil(height * scale) / 32)
    # The resize budget is a conservative upper bound; exact resized coverage can be slightly smaller. Original
    # detail on recent models is not resized to the 30,000-patch rejection limit, so oversized input stays visible.
    if not (recent and detail == "original"):
        patches = min(patches, patch_limit)
    return math.ceil(patches * 1.2)


def _embedded_image_dimensions(source: object) -> tuple[int, int] | None:
    """Read a known image header from a base64 data URL, with no URL fetch or pixel decoding."""
    if not isinstance(source, str) or not source.startswith("data:image/") or ";base64," not in source:
        return None
    try:
        offset = source.index(",") + 1
        data = base64.b64decode(source[offset : offset + _HEADER_SCAN_BYTES], validate=True)
        return _header_dimensions(data)
    except (ValueError, TypeError, struct.error):
        return None


def _header_dimensions(data: bytes) -> tuple[int, int] | None:
    """Return the width and height a PNG, GIF, JPEG, or WebP header records, or None for any other data."""
    if len(data) < 12:
        return None
    if data[0:8] == b"\x89PNG\r\n\x1a\n":
        width, height = struct.unpack(">LL", data[16:24])
    elif data[0:4] == b"GIF8" and data[5:6] == b"a":
        width, height = struct.unpack("<HH", data[6:10])
    elif data[0:3] == b"\xff\xd8\xff":
        width, height = _jpeg_dimensions(data)
    elif data[0:4] == b"RIFF" and data[8:12] == b"WEBP":
        dimensions = _webp_dimensions(data)
        if dimensions is None:
            return None
        width, height = dimensions
    else:
        return None
    return (width, height) if width > 0 and height > 0 else None


def _jpeg_dimensions(data: bytes) -> tuple[int, int]:
    """Scan for the first start-of-frame marker; markers 0xC4, 0xC8, and 0xCC are not frames."""
    with io.BytesIO(data) as stream:
        size = 2
        marker = 0
        while not 0xC0 <= marker <= 0xCF or marker in (0xC4, 0xC8, 0xCC):
            stream.seek(size, 1)
            byte = stream.read(1)
            while ord(byte) == 0xFF:
                byte = stream.read(1)
            marker = ord(byte)
            size = struct.unpack(">H", stream.read(2))[0] - 2
        stream.seek(1, 1)
        height, width = struct.unpack(">HH", stream.read(4))
    return width, height


def _webp_dimensions(data: bytes) -> tuple[int, int] | None:
    """Read the extended, lossy, or lossless WebP header; another chunk type has no known dimensions."""
    chunk = data[12:16]
    if chunk == b"VP8X":
        return struct.unpack("<I", data[24:27] + b"\x00")[0] + 1, struct.unpack("<I", data[27:30] + b"\x00")[0] + 1
    if chunk == b"VP8 ":
        return struct.unpack("<H", data[26:28])[0] & 0x3FFF, struct.unpack("<H", data[28:30])[0] & 0x3FFF
    if chunk == b"VP8L":
        bits = struct.unpack("<I", data[21:25])[0]
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    return None
