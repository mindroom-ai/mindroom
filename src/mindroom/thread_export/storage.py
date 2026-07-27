"""Thread-export document serialization and filesystem reconciliation."""

from __future__ import annotations

import json
import os
import shutil
import stat
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote, unquote
from uuid import uuid4

import yaml

from mindroom import yaml_io
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.thread_export.models import ThreadExportRoom

_EXPORT_SCHEMA_VERSION = 1
_ROOM_INDEX_FILENAME = "index.json"
_ROOT_MARKER_FILENAME = ".mindroom-thread-exports"
_ROOT_MARKER_TEXT = '{"format":"mindroom-thread-exports","version":1}\n'
_THREAD_SUMMARY_CONTENT_KEY = "io.mindroom.thread_summary"
_DIRECTORY_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


logger = get_logger(__name__)


class _UnsafeThreadExportPathError(RuntimeError):
    """Raised when an export path is unsafe or lacks ownership proof."""


def _safe_path_segment(value: str) -> str:
    """Return one filesystem-safe path segment while keeping Matrix IDs reversible."""
    encoded = quote(value.strip() or "unknown", safe="")
    if encoded in {".", ".."}:
        return encoded.replace(".", "%2E")
    return encoded


def _room_path_segment(room_key: str) -> str:
    """Return a room segment outside the export-root marker namespace."""
    encoded = _safe_path_segment(room_key)
    if encoded == _ROOT_MARKER_FILENAME:
        return f"%2E{encoded[1:]}"
    return encoded


def _is_encoded_room_segment(value: str) -> bool:
    """Return whether a directory name is one canonical room segment."""
    if value == _ROOT_MARKER_FILENAME:
        return False
    try:
        return _room_path_segment(unquote(value, errors="strict")) == value
    except UnicodeError:
        return False


def _is_thread_export_filename(value: str) -> bool:
    """Return whether a filename is one canonical Matrix thread export."""
    if not value.endswith(".yaml"):
        return False
    stem = value.removesuffix(".yaml")
    try:
        thread_id = unquote(stem, errors="strict")
    except UnicodeError:
        return False
    return thread_id.startswith("$") and f"{_safe_path_segment(thread_id)}.yaml" == value


def _unsafe_directory(path: Path, label: str) -> _UnsafeThreadExportPathError:
    """Return a normalized failure for an unsafe controlled directory component."""
    return _UnsafeThreadExportPathError(f"Refusing symlinked thread export {label}: {path}")


def canonicalize_output_dir(output_dir: Path) -> Path:
    """Reject a broad authored path and return an absolute lexical path."""
    if output_dir.name in {"", ".", ".."}:
        msg = f"Thread export output must end in an explicit directory name, not '.' or '..': {output_dir}"
        raise _UnsafeThreadExportPathError(msg)
    return Path(os.path.abspath(output_dir))  # noqa: PTH100 - resolve() would hide a final symlink


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
    """Open the final export directory without following a symlink at that entry."""
    canonical_output_dir = canonicalize_output_dir(output_dir)
    if create:
        canonical_output_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        parent_fd = os.open(canonical_output_dir.parent, _DIRECTORY_OPEN_FLAGS)
    except FileNotFoundError:
        if not create:
            return None
        raise
    except OSError as exc:
        raise _unsafe_directory(canonical_output_dir.parent, "root parent") from exc
    try:
        return _open_directory_at(
            parent_fd,
            canonical_output_dir.name,
            path=canonical_output_dir,
            label="root",
            create=create,
        )
    finally:
        os.close(parent_fd)


def _regular_file_at(directory_fd: int, filename: str) -> bool:
    """Return whether one descriptor-relative entry is a regular file."""
    try:
        mode = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False).st_mode
    except FileNotFoundError:
        return False
    return stat.S_ISREG(mode)


def _atomic_temp_destination(filename: str) -> str | None:
    """Return the known-file destination encoded by an atomic-write temp name."""
    if not filename.startswith(".") or not filename.endswith(".tmp"):
        return None
    destination, separator, token = filename[1:-4].rpartition(".")
    if separator != "." or len(token) != 32 or any(character not in "0123456789abcdef" for character in token):
        return None
    return destination


