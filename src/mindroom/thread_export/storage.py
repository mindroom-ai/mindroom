"""Thread-export document serialization and filesystem reconciliation."""

from __future__ import annotations

import json
import os
import shutil
import stat
from contextlib import suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import quote
from uuid import uuid4

import yaml

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.thread_export.models import ThreadExportRoom

_EXPORT_SCHEMA_VERSION = 1
_ROOM_INDEX_FILENAME = "index.json"
_THREAD_SUMMARY_CONTENT_KEY = "io.mindroom.thread_summary"
_DIRECTORY_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class _UnsafeThreadExportPathError(RuntimeError):
    """Raised when an export path could escape through a symlink."""


def _safe_path_segment(value: str) -> str:
    """Return one filesystem-safe path segment while keeping Matrix IDs reversible."""
    encoded = quote(value.strip() or "unknown", safe="")
    if encoded in {".", ".."}:
        return encoded.replace(".", "%2E")
    return encoded


def _unsafe_directory(path: Path, label: str) -> _UnsafeThreadExportPathError:
    """Return a normalized failure for an unsafe controlled directory component."""
    return _UnsafeThreadExportPathError(f"Refusing symlinked thread export {label}: {path}")


def _open_directory_at(
    parent_fd: int,
    name: str,
    *,
    path: Path,
    label: str,
    create: bool,
) -> int | None:
    """Open one directory relative to a pinned parent without following symlinks."""
    if create:
        try:
            os.mkdir(name, dir_fd=parent_fd)
        except FileExistsError:
            pass
        except OSError as exc:
            raise _unsafe_directory(path, label) from exc
    try:
        return os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        if not create:
            return None
        raise
    except OSError as exc:
        raise _unsafe_directory(path, label) from exc


def _open_export_root(output_dir: Path, *, create: bool) -> int | None:
    """Open and pin the export root so later operations cannot be redirected."""
    if create:
        output_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        parent_fd = os.open(output_dir.parent, _DIRECTORY_OPEN_FLAGS)
    except FileNotFoundError:
        if not create:
            return None
        raise
    except OSError as exc:
        raise _unsafe_directory(output_dir.parent, "root parent") from exc
    try:
        return _open_directory_at(
            parent_fd,
            output_dir.name,
            path=output_dir,
            label="root",
            create=create,
        )
    finally:
        os.close(parent_fd)


def _open_room_directory(
    root_fd: int,
    output_dir: Path,
    room: ThreadExportRoom,
    *,
    create: bool,
) -> int | None:
    """Open and pin one exporter-controlled room directory."""
    room_name = _safe_path_segment(room.key)
    return _open_directory_at(
        root_fd,
        room_name,
        path=output_dir / room_name,
        label="room directory",
        create=create,
    )


def _fsync_directory_fd(directory_fd: int) -> None:
    """Best-effort flush one already-pinned directory."""
    with suppress(OSError):
        os.fsync(directory_fd)


def _read_text_at(directory_fd: int, filename: str) -> str | None:
    """Read a regular file relative to a pinned directory without following symlinks."""
    try:
        file_fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            return None
        with os.fdopen(file_fd, encoding="utf-8") as file:
            file_fd = -1
            return file.read()
    except (OSError, UnicodeDecodeError):
        return None
    finally:
        if file_fd >= 0:
            os.close(file_fd)


