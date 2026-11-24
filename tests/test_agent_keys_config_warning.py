"""Tests for the warning about agents that opt into Agent Keys while their only command tool is a primary python."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from structlog.testing import capture_logs

from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.orchestration.config_warnings import warn_about_config_risks

if TYPE_CHECKING:
    from pathlib import Path

_WARNING = (
    "Agent lists the egress_credentials tool, but its only command tool is python in the primary process, "
    "so Agent Keys never applies to it"
)
_BROKER_AND_RUNNER = {"MINDROOM_EGRESS_BROKER_PORT": "8768", "MINDROOM_SANDBOX_PROXY_URL": "http://sandbox-runner.test"}


def _warnings(tmp_path: Path, agent: dict[str, object], process_env: dict[str, str]) -> list[dict[str, object]]:
    config = Config.model_validate(
        {"agents": {"coder": {"display_name": "Coder", "include_default_tools": False, **agent}}},
    )
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env=process_env,
    )
    with capture_logs() as logs:
        warn_about_config_risks(config, runtime_paths)
    return [log for log in logs if log["event"] == _WARNING]


def test_warns_when_agent_keys_cannot_reach_the_agents_commands(tmp_path: Path) -> None:
    """An agent that lists `egress_credentials` but has only python, in the primary, is left off the Agent Keys pages."""
    agent = {"tools": ["python", "egress_credentials"], "worker_tools": []}

    warnings = _warnings(tmp_path, agent, _BROKER_AND_RUNNER)

    assert [warning["agent"] for warning in warnings] == ["coder"]


@pytest.mark.parametrize(
    ("agent", "process_env"),
    [
        pytest.param(
            {"tools": ["shell", "egress_credentials"], "worker_tools": ["shell"]},
            _BROKER_AND_RUNNER,
            id="shell-in-worker",
        ),
        pytest.param(
            {"tools": ["shell", "python", "egress_credentials"], "worker_tools": []},
            _BROKER_AND_RUNNER,
            id="shell-in-primary",
        ),
        pytest.param(
            {"tools": ["python", "egress_credentials"], "worker_tools": ["python"]},
            _BROKER_AND_RUNNER,
            id="python-in-worker",
        ),
        pytest.param({"tools": ["shell"], "worker_tools": []}, _BROKER_AND_RUNNER, id="no-egress-tool"),
        # `egress_credentials` in `defaults.tools` reaches agents without command tools, which have nothing to route.
        pytest.param({"tools": ["egress_credentials"]}, _BROKER_AND_RUNNER, id="no-command-tool"),
        pytest.param(
            {"tools": ["python", "egress_credentials"], "worker_tools": []},
            {"MINDROOM_SANDBOX_PROXY_URL": "http://sandbox-runner.test", "MINDROOM_EGRESS_BROKER_PORT": "off"},
            id="broker-off",
        ),
    ],
)
def test_no_warning_when_agent_keys_reach_the_commands_or_are_not_asked_for(
    tmp_path: Path,
    agent: dict[str, object],
    process_env: dict[str, str],
) -> None:
    """A shell anywhere, a worker python, no egress tool, no command tool, or the broker off are consistent setups."""
    assert _warnings(tmp_path, agent, process_env) == []
