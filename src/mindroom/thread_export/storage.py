"""Thread-export document serialization and filesystem reconciliation."""

from __future__ import annotations

import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING
from urllib.parse import quote

import yaml

from mindroom.durable_write import fsync_directory, write_json_file_durable

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.thread_export.models import ThreadExportRoom

_EXPORT_SCHEMA_VERSION = 1
_ROOM_INDEX_FILENAME = "index.json"
_THREAD_SUMMARY_CONTENT_KEY = "io.mindroom.thread_summary"


class _UnsafeThreadExportPathError(RuntimeError):
    """Raised when an export path could escape through a symlink."""


def _safe_path_segment(value: str) -> str:
    """Return one filesystem-safe path segment while keeping Matrix IDs reversible."""
    encoded = quote(value.strip() or "unknown", safe="")
    if encoded in {".", ".."}:
        return encoded.replace(".", "%2E")
    return encoded


def _require_real_directory(path: Path, *, label: str, create: bool) -> Path:
    """Return a real directory, rejecting a symlink at the controlled path component."""
    if path.is_symlink():
        msg = f"Refusing symlinked thread export {label}: {path}"
        raise _UnsafeThreadExportPathError(msg)
    if create:
        path.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            msg = f"Refusing symlinked thread export {label}: {path}"
            raise _UnsafeThreadExportPathError(msg)
    if not path.is_dir():
        msg = f"Thread export {label} is not a directory: {path}"
        raise _UnsafeThreadExportPathError(msg)
    return path


def _existing_export_root(output_dir: Path) -> Path | None:
    """Return an existing safe export root, or None when it does not exist."""
    if not output_dir.exists() and not output_dir.is_symlink():
        return None
    return _require_real_directory(output_dir, label="root", create=False)


def _room_export_dir(output_dir: Path, room: ThreadExportRoom, *, create: bool = False) -> Path:
    """Return a room directory after validating the exporter-controlled path components."""
    if not create:
        root = _existing_export_root(output_dir)
        if root is None:
            return output_dir / _safe_path_segment(room.key)
    else:
        root = _require_real_directory(output_dir, label="root", create=True)
    room_dir = root / _safe_path_segment(room.key)
    if not create and not room_dir.exists() and not room_dir.is_symlink():
        return room_dir
    return _require_real_directory(room_dir, label="room directory", create=create)


def _timestamp_iso(timestamp_ms: int) -> str | None:
    """Return UTC ISO timestamp for one Matrix millisecond timestamp."""
    if timestamp_ms <= 0:
        return None
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC).isoformat()


def _message_payload(message: ResolvedVisibleMessage) -> dict[str, object]:
    """Return one grep-friendly YAML message entry."""
    payload: dict[str, object] = {
        "event_id": message.event_id,
        "latest_event_id": message.latest_event_id,
        "sender": message.sender,
        "timestamp": message.timestamp,
        "body": message.body,
    }
    if timestamp_iso := _timestamp_iso(message.timestamp):
        payload["timestamp_iso"] = timestamp_iso
    if message.thread_id is not None:
        payload["thread_id"] = message.thread_id
    if message.reply_to_event_id is not None:
        payload["reply_to_event_id"] = message.reply_to_event_id
    if message.stream_status is not None:
        payload["stream_status"] = message.stream_status
    msgtype = message.content.get("msgtype")
    if isinstance(msgtype, str) and msgtype != "m.text":
        payload["msgtype"] = msgtype
    return payload


def _latest_thread_summary(messages: list[ResolvedVisibleMessage]) -> str | None:
    """Return the latest thread-summary notice text, when one exists."""
    for message in reversed(messages):
        meta = message.content.get(_THREAD_SUMMARY_CONTENT_KEY)
        if isinstance(meta, dict):
            summary = meta.get("summary")
            return summary if isinstance(summary, str) and summary else message.body
    return None


def thread_payload(
    *,
    room: ThreadExportRoom,
    thread_id: str,
    messages: list[ResolvedVisibleMessage],
    exported_at: datetime,
) -> dict[str, object]:
    """Build one YAML document for a Matrix thread."""
    thread_block: dict[str, object] = {
        "id": thread_id,
        "source": "matrix",
    }
    if summary := _latest_thread_summary(messages):
        thread_block["summary"] = summary
    thread_block["exported_at"] = exported_at.isoformat()
    thread_block["message_count"] = len(messages)
    return {
        "version": _EXPORT_SCHEMA_VERSION,
        "room": {
            "key": room.key,
            "id": room.room_id,
            "name": room.name,
            "alias": room.alias,
        },
        "thread": thread_block,
        "messages": [_message_payload(message) for message in messages],
    }


def _write_yaml_atomic(path: Path, payload: dict[str, object]) -> None:
    """Atomically write one YAML payload inside a validated room directory."""
    if path.parent.is_symlink():
        msg = f"Refusing symlinked thread export room directory: {path.parent}"
        raise _UnsafeThreadExportPathError(msg)
    temp_path: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            yaml.safe_dump(payload, temp_file, default_flow_style=False, sort_keys=False, allow_unicode=True)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        temp_path.replace(path)
        fsync_directory(path.parent)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def _thread_export_path(output_dir: Path, room: ThreadExportRoom, thread_id: str) -> Path:
    """Return a validated output path for one thread export."""
    room_dir = _room_export_dir(output_dir, room, create=True)
    return room_dir / f"{_safe_path_segment(thread_id)}.yaml"


