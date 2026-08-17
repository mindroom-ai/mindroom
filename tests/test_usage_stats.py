"""Direct retained-run usage aggregation behavior."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from itertools import count
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from mindroom.usage_stats import (
    UsageStatsSourceUnavailableError,
    UsageStatsValidationError,
    collect_admin_usage,
    collect_self_usage,
    parse_usage_window,
)
from mindroom.usage_stats_storage import (
    UsageSessionRow,
    UsageStorageDiagnostic,
    UsageStorageSource,
    _extract_run_node,
)

if TYPE_CHECKING:
    from collections.abc import Iterable


_ROW_KEYS = count()


def _config(*, timezone: str = "UTC", team_names: tuple[str, ...] = ()) -> Config:
    return Config(
        agents={"code": AgentConfig(display_name="Code"), "other": AgentConfig(display_name="Other")},
        teams={
            team_name: TeamConfig(display_name=team_name.title(), role="Test team", agents=["code"])
            for team_name in team_names
        },
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


def _identity(*, requester_id: str = "@alice:example.test") -> ToolExecutionIdentity:
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id=requester_id,
        room_id="!private:example.test",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="private-session",
    )


def _source(
    *,
    scope: str = "shared_agent",
    source_agent_id: str | None = "code",
    allowed_agents: frozenset[str] = frozenset({"code", "other"}),
    allowed_teams: frozenset[str] = frozenset(),
    requester_isolated: bool = False,
) -> UsageStorageSource:
    return UsageStorageSource(
        path=Path("/not-a-real-storage-path.db"),
        path_label="agents/code/sessions/code.db",
        scope=scope,  # type: ignore[arg-type]
        expected_session_table="code_sessions",
        source_agent_id=source_agent_id,
        allowed_agent_ids=allowed_agents,
        allowed_team_ids=allowed_teams,
        requester_isolated=requester_isolated,
    )


def _raw_run(
    *,
    requester_id: str | None = "@alice:example.test",
    user_id: str | None = "@alice:example.test",
    agent_id: str | None = "code",
    team_id: str | None = None,
    created_at: str | None = "2026-01-02T12:00:00Z",
    metrics: dict[str, object] | None = None,
    model: str = "gpt-5.6",
    provider: str = "openai",
    run_id: str | None = None,
    nested: list[dict[str, object]] | None = None,
    sensitive_text: str = "never expose this prompt",
) -> dict[str, object]:
    metadata = (
        {"requester_id": requester_id, "prompt": sensitive_text}
        if requester_id is not None
        else {"prompt": sensitive_text}
    )
    return {
        "run_id": run_id,
        "agent_id": agent_id,
        "team_id": team_id,
        "user_id": user_id,
        "created_at": created_at,
        "model_provider": provider,
        "model": model,
        "status": "completed",
        "metrics": metrics if metrics is not None else {"total_tokens": 10},
        "metadata": metadata,
        "messages": [{"content": sensitive_text}],
        "room_id": "!secret-room:example.test",
        "thread_id": "$secret-thread",
        "session_id": "secret-session",
        "tools": [{"arguments": {"token": "sensitive-value"}}],
        "member_responses": nested or [],
    }


def _row(
    raw_run: dict[str, object],
    *,
    entity_id: str = "code",
    entity_kind: str = "agent",
    source: UsageStorageSource | None = None,
    session_metrics: dict[str, object] | None = None,
) -> UsageSessionRow:
    return UsageSessionRow(
        source=source or _source(),
        entity_id=entity_id,
        entity_kind=entity_kind,  # type: ignore[arg-type]
        row_key=f"test-row-{next(_ROW_KEYS)}",
        session_metrics=session_metrics,
        runs=(_extract_run_node(raw_run, depth=0, extracted_nodes=[0], extracted_model_metrics=[0]),),
    )


def _wire_self(monkeypatch: pytest.MonkeyPatch, rows: Iterable[UsageSessionRow]) -> None:
    monkeypatch.setattr("mindroom.usage_stats.discover_self_usage_sources", lambda **_: (_source(),))
    monkeypatch.setattr("mindroom.usage_stats.iter_usage_storage_rows", lambda _: iter(rows))


def _wire_admin(monkeypatch: pytest.MonkeyPatch, rows: Iterable[UsageSessionRow]) -> None:
    monkeypatch.setattr("mindroom.usage_stats.discover_admin_usage_sources", lambda **_: (_source(),))
    monkeypatch.setattr("mindroom.usage_stats.iter_usage_storage_rows", lambda _: iter(rows))


def test_self_usage_uses_storage_requester_precedence_and_canonical_aliases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A changed requester precedence or alias comparison would leak or omit a retained run."""
    _wire_self(
        monkeypatch,
        (
            _row(_raw_run(requester_id="@alice:example.test", user_id="@wrong:example.test")),
            _row(_raw_run(requester_id=None, user_id="@telegram-alice:example.test")),
            _row(
                _raw_run(requester_id="@alice:example.test", agent_id="other"),
                source=_source(source_agent_id="other"),
            ),
            _row(_raw_run(requester_id=None, user_id=None)),
        ),
    )

    report = collect_self_usage(
        agent_name="code",
        requester_id="@telegram-alice:example.test",
        config=_config(),
        runtime_paths=_paths(tmp_path),
        execution_identity=_identity(requester_id="@telegram-alice:example.test"),
        start=None,
        end=None,
        group_by="day",
        as_of=datetime(2026, 1, 3, tzinfo=UTC),
    )

    assert report.run_count == 2
    assert report.totals.total_tokens == 20
    assert report.breakdown[0].key == "2026-01-02"
    assert report.coverage.note == "Retained run usage only; session compaction can make retained history incomplete."


