"""Tests for the warning about unrestricted file access next to worker-isolated code tools."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from structlog.testing import capture_logs

from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.orchestration.config_warnings import warn_about_config_risks

if TYPE_CHECKING:
    from pathlib import Path

_WARNING = (
    "Agent routes code tools to a worker but has file_access 'unrestricted'; "
    "its primary-process path tools can read runtime secrets"
)


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "mindroom_data")


def _warnings(config: Config, runtime_paths: RuntimePaths) -> list[dict[str, object]]:
    with capture_logs() as logs:
        warn_about_config_risks(config, runtime_paths)
    return [log for log in logs if log["event"] == _WARNING]


def _config(agent: dict[str, object]) -> Config:
    return Config.model_validate({"agents": {"coder": {"display_name": "Coder", "tools": ["shell", "gmail"], **agent}}})


def test_warns_for_unrestricted_agent_with_worker_routed_code_tools(tmp_path: Path) -> None:
    """Unrestricted path tools next to a worker-isolated shell defeat the worker, so say so once."""
    config = _config({"file_access": "unrestricted", "worker_tools": ["shell"]})

    warnings = _warnings(config, _runtime_paths(tmp_path))

    assert len(warnings) == 1
    assert warnings[0]["agent"] == "coder"
    assert warnings[0]["worker_code_tools"] == ["shell"]


@pytest.mark.parametrize(
    "agent",
    [
        {"file_access": "unrestricted", "worker_tools": []},
        {"file_access": "workspace", "worker_tools": ["shell"]},
        {"worker_tools": ["shell"]},
    ],
)
def test_no_warning_when_path_tools_cannot_bypass_a_worker(tmp_path: Path, agent: dict[str, object]) -> None:
    """Full-trust agents and workspace-confined agents are both consistent setups."""
    assert _warnings(_config(agent), _runtime_paths(tmp_path)) == []