def _thread_index_entry(thread_file: Path) -> tuple[int, dict[str, object]] | None:
    """Return one (last-activity, entry) index pair from an exported thread file."""
    if thread_file.is_symlink():
        return None
    try:
        payload = yaml.safe_load(thread_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
    if not isinstance(payload, dict):
        return None
    thread = payload.get("thread")
    messages = payload.get("messages")
    if not isinstance(thread, dict) or not isinstance(messages, list):
        return None
    message_dicts = [message for message in messages if isinstance(message, dict)]
    entry: dict[str, object] = {
        "file": thread_file.name,
        "thread_id": thread.get("id"),
        "message_count": thread.get("message_count"),
        "participants": sorted(
            {sender for message in message_dicts if isinstance(sender := message.get("sender"), str)},
        ),
    }
    summary = thread.get("summary")
    if isinstance(summary, str):
        entry["summary"] = summary
    last_timestamp = 0
    if message_dicts:
        last_message = message_dicts[-1]
        if isinstance(raw_timestamp := last_message.get("timestamp"), int):
            last_timestamp = raw_timestamp
            entry["last_timestamp"] = raw_timestamp
        if isinstance(timestamp_iso := last_message.get("timestamp_iso"), str):
            entry["last_timestamp_iso"] = timestamp_iso
    return last_timestamp, entry


def _room_index_payload(room_dir: Path, room: ThreadExportRoom) -> dict[str, object]:
    """Build one room index document from the exported thread files on disk."""
    indexed = [
        indexed_entry
        for thread_file in sorted(room_dir.glob("*.yaml"))
        if (indexed_entry := _thread_index_entry(thread_file)) is not None
    ]
    indexed.sort(key=lambda item: item[0], reverse=True)
    entries = [entry for _, entry in indexed]
    return {
        "version": _EXPORT_SCHEMA_VERSION,
        "room": {
            "key": room.key,
            "id": room.room_id,
            "name": room.name,
            "alias": room.alias,
        },
        "thread_count": len(entries),
        "threads": entries,
    }


def write_room_index(output_dir: Path, room: ThreadExportRoom) -> None:
    """Write one room's index.json when its content changed."""
    room_dir = _room_export_dir(output_dir, room)
    if not room_dir.exists():
        return
    index_path = room_dir / _ROOM_INDEX_FILENAME
    payload = _room_index_payload(room_dir, room)
    text = f"{json.dumps(payload, indent=2)}\n"
    if index_path.is_symlink():
        index_path.unlink()
    elif index_path.exists() and index_path.read_text(encoding="utf-8") == text:
        return
    write_json_file_durable(index_path, payload, indent=2, trailing_newline=True)


def room_index_exists(output_dir: Path, room: ThreadExportRoom) -> bool:
    """Return whether a room has a regular index file inside a safe export directory."""
    room_dir = _room_export_dir(output_dir, room)
    if not room_dir.exists():
        return False
    index_path = room_dir / _ROOM_INDEX_FILENAME
    return index_path.is_file() and not index_path.is_symlink()


def remove_room_export(output_dir: Path, room: ThreadExportRoom) -> bool:
    """Remove one room's exported data without following workspace symlinks."""
    root = _existing_export_root(output_dir)
    if root is None:
        return False
    room_dir = root / _safe_path_segment(room.key)
    if not room_dir.exists() and not room_dir.is_symlink():
        return False
    if room_dir.is_symlink() or not room_dir.is_dir():
        room_dir.unlink()
    else:
        shutil.rmtree(room_dir)
    fsync_directory(root)
    return True


def remove_stale_thread_exports(
    output_dir: Path,
    room: ThreadExportRoom,
    thread_ids: Sequence[str],
) -> bool:
    """Remove thread files absent from a complete homeserver enumeration."""
    root = _existing_export_root(output_dir)
    if root is None:
        return False
    room_dir = _room_export_dir(root, room)
    if not room_dir.exists():
        return False
    expected_names = {f"{_safe_path_segment(thread_id)}.yaml" for thread_id in thread_ids}
    stale_files = [thread_file for thread_file in room_dir.glob("*.yaml") if thread_file.name not in expected_names]
    for thread_file in stale_files:
        thread_file.unlink()
    if stale_files:
        fsync_directory(room_dir)
    return bool(stale_files)


def reconcile_room_directories(output_dir: Path, retained_room_keys: set[str]) -> None:
    """Remove room directories outside the target's full-pass authorization scope."""
    root = _existing_export_root(output_dir)
    if root is None:
        return
    retained_names = {_safe_path_segment(room_key) for room_key in retained_room_keys}
    removed = False
    for candidate in root.iterdir():
        if candidate.name in retained_names:
            continue
        if candidate.is_symlink():
            candidate.unlink()
        elif candidate.is_dir():
            shutil.rmtree(candidate)
        else:
            continue
        removed = True
    if removed:
        fsync_directory(root)


def _payload_without_exported_at(payload: dict[str, object]) -> dict[str, object]:
    """Return one thread payload with the per-pass exported_at timestamp removed."""
    normalized = dict(payload)
    thread = normalized.get("thread")
    if isinstance(thread, dict):
        normalized["thread"] = {key: value for key, value in thread.items() if key != "exported_at"}
    return normalized


def _existing_payload_matches(path: Path, payload: dict[str, object]) -> bool:
    """Return whether one regular export file already holds this payload, ignoring exported_at."""
    if path.is_symlink() or not path.is_file():
        return False
    try:
        existing = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return False
    if not isinstance(existing, dict):
        return False
    return _payload_without_exported_at(existing) == _payload_without_exported_at(payload)


def write_thread_payload(
    output_dir: Path,
    room: ThreadExportRoom,
    thread_id: str,
    payload: dict[str, object],
) -> bool:
    """Write one thread payload when changed and return whether bytes were replaced."""
    export_path = _thread_export_path(output_dir, room, thread_id)
    if _existing_payload_matches(export_path, payload):
        return False
    _write_yaml_atomic(export_path, payload)
    return True
