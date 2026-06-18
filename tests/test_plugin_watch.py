"""Tests for plugin watcher snapshot collection."""

from collections.abc import Callable
from pathlib import Path

import pytest

from mindroom.orchestration import plugin_watch


@pytest.mark.asyncio
async def test_plugin_change_collection_offloads_tree_snapshots(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Plugin root scans should run through the thread offload boundary."""
    root = tmp_path / "plugins"
    plugin_file = root / "plugin.py"
    last_snapshot_by_root: dict[Path, dict[Path, int]] = {root: {}}
    to_thread_calls = 0

    def fake_tree_snapshot(path: Path) -> dict[Path, int]:
        assert path == root
        return {plugin_file: 1}

    async def fake_to_thread(function: Callable[..., object], *args: object, **kwargs: object) -> object:
        nonlocal to_thread_calls
        to_thread_calls += 1
        return function(*args, **kwargs)

    monkeypatch.setattr(plugin_watch.file_watcher, "_tree_snapshot", fake_tree_snapshot)
    monkeypatch.setattr(plugin_watch.asyncio, "to_thread", fake_to_thread)

    changed_paths = await plugin_watch._collect_plugin_root_changes((root,), last_snapshot_by_root)

    assert changed_paths == {plugin_file}
    assert last_snapshot_by_root[root] == {plugin_file: 1}
    assert to_thread_calls == 1


@pytest.mark.asyncio
async def test_plugin_change_collection_preserves_concurrently_replaced_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Stale thread snapshots should not overwrite a concurrently replaced baseline."""
    root = tmp_path / "plugins"
    plugin_file = root / "plugin.py"
    original_snapshot = {plugin_file: 1}
    replaced_snapshot = {plugin_file: 3}
    stale_thread_snapshot = {plugin_file: 2}
    last_snapshot_by_root: dict[Path, dict[Path, int]] = {root: original_snapshot}

    def fake_tree_snapshot(path: Path) -> dict[Path, int]:
        assert path == root
        return stale_thread_snapshot

    async def fake_to_thread(function: Callable[..., object], *args: object, **kwargs: object) -> object:
        result = function(*args, **kwargs)
        last_snapshot_by_root[root] = replaced_snapshot
        return result

    monkeypatch.setattr(plugin_watch.file_watcher, "_tree_snapshot", fake_tree_snapshot)
    monkeypatch.setattr(plugin_watch.asyncio, "to_thread", fake_to_thread)

    changed_paths = await plugin_watch._collect_plugin_root_changes((root,), last_snapshot_by_root)

    assert changed_paths == set()
    assert last_snapshot_by_root[root] is replaced_snapshot
