"""Tests for the deprecated `dynamic_workflow` -> `workflow` alias and retired usage records."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError
from structlog.testing import capture_logs

import mindroom.tools  # noqa: F401
from mindroom.config.contacts import ContactsConfig
from mindroom.config.main import Config, ConfigRuntimeValidationError
from mindroom.config.tool_aliases import TOOL_ALIASES, canonical_tool_name
from mindroom.constants import resolve_runtime_paths
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.llm.usage import RunMetrics, TokenCounts
from mindroom.session_storage.agent_storage import create_agent_state_storage
from mindroom.session_storage.schema import usage_upsert_sql
from mindroom.session_storage.types import ConversationSession
from mindroom.usage.stats import collect_admin_usage
from mindroom.usage.stats_storage import _extract_run
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


@pytest.mark.parametrize("tool_name", ["workflow", "dynamic_workflow"])
def test_contacts_tools_rejects_workflow_and_dynamic_workflow(tool_name: str) -> None:
    """Contact turns forbid both `workflow` and its deprecated `dynamic_workflow` alias."""
    with pytest.raises(ValidationError, match=r"contacts\.tools may not include workflow"):
        ContactsConfig(tools=[tool_name])


def test_legacy_dynamic_workflow_usage_row_is_skipped_cleanly(tmp_path: Path) -> None:
    """Historical SQLite usage rows with kind='dynamic_workflow' are skipped cleanly without crashing."""
    legacy_row = {
        "run_id": "dynamic_workflow:deadbeef",
        "parent_run_id": None,
        "team_id": None,
        "user_id": "@alice:localhost",
        "model_provider": "openai",
        "model": "gpt-6-astra",
        "created_at": 1790000000,
        "metrics": {"input_tokens": 20, "output_tokens": 2, "total_tokens": 22},
        "requests": [],
        "kind": "dynamic_workflow",
    }
    assert _extract_run(legacy_row, row_requester="@alice:localhost", include_team_members=False) is None

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
    assert report.totals.total_tokens == 150
    assert report.request_breakdown is not None
    assert all(row.kind != "dynamic_workflow" for row in report.request_breakdown)
