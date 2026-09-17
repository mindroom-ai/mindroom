"""Private-agent accounting across users, storage layouts, and tool scopes."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from agno.metrics import RunMetrics
from agno.run.agent import RunOutput
from agno.session.agent import AgentSession

from mindroom import usage_stats
from mindroom.agent_storage import create_state_storage
from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.custom_tools.usage_stats import UsageStatsTools
from mindroom.legacy_private_storage_aliases import historical_private_instance_worker_key
from mindroom.private_instance_identity_store import ensure_private_instance_identity, load_private_instance_identity
from mindroom.runtime_resolution import resolve_agent_storage
from mindroom.tool_system.worker_routing import build_tool_execution_identity, private_instance_scope_root_path
from tests.conftest import seed_session
from tests.test_usage_stats import _config, _metrics, _model_metrics, _paths, _row, _run, _source, _wire
from tests.test_usage_stats_tool import _context

if TYPE_CHECKING:
    from collections.abc import Iterator

ALICE = "@alice:example.test"
BOB = "@bob:example.test"
ALIAS = "@telegram-alice:example.test"


@dataclass
class PrivateUsageData:
    """Real stores for two private scopes and an unrelated shared agent."""

    config: Config
    paths: RuntimePaths


def private_usage_data(tmp_path: Path, *, separate_sessions: bool = True) -> PrivateUsageData:
    """Persist known totals, including history no longer present in run detail."""
    paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"MINDROOM_SESSION_STORAGE_PATH": str(tmp_path / "sessions")} if separate_sessions else {},
    )
    config = Config(
        agents={
            "code": AgentConfig(display_name="Code", private=AgentPrivateConfig(per="user")),
            "helper": AgentConfig(display_name="Helper", private=AgentPrivateConfig(per="user_agent")),
            "shared": AgentConfig(display_name="Shared"),
        },
        administrators=[ALICE],
        authorization=AuthorizationConfig(aliases={ALICE: [ALIAS]}),
    )
    data = PrivateUsageData(config, paths)
    seed_private_usage(data, "code", ALICE, session_tokens=100, run_tokens=20, stored_requester=None)
    seed_private_usage(data, "helper", ALICE, session_tokens=50, run_tokens=10, stored_requester=ALIAS)
    seed_private_usage(data, "code", BOB, session_tokens=70, run_tokens=30, stored_requester=BOB)
    seed_private_usage(data, "shared", ALICE, session_tokens=500, run_tokens=400, stored_requester=ALICE)
    return data


def seed_private_usage(
    data: PrivateUsageData,
    agent: str,
    requester: str,
    *,
    session_tokens: int,
    run_tokens: int,
    stored_requester: str | None,
    owner_record: bool = True,
) -> Path:
    """Seed real Agno storage without storing conversation content."""
    identity = build_tool_execution_identity(
        channel="matrix",
        agent_name=agent,
        runtime_paths=data.paths,
        requester_id=requester,
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    resolved = resolve_agent_storage(agent, data.config, data.paths, identity)
    if resolved.execution.is_private and owner_record:
        assert resolved.execution.worker_key is not None
        ensure_private_instance_identity(
            data.paths.storage_root,
            worker_key=resolved.execution.worker_key,
            requester_id=requester,
        )
    storage = create_state_storage(
        agent,
        resolved.session_state_root,
        subdir="sessions",
        session_table=f"{agent}_sessions",
    )
    try:
        seed_session(
            storage,
            AgentSession(
                session_id="same-session-id",
                agent_id=agent,
                user_id=stored_requester,
                session_data={"session_metrics": {"total_tokens": session_tokens}},
                runs=[
                    RunOutput(
                        run_id="same-run-id",
                        user_id=stored_requester,
                        model_provider="test-provider",
                        model="test-model",
                        created_at=1_700_000_000 if agent == "code" else 1_700_086_400,
                        metrics=RunMetrics(
                            input_tokens=run_tokens - 3,
                            output_tokens=3,
                            total_tokens=run_tokens,
                            cache_read_tokens=2,
                            cache_write_tokens=1,
                        ),
                    ),
                ],
            ),
        )
    finally:
        storage.close()
    return resolved.session_state_root / "sessions" / f"{agent}.db"


@pytest.mark.parametrize("separate_sessions", [False, True])
@pytest.mark.parametrize("include_daily", [False, True])
def test_admin_private_usage_splits_users_and_preserves_compacted_totals(
    tmp_path: Path,
    separate_sessions: bool,
    include_daily: bool,
) -> None:
    """Two owners of one agent remain separate even when run IDs are identical."""
    data = private_usage_data(tmp_path, separate_sessions=separate_sessions)
    report = usage_stats.collect_admin_usage(
        config=data.config,
        runtime_paths=data.paths,
        include_daily=include_daily,
    ).to_dict()
    rows = {(row["user_id"], row["agent_name"]): row for row in report["private_agent_breakdown"]}
    assert set(rows) == {(ALICE, "code"), (ALICE, "helper"), (BOB, "code")}
    assert rows[ALICE, "code"]["totals"]["total_tokens"] == 100
    assert rows[ALICE, "helper"]["totals"]["total_tokens"] == 50
    assert rows[BOB, "code"]["totals"]["total_tokens"] == 70
    alice_code = rows[ALICE, "code"]
    assert alice_code["session_count"] == 1
    assert alice_code["run_count"] == 1
    assert alice_code["retained_run_totals"]["total_tokens"] == 20
    assert alice_code["model_breakdown"][0]["totals"]["cache_read_tokens"] == 2
    assert alice_code["model_breakdown"][0]["totals"]["cache_write_tokens"] == 1
    if include_daily:
        assert alice_code["daily_breakdown"][0]["date"] == "2023-11-14"
        assert alice_code["daily_breakdown"][0]["totals"]["total_tokens"] == 20
    else:
        assert all("daily_breakdown" not in row for row in rows.values())
    assert report["totals"]["total_tokens"] == 720
    users = {row["user_id"]: row for row in report["user_breakdown"]}
    assert users[ALICE]["totals"]["total_tokens"] == 430
    private_retained = sum(row["retained_run_totals"]["total_tokens"] for row in rows.values())
    private_sessions = sum(row["totals"]["total_tokens"] for row in rows.values())
    retained_users = sum(row["totals"]["total_tokens"] for row in users.values())
    assert retained_users - private_retained + private_sessions == 620


@pytest.mark.parametrize("include_daily", [False, True])
def test_personal_usage_reads_only_owned_private_agents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    include_daily: bool,
) -> None:
    """Personal collection never scans other owners or shared-agent databases."""
    data = private_usage_data(tmp_path)
    original_iterdir = Path.iterdir

    def deny_owner_enumeration(path: Path) -> Iterator[Path]:
        if path.name == "private_instances":
            pytest.fail("Personal usage must resolve exact requester paths")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", deny_owner_enumeration)
    report = usage_stats.collect_private_usage(
        requester_id=ALIAS,
        config=data.config,
        runtime_paths=data.paths,
        include_daily=include_daily,
    ).to_dict()
    assert report["scope"] == "self"
    assert report["totals"]["total_tokens"] == 150
    assert report["session_count"] == 2
    assert "user_breakdown" not in report
    rows = {row["agent_name"]: row for row in report["private_agent_breakdown"]}
    assert set(rows) == {"code", "helper"}
    assert rows["code"]["totals"]["total_tokens"] == 100
    assert rows["helper"]["retained_run_totals"]["total_tokens"] == 10
    assert all("user_id" not in row for row in rows.values())
    assert BOB not in json.dumps(report)
    assert report["model_breakdown"][0]["totals"]["total_tokens"] == 30
    assert ("daily_breakdown" in report) is include_daily


def test_private_usage_keeps_unattributed_history_without_guessing_owner(tmp_path: Path) -> None:
    """Legacy stores without an owner or requester retain explicitly unknown totals."""
    data = private_usage_data(tmp_path)
    seed_private_usage(
        data,
        "code",
        "@unknown:example.test",
        session_tokens=25,
        run_tokens=5,
        stored_requester=None,
        owner_record=False,
    )
    report = usage_stats.collect_admin_usage(config=data.config, runtime_paths=data.paths).to_dict()
    row = next(row for row in report["private_agent_breakdown"] if row["user_id"] is None)
    assert row["agent_name"] == "code"
    assert row["totals"]["total_tokens"] == 25
    assert row["retained_run_totals"]["total_tokens"] == 5


def test_private_usage_retains_legacy_alias_stores(tmp_path: Path) -> None:
    """Known requester aliases can retain their own older private database."""
    data = private_usage_data(tmp_path)
    seed_private_usage(
        data,
        "code",
        ALIAS,
        session_tokens=11,
        run_tokens=4,
        stored_requester=ALIAS,
        owner_record=False,
    )
    report = usage_stats.collect_private_usage(
        requester_id=ALICE,
        config=data.config,
        runtime_paths=data.paths,
    ).to_dict()
    assert report["totals"]["total_tokens"] == 161
    row = next(row for row in report["private_agent_breakdown"] if row["agent_name"] == "code")
    assert row["totals"]["total_tokens"] == 111
    assert row["retained_run_totals"]["total_tokens"] == 24


@pytest.mark.asyncio
async def test_private_usage_tool_uses_requester_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A shared reporting agent can report only its requester's private agents."""
    data = private_usage_data(tmp_path)
    context = replace(_context(tmp_path), agent_name="shared", config=data.config, runtime_paths=data.paths)
    monkeypatch.setattr("mindroom.custom_tools.usage_stats.get_tool_runtime_context", lambda: context)
    tools = UsageStatsTools(agent_name="shared")
    report = json.loads(await tools.get_my_private_usage(include_daily=True))
    assert report["status"] == "ok"
    assert report["totals"]["total_tokens"] == 150
    assert {row["agent_name"] for row in report["private_agent_breakdown"]} == {"code", "helper"}
    assert "user_breakdown" not in report
    assert BOB not in json.dumps(report)
    assert report["daily_breakdown"][0]["totals"]["total_tokens"] == 20


