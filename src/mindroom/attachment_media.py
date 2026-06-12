"""Attachment-to-model media conversion helpers."""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import TYPE_CHECKING

from agno.media import Audio, File, Image, Video

from .attachments import AttachmentRecord, filter_attachments_for_context, resolve_attachments
from .logging_config import get_logger
from .timing import emit_elapsed_timing

if TYPE_CHECKING:
    from pathlib import Path

logger = get_logger(__name__)

_MAX_INLINE_MEDIA_RECORDS = 512
_INLINE_MEDIA_RECORDS_BY_ID: OrderedDict[str, AttachmentRecord] = OrderedDict()
_INLINE_MEDIA_RECORDS_BY_PATH: OrderedDict[str, AttachmentRecord] = OrderedDict()


def _remember_attachment_record(record: AttachmentRecord) -> None:
    _INLINE_MEDIA_RECORDS_BY_ID[record.attachment_id] = record
    _INLINE_MEDIA_RECORDS_BY_ID.move_to_end(record.attachment_id)
    while len(_INLINE_MEDIA_RECORDS_BY_ID) > _MAX_INLINE_MEDIA_RECORDS:
        _INLINE_MEDIA_RECORDS_BY_ID.popitem(last=False)

    path_key = str(record.local_path.absolute())
    _INLINE_MEDIA_RECORDS_BY_PATH[path_key] = record
    _INLINE_MEDIA_RECORDS_BY_PATH.move_to_end(path_key)
    while len(_INLINE_MEDIA_RECORDS_BY_PATH) > _MAX_INLINE_MEDIA_RECORDS:
        _INLINE_MEDIA_RECORDS_BY_PATH.popitem(last=False)


def _inline_media_content_key(record: AttachmentRecord) -> tuple[str, ...]:
    mime_type = record.mime_type or ""
    if record.content_sha256:
        return (record.kind, mime_type, record.content_sha256)
    return (record.kind, mime_type, "filepath", str(record.local_path))


def attachment_records_to_media(
    attachment_records: list[AttachmentRecord],
) -> tuple[list[Audio], list[Image], list[File], list[Video]]:
    """Convert attachment records into Agno media objects and remember them for dedupe."""
    for record in attachment_records:
        _remember_attachment_record(record)

    audio: list[Audio] = []
    images: list[Image] = []
    files: list[File] = []
    videos: list[Video] = []

    for record in attachment_records:
        if not record.local_path.is_file():
            continue
        if record.kind == "audio":
            audio.append(
                Audio(
                    id=record.attachment_id,
                    filepath=str(record.local_path),
                    mime_type=record.mime_type,
                ),
            )
        elif record.kind == "image":
            images.append(
                Image(
                    id=record.attachment_id,
                    filepath=str(record.local_path),
                    mime_type=record.mime_type,
                ),
            )
        elif record.kind == "file":
            try:
                file_media = File(
                    id=record.attachment_id,
                    filepath=str(record.local_path),
                    mime_type=record.mime_type,
                    filename=record.filename,
                )
            except ValueError:
                # Agno validates file MIME types against a strict allow-list.
                # Fall back to filepath+filename so arbitrary attachments still work.
                file_media = File(
                    id=record.attachment_id,
                    filepath=str(record.local_path),
                    filename=record.filename,
                )
            files.append(file_media)
        elif record.kind == "video":
            videos.append(
                Video(
                    id=record.attachment_id,
                    filepath=str(record.local_path),
                    mime_type=record.mime_type,
                ),
            )

    return audio, images, files, videos


def resolve_scoped_attachments(
    storage_path: Path,
    attachment_ids: list[str],
    *,
    room_id: str | None = None,
    thread_id: str | None = None,
) -> list[AttachmentRecord]:
    """Resolve attachment IDs into records scoped to the current context.

    When *room_id* is provided, only attachments registered for the current
    room/thread context are included. Mismatched records are dropped with a
    debug log.
    """
    started = time.monotonic()
    rejected_count = 0
    attachment_records = resolve_attachments(storage_path, attachment_ids)
    if room_id is not None:
        attachment_records, rejected = filter_attachments_for_context(
            attachment_records,
            room_id=room_id,
            thread_id=thread_id,
        )
        rejected_count = len(rejected)
        if rejected:
            logger.debug(
                "Rejected out-of-context attachment IDs",
                rejected=rejected,
                room_id=room_id,
                thread_id=thread_id,
            )
    for record in attachment_records:
        _remember_attachment_record(record)
    emit_elapsed_timing(
        "response_payload.resolve_scoped_attachments",
        started,
        room_id=room_id,
        thread_id=thread_id,
        requested_attachment_count=len(attachment_ids),
        resolved_attachment_count=len(attachment_records),
        rejected_attachment_count=rejected_count,
    )
    return attachment_records