def _atomic_write_at(directory_fd: int, filename: str, text: str) -> None:
    """Durably replace one file relative to an already-pinned directory."""
    temp_name = f".{filename}.{uuid4().hex}.tmp"
    temp_fd = -1
    try:
        temp_fd = os.open(
            temp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        with os.fdopen(temp_fd, mode="w", encoding="utf-8") as temp_file:
            temp_fd = -1
            temp_file.write(text)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_name, filename, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        _fsync_directory_fd(directory_fd)
    finally:
        if temp_fd >= 0:
            os.close(temp_fd)
        with suppress(FileNotFoundError):
            os.unlink(temp_name, dir_fd=directory_fd)


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


def _thread_index_entry_at(directory_fd: int, filename: str) -> tuple[int, dict[str, object]] | None:
    """Return one index pair from a thread file below a pinned room directory."""
    text = _read_text_at(directory_fd, filename)
    if text is None:
        return None
    try:
        payload = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    if not isinstance(payload, dict):
        return None
    thread = payload.get("thread")
    messages = payload.get("messages")
    if not isinstance(thread, dict) or not isinstance(messages, list):
        return None
    message_dicts = [message for message in messages if isinstance(message, dict)]
    entry: dict[str, object] = {
        "file": filename,
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


def _room_index_payload(room_fd: int, room: ThreadExportRoom) -> dict[str, object]:
    """Build one room index document from the exported thread files on disk."""
    indexed = [
        indexed_entry
        for filename in sorted(name for name in os.listdir(room_fd) if name.endswith(".yaml"))
        if (indexed_entry := _thread_index_entry_at(room_fd, filename)) is not None
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
    root_fd = _open_export_root(output_dir, create=False)
    if root_fd is None:
        return
    try:
        room_fd = _open_room_directory(root_fd, output_dir, room, create=False)
    finally:
        os.close(root_fd)
    if room_fd is None:
        return
    try:
        payload = _room_index_payload(room_fd, room)
        text = f"{json.dumps(payload, indent=2)}\n"
        if _read_text_at(room_fd, _ROOM_INDEX_FILENAME) == text:
            return
        _atomic_write_at(room_fd, _ROOM_INDEX_FILENAME, text)
    finally:
        os.close(room_fd)


def room_index_exists(output_dir: Path, room: ThreadExportRoom) -> bool:
    """Return whether a room has a regular index file inside a safe export directory."""
    root_fd = _open_export_root(output_dir, create=False)
    if root_fd is None:
        return False
    try:
        room_fd = _open_room_directory(root_fd, output_dir, room, create=False)
    finally:
        os.close(root_fd)
    if room_fd is None:
        return False
    try:
        try:
            return stat.S_ISREG(os.stat(_ROOM_INDEX_FILENAME, dir_fd=room_fd, follow_symlinks=False).st_mode)
        except FileNotFoundError:
            return False
    finally:
        os.close(room_fd)


def remove_room_export(output_dir: Path, room: ThreadExportRoom) -> bool:
    """Remove one room's exported data without following workspace symlinks."""
    root_fd = _open_export_root(output_dir, create=False)
    if root_fd is None:
        return False
    room_name = _safe_path_segment(room.key)
    try:
        try:
            mode = os.stat(room_name, dir_fd=root_fd, follow_symlinks=False).st_mode
        except FileNotFoundError:
            return False
        if stat.S_ISDIR(mode):
            if not shutil.rmtree.avoids_symlink_attacks:
                msg = "Safe descriptor-relative directory removal is unavailable"
                raise RuntimeError(msg)
            shutil.rmtree(room_name, dir_fd=root_fd)
        else:
            os.unlink(room_name, dir_fd=root_fd)
        _fsync_directory_fd(root_fd)
        return True
    finally:
        os.close(root_fd)


def remove_stale_thread_exports(
    output_dir: Path,
    room: ThreadExportRoom,
    thread_ids: Sequence[str],
) -> bool:
    """Remove thread files absent from a complete homeserver enumeration."""
    root_fd = _open_export_root(output_dir, create=False)
    if root_fd is None:
        return False
    try:
        room_fd = _open_room_directory(root_fd, output_dir, room, create=False)
    finally:
        os.close(root_fd)
    if room_fd is None:
        return False
    try:
        expected_names = {f"{_safe_path_segment(thread_id)}.yaml" for thread_id in thread_ids}
        stale_names = [
            name
            for name in os.listdir(room_fd)  # noqa: PTH208 - descriptor pinning prevents path races
            if name.endswith(".yaml") and name not in expected_names
        ]
        removed = False
        for filename in stale_names:
            try:
                mode = os.stat(filename, dir_fd=room_fd, follow_symlinks=False).st_mode
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(mode):
                continue
            os.unlink(filename, dir_fd=room_fd)
            removed = True
        if removed:
            _fsync_directory_fd(room_fd)
        return removed
    finally:
        os.close(room_fd)


def reconcile_room_directories(output_dir: Path, retained_room_keys: set[str]) -> None:
    """Remove room directories outside the target's full-pass authorization scope."""
    root_fd = _open_export_root(output_dir, create=False)
    if root_fd is None:
        return
    try:
        retained_names = {_safe_path_segment(room_key) for room_key in retained_room_keys}
        removed = False
        for name in os.listdir(root_fd):  # noqa: PTH208 - descriptor pinning prevents path races
            if name in retained_names:
                continue
            try:
                mode = os.stat(name, dir_fd=root_fd, follow_symlinks=False).st_mode
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(mode):
                os.unlink(name, dir_fd=root_fd)
            elif stat.S_ISDIR(mode):
                if not shutil.rmtree.avoids_symlink_attacks:
                    msg = "Safe descriptor-relative directory removal is unavailable"
                    raise RuntimeError(msg)
                shutil.rmtree(name, dir_fd=root_fd)
            else:
                continue
            removed = True
        if removed:
            _fsync_directory_fd(root_fd)
    finally:
        os.close(root_fd)


def _payload_without_exported_at(payload: dict[str, object]) -> dict[str, object]:
    """Return one thread payload with the per-pass exported_at timestamp removed."""
    normalized = dict(payload)
    thread = normalized.get("thread")
    if isinstance(thread, dict):
        normalized["thread"] = {key: value for key, value in thread.items() if key != "exported_at"}
    return normalized


def _existing_payload_matches(room_fd: int, filename: str, payload: dict[str, object]) -> bool:
    """Return whether one regular export file already holds this payload, ignoring exported_at."""
    text = _read_text_at(room_fd, filename)
    if text is None:
        return False
    try:
        existing = yaml.safe_load(text)
    except yaml.YAMLError:
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
    root_fd = _open_export_root(output_dir, create=True)
    if root_fd is None:
        msg = f"Failed to create thread export root: {output_dir}"
        raise RuntimeError(msg)
    try:
        room_fd = _open_room_directory(root_fd, output_dir, room, create=True)
    finally:
        os.close(root_fd)
    if room_fd is None:
        msg = f"Failed to create thread export room directory: {room.key}"
        raise RuntimeError(msg)
    try:
        filename = f"{_safe_path_segment(thread_id)}.yaml"
        if _existing_payload_matches(room_fd, filename, payload):
            return False
        text = yaml.safe_dump(
            payload,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
        )
        _atomic_write_at(room_fd, filename, text)
        return True
    finally:
        os.close(room_fd)
