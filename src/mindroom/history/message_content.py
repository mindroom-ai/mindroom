"""Shared text and media projections for history serialization and estimation."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, cast

from pydantic import BaseModel

from mindroom.media_delivery import VIEWED_IMAGE_ID_PREFIX
from mindroom.token_budget import stable_serialize

if TYPE_CHECKING:
    from agno.media import Image
    from agno.models.message import Message


_MAX_HISTORY_VIEWED_IMAGES = 4
_MAX_HISTORY_VIEWED_IMAGE_BYTES = 10 * 1024 * 1024
HISTORY_VIEWED_IMAGE_FALLBACK_TOKENS = 5_000


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


def project_history_media_for_replay(messages: Sequence[Message]) -> list[Message]:
    """Copy changed history messages and retain only bounded newest viewed images."""
    retained: set[tuple[int, int]] = set()
    retained_count = 0
    retained_bytes = 0
    for message_index in range(len(messages) - 1, -1, -1):
        images = messages[message_index].images or []
        for image_index in range(len(images) - 1, -1, -1):
            image = images[image_index]
            content = image.content
            if not _is_viewed_image(image) or not isinstance(content, bytes) or not content:
                continue
            image_bytes = len(content)
            if (
                retained_count >= _MAX_HISTORY_VIEWED_IMAGES
                or image_bytes > _MAX_HISTORY_VIEWED_IMAGE_BYTES - retained_bytes
            ):
                continue
            retained.add((message_index, image_index))
            retained_count += 1
            retained_bytes += image_bytes

    projected: list[Message] = []
    for message_index, message in enumerate(messages):
        source_images = message.images or []
        replay_images = [
            image for image_index, image in enumerate(source_images) if (message_index, image_index) in retained
        ]
        omitted_viewed = sum(
            _is_viewed_image(image) and (message_index, image_index) not in retained
            for image_index, image in enumerate(source_images)
        )
        updates: dict[str, object] = {
            "audio": None,
            "images": replay_images or None,
            "files": None,
            "videos": None,
        }
        if omitted_viewed:
            updates["content"] = _content_with_viewed_image_omission(message, omitted_viewed)
            updates["compressed_content"] = None
        changed = bool(message.audio or message.images or message.files or message.videos or omitted_viewed)
        projected.append(message.model_copy(update=updates) if changed else message)
    return projected


def _is_viewed_image(image: Image) -> bool:
    image_id = image.id
    return isinstance(image_id, str) and image_id.startswith(VIEWED_IMAGE_ID_PREFIX)


def _content_with_viewed_image_omission(message: Message, omitted: int) -> str:
    noun = "image" if omitted == 1 else "images"
    notice = (
        f"[{omitted} historical viewed {noun} omitted from replay to keep history within the "
        f"{_MAX_HISTORY_VIEWED_IMAGES}-image, {_MAX_HISTORY_VIEWED_IMAGE_BYTES // (1024 * 1024)} MiB limit. "
        "Reopen the original workspace path with view_file, or use an authorized attachment ID.]"
    )
    content = render_message_content(message)
    return f"{content}\n\n{notice}" if content else notice


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
