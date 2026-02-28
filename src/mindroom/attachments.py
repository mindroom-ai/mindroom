"""Attachment persistence and media conversion helpers."""

from __future__ import annotations

import json
import mimetypes
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import nio
from agno.media import Audio, File, Video
from nio import crypto

from .constants import ATTACHMENT_IDS_KEY, VOICE_RAW_AUDIO_FALLBACK_KEY
from .logging_config import get_logger

logger = get_logger(__name__)

AttachmentKind = Literal["audio", "file", "video", "image"]
FileOrVideoEvent = nio.RoomMessageFile | nio.RoomEncryptedFile | nio.RoomMessageVideo | nio.RoomEncryptedVideo


@dataclass(frozen=True)
class AttachmentRecord:
    """Persistent metadata for an attachment stored on local disk."""

    attachment_id: str
    local_path: Path
    kind: AttachmentKind
    filename: str | None = None
    mime_type: str | None = None
    room_id: str | None = None
    thread_id: str | None = None
    source_event_id: str | None = None
    sender: str | None = None
    size_bytes: int | None = None
    created_at: str | None = None

    def to_payload(self) -> dict[str, Any]:
        """Serialize record into a JSON-safe dictionary."""
        return {
            "attachment_id": self.attachment_id,
            "local_path": str(self.local_path),
            "kind": self.kind,
            "filename": self.filename,
            "mime_type": self.mime_type,
            "room_id": self.room_id,
            "thread_id": self.thread_id,
            "source_event_id": self.source_event_id,
            "sender": self.sender,
            "size_bytes": self.size_bytes,
            "created_at": self.created_at,
        }


def parse_attachment_ids_from_event_source(event_source: dict[str, Any] | None) -> list[str]:
    """Extract attachment IDs from Matrix event content metadata."""
    if not isinstance(event_source, dict):
        return []
    content = event_source.get("content")
    if not isinstance(content, dict):
        return []
    raw_attachment_ids = content.get(ATTACHMENT_IDS_KEY)
    if not isinstance(raw_attachment_ids, list):
        return []
    normalized: list[str] = []
    for raw_attachment_id in raw_attachment_ids:
        if not isinstance(raw_attachment_id, str):
            continue
        attachment_id = raw_attachment_id.strip()
        if attachment_id and attachment_id not in normalized:
            normalized.append(attachment_id)
    return normalized


def merge_attachment_ids(*attachment_id_lists: list[str]) -> list[str]:
    """Merge attachment IDs preserving first-seen order."""
    merged: list[str] = []
    for attachment_ids in attachment_id_lists:
        for attachment_id in attachment_ids:
            if attachment_id and attachment_id not in merged:
                merged.append(attachment_id)
    return merged


def is_voice_raw_audio_fallback(event_source: dict[str, Any] | None) -> bool:
    """Return whether this message carries raw-audio fallback metadata."""
    if not isinstance(event_source, dict):
        return False
    content = event_source.get("content")
    if not isinstance(content, dict):
        return False
    return bool(content.get(VOICE_RAW_AUDIO_FALLBACK_KEY, False))


def append_attachment_ids_prompt(prompt: str, attachment_ids: list[str]) -> str:
    """Append attachment guidance to a prompt when attachment IDs are available."""
    if not attachment_ids:
        return prompt
    return (
        f"{prompt}\n\nAvailable attachment IDs: {', '.join(attachment_ids)}. Use tool calls to inspect or process them."
    )


def _attachments_dir(storage_path: Path) -> Path:
    return storage_path / "attachments"


def _attachment_record_path(storage_path: Path, attachment_id: str) -> Path:
    return _attachments_dir(storage_path) / f"{attachment_id}.json"


def _extension_from_mime_type(mime_type: str | None) -> str:
    """Map MIME type to a stable file extension."""
    if not mime_type:
        return ".bin"
    normalized_mime_type = mime_type.split(";", 1)[0].strip().lower()
    extension = mimetypes.guess_extension(normalized_mime_type)
    if extension:
        return extension
    return ".bin"


def store_media_bytes_locally(
    storage_path: Path,
    event_id: str,
    media_bytes: bytes | None,
    mime_type: str | None,
) -> Path | None:
    """Persist media bytes to storage so agents can access them as files."""
    if media_bytes is None:
        return None
    incoming_media_dir = storage_path / "incoming_media"
    safe_event_id = "".join(ch if ch.isalnum() else "_" for ch in event_id).strip("_") or "media_event"
    extension = _extension_from_mime_type(mime_type)
    media_path = incoming_media_dir / f"{safe_event_id}{extension}"
    try:
        incoming_media_dir.mkdir(parents=True, exist_ok=True)
        media_path.write_bytes(media_bytes)
    except OSError:
        logger.exception("Failed to persist media payload")
        return None
    return media_path


