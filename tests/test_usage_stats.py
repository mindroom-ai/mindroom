"""Direct retained-run usage aggregation behavior."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from mindroom.usage_stats import collect_admin_usage, collect_self_usage, parse_usage_window
from mindroom.usage_stats_storage import (
    UsageSessionRow,
    UsageStorageSource,
    _extract_run_node,
)

if TYPE_CHECKING:
    from collections.abc import Iterable


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
    return resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage", process_env={})


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


def _source() -> UsageStorageSource:
    return UsageStorageSource(
        path=Path("/not-a-real-storage-path.db"),
        path_label="agents/code/sessions/code.db",
        scope="shared_agent",
        expected_session_table="code_sessions",
        source_agent_id="code",
        allowed_agent_ids=frozenset({"code", "other"}),
        allowed_team_ids=frozenset(),
        requester_isolated=False,
    )


def _raw_run(
    *,
    requester_id: str | None = "@alice:example.test",
    user_id: str | None = "@alice:example.test",
    agent_id: str | None = "code",
    team_id: str | None = None,
    created_at: str = "2026-01-02T12:00:00Z",
    metrics: dict[str, object] | None = None,
    model: str = "gpt-5.6",
    provider: str = "openai",
    sensitive_text: str = "never expose this prompt",
) -> dict[str, object]:
    metadata = (
        {"requester_id": requester_id, "prompt": sensitive_text}
        if requester_id is not None
        else {"prompt": sensitive_text}
    )
    return {
        "run_id": "event-secret",
        "agent_id": agent_id,
        "team_id": team_id,
        "user_id": user_id,
        "created_at": created_at,
        "model_provider": provider,
        "model": model,
        "status": "completed",
        "metrics": metrics or {"total_tokens": 10},
        "metadata": metadata,
        "messages": [{"content": sensitive_text}],
        "room_id": "!secret-room:example.test",
        "thread_id": "$secret-thread",
        "session_id": "secret-session",
        "tools": [{"arguments": {"token": "sensitive-value"}}],
    }


def _row(raw_run: dict[str, object], *, entity_id: str = "code") -> UsageSessionRow:
    return UsageSessionRow(
        source=_source(),
        entity_id=entity_id,
        entity_kind="agent",
        row_key="storage-row-secret",
        session_user_id="@session-secret:example.test",
        session_metrics={"total_tokens": 1_000_000},
        runs=(_extract_run_node(raw_run, depth=0, extracted_nodes=[0], extracted_model_metrics=[0]),),
    )


def _wire_self(monkeypatch: pytest.MonkeyPatch, rows: Iterable[UsageSessionRow]) -> None:
    monkeypatch.setattr("mindroom.usage_stats.discover_self_usage_sources", lambda **_: (_source(),))
    monkeypatch.setattr("mindroom.usage_stats.iter_usage_storage_rows", lambda _: iter(rows))


def _wire_admin(monkeypatch: pytest.MonkeyPatch, rows: Iterable[UsageSessionRow]) -> None:
    monkeypatch.setattr("mindroom.usage_stats.discover_admin_usage_sources", lambda **_: (_source(),))
    monkeypatch.setattr("mindroom.usage_stats.iter_usage_storage_rows", lambda _: iter(rows))


def test_self_usage_uses_storage_requester_precedence_and_canonical_aliases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A changed requester precedence or alias comparison would leak or omit a retained run."""
    _wire_self(
        monkeypatch,
        (
            _row(_raw_run(requester_id="@alice:example.test", user_id="@wrong:example.test")),
            _row(_raw_run(requester_id=None, user_id="@telegram-alice:example.test")),
            _row(_raw_run(requester_id="@alice:example.test", agent_id="other")),
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

    with pytest.raises(ValueError, match="Unknown entity"):
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


def test_admin_entity_grouping_and_filters_use_direct_run_attribution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A changed attribution source would report a row owner instead of direct run entities."""
    _wire_admin(
        monkeypatch,
        (
            _row(_raw_run(agent_id="other", metrics={"total_tokens": 7}), entity_id="code"),
            _row(_raw_run(agent_id=None, team_id="engineering", metrics={"total_tokens": 5}), entity_id="code"),
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


def test_usage_collection_includes_start_and_excludes_exact_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_breakdown_retains_top_200_rows_with_stable_total_token_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A changed breakdown cap or sort would return an unbounded or unstable report."""
    rows = tuple(
        _row(_raw_run(model=f"model-{index:03}", metrics={"total_tokens": index + 1}))
        for index in range(201)
    )
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