def test_self_usage_shared_coverage_ignores_other_requester_activity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another requester in a shared database must not change any self-visible aggregate or coverage count."""
    own = _row(_raw_run(run_id="shared-run", metrics={"total_tokens": 7}))

    def collect(rows: tuple[UsageSessionRow, ...]) -> dict[str, object]:
        _wire_self(monkeypatch, rows)
        return collect_self_usage(
            agent_name="code",
            requester_id="@alice:example.test",
            config=_config(),
            runtime_paths=_paths(tmp_path),
            execution_identity=_identity(),
            start=None,
            end=None,
            group_by="day",
            as_of=datetime(2026, 1, 3, tzinfo=UTC),
        ).to_dict()

    baseline = collect((own,))
    with_other_requester = collect(
        (
            _row(
                _raw_run(
                    requester_id="@bob:example.test",
                    user_id="@bob:example.test",
                    run_id="shared-run",
                    metrics={"total_tokens": 99},
                ),
            ),
            _row(
                _raw_run(
                    requester_id="@bob:example.test",
                    user_id="@bob:example.test",
                    created_at=None,
                    metrics={"total_tokens": "invalid"},
                ),
            ),
            own,
        ),
    )

    assert with_other_requester == baseline


def test_admin_usage_groups_missing_requesters_as_unknown_and_canonicalizes_filters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A changed missing-requester branch would lose the admin diagnostic and unknown bucket."""
    _wire_admin(
        monkeypatch,
        (
            _row(_raw_run(requester_id="@telegram-alice:example.test")),
            _row(_raw_run(requester_id=None, user_id=None)),
        ),
    )

    report = collect_admin_usage(
        config=_config(),
        runtime_paths=_paths(tmp_path),
        start=None,
        end=None,
        group_by="requester",
        entity_names=None,
        requester_ids=None,
        as_of=datetime(2026, 1, 3, tzinfo=UTC),
    )

    assert report.run_count == 2
    assert report.coverage.missing_requester_runs == 1
    assert [row.key for row in report.breakdown] == ["@alice:example.test", "unknown"]

    _wire_admin(monkeypatch, (_row(_raw_run(requester_id="@telegram-alice:example.test")),))
    filtered = collect_admin_usage(
        config=_config(),
        runtime_paths=_paths(tmp_path),
        start=None,
        end=None,
        group_by="requester",
        entity_names=None,
        requester_ids=("@alice:example.test",),
        as_of=datetime(2026, 1, 3, tzinfo=UTC),
    )
    assert filtered.run_count == 1
    assert filtered.breakdown[0].key == "@alice:example.test"


def test_admin_validates_entity_filters_before_scanning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A changed validation order would scan storage for an invalid entity filter."""
    scanned = False

    def discover(**_: object) -> tuple[UsageStorageSource, ...]:
        nonlocal scanned
        scanned = True
        return (_source(),)

    monkeypatch.setattr("mindroom.usage_stats.discover_admin_usage_sources", discover)

    with pytest.raises(UsageStatsValidationError, match="Unknown entity"):
        collect_admin_usage(
            config=_config(),
            runtime_paths=_paths(tmp_path),
            start=None,
            end=None,
            group_by="entity",
            entity_names=("removed",),
            requester_ids=None,
        )

    assert not scanned


def test_admin_validates_runtime_grouping_before_scanning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A runtime caller cannot route an unsupported grouping through the day fallback."""
    scanned = False

    def discover(**_: object) -> tuple[UsageStorageSource, ...]:
        nonlocal scanned
        scanned = True
        return (_source(),)

    monkeypatch.setattr("mindroom.usage_stats.discover_admin_usage_sources", discover)

    with pytest.raises(UsageStatsValidationError, match="group"):
        collect_admin_usage(
            config=_config(),
            runtime_paths=_paths(tmp_path),
            start=None,
            end=None,
            group_by="unsupported",  # type: ignore[arg-type]
            entity_names=None,
            requester_ids=None,
        )

    assert not scanned


def test_self_validates_runtime_grouping_before_scanning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Self collection rejects an unsupported grouping before source discovery."""
    scanned = False

    def discover(**_: object) -> tuple[UsageStorageSource, ...]:
        nonlocal scanned
        scanned = True
        return (_source(),)

    monkeypatch.setattr("mindroom.usage_stats.discover_self_usage_sources", discover)

    with pytest.raises(UsageStatsValidationError, match="group"):
        collect_self_usage(
            agent_name="code",
            requester_id="@alice:example.test",
            config=_config(),
            runtime_paths=_paths(tmp_path),
            execution_identity=_identity(),
            start=None,
            end=None,
            group_by="unsupported",  # type: ignore[arg-type]
        )

    assert not scanned


def test_admin_entity_grouping_and_filters_use_direct_run_attribution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A changed attribution source would report a row owner instead of direct run entities."""
    _wire_admin(
        monkeypatch,
        (
            _row(
                _raw_run(agent_id="other", metrics={"total_tokens": 7}),
                entity_id="code",
                source=_source(source_agent_id="other"),
            ),
            _row(
                _raw_run(agent_id=None, team_id="engineering", metrics={"total_tokens": 5}),
                entity_id="code",
                source=_source(scope="team", source_agent_id=None, allowed_teams=frozenset({"engineering"})),
            ),
        ),
    )

    report = collect_admin_usage(
        config=_config(team_names=("engineering",)),
        runtime_paths=_paths(tmp_path),
        start=None,
        end=None,
        group_by="entity",
        entity_names=("other", "engineering"),
        requester_ids=None,
        as_of=datetime(2026, 1, 3, tzinfo=UTC),
    )

    assert {(row.key, row.totals.total_tokens) for row in report.breakdown} == {("other", 7), ("engineering", 5)}