def media_mime_type(
    event: nio.RoomMessageMedia | nio.RoomEncryptedMedia,
) -> str | None:
    """Extract MIME type from Matrix media events."""
    if isinstance(event, nio.RoomEncryptedMedia):
        mimetype = getattr(event, "mimetype", None)
        if isinstance(mimetype, str) and mimetype:
            return mimetype
    content = event.source.get("content", {})
    info = content.get("info", {}) if isinstance(content, dict) else {}
    mimetype = info.get("mimetype") if isinstance(info, dict) else None
    return mimetype if isinstance(mimetype, str) and mimetype else None


def extract_file_or_video_caption(
    event: FileOrVideoEvent,
) -> str:
    """Extract user caption for file/video events using MSC2530 semantics."""
    content = event.source.get("content", {})
    filename = content.get("filename")
    body = event.body
    if filename and filename != body and body:
        return body
    if isinstance(event, nio.RoomMessageVideo | nio.RoomEncryptedVideo):
        return "[Attached video]"
    return "[Attached file]"


def _decrypt_encrypted_media_bytes(event: nio.RoomEncryptedMedia, encrypted_bytes: bytes) -> bytes | None:
    """Decrypt encrypted Matrix media payload bytes."""
    try:
        key = event.source["content"]["file"]["key"]["k"]
        sha256 = event.source["content"]["file"]["hashes"]["sha256"]
        iv = event.source["content"]["file"]["iv"]
    except (KeyError, TypeError):
        logger.exception("Encrypted media payload missing decryption fields", event_id=event.event_id)
        return None

    try:
        return crypto.attachments.decrypt_attachment(encrypted_bytes, key, sha256, iv)
    except Exception:
        logger.exception("Media decryption failed", event_id=event.event_id)
        return None


async def download_media_bytes(
    client: nio.AsyncClient,
    event: nio.RoomMessageMedia | nio.RoomEncryptedMedia,
) -> bytes | None:
    """Download and decrypt Matrix media payload bytes."""
    try:
        response = await client.download(event.url)
    except Exception:
        logger.exception("Error downloading media")
        return None

    if isinstance(response, nio.DownloadError):
        logger.error("Media download failed", event_id=event.event_id, error=str(response))
        return None
    if not isinstance(response.body, bytes):
        logger.error("Media download returned non-bytes payload", event_id=event.event_id)
        return None

    if isinstance(event, nio.RoomEncryptedMedia):
        return _decrypt_encrypted_media_bytes(event, response.body)
    return response.body


async def store_file_or_video_locally(
    client: nio.AsyncClient,
    storage_path: Path,
    event: FileOrVideoEvent,
) -> Path | None:
    """Download and persist file/video media to local storage."""
    media_bytes = await download_media_bytes(client, event)
    return store_media_bytes_locally(storage_path, event.event_id, media_bytes, media_mime_type(event))


def attachment_id_for_event(event_id: str) -> str:
    """Create a stable attachment ID from a Matrix event ID."""
    normalized = "".join(ch for ch in event_id if ch.isalnum())
    if not normalized:
        normalized = uuid4().hex
    return f"att_{normalized[:32]}"


def register_local_attachment(
    storage_path: Path,
    local_path: Path,
    *,
    kind: AttachmentKind,
    attachment_id: str | None = None,
    filename: str | None = None,
    mime_type: str | None = None,
    room_id: str | None = None,
    thread_id: str | None = None,
    source_event_id: str | None = None,
    sender: str | None = None,
) -> AttachmentRecord | None:
    """Register a local file as an attachment and persist metadata."""
    if not local_path.is_file():
        logger.warning("Attachment path does not exist", path=str(local_path), kind=kind)
        return None

    try:
        size_bytes = local_path.stat().st_size
    except OSError:
        logger.exception("Failed to read attachment file metadata", path=str(local_path))
        return None

    attachment_id = attachment_id or f"att_{uuid4().hex[:16]}"
    record = AttachmentRecord(
        attachment_id=attachment_id,
        local_path=local_path.resolve(),
        kind=kind,
        filename=filename,
        mime_type=mime_type,
        room_id=room_id,
        thread_id=thread_id,
        source_event_id=source_event_id,
        sender=sender,
        size_bytes=size_bytes,
        created_at=datetime.now(UTC).isoformat(),
    )

    record_path = _attachment_record_path(storage_path, attachment_id)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = record_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(record.to_payload(), sort_keys=True), encoding="utf-8")
    tmp_path.replace(record_path)
    return record


async def register_file_or_video_attachment(
    client: nio.AsyncClient,
    storage_path: Path,
    *,
    room_id: str,
    thread_id: str | None,
    event: FileOrVideoEvent,
) -> AttachmentRecord | None:
    """Persist a file/video event and register it as an attachment record."""
    local_media_path = await store_file_or_video_locally(client, storage_path, event)
    if local_media_path is None:
        return None

    content = event.source.get("content", {})
    filename = content.get("filename")
    if not isinstance(filename, str) or not filename:
        filename = event.body
    kind: AttachmentKind = "video" if isinstance(event, nio.RoomMessageVideo | nio.RoomEncryptedVideo) else "file"

    return register_local_attachment(
        storage_path,
        local_media_path,
        kind=kind,
        attachment_id=attachment_id_for_event(event.event_id),
        filename=filename if isinstance(filename, str) else None,
        mime_type=media_mime_type(event),
        room_id=room_id,
        thread_id=thread_id,
        source_event_id=event.event_id,
        sender=event.sender,
    )


