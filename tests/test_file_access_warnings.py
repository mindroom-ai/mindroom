"""Tests for the warning about unconfined primary-process tools next to worker-isolated code tools."""

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
    "Agent isolates code tools in a worker, but primary-process tools that are not confined "
    "by file_access can read runtime secrets"
)


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "mindroom_data")


def _warnings(config: Config, runtime_paths: RuntimePaths) -> list[dict[str, object]]:
    with capture_logs() as logs:
        warn_about_config_risks(config, runtime_paths)
    return [log for log in logs if log["event"] == _WARNING]


def _config(tools: list[str], agent: dict[str, object]) -> Config:
    return Config.model_validate({"agents": {"coder": {"display_name": "Coder", "tools": tools, **agent}}})


@pytest.mark.parametrize(
    ("tools", "agent", "unconfined"),
    [
        (["shell", "gmail"], {"file_access": "unrestricted", "worker_tools": ["shell"]}, ["gmail"]),
        (["shell", "duckdb"], {"worker_tools": ["shell"]}, ["duckdb"]),
    ],
)
def test_warns_when_worker_code_tools_sit_next_to_unconfined_primary_tools(
    tmp_path: Path,
    tools: list[str],
    agent: dict[str, object],
    unconfined: list[str],
) -> None:
    """Unrestricted path tools or unconfined primary tools next to a worker-isolated shell defeat the worker."""
    warnings = _warnings(_config(tools, agent), _runtime_paths(tmp_path))

    assert len(warnings) == 1
    assert warnings[0]["agent"] == "coder"
    assert warnings[0]["worker_code_tools"] == ["shell"]
    assert warnings[0]["unconfined_primary_tools"] == unconfined


@pytest.mark.parametrize(
    ("tools", "agent"),
    [
        (["shell", "gmail"], {"worker_tools": ["shell"]}),
        (["shell", "gmail"], {"file_access": "workspace", "worker_tools": ["shell"]}),
        (["shell", "duckdb"], {"file_access": "unrestricted", "worker_tools": []}),
        (["gmail", "duckdb"], {"file_access": "unrestricted", "worker_tools": ["gmail"]}),
    ],
)
def test_no_warning_when_primary_tools_cannot_bypass_a_worker(
    tmp_path: Path,
    tools: list[str],
    agent: dict[str, object],
) -> None:
    """Confined primary tools, full-trust agents, and agents without worker code tools are consistent setups."""
    assert _warnings(_config(tools, agent), _runtime_paths(tmp_path)) == []
