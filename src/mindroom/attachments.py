"""Attachment persistence and media conversion helpers."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import mimetypes
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import nio

from .constants import ATTACHMENT_IDS_KEY, VOICE_RAW_AUDIO_FALLBACK_KEY
from .logging_config import get_logger
from .matrix.media import download_media_bytes, media_mime_type

logger = get_logger(__name__)

AttachmentKind = Literal["audio", "file", "video"]
FileOrVideoEvent = nio.RoomMessageFile | nio.RoomEncryptedFile | nio.RoomMessageVideo | nio.RoomEncryptedVideo
_ATTACHMENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,127}$")
_ATTACHMENT_RETENTION_DAYS = 30
_CLEANUP_INTERVAL = timedelta(hours=1)
_last_cleanup_time_by_storage_path: dict[Path, datetime] = {}


def _normalize_attachment_id(raw_attachment_id: str) -> str | None:
    """Normalize attachment IDs and reject unsafe values."""
    attachment_id = raw_attachment_id.strip()
    if not attachment_id or not _ATTACHMENT_ID_PATTERN.fullmatch(attachment_id):
        return None
    return attachment_id


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
        attachment_id = _normalize_attachment_id(raw_attachment_id)
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


def _incoming_media_dir(storage_path: Path) -> Path:
    return storage_path / "incoming_media"


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


def _store_media_bytes_locally(
    storage_path: Path,
    event_id: str,
    media_bytes: bytes | None,
    mime_type: str | None,
) -> Path | None:
    """Persist media bytes to storage so agents can access them as files."""
    if media_bytes is None:
        return None
    incoming_media_dir = _incoming_media_dir(storage_path)
    safe_name = attachment_id_for_event(event_id)
    extension = _extension_from_mime_type(mime_type)
    media_path = incoming_media_dir / f"{safe_name}{extension}"
    try:
        incoming_media_dir.mkdir(parents=True, exist_ok=True)
        media_path.write_bytes(media_bytes)
    except OSError:
        logger.exception("Failed to persist media payload")
        return None
    return media_path


async def _store_media_bytes_locally_async(
    storage_path: Path,
    event_id: str,
    media_bytes: bytes | None,
    mime_type: str | None,
) -> Path | None:
    """Persist media bytes without blocking the event loop."""
    return await asyncio.to_thread(
        _store_media_bytes_locally,
        storage_path,
        event_id,
        media_bytes,
        mime_type,
    )


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


async def _store_file_or_video_locally(
    client: nio.AsyncClient,
    storage_path: Path,
    event: FileOrVideoEvent,
) -> Path | None:
    """Download and persist file/video media to local storage."""
    media_bytes = await download_media_bytes(client, event)
    return await _store_media_bytes_locally_async(
        storage_path,
        event.event_id,
        media_bytes,
        media_mime_type(event),
    )


def attachment_id_for_event(event_id: str) -> str:
    """Create a stable low-collision attachment ID from a Matrix event ID."""
    digest = hashlib.sha256(event_id.encode("utf-8")).hexdigest()
    return f"att_{digest[:24]}"


def _record_mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, UTC)
    except OSError:
        return None


def _record_created_at(record: AttachmentRecord, record_path: Path) -> datetime | None:
    if isinstance(record.created_at, str) and record.created_at:
        with contextlib.suppress(ValueError):
            parsed = datetime.fromisoformat(record.created_at)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC)
    return _record_mtime(record_path)


def _is_managed_media_path(storage_path: Path, local_path: Path) -> bool:
    """Return whether *local_path* lives under this storage's incoming media dir."""
    incoming_media_dir = _incoming_media_dir(storage_path).resolve()
    with contextlib.suppress(OSError):
        return local_path.resolve().is_relative_to(incoming_media_dir)
    return False


def _collect_attachment_cleanup_state(
    storage_path: Path,
    *,
    cutoff: datetime,
) -> tuple[
    set[Path],
    dict[Path, int],
    list[tuple[AttachmentRecord, Path]],
    list[Path],
]:
    """Collect active/expired attachment metadata needed for cleanup."""
    active_media_paths: set[Path] = set()
    active_media_ref_counts: dict[Path, int] = {}
    expired_records: list[tuple[AttachmentRecord, Path]] = []
    stale_record_paths: list[Path] = []

    for record_path in _attachments_dir(storage_path).glob("*.json"):
        record = load_attachment(storage_path, record_path.stem)
        if record is None:
            record_mtime = _record_mtime(record_path)
            if record_mtime is not None and record_mtime < cutoff:
                stale_record_paths.append(record_path)
            continue

        created_at = _record_created_at(record, record_path)
        if created_at is not None and created_at < cutoff:
            expired_records.append((record, record_path))
            continue

        resolved_media_path = record.local_path.resolve()
        active_media_paths.add(resolved_media_path)
        active_media_ref_counts[resolved_media_path] = active_media_ref_counts.get(resolved_media_path, 0) + 1

    return active_media_paths, active_media_ref_counts, expired_records, stale_record_paths