def register_audio_attachment(
    storage_path: Path,
    *,
    event_id: str,
    audio_bytes: bytes | None,
    mime_type: str | None,
    room_id: str,
    thread_id: str | None,
    sender: str,
    filename: str | None = None,
) -> AttachmentRecord | None:
    """Persist raw audio bytes and register them as an attachment record."""
    local_audio_path = store_media_bytes_locally(
        storage_path,
        event_id,
        audio_bytes,
        mime_type,
    )
    if local_audio_path is None:
        return None
    return register_local_attachment(
        storage_path,
        local_audio_path,
        kind="audio",
        attachment_id=attachment_id_for_event(event_id),
        filename=filename,
        mime_type=mime_type,
        room_id=room_id,
        thread_id=thread_id,
        source_event_id=event_id,
        sender=sender,
    )


def load_attachment(storage_path: Path, attachment_id: str) -> AttachmentRecord | None:
    """Load attachment metadata by ID."""
    normalized_attachment_id = attachment_id.strip()
    if not normalized_attachment_id:
        return None
    record_path = _attachment_record_path(storage_path, normalized_attachment_id)
    if not record_path.is_file():
        return None

    try:
        raw_payload = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.exception("Failed to parse attachment metadata", attachment_id=normalized_attachment_id)
        return None

    if not isinstance(raw_payload, dict):
        return None

    kind = raw_payload.get("kind")
    local_path = raw_payload.get("local_path")
    if kind not in {"audio", "file", "video", "image"} or not isinstance(local_path, str) or not local_path:
        return None

    return AttachmentRecord(
        attachment_id=normalized_attachment_id,
        local_path=Path(local_path),
        kind=kind,
        filename=raw_payload.get("filename") if isinstance(raw_payload.get("filename"), str) else None,
        mime_type=raw_payload.get("mime_type") if isinstance(raw_payload.get("mime_type"), str) else None,
        room_id=raw_payload.get("room_id") if isinstance(raw_payload.get("room_id"), str) else None,
        thread_id=raw_payload.get("thread_id") if isinstance(raw_payload.get("thread_id"), str) else None,
        source_event_id=(
            raw_payload.get("source_event_id") if isinstance(raw_payload.get("source_event_id"), str) else None
        ),
        sender=raw_payload.get("sender") if isinstance(raw_payload.get("sender"), str) else None,
        size_bytes=raw_payload.get("size_bytes") if isinstance(raw_payload.get("size_bytes"), int) else None,
        created_at=raw_payload.get("created_at") if isinstance(raw_payload.get("created_at"), str) else None,
    )


def resolve_attachments(storage_path: Path, attachment_ids: list[str]) -> list[AttachmentRecord]:
    """Resolve a list of attachment IDs into records, preserving order."""
    resolved: list[AttachmentRecord] = []
    seen_ids: set[str] = set()
    for attachment_id in attachment_ids:
        normalized_attachment_id = attachment_id.strip()
        if not normalized_attachment_id or normalized_attachment_id in seen_ids:
            continue
        seen_ids.add(normalized_attachment_id)
        record = load_attachment(storage_path, normalized_attachment_id)
        if record is not None:
            resolved.append(record)
    return resolved


async def resolve_thread_attachment_ids(
    client: nio.AsyncClient,
    storage_path: Path,
    *,
    room_id: str,
    thread_id: str,
) -> list[str]:
    """Resolve attachment IDs from thread root event metadata or file/video payload."""
    response = await client.room_get_event(room_id, thread_id)
    if not isinstance(response, nio.RoomGetEventResponse):
        return []
    event = response.event

    event_attachment_ids = parse_attachment_ids_from_event_source(event.source)
    if event_attachment_ids:
        return event_attachment_ids

    if not isinstance(
        event,
        nio.RoomMessageFile | nio.RoomEncryptedFile | nio.RoomMessageVideo | nio.RoomEncryptedVideo,
    ):
        return []

    record = await register_file_or_video_attachment(
        client,
        storage_path,
        room_id=room_id,
        thread_id=thread_id,
        event=event,
    )
    if record is None:
        return []
    return [record.attachment_id]


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


def attachments_for_tool_payload(
    attachment_records: list[AttachmentRecord],
    *,
    include_local_path: bool = True,
) -> list[dict[str, Any]]:
    """Render attachment records for tool JSON responses."""
    payloads: list[dict[str, Any]] = []
    for record in attachment_records:
        payload: dict[str, Any] = {
            "attachment_id": record.attachment_id,
            "kind": record.kind,
            "filename": record.filename,
            "mime_type": record.mime_type,
            "size_bytes": record.size_bytes,
            "room_id": record.room_id,
            "thread_id": record.thread_id,
            "source_event_id": record.source_event_id,
            "available": record.local_path.is_file(),
        }
        if include_local_path:
            payload["local_path"] = str(record.local_path)
        payloads.append(payload)
    return payloads
