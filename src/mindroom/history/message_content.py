"""Shared message projections for history serialization, estimation, and replay: text, media, and narrowed tool calls."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

from mindroom.llm.media import VIEWED_IMAGE_ID_PREFIX, Audio, File, Image, Video
from mindroom.token_budget import stable_serialize

if TYPE_CHECKING:
    from mindroom.llm.messages import Message


_MAX_HISTORY_VIEWED_IMAGES = 4
_MAX_HISTORY_VIEWED_IMAGE_BYTES = 10 * 1024 * 1024
HISTORY_VIEWED_IMAGE_FALLBACK_TOKENS = 5_000


def message_media_entries(message: Message) -> tuple[tuple[str, object | None], ...]:
    """Return the message's media fields in stable order."""
    return (
        ("images", message.images),
        ("audio", message.audio),
        ("videos", message.videos),
        ("files", message.files),
    )


def media_payload_snapshot(media_value: object) -> object:
    """Serialize media metadata without embedding the binary content."""
    if isinstance(media_value, (Image, Audio, Video, File)):
        return media_value.to_dict(include_content=False)
    if isinstance(media_value, Sequence) and not isinstance(media_value, (str, bytes, bytearray)):
        return [media_payload_snapshot(item) for item in media_value]
    return media_value


def _retained_viewed_images(messages: Sequence[Message]) -> set[tuple[int, int]]:
    """Return ``(message, image)`` indexes of the newest viewed images replay keeps within its count and byte caps."""
    retained: set[tuple[int, int]] = set()
    retained_bytes = 0
    for message_index in reversed(range(len(messages))):
        images = messages[message_index].images or []
        for image_index in reversed(range(len(images))):
            content = images[image_index].content
            if not _is_viewed_image(images[image_index]) or not content:
                continue
            if (
                len(retained) >= _MAX_HISTORY_VIEWED_IMAGES
                or len(content) > _MAX_HISTORY_VIEWED_IMAGE_BYTES - retained_bytes
            ):
                continue
            retained.add((message_index, image_index))
            retained_bytes += len(content)
    return retained


def project_history_media_for_replay(messages: Sequence[Message]) -> list[Message]:
    """Drop the media of replayed history except the newest viewed images, noting the viewed images left out.

    Attachments replay as the text that names them; only images ``view_file`` attached replay, within a count and a
    byte cap. Messages without media are returned as they are; the others are copies, so stored runs never change.
    """
    retained = _retained_viewed_images(messages)
    projected: list[Message] = []
    for message_index, message in enumerate(messages):
        if not (message.images or message.audio or message.videos or message.files):
            projected.append(message)
            continue
        images = message.images or ()
        kept = [image for image_index, image in enumerate(images) if (message_index, image_index) in retained]
        omitted = sum(_is_viewed_image(image) for image in images) - len(kept)
        copy = replace(message, images=kept or None, audio=None, videos=None, files=None)
        if omitted:
            copy = replace(copy, content=_content_with_viewed_image_omission(message, omitted), compressed_content=None)
        projected.append(copy)
    return projected


def _is_viewed_image(image: Image) -> bool:
    """Return whether an image is one ``view_file`` attached, the only historical media history replays."""
    return image.id.startswith(VIEWED_IMAGE_ID_PREFIX)


def _content_with_viewed_image_omission(message: Message, omitted: int) -> str:
    """Return the message's content followed by the notice that ``omitted`` of its viewed images do not replay."""
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
        block = cast("dict[object, object]", value)
        if block.get("type") == "input_image":
            return {key: item for key, item in block.items() if key not in {"image_url", "file_id"}}
    return value


def render_message_content(message: Message) -> str:
    """Render one replayable string form of a message body."""
    # TODO(simplify): A stored ``compressed_content``, which only the retired compress_tool_results wrote and no provider
    # sends, replaces the content in estimates, summary input, and a replayed viewed-image omission notice. Read
    # ``content`` only. Models and operators would notice only on runs stored while that option was on.
    content = message.compressed_content if message.compressed_content is not None else message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(stable_serialize(part) for part in content)
    if content is None:
        return ""
    return stable_serialize(content)


def with_tool_calls(message: Message, calls: list[dict[str, Any]]) -> Message | None:
    """Return a copy of an assistant ``message`` holding only ``calls``, or None when it then has no call and no content.

    A message left with nothing would replay as an empty assistant turn that takes a message slot, so every step that
    narrows a message's calls, the legacy repair and the tool-call window, drops it instead. A message that keeps its
    content ends without calls as None, the shape of a message that never had any.
    """
    if not calls and not message.content:
        return None
    return replace(message, tool_calls=calls or None)
