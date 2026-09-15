"""Shared text and media projections for history serialization and estimation."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, cast

from pydantic import BaseModel

from mindroom.token_budget import stable_serialize

if TYPE_CHECKING:
    from agno.models.message import Message


def message_media_entries(message: Message) -> tuple[tuple[str, object | None], ...]:
    """Return replayable input and output media fields in stable order."""
    return (
        ("images", message.images),
        ("audio", message.audio),
        ("videos", message.videos),
        ("files", message.files),
        ("audio_output", message.audio_output),
        ("image_output", message.image_output),
        ("video_output", message.video_output),
        ("file_output", message.file_output),
    )


def media_payload_snapshot(media_value: object) -> object:
    """Serialize media metadata without embedding the binary content."""
    if isinstance(media_value, BaseModel):
        payload = cast("dict[str, object]", media_value.model_dump(exclude_none=True))
        payload.pop("content", None)
        return payload
    if isinstance(media_value, Sequence) and not isinstance(media_value, (str, bytes, bytearray)):
        return [media_payload_snapshot(item) for item in media_value]
    return media_value


def image_content_for_token_estimation(value: object) -> object:
    """Remove transport fields only from a typed image block, retaining its shape."""
    if isinstance(value, dict):
        block = cast("dict[str, object]", value)
        if block.get("type") == "input_image":
            return {key: item for key, item in block.items() if key not in {"image_url", "file_id"}}
    return value


def render_message_content(message: Message) -> str:
    """Render one replayable string form of a message body."""
    content = message.compressed_content if message.compressed_content is not None else message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(stable_serialize(part) for part in content)
    if content is None:
        return ""
    return stable_serialize(content)
