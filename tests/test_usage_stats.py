"""Read-only retained usage aggregation."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType

import pytest

from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from mindroom.usage_stats import (
    UsageStatsValidationError,
    collect_admin_usage,
    collect_self_usage,
    parse_usage_window,
)
from mindroom.usage_stats_storage import (
    UsageRunNode,
    UsageSessionRow,
    UsageStorageDiagnostic,
    UsageStorageSource,
)


def _config(*, timezone: str = "UTC") -> Config:
    return Config(
        agents={"code": AgentConfig(display_name="Code"), "other": AgentConfig(display_name="Other")},
        teams={"engineering": TeamConfig(display_name="Engineering", role="Team", agents=["code"])},
        timezone=timezone,
        authorization=AuthorizationConfig(
            aliases={"@alice:example.test": ["@telegram-alice:example.test"]},
        ),
    )


def _paths(tmp_path: Path) -> RuntimePaths:
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )


def _identity() -> ToolExecutionIdentity:
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id="@alice:example.test",
        room_id="!room:example.test",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session",
    )


def _source(
    *,
    scope: str = "shared_agent",
    agent_name: str | None = "code",
    requester_isolated: bool = False,
) -> UsageStorageSource:
    return UsageStorageSource(
        path=Path("/not-read.db"),
        path_label=f"{scope}/{agent_name or 'team'}.db",
        scope=scope,  # type: ignore[arg-type]
        expected_session_table="sessions",
        source_agent_id=agent_name,
        allowed_agent_ids=frozenset({"code", "other"}),
        allowed_team_ids=frozenset({"engineering"}),
        requester_isolated=requester_isolated,
    )


def _run(
    *,
    requester_id: str | None = "@alice:example.test",
    team_id: str | None = None,
    run_id: str | None = "run-1",
    created_at: str | None = "2026-01-02T12:00:00Z",
    total_tokens: int = 10,
    model: str = "gpt-5.6",
) -> UsageRunNode:
    return UsageRunNode(
        team_id=team_id,
        requester_id=requester_id,
        created_at=created_at,
        model_provider="openai",
        model_id=model,
        run_id=run_id,
        status="completed",
        metrics=MappingProxyType(
            {"input_tokens": total_tokens - 3, "output_tokens": 3, "total_tokens": total_tokens},
        ),
    )


def _row(
    source: UsageStorageSource,
    *runs: UsageRunNode,
    entity_id: str | None = None,
    row_key: str = "session-1",
) -> UsageSessionRow:
    is_team = source.scope == "team"
    return UsageSessionRow(
        source=source,
        entity_id=entity_id or ("engineering" if is_team else source.source_agent_id or "unknown"),
        entity_kind="team" if is_team else "agent",
        row_key=row_key,
        runs=tuple(runs),
    )


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    sources: tuple[UsageStorageSource | UsageStorageDiagnostic, ...],
    rows: dict[str, tuple[UsageSessionRow | UsageStorageDiagnostic, ...]],
) -> None:
    monkeypatch.setattr("mindroom.usage_stats.discover_self_usage_sources", lambda **_: sources)
    monkeypatch.setattr("mindroom.usage_stats.discover_admin_usage_sources", lambda **_: sources)
    monkeypatch.setattr(
        "mindroom.usage_stats.iter_usage_storage_rows",
        lambda source: iter(rows.get(source.path_label, ())),
    )


def test_self_report_has_small_retained_usage_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Billing and persistence internals must stay out of the public report."""
    source = _source()
    _wire(monkeypatch, (source,), {source.path_label: (_row(source, _run()),)})

    report = collect_self_usage(
        agent_name="code",
        requester_id="@alice:example.test",
        config=_config(),
        runtime_paths=_paths(tmp_path),
        execution_identity=_identity(),
        start=None,
        end=None,
        group_by="day",
        as_of=datetime(2026, 1, 3, tzinfo=UTC),
    )

    payload = report.to_dict()
    expected_totals = {
        "input_tokens": 7,
        "output_tokens": 3,
        "total_tokens": 10,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
        "audio_input_tokens": 0,
        "audio_output_tokens": 0,
        "audio_total_tokens": 0,
    }
    assert payload["scope"] == "self"
    assert payload["totals"] == expected_totals
    assert payload["run_count"] == 1
    assert payload["session_count"] == 1
    assert payload["breakdown"] == [
        {
            "dimension": "day",
            "key": "2026-01-02",
            "totals": expected_totals,
            "run_count": 1,
        },
    ]
    assert payload["coverage"] == {
        "scanned_sources": 1,
        "unavailable_sources": 0,
        "skipped_runs": 0,
        "note": (
            "Retained top-level Agno runs only; nested team members are not attributed, "
            "and compacted history is unavailable."
        ),
    }
    assert "cost" not in payload
    assert "turn_count" not in payload


