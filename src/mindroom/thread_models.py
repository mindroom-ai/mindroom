"""Durable per-thread model overrides for mid-thread model switching."""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from mindroom.constants import tracking_dir

if TYPE_CHECKING:
    from collections.abc import Container

    from mindroom.constants import RuntimePaths

_THREAD_MODELS_FILENAME = "thread_models.json"
_MAX_TRACKED_THREADS = 1000

# Parsed store keyed by path and invalidated by mtime, so per-turn model
# resolution does not re-read and re-parse the file on every call.
_load_cache: dict[Path, tuple[int, dict[str, dict[str, str]]]] = {}


def _store_path(runtime_paths: RuntimePaths) -> Path:
    return tracking_dir(runtime_paths) / _THREAD_MODELS_FILENAME


def _load_overrides(path: Path) -> dict[str, dict[str, str]]:
    """Load persisted overrides, treating a missing or unreadable file as empty."""
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
        return {}
    if not isinstance(data, dict):
        return {}
    # Records must keep the shape the store guarantees: a string model name
    # and a string set_at so prune sorting cannot fail on mixed types.
    overrides = {
        thread_id: record
        for thread_id, record in data.items()
        if isinstance(record, dict)
        and isinstance(record.get("model"), str)
        and isinstance(record.get("set_at", ""), str)
    }
    _load_cache[path] = (mtime_ns, overrides)
    return overrides


def _save_overrides(path: Path, overrides: dict[str, dict[str, str]]) -> None:
    if len(overrides) > _MAX_TRACKED_THREADS:
        newest = sorted(overrides.items(), key=lambda item: item[1].get("set_at", ""), reverse=True)
        overrides = dict(newest[:_MAX_TRACKED_THREADS])
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, suffix=".tmp", delete=False, encoding="utf-8") as temp_file:
        temp_file.write(json.dumps(overrides, indent=2, sort_keys=True))
        temp_path = Path(temp_file.name)
    temp_path.replace(path)
    _load_cache.pop(path, None)


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
