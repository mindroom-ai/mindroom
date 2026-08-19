"""Shared JSON metadata helpers for local worker backends."""

from __future__ import annotations

import json
import shutil
from contextlib import nullcontext
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from threading import Lock


class _WorkerStatePathsLike(Protocol):
    """Filesystem paths required for worker metadata persistence."""

    root: Path
    metadata_dir: Path
    metadata_file: Path


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
    """Load one worker metadata JSON document into the requested dataclass type."""
    if not paths.metadata_file.exists():
        return None

    try:
        with paths.metadata_file.open(encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None

    try:
        return metadata_type(**data)
    except TypeError:
        return None


def save_worker_metadata(
    paths: _WorkerStatePathsLike,
    metadata: object,
    *,
    ensure_root: bool = False,
    lock: Lock | None = None,
) -> None:
    """Persist one worker metadata dataclass to JSON."""
    if ensure_root:
        paths.root.mkdir(parents=True, exist_ok=True)
    paths.metadata_dir.mkdir(parents=True, exist_ok=True)

    lock_context = nullcontext() if lock is None else lock
    with lock_context, paths.metadata_file.open("w", encoding="utf-8") as f:
        json.dump(vars(metadata), f, sort_keys=True)


def remove_worker_state_root(worker_root: Path, *, workers_root: Path) -> None:
    """Remove one exact worker root after validating its resolved parent."""
    resolved_workers_root = workers_root.expanduser().resolve()
    candidate = worker_root.expanduser()
    if candidate.is_symlink():
        msg = f"Worker state root cannot be a symbolic link: {candidate}"
        raise ValueError(msg)
    resolved_worker_root = candidate.resolve()
    if resolved_worker_root.parent != resolved_workers_root:
        msg = f"Worker state root must be one direct child of {resolved_workers_root}: {resolved_worker_root}"
        raise ValueError(msg)
    try:
        shutil.rmtree(resolved_worker_root)
    except FileNotFoundError:
        return