def _is_room_export_temp_file(room_fd: int, filename: str) -> bool:
    """Return whether one regular file is exact residue from a room export write."""
    destination = _atomic_temp_destination(filename)
    return (
        destination is not None
        and (destination == _ROOM_INDEX_FILENAME or _is_thread_export_filename(destination))
        and _regular_file_at(room_fd, filename)
    )


def _is_room_export_file(room_fd: int, filename: str) -> bool:
    """Return whether one room entry is an exporter-owned regular file."""
    return (
        (filename == _ROOM_INDEX_FILENAME and _regular_file_at(room_fd, filename))
        or (_is_thread_export_filename(filename) and _regular_file_at(room_fd, filename))
        or _is_room_export_temp_file(room_fd, filename)
    )


def _room_contains_only_export_files(room_fd: int) -> bool:
    """Return whether one room directory contains an index and only exporter-owned files."""
    names = os.listdir(room_fd)
    return _regular_file_at(room_fd, _ROOM_INDEX_FILENAME) and all(
        _is_room_export_file(room_fd, name) for name in names
    )


def _contains_thread_export_file(room_fd: int) -> bool:
    """Return whether one pinned directory holds a committed Matrix thread export."""
    names = os.listdir(room_fd)
    return any(_is_thread_export_filename(name) and _regular_file_at(room_fd, name) for name in names)


def _is_thread_export_payload(room_fd: int, filename: str) -> bool:
    """Return whether one file parses as a MindRoom thread export document."""
    text = _read_text_at(room_fd, filename)
    if text is None:
        return False
    try:
        payload = yaml_io.safe_load(text)
    except yaml.YAMLError:
        return False
    if not isinstance(payload, dict) or not isinstance(payload.get("room"), dict):
        return False
    if not isinstance(payload.get("messages"), list):
        return False
    thread = payload.get("thread")
    return isinstance(thread, dict) and isinstance(thread.get("id"), str)


def _contains_valid_thread_export(room_fd: int) -> bool:
    """Return whether one pinned directory holds a thread export with MindRoom's own payload.

    Ownership evidence reads the document instead of trusting the filename, because a
    percent-encoded name like ``%24notes.yaml`` is something an unrelated directory can hold.
    """
    names = os.listdir(room_fd)
    return any(
        _is_thread_export_filename(name)
        and _regular_file_at(room_fd, name)
        and _is_thread_export_payload(room_fd, name)
        for name in names
    )


def _open_canonical_room_directory(root_fd: int, output_dir: Path, name: str) -> int | None:
    """Open one canonically named, non-symlinked room directory below a pinned root."""
    if not _is_encoded_room_segment(name):
        return None
    try:
        mode = os.stat(name, dir_fd=root_fd, follow_symlinks=False).st_mode
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(mode):
        return None
    return _open_directory_at(
        root_fd,
        name,
        path=output_dir / name,
        label="room directory",
        create=False,
    )


def _recognizable_room_directory(root_fd: int, output_dir: Path, name: str) -> bool:
    """Return whether one root entry holds an index and nothing the exporter does not own."""
    room_fd = _open_canonical_room_directory(root_fd, output_dir, name)
    if room_fd is None:
        return False
    try:
        return _room_contains_only_export_files(room_fd)
    finally:
        os.close(room_fd)


def _room_directory_with_thread_exports(root_fd: int, output_dir: Path, name: str) -> bool:
    """Return whether one root entry is a canonically named room holding a real thread export."""
    room_fd = _open_canonical_room_directory(root_fd, output_dir, name)
    if room_fd is None:
        return False
    try:
        return _contains_valid_thread_export(room_fd)
    finally:
        os.close(room_fd)


