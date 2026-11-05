"""Durable room-level reply mode overrides controlled from chat."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, cast, get_args

from mindroom.constants import tracking_dir
from mindroom.durable_write import (
    OverrideRecord,
    load_cached_override_records,
    write_bounded_override_records,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths

# `auto` lets the router and a room's only responder answer a message that mentions no one; `mentions` does not.
RoomReplyMode = Literal["auto", "mentions"]
ROOM_REPLY_MODES: tuple[RoomReplyMode, ...] = get_args(RoomReplyMode)

_ROOM_REPLY_MODES_FILENAME = "room_reply_modes.json"
_MAX_TRACKED_ROOMS = 1000


def _store_path(runtime_paths: RuntimePaths) -> Path:
    return tracking_dir(runtime_paths) / _ROOM_REPLY_MODES_FILENAME


def _is_valid_override(_room_id: str, record: dict[object, object]) -> bool:
    """Return whether one persisted room reply mode record has the required shape."""
    return record.get("mode") in ROOM_REPLY_MODES and isinstance(record.get("set_at", ""), str)


def _load_overrides(path: Path) -> dict[str, OverrideRecord]:
    """Load persisted room reply mode overrides, treating missing or unreadable files as empty."""
    return load_cached_override_records(path, _is_valid_override)


def resolve_room_reply_mode_override(runtime_paths: RuntimePaths, room_id: str) -> RoomReplyMode | None:
    """Return the reply mode stored for one Matrix room, if any."""
    record = _load_overrides(_store_path(runtime_paths)).get(room_id)
    return None if record is None else cast("RoomReplyMode", record["mode"])


def set_room_reply_mode_override(runtime_paths: RuntimePaths, *, room_id: str, mode: RoomReplyMode) -> None:
    """Persist one room's reply mode override, replacing any previous one."""
    path = _store_path(runtime_paths)
    overrides = _load_overrides(path)
    # The bounded write keeps the newest records by set_at.
    overrides[room_id] = {"mode": mode, "set_at": datetime.now(UTC).isoformat()}
    write_bounded_override_records(path, overrides, max_records=_MAX_TRACKED_ROOMS)


def clear_room_reply_mode_override(runtime_paths: RuntimePaths, room_id: str) -> bool:
    """Remove one room's reply mode override; return whether one was present."""
    path = _store_path(runtime_paths)
    overrides = _load_overrides(path)
    if room_id not in overrides:
        return False
    del overrides[room_id]
    write_bounded_override_records(path, overrides, max_records=_MAX_TRACKED_ROOMS)
    return True
