"""Durable per-thread model overrides for mid-thread model switching."""

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

_THREAD_MODELS_FILENAME = "thread_models.json"
_MAX_TRACKED_THREADS = 1000


def _store_path(runtime_paths: RuntimePaths) -> Path:
    return tracking_dir(runtime_paths) / _THREAD_MODELS_FILENAME


def _is_valid_override(_thread_id: str, record: dict[object, object]) -> bool:
    """Return whether one persisted thread-model record has the required shape."""
    return isinstance(record.get("model"), str) and isinstance(record.get("set_at", ""), str)


def _load_overrides(path: Path) -> dict[str, OverrideRecord]:
    """Load persisted overrides, treating a missing or unreadable file as empty."""
    return load_cached_override_records(path, _is_valid_override)


def _save_overrides(path: Path, overrides: dict[str, OverrideRecord]) -> None:
    write_bounded_override_records(path, overrides, max_records=_MAX_TRACKED_THREADS)


def _get_thread_model_override(runtime_paths: RuntimePaths, thread_id: str | None) -> str | None:
    """Return the model name stored for one thread root, if any."""
    if thread_id is None:
        return None
    record = _load_overrides(_store_path(runtime_paths)).get(thread_id)
    return record["model"] if record is not None else None


@dataclass(frozen=True)
class _ThreadModelOverrideState:
    """One thread's stored override split into the runtime-active name and a stale leftover."""

    active: str | None
    stale: str | None


def resolve_thread_model_override(
    runtime_paths: RuntimePaths,
    thread_id: str | None,
    *,
    configured_models: Container[str],
) -> _ThreadModelOverrideState:
    """Classify one thread's stored override against the configured model names.

    An override naming a model that no longer exists in the config is stale:
    runtime resolution, `!model`, and the `thread_model` tool must all ignore
    it rather than apply or report it as active.
    """
    override = _get_thread_model_override(runtime_paths, thread_id)
    if override is None:
        return _ThreadModelOverrideState(active=None, stale=None)
    if override in configured_models:
        return _ThreadModelOverrideState(active=override, stale=None)
    return _ThreadModelOverrideState(active=None, stale=override)


def set_thread_model_override(
    runtime_paths: RuntimePaths,
    *,
    thread_id: str,
    model_name: str,
    room_id: str,
    set_by: str,
) -> None:
    """Persist one thread's model override, replacing any previous one."""
    path = _store_path(runtime_paths)
    overrides = dict(_load_overrides(path))
    overrides[thread_id] = {
        "model": model_name,
        "room_id": room_id,
        "set_by": set_by,
        "set_at": datetime.now(UTC).isoformat(),
    }
    _save_overrides(path, overrides)


def clear_thread_model_override(runtime_paths: RuntimePaths, thread_id: str) -> bool:
    """Remove one thread's model override; return whether one was present."""
    path = _store_path(runtime_paths)
    overrides = dict(_load_overrides(path))
    if thread_id not in overrides:
        return False
    del overrides[thread_id]
    _save_overrides(path, overrides)
    return True
