"""Shared message projections for history serialization, estimation, and replay: text, media, and narrowed tool calls."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from mindroom.llm.media import VIEWED_IMAGE_ID_PREFIX
from mindroom.llm.messages import PRUNED_MEDIA_FOLLOW_UP_TEXT, is_media_follow_up, stable_serialize

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.llm.media import Audio, File, Image, Video
    from mindroom.llm.messages import Message


_MAX_HISTORY_VIEWED_IMAGES = 4
_MAX_HISTORY_VIEWED_IMAGE_BYTES = 10 * 1024 * 1024

_IN_TURN_TRANSIENT_IMAGE_TRIGGER_COUNT = 10
_IN_TURN_TRANSIENT_IMAGE_TRIGGER_BYTES = 4 * 1024 * 1024
_IN_TURN_TRANSIENT_IMAGE_TARGET_COUNT = 2
_IN_TURN_TRANSIENT_IMAGE_TARGET_BYTES = 1 * 1024 * 1024

_IN_TURN_VIEWED_IMAGE_TRIGGER_COUNT = 12
_IN_TURN_VIEWED_IMAGE_TRIGGER_BYTES = 15 * 1024 * 1024

_VIEWED_IMAGE_REOPEN_HINT = (
    "Reopen the original workspace path with view_file, or an authorized attachment ID "
    'with attachment(action="get", attachment_id=..., view=True).'
)


def message_media_entries(message: Message) -> tuple[tuple[str, Sequence[Image | Audio | Video | File] | None], ...]:
    """Return the message's media fields in stable order."""
    return (
        ("images", message.images),
        ("audio", message.audio),
        ("videos", message.videos),
        ("files", message.files),
    )


def media_payload_snapshot(media: Sequence[Image | Audio | Video | File]) -> list[dict[str, Any]]:
    """Serialize media metadata without embedding the binary content."""
    return [item.to_dict(include_content=False) for item in media]


def _retained_viewed_images(messages: Sequence[Message]) -> set[tuple[int, int]]:
    """Return ``(message, image)`` indexes of the newest viewed images replay keeps within its count and byte caps."""
    retained: set[tuple[int, int]] = set()
    retained_bytes = 0
    for message_index in reversed(range(len(messages))):
        images = messages[message_index].images or []
        for image_index in reversed(range(len(images))):
            image = images[image_index]
            if not _is_viewed_image(image) or not image.content:
                continue
            if (
                len(retained) >= _MAX_HISTORY_VIEWED_IMAGES
                or len(image.content) > _MAX_HISTORY_VIEWED_IMAGE_BYTES - retained_bytes
            ):
                continue
            retained.add((message_index, image_index))
            retained_bytes += len(image.content)
    return retained


def project_history_media_for_replay(messages: Sequence[Message]) -> list[Message]:
    """Drop the media of replayed history except the newest viewed images, noting the viewed images left out.

    Attachments replay as the text that names them; only images ``image_result`` marks as viewed replay, within a
    count and a byte cap. Messages without media are returned as they are; the others are copies, so stored runs
    never change.
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
    """Return whether an image is a viewed one ``image_result`` attached, the only historical media history replays."""
    return image.id.startswith(VIEWED_IMAGE_ID_PREFIX)


def _content_with_viewed_image_omission(message: Message, omitted: int) -> str:
    """Return the message's content followed by the notice that ``omitted`` of its viewed images do not replay."""
    noun = "image" if omitted == 1 else "images"
    notice = (
        f"[{omitted} historical viewed {noun} omitted from replay to keep history within the "
        f"{_MAX_HISTORY_VIEWED_IMAGES}-image, {_MAX_HISTORY_VIEWED_IMAGE_BYTES // (1024 * 1024)} MiB limit. "
        f"{_VIEWED_IMAGE_REOPEN_HINT}]"
    )
    content = render_message_content(message)
    return f"{content}\n\n{notice}" if content else notice


def _in_turn_viewed_omission_notice(omitted: int) -> str:
    """Return the notice appended when in-turn pruning drops ``omitted`` viewed images."""
    noun = "image" if omitted == 1 else "images"
    return (
        f"[{omitted} earlier viewed {noun} omitted to keep context within the "
        f"{_MAX_HISTORY_VIEWED_IMAGES}-image, {_MAX_HISTORY_VIEWED_IMAGE_BYTES // (1024 * 1024)} MiB limit. "
        f"{_VIEWED_IMAGE_REOPEN_HINT}]"
    )


