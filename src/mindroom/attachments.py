"""Attachment persistence and media conversion helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from agno.media import Audio, File, Video

from .constants import ATTACHMENT_IDS_KEY
from .logging_config import get_logger

logger = get_logger(__name__)

AttachmentKind = Literal["audio", "file", "video", "image"]


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


def _attachments_dir(storage_path: Path) -> Path:
    return storage_path / "attachments"


def _attachment_record_path(storage_path: Path, attachment_id: str) -> Path:
    return _attachments_dir(storage_path) / f"{attachment_id}.json"


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
