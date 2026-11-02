# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""Size the history a Responses request replays explicitly, as the request sends it.

History budgets take the larger of this estimate and the canonical one. A typed ``input_image`` block counts by
the visual patches OpenAI bills for it, read from the image header when the bytes are inline, never by its encoded
transport bytes. Encrypted reasoning counts by the reasoning tokens its response reported, never by its ciphertext.
"""

from __future__ import annotations

import base64
import io
import math
import struct
from typing import TYPE_CHECKING, Any

from mindroom.history.message_content import image_content_for_token_estimation
from mindroom.llm.messages import is_own_model_message, stable_serialize
from mindroom.model_defaults import OPENAI_IMAGE_ORIGINAL_NO_PATCH_BUDGET_PREFIXES, OPENAI_IMAGE_PATCH_MODEL_PREFIXES
from mindroom.providers.openai.response_replay import recorded_output
from mindroom.providers.openai.responses_wire import responses_input, responses_replay
from mindroom.token_counting.counts import count, opaque_tokens

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from mindroom.llm.messages import Message
    from mindroom.llm.provider import TokenCounter
    from mindroom.token_counting.requests import RequestParts

__all__ = ["estimate_responses_replay_tokens", "responses_body_parts", "responses_replay_uses_visual_tokens"]

# Header bytes read from an inline image; longer metadata before the frame falls back to the patch ceiling.
_HEADER_SCAN_BYTES = 64 * 1024


def responses_replay_uses_visual_tokens(model_id: str) -> bool:
    """Return whether images are priced by their visual patches, which only models with known patch budgets allow."""
    return model_id.startswith(OPENAI_IMAGE_PATCH_MODEL_PREFIXES)


def estimate_responses_replay_tokens(
    messages: Sequence[Message],
    *,
    counter: TokenCounter,
    model_id: str,
    cache_system_prompt: bool,
    explicit_breakpoint: bool,
    provider: str,
) -> int:
    """Count the ``input`` an explicitly replayed Responses request sends for ``messages``, its text with ``counter``.

    An attachment file that cannot be read raises ``InvalidRequest`` naming ``provider``, as the request would.
    """
    replayed = responses_input(
        responses_replay(messages, store=False, portable_replay=True, native_compaction=None),
        cache_system_prompt=cache_system_prompt,
        explicit_breakpoint=explicit_breakpoint,
        provider=provider,
    )
    formatted = [_without_reasoning_ciphertext(item) for item in replayed]
    reasoning_tokens = _earlier_reasoning_tokens(messages, replayed)
    if not responses_replay_uses_visual_tokens(model_id):
        return count(counter, stable_serialize(formatted)) + reasoning_tokens
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
    return count(counter, stable_serialize(estimated_input)) + image_tokens + reasoning_tokens


def responses_body_parts(
    body: Mapping[str, Any],
    messages: Sequence[Message],
    parts: RequestParts,
    *,
    model_id: str,
    chained: bool,
    static: bool,
) -> None:
    """Add what a Responses body built as an explicit replay of ``messages`` gives its model to ``parts``.

    The input items count serialized without their ciphertext: an encrypted reasoning item counts the reasoning tokens
    its answer reported, a checkpoint's ciphertext a quarter token per character, and an image its visual patches on
    a model priced by them. A request that ``chained`` continues a stored response whose reasoning the provider keeps,
    so each of this run's answers whose reasoning the replay does not send counts its reported reasoning tokens.
    With ``static`` the instructions and the tool definitions the request bills count too; a deferred tool counts
    once a tool search loads it, inside that search's output.
    """
    items = list(body.get("input") or [])
    ciphertext = sum(
        len(str(item.get("encrypted_content") or ""))
        for item in items
        if isinstance(item, dict) and item.get("type") == "compaction"
    )
    parts.cost(opaque_tokens(ciphertext))
    formatted = [_without_ciphertext(item) for item in items]
    if responses_replay_uses_visual_tokens(model_id):
        formatted, image_tokens = _without_image_transport(formatted, model_id)
        parts.cost(image_tokens)
    parts.value(formatted)
    replayed = _replayed_reasoning_ids(items)
    for message in messages:
        if message.role != "assistant":
            continue
        if replayed & _reasoning_ids(message) or (chained and not message.from_history):
            parts.cost(message.metrics.tokens.reasoning_tokens)
    if static:
        if isinstance(instructions := body.get("instructions"), str):
            parts.text(instructions)
        parts.value([tool for tool in body.get("tools") or () if not tool.get("defer_loading")])


def _replayed_reasoning_ids(items: Sequence[Any]) -> set[object]:
    return {
        item["id"]
        for item in items
        if isinstance(item, dict)
        and item.get("type") == "reasoning"
        and item.get("encrypted_content")
        and item.get("id")
    }


def _without_image_transport(items: list[Any], model_id: str) -> tuple[list[Any], int]:
    """Return the items without image transport fields, and the visual patches their images cost."""
    projected_items: list[Any] = []
    image_tokens = 0
    for item in items:
        projected = dict(item) if isinstance(item, dict) else item
        for field_name in ("content", "output"):
            content = item.get(field_name) if isinstance(item, dict) else None
            if isinstance(content, list):
                projected[field_name] = [image_content_for_token_estimation(block) for block in content]
                image_tokens += sum(
                    _image_tokens(block, model_id)
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "input_image"
                )
        projected_items.append(projected)
    return projected_items, image_tokens


def _without_ciphertext(item: Any) -> Any:  # noqa: ANN401
    """Drop the ciphertext of encrypted reasoning and of a checkpoint, which its length does not measure."""
    if isinstance(item, dict) and "encrypted_content" in item:
        return {key: value for key, value in item.items() if key != "encrypted_content"}
    return item


def _without_reasoning_ciphertext(item: Any) -> Any:  # noqa: ANN401
    """Drop encrypted reasoning, whose base64 length says nothing about the tokens the provider counts."""
    if isinstance(item, dict) and item.get("type") == "reasoning" and "encrypted_content" in item:
        return {key: value for key, value in item.items() if key != "encrypted_content"}
    return item


def _earlier_reasoning_tokens(messages: Sequence[Message], replayed: list[Any]) -> int:
    """Charge each earlier answer whose encrypted reasoning the request replays the reasoning tokens it reported.

    A model that renders earlier turns' reasoning (``reasoning.context: all_turns``) holds it in context. The
    running turn's own reasoning is left to mid-turn sizing, which adds each of its responses' reported thinking.
    """
    replayed_ids = {
        item["id"]
        for item in replayed
        if isinstance(item, dict)
        and item.get("type") == "reasoning"
        and item.get("encrypted_content")
        and item.get("id")
    }
    return sum(
        message.metrics.tokens.reasoning_tokens
        for message in messages
        if message.role == "assistant" and not is_own_model_message(message) and replayed_ids & _reasoning_ids(message)
    )


def _reasoning_ids(message: Message) -> set[object]:
    """Return the ids of the reasoning items in a stored answer's ordered output, the only reasoning replay sends."""
    return {item.get("id") for item in recorded_output(message) or () if item.get("type") == "reasoning"}


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