def test_self_filters_shared_storage_by_canonical_requester(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shared agent must not expose another requester's retained run."""
    source = _source()
    rows = (
        _row(
            source,
            _run(requester_id="@telegram-alice:example.test", run_id="own"),
            _run(requester_id="@bob:example.test", run_id="other", total_tokens=50),
        ),
    )
    _wire(monkeypatch, (source,), {source.path_label: rows})

    report = collect_self_usage(
        agent_name="code",
        requester_id="@alice:example.test",
        config=_config(),
        runtime_paths=_paths(tmp_path),
        execution_identity=_identity(),
        start=None,
        end=None,
        group_by="model",
        as_of=datetime(2026, 1, 3, tzinfo=UTC),
    )

    assert report.run_count == 1
    assert report.totals.total_tokens == 10
    assert report.coverage.skipped_runs == 0


def test_self_private_storage_uses_physical_requester_isolation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A private-agent database remains usable when an old run lacks requester metadata."""
    source = _source(scope="private_agent", requester_isolated=True)
    _wire(monkeypatch, (source,), {source.path_label: (_row(source, _run(requester_id=None)),)})

    report = collect_self_usage(
        agent_name="code",
        requester_id="@alice:example.test",
        config=_config(),
        runtime_paths=_paths(tmp_path),
        execution_identity=_identity(),
        start=None,
        end=None,
        group_by="model",
        as_of=datetime(2026, 1, 3, tzinfo=UTC),
    )

    assert report.run_count == 1
    assert report.totals.total_tokens == 10


def test_admin_groups_top_level_agent_and_team_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Admin totals cover configured entities without attributing nested team members."""
    agent_source = _source()
    team_source = _source(scope="team", agent_name=None)
    _wire(
        monkeypatch,
        (agent_source, team_source),
        {
            agent_source.path_label: (_row(agent_source, _run(run_id="agent")),),
            team_source.path_label: (_row(team_source, _run(team_id="engineering", run_id="team", total_tokens=20)),),
        },
    )

    report = collect_admin_usage(
        config=_config(),
        runtime_paths=_paths(tmp_path),
        start=None,
        end=None,
        group_by="entity",
        entity_names=None,
        requester_ids=None,
        as_of=datetime(2026, 1, 3, tzinfo=UTC),
    )

    assert report.totals.total_tokens == 30
    assert {(row.key, row.totals.total_tokens) for row in report.breakdown} == {
        ("code", 10),
        ("engineering", 20),
    }


def test_admin_validates_entity_filter_before_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown entity names cannot turn into storage paths."""
    monkeypatch.setattr(
        "mindroom.usage_stats.discover_admin_usage_sources",
        lambda **_: pytest.fail("discovery must not run"),
    )

    with pytest.raises(UsageStatsValidationError, match="Unknown usage entities"):
        collect_admin_usage(
            config=_config(),
            runtime_paths=_paths(tmp_path),
            start=None,
            end=None,
            group_by="entity",
            entity_names=("missing",),
            requester_ids=None,
        )


def test_unavailable_sources_are_reported_without_exposing_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed read changes only the aggregate coverage counters."""
    diagnostic = UsageStorageDiagnostic(path_label="secret/path.db", status="busy", detail="database busy")
    _wire(monkeypatch, (diagnostic,), {})

    report = collect_admin_usage(
        config=_config(),
        runtime_paths=_paths(tmp_path),
        start=None,
        end=None,
        group_by="entity",
        entity_names=None,
        requester_ids=None,
        as_of=datetime(2026, 1, 3, tzinfo=UTC),
    )

    assert report.coverage.unavailable_sources == 1
    assert "secret" not in str(report.to_dict())


def test_usage_window_uses_configured_timezone_and_exclusive_end() -> None:
    """Date boundaries use local midnight and keep the end exclusive."""
    start, end = parse_usage_window(
        start="2026-01-02",
        end="2026-01-03",
        timezone_name="America/Los_Angeles",
        as_of=datetime(2026, 1, 4, tzinfo=UTC),
    )

    assert start == datetime(2026, 1, 2, 8, tzinfo=UTC)
    assert end == datetime(2026, 1, 3, 8, tzinfo=UTC)
