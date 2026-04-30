"""Tests for OAuth state persistence."""

# ruff: noqa: D103

from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path

from mindroom.constants import resolve_primary_runtime_paths
from mindroom.oauth.state import issue_opaque_oauth_state, read_opaque_oauth_state


def _issue_state_child(config_path: str, storage_root: str, queue: multiprocessing.Queue) -> None:
    runtime_paths = resolve_primary_runtime_paths(
        config_path=Path(config_path),
        storage_path=Path(storage_root),
        process_env={},
    )
    token = issue_opaque_oauth_state(
        runtime_paths,
        kind="test_state",
        ttl_seconds=60,
        data={"pid": os.getpid()},
    )
    queue.put(token)


def test_issue_opaque_oauth_state_keeps_concurrent_process_writes(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    storage_root = tmp_path / "storage"
    config_path.write_text("models: {}\nagents: {}\n", encoding="utf-8")
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    processes = [
        ctx.Process(target=_issue_state_child, args=(str(config_path), str(storage_root), queue)),
        ctx.Process(target=_issue_state_child, args=(str(config_path), str(storage_root), queue)),
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)

    assert {process.exitcode for process in processes} == {0}
    tokens = {queue.get(timeout=1) for _process in processes}
    runtime_paths = resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=storage_root,
        process_env={},
    )

    for token in tokens:
        assert read_opaque_oauth_state(runtime_paths, kind="test_state", token=token)["pid"]

    stored = json.loads((storage_root / "oauth_state.json").read_text(encoding="utf-8"))
    assert tokens <= set(stored["states"])
