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
    from collections.abc import Container, Iterable
    from pathlib import Path

    from mindroom.constants import RuntimePaths

_THREAD_MODELS_FILENAME = "thread_models.json"
_MAX_TRACKED_THREADS = 1000
# Each entity's selection is one record field, so a thread holds different models for entities with different setters.
_ENTITY_MODEL_PREFIX = "model:"


def _store_path(runtime_paths: RuntimePaths) -> Path:
    return tracking_dir(runtime_paths) / _THREAD_MODELS_FILENAME


def _entity_fields(entity_names: Iterable[str]) -> set[str]:
    return {f"{_ENTITY_MODEL_PREFIX}{entity_name}" for entity_name in entity_names}


def _entity_models(record: OverrideRecord) -> dict[str, str]:
    """Return the model name stored for each entity in one thread record."""
    return {
        field.removeprefix(_ENTITY_MODEL_PREFIX): model_name
        for field, model_name in record.items()
        if field.startswith(_ENTITY_MODEL_PREFIX)
    }


def _is_valid_override(_thread_id: str, record: dict[object, object]) -> bool:
    """Return whether one persisted thread-model record has the required shape."""
    return all(isinstance(value, str) for value in record.values()) and any(
        isinstance(field, str) and field.startswith(_ENTITY_MODEL_PREFIX) for field in record
    )


def _load_overrides(path: Path) -> dict[str, OverrideRecord]:
    """Load persisted overrides, treating a missing or unreadable file as empty."""
    return load_cached_override_records(path, _is_valid_override)


def _save_thread_record(
    path: Path,
    overrides: dict[str, OverrideRecord],
    thread_id: str,
    record: OverrideRecord,
) -> None:
    """Store one thread's record, dropping it once no entity selection remains."""
    if _entity_models(record):
        overrides[thread_id] = record
    else:
        overrides.pop(thread_id, None)
    write_bounded_override_records(path, overrides, max_records=_MAX_TRACKED_THREADS)


def _get_thread_model_override(runtime_paths: RuntimePaths, thread_id: str | None) -> OverrideRecord | None:
    """Return the override record stored for one thread root, if any."""
    if thread_id is None:
        return None
    return _load_overrides(_store_path(runtime_paths)).get(thread_id)


@dataclass(frozen=True)
class _ThreadModelOverrideState:
    """One thread's stored overrides split into runtime-active names and stale leftovers.

    Both map an entity name to the model name stored for that entity.
    """

    active: dict[str, str]
    stale: dict[str, str]


def resolve_thread_model_override(
    runtime_paths: RuntimePaths,
    thread_id: str | None,
    *,
    configured_models: Container[str],
) -> _ThreadModelOverrideState:
    """Classify one thread's stored overrides against the configured model names.

    An override naming a model that no longer exists in the config is stale:
    runtime resolution, `!model`, and the `thread_model` tool must all ignore
    it rather than apply or report it as active.
    """
    record = _get_thread_model_override(runtime_paths, thread_id)
    entity_models = {} if record is None else _entity_models(record)
    return _ThreadModelOverrideState(
        active={entity: model for entity, model in entity_models.items() if model in configured_models},
        stale={entity: model for entity, model in entity_models.items() if model not in configured_models},
    )


def set_thread_model_override(
    runtime_paths: RuntimePaths,
    *,
    thread_id: str,
    model_name: str,
    room_id: str,
    set_by: str,
    entity_names: Iterable[str],
) -> None:
    """Persist one thread's model override for the entities its setter may address, keeping every other entity's."""
    path = _store_path(runtime_paths)
    overrides = _load_overrides(path)
    record = {
        **overrides.get(thread_id, {}),
        **dict.fromkeys(_entity_fields(entity_names), model_name),
        "room_id": room_id,
        "set_by": set_by,
        "set_at": datetime.now(UTC).isoformat(),
    }
    _save_thread_record(path, overrides, thread_id, record)


def clear_thread_model_override(runtime_paths: RuntimePaths, thread_id: str, *, entity_names: Iterable[str]) -> bool:
    """Remove one thread's model override for the given entities; return whether any was present."""
    path = _store_path(runtime_paths)
    overrides = _load_overrides(path)
    record = overrides.get(thread_id)
    fields = _entity_fields(entity_names)
    if record is None or fields.isdisjoint(record):
        return False
    _save_thread_record(
        path,
        overrides,
        thread_id,
        {field: value for field, value in record.items() if field not in fields},
    )
    return True