def _root_has_export_evidence(root_fd: int, output_dir: Path) -> bool:
    """Return whether an empty root, or one holding a thread export, proves exporter ownership.

    Evidence is a percent-encoded room directory holding a document that parses as one of this
    exporter's thread exports. Neither a generic ``index.json`` nor a thread-shaped filename is
    enough on its own, because a build directory can hold the former and any directory can hold
    a file named ``%24notes.yaml``; adopting on either would expose unrelated data to
    reconciliation.
    Unrelated entries beside real evidence do not veto ownership, because a stray ``.DS_Store``,
    ``.git`` directory, or operator note must not strand a real corpus, and every destructive
    path is independently scoped to exporter-owned entries.
    """
    names = os.listdir(root_fd)
    return not names or any(_room_directory_with_thread_exports(root_fd, output_dir, name) for name in names)


def _has_valid_export_root_marker(root_fd: int) -> bool:
    """Return whether the marker contains the exact supported ownership text."""
    try:
        marker_fd = os.open(_ROOT_MARKER_FILENAME, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root_fd)
    except FileNotFoundError:
        return False
    try:
        with os.fdopen(marker_fd, encoding="utf-8") as marker_file:
            marker_fd = -1
            return marker_file.read(len(_ROOT_MARKER_TEXT) + 1) == _ROOT_MARKER_TEXT
    finally:
        if marker_fd >= 0:
            os.close(marker_fd)


def _unowned_export_root(path: Path) -> _UnsafeThreadExportPathError:
    """Return a failure for a root without MindRoom ownership proof."""
    return _UnsafeThreadExportPathError(
        f"Refusing unowned thread export root: {path}; "
        f"the root must be empty, already contain an exported room directory, or contain a "
        f"{_ROOT_MARKER_FILENAME} file whose only line is {_ROOT_MARKER_TEXT.strip()}",
    )


def _claim_export_root(root_fd: int, output_dir: Path) -> None:
    """Install the marker on an empty root or one that already holds an exported room."""
    if _has_valid_export_root_marker(root_fd):
        return
    if _root_has_export_evidence(root_fd, output_dir):
        _atomic_write_at(root_fd, _ROOT_MARKER_FILENAME, _ROOT_MARKER_TEXT)
        return
    logger.warning(
        "Refusing to mark unrecognized thread export root",
        output_dir=str(output_dir),
    )
    raise _unowned_export_root(output_dir)


def _require_owned_export_root(root_fd: int, output_dir: Path) -> None:
    """Require the marker before a destructive operation."""
    if _has_valid_export_root_marker(root_fd):
        return
    logger.warning(
        "Refusing destructive operation on markerless thread export root",
        output_dir=str(output_dir),
    )
    raise _unowned_export_root(output_dir)


def prepare_export_root(output_dir: Path) -> None:
    """Create an export root if needed and install its marker when recognizable."""
    canonical_output_dir = canonicalize_output_dir(output_dir)
    root_fd = _open_export_root(canonical_output_dir, create=True)
    assert root_fd is not None
    try:
        _claim_export_root(root_fd, canonical_output_dir)
    finally:
        os.close(root_fd)


def _open_owned_export_root(output_dir: Path, *, create: bool) -> int | None:
    """Open an owned root, claiming recognizable storage only for write creation."""
    canonical_output_dir = canonicalize_output_dir(output_dir)
    root_fd = _open_export_root(canonical_output_dir, create=create)
    if root_fd is None:
        return None
    try:
        if create:
            _claim_export_root(root_fd, canonical_output_dir)
        else:
            _require_owned_export_root(root_fd, canonical_output_dir)
    except Exception:
        os.close(root_fd)
        raise
    return root_fd


def _open_room_directory(
    root_fd: int,
    output_dir: Path,
    room: ThreadExportRoom,
    *,
    create: bool,
) -> int | None:
    """Open and pin one exporter-controlled room directory."""
    room_name = _room_path_segment(room.key)
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
    if message.edited_timestamp is not None:
        payload["edited_timestamp"] = message.edited_timestamp
        if edited_timestamp_iso := _timestamp_iso(message.edited_timestamp):
            payload["edited_timestamp_iso"] = edited_timestamp_iso
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


