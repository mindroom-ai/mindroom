"""Durable room-level thread mode overrides controlled from chat."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, cast

from mindroom.constants import tracking_dir
from mindroom.durable_write import (
    OverrideRecord,
    load_cached_override_records,
    write_bounded_override_records,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths

RoomThreadMode = Literal["thread", "room"]

_ROOM_THREAD_MODES_FILENAME = "room_thread_modes.json"
_VALID_ROOM_THREAD_MODES: frozenset[str] = frozenset({"thread", "room"})
_MAX_TRACKED_ROOMS = 1000


def _store_path(runtime_paths: RuntimePaths) -> Path:
    return tracking_dir(runtime_paths) / _ROOM_THREAD_MODES_FILENAME


def _is_valid_override(_room_id: str, record: dict[object, object]) -> bool:
    """Return whether one persisted room-mode record has the required shape."""
    return record.get("mode") in _VALID_ROOM_THREAD_MODES and isinstance(record.get("set_at", ""), str)


def _load_overrides(path: Path) -> dict[str, OverrideRecord]:
    """Load persisted room thread mode overrides, treating missing or unreadable files as empty."""
    return load_cached_override_records(path, _is_valid_override)


def _save_overrides(path: Path, overrides: dict[str, OverrideRecord]) -> None:
    write_bounded_override_records(path, overrides, max_records=_MAX_TRACKED_ROOMS)


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
    overrides = _load_overrides(path)
    overrides[room_id] = {
        "mode": mode,
        "set_by": set_by,
        "set_at": datetime.now(UTC).isoformat(),
    }
    _save_overrides(path, overrides)


def clear_room_thread_mode_override(runtime_paths: RuntimePaths, room_id: str) -> bool:
    """Remove one room's thread mode override; return whether one was present."""
    path = _store_path(runtime_paths)
    overrides = _load_overrides(path)
    if room_id not in overrides:
        return False
    del overrides[room_id]
    _save_overrides(path, overrides)
    return True
