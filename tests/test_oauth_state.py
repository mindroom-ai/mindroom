"""Tests for OAuth state persistence."""

# ruff: noqa: D103

from __future__ import annotations

import json
import subprocess
import sys
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from mindroom.constants import resolve_primary_runtime_paths
from mindroom.oauth.providers import OAuthProviderError
from mindroom.oauth.state import consume_opaque_oauth_state, issue_opaque_oauth_state, read_opaque_oauth_state

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


_ISSUE_STATE_CHILD_SCRIPT = """
import os
import sys
from pathlib import Path

from mindroom.constants import resolve_primary_runtime_paths
from mindroom.oauth.state import issue_opaque_oauth_state

runtime_paths = resolve_primary_runtime_paths(
    config_path=Path(sys.argv[1]),
    storage_path=Path(sys.argv[2]),
    process_env={},
)
token = issue_opaque_oauth_state(
    runtime_paths,
    kind="test_state",
    ttl_seconds=60,
    data={"pid": os.getpid()},
)
Path(sys.argv[3]).write_text(token, encoding="utf-8")
"""


def _state_file(storage_root: Path) -> Path:
    return storage_root / "oauth_state" / "oauth_state.json"


def _start_issue_state_child(config_path: Path, storage_root: Path, token_path: Path) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            _ISSUE_STATE_CHILD_SCRIPT,
            str(config_path),
            str(storage_root),
            str(token_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_issue_opaque_oauth_state_keeps_concurrent_process_writes(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    storage_root = tmp_path / "storage"
    config_path.write_text("models: {}\nagents: {}\n", encoding="utf-8")
    token_paths = [tmp_path / "token-1.txt", tmp_path / "token-2.txt"]
    processes = [
        _start_issue_state_child(config_path, storage_root, token_paths[0]),
        _start_issue_state_child(config_path, storage_root, token_paths[1]),
    ]

    try:
        results: list[tuple[int | None, str, str]] = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=10)
            results.append((process.returncode, stdout, stderr))

        assert {returncode for returncode, _stdout, _stderr in results} == {0}, results
        tokens = {token_path.read_text(encoding="utf-8") for token_path in token_paths}
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate()

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


def test_consume_opaque_oauth_state_removes_token_before_kind_validation(tmp_path: Path) -> None:
    runtime_paths = resolve_primary_runtime_paths(storage_path=tmp_path / "storage", process_env={})
    token = issue_opaque_oauth_state(
        runtime_paths,
        kind="other_state",
        ttl_seconds=60,
        data={"value": "stored"},
    )

    with pytest.raises(OAuthProviderError, match="OAuth state does not match this integration"):
        consume_opaque_oauth_state(runtime_paths, kind="test_state", token=token)

    with pytest.raises(OAuthProviderError, match="OAuth state is invalid or expired"):
        read_opaque_oauth_state(runtime_paths, kind="other_state", token=token)


@pytest.mark.parametrize(
    "accessor",
    [read_opaque_oauth_state, consume_opaque_oauth_state],
)
def test_opaque_oauth_state_rejects_non_mapping_data(
    tmp_path: Path,
    accessor: Callable[..., dict[str, Any]],
) -> None:
    runtime_paths = resolve_primary_runtime_paths(storage_path=tmp_path / "storage", process_env={})
    token = issue_opaque_oauth_state(
        runtime_paths,
        kind="test_state",
        ttl_seconds=60,
        data={"value": "stored"},
    )
    stored = json.loads(_state_file(runtime_paths.storage_root).read_text(encoding="utf-8"))
    stored["states"][token]["data"] = "invalid"
    _state_file(runtime_paths.storage_root).write_text(json.dumps(stored), encoding="utf-8")

    with pytest.raises(OAuthProviderError, match="OAuth state is invalid or expired"):
        accessor(runtime_paths, kind="test_state", token=token)


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
