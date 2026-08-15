"""Durable room-level model overrides controlled from chat."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from mindroom.constants import tracking_dir
from mindroom.durable_write import (
    OverrideRecord,
    load_cached_override_records,
    write_bounded_override_records,
)

if TYPE_CHECKING:
    from collections.abc import Container
    from pathlib import Path

    from mindroom.constants import RuntimePaths

_ROOM_MODEL_OVERRIDES_FILENAME = "room_model_overrides.json"
_MAX_TRACKED_ROOMS = 1000


def _store_path(runtime_paths: RuntimePaths) -> Path:
    return tracking_dir(runtime_paths) / _ROOM_MODEL_OVERRIDES_FILENAME


def _is_valid_override(_room_id: str, record: dict[object, object]) -> bool:
    """Return whether one persisted room-model record has the required shape."""
    return isinstance(record.get("model"), str) and isinstance(record.get("set_at", ""), str)


def _load_overrides(path: Path) -> dict[str, OverrideRecord]:
    """Load persisted overrides, treating a missing or unreadable file as empty."""
    return load_cached_override_records(path, _is_valid_override)


def _save_overrides(path: Path, overrides: dict[str, OverrideRecord]) -> None:
    write_bounded_override_records(path, overrides, max_records=_MAX_TRACKED_ROOMS)


@dataclass(frozen=True)
class _RoomModelOverrideState:
    """One room's stored override split into active and stale model names."""

    active: str | None
    stale: str | None
    set_by: str | None = None
    set_at: str | None = None


def resolve_room_model_override(
    runtime_paths: RuntimePaths,
    room_id: str | None,
    *,
    configured_models: Container[str],
) -> _RoomModelOverrideState:
    """Classify one room's stored override against configured model names."""
    if room_id is None:
        return _RoomModelOverrideState(active=None, stale=None)
    record = _load_overrides(_store_path(runtime_paths)).get(room_id)
    if record is None:
        return _RoomModelOverrideState(active=None, stale=None)
    model_name = record["model"]
    metadata = {"set_by": record.get("set_by"), "set_at": record.get("set_at")}
    if model_name in configured_models:
        return _RoomModelOverrideState(active=model_name, stale=None, **metadata)
    return _RoomModelOverrideState(active=None, stale=model_name, **metadata)


def set_room_model_override(
    runtime_paths: RuntimePaths,
    *,
    room_id: str,
    model_name: str,
    set_by: str,
) -> None:
    """Persist one room's model override, replacing any previous one."""
    path = _store_path(runtime_paths)
    overrides = _load_overrides(path)
    overrides[room_id] = {
        "model": model_name,
        "set_by": set_by,
        "set_at": datetime.now(UTC).isoformat(),
    }
    _save_overrides(path, overrides)


def clear_room_model_override(runtime_paths: RuntimePaths, room_id: str) -> bool:
    """Remove one room's model override; return whether one was present."""
    path = _store_path(runtime_paths)
    overrides = _load_overrides(path)
    if room_id not in overrides:
        return False
    del overrides[room_id]
    _save_overrides(path, overrides)
    return True
