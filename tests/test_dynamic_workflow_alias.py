"""Tests for the deprecated `dynamic_workflow` -> `workflow` alias and legacy usage records."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, get_args

import pytest
from pydantic import ValidationError
from structlog.testing import capture_logs

import mindroom.tools  # noqa: F401
from mindroom.budgets.pricing import PricedModel
from mindroom.budgets.spend import collect_monthly_spend
from mindroom.config.contacts import ContactsConfig
from mindroom.config.main import Config, ConfigRuntimeValidationError
from mindroom.config.models import ModelPricing
from mindroom.config.tool_aliases import TOOL_ALIASES, canonical_tool_name
from mindroom.constants import resolve_runtime_paths
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.custom_tools.workflow import WorkflowTools
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.llm.usage import RunMetrics, TokenCounts
from mindroom.session_storage.agent_storage import create_agent_state_storage
from mindroom.session_storage.schema import usage_upsert_sql
from mindroom.session_storage.types import ConversationSession
from mindroom.tool_system.agent_tools import resolve_agent_tools
from mindroom.tool_system.metadata import ToolConfigOverrideError, get_tool_by_name
from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.usage.stats import collect_admin_usage
from mindroom.usage.stats_storage import _extract_run
from mindroom.usage.storage import LEGACY_USAGE_KINDS, USAGE_KINDS, IndependentUsageKind
from tests.conftest import persist_entity_accounts, persist_runs, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path


def _base_config(**extra: object) -> dict[str, object]:
    return {
        "models": {"default": {"provider": "openai", "id": "gpt-6-astra"}},
        "agents": {"planner": {"display_name": "Planner"}},
        **extra,
    }


def _warning_events(logs: list[dict[str, object]]) -> list[str]:
    return [str(entry["event"]) for entry in logs if entry.get("log_level") == "warning"]


def test_dynamic_workflow_alias_table() -> None:
    """`dynamic_workflow` maps to `workflow` in the canonical alias table."""
    assert TOOL_ALIASES["dynamic_workflow"] == "workflow"
    assert canonical_tool_name("dynamic_workflow") == "workflow"


@pytest.mark.parametrize(
    ("tools_config", "layer"),
    [
        ({"agents": {"planner": {"display_name": "Planner", "tools": ["dynamic_workflow"]}}}, "agent"),
        ({"defaults": {"tools": ["dynamic_workflow"]}}, "defaults"),
    ],
)
def test_dynamic_workflow_bare_agent_and_defaults_alias_with_warning(
    tmp_path: Path,
    tools_config: dict[str, object],
    layer: str,
) -> None:
    """Bare `dynamic_workflow` in agent or default tools loads as `workflow` with a one-line warning."""
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")
    with capture_logs() as logs:
        config = Config.validate_with_runtime(_base_config(**tools_config), runtime_paths)

    if layer == "agent":
        assert [entry.name for entry in config.agents["planner"].tools] == ["workflow"]
    else:
        assert [entry.name for entry in config.defaults.tools] == ["workflow"]
    assert "workflow" in config.resolve_agent("planner").tools.tool_names

    warnings = _warning_events(logs)
    assert len(warnings) == 1
    assert "\n" not in warnings[0]
    assert "dynamic_workflow" in warnings[0]
    assert "workflow" in warnings[0]
    assert "allowed_tools" not in warnings[0]


def test_dynamic_workflow_with_defer_loads_as_workflow_and_warns(tmp_path: Path) -> None:
    """`{"dynamic_workflow": {"defer": true}}` loads as `workflow` with `defer=True` and warns."""
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")
    with capture_logs() as logs:
        config = Config.validate_with_runtime(
            _base_config(
                agents={"planner": {"display_name": "Planner", "tools": [{"dynamic_workflow": {"defer": True}}]}},
            ),
            runtime_paths,
        )

    [entry] = config.agents["planner"].tools
    assert entry.name == "workflow"
    assert entry.defer is True
    assert entry.overrides == {}
    warnings = _warning_events(logs)
    assert len(warnings) == 1
    assert "\n" not in warnings[0]
    assert "dynamic_workflow" in warnings[0]
    assert "workflow" in warnings[0]


@pytest.mark.parametrize(
    ("authored_entry", "expected_defer"),
    [
        ({"dynamic_workflow": {"allowed_tools": ["shell", "filesystem"]}}, False),
        ({"dynamic_workflow": {"defer": True, "allowed_tools": ["*"]}}, True),
    ],
)
def test_dynamic_workflow_strips_allowed_tools_and_warns(
    tmp_path: Path,
    authored_entry: dict[str, object],
    expected_defer: bool,
) -> None:
    """`allowed_tools` on the `dynamic_workflow` alias is ignored and noted in the one-line warning."""
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")
    with capture_logs() as logs:
        config = Config.validate_with_runtime(
            _base_config(agents={"planner": {"display_name": "Planner", "tools": [authored_entry]}}),
            runtime_paths,
        )

    [entry] = config.agents["planner"].tools
    assert entry.name == "workflow"
    assert entry.defer is expected_defer
    assert entry.overrides == {}

    warnings = _warning_events(logs)
    assert len(warnings) == 1
    assert "\n" not in warnings[0]
    assert "allowed_tools" in warnings[0]
    assert "inherits its caller's tools" in warnings[0]


@pytest.mark.parametrize(
    ("tools_config", "expect_allowed_tools_note"),
    [
        (
            {"agents": {"planner": {"display_name": "Planner", "tools": ["dynamic_workflow", "workflow"]}}},
            False,
        ),
        (
            {
                "agents": {
                    "planner": {
                        "display_name": "Planner",
                        "tools": [{"dynamic_workflow": {"allowed_tools": ["*"]}}, "workflow"],
                    },
                },
            },
            True,
        ),
        (
            {
                "agents": {
                    "planner": {
                        "display_name": "Planner",
                        "tools": ["workflow", {"dynamic_workflow": {"allowed_tools": ["*"]}}],
                    },
                },
            },
            True,
        ),
        (
            {
                "defaults": {"tools": ["dynamic_workflow"]},
                "agents": {"planner": {"display_name": "Planner", "tools": ["workflow"]}},
            },
            False,
        ),
    ],
)
def test_dynamic_workflow_deduplicates_with_workflow(
    tmp_path: Path,
    tools_config: dict[str, object],
    expect_allowed_tools_note: bool,
) -> None:
    """Combining `dynamic_workflow` and `workflow` keeps `workflow` once and logs one warning."""
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")
    with capture_logs() as logs:
        config = Config.validate_with_runtime(_base_config(**tools_config), runtime_paths)

    assert config.agents["planner"].tool_names == ["workflow"]
    assert config.resolve_agent("planner").tools.tool_names.count("workflow") == 1
    warnings = _warning_events(logs)
    assert len(warnings) == 1
    assert "\n" not in warnings[0]
    assert ("allowed_tools" in warnings[0]) is expect_allowed_tools_note


@pytest.mark.parametrize(
    ("tools_config", "expected_defer", "expect_allowed_tools_note"),
    [
        pytest.param(
            {
                "agents": {
                    "planner": {
                        "display_name": "Planner",
                        "tools": ["dynamic_workflow", {"workflow": {"max_concurrent_runs": 2, "defer": True}}],
                    },
                },
            },
            True,
            False,
            id="same-layer-bare-then-configured",
        ),
        pytest.param(
            {
                "agents": {
                    "planner": {
                        "display_name": "Planner",
                        "tools": [{"workflow": {"max_concurrent_runs": 2, "defer": True}}, "dynamic_workflow"],
                    },
                },
            },
            True,
            False,
            id="same-layer-configured-then-bare",
        ),
        pytest.param(
            {
                "agents": {
                    "planner": {
                        "display_name": "Planner",
                        "tools": [
                            {"dynamic_workflow": {"allowed_tools": ["*"]}},
                            {"workflow": {"max_concurrent_runs": 2, "defer": True}},
                        ],
                    },
                },
            },
            True,
            True,
            id="same-layer-allowed-tools-then-configured",
        ),
        pytest.param(
            {
                "agents": {
                    "planner": {
                        "display_name": "Planner",
                        "tools": [
                            {"workflow": {"max_concurrent_runs": 2, "defer": True}},
                            {"dynamic_workflow": {"allowed_tools": ["*"]}},
                        ],
                    },
                },
            },
            True,
            True,
            id="same-layer-configured-then-allowed-tools",
        ),
        pytest.param(
            {
                "defaults": {"tools": ["dynamic_workflow"]},
                "agents": {
                    "planner": {
                        "display_name": "Planner",
                        "tools": [{"workflow": {"max_concurrent_runs": 2, "defer": True}}],
                    },
                },
            },
            True,
            False,
            id="defaults-bare-agent-configured",
        ),
        pytest.param(
            {
                "defaults": {"tools": [{"workflow": {"max_concurrent_runs": 2}}]},
                "agents": {"planner": {"display_name": "Planner", "tools": ["dynamic_workflow"]}},
            },
            False,
            False,
            id="defaults-configured-agent-bare",
        ),
        pytest.param(
            {
                "defaults": {"tools": [{"workflow": {"max_concurrent_runs": 2}}]},
                "agents": {
                    "planner": {
                        "display_name": "Planner",
                        "tools": [{"dynamic_workflow": {"allowed_tools": ["*"]}}],
                    },
                },
            },
            False,
            True,
            id="defaults-configured-agent-allowed-tools",
        ),
    ],
)
def test_dual_tool_plain_dynamic_workflow_merges_with_configured_workflow(
    tmp_path: Path,
    tools_config: dict[str, object],
    expected_defer: bool,
    expect_allowed_tools_note: bool,
) -> None:
    """A plain (or allowed_tools-only) `dynamic_workflow` merges cleanly with a configured `workflow` across layers."""
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")
    with capture_logs() as logs:
        config = Config.validate_with_runtime(_base_config(**tools_config), runtime_paths)

    candidates = [
        entry
        for entry in resolve_agent_tools(config, "planner", builtin_tool_registry()).candidates()
        if entry.name == "workflow"
    ]
    assert len(candidates) == 1
    assert candidates[0].tool_config_overrides == {"max_concurrent_runs": 2}
    assert candidates[0].defer is expected_defer

    warnings = _warning_events(logs)
    assert len(warnings) == 1
    assert ("allowed_tools" in warnings[0]) is expect_allowed_tools_note


@pytest.mark.parametrize(
    "tools_config",
    [
        pytest.param(
            {
                "agents": {
                    "planner": {
                        "display_name": "Planner",
                        "tools": [{"dynamic_workflow": {"defer": True}}, "workflow"],
                    },
                },
            },
            id="same-layer-defer-conflict",
        ),
        pytest.param(
            {
                "agents": {
                    "planner": {
                        "display_name": "Planner",
                        "tools": [
                            {"dynamic_workflow": {"defer": False}},
                            {"workflow": {"defer": True}},
                        ],
                    },
                },
            },
            id="same-layer-explicit-defer-false-conflict",
        ),
        pytest.param(
            {
                "agents": {
                    "planner": {
                        "display_name": "Planner",
                        "tools": [
                            {"dynamic_workflow": {"max_concurrent_runs": 1}},
                            {"workflow": {"max_concurrent_runs": 2}},
                        ],
                    },
                },
            },
            id="same-layer-override-conflict",
        ),
        pytest.param(
            {
                "defaults": {"tools": [{"dynamic_workflow": {"max_concurrent_runs": 1}}]},
                "agents": {
                    "planner": {
                        "display_name": "Planner",
                        "tools": [{"workflow": {"max_concurrent_runs": 2}}],
                    },
                },
            },
            id="cross-layer-override-conflict",
        ),
    ],
)
def test_dual_tool_conflicting_dynamic_workflow_settings_still_rejected(
    tmp_path: Path,
    tools_config: dict[str, object],
) -> None:
    """Conflicting settings or lazy flags between `dynamic_workflow` and `workflow` are still rejected."""
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")
    with pytest.raises((ValidationError, ConfigRuntimeValidationError), match="both select workflow"):
        Config.validate_with_runtime(_base_config(**tools_config), runtime_paths)


def test_workflow_direct_allowed_tools_still_rejected(tmp_path: Path) -> None:
    """`allowed_tools` on `workflow` (not the legacy alias) is rejected as an unknown override."""
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")
    with pytest.raises(ConfigRuntimeValidationError, match="allowed_tools"):
        Config.validate_with_runtime(
            _base_config(
                agents={"planner": {"display_name": "Planner", "tools": [{"workflow": {"allowed_tools": ["*"]}}]}},
            ),
            runtime_paths,
        )


def test_dashboard_stored_dynamic_workflow_allowed_tools_does_not_block_workflow(tmp_path: Path) -> None:
    """Dashboard-saved `allowed_tools` under service `dynamic_workflow` is ignored; other stored keys still fail."""
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")
    config = Config.validate_with_runtime(
        _base_config(agents={"planner": {"display_name": "Planner", "tools": ["workflow"]}}),
        runtime_paths,
    )
    manager = get_runtime_credentials_manager(runtime_paths)
    manager.save_credentials("dynamic_workflow", {"_source": "ui", "allowed_tools": ["*"]})

    toolkit = get_tool_by_name(
        "workflow",
        runtime_paths,
        runtime_config=config,
        tools=builtin_tool_registry(),
        worker_target=None,
    )
    assert isinstance(toolkit, WorkflowTools)

    manager.save_credentials("dynamic_workflow", {"_source": "ui", "allowed_tools": ["*"], "unexpected": True})
    with pytest.raises(ToolConfigOverrideError, match="settings stored under 'dynamic_workflow' no longer apply"):
        get_tool_by_name(
            "workflow",
            runtime_paths,
            runtime_config=config,
            tools=builtin_tool_registry(),
            worker_target=None,
        )


@pytest.mark.parametrize(
    ("data_extra", "expect_allowed_tools_note"),
    [
        pytest.param(
            {
                "defaults": {"tools": ["dynamic_workflow"]},
                "personal_agents": {
                    "helper": {"agent": {"display_name": "{user_display_name}'s Helper"}},
                },
            },
            False,
            id="defaults-with-unowned-template",
        ),
        pytest.param(
            {
                "defaults": {"tools": []},
                "personal_agents": {
                    "helper": {
                        "agent": {
                            "display_name": "{user_display_name}'s Helper",
                            "tools": [{"dynamic_workflow": {"allowed_tools": ["*"]}}],
                        },
                    },
                },
            },
            True,
            id="unowned-template-only-with-allowed-tools",
        ),
    ],
)
def test_validate_with_runtime_logs_deprecation_warning_once_with_unowned_personal_agent_template(
    tmp_path: Path,
    data_extra: dict[str, object],
    expect_allowed_tools_note: bool,
) -> None:
    """`Config.validate_with_runtime` logs the deprecation warning exactly once even when probing personal_agents."""
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")
    with capture_logs() as logs:
        Config.validate_with_runtime(_base_config(**data_extra), runtime_paths)

    warnings = _warning_events(logs)
    assert len(warnings) == 1
    assert "\n" not in warnings[0]
    assert "dynamic_workflow" in warnings[0]
    assert ("allowed_tools" in warnings[0]) is expect_allowed_tools_note


@pytest.mark.parametrize("tool_name", ["workflow", "dynamic_workflow"])
def test_contacts_tools_rejects_workflow_and_dynamic_workflow(tool_name: str) -> None:
    """Contact turns forbid both `workflow` and its deprecated `dynamic_workflow` alias."""
    with pytest.raises(ValidationError, match=r"contacts\.tools may not include workflow"):
        ContactsConfig(tools=[tool_name])


def test_legacy_dynamic_workflow_usage_row_is_preserved(tmp_path: Path) -> None:
    """Historical SQLite usage rows with kind='dynamic_workflow' keep counting in usage reports and monthly budgets."""
    assert "dynamic_workflow" not in get_args(IndependentUsageKind.__value__)
    assert "dynamic_workflow" in LEGACY_USAGE_KINDS
    assert "dynamic_workflow" in USAGE_KINDS

    now = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    created_at = int(now.timestamp())
    legacy_row = {
        "run_id": "dynamic_workflow:deadbeef",
        "parent_run_id": None,
        "team_id": None,
        "user_id": "@alice:localhost",
        "model_provider": "openai",
        "model": "gpt-6-astra",
        "created_at": created_at,
        "metrics": {"input_tokens": 20, "output_tokens": 2, "total_tokens": 22},
        "requests": [],
        "kind": "dynamic_workflow",
    }
    extracted = _extract_run(legacy_row, row_requester="@alice:localhost", include_team_members=False)
    assert extracted is not None
    assert extracted.kind == "dynamic_workflow"

    paths = test_runtime_paths(tmp_path)
    config = Config.model_validate(
        {
            "models": {"default": {"provider": "openai", "id": "gpt-6-astra"}},
            "agents": {"planner": {"display_name": "Planner"}},
        },
    )
    persist_entity_accounts(config, paths)
    storage = create_agent_state_storage("planner", paths.storage_root / "agents" / "planner")
    live_run = RunRecord(
        run_id="live-run-1",
        agent_id="planner",
        session_id="session-1",
        user_id="@alice:localhost",
        model="gpt-6-astra",
        model_provider="openai",
        created_at=created_at,
        status=RunStatus.completed,
        metrics=RunMetrics(tokens=TokenCounts(input_tokens=100, output_tokens=50, total_tokens=150)),
    )
    persist_runs(storage, ConversationSession(kind="agent", session_id="session-1", owner_id="planner"), live_run)
    with storage.transaction(write=True) as conn:
        conn.execute(
            usage_upsert_sql(storage.names),
            ("session-1", "dynamic_workflow:deadbeef", json.dumps(legacy_row)),
        )

    report = collect_admin_usage(config=config, runtime_paths=paths, include_requests=True)
    assert report.totals.total_tokens == 172

    spend = collect_monthly_spend(
        config=config,
        runtime_paths=paths,
        now=now,
        prices={
            ("openai", "gpt-6-astra"): PricedModel(
                pricing=ModelPricing(input=1.0, output=1.0),
                input_includes_cache=True,
            ),
        },
    )
    assert spend.spend_usd["@alice:localhost"] == pytest.approx(172 / 1_000_000)
