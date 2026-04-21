"""Plugin watcher loop and snapshot helpers for the orchestrator."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Protocol

from mindroom import file_watcher
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.tool_system.plugins import PluginReloadResult

logger = get_logger(__name__)


class _PluginWatcherRuntime(Protocol):
    """Minimal orchestrator surface needed by the plugin watcher."""

    running: bool
    config: Config | None
    _plugin_watch_last_snapshot_by_root: dict[Path, dict[Path, int]]
    _plugin_watch_state_revision: int

    def _sync_plugin_watch_roots(self, config: Config | None = None) -> tuple[Path, ...]:
        """Align watcher baselines with the currently configured plugin roots."""

    async def reload_plugins_now(
        self,
        *,
        source: str,
        changed_paths: tuple[Path, ...] = (),
    ) -> PluginReloadResult:
        """Rebuild and atomically swap the live plugin registry snapshot."""


async def watch_plugins_task(orchestrator: _PluginWatcherRuntime) -> None:
    """Watch configured plugin roots and hot-reload them after debounced edits."""
    last_snapshot_by_root = orchestrator._plugin_watch_last_snapshot_by_root
    pending_changes = set()
    last_change_at: float | None = None
    loop = asyncio.get_running_loop()

    while not orchestrator.running:  # noqa: ASYNC110
        await asyncio.sleep(0.1)

    configured_roots = orchestrator._sync_plugin_watch_roots()
    watch_state_revision = orchestrator._plugin_watch_state_revision

    while orchestrator.running:
        await asyncio.sleep(file_watcher._WATCH_SCAN_INTERVAL_SECONDS)

        try:
            config = orchestrator.config
            if config is None:
                continue

            configured_roots = orchestrator._sync_plugin_watch_roots(config)
            if watch_state_revision != orchestrator._plugin_watch_state_revision:
                pending_changes.clear()
                last_change_at = None
                watch_state_revision = orchestrator._plugin_watch_state_revision
            pending_changes = _filter_pending_plugin_changes(pending_changes, configured_roots)
            changed_paths = collect_plugin_root_changes(configured_roots, last_snapshot_by_root)

            if changed_paths:
                pending_changes.update(changed_paths)
                last_change_at = loop.time()
                continue

            if (
                pending_changes
                and last_change_at is not None
                and loop.time() - last_change_at >= file_watcher._WATCH_TREE_DEBOUNCE_SECONDS
            ):
                changed_paths = tuple(sorted(pending_changes))
                pending_changes.clear()
                last_change_at = None
                await orchestrator.reload_plugins_now(
                    source="watcher",
                    changed_paths=changed_paths,
                )
        except Exception:
            logger.exception("Exception during plugin watcher callback - continuing to watch")


def _filter_pending_plugin_changes(
    pending_changes: set[Path],
    configured_roots: tuple[Path, ...],
) -> set[Path]:
    """Drop pending changes for roots that are no longer configured."""
    return {path for path in pending_changes if _path_is_under_any_root(path, configured_roots)}


def collect_plugin_root_changes(
    configured_roots: tuple[Path, ...],
    last_snapshot_by_root: dict[Path, dict[Path, int]],
) -> set[Path]:
    """Collect edits across all currently configured plugin roots."""
    changed_paths = set()
    for root in configured_roots:
        current_snapshot = file_watcher._tree_snapshot(root)
        previous_snapshot = last_snapshot_by_root.get(root)
        last_snapshot_by_root[root] = current_snapshot
        if previous_snapshot is None:
            continue
        changed_paths.update(file_watcher._tree_changed_paths(previous_snapshot, current_snapshot))
    return changed_paths


def sync_plugin_root_snapshots(
    configured_roots: tuple[Path, ...],
    last_snapshot_by_root: dict[Path, dict[Path, int]],
) -> None:
    """Drop removed roots and seed baselines for newly configured ones."""
    configured_root_set = set(configured_roots)
    for root in tuple(last_snapshot_by_root):
        if root not in configured_root_set:
            last_snapshot_by_root.pop(root, None)
    for root in configured_roots:
        if root in last_snapshot_by_root:
            continue
        last_snapshot_by_root[root] = file_watcher._tree_snapshot(root)


def capture_plugin_root_snapshots(configured_roots: tuple[Path, ...]) -> dict[Path, dict[Path, int]]:
    """Return the current watcher baselines for one explicit plugin-root set."""
    return {root: file_watcher._tree_snapshot(root) for root in configured_roots}


def replace_plugin_root_snapshots(
    configured_roots: tuple[Path, ...],
    root_snapshots: dict[Path, dict[Path, int]],
    last_snapshot_by_root: dict[Path, dict[Path, int]],
) -> None:
    """Replace watcher baselines with the snapshots that match the applied plugin runtime."""
    configured_root_set = set(configured_roots)
    for root in tuple(last_snapshot_by_root):
        if root not in configured_root_set:
            last_snapshot_by_root.pop(root, None)
    for root in configured_roots:
        last_snapshot_by_root[root] = root_snapshots.get(root, {}).copy()


def _path_is_under_any_root(path: Path, roots: tuple[Path, ...]) -> bool:
    """Return whether one path belongs to any configured plugin root."""
    return any(path == root or path.is_relative_to(root) for root in roots)
