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
from mindroom.config.personal_agents import GeneratedAgentEdits, PersonalAgentRoster, PersonalAgentRosterEntry
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
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.tool_system.settings import ToolConfigOverrideError
from mindroom.usage.stats import collect_admin_usage
from mindroom.usage.stats_storage import _extract_run
from mindroom.usage.storage import _LEGACY_USAGE_KINDS, USAGE_KINDS, IndependentUsageKind
from tests.conftest import persist_entity_accounts, persist_runs, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths

_WF_2 = {"workflow": {"max_concurrent_runs": 2}}
_WF_2_DEFER = {"workflow": {"max_concurrent_runs": 2, "defer": True}}
_DW_ALLOWED = {"dynamic_workflow": {"allowed_tools": ["*"]}}


def _paths(tmp_path: Path) -> RuntimePaths:
    return resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")


def _cfg(
    *,
    agent_tools: list[object] | None = None,
    default_tools: list[object] | None = None,
    **extra: object,
) -> dict[str, object]:
    planner: dict[str, object] = {"display_name": "Planner"}
    if agent_tools is not None:
        planner["tools"] = agent_tools
    data: dict[str, object] = {
        "models": {"default": {"provider": "openai", "id": "gpt-6-astra"}},
        "agents": {"planner": planner},
        **extra,
    }
    defaults: dict[str, object] = {"desktop_pairing": "explicit_only"}
    if default_tools is not None:
        defaults["tools"] = default_tools
    data["defaults"] = defaults
    return data


def _load_with_warnings(
    tmp_path: Path,
    data: dict[str, object],
    *,
    roster: PersonalAgentRoster | None = None,
) -> tuple[Config, list[str]]:
    with capture_logs() as logs:
        config = Config.validate_with_runtime(data, _paths(tmp_path), roster=roster)
    return config, [str(entry["event"]) for entry in logs if entry.get("log_level") == "warning"]


def _assert_single_alias_warning(warnings: list[str], *, expect_allowed_tools: bool) -> None:
    assert len(warnings) == 1
    assert "\n" not in warnings[0]
    assert "dynamic_workflow" in warnings[0]
    assert "workflow" in warnings[0]
    assert ("allowed_tools" in warnings[0]) is expect_allowed_tools
    if expect_allowed_tools:
        assert "inherits its caller's tools" in warnings[0]


def test_dynamic_workflow_alias_table() -> None:
    """`dynamic_workflow` maps to `workflow` in the canonical alias table."""
    assert TOOL_ALIASES["dynamic_workflow"] == "workflow"
    assert canonical_tool_name("dynamic_workflow") == "workflow"


@pytest.mark.parametrize(
    ("kwargs", "layer"),
    [({"agent_tools": ["dynamic_workflow"]}, "agent"), ({"default_tools": ["dynamic_workflow"]}, "defaults")],
)
def test_dynamic_workflow_bare_agent_and_defaults_alias_with_warning(
    tmp_path: Path,
    kwargs: dict[str, list[object]],
    layer: str,
) -> None:
    """Bare `dynamic_workflow` in agent or default tools loads as `workflow` with a one-line warning."""
    config, warnings = _load_with_warnings(tmp_path, _cfg(**kwargs))
    entries = config.agents["planner"].tools if layer == "agent" else config.defaults.tools
    assert [entry.name for entry in entries] == ["workflow"]
    assert "workflow" in config.resolve_agent("planner").tools.tool_names
    _assert_single_alias_warning(warnings, expect_allowed_tools=False)


