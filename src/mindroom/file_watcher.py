"""Simple file watcher utility without external dependencies."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = get_logger(__name__)
_WATCH_SCAN_INTERVAL_SECONDS = 1.0
_WATCH_TREE_DEBOUNCE_SECONDS = 1.0


def _is_relevant_path(path: Path) -> bool:
    """Return whether one tree entry should participate in change snapshots."""
    if not path.is_file():
        return False
    if any(part in {"__pycache__", ".ruff_cache", ".mypy_cache", ".pytest_cache", ".git"} for part in path.parts):
        return False
    name = path.name
    return not (name.endswith((".pyc", ".pyo", ".swp", ".swo", "~", ".tmp")) or name.startswith(".#"))


async def watch_file(
    file_path: Path | str,
    callback: Callable[[], Awaitable[None]],
    stop_event: asyncio.Event | None = None,
) -> None:
    """Watch a file for changes and call callback when modified.

    Args:
        file_path: Path to the file to watch
        callback: Async function to call when file changes
        stop_event: Optional event to signal when to stop watching

    """
    file_path = Path(file_path)
    last_mtime = file_path.stat().st_mtime if file_path.exists() else 0

    while stop_event is None or not stop_event.is_set():
        await asyncio.sleep(_WATCH_SCAN_INTERVAL_SECONDS)

        try:
            if file_path.exists():
                current_mtime = file_path.stat().st_mtime
                if current_mtime != last_mtime:
                    last_mtime = current_mtime
                    await callback()
        except (OSError, PermissionError):
            # File might have been deleted or become unreadable
            # Reset mtime so we detect when it comes back
            last_mtime = 0
        except Exception:
            # Don't let callback errors stop the watcher
            # The callback should handle its own errors
            logger.exception("Exception during file watcher callback - continuing to watch")


async def watch_tree(
    root_path: Path | str,
    callback: Callable[[tuple[Path, ...]], Awaitable[None]],
    stop_event: asyncio.Event | None = None,
) -> None:
    """Watch one directory tree for debounced file additions, removals, and edits."""
    root_path = Path(root_path)
    last_snapshot = _tree_snapshot(root_path)
    pending_changes: set[Path] = set()
    last_change_at: float | None = None
    loop = asyncio.get_running_loop()

    while stop_event is None or not stop_event.is_set():
        await asyncio.sleep(_WATCH_SCAN_INTERVAL_SECONDS)

        try:
            current_snapshot = _tree_snapshot(root_path)
            changed_paths = _tree_changed_paths(last_snapshot, current_snapshot)
            last_snapshot = current_snapshot
            if changed_paths:
                pending_changes.update(changed_paths)
                last_change_at = loop.time()
                continue
            if (
                pending_changes
                and last_change_at is not None
                and loop.time() - last_change_at >= _WATCH_TREE_DEBOUNCE_SECONDS
            ):
                await callback(tuple(sorted(pending_changes)))
                pending_changes.clear()
                last_change_at = None
        except Exception:
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