@pytest.mark.parametrize("group_by", ["day", "requester", "model"])
def test_unfiltered_admin_non_entity_grouping_rejects_entityless_direct_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    group_by: str,
) -> None:
    """Every admin metric needs valid configured attribution, regardless of grouping."""
    _wire_admin(
        monkeypatch,
        (
            _row(
                _raw_run(agent_id=None, team_id=None, metrics={"total_tokens": 11}),
                source=_source(source_agent_id=None),
            ),
        ),
    )

    report = collect_admin_usage(
        config=_config(),
        runtime_paths=_paths(tmp_path),
        start=None,
        end=None,
        group_by=group_by,  # type: ignore[arg-type]
        entity_names=None,
        requester_ids=None,
        as_of=datetime(2026, 1, 3, tzinfo=UTC),
    )

    assert report.turn_count == 0
    assert report.run_count == 0
    assert report.totals.total_tokens == 0
    assert report.breakdown == ()
    assert report.coverage.malformed_runs == 1
    assert report.coverage.skipped_runs == 1
    assert report.coverage.status == "partial"


@pytest.mark.parametrize(
    ("group_by", "entity_names"),
    [("entity", None), ("day", ("code",))],
)
def test_entityless_direct_runs_are_skipped_when_admin_entity_semantics_require_attribution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    group_by: str,
    entity_names: tuple[str, ...] | None,
) -> None:
    """A changed missing-entity policy would admit an ungroupable or unfilterable direct run."""
    _wire_admin(
        monkeypatch,
        (
            _row(
                _raw_run(agent_id=None, team_id=None, metrics={"total_tokens": 11}),
                source=_source(source_agent_id=None),
            ),
        ),
    )

    report = collect_admin_usage(
        config=_config(),
        runtime_paths=_paths(tmp_path),
        start=None,
        end=None,
        group_by=group_by,  # type: ignore[arg-type]
        entity_names=entity_names,
        requester_ids=None,
        as_of=datetime(2026, 1, 3, tzinfo=UTC),
    )

    assert report.run_count == 0
    assert report.breakdown == ()
    assert report.coverage.malformed_runs == 1
    assert report.coverage.skipped_runs == 1


def test_unfiltered_admin_rejects_removed_nested_entity_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nested metrics for an entity absent from current configuration never enter totals."""
    source = _source(scope="team", source_agent_id=None, allowed_teams=frozenset({"engineering"}))
    root = _raw_run(
        agent_id=None,
        team_id="engineering",
        metrics={"total_tokens": 4},
        nested=[
            _raw_run(
                agent_id="removed",
                requester_id=None,
                user_id=None,
                metrics={"total_tokens": 6},
            ),
        ],
    )
    _wire_admin(monkeypatch, (_row(root, entity_id="engineering", entity_kind="team", source=source),))

    report = collect_admin_usage(
        config=_config(team_names=("engineering",)),
        runtime_paths=_paths(tmp_path),
        start=None,
        end=None,
        group_by="day",
        entity_names=None,
        requester_ids=None,
        as_of=datetime(2026, 1, 3, tzinfo=UTC),
    )

    assert report.turn_count == 1
    assert report.run_count == 1
    assert report.totals.total_tokens == 4
    assert report.coverage.malformed_runs == 1
    assert report.coverage.status == "partial"


def test_malformed_metrics_still_count_missing_requester(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed metrics must not hide an independent requester-attribution gap."""
    _wire_admin(
        monkeypatch,
        (
            _row(
                _raw_run(
                    requester_id=None,
                    user_id=None,
                    metrics={"total_tokens": "not-a-number"},
                ),
            ),
        ),
    )

    report = collect_admin_usage(
        config=_config(),
        runtime_paths=_paths(tmp_path),
        start=None,
        end=None,
        group_by="day",
        entity_names=None,
        requester_ids=None,
        as_of=datetime(2026, 1, 3, tzinfo=UTC),
    )

    assert report.run_count == 0
    assert report.coverage.malformed_runs == 1
    assert report.coverage.missing_requester_runs == 1
    assert report.coverage.skipped_runs == 1
    assert report.coverage.status == "partial"


def test_usage_window_uses_configured_timezone_and_exclusive_date_end() -> None:
    """A changed timezone conversion or end boundary would include the following local midnight."""
    start, end = parse_usage_window(
        start="2026-01-02",
        end="2026-01-02",
        timezone_name="America/Los_Angeles",
        as_of=datetime(2026, 1, 3, tzinfo=UTC),
    )

    assert start == datetime(2026, 1, 2, 8, tzinfo=UTC)
    assert end == datetime(2026, 1, 3, 8, tzinfo=UTC)

    with pytest.raises(ValueError, match="offset"):
        parse_usage_window(
            start="2026-01-02T12:00:00",
            end=None,
            timezone_name="UTC",
            as_of=datetime(2026, 1, 3, tzinfo=UTC),
        )


