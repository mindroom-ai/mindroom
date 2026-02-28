"""Attachment-to-model media conversion helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agno.media import Audio, File, Video

from .attachments import AttachmentRecord, resolve_attachments

if TYPE_CHECKING:
    from pathlib import Path


def attachment_records_to_media(
    attachment_records: list[AttachmentRecord],
) -> tuple[list[Audio], list[File], list[Video]]:
    """Convert persisted attachments into Agno media objects."""
    audio: list[Audio] = []
    files: list[File] = []
    videos: list[Video] = []

    for record in attachment_records:
        if not record.local_path.is_file():
            continue
        if record.kind == "audio":
            audio.append(
                Audio(
                    filepath=str(record.local_path),
                    mime_type=record.mime_type,
                ),
            )
        elif record.kind == "file":
            try:
                file_media = File(
                    filepath=str(record.local_path),
                    mime_type=record.mime_type,
                    filename=record.filename,
                )
            except Exception:
                # Agno validates file MIME types against a strict allow-list.
                # Fall back to filepath+filename so arbitrary attachments still work.
                file_media = File(
                    filepath=str(record.local_path),
                    filename=record.filename,
                )
            files.append(file_media)
        elif record.kind == "video":
            videos.append(
                Video(
                    filepath=str(record.local_path),
                    mime_type=record.mime_type,
                ),
            )

    return audio, files, videos


def resolve_attachment_media(
    storage_path: Path,
    attachment_ids: list[str],
) -> tuple[list[str], list[Audio], list[File], list[Video]]:
    """Resolve attachment IDs into Agno media objects."""
    attachment_records = resolve_attachments(storage_path, attachment_ids)
    resolved_attachment_ids = [record.attachment_id for record in attachment_records]
    attachment_audio, attachment_files, attachment_videos = attachment_records_to_media(attachment_records)
    return resolved_attachment_ids, attachment_audio, attachment_files, attachment_videos
