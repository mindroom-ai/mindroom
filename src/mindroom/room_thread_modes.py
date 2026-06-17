"""Durable room-level thread mode overrides controlled from chat."""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from mindroom.constants import tracking_dir

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths

RoomThreadMode = Literal["thread", "room"]

_ROOM_THREAD_MODES_FILENAME = "room_thread_modes.json"
_VALID_ROOM_THREAD_MODES: frozenset[str] = frozenset({"thread", "room"})
_MAX_TRACKED_ROOMS = 1000

_load_cache: dict[Path, tuple[int, dict[str, dict[str, str]]]] = {}


def _store_path(runtime_paths: RuntimePaths) -> Path:
    return tracking_dir(runtime_paths) / _ROOM_THREAD_MODES_FILENAME


def _load_overrides(path: Path) -> dict[str, dict[str, str]]:
    """Load persisted room thread mode overrides, treating missing or unreadable files as empty."""
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        return {}
    cached = _load_cache.get(path)
    if cached is not None and cached[0] == mtime_ns:
        return cached[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    overrides = {
        room_id: record
        for room_id, record in data.items()
        if isinstance(room_id, str)
        and isinstance(record, dict)
        and record.get("mode") in _VALID_ROOM_THREAD_MODES
        and isinstance(record.get("set_at", ""), str)
    }
    _load_cache[path] = (mtime_ns, overrides)
    return overrides


def _save_overrides(path: Path, overrides: dict[str, dict[str, str]]) -> None:
    if len(overrides) > _MAX_TRACKED_ROOMS:
        newest = sorted(overrides.items(), key=lambda item: item[1].get("set_at", ""), reverse=True)
        overrides = dict(newest[:_MAX_TRACKED_ROOMS])
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, suffix=".tmp", delete=False, encoding="utf-8") as temp_file:
        temp_file.write(json.dumps(overrides, indent=2, sort_keys=True))
        temp_path = Path(temp_file.name)
    temp_path.replace(path)
    _load_cache.pop(path, None)


def _get_room_thread_mode_override(runtime_paths: RuntimePaths, room_id: str | None) -> RoomThreadMode | None:
    """Return the mode stored for one Matrix room, if any."""
    if room_id is None:
        return None
    record = _load_overrides(_store_path(runtime_paths)).get(room_id)
    if record is None:
        return None
    mode = record["mode"]
    if mode not in _VALID_ROOM_THREAD_MODES:
        return None
    return cast("RoomThreadMode", mode)


@dataclass(frozen=True)
class _RoomThreadModeOverride:
    """One room's stored thread mode override."""

    mode: RoomThreadMode | None
    set_by: str | None = None
    set_at: str | None = None


def get_room_thread_mode_override(runtime_paths: RuntimePaths, room_id: str) -> _RoomThreadModeOverride:
    """Return one room's full override record."""
    record = _load_overrides(_store_path(runtime_paths)).get(room_id)
    if record is None:
        return _RoomThreadModeOverride(mode=None)
    mode = record["mode"]
    if mode not in _VALID_ROOM_THREAD_MODES:
        return _RoomThreadModeOverride(mode=None)
    return _RoomThreadModeOverride(
        mode=cast("RoomThreadMode", mode),
        set_by=record.get("set_by"),
        set_at=record.get("set_at"),
    )


def resolve_room_thread_mode_override(runtime_paths: RuntimePaths, room_id: str | None) -> RoomThreadMode | None:
    """Return one room's active override mode, if present."""
    return _get_room_thread_mode_override(runtime_paths, room_id)


def set_room_thread_mode_override(
    runtime_paths: RuntimePaths,
    *,
    room_id: str,
    mode: RoomThreadMode,
    set_by: str,
) -> None:
    """Persist one room's thread mode override, replacing any previous one."""
    path = _store_path(runtime_paths)
    overrides = dict(_load_overrides(path))
    overrides[room_id] = {
        "mode": mode,
        "set_by": set_by,
        "set_at": datetime.now(UTC).isoformat(),
    }
    _save_overrides(path, overrides)


def clear_room_thread_mode_override(runtime_paths: RuntimePaths, room_id: str) -> bool:
    """Remove one room's thread mode override; return whether one was present."""
    path = _store_path(runtime_paths)
    overrides = dict(_load_overrides(path))
    if room_id not in overrides:
        return False
    del overrides[room_id]
    _save_overrides(path, overrides)
    return True