def _select_images_to_drop(
    entries: Sequence[tuple[int, int, int, bool]],
    *,
    trigger_count: int,
    trigger_bytes: int,
    target_count: int,
    target_bytes: int,
) -> set[tuple[int, int]]:
    """Return ``(message, image)`` indexes of oldest seen entries to drop when totals exceed the trigger."""
    total_count = len(entries)
    total_bytes = sum(size for _, _, size, _ in entries)
    if total_count <= trigger_count and total_bytes <= trigger_bytes:
        return set()
    dropped: set[tuple[int, int]] = set()
    for message_index, image_index, size, seen in entries:
        if (total_count <= target_count and total_bytes <= target_bytes) or total_count <= 1:
            break
        if not seen:
            continue
        dropped.add((message_index, image_index))
        total_count -= 1
        total_bytes -= size
    return dropped


def _prune_follow_up_message(message: Message, message_index: int, dropped: set[tuple[int, int]]) -> Message:
    """Return a copy of ``message`` with ``dropped`` images removed and its omission text updated."""
    kept: list[Image] = []
    dropped_transient = 0
    dropped_viewed = 0
    for image_index, image in enumerate(message.images or ()):
        if (message_index, image_index) in dropped:
            if _is_viewed_image(image):
                dropped_viewed += 1
            else:
                dropped_transient += 1
        else:
            kept.append(image)
    has_remaining_media = bool(kept or message.audio or message.videos or message.files)
    notes: list[str] = []
    if has_remaining_media and dropped_transient:
        noun = "image" if dropped_transient == 1 else "images"
        notes.append(f"[{dropped_transient} earlier tool {noun} omitted to keep context bounded.]")
    if dropped_viewed:
        notes.append(_in_turn_viewed_omission_notice(dropped_viewed))
    existing = message.content if isinstance(message.content, str) else ""
    base = existing if has_remaining_media else PRUNED_MEDIA_FOLLOW_UP_TEXT
    return replace(message, images=kept or None, content="\n\n".join((base, *notes)))


def prune_in_turn_tool_images(messages: list[Message]) -> bool:
    """Batch-prune older seen in-turn tool images in place using two-tier hysteresis.

    Only non-history media follow-up messages with images are eligible. Images after the latest assistant message
    count toward totals so oversized batches still trigger pruning of older seen images, but are never dropped.
    Replaces pruned slots with ``dataclasses.replace`` copies, preserving ids, timestamps, and ordinals.
    """
    last_assistant = max((index for index, message in enumerate(messages) if message.role == "assistant"), default=-1)
    transient: list[tuple[int, int, int, bool]] = []
    viewed: list[tuple[int, int, int, bool]] = []
    for message_index, message in enumerate(messages):
        if message.from_history or not is_media_follow_up(message) or not message.images:
            continue
        seen = message_index < last_assistant
        for image_index, image in enumerate(message.images):
            size = len(image.content) if image.content else 0
            (viewed if _is_viewed_image(image) else transient).append((message_index, image_index, size, seen))
    dropped = _select_images_to_drop(
        transient,
        trigger_count=_IN_TURN_TRANSIENT_IMAGE_TRIGGER_COUNT,
        trigger_bytes=_IN_TURN_TRANSIENT_IMAGE_TRIGGER_BYTES,
        target_count=_IN_TURN_TRANSIENT_IMAGE_TARGET_COUNT,
        target_bytes=_IN_TURN_TRANSIENT_IMAGE_TARGET_BYTES,
    ) | _select_images_to_drop(
        viewed,
        trigger_count=_IN_TURN_VIEWED_IMAGE_TRIGGER_COUNT,
        trigger_bytes=_IN_TURN_VIEWED_IMAGE_TRIGGER_BYTES,
        target_count=_MAX_HISTORY_VIEWED_IMAGES,
        target_bytes=_MAX_HISTORY_VIEWED_IMAGE_BYTES,
    )
    if not dropped:
        return False
    for message_index in sorted({index for index, _ in dropped}):
        messages[message_index] = _prune_follow_up_message(messages[message_index], message_index, dropped)
    return True


def render_message_content(message: Message) -> str:
    """Render one replayable string form of a message body: a legacy row's compressed copy, if any, else the content.

    The copy is legacy data (``Message.compressed_content``); no provider is sent it.
    """
    content = message.compressed_content if message.compressed_content is not None else message.content
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    return "\n".join(stable_serialize(part) for part in content)


def with_tool_calls(message: Message, calls: list[dict[str, Any]]) -> Message | None:
    """Return a copy of an assistant ``message`` holding only ``calls``, or None when it then has no call and no content.

    A message left with nothing would replay as an empty assistant turn that takes a message slot, so every step that
    narrows a message's calls, the legacy repair and the tool-call window, drops it instead. A message that keeps its
    content ends without calls as None, the shape of a message that never had any.
    """
    if not calls and not message.content:
        return None
    return replace(message, tool_calls=calls or None)
