"""How the chart-managed approved egress switch adds its tool and approval rule to a loaded config."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from mindroom.approval.tool_approval import evaluate_tool_approval
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.tool_system.automation_approval import build_automation_approval_config

if TYPE_CHECKING:
    from pathlib import Path

_DAY = 24 * 60 * 60
_REQUEST = {"hostnames": ["unlisted.invalid"], "ttl_minutes": 5, "reason": "Need docs."}


def _runtime_paths(tmp_path: Path, *, enabled: bool) -> RuntimePaths:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\n", encoding="utf-8")
    env = {"MINDROOM_APPROVED_EGRESS_ENABLED": "true"} if enabled else {}
    return resolve_runtime_paths(config_path=config_path, storage_path=tmp_path / "data", process_env=env)


def _config(tmp_path: Path, *, enabled: bool = True, **raw: Any) -> tuple[Config, RuntimePaths]:  # noqa: ANN401
    runtime_paths = _runtime_paths(tmp_path, enabled=enabled)
    data: dict[str, Any] = {
        "agents": {"code": {"display_name": "Code"}},
        "models": {"default": {"provider": "openai", "id": "gpt-6-astra"}},
        **raw,
    }
    return Config.validate_with_runtime(data, runtime_paths), runtime_paths


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rules", "expected"),
    [
        pytest.param([], (True, 7 * _DAY), id="no-rules"),
        pytest.param([{"match": "*", "action": "auto_approve"}], (True, 7 * _DAY), id="wildcard-auto-approve"),
        pytest.param(
            [{"match": "request_network_access", "action": "auto_approve"}],
            (True, 7 * _DAY),
            id="exact-auto-approve",
        ),
        pytest.param(
            [{"match": "request_*", "action": "require_approval", "timeout_days": 2}],
            (True, 2 * _DAY),
            id="authored-require-approval-governs",
        ),
        pytest.param(
            [{"match": "other", "action": "auto_approve"}, {"match": "*", "action": "auto_approve"}],
            (True, 7 * _DAY),
            id="later-auto-approve",
        ),
        pytest.param([{"match": "*", "script": "check.py"}], (True, 7 * _DAY), id="script-rule"),
    ],
)
async def test_request_network_access_always_needs_approval_while_the_switch_is_on(
    tmp_path: Path,
    rules: list[dict[str, object]],
    expected: tuple[bool, float],
) -> None:
    """Only an authored rule that itself requires approval governs the call; every other rule is overridden."""
    config, runtime_paths = _config(tmp_path, tool_approval={"rules": rules})
    assert await evaluate_tool_approval(config, runtime_paths, "request_network_access", _REQUEST, "code") == expected


@pytest.mark.asyncio
async def test_automation_preapproval_does_not_free_request_network_access(tmp_path: Path) -> None:
    """Unattended runs that preapprove every toolkit still ask for approved egress."""
    config, runtime_paths = _config(tmp_path)
    automation_config = build_automation_approval_config(
        config,
        function_owners={"request_network_access": frozenset({"approved_egress"})},
        preapproved_toolkits=frozenset({"*"}),
        never_preapprove_toolkits=frozenset(),
    )
    requires, _ = await evaluate_tool_approval(
        automation_config,
        runtime_paths,
        "request_network_access",
        _REQUEST,
        "code",
    )
    assert requires is True


@pytest.mark.asyncio
async def test_switch_off_leaves_authored_rules_alone(tmp_path: Path) -> None:
    """Without the switch, an authored auto-approve rule decides the call."""
    config, runtime_paths = _config(
        tmp_path,
        enabled=False,
        tool_approval={"rules": [{"match": "*", "action": "auto_approve"}]},
    )
    requires, _ = await evaluate_tool_approval(config, runtime_paths, "request_network_access", _REQUEST, "code")
    assert requires is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param({}, True, id="inherited-defaults"),
        pytest.param({"defaults": {"tools": ["calculator"]}}, True, id="authored-defaults"),
        pytest.param(
            {"agents": {"code": {"display_name": "Code", "include_default_tools": False}}},
            False,
            id="opt-out",
        ),
    ],
)
def test_the_switch_gives_default_tool_agents_the_approved_egress_tool(
    tmp_path: Path,
    raw: dict[str, Any],
    expected: bool,
) -> None:
    """Agents that take the default tools get the tool; an agent that opts out does not."""
    config, _ = _config(tmp_path, **raw)
    assert ("approved_egress" in config.resolve_agent("code").tools.tool_names) is expected


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param({}, id="inherited-defaults"),
        pytest.param({"defaults": {"tools": ["calculator"]}}, id="authored-defaults"),
        pytest.param({"defaults": {"tools": ["approved_egress"]}}, id="authored-tool"),
        pytest.param(
            {"tool_approval": {"rules": [{"match": "request_network_access", "action": "require_approval"}]}},
            id="authored-rule",
        ),
    ],
)
def test_saving_a_config_writes_back_only_what_was_authored(tmp_path: Path, raw: dict[str, Any]) -> None:
    """The switch's tool and rule never reach the saved file, and authored ones stay."""
    config, _ = _config(tmp_path, **raw)
    expected = {
        "agents": {"code": {"display_name": "Code"}},
        "models": {"default": {"provider": "openai", "id": "gpt-6-astra"}},
        **raw,
    }
    assert config.authored_model_dump() == expected
