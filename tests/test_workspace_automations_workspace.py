"""Tests for workspace resolution needed by workspace automations."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.tool_system.worker_routing import agent_workspace_root_path

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def runtime_paths(tmp_path: Path) -> RuntimePaths:
    """Create isolated runtime paths for workspace resolution."""
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_NAMESPACE": "",
        },
    )


def _config(runtime_paths: RuntimePaths, agent: dict[str, object]) -> Config:
    return Config.validate_with_runtime(
        {
            "memory": {"backend": "none"},
            "agents": {"ops": {"display_name": "Ops", **agent}},
        },
        runtime_paths,
    )


def test_shared_agent_with_enabled_workspace_automations_gets_workspace_without_file_memory(
    runtime_paths: RuntimePaths,
) -> None:
    """Enabled shared agents should have a canonical workspace even without file memory."""
    config = _config(
        runtime_paths,
        {
            "workspace_automations": {"enabled": True},
        },
    )

    runtime = resolve_agent_runtime("ops", config, runtime_paths, execution_identity=None, create=True)

    expected_root = agent_workspace_root_path(runtime_paths.storage_root, "ops")
    assert runtime.workspace is not None
    assert runtime.workspace.root == expected_root
    assert runtime.tool_base_dir == expected_root
    assert expected_root.is_dir()


def test_disabled_shared_agent_without_file_memory_keeps_no_workspace(runtime_paths: RuntimePaths) -> None:
    """Disabled shared agents should keep the existing no-workspace behavior."""
    config = _config(
        runtime_paths,
        {
            "workspace_automations": {"enabled": False},
        },
    )

    runtime = resolve_agent_runtime("ops", config, runtime_paths, execution_identity=None, create=True)

    expected_root = agent_workspace_root_path(runtime_paths.storage_root, "ops")
    assert runtime.workspace is None
    assert runtime.tool_base_dir is None
    assert runtime.file_memory_root is None
    assert not expected_root.exists()


def test_automation_only_workspace_does_not_enable_file_memory(runtime_paths: RuntimePaths) -> None:
    """Automation-only workspaces should not implicitly enable file-backed memory."""
    config = _config(
        runtime_paths,
        {
            "workspace_automations": {"enabled": True},
        },
    )

    runtime = resolve_agent_runtime("ops", config, runtime_paths, execution_identity=None, create=True)

    assert runtime.workspace is not None
    assert runtime.workspace.file_memory_path is None
    assert runtime.file_memory_root is None


def test_file_memory_workspace_keeps_file_memory_path_at_root(runtime_paths: RuntimePaths) -> None:
    """Shared file-memory agents should keep using the workspace root for file memory."""
    config = _config(
        runtime_paths,
        {
            "memory_backend": "file",
            "workspace_automations": {"enabled": False},
        },
    )

    runtime = resolve_agent_runtime("ops", config, runtime_paths, execution_identity=None, create=True)

    expected_root = agent_workspace_root_path(runtime_paths.storage_root, "ops")
    assert runtime.workspace is not None
    assert runtime.workspace.root == expected_root
    assert runtime.workspace.file_memory_path == expected_root
    assert runtime.file_memory_root == expected_root