def test_usage_collection_includes_start_and_excludes_exact_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A changed boundary comparison would include the run at the exclusive end instant."""
    _wire_admin(
        monkeypatch,
        (
            _row(_raw_run(created_at="2026-01-02T00:00:00Z")),
            _row(_raw_run(created_at="2026-01-03T00:00:00Z")),
        ),
    )

    report = collect_admin_usage(
        config=_config(),
        runtime_paths=_paths(tmp_path),
        start="2026-01-02",
        end="2026-01-02",
        group_by="day",
        entity_names=None,
        requester_ids=None,
        as_of=datetime(2026, 1, 4, tzinfo=UTC),
    )

    assert report.run_count == 1
    assert report.first_observed_at == "2026-01-02T00:00:00Z"


def test_usage_window_preserves_configured_timezone_for_explicit_offsets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A changed report serializer would replace configured timezone with an input offset zone."""
    _wire_admin(monkeypatch, (_row(_raw_run(created_at="2026-01-02T12:00:00Z")),))

    report = collect_admin_usage(
        config=_config(timezone="America/Los_Angeles"),
        runtime_paths=_paths(tmp_path),
        start="2026-01-02T00:00:00+02:00",
        end="2026-01-03T00:00:00+02:00",
        group_by="day",
        entity_names=None,
        requester_ids=None,
        as_of=datetime(2026, 1, 3, tzinfo=UTC),
    )

    serialized = report.to_dict()
    assert serialized["window"] == {
        "start": "2026-01-01T22:00:00Z",
        "end": "2026-01-02T22:00:00Z",
        "timezone": "America/Los_Angeles",
    }


def test_direct_usage_aggregates_each_token_field_prefers_model_details_and_uses_decimal_cost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A changed metric source or Decimal conversion would corrupt model totals or known cost."""
    detailed_metrics = {
        "input_tokens": 100,
        "output_tokens": 100,
        "total_tokens": 200,
        "cache_read_tokens": 100,
        "cache_write_tokens": 100,
        "reasoning_tokens": 100,
        "audio_input_tokens": 100,
        "audio_output_tokens": 100,
        "audio_total_tokens": 100,
        "cost": 9.9,
        "details": {
            "chat": [
                {
                    "id": "gpt-detailed",
                    "provider": "openai",
                    "input_tokens": 1,
                    "output_tokens": 2,
                    "total_tokens": 3,
                    "cache_read_tokens": 4,
                    "cache_write_tokens": 5,
                    "reasoning_tokens": 6,
                    "audio_input_tokens": 7,
                    "audio_output_tokens": 8,
                    "audio_total_tokens": 9,
                    "cost": 0.1,
                },
            ],
        },
    }
    fallback_metrics = {
        "input_tokens": 10,
        "output_tokens": 20,
        "total_tokens": 30,
        "cache_read_tokens": 40,
        "cache_write_tokens": 50,
        "reasoning_tokens": 60,
        "audio_input_tokens": 70,
        "audio_output_tokens": 80,
        "audio_total_tokens": 90,
        "cost": "0.20",
    }
    no_cost_metrics = {"total_tokens": 4}
    _wire_admin(
        monkeypatch,
        (
            _row(_raw_run(metrics=detailed_metrics, model="ignored-top-level")),
            _row(_raw_run(metrics=fallback_metrics, model="gpt-fallback")),
            _row(_raw_run(metrics=no_cost_metrics, model="gpt-no-cost")),
        ),
    )

    report = collect_admin_usage(
        config=_config(),
        runtime_paths=_paths(tmp_path),
        start=None,
        end=None,
        group_by="model",
        entity_names=None,
        requester_ids=None,
        as_of=datetime(2026, 1, 3, tzinfo=UTC),
    )

    assert report.totals.to_dict() == {
        "input_tokens": 11,
        "output_tokens": 22,
        "total_tokens": 37,
        "cache_read_tokens": 44,
        "cache_write_tokens": 55,
        "reasoning_tokens": 66,
        "audio_input_tokens": 77,
        "audio_output_tokens": 88,
        "audio_total_tokens": 99,
    }
    assert report.cost.to_dict() == {"known_cost": "0.30", "runs_with_cost": 2, "runs_without_cost": 1}
    assert {(row.model_type, row.provider, row.model_id) for row in report.breakdown} == {
        ("chat", "openai", "gpt-detailed"),
        ("model", "openai", "gpt-fallback"),
        ("model", "openai", "gpt-no-cost"),
    }


def test_report_uses_retained_runs_only_and_never_serializes_persisted_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A changed serializer could disclose fields the selective storage reader intentionally discarded."""
    _wire_self(monkeypatch, (_row(_raw_run(sensitive_text="private prompt and tool argument")),))

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
    payload = repr(report.to_dict())

    assert report.totals.total_tokens == 10
    assert report.coverage.retained_runs == 1
    for forbidden in (
        "metadata",
        "private prompt",
        "secret-room",
        "secret-thread",
        "secret-session",
        "event-secret",
        "storage-row-secret",
        "arguments",
        "not-a-real-storage-path",
    ):
        assert forbidden not in payload