@pytest.mark.parametrize(
    ("authored_entry", "expected_defer", "expect_allowed_tools"),
    [
        ({"dynamic_workflow": {"defer": True}}, True, False),
        ({"dynamic_workflow": {"allowed_tools": ["shell", "filesystem"]}}, False, True),
        ({"dynamic_workflow": {"defer": True, "allowed_tools": ["*"]}}, True, True),
    ],
)
def test_dynamic_workflow_strips_allowed_tools_and_warns(
    tmp_path: Path,
    authored_entry: dict[str, object],
    expected_defer: bool,
    expect_allowed_tools: bool,
) -> None:
    """`defer` is preserved and legacy `allowed_tools` is stripped with a one-line warning."""
    config, warnings = _load_with_warnings(tmp_path, _cfg(agent_tools=[authored_entry]))
    [entry] = config.agents["planner"].tools
    assert entry.name == "workflow"
    assert entry.defer is expected_defer
    assert entry.overrides == {}
    _assert_single_alias_warning(warnings, expect_allowed_tools=expect_allowed_tools)


@pytest.mark.parametrize(
    ("agent_tools", "default_tools", "expect_allowed_tools"),
    [
        (["dynamic_workflow", "workflow"], None, False),
        ([_DW_ALLOWED, "workflow"], None, True),
        (["workflow", _DW_ALLOWED], None, True),
        (["workflow"], ["dynamic_workflow"], False),
    ],
)
def test_dynamic_workflow_deduplicates_with_workflow(
    tmp_path: Path,
    agent_tools: list[object],
    default_tools: list[object] | None,
    expect_allowed_tools: bool,
) -> None:
    """Combining `dynamic_workflow` and `workflow` keeps `workflow` once and logs one warning."""
    config, warnings = _load_with_warnings(tmp_path, _cfg(agent_tools=agent_tools, default_tools=default_tools))
    assert config.agents["planner"].tool_names == ["workflow"]
    assert config.resolve_agent("planner").tools.tool_names.count("workflow") == 1
    _assert_single_alias_warning(warnings, expect_allowed_tools=expect_allowed_tools)


@pytest.mark.parametrize(
    ("default_tools", "agent_tools", "expected_defer", "expect_allowed_tools"),
    [
        pytest.param(None, ["dynamic_workflow", _WF_2_DEFER], True, False, id="same-bare-then-configured"),
        pytest.param(None, [_WF_2_DEFER, "dynamic_workflow"], True, False, id="same-configured-then-bare"),
        pytest.param(None, [_DW_ALLOWED, _WF_2_DEFER], True, True, id="same-allowed-then-configured"),
        pytest.param(None, [_WF_2_DEFER, _DW_ALLOWED], True, True, id="same-configured-then-allowed"),
        pytest.param(["dynamic_workflow"], [_WF_2_DEFER], True, False, id="defaults-bare-agent-configured"),
        pytest.param([_WF_2], ["dynamic_workflow"], False, False, id="defaults-configured-agent-bare"),
        pytest.param([_WF_2], [_DW_ALLOWED], False, True, id="defaults-configured-agent-allowed"),
        pytest.param(["workflow"], ["dynamic_workflow", _WF_2], False, False, id="defaults-wf-agent-merged-dw-cfg"),
        pytest.param([_WF_2], ["workflow", "dynamic_workflow"], False, False, id="defaults-cfg-wf-agent-merged-wf-dw"),
    ],
)
def test_dual_tool_plain_dynamic_workflow_merges_with_configured_workflow(
    tmp_path: Path,
    default_tools: list[object] | None,
    agent_tools: list[object],
    expected_defer: bool,
    expect_allowed_tools: bool,
) -> None:
    """A plain (or allowed_tools-only) `dynamic_workflow` merges cleanly with `workflow` within and across layers."""
    config, warnings = _load_with_warnings(tmp_path, _cfg(default_tools=default_tools, agent_tools=agent_tools))
    candidates = [
        entry
        for entry in resolve_agent_tools(config, "planner", builtin_tool_registry()).candidates()
        if entry.name == "workflow"
    ]
    assert len(candidates) == 1
    assert candidates[0].tool_config_overrides == {"max_concurrent_runs": 2}
    assert candidates[0].defer is expected_defer
    _assert_single_alias_warning(warnings, expect_allowed_tools=expect_allowed_tools)


