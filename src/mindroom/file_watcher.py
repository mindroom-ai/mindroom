"""Simple file watcher utility without external dependencies."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

logger = get_logger(__name__)
_WATCH_SCAN_INTERVAL_SECONDS = 1.0
_WATCH_TREE_DEBOUNCE_SECONDS = 1.0
_IGNORED_TREE_PARTS = {"__pycache__", ".ruff_cache", ".mypy_cache", ".pytest_cache", ".git"}
_IGNORED_TREE_SUFFIXES = (".pyc", ".pyo", ".swp", ".swo", "~", ".tmp")


def _is_relevant_path(path: Path) -> bool:
    """Return whether one tree entry should participate in change snapshots."""
    if not path.is_file():
        return False
    if any(part in _IGNORED_TREE_PARTS for part in path.parts):
        return False
    name = path.name
    return not (name.endswith(_IGNORED_TREE_SUFFIXES) or name.startswith(".#"))


def _paths_mtime_snapshot(paths: Iterable[Path | str]) -> dict[Path, float]:
    """Return the current mtime snapshot for one dynamic set of files."""
    snapshot: dict[Path, float] = {}
    for raw_path in paths:
        path = Path(raw_path)
        try:
            snapshot[path] = path.stat().st_mtime if path.exists() else 0.0
        except (OSError, PermissionError):
            snapshot[path] = 0.0
    return snapshot


async def watch_paths(
    paths_provider: Callable[[], Iterable[Path | str]],
    callback: Callable[[], Awaitable[None]],
    stop_event: asyncio.Event | None = None,
) -> None:
    """Watch a dynamic set of files and call callback when any of them changes.

    ``paths_provider`` is re-evaluated every scan so the watched set can grow or
    shrink between calls (e.g. config ``!include`` files after a reload). Paths
    that enter the set are baselined silently, and a change fires only for a
    file that existed in the previous scan with a different mtime, so a
    momentarily missing file during an editor's delete-and-rename save cannot
    fire a change until it reappears.
    """
    last_snapshot = _paths_mtime_snapshot(paths_provider())

    while stop_event is None or not stop_event.is_set():
        await asyncio.sleep(_WATCH_SCAN_INTERVAL_SECONDS)

        try:
            current_snapshot = _paths_mtime_snapshot(paths_provider())
            changed = any(
                mtime != 0.0 and last_snapshot.get(path, mtime) != mtime for path, mtime in current_snapshot.items()
            )
            last_snapshot = current_snapshot
            if changed:
                await callback()
        except Exception:
            # Don't let callback errors stop the watcher
            # The callback should handle its own errors
            logger.exception("Exception during file watcher callback - continuing to watch")


def _tree_snapshot(root_path: Path) -> dict[Path, int]:
    """Return the current mtime snapshot for one directory tree."""
    if not root_path.exists():
        return {}

    snapshot: dict[Path, int] = {}
    for path in root_path.rglob("*"):
        if not _is_relevant_path(path):
            continue
        try:
            snapshot[path] = path.stat().st_mtime_ns
        except (OSError, PermissionError):
            continue
    return snapshot


def _tree_changed_paths(previous: dict[Path, int], current: dict[Path, int]) -> set[Path]:
    """Return the set of paths added, removed, or modified since the last scan."""
    changed_paths = set(previous) ^ set(current)
    for path in set(previous) & set(current):
        if previous[path] != current[path]:
            changed_paths.add(path)
    return changed_paths
