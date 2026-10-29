"""Shared JSON metadata helpers for local worker backends.

A record's lifecycle is one :class:`WorkerLifecycleState` field in memory and flat keys beside its other fields on disk.
"""

from __future__ import annotations

import json
from contextlib import nullcontext
from dataclasses import asdict, fields
from typing import TYPE_CHECKING, Protocol

from mindroom.durable_write import write_json_file_durable
from mindroom.workers.backends._lifecycle import WorkerLifecycleState

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from threading import Lock


class _WorkerStatePathsLike(Protocol):
    """Filesystem paths required for worker metadata persistence."""

    root: Path
    metadata_dir: Path
    metadata_file: Path


class _WorkerRecord(Protocol):
    """Worker metadata dataclass carrying its lifecycle state."""

    lifecycle: WorkerLifecycleState


_LIFECYCLE_FIELD_NAMES = tuple(field.name for field in fields(WorkerLifecycleState))


def list_worker_state_paths[PathsT](
    workers_root: Path,
    *,
    state_paths_from_root: Callable[[Path], PathsT],
) -> list[PathsT]:
    """List worker state paths rooted under one workers directory."""
    if not workers_root.exists():
        return []

    return [
        state_paths_from_root(metadata_file.parents[1])
        for metadata_file in sorted(workers_root.glob("*/metadata/worker.json"))
    ]


def load_worker_metadata[MetadataT](
    paths: _WorkerStatePathsLike,
    *,
    metadata_type: type[MetadataT],
) -> MetadataT | None:
    """Load one worker metadata JSON document into the requested dataclass type, or ``None`` when it is not one."""
    if not paths.metadata_file.exists():
        return None

    try:
        with paths.metadata_file.open(encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    lifecycle_data = {name: data.pop(name) for name in _LIFECYCLE_FIELD_NAMES if name in data}
    try:
        return metadata_type(**data, lifecycle=WorkerLifecycleState(**lifecycle_data))
    except TypeError:
        return None


def save_worker_metadata(
    paths: _WorkerStatePathsLike,
    metadata: _WorkerRecord,
    *,
    ensure_root: bool = False,
    lock: Lock | None = None,
) -> None:
    """Persist one worker metadata dataclass to JSON as one flat object."""
    if ensure_root:
        paths.root.mkdir(parents=True, exist_ok=True)
    paths.metadata_dir.mkdir(parents=True, exist_ok=True)

    document = vars(metadata) | asdict(metadata.lifecycle)
    del document["lifecycle"]
    lock_context = nullcontext() if lock is None else lock
    with lock_context:
        write_json_file_durable(
            paths.metadata_file,
            document,
            strict_atomic_replace=True,
            sort_keys=True,
        )