@pytest.mark.parametrize(
    ("default_tools", "agent_tools"),
    [
        pytest.param(None, [{"dynamic_workflow": {"defer": True}}, "workflow"], id="same-defer-conflict"),
        pytest.param(None, [{"dynamic_workflow": {"defer": False}}, {"workflow": {"defer": True}}], id="defer-false"),
        pytest.param(None, [{"dynamic_workflow": {"max_concurrent_runs": 1}}, _WF_2], id="same-override-conflict"),
        pytest.param([{"dynamic_workflow": {"max_concurrent_runs": 1}}], [_WF_2], id="cross-override-conflict"),
    ],
)
def test_dual_tool_conflicting_dynamic_workflow_settings_still_rejected(
    tmp_path: Path,
    default_tools: list[object] | None,
    agent_tools: list[object],
) -> None:
    """Conflicting settings or lazy flags between `dynamic_workflow` and `workflow` are still rejected."""
    with pytest.raises((ValidationError, ConfigRuntimeValidationError), match="both select workflow"):
        Config.validate_with_runtime(_cfg(default_tools=default_tools, agent_tools=agent_tools), _paths(tmp_path))


def test_workflow_direct_allowed_tools_still_rejected(tmp_path: Path) -> None:
    """`allowed_tools` on `workflow` (not the legacy alias) is rejected as an unknown override."""
    with pytest.raises(ConfigRuntimeValidationError, match="allowed_tools"):
        Config.validate_with_runtime(_cfg(agent_tools=[{"workflow": {"allowed_tools": ["*"]}}]), _paths(tmp_path))


def test_dashboard_stored_dynamic_workflow_allowed_tools_does_not_block_workflow(tmp_path: Path) -> None:
    """Dashboard-saved `allowed_tools` under service `dynamic_workflow` is ignored; other stored keys still fail."""
    paths = _paths(tmp_path)
    config = Config.validate_with_runtime(_cfg(agent_tools=["workflow"]), paths)
    manager = get_runtime_credentials_manager(paths)
    manager.save_credentials("dynamic_workflow", {"_source": "ui", "allowed_tools": ["*"]})

    toolkit = get_tool_by_name(
        "workflow",
        paths,
        runtime_config=config,
        tools=builtin_tool_registry(),
        worker_target=None,
    )
    assert isinstance(toolkit, WorkflowTools)

    manager.save_credentials("dynamic_workflow", {"_source": "ui", "allowed_tools": ["*"], "unexpected": True})
    with pytest.raises(ToolConfigOverrideError, match="settings stored under 'dynamic_workflow' no longer apply"):
        get_tool_by_name("workflow", paths, runtime_config=config, tools=builtin_tool_registry(), worker_target=None)


@pytest.mark.parametrize(
    ("default_tools", "template_tools", "expect_allowed_tools"),
    [
        pytest.param(["dynamic_workflow"], None, False, id="defaults-with-unowned-template"),
        pytest.param([], [_DW_ALLOWED], True, id="unowned-template-only"),
    ],
)
def test_validate_with_runtime_logs_deprecation_warning_once_with_unowned_personal_agent_template(
    tmp_path: Path,
    default_tools: list[object],
    template_tools: list[object] | None,
    expect_allowed_tools: bool,
) -> None:
    """`Config.validate_with_runtime` logs the deprecation warning once when probing unowned `personal_agents`."""
    template: dict[str, object] = {"display_name": "{user_display_name}'s Helper"}
    if template_tools is not None:
        template["tools"] = template_tools
    _, warnings = _load_with_warnings(
        tmp_path,
        _cfg(default_tools=default_tools, personal_agents={"helper": {"agent": template}}),
    )
    _assert_single_alias_warning(warnings, expect_allowed_tools=expect_allowed_tools)