def _latest_thread_summary(
    messages: list[ResolvedVisibleMessage],
    *,
    trusted_sender_ids: Collection[str],
) -> str | None:
    """Return the latest trusted thread-summary notice text, when one exists."""
    for message in reversed(messages):
        if message.sender not in trusted_sender_ids:
            continue
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
    trusted_sender_ids: Collection[str],
) -> dict[str, object]:
    """Build one YAML document for a Matrix thread."""
    thread_block: dict[str, object] = {
        "id": thread_id,
        "source": "matrix",
    }
    if summary := _latest_thread_summary(messages, trusted_sender_ids=trusted_sender_ids):
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
        payload = yaml_io.safe_load(text)
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
    """Build one room index document from the recognizable thread files on disk."""
    indexed = [
        indexed_entry
        for filename in sorted(
            name for name in os.listdir(room_fd) if _is_thread_export_filename(name) and _regular_file_at(room_fd, name)
        )
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


def _declared_room_index_filenames(room_fd: int) -> set[str] | None:
    """Return the thread filename set declared by the current room index."""
    text = _read_text_at(room_fd, _ROOM_INDEX_FILENAME)
    if text is None:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or not isinstance(threads := payload.get("threads"), list):
        return None
    filenames: set[str] = set()
    for entry in threads:
        if not isinstance(entry, dict) or not isinstance(filename := entry.get("file"), str):
            return None
        filenames.add(filename)
    return filenames


def _room_index_filename_set_matches(room_fd: int) -> bool:
    """Return whether index membership matches regular thread YAML files on disk."""
    declared_filenames = _declared_room_index_filenames(room_fd)
    if declared_filenames is None:
        return False
    disk_filenames = {
        filename
        for filename in os.listdir(room_fd)
        if _is_thread_export_filename(filename) and _regular_file_at(room_fd, filename)
    }
    return declared_filenames == disk_filenames


def write_room_index(
    output_dir: Path,
    room: ThreadExportRoom,
    *,
    thread_files_changed: bool = True,
) -> None:
    """Rebuild a room index after YAML changes or detected filename-set drift."""
    root_fd = _open_owned_export_root(output_dir, create=False)
    if root_fd is None:
        return
    try:
        room_fd = _open_room_directory(root_fd, output_dir, room, create=False)
    finally:
        os.close(root_fd)
    if room_fd is None:
        return
    try:
        if not thread_files_changed and _room_index_filename_set_matches(room_fd):
            return
        payload = _room_index_payload(room_fd, room)
        text = f"{json.dumps(payload, indent=2)}\n"
        if _read_text_at(room_fd, _ROOM_INDEX_FILENAME) != text:
            _atomic_write_at(room_fd, _ROOM_INDEX_FILENAME, text)
    finally:
        os.close(room_fd)


def room_has_thread_exports(output_dir: Path, room: ThreadExportRoom) -> bool:
    """Return whether a safe room directory contains recognizable thread YAML."""
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
        return _contains_thread_export_file(room_fd)
    finally:
        os.close(room_fd)


def _log_unrecognized_entry(output_dir: Path, entry: str, *, room_key: str | None = None) -> None:
    """Warn that deletion left an unrecognized entry untouched."""
    logger.warning(
        "Leaving unrecognized thread export entry untouched",
        output_dir=str(output_dir),
        room_key=room_key,
        entry=entry,
    )


def _remove_room_export_entries(
    root_fd: int,
    output_dir: Path,
    room_name: str,
    *,
    room_key: str | None,
) -> bool:
    """Remove exporter-owned room entries, preserving and reporting everything else."""
    room_fd = _open_directory_at(
        root_fd,
        room_name,
        path=output_dir / room_name,
        label="room directory",
        create=False,
    )
    if room_fd is None:
        return False
    try:
        filenames = os.listdir(room_fd)  # noqa: PTH208 - room_fd pins the directory
        removed_files = False
        for filename in filenames:
            if _is_room_export_file(room_fd, filename):
                os.unlink(filename, dir_fd=room_fd)
                removed_files = True
            else:
                _log_unrecognized_entry(output_dir, filename, room_key=room_key)
        if removed_files:
            _fsync_directory_fd(room_fd)
        removed_directory = False
        if not os.listdir(room_fd):  # noqa: PTH208 - room_fd pins the directory
            with suppress(OSError):
                os.rmdir(room_name, dir_fd=root_fd)
                removed_directory = True
        return removed_files or removed_directory
    finally:
        os.close(room_fd)


def remove_room_export(output_dir: Path, room: ThreadExportRoom) -> None:
    """Retract one room export, preserving and reporting entries the exporter does not own.

    Retraction is idempotent: once every exporter-owned entry is gone, later passes over the
    same room are quiet no-ops rather than a failure the operator can never clear.
    """
    root_fd = _open_owned_export_root(output_dir, create=False)
    if root_fd is None:
        return
    room_name = _room_path_segment(room.key)
    try:
        try:
            mode = os.stat(room_name, dir_fd=root_fd, follow_symlinks=False).st_mode
        except FileNotFoundError:
            return
        if stat.S_ISLNK(mode):
            raise _unsafe_directory(output_dir / room_name, "room directory")
        if _recognizable_room_directory(root_fd, output_dir, room_name):
            if not shutil.rmtree.avoids_symlink_attacks:
                msg = "Safe descriptor-relative directory removal is unavailable"
                raise RuntimeError(msg)
            shutil.rmtree(room_name, dir_fd=root_fd)
            _fsync_directory_fd(root_fd)
            return
        if not stat.S_ISDIR(mode):
            _log_unrecognized_entry(output_dir, room_name, room_key=room.key)
            return
        if _remove_room_export_entries(root_fd, output_dir, room_name, room_key=room.key):
            _fsync_directory_fd(root_fd)
    finally:
        os.close(root_fd)


def remove_stale_thread_exports(
    output_dir: Path,
    room: ThreadExportRoom,
    thread_ids: Sequence[str],
) -> bool:
    """Remove recognizable thread files absent from a complete enumeration."""
    root_fd = _open_owned_export_root(output_dir, create=False)
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
        removed = False
        for filename in os.listdir(room_fd):  # noqa: PTH208 - room_fd pins the directory
            if not filename.endswith(".yaml") or filename in expected_names:
                continue
            if not _is_thread_export_filename(filename) or not _regular_file_at(room_fd, filename):
                _log_unrecognized_entry(output_dir, filename, room_key=room.key)
                continue
            os.unlink(filename, dir_fd=room_fd)
            removed = True
        if removed:
            _fsync_directory_fd(room_fd)
        return removed
    finally:
        os.close(room_fd)


def _remove_reconciliation_room(root_fd: int, output_dir: Path, room_name: str) -> bool:
    """Remove a stale indexed room or its recognizable partial thread files."""
    if _recognizable_room_directory(root_fd, output_dir, room_name):
        if not shutil.rmtree.avoids_symlink_attacks:
            msg = "Safe descriptor-relative directory removal is unavailable"
            raise RuntimeError(msg)
        shutil.rmtree(room_name, dir_fd=root_fd)
        return True
    if not _is_encoded_room_segment(room_name):
        _log_unrecognized_entry(output_dir, room_name)
        return False
    try:
        mode = os.stat(room_name, dir_fd=root_fd, follow_symlinks=False).st_mode
    except FileNotFoundError:
        return False
    if stat.S_ISDIR(mode):
        return _remove_room_export_entries(root_fd, output_dir, room_name, room_key=None)
    _log_unrecognized_entry(output_dir, room_name)
    return False


def reconcile_room_directories(output_dir: Path, retained_room_keys: set[str]) -> None:
    """Remove recognizable room directories outside the retained authorization scope."""
    root_fd = _open_owned_export_root(output_dir, create=False)
    if root_fd is None:
        return
    try:
        retained_names = {_room_path_segment(room_key) for room_key in retained_room_keys}
        removed = False
        for name in os.listdir(root_fd):  # noqa: PTH208 - root_fd pins the directory
            if name == _ROOT_MARKER_FILENAME or name in retained_names:
                continue
            removed = _remove_reconciliation_room(root_fd, output_dir, name) or removed
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
        existing = yaml_io.safe_load(text)
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
    root_fd = _open_owned_export_root(output_dir, create=True)
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
        text = yaml_io.safe_dump(
            payload,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
        )
        _atomic_write_at(room_fd, filename, text)
        return True
    finally:
        os.close(room_fd)