def _remove_paths(paths: list[Path]) -> int:
    """Delete filesystem paths, ignoring filesystem errors."""
    removed = 0
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            continue
        removed += 1
    return removed


def _collect_removable_media_paths(
    storage_path: Path,
    *,
    expired_records: list[tuple[AttachmentRecord, Path]],
    active_media_ref_counts: dict[Path, int],
) -> set[Path]:
    """Return expired managed media files that are no longer referenced."""
    removable_media_paths: set[Path] = set()
    for record, record_path in expired_records:
        with contextlib.suppress(OSError):
            record_path.unlink(missing_ok=True)

        resolved_media_path = record.local_path.resolve()
        if not _is_managed_media_path(storage_path, resolved_media_path):
            continue
        if active_media_ref_counts.get(resolved_media_path, 0) == 0:
            removable_media_paths.add(resolved_media_path)
    return removable_media_paths


def _prune_orphan_incoming_media(
    storage_path: Path,
    *,
    cutoff: datetime,
    active_media_paths: set[Path],
) -> int:
    """Delete old incoming-media files that are no longer referenced."""
    incoming_media_dir = _incoming_media_dir(storage_path)
    if not incoming_media_dir.is_dir():
        return 0

    removed = 0
    for media_path in incoming_media_dir.iterdir():
        if not media_path.is_file():
            continue
        resolved_media_path = media_path.resolve()
        if resolved_media_path in active_media_paths:
            continue
        media_mtime = _record_mtime(media_path)
        if media_mtime is None or media_mtime >= cutoff:
            continue
        try:
            media_path.unlink(missing_ok=True)
        except OSError:
            continue
        removed += 1
    return removed


def _cleanup_attachment_storage(storage_path: Path) -> None:
    """Prune expired attachment metadata and managed media files."""
    attachments_dir = _attachments_dir(storage_path)
    if not attachments_dir.is_dir():
        return

    cutoff = datetime.now(UTC) - timedelta(days=_ATTACHMENT_RETENTION_DAYS)
    active_media_paths, active_media_ref_counts, expired_records, stale_record_paths = (
        _collect_attachment_cleanup_state(
            storage_path,
            cutoff=cutoff,
        )
    )
    stale_records_removed = _remove_paths(stale_record_paths)

    removable_media_paths = _collect_removable_media_paths(
        storage_path,
        expired_records=expired_records,
        active_media_ref_counts=active_media_ref_counts,
    )
    expired_media_removed = _remove_paths(list(removable_media_paths))
    orphan_media_removed = _prune_orphan_incoming_media(
        storage_path,
        cutoff=cutoff,
        active_media_paths=active_media_paths,
    )
    logger.debug(
        "Attachment cleanup completed",
        stale_records_removed=stale_records_removed,
        expired_records_removed=len(expired_records),
        expired_media_removed=expired_media_removed,
        orphan_media_removed=orphan_media_removed,
        active_media_paths=len(active_media_paths),
    )