def test_validate_with_runtime_preserves_deprecation_warning_across_pass_two_edits(tmp_path: Path) -> None:
    """Owner edits triggering pass 2 `generated_data` validation keep the pass 1 deprecation warning."""
    roster = PersonalAgentRoster(
        entries=[
            PersonalAgentRosterEntry(
                template="helper",
                user_id="@alice:localhost",
                username="alice",
                agent_name="helper_alice",
                matrix_username=None,
                user_display_name="Alice",
                source_room_id="!lobby:localhost",
                edits=GeneratedAgentEdits(display_name="Alice's Custom Helper"),
            ),
        ],
    )
    data = _cfg(
        default_tools=[],
        personal_agents={
            "helper": {
                "agent": {
                    "display_name": "{user_display_name}'s Helper",
                    "tools": [{"dynamic_workflow": {"allowed_tools": ["calculator"]}}],
                },
            },
        },
    )
    config, warnings = _load_with_warnings(tmp_path, data, roster=roster)
    assert config.agents["helper_alice"].display_name == "Alice's Custom Helper"
    assert config.agents["helper_alice"].tool_names == ["workflow"]
    _assert_single_alias_warning(warnings, expect_allowed_tools=True)


@pytest.mark.parametrize(
    ("agent_tools", "approval", "expected_match"),
    [
        pytest.param(
            ["dynamic_workflow"],
            {"rules": [{"match": "run_workflow", "action": "require_approval"}]},
            "workflow",
            id="run-workflow-alias",
        ),
        pytest.param(
            ["workflow", "dynamic_workflow"],
            {"rules": [{"match": "run_workflow", "action": "require_approval"}]},
            "workflow",
            id="run-workflow-merged-alias",
        ),
        pytest.param(
            ["dynamic_workflow"],
            {"rules": [{"match": "create_workflow", "action": "require_approval"}]},
            "workflow",
            id="create-workflow",
        ),
        pytest.param(
            ["dynamic_workflow"],
            {"rules": [{"match": "list_workflows", "action": "require_approval"}]},
            "workflow",
            id="list-workflows",
        ),
        pytest.param(
            ["dynamic_workflow"],
            {"default": "require_approval", "rules": [{"match": "work*", "action": "auto_approve"}]},
            "workflow",
            id="broad-glob-weakening-default",
        ),
    ],
)
def test_retired_dynamic_workflow_approval_rules_fail(
    tmp_path: Path,
    agent_tools: list[object],
    approval: dict[str, object],
    expected_match: str,
) -> None:
    """Configs authoring `dynamic_workflow` reject approval rules that stop deciding its retired functions."""
    with pytest.raises(ConfigRuntimeValidationError, match=expected_match):
        Config.validate_with_runtime(_cfg(agent_tools=agent_tools, tool_approval=approval), _paths(tmp_path))


@pytest.mark.parametrize(
    ("agent_tools", "rules"),
    [
        pytest.param(
            ["dynamic_workflow"],
            [
                {"match": "run_workflow", "action": "require_approval"},
                {"match": "workflow_read", "action": "require_approval"},
                {"match": "workflow_write", "action": "require_approval"},
            ],
            id="explicit-exact-replacements",
        ),
        pytest.param(["dynamic_workflow"], [{"match": "*workflow*", "action": "require_approval"}], id="wildcard"),
        pytest.param(["calculator"], [{"match": "run_workflow", "action": "require_approval"}], id="without-workflow"),
    ],
)
def test_retired_dynamic_workflow_approval_rules_allowed(
    tmp_path: Path,
    agent_tools: list[object],
    rules: list[dict[str, object]],
) -> None:
    """Exact replacement rules, matching wildcards, and configs without `dynamic_workflow` load cleanly."""
    config = Config.validate_with_runtime(
        _cfg(agent_tools=agent_tools, tool_approval={"rules": rules}),
        _paths(tmp_path),
    )
    assert len(config.tool_approval.rules) == len(rules)


