"""Tests for OAuth state persistence."""

# ruff: noqa: D103

from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from mindroom.constants import resolve_primary_runtime_paths
from mindroom.oauth.providers import OAuthProviderError
from mindroom.oauth.state import issue_opaque_oauth_state, read_opaque_oauth_state


def _state_file(storage_root: Path) -> Path:
    return storage_root / "oauth_state" / "oauth_state.json"


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

    stored = json.loads(_state_file(storage_root).read_text(encoding="utf-8"))
    assert tokens <= set(stored["states"])


def test_corrupt_state_file_logs_warning_and_does_not_overwrite(tmp_path: Path) -> None:
    runtime_paths = resolve_primary_runtime_paths(storage_path=tmp_path / "storage", process_env={})
    state_file = _state_file(runtime_paths.storage_root)
    state_file.parent.mkdir(parents=True)
    state_file.write_text("{not json", encoding="utf-8")

    with patch("mindroom.oauth.state.logger") as mock_logger, pytest.raises(OAuthProviderError):
        read_opaque_oauth_state(runtime_paths, kind="test_state", token="missing")  # noqa: S106

    mock_logger.warning.assert_called_once()
    corrupt_files = list(state_file.parent.glob("oauth_state.json.corrupt-*"))
    assert len(corrupt_files) == 1
    assert corrupt_files[0].read_text(encoding="utf-8") == "{not json"
    assert not state_file.exists()


def test_read_opaque_oauth_state_does_not_write_to_disk(tmp_path: Path) -> None:
    runtime_paths = resolve_primary_runtime_paths(storage_path=tmp_path / "storage", process_env={})
    token = issue_opaque_oauth_state(
        runtime_paths,
        kind="test_state",
        ttl_seconds=60,
        data={"value": "stored"},
    )

    with patch("mindroom.oauth.state._save_state_store") as mock_save:
        assert read_opaque_oauth_state(runtime_paths, kind="test_state", token=token) == {"value": "stored"}

    mock_save.assert_not_called()


def test_corrupt_state_file_renamed_to_corrupt_suffix(tmp_path: Path) -> None:
    runtime_paths = resolve_primary_runtime_paths(storage_path=tmp_path / "storage", process_env={})
    state_file = _state_file(runtime_paths.storage_root)
    state_file.parent.mkdir(parents=True)
    state_file.write_text("[", encoding="utf-8")

    with pytest.raises(OAuthProviderError):
        read_opaque_oauth_state(runtime_paths, kind="test_state", token="missing")  # noqa: S106

    corrupt_files = list(state_file.parent.glob("oauth_state.json.corrupt-*"))
    assert len(corrupt_files) == 1
    assert corrupt_files[0].name.startswith("oauth_state.json.corrupt-")
    assert corrupt_files[0].read_text(encoding="utf-8") == "["