def test_breakdown_retains_top_200_rows_with_stable_total_token_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A changed breakdown cap or sort would return an unbounded or unstable report."""
    rows = tuple(_row(_raw_run(model=f"model-{index:03}", metrics={"total_tokens": index + 1})) for index in range(201))
    _wire_admin(monkeypatch, rows)

    report = collect_admin_usage(
        config=_config(),
        runtime_paths=_paths(tmp_path),
        start=None,
        end=None,
        group_by="model",
        entity_names=None,
        requester_ids=None,
        as_of=datetime(2026, 1, 3, tzinfo=UTC),
    )

    assert len(report.breakdown) == 200
    assert report.breakdown_truncated is True
    assert report.breakdown_omitted == 1
    assert report.breakdown[0].model_id == "model-200"
    assert report.breakdown[-1].model_id == "model-001"


def test_team_nested_runs_inherit_requester_and_deduplicate_stable_member_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing traversal, requester inheritance, or stable dedup would change retained team usage."""
    source = _source(
        scope="team",
        source_agent_id=None,
        allowed_teams=frozenset({"engineering", "nested"}),
    )
    member = _raw_run(agent_id="code", run_id="member-1", requester_id=None, user_id=None, metrics={"total_tokens": 3})
    root = _raw_run(
        agent_id=None,
        team_id="engineering",
        run_id="leader-1",
        metrics={"total_tokens": 10},
        nested=[
            member,
            member,
            _raw_run(
                agent_id=None,
                team_id="nested",
                run_id="nested-1",
                requester_id=None,
                user_id=None,
                metrics={"total_tokens": 4},
            ),
            _raw_run(agent_id="other", run_id="other-1", requester_id="@bob:example.test", metrics={"total_tokens": 9}),
        ],
    )
    _wire_admin(monkeypatch, (_row(root, entity_id="engineering", entity_kind="team", source=source),))

    report = collect_admin_usage(
        config=_config(team_names=("engineering", "nested")),
        runtime_paths=_paths(tmp_path),
        start=None,
        end=None,
        group_by="entity",
        entity_names=None,
        requester_ids=("@alice:example.test",),
        as_of=datetime(2026, 1, 3, tzinfo=UTC),
    )

    assert {(row.key, row.totals.total_tokens) for row in report.breakdown} == {
        ("engineering", 10),
        ("code", 3),
        ("nested", 4),
    }
    assert report.turn_count == 1
    assert report.run_count == 3


def test_self_usage_finds_nested_team_member_without_leader_or_sibling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A changed self admission rule would include team leader or sibling retained metrics."""
    source = _source(scope="team", source_agent_id=None, allowed_teams=frozenset({"engineering"}))
    root = _raw_run(
        agent_id=None,
        team_id="engineering",
        metrics={"total_tokens": 10},
        nested=[
            _raw_run(
                agent_id="code",
                run_id="code-member",
                requester_id=None,
                user_id=None,
                metrics={"total_tokens": 3},
            ),
            _raw_run(
                agent_id="other",
                run_id="other-member",
                requester_id=None,
                user_id=None,
                metrics={"total_tokens": 8},
            ),
        ],
    )
    monkeypatch.setattr("mindroom.usage_stats.discover_self_usage_sources", lambda **_: (source,))
    monkeypatch.setattr(
        "mindroom.usage_stats.iter_usage_storage_rows",
        lambda _: iter((_row(root, entity_id="engineering", entity_kind="team", source=source),)),
    )

    report = collect_self_usage(
        agent_name="code",
        requester_id="@alice:example.test",
        config=_config(team_names=("engineering",)),
        runtime_paths=_paths(tmp_path),
        execution_identity=_identity(),
        start=None,
        end=None,
        group_by="model",
        as_of=datetime(2026, 1, 3, tzinfo=UTC),
    )

    assert report.run_count == 1
    assert report.totals.total_tokens == 3


def test_nested_runs_without_ids_count_once_per_structural_location(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A changed structural dedup key would collapse distinct anonymous nested runs."""
    source = _source(scope="team", source_agent_id=None, allowed_teams=frozenset({"engineering"}))
    root = _raw_run(
        agent_id=None,
        team_id="engineering",
        run_id="leader",
        metrics={"total_tokens": 0},
        nested=[
            _raw_run(agent_id="code", run_id=None, requester_id=None, user_id=None, metrics={"total_tokens": 3}),
            _raw_run(agent_id="code", run_id=None, requester_id=None, user_id=None, metrics={"total_tokens": 3}),
        ],
    )
    _wire_admin(monkeypatch, (_row(root, entity_id="engineering", entity_kind="team", source=source),))

    report = collect_admin_usage(
        config=_config(team_names=("engineering",)),
        runtime_paths=_paths(tmp_path),
        start=None,
        end=None,
        group_by="entity",
        entity_names=None,
        requester_ids=None,
        as_of=datetime(2026, 1, 3, tzinfo=UTC),
    )

    assert report.turn_count == 1
    assert report.run_count == 3
    assert report.totals.total_tokens == 6


