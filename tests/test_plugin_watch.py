from collections.abc import Callable
from pathlib import Path

import pytest

from mindroom.orchestration import plugin_watch


@pytest.mark.asyncio
async def test_plugin_change_collection_offloads_tree_snapshots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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