def _maybe_cleanup_attachment_storage(storage_path: Path) -> None:
    """Run cleanup at most once per ``_CLEANUP_INTERVAL``."""
    resolved_storage_path = storage_path.resolve()
    now = datetime.now(UTC)
    last_cleanup_time = _last_cleanup_time_by_storage_path.get(resolved_storage_path)
    if last_cleanup_time is not None and now - last_cleanup_time < _CLEANUP_INTERVAL:
        return
    try:
        _cleanup_attachment_storage(resolved_storage_path)
        _last_cleanup_time_by_storage_path[resolved_storage_path] = now
    except Exception:
        logger.exception("Failed to prune expired attachment storage")


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

    resolved_attachment_id = attachment_id or f"att_{uuid4().hex[:16]}"
    normalized_attachment_id = _normalize_attachment_id(resolved_attachment_id)
    if normalized_attachment_id is None:
        logger.warning("Invalid attachment ID", attachment_id=resolved_attachment_id)
        return None

    record = AttachmentRecord(
        attachment_id=normalized_attachment_id,
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

    record_path = _attachment_record_path(storage_path, normalized_attachment_id)
    tmp_path = record_path.with_suffix(f".{uuid4().hex[:8]}.tmp")
    try:
        record_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(json.dumps(record.to_payload(), sort_keys=True), encoding="utf-8")
        tmp_path.replace(record_path)
    except OSError:
        logger.exception("Failed to persist attachment metadata", attachment_id=normalized_attachment_id)
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        return None

    _maybe_cleanup_attachment_storage(storage_path)

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
    local_media_path = await _store_file_or_video_locally(client, storage_path, event)
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


async def register_audio_attachment(
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
    local_audio_path = await _store_media_bytes_locally_async(
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
    normalized_attachment_id = _normalize_attachment_id(attachment_id)
    if normalized_attachment_id is None:
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
    if kind not in {"audio", "file", "video"} or not isinstance(local_path, str) or not local_path:
        return None

    filename = raw_payload.get("filename")
    mime_type = raw_payload.get("mime_type")
    room_id = raw_payload.get("room_id")
    thread_id = raw_payload.get("thread_id")
    source_event_id = raw_payload.get("source_event_id")
    sender = raw_payload.get("sender")
    size_bytes = raw_payload.get("size_bytes")
    created_at = raw_payload.get("created_at")

    return AttachmentRecord(
        attachment_id=normalized_attachment_id,
        local_path=Path(local_path),
        kind=kind,
        filename=filename if isinstance(filename, str) else None,
        mime_type=mime_type if isinstance(mime_type, str) else None,
        room_id=room_id if isinstance(room_id, str) else None,
        thread_id=thread_id if isinstance(thread_id, str) else None,
        source_event_id=source_event_id if isinstance(source_event_id, str) else None,
        sender=sender if isinstance(sender, str) else None,
        size_bytes=size_bytes if isinstance(size_bytes, int) else None,
        created_at=created_at if isinstance(created_at, str) else None,
    )


def resolve_attachments(storage_path: Path, attachment_ids: list[str]) -> list[AttachmentRecord]:
    """Resolve a list of attachment IDs into records, preserving order."""
    resolved: list[AttachmentRecord] = []
    seen_ids: set[str] = set()
    for attachment_id in attachment_ids:
        normalized_attachment_id = _normalize_attachment_id(attachment_id)
        if normalized_attachment_id is None or normalized_attachment_id in seen_ids:
            continue
        seen_ids.add(normalized_attachment_id)
        record = load_attachment(storage_path, normalized_attachment_id)
        if record is not None:
            resolved.append(record)
    return resolved


def filter_attachments_for_context(
    attachment_records: list[AttachmentRecord],
    *,
    room_id: str,
    thread_id: str | None = None,
) -> tuple[list[AttachmentRecord], list[str]]:
    """Keep only attachments registered for the current room/thread context."""
    allowed_records: list[AttachmentRecord] = []
    rejected_attachment_ids: list[str] = []
    for record in attachment_records:
        if record.room_id != room_id:
            rejected_attachment_ids.append(record.attachment_id)
            continue

        if thread_id is None:
            if record.thread_id is None:
                allowed_records.append(record)
            else:
                rejected_attachment_ids.append(record.attachment_id)
            continue

        if record.thread_id == thread_id:
            allowed_records.append(record)
        else:
            rejected_attachment_ids.append(record.attachment_id)
    return allowed_records, rejected_attachment_ids


async def resolve_thread_attachment_ids(
    client: nio.AsyncClient,
    storage_path: Path,
    *,
    room_id: str,
    thread_id: str,
    thread_root_event: nio.Event | None = None,
) -> list[str]:
    """Resolve attachment IDs from thread root event metadata or file/video payload.

    When *thread_root_event* is provided, the ``room_get_event`` round-trip
    is skipped, avoiding duplicate homeserver calls when the caller already
    fetched the root event for image/audio resolution.
    """
    event = thread_root_event
    if event is None:
        response = await client.room_get_event(room_id, thread_id)
        if not isinstance(response, nio.RoomGetEventResponse):
            return []
        event = response.event

    event_attachment_ids = parse_attachment_ids_from_event_source(event.source)
    if event_attachment_ids:
        return event_attachment_ids

    # Check for an existing attachment record for any media root (file, video,
    # or audio).  Audio roots are registered by the voice fallback handler
    # and can be looked up but not re-downloaded here.
    is_file_or_video = isinstance(
        event,
        nio.RoomMessageFile | nio.RoomEncryptedFile | nio.RoomMessageVideo | nio.RoomEncryptedVideo,
    )
    is_audio = isinstance(event, nio.RoomMessageAudio | nio.RoomEncryptedAudio)
    if not is_file_or_video and not is_audio:
        return []

    existing_attachment_id = attachment_id_for_event(event.event_id)
    existing_record = load_attachment(storage_path, existing_attachment_id)
    if (
        existing_record is not None
        and existing_record.room_id == room_id
        and existing_record.thread_id == thread_id
        and existing_record.local_path.is_file()
    ):
        return [existing_record.attachment_id]

    # Audio roots cannot be re-registered here (the voice handler owns that
    # lifecycle).  Only file/video roots can be lazily downloaded and stored.
    if not is_file_or_video:
        return []

    assert isinstance(
        event,
        nio.RoomMessageFile | nio.RoomEncryptedFile | nio.RoomMessageVideo | nio.RoomEncryptedVideo,
    )
    record = await register_file_or_video_attachment(
        client,
        storage_path,
        room_id=room_id,
        thread_id=thread_id,
        event=event,
    )
    return [record.attachment_id] if record is not None else []


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