def test_nested_runs_inherit_only_missing_parent_timestamps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing child timestamp inherits its parent, while an explicit child timestamp wins."""
    source = _source(scope="team", source_agent_id=None, allowed_teams=frozenset({"engineering"}))
    root = _raw_run(
        agent_id=None,
        team_id="engineering",
        created_at="2026-01-02T12:00:00Z",
        metrics={},
        nested=[
            _raw_run(
                agent_id="code",
                requester_id=None,
                user_id=None,
                created_at=None,
                metrics={"total_tokens": 3},
            ),
            _raw_run(
                agent_id="other",
                requester_id=None,
                user_id=None,
                created_at="2026-01-03T12:00:00Z",
                metrics={"total_tokens": 4},
            ),
        ],
    )
    _wire_admin(monkeypatch, (_row(root, entity_id="engineering", entity_kind="team", source=source),))

    report = collect_admin_usage(
        config=_config(team_names=("engineering",)),
        runtime_paths=_paths(tmp_path),
        start=None,
        end=None,
        group_by="day",
        entity_names=None,
        requester_ids=None,
        as_of=datetime(2026, 1, 4, tzinfo=UTC),
    )

    assert report.turn_count == 1
    assert report.run_count == 2
    assert report.totals.total_tokens == 7
    assert [(row.key, row.totals.total_tokens) for row in report.breakdown] == [
        ("2026-01-03", 4),
        ("2026-01-02", 3),
    ]


def test_admitted_empty_metric_turn_is_not_a_metered_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An admitted retained turn without any token or cost field is not metered."""
    _wire_admin(monkeypatch, (_row(_raw_run(metrics={}), session_metrics={}),))

    report = collect_admin_usage(
        config=_config(),
        runtime_paths=_paths(tmp_path),
        start=None,
        end=None,
        group_by="day",
        entity_names=None,
        requester_ids=None,
        as_of=datetime(2026, 1, 3, tzinfo=UTC),
    )

    assert report.turn_count == 1
    assert report.run_count == 0
    assert report.cost.to_dict() == {"known_cost": "0", "runs_with_cost": 0, "runs_without_cost": 0}
    assert report.coverage.retained_runs == 0


