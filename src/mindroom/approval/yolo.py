"""Durable per-thread YOLO windows that approve one requester's gated tool calls without asking."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from mindroom.constants import tracking_dir
from mindroom.durable_write import (
    OverrideRecord,
    load_cached_override_records,
    write_bounded_override_records,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths

YOLO_MAX_DURATION = timedelta(hours=24)
_YOLO_FILENAME = "yolo_windows.json"
_MAX_TRACKED_WINDOWS = 1000


def _store_path(runtime_paths: RuntimePaths) -> Path:
    return tracking_dir(runtime_paths) / _YOLO_FILENAME


def _window_key(room_id: str, thread_id: str | None, requester_id: str) -> str:
    return json.dumps([room_id, thread_id, requester_id], separators=(",", ":"))


def _expires_at(record: OverrideRecord) -> datetime:
    return datetime.fromisoformat(record["expires_at"])


def _is_valid_window(_key: str, record: dict[object, object]) -> bool:
    """Return whether one persisted window has an aware expiry and a set time."""
    expires_at = record.get("expires_at")
    if not isinstance(expires_at, str) or not isinstance(record.get("set_at"), str):
        return False
    try:
        return datetime.fromisoformat(expires_at).tzinfo is not None
    except ValueError:
        return False


def _load_active_windows(path: Path, now: datetime) -> dict[str, OverrideRecord]:
    """Load persisted windows that have not expired yet."""
    windows = load_cached_override_records(path, _is_valid_window)
    return {key: record for key, record in windows.items() if _expires_at(record) > now}


def yolo_expires_at(
    runtime_paths: RuntimePaths,
    *,
    room_id: str | None,
    thread_id: str | None,
    requester_id: str | None,
) -> datetime | None:
    """Return when the requester's YOLO window in one conversation ends, or None when none is active."""
    if room_id is None or requester_id is None:
        return None
    now = datetime.now(UTC)
    record = _load_active_windows(_store_path(runtime_paths), now).get(_window_key(room_id, thread_id, requester_id))
    return None if record is None else _expires_at(record)


def start_yolo(
    runtime_paths: RuntimePaths,
    *,
    room_id: str,
    thread_id: str | None,
    requester_id: str,
    duration: timedelta,
) -> datetime:
    """Start or replace the requester's YOLO window in one conversation and return when it ends."""
    path = _store_path(runtime_paths)
    now = datetime.now(UTC)
    windows = _load_active_windows(path, now)
    expires_at = now + duration
    windows[_window_key(room_id, thread_id, requester_id)] = {
        "expires_at": expires_at.isoformat(),
        "set_at": now.isoformat(),
    }
    write_bounded_override_records(path, windows, max_records=_MAX_TRACKED_WINDOWS)
    return expires_at


def end_yolo(runtime_paths: RuntimePaths, *, room_id: str, thread_id: str | None, requester_id: str) -> bool:
    """End the requester's YOLO window in one conversation; return whether one was active."""
    path = _store_path(runtime_paths)
    windows = _load_active_windows(path, datetime.now(UTC))
    if windows.pop(_window_key(room_id, thread_id, requester_id), None) is None:
        return False
    write_bounded_override_records(path, windows, max_records=_MAX_TRACKED_WINDOWS)
    return True