@pytest.mark.parametrize(
    ("agent_tools", "matches", "error_pattern"),
    [
        (["dynamic_workflow"], ["*_workflow*"], r"check\.py.*run_workflow.*workflow_write"),
        (["dynamic_workflow"], ["*workflow*"], r"check\.py.*run_workflow.*workflow_write"),
        (["dynamic_workflow"], ["run_*"], r"check\.py.*run_workflow.*workflow_write"),
        (["dynamic_workflow"], ["*workflow*", "workflow_write"], r"check\.py.*run_workflow.*workflow_write"),
        (["dynamic_workflow"], ["skill", "*"], r"check\.py.*run_workflow.*workflow_write"),
        (["dynamic_workflow"], ["workflow_read", "workflow_write", "*_workflow*"], None),
        (["dynamic_workflow"], ["workflow_read", "workflow_write", "*workflow*"], None),
        (["dynamic_workflow"], ["workflow_read", "workflow_write", "skill", "*"], None),
        (["calculator"], ["*_workflow*"], None),
    ],
)
def test_wildcard_approval_script_rule_fails_closed_for_retired_dynamic_workflow_names(
    tmp_path: Path,
    agent_tools: list[object],
    matches: list[str],
    error_pattern: str | None,
) -> None:
    """Wildcard approval scripts matching retired `dynamic_workflow` names fail closed unless `workflow` precedes them."""
    (tmp_path / "check.py").write_text("def check(tool_name, arguments, agent_name): return True\n", encoding="utf-8")
    approval = {"rules": [{"match": match, "script": "check.py"} for match in matches]}
    if error_pattern is not None:
        with pytest.raises(ConfigRuntimeValidationError, match=error_pattern):
            Config.validate_with_runtime(_cfg(agent_tools=agent_tools, tool_approval=approval), _paths(tmp_path))
        return
    config = Config.validate_with_runtime(_cfg(agent_tools=agent_tools, tool_approval=approval), _paths(tmp_path))
    assert [rule.match for rule in config.tool_approval.rules] == matches


@pytest.mark.parametrize("pattern", ["run_*", "create_*"])
@pytest.mark.parametrize(
    ("default_action", "rule_action"),
    [("auto_approve", "require_approval"), ("require_approval", "auto_approve")],
)
def test_canonical_workflow_wildcard_approval_rules_load_cleanly(
    tmp_path: Path,
    pattern: str,
    default_action: str,
    rule_action: str,
) -> None:
    """Canonical-only `workflow` configs are not falsely blocked by ordinary wildcard approval rules."""
    approval = {"default": default_action, "rules": [{"match": pattern, "action": rule_action}]}
    config = Config.validate_with_runtime(_cfg(agent_tools=["workflow"], tool_approval=approval), _paths(tmp_path))
    assert config.tool_approval.rules[0].match == pattern


@pytest.mark.parametrize("tool_name", ["workflow", "dynamic_workflow"])
def test_contacts_tools_rejects_workflow_and_dynamic_workflow(tool_name: str) -> None:
    """Contact turns forbid both `workflow` and its deprecated `dynamic_workflow` alias."""
    with pytest.raises(ValidationError, match=r"contacts\.tools may not include workflow"):
        ContactsConfig(tools=[tool_name])


def test_legacy_dynamic_workflow_usage_row_is_preserved(tmp_path: Path) -> None:
    """Historical SQLite usage rows with kind='dynamic_workflow' keep counting in usage reports and monthly budgets."""
    assert "dynamic_workflow" not in get_args(IndependentUsageKind.__value__)
    assert "dynamic_workflow" in _LEGACY_USAGE_KINDS
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
    config = Config.model_validate(_cfg())
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

    assert collect_admin_usage(config=config, runtime_paths=paths, include_requests=True).totals.total_tokens == 172
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