def test_coverage_compaction_and_source_diagnostics_are_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A changed coverage classifier would hide compaction or unreadable-source partial results."""
    source = _source()
    readable = _row(_raw_run(metrics={"total_tokens": 10}), source=source, session_metrics={"total_tokens": 20})
    busy = UsageStorageDiagnostic(path_label="safe-label", status="busy", detail="database busy")
    monkeypatch.setattr("mindroom.usage_stats.discover_admin_usage_sources", lambda **_: (source,))
    monkeypatch.setattr("mindroom.usage_stats.iter_usage_storage_rows", lambda _: iter((readable, busy)))

    report = collect_admin_usage(
        config=_config(),
        runtime_paths=_paths(tmp_path),
        start=None,
        end=None,
        group_by="day",
        entity_names=None,
        requester_ids=None,
        as_of=datetime(2026, 1, 3, tzinfo=UTC),
    )

    assert report.coverage.status == "partial"
    assert report.coverage.compacted_sessions == 1
    assert report.coverage.partial_sources == 1


@pytest.mark.parametrize(
    ("raw_run", "expected_turn_count", "expected_malformed", "expected_missing_timestamp"),
    [
        (_raw_run(agent_id=None, team_id=None, metrics={"total_tokens": 10}), 0, 1, 0),
        (_raw_run(created_at=None, metrics={"total_tokens": 10}), 0, 0, 1),
        (_raw_run(metrics={"total_tokens": "invalid"}), 1, 1, 0),
    ],
)
def test_coverage_exclusions_are_partial_without_fabricating_compaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw_run: dict[str, object],
    expected_turn_count: int,
    expected_malformed: int,
    expected_missing_timestamp: int,
) -> None:
    """Excluded retained records cannot prove complete history or a compaction gap."""
    source = _source(source_agent_id=None) if raw_run.get("agent_id") is None else _source()
    _wire_admin(monkeypatch, (_row(raw_run, source=source, session_metrics={"total_tokens": 10}),))

    report = collect_admin_usage(
        config=_config(),
        runtime_paths=_paths(tmp_path),
        start=None,
        end=None,
        group_by="day",
        entity_names=None,
        requester_ids=None,
        as_of=datetime(2026, 1, 3, tzinfo=UTC),
    )

    assert report.coverage.status == "partial"
    assert report.turn_count == expected_turn_count
    assert report.coverage.compacted_sessions == 0
    assert report.coverage.skipped_runs == 1
    assert report.coverage.malformed_runs == expected_malformed
    assert report.coverage.missing_timestamp_runs == expected_missing_timestamp


def test_oversized_numeric_metric_skips_one_source_and_continues_others(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A byte-bounded but conversion-hostile metric cannot abort independent sources."""
    oversized = replace(_source(), path_label="agents/code/sessions/code.db")
    valid = replace(_source(source_agent_id="other"), path_label="agents/other/sessions/other.db")
    rows = {
        oversized.path_label: _row(
            _raw_run(metrics={"total_tokens": "9" * 5_000}),
            source=oversized,
            session_metrics={"total_tokens": 0},
        ),
        valid.path_label: _row(
            _raw_run(agent_id="other", metrics={"total_tokens": 7}),
            source=valid,
            session_metrics={"total_tokens": 7},
        ),
    }
    monkeypatch.setattr(
        "mindroom.usage_stats.discover_admin_usage_sources",
        lambda **_: (oversized, valid),
    )
    monkeypatch.setattr(
        "mindroom.usage_stats.iter_usage_storage_rows",
        lambda source: iter((rows[source.path_label],)),
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

    assert report.totals.total_tokens == 7
    assert report.run_count == 1
    assert report.coverage.scanned_sources == 2
    assert report.coverage.malformed_runs == 1
    assert report.coverage.status == "partial"


def test_non_text_numeric_limits_skip_hostile_runs_and_keep_independent_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Huge JSON numbers and Decimal exponents must not abort or escape the bounded public report."""
    source = _source()
    _wire_admin(
        monkeypatch,
        (
            _row(_raw_run(metrics={"total_tokens": 10**200}), source=source),
            _row(_raw_run(metrics={"total_tokens": 2, "cost": "1e999999999"}), source=source),
            _row(_raw_run(metrics={"total_tokens": 7, "cost": "0.25"}), source=source),
        ),
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

    assert report.totals.total_tokens == 7
    assert report.cost.known_cost == "0.25"
    assert report.run_count == 1
    assert report.coverage.malformed_runs == 2
    assert report.coverage.status == "partial"


def test_request_row_budget_returns_bounded_partial_admin_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A request stops after its global row budget instead of retaining an unbounded all-history scan."""
    rows = tuple(_row(_raw_run(run_id=f"run-{index}", metrics={"total_tokens": 1})) for index in range(3))
    _wire_admin(monkeypatch, rows)
    monkeypatch.setattr("mindroom.usage_stats._MAX_SCANNED_STORAGE_ROWS", 2)

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

    assert report.totals.total_tokens == 2
    assert report.run_count == 2
    assert report.coverage.partial_sources == 1
    assert report.coverage.status == "partial"


def test_request_metric_budget_bounds_breakdown_cardinality(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Model-detail fanout cannot grow aggregation maps beyond the request budget."""
    detailed = {
        "details": {
            "chat": [
                {"id": "model-a", "provider": "provider", "total_tokens": 2},
                {"id": "model-b", "provider": "provider", "total_tokens": 3},
            ],
        },
    }
    _wire_admin(
        monkeypatch,
        (
            _row(_raw_run(run_id="kept", metrics={"total_tokens": 7})),
            _row(_raw_run(run_id="limited", metrics=detailed)),
        ),
    )
    monkeypatch.setattr("mindroom.usage_stats._MAX_METRIC_RECORDS", 2)

    report = collect_admin_usage(
        config=_config(),
        runtime_paths=_paths(tmp_path),
        start=None,
        end=None,
        group_by="model",
        entity_names=None,
        requester_ids=None,
        as_of=datetime(2026, 1, 3, tzinfo=UTC),
    )

    assert report.totals.total_tokens == 7
    assert report.run_count == 1
    assert len(report.breakdown) == 1
    assert report.coverage.partial_sources == 1
    assert report.coverage.status == "partial"


def test_request_source_budget_bounds_discovery_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even diagnostic-only discovery outcomes cannot create unbounded request work."""
    diagnostics = tuple(
        UsageStorageDiagnostic(path_label=f"source-{index}", status="partial", detail="unavailable")
        for index in range(3)
    )
    monkeypatch.setattr("mindroom.usage_stats.discover_admin_usage_sources", lambda **_: diagnostics)
    monkeypatch.setattr("mindroom.usage_stats._MAX_SCANNED_SOURCES", 1)

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

    assert report.coverage.scanned_sources == 1
    assert report.coverage.partial_sources == 1
    assert report.coverage.status == "partial"


def test_request_run_node_budget_stops_recursive_aggregation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decoded run trees cannot grow aggregate state beyond the request-wide node budget."""
    _wire_admin(
        monkeypatch,
        (
            _row(_raw_run(run_id="kept", metrics={"total_tokens": 7})),
            _row(_raw_run(run_id="limited", metrics={"total_tokens": 9})),
        ),
    )
    monkeypatch.setattr("mindroom.usage_stats._MAX_SCANNED_RUN_NODES", 1)

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

    assert report.totals.total_tokens == 7
    assert report.run_count == 1
    assert report.coverage.partial_sources == 1


def test_request_time_budget_returns_bounded_partial_admin_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exhausted wall-clock budget stops before persisted rows are accumulated."""
    _wire_admin(monkeypatch, (_row(_raw_run(metrics={"total_tokens": 7})),))
    monkeypatch.setattr("mindroom.usage_stats._QUERY_TIME_BUDGET_SECONDS", -1.0)

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

    assert report.run_count == 0
    assert report.coverage.partial_sources == 1
    assert report.coverage.status == "partial"


def test_self_coverage_uses_private_cumulative_evidence_but_keeps_shared_evidence_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A changed privacy gate would expose or trust shared cumulative session metrics."""
    private_source = _source(scope="private_agent", requester_isolated=True)
    private_row = _row(
        _raw_run(metrics={"total_tokens": 10}),
        source=private_source,
        session_metrics={"total_tokens": 10},
    )
    monkeypatch.setattr("mindroom.usage_stats.discover_self_usage_sources", lambda **_: (private_source,))
    monkeypatch.setattr("mindroom.usage_stats.iter_usage_storage_rows", lambda _: iter((private_row,)))

    private_report = collect_self_usage(
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
    assert private_report.coverage.status == "complete_retained"

    shared_source = _source()
    shared_row = _row(
        _raw_run(metrics={"total_tokens": 10}),
        source=shared_source,
        session_metrics={"total_tokens": 10},
    )
    monkeypatch.setattr("mindroom.usage_stats.discover_self_usage_sources", lambda **_: (shared_source,))
    monkeypatch.setattr("mindroom.usage_stats.iter_usage_storage_rows", lambda _: iter((shared_row,)))
    shared_report = collect_self_usage(
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
    assert shared_report.coverage.status == "unknown"
    assert "session_metrics" not in repr(shared_report.to_dict())


def test_self_coverage_distinguishes_unavailable_sources_from_absent_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A changed diagnostic policy would silently treat an unreadable self source as empty history."""
    source = _source()
    monkeypatch.setattr("mindroom.usage_stats.discover_self_usage_sources", lambda **_: (source,))
    monkeypatch.setattr(
        "mindroom.usage_stats.iter_usage_storage_rows",
        lambda _: iter((UsageStorageDiagnostic(path_label="safe", status="busy", detail="database busy"),)),
    )

    with pytest.raises(UsageStatsSourceUnavailableError, match="Usage source unavailable"):
        collect_self_usage(
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

    monkeypatch.setattr(
        "mindroom.usage_stats.iter_usage_storage_rows",
        lambda _: iter((UsageStorageDiagnostic(path_label="safe", status="absent", detail="database absent"),)),
    )
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
    assert report.run_count == 0


@pytest.mark.parametrize(
    ("start", "entity_names", "requester_ids"),
    [("2026-01-03", None, None), (None, ("other",), None), (None, None, ("@bob:example.test",))],
)
def test_coverage_history_comparison_is_independent_of_query_filters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    start: str | None,
    entity_names: tuple[str, ...] | None,
    requester_ids: tuple[str, ...] | None,
) -> None:
    """A filtered query must not compare session totals to its filtered report subtotal."""
    source = _source()
    row = _row(_raw_run(metrics={"total_tokens": 10}), source=source, session_metrics={"total_tokens": 10})
    _wire_admin(monkeypatch, (row,))

    report = collect_admin_usage(
        config=_config(),
        runtime_paths=_paths(tmp_path),
        start=start,
        end=None,
        group_by="day",
        entity_names=entity_names,
        requester_ids=requester_ids,
        as_of=datetime(2026, 1, 4, tzinfo=UTC),
    )

    assert report.run_count == 0
    assert report.coverage.status == "complete_retained"


@pytest.mark.parametrize(
    "session_metrics",
    [{"input_tokens": 4, "output_tokens": 6, "total_tokens": 10}, {"total_tokens": 9}],
)
def test_coverage_unequal_or_lower_cumulative_tokens_are_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_metrics: dict[str, object],
) -> None:
    """A non-equal cumulative token vector cannot establish complete retained history."""
    source = _source()
    row = _row(
        _raw_run(metrics={"input_tokens": 5, "output_tokens": 5, "total_tokens": 10}),
        source=source,
        session_metrics=session_metrics,
    )
    _wire_admin(monkeypatch, (row,))

    report = collect_admin_usage(
        config=_config(),
        runtime_paths=_paths(tmp_path),
        start=None,
        end=None,
        group_by="day",
        entity_names=None,
        requester_ids=None,
        as_of=datetime(2026, 1, 3, tzinfo=UTC),
    )
    assert report.coverage.status == "unknown"


def test_top_level_stable_run_ids_deduplicate_without_collapsing_anonymous_locations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A changed stable key would count repeated top-level identities more than once."""
    source = _source()
    _wire_admin(
        monkeypatch,
        (
            _row(_raw_run(run_id="same-run", metrics={"total_tokens": 10}), source=source),
            _row(_raw_run(run_id="same-run", metrics={"total_tokens": 10}), source=source),
        ),
    )
    report = collect_admin_usage(
        config=_config(),
        runtime_paths=_paths(tmp_path),
        start=None,
        end=None,
        group_by="day",
        entity_names=None,
        requester_ids=None,
        as_of=datetime(2026, 1, 3, tzinfo=UTC),
    )
    assert report.turn_count == 1
    assert report.run_count == 1


def test_duplicate_stable_ids_across_session_rows_keep_per_session_coverage_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A global query dedup must not erase the second row's retained-history comparison total."""
    source = _source()
    _wire_admin(
        monkeypatch,
        (
            _row(
                _raw_run(run_id="same-run", metrics={"total_tokens": 10}),
                source=source,
                session_metrics={"total_tokens": 10},
            ),
            _row(
                _raw_run(run_id="same-run", metrics={"total_tokens": 10}),
                source=source,
                session_metrics={"total_tokens": 10},
            ),
        ),
    )

    report = collect_admin_usage(
        config=_config(),
        runtime_paths=_paths(tmp_path),
        start=None,
        end=None,
        group_by="day",
        entity_names=None,
        requester_ids=None,
        as_of=datetime(2026, 1, 3, tzinfo=UTC),
    )

    assert report.run_count == 1
    assert report.totals.total_tokens == 10
    assert report.coverage.status == "complete_retained"


def test_empty_readable_self_source_makes_mixed_unreadable_sources_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clean empty source must prevent a mixed self scan from becoming source-unavailable."""
    readable = _source()
    busy = replace(_source(), path_label="busy-source")
    monkeypatch.setattr("mindroom.usage_stats.discover_self_usage_sources", lambda **_: (readable, busy))
    monkeypatch.setattr(
        "mindroom.usage_stats.iter_usage_storage_rows",
        lambda source: (
            iter(())
            if source.path_label == readable.path_label
            else iter((UsageStorageDiagnostic(path_label="busy-source", status="busy", detail="database busy"),))
        ),
    )
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
    assert report.coverage.status == "partial"
