"""Plugin watcher loop and snapshot state for the orchestrator."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from mindroom import file_watcher
from mindroom.logging_config import get_logger
from mindroom.tool_system.plugins import get_configured_plugin_roots

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.plugins import PluginReloadResult

logger = get_logger(__name__)


@dataclass
class PluginWatchState:
    """Watcher baselines and dirty-state revision for the configured plugin roots."""

    runtime_paths: RuntimePaths
    last_snapshot_by_root: dict[Path, dict[Path, int]] = field(default_factory=dict)
    revision: int = 0

    def _configured_roots(self, config: Config | None) -> tuple[Path, ...]:
        """Return the plugin roots configured by one config snapshot."""
        if config is None:
            return ()
        return get_configured_plugin_roots(config, self.runtime_paths)

    def sync_roots(self, config: Config | None) -> tuple[Path, ...]:
        """Drop removed roots and seed baselines for newly configured ones."""
        configured_roots = self._configured_roots(config)
        _drop_unconfigured_plugin_root_snapshots(configured_roots, self.last_snapshot_by_root)
        for root in configured_roots:
            if root not in self.last_snapshot_by_root:
                self.last_snapshot_by_root[root] = file_watcher._tree_snapshot(root)
        return configured_roots

    def capture(self, config: Config | None) -> tuple[tuple[Path, ...], dict[Path, dict[Path, int]]]:
        """Return the configured roots and their current snapshots without committing them."""
        configured_roots = self._configured_roots(config)
        return configured_roots, {root: file_watcher._tree_snapshot(root) for root in configured_roots}

    def replace_snapshots(
        self,
        configured_roots: tuple[Path, ...],
        root_snapshots: dict[Path, dict[Path, int]],
    ) -> None:
        """Replace watcher baselines and clear any stale pending dirty state."""
        _drop_unconfigured_plugin_root_snapshots(configured_roots, self.last_snapshot_by_root)
        for root in configured_roots:
            self.last_snapshot_by_root[root] = root_snapshots[root]
        self.revision += 1

    def refresh(self, config: Config | None) -> tuple[Path, ...]:
        """Capture fresh watcher baselines for the current plugin roots."""
        configured_roots, root_snapshots = self.capture(config)
        self.replace_snapshots(configured_roots, root_snapshots)
        return configured_roots


class _PluginWatcherRuntime(Protocol):
    """Minimal orchestrator surface needed by the plugin watcher."""

    running: bool
    config: Config | None
    plugin_watch: PluginWatchState

    async def reload_plugins_now(
        self,
        *,
        source: str,
        changed_paths: tuple[Path, ...] = (),
    ) -> PluginReloadResult:
        """Rebuild and atomically swap the live plugin registry snapshot."""


async def watch_plugins_task(orchestrator: _PluginWatcherRuntime) -> None:
    """Watch configured plugin roots and hot-reload them after debounced edits."""
    watch_state = orchestrator.plugin_watch
    pending_changes = set()
    last_change_at: float | None = None
    loop = asyncio.get_running_loop()

    while not orchestrator.running:  # noqa: ASYNC110
        await asyncio.sleep(0.1)

    configured_roots = watch_state.sync_roots(orchestrator.config)
    watch_state_revision = watch_state.revision

    while orchestrator.running:
        await asyncio.sleep(file_watcher._WATCH_SCAN_INTERVAL_SECONDS)

        try:
            config = orchestrator.config
            if config is None:
                continue

            configured_roots = watch_state.sync_roots(config)
            if watch_state_revision != watch_state.revision:
                pending_changes.clear()
                last_change_at = None
                watch_state_revision = watch_state.revision
            pending_changes = _filter_pending_plugin_changes(pending_changes, configured_roots)
            changed_paths = await _collect_plugin_root_changes(configured_roots, watch_state.last_snapshot_by_root)

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


async def _collect_plugin_root_changes(
    configured_roots: tuple[Path, ...],
    last_snapshot_by_root: dict[Path, dict[Path, int]],
) -> set[Path]:
    """Collect edits across all currently configured plugin roots."""
    previous_snapshot_by_root = {root: last_snapshot_by_root.get(root) for root in configured_roots}
    current_snapshots = await asyncio.to_thread(_capture_plugin_root_snapshots, configured_roots)
    changed_paths = set()
    for root in configured_roots:
        current_snapshot = current_snapshots[root]
        previous_snapshot = previous_snapshot_by_root[root]
        if last_snapshot_by_root.get(root) is not previous_snapshot:
            continue
        last_snapshot_by_root[root] = current_snapshot
        if previous_snapshot is None:
            continue
        changed_paths.update(file_watcher._tree_changed_paths(previous_snapshot, current_snapshot))
    return changed_paths


def _capture_plugin_root_snapshots(configured_roots: tuple[Path, ...]) -> dict[Path, dict[Path, int]]:
    """Capture plugin root snapshots away from the event loop."""
    return {root: file_watcher._tree_snapshot(root) for root in configured_roots}


def _drop_unconfigured_plugin_root_snapshots(
    configured_roots: tuple[Path, ...],
    last_snapshot_by_root: dict[Path, dict[Path, int]],
) -> None:
    for root in set(last_snapshot_by_root) - set(configured_roots):
        last_snapshot_by_root.pop(root, None)


def _path_is_under_any_root(path: Path, roots: tuple[Path, ...]) -> bool:
    """Return whether one path belongs to any configured plugin root."""
    return any(path == root or path.is_relative_to(root) for root in roots)