def test_private_rows_share_report_wide_run_deduplication(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Conflicting requester metadata on a duplicate run cannot double count private detail."""
    source = _source(scope="private_agent", requester_isolated=True)
    row = replace(
        _row(source, _run(requester_id=ALICE), _run(requester_id=BOB), session_metrics=_metrics(100)),
        requester_id=ALICE,
    )
    _wire(monkeypatch, (source,), {source.path_label: (row,)})
    report = usage_stats.collect_admin_usage(config=_config(), runtime_paths=_paths(tmp_path)).to_dict()
    rows = report["private_agent_breakdown"]
    assert sum(row["retained_run_totals"]["total_tokens"] for row in rows) == 10
    assert {row["user_id"] for row in rows} == {ALICE}


def test_combined_private_ownership_differs_from_recorded_requester(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Combining private session totals changes attribution only for private usage."""
    private_source = replace(_source(scope="private_agent", requester_isolated=True), owner_id=ALICE)
    shared_source = _source()
    shared_only_user = "@carol:example.test"
    private_row = _row(
        private_source,
        _run(requester_id=BOB, total_tokens=20),
        session_metrics=_metrics(100),
        session_model_metrics=(_model_metrics("cumulative-provider", "cumulative-model", **dict(_metrics(100))),),
    )
    shared_row = _row(
        shared_source,
        _run(requester_id=BOB, total_tokens=30),
        _run(requester_id=shared_only_user, run_id="shared-only", total_tokens=40),
        session_metrics=_metrics(80),
    )
    _wire(
        monkeypatch,
        (private_source, shared_source),
        {private_source.path_label: (private_row,), shared_source.path_label: (shared_row,)},
    )

    report = usage_stats.collect_admin_usage(config=_config(), runtime_paths=_paths(tmp_path)).to_dict()

    users = {row["user_id"]: row["totals"]["total_tokens"] for row in report["user_breakdown"]}
    assert users == {BOB: 50, shared_only_user: 40}
    entity = report["breakdown"][0]
    assert (entity["key"], entity["run_count"], entity["retained_run_totals"]["total_tokens"]) == ("code", 3, 90)
    assert {row["user_id"]: row["totals"]["total_tokens"] for row in entity["user_breakdown"]} == {
        BOB: 50,
        shared_only_user: 40,
    }
    private = {row["user_id"]: row for row in report["private_agent_breakdown"]}
    assert private[ALICE]["totals"]["total_tokens"] == 100
    assert private[ALICE]["cumulative_model_breakdown"][0]["provider"] == "cumulative-provider"
    assert private[ALICE]["cumulative_model_breakdown"][0]["totals"]["total_tokens"] == 100
    assert private[ALICE]["cumulative_model_breakdown"][0]["session_count"] == 1
    assert private[ALICE]["retained_run_totals"]["total_tokens"] == 0
    assert private[BOB]["totals"]["total_tokens"] == 0
    assert private[BOB]["cumulative_model_breakdown"] == []
    assert private[BOB]["retained_run_totals"]["total_tokens"] == 20
    private_retained = {user: row["retained_run_totals"]["total_tokens"] for user, row in private.items()}
    private_sessions = {user: row["totals"]["total_tokens"] for user, row in private.items()}
    combined = {
        user: users.get(user, 0) - private_retained.get(user, 0) + private_sessions.get(user, 0)
        for user in users.keys() | private.keys()
    }
    assert combined == {ALICE: 100, BOB: 30, shared_only_user: 40}


def test_unknown_summary_preserves_private_ownership_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown requester detail can still be replaced with known private ownership."""
    source = replace(_source(scope="private_agent", requester_isolated=True), owner_id=ALICE)
    summary = replace(_run(requester_id=None, run_id="summary", total_tokens=110), kind="compaction_summary")
    row = _row(source, _run(requester_id=ALICE), summary, session_metrics=_metrics(10))
    _wire(monkeypatch, (source,), {source.path_label: (row,)})
    report = usage_stats.collect_admin_usage(config=_config(), runtime_paths=_paths(tmp_path))
    users = {row.user_id: row.totals.total_tokens for row in report.user_breakdown}
    assert users == {ALICE: 10, None: 110}
    owned = {row.user_id: row for row in report.private_agent_breakdown}
    combined = {
        user: users.get(user, 0) - detail.retained_run_totals.total_tokens + detail.totals.total_tokens
        for user, detail in owned.items()
    }
    assert combined == {ALICE: 120, None: 0}


def test_private_self_exports_only_owned_cumulative_models(tmp_path: Path) -> None:
    """All-private self reports expose no shared or other-owner cumulative totals."""
    data = private_usage_data(tmp_path)
    report = usage_stats.collect_private_usage(
        requester_id=ALICE,
        config=data.config,
        runtime_paths=data.paths,
    ).to_dict()

    assert report["cumulative_model_breakdown"][0]["provider"] == "unknown"
    assert report["cumulative_model_breakdown"][0]["totals"]["total_tokens"] == 150
    assert report["cumulative_model_breakdown"][0]["session_count"] == 2
    assert report["cumulative_model_coverage"]["unavailable_sources"] == 2

    shared = usage_stats.collect_self_usage(
        agent_name="shared",
        requester_id=ALICE,
        config=data.config,
        runtime_paths=data.paths,
        execution_identity=build_tool_execution_identity(
            channel="matrix",
            agent_name="shared",
            runtime_paths=data.paths,
            requester_id=ALICE,
            room_id=None,
            thread_id=None,
            resolved_thread_id=None,
            session_id=None,
        ),
    ).to_dict()
    assert "cumulative_model_breakdown" not in shared
    assert "cumulative_model_coverage" not in shared


def test_private_rows_report_unreadable_run_detail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unavailable retained runs must not make private coverage look complete."""
    source = _source(scope="private_agent", requester_isolated=True)
    row = replace(_row(source, session_metrics=_metrics(100), runs_available=False), requester_id=ALICE)
    _wire(monkeypatch, (source,), {source.path_label: (row,)})
    report = usage_stats.collect_admin_usage(config=_config(), runtime_paths=_paths(tmp_path)).to_dict()
    assert report["private_agent_breakdown"][0]["totals"]["total_tokens"] == 100
    assert report["private_agent_coverage"]["unavailable_sources"] == 1


def test_private_coverage_reports_unreadable_namespace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Failed private discovery must not look like an empty, complete report."""
    data = private_usage_data(tmp_path)
    private_root = tmp_path / "sessions" / "private_instances"
    original_iterdir = Path.iterdir

    def fail_private_directory(path: Path) -> Iterator[Path]:
        if path == private_root:
            raise OSError
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", fail_private_directory)
    report = usage_stats.collect_admin_usage(config=data.config, runtime_paths=data.paths).to_dict()
    assert report["totals"]["total_tokens"] == 500
    assert report["coverage"]["unavailable_sources"] == 1
    assert report["private_agent_breakdown"] == []
    assert report["private_agent_coverage"]["unavailable_sources"] == 1


def _publish_legacy_usage_alias(data: PrivateUsageData) -> tuple[Path, Path]:
    """Publish the exact owner-bound primary alias and optional session mirror."""
    database = seed_private_usage(
        data,
        "code",
        ALICE,
        session_tokens=100,
        run_tokens=20,
        stored_requester=None,
    )
    session_directory = database.parents[2]
    primary_directory = data.paths.storage_root / "private_instances" / session_directory.name
    owner = load_private_instance_identity(data.paths.storage_root, primary_directory)
    assert owner is not None
    historical_key = historical_private_instance_worker_key(owner.worker_key, owner.requester_id)
    primary_alias = private_instance_scope_root_path(data.paths.storage_root, historical_key)
    primary_alias.symlink_to(primary_directory.name, target_is_directory=True)
    session_alias = session_directory.parent / primary_alias.name
    if session_alias != primary_alias:
        session_alias.symlink_to(session_directory.name, target_is_directory=True)
    return primary_alias, session_alias


@pytest.mark.parametrize("separate_sessions", [False, True])
def test_private_coverage_does_not_count_verified_alias_as_missing(tmp_path: Path, separate_sessions: bool) -> None:
    """Historical aliases must neither duplicate totals nor report unreadable storage."""
    data = private_usage_data(tmp_path, separate_sessions=separate_sessions)
    _publish_legacy_usage_alias(data)

    report = usage_stats.collect_admin_usage(config=data.config, runtime_paths=data.paths, include_daily=True).to_dict()

    assert report["totals"]["total_tokens"] == 720
    assert report["coverage"]["scanned_sources"] == 4
    assert report["private_agent_coverage"]["scanned_sources"] == 3
    for field in ("coverage", "model_coverage", "user_coverage", "daily_coverage", "private_agent_coverage"):
        assert report[field]["unavailable_sources"] == 0


@pytest.mark.parametrize("damage", ["unverified_name", "absolute_target", "missing_primary", "different_target"])
def test_private_coverage_preserves_warning_for_unverified_alias(tmp_path: Path, damage: str) -> None:
    """A readable symlink target alone must not suppress discovery warnings."""
    data = private_usage_data(tmp_path)
    primary_alias, session_alias = _publish_legacy_usage_alias(data)
    target = session_alias.resolve()
    if damage == "unverified_name":
        session_alias.rename(session_alias.with_name("unverified-0000000000000000"))
    elif damage == "absolute_target":
        session_alias.unlink()
        session_alias.symlink_to(target, target_is_directory=True)
    elif damage == "missing_primary":
        primary_alias.unlink()
    else:
        other = next(
            path for path in target.parent.iterdir() if path.is_dir() and not path.is_symlink() and path != target
        )
        session_alias.unlink()
        session_alias.symlink_to(other.name, target_is_directory=True)

    report = usage_stats.collect_admin_usage(config=data.config, runtime_paths=data.paths).to_dict()
    assert report["totals"]["total_tokens"] == 720
    assert report["coverage"]["unavailable_sources"] == 1
    assert report["private_agent_coverage"]["unavailable_sources"] == 1


@pytest.mark.parametrize("separate_sessions", [False, True])
@pytest.mark.parametrize("admin", [False, True])
@pytest.mark.parametrize("nested", [False, True])
def test_usage_isolates_rejected_private_paths(
    tmp_path: Path,
    separate_sessions: bool,
    admin: bool,
    nested: bool,
) -> None:
    """One unsafe worker path cannot hide usage from other owned private agents."""
    data = private_usage_data(tmp_path, separate_sessions=separate_sessions)
    database = seed_private_usage(
        data,
        "code",
        ALICE,
        session_tokens=100,
        run_tokens=20,
        stored_requester=None,
    )
    worker_root = (
        database.parents[2] if admin else data.paths.storage_root / "private_instances" / database.parents[2].name
    )
    rejected_path = database if nested else worker_root
    blocked_path = rejected_path.rename(tmp_path / "blocked-private-path")
    rejected_path.symlink_to(blocked_path, target_is_directory=not nested)

    report = (
        usage_stats.collect_admin_usage(config=data.config, runtime_paths=data.paths)
        if admin
        else usage_stats.collect_private_usage(requester_id=ALICE, config=data.config, runtime_paths=data.paths)
    ).to_dict()
    assert report["totals"]["total_tokens"] == (620 if admin else 50)
    assert [row["agent_name"] for row in report["private_agent_breakdown"]] == (
        ["helper", "code"] if admin else ["helper"]
    )
    assert report["coverage"]["unavailable_sources"] == 1
    assert report["private_agent_coverage"]["unavailable_sources"] == 1
