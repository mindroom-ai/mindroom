"""Read-only retained usage aggregation."""
# ruff: noqa: D103

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

import pytest
from agno.metrics import ModelMetrics, RunMetrics
from agno.run.agent import RunOutput
from agno.session.agent import AgentSession

from mindroom.agent_storage import create_state_storage
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.legacy_usage_storage import migrate_usage_database
from mindroom.requester_identity import resolve_human_requester_alias
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from mindroom.usage_stats import collect_admin_usage, collect_self_usage
from mindroom.usage_stats_storage import (
    UsageModelMetrics,
    UsageRunNode,
    UsageSessionRow,
    UsageStorageDiagnostic,
    UsageStorageSource,
)
from tests.conftest import create_agno_2_sessions_db, seed_session

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping


def _config() -> Config:
    return Config(
        agents={"code": AgentConfig(display_name="Code"), "other": AgentConfig(display_name="Other")},
        teams={"engineering": TeamConfig(display_name="Engineering", role="Team", agents=["code"])},
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


def _metrics(total_tokens: int = 10) -> Mapping[str, int]:
    return MappingProxyType(
        {"input_tokens": total_tokens - 3, "output_tokens": 3, "total_tokens": total_tokens},
    )


def _run(
    *,
    requester_id: str | None = "@alice:example.test",
    run_id: str | None = "run-1",
    total_tokens: int = 10,
    model_provider: str | None = "openai",
    model: str | None = "gpt-6-astra",
    created_at: float | None = None,
) -> UsageRunNode:
    return UsageRunNode(
        team_id=None,
        requester_id=requester_id,
        run_id=run_id,
        model_provider=model_provider,
        model=model,
        metrics=_metrics(total_tokens),
        created_at=created_at,
    )


def _row(
    source: UsageStorageSource,
    *runs: UsageRunNode,
    entity_id: str | None = None,
    session_metrics: Mapping[str, object] | None = None,
    session_model_metrics: tuple[UsageModelMetrics, ...] | None = (),
    row_key: str = "session-1",
    runs_available: bool = True,
    session_metrics_available: bool = True,
) -> UsageSessionRow:
    is_team = source.scope == "team"
    return UsageSessionRow(
        source=source,
        entity_id=entity_id or ("engineering" if is_team else source.source_agent_id or "unknown"),
        entity_kind="team" if is_team else "agent",
        row_key=row_key,
        runs=tuple(runs),
        session_metrics=MappingProxyType(dict(session_metrics or {})),
        session_model_metrics=session_model_metrics,
        runs_available=runs_available,
        session_metrics_available=session_metrics_available,
    )


def _model_metrics(
    provider: str | None,
    model: str | None,
    **metrics: int,
) -> UsageModelMetrics:
    return UsageModelMetrics(provider, model, MappingProxyType(metrics))


@pytest.mark.parametrize(
    ("source_scope", "agent_name"),
    [("shared_agent", "code"), ("team", None)],
)
def test_admin_exports_reconciled_cumulative_models_separately_from_retained_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_scope: str,
    agent_name: str | None,
) -> None:
    """Compacted session totals retain exact per-model counters and session counts."""
    source = _source(scope=source_scope, agent_name=agent_name)
    session_totals = {
        "input_tokens": 60,
        "output_tokens": 40,
        "total_tokens": 100,
        "cache_read_tokens": 30,
        "cache_write_tokens": 10,
        "reasoning_tokens": 9,
        "audio_input_tokens": 8,
        "audio_output_tokens": 7,
        "audio_total_tokens": 15,
    }
    session_models = (
        _model_metrics(
            "provider-a",
            "model-a",
            input_tokens=20,
            output_tokens=10,
            total_tokens=30,
            cache_read_tokens=10,
            cache_write_tokens=4,
            reasoning_tokens=3,
            audio_input_tokens=2,
            audio_output_tokens=1,
            audio_total_tokens=3,
        ),
        _model_metrics(
            "provider-a",
            "model-a",
            input_tokens=10,
            output_tokens=10,
            total_tokens=20,
            cache_read_tokens=5,
            cache_write_tokens=1,
            reasoning_tokens=2,
            audio_input_tokens=2,
            audio_output_tokens=1,
            audio_total_tokens=3,
        ),
        _model_metrics(
            "provider-b",
            "model-b",
            input_tokens=30,
            output_tokens=20,
            total_tokens=50,
            cache_read_tokens=15,
            cache_write_tokens=5,
            reasoning_tokens=4,
            audio_input_tokens=4,
            audio_output_tokens=5,
            audio_total_tokens=9,
        ),
    )
    _wire(
        monkeypatch,
        (source,),
        {
            source.path_label: (
                _row(
                    source,
                    _run(total_tokens=20, model_provider="retained-provider", model="retained-model"),
                    session_metrics=MappingProxyType(session_totals),
                    session_model_metrics=session_models,
                ),
            ),
        },
    )

    payload = collect_admin_usage(config=_config(), runtime_paths=_paths(tmp_path)).to_dict()

    assert payload["totals"] == session_totals
    assert payload["model_breakdown"] == [
        {
            "provider": "retained-provider",
            "model": "retained-model",
            "totals": {
                "input_tokens": 17,
                "output_tokens": 3,
                "total_tokens": 20,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "reasoning_tokens": 0,
                "audio_input_tokens": 0,
                "audio_output_tokens": 0,
                "audio_total_tokens": 0,
            },
            "run_count": 1,
        },
    ]
    assert payload["cumulative_model_breakdown"] == [
        {
            "provider": "provider-a",
            "model": "model-a",
            "totals": {
                "input_tokens": 30,
                "output_tokens": 20,
                "total_tokens": 50,
                "cache_read_tokens": 15,
                "cache_write_tokens": 5,
                "reasoning_tokens": 5,
                "audio_input_tokens": 4,
                "audio_output_tokens": 2,
                "audio_total_tokens": 6,
            },
            "session_count": 1,
        },
        {
            "provider": "provider-b",
            "model": "model-b",
            "totals": {
                "input_tokens": 30,
                "output_tokens": 20,
                "total_tokens": 50,
                "cache_read_tokens": 15,
                "cache_write_tokens": 5,
                "reasoning_tokens": 4,
                "audio_input_tokens": 4,
                "audio_output_tokens": 5,
                "audio_total_tokens": 9,
            },
            "session_count": 1,
        },
    ]
    assert payload["breakdown"][0]["cumulative_model_breakdown"] == payload["cumulative_model_breakdown"]
    assert payload["cumulative_model_coverage"]["unavailable_sources"] == 0
    assert "retained sessions" in payload["cumulative_model_coverage"]["note"]
    assert "cumulative_model_breakdown" not in payload["user_breakdown"][0]


@pytest.mark.parametrize(
    "session_model_metrics",
    [
        (),
        None,
        (_model_metrics("provider-a", "model-a", total_tokens=-1),),
        (_model_metrics("provider-a", "model-a", total_tokens=5),),
        (_model_metrics(None, "model-a", input_tokens=97, output_tokens=3, total_tokens=100),),
        (_model_metrics("provider-a", None, input_tokens=97, output_tokens=3, total_tokens=100),),
        (
            _model_metrics("provider-a", "model-a", input_tokens=97, output_tokens=3, total_tokens=100),
            _model_metrics("provider-b", "model-b"),
        ),
    ],
    ids=[
        "absent",
        "malformed",
        "negative",
        "unreconciled",
        "missing-provider",
        "missing-model",
        "empty-entry",
    ],
)
def test_admin_keeps_cumulative_totals_under_unknown_when_model_detail_is_unusable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_model_metrics: tuple[UsageModelMetrics, ...] | None,
) -> None:
    """Unusable cumulative attribution cannot discard session or retained-run totals."""
    source = _source()
    _wire(
        monkeypatch,
        (source,),
        {
            source.path_label: (
                _row(
                    source,
                    _run(total_tokens=20),
                    session_metrics=_metrics(100),
                    session_model_metrics=session_model_metrics,
                ),
            ),
        },
    )

    payload = collect_admin_usage(config=_config(), runtime_paths=_paths(tmp_path)).to_dict()

    assert payload["totals"]["total_tokens"] == 100
    assert payload["model_breakdown"][0]["totals"]["total_tokens"] == 20
    assert payload["cumulative_model_breakdown"] == [
        {
            "provider": "unknown",
            "model": "unknown",
            "totals": {
                "input_tokens": 97,
                "output_tokens": 3,
                "total_tokens": 100,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "reasoning_tokens": 0,
                "audio_input_tokens": 0,
                "audio_output_tokens": 0,
                "audio_total_tokens": 0,
            },
            "session_count": 1,
        },
    ]
    assert payload["cumulative_model_coverage"]["unavailable_sources"] == 1


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    sources: tuple[UsageStorageSource | UsageStorageDiagnostic, ...],
    rows: dict[str, tuple[UsageSessionRow | UsageStorageDiagnostic, ...]],
    calls: list[tuple[str, str]] | None = None,
) -> None:
    monkeypatch.setattr("mindroom.usage_stats.discover_self_usage_sources", lambda **_: sources)
    monkeypatch.setattr("mindroom.usage_stats.discover_admin_usage_sources", lambda **_: sources)

    def iter_rows(
        source: UsageStorageSource,
        *,
        mode: str = "runs",
    ) -> Iterator[UsageSessionRow | UsageStorageDiagnostic]:
        if calls is not None:
            calls.append((source.path_label, mode))
        yield from rows.get(source.path_label, ())

    monkeypatch.setattr("mindroom.usage_stats.iter_usage_storage_rows", iter_rows)


@pytest.mark.parametrize("include_daily", [False, True])
def test_admin_groups_canonical_users_and_models_without_counting_duplicate_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    include_daily: bool,
) -> None:
    source = _source()
    first_day = datetime(2026, 9, 14, 23, 59, 59, tzinfo=UTC).timestamp()
    second_day = datetime(2026, 9, 15, tzinfo=UTC).timestamp()
    cached = replace(
        _run(run_id="cached", created_at=first_day),
        metrics=MappingProxyType({**_metrics(10), "cache_read_tokens": 6, "cache_write_tokens": 1}),
    )
    _wire(
        monkeypatch,
        (source,),
        {
            source.path_label: (
                _row(
                    source,
                    cached,
                    cached,
                    _run(
                        requester_id="@telegram-alice:example.test",
                        run_id="alias",
                        total_tokens=20,
                        model="other-model",
                        created_at=second_day,
                    ),
                    _run(requester_id="@bob:example.test", run_id="bob", total_tokens=15, created_at=first_day),
                    _run(requester_id=None, run_id="unattributed", total_tokens=5, created_at=first_day),
                    session_metrics=_metrics(100),
                ),
            ),
        },
    )

    payload = collect_admin_usage(
        config=_config(),
        runtime_paths=_paths(tmp_path),
        include_daily=include_daily,
    ).to_dict()

    users = payload["user_breakdown"]
    assert [row["user_id"] for row in users] == ["@alice:example.test", "@bob:example.test", None]
    assert [row["totals"]["total_tokens"] for row in users] == [30, 15, 5]
    assert users[0]["run_count"] == 2
    assert users[0]["totals"]["cache_read_tokens"] == 6
    assert users[0]["totals"]["cache_write_tokens"] == 1
    assert [(row["model"], row["totals"]["total_tokens"]) for row in users[0]["model_breakdown"]] == [
        ("other-model", 20),
        ("gpt-6-astra", 10),
    ]
    assert payload["totals"]["total_tokens"] == 100
    assert "retained top-level runs" in payload["user_coverage"]["note"]
    assert "null" in payload["user_coverage"]["note"]
    if include_daily:
        alice_days = users[0]["daily_breakdown"]
        assert [(day["date"], day["run_count"], day["totals"]["total_tokens"]) for day in alice_days] == [
            ("2026-09-14", 1, 10),
            ("2026-09-15", 1, 20),
        ]
        assert alice_days[0]["totals"]["input_tokens"] == 7
        assert alice_days[0]["totals"]["output_tokens"] == 3
        assert alice_days[0]["totals"]["cache_read_tokens"] == 6
        assert alice_days[0]["totals"]["cache_write_tokens"] == 1
        assert alice_days[0]["model_breakdown"] == [users[0]["model_breakdown"][1]]
        assert alice_days[1]["model_breakdown"] == [users[0]["model_breakdown"][0]]
        assert [(day["date"], day["totals"]["total_tokens"]) for day in users[1]["daily_breakdown"]] == [
            ("2026-09-14", 15),
        ]
        assert [(day["date"], day["totals"]["total_tokens"]) for day in users[2]["daily_breakdown"]] == [
            ("2026-09-14", 5),
        ]
        assert [
            (day["date"], day["run_count"], day["totals"]["total_tokens"]) for day in payload["daily_breakdown"]
        ] == [
            ("2026-09-14", 3, 30),
            ("2026-09-15", 1, 20),
        ]
    else:
        assert all("daily_breakdown" not in user for user in users)


def test_daily_usage_groups_utc_dates_and_deduplicates_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _source()
    team = _source(scope="team", agent_name=None)
    before_midnight = replace(
        _run(created_at=datetime(2026, 9, 14, 23, 59, 59, tzinfo=UTC).timestamp()),
        metrics=MappingProxyType({**_metrics(), "cache_read_tokens": 4, "reasoning_tokens": 2}),
    )
    after_midnight = _run(
        run_id="later",
        total_tokens=20,
        created_at=datetime(2026, 9, 15, tzinfo=UTC).timestamp(),
    )
    _wire(
        monkeypatch,
        (source, team),
        {
            source.path_label: (
                _row(source, after_midnight, before_midnight, before_midnight, session_metrics=_metrics(100)),
                _row(source, before_midnight, row_key="session-2", session_metrics=_metrics(70)),
            ),
            team.path_label: (_row(team, after_midnight, session_metrics=_metrics(100)),),
        },
    )
    config = _config()
    config.timezone = "America/Los_Angeles"

    payload = collect_admin_usage(config=config, runtime_paths=_paths(tmp_path), include_daily=True).to_dict()

    assert [(row["date"], row["totals"]["total_tokens"], row["run_count"]) for row in payload["daily_breakdown"]] == [
        ("2026-09-14", 20, 2),
        ("2026-09-15", 40, 2),
    ]
    assert payload["daily_breakdown"][0]["totals"] == {
        "input_tokens": 14,
        "output_tokens": 6,
        "total_tokens": 20,
        "cache_read_tokens": 8,
        "cache_write_tokens": 0,
        "reasoning_tokens": 4,
        "audio_input_tokens": 0,
        "audio_output_tokens": 0,
        "audio_total_tokens": 0,
    }
    assert payload["totals"]["total_tokens"] == 270
    assert payload["daily_coverage"]["scanned_sources"] == 2
    assert payload["daily_coverage"]["unavailable_sources"] == 0
    assert "UTC" in payload["daily_coverage"]["note"]
    assert "retained top-level runs" in payload["daily_coverage"]["note"]


@pytest.mark.parametrize("private", [False, True])
def test_daily_self_usage_keeps_requester_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    private: bool,
) -> None:
    source = _source(scope="private_agent" if private else "shared_agent", requester_isolated=private)
    timestamp = datetime(2026, 9, 15, tzinfo=UTC).timestamp()
    _wire(
        monkeypatch,
        (source,),
        {
            source.path_label: (
                _row(
                    source,
                    _run(requester_id="@telegram-alice:example.test", created_at=timestamp),
                    _run(requester_id=None if private else "@bob:example.test", run_id="other", created_at=timestamp),
                    session_metrics=_metrics(100),
                ),
            ),
        },
    )

    payload = collect_self_usage(
        agent_name="code",
        requester_id="@alice:example.test",
        config=_config(),
        runtime_paths=_paths(tmp_path),
        execution_identity=_identity(),
        include_daily=True,
    ).to_dict()

    assert len(payload["daily_breakdown"]) == 1
    assert payload["daily_breakdown"][0]["totals"]["total_tokens"] == (20 if private else 10)
    assert payload["daily_breakdown"][0]["run_count"] == (2 if private else 1)
    assert payload["totals"]["total_tokens"] == (100 if private else 10)
    assert payload["daily_coverage"]["unavailable_sources"] == 0
    assert "user_breakdown" not in payload
    assert "@bob" not in str(payload)


@pytest.mark.parametrize("created_at", [None, float("nan"), float("inf"), 10**100])
def test_daily_usage_skips_undatable_runs_without_losing_other_totals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    created_at: float | None,
) -> None:
    source = _source()
    unavailable = UsageStorageDiagnostic(path_label="unreadable.db", status="corrupt", detail="database corrupt")
    _wire(
        monkeypatch,
        (source, unavailable),
        {
            source.path_label: (
                _row(
                    source,
                    _run(run_id="dated", created_at=datetime(2026, 9, 15, tzinfo=UTC).timestamp()),
                    _run(run_id="undated", requester_id="@bob:example.test", created_at=created_at),
                    session_metrics=_metrics(100),
                ),
            ),
        },
    )

    payload = collect_admin_usage(config=_config(), runtime_paths=_paths(tmp_path), include_daily=True).to_dict()

    assert payload["daily_breakdown"][0]["totals"]["total_tokens"] == 10
    assert payload["daily_breakdown"][0]["run_count"] == 1
    assert payload["daily_coverage"]["unavailable_sources"] == 2
    assert payload["model_coverage"]["unavailable_sources"] == 1
    assert payload["model_breakdown"][0]["totals"]["total_tokens"] == 20
    assert payload["totals"]["total_tokens"] == 100
    users = {user["user_id"]: user for user in payload["user_breakdown"]}
    assert users["@alice:example.test"]["daily_breakdown"] == payload["daily_breakdown"]
    assert users["@bob:example.test"]["daily_breakdown"] == []
    assert users["@bob:example.test"]["totals"]["total_tokens"] == 10


def test_daily_usage_returns_empty_breakdown_without_retained_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source()
    _wire(monkeypatch, (source,), {source.path_label: (_row(source, session_metrics=_metrics(100)),)})

    payload = collect_admin_usage(config=_config(), runtime_paths=_paths(tmp_path), include_daily=True).to_dict()

    assert payload["daily_breakdown"] == []
    assert payload["daily_coverage"]["scanned_sources"] == 1
    assert payload["totals"]["total_tokens"] == 100


@pytest.mark.parametrize("admin", [False, True])
def test_daily_and_combined_models_use_agno_details(tmp_path: Path, admin: bool) -> None:
    paths = _paths(tmp_path)
    config = Config(agents={"code": AgentConfig(display_name="Code")})
    storage = create_state_storage(
        "code",
        paths.storage_root / "agents" / "code",
        subdir="sessions",
        session_table="code_sessions",
    )
    first_day = int(datetime(2026, 9, 14, tzinfo=UTC).timestamp())
    metrics = RunMetrics(
        input_tokens=17,
        output_tokens=7,
        total_tokens=24,
        cache_read_tokens=10,
        cache_write_tokens=3,
        details={
            "model": [
                ModelMetrics(
                    id="model-a",
                    provider="provider-a",
                    input_tokens=10,
                    output_tokens=4,
                    total_tokens=14,
                    cache_read_tokens=6,
                    cache_write_tokens=2,
                ),
            ],
            "reasoning_model": [
                ModelMetrics(
                    id="model-a",
                    provider="provider-a",
                    input_tokens=2,
                    output_tokens=1,
                    total_tokens=3,
                    cache_read_tokens=1,
                ),
            ],
            "output_model": [
                ModelMetrics(
                    id="model-b",
                    provider="provider-b",
                    input_tokens=5,
                    output_tokens=2,
                    total_tokens=7,
                    cache_read_tokens=3,
                    cache_write_tokens=1,
                ),
            ],
        },
    )
    totals = {
        "input_tokens": 41,
        "output_tokens": 11,
        "total_tokens": 52,
        "cache_read_tokens": 24,
        "cache_write_tokens": 7,
    }
    try:
        seed_session(
            storage,
            AgentSession(
                session_id="session-1",
                agent_id="code",
                user_id="@alice:example.test",
                session_data={"session_metrics": totals},
                runs=[
                    RunOutput(
                        run_id="mixed",
                        model_provider="provider-a",
                        model="model-a",
                        created_at=first_day,
                        metrics=metrics,
                    ),
                    RunOutput(
                        run_id="other-provider",
                        model_provider="other-provider",
                        model="model-a",
                        created_at=first_day,
                        metrics=RunMetrics(
                            input_tokens=20,
                            output_tokens=3,
                            total_tokens=23,
                            cache_read_tokens=12,
                            cache_write_tokens=4,
                        ),
                    ),
                    RunOutput(
                        run_id="next-day",
                        model_provider="provider-a",
                        model="model-a",
                        created_at=first_day + 86400,
                        metrics=RunMetrics(input_tokens=4, output_tokens=1, total_tokens=5, cache_read_tokens=2),
                    ),
                ],
            ),
        )
    finally:
        storage.close()

    report = (
        collect_admin_usage(config=config, runtime_paths=paths, include_daily=True)
        if admin
        else collect_self_usage(
            agent_name="code",
            requester_id="@alice:example.test",
            config=config,
            runtime_paths=paths,
            execution_identity=_identity(),
            include_daily=True,
        )
    ).to_dict()

    assert {key: report["totals"][key] for key in totals} == totals
    daily = report["daily_breakdown"]
    assert [(row["date"], row["run_count"]) for row in daily] == [("2026-09-14", 2), ("2026-09-15", 1)]
    assert [
        (row["provider"], row["model"], row["run_count"], row["totals"]["total_tokens"])
        for row in daily[0]["model_breakdown"]
    ] == [
        ("other-provider", "model-a", 1, 23),
        ("provider-a", "model-a", 1, 17),
        ("provider-b", "model-b", 1, 7),
    ]
    assert {key: daily[0]["totals"][key] for key in totals} == {
        "input_tokens": 37,
        "output_tokens": 10,
        "total_tokens": 47,
        "cache_read_tokens": 22,
        "cache_write_tokens": 7,
    }
    assert {key: daily[0]["model_breakdown"][1]["totals"][key] for key in totals} == {
        "input_tokens": 12,
        "output_tokens": 5,
        "total_tokens": 17,
        "cache_read_tokens": 7,
        "cache_write_tokens": 2,
    }
    assert [
        (row["provider"], row["model"], row["run_count"], row["totals"]["total_tokens"])
        for row in report["model_breakdown"]
    ] == [
        ("other-provider", "model-a", 1, 23),
        ("provider-a", "model-a", 2, 22),
        ("provider-b", "model-b", 1, 7),
    ]
    assert report["daily_coverage"]["unavailable_sources"] == 0
    if admin:
        assert report["user_breakdown"][0]["run_count"] == 3
        assert report["user_breakdown"][0]["totals"]["total_tokens"] == 52
        assert report["user_breakdown"][0]["model_breakdown"] == report["model_breakdown"]
        assert report["user_breakdown"][0]["daily_breakdown"] == report["daily_breakdown"]


@pytest.mark.parametrize("double_encoded", [False, True])
def test_daily_models_read_real_agno_2_history(tmp_path: Path, double_encoded: bool) -> None:
    paths = _paths(tmp_path)
    database = create_agno_2_sessions_db(paths.storage_root / "agents" / "code" / "sessions" / "code.db")
    if not double_encoded:
        with sqlite3.connect(database) as connection:
            runs = connection.execute("SELECT runs FROM code_sessions").fetchone()[0]
            connection.execute("UPDATE code_sessions SET runs = ?", (json.loads(runs),))
    migrate_usage_database(database, "code_sessions")

    report = collect_self_usage(
        agent_name="code",
        requester_id="@alice:example.test",
        config=_config(),
        runtime_paths=paths,
        execution_identity=_identity(),
        include_daily=True,
    ).to_dict()

    assert report["totals"]["total_tokens"] == 12
    assert len(report["daily_breakdown"]) == 1
    day = report["daily_breakdown"][0]
    assert day["date"] == "2023-11-14"
    assert day["run_count"] == 3
    assert day["totals"]["input_tokens"] == 6
    assert day["totals"]["output_tokens"] == 6
    assert day["totals"]["cache_read_tokens"] == 0
    assert day["model_breakdown"] == report["model_breakdown"]
    assert day["model_breakdown"][0]["provider"] == "unknown"
    assert day["model_breakdown"][0]["model"] == "unknown"
    assert day["model_breakdown"][0]["totals"]["total_tokens"] == 12


def test_migration_keeps_valid_usage_beside_a_malformed_counter(tmp_path: Path) -> None:
    """A damaged historical metric must flag incomplete coverage without hiding usable neighbors."""
    paths = _paths(tmp_path)
    database = create_agno_2_sessions_db(paths.storage_root / "agents/code/sessions/code.db")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE code_sessions SET runs = ?",
            (
                json.dumps(
                    [
                        {"run_id": "valid", "user_id": "@alice:example.test", "metrics": {"total_tokens": 5}},
                        {
                            "run_id": "broken",
                            "user_id": "@alice:example.test",
                            "metrics": {"total_tokens": {"private": "content"}},
                        },
                    ],
                ),
            ),
        )
    migrate_usage_database(database, "code_sessions")

    report = collect_self_usage(
        agent_name="code",
        requester_id="@alice:example.test",
        config=_config(),
        runtime_paths=paths,
        execution_identity=_identity(),
    ).to_dict()

    assert report["totals"]["total_tokens"] == 5
    assert sum(model["totals"]["total_tokens"] for model in report["model_breakdown"]) == 5
    assert report["coverage"]["unavailable_sources"] == 1
    assert report["model_coverage"]["unavailable_sources"] == 1


@pytest.mark.parametrize(
    "details",
    [
        "invalid",
        {},
        {"model": []},
        {"model": ["invalid"]},
        {"model": [{"id": "model-a", "provider": "provider-a", "total_tokens": -1}]},
        {"model": [{"id": "model-a", "provider": "provider-a", "total_tokens": 5}]},
    ],
)
def test_unusable_model_details_preserve_daily_totals(tmp_path: Path, details: object) -> None:
    paths = _paths(tmp_path)
    config = Config(agents={"code": AgentConfig(display_name="Code")})
    storage = create_state_storage(
        "code",
        paths.storage_root / "agents" / "code",
        subdir="sessions",
        session_table="code_sessions",
    )
    try:
        storage.upsert_session(AgentSession(session_id="session-1", agent_id="code", user_id="@alice:example.test"))
        storage.upsert_run(
            {
                "run_id": "run-1",
                "model_provider": "provider-a",
                "model": "model-a",
                "created_at": 1_700_000_000,
                "metrics": {"input_tokens": 12, "output_tokens": 8, "total_tokens": 20, "details": details},
            },
            session_id="session-1",
        )
    finally:
        storage.close()

    report = collect_self_usage(
        agent_name="code",
        requester_id="@alice:example.test",
        config=config,
        runtime_paths=paths,
        execution_identity=_identity(),
        include_daily=True,
    ).to_dict()

    assert report["totals"]["total_tokens"] == 20
    day = report["daily_breakdown"][0]
    assert day["totals"]["total_tokens"] == 20
    assert day["model_breakdown"][0]["provider"] == "unknown"
    assert day["model_breakdown"][0]["model"] == "unknown"
    assert day["model_breakdown"][0]["totals"]["total_tokens"] == 20
    assert report["model_coverage"]["unavailable_sources"] == 1
    assert report["daily_coverage"]["unavailable_sources"] == 1


def test_admin_resolves_repeated_requesters_once_per_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _source()
    run = _run(requester_id="@telegram-alice:example.test")
    _wire(
        monkeypatch,
        (source,),
        {
            source.path_label: (
                _row(source, run, row_key="first"),
                _row(source, run, row_key="second"),
            ),
        },
    )
    resolutions: list[str] = []

    def resolve(user_id: str, config: Config, runtime_paths: RuntimePaths) -> str:
        resolutions.append(user_id)
        return resolve_human_requester_alias(user_id, config, runtime_paths)

    monkeypatch.setattr("mindroom.usage_stats.resolve_human_requester_alias", resolve)
    first = collect_admin_usage(config=_config(), runtime_paths=_paths(tmp_path))
    changed_config = _config()
    changed_config.authorization.aliases = {"@bob:example.test": ["@telegram-alice:example.test"]}
    second = collect_admin_usage(config=changed_config, runtime_paths=_paths(tmp_path))

    assert first.user_breakdown[0].user_id == "@alice:example.test"
    assert second.user_breakdown[0].user_id == "@bob:example.test"
    assert resolutions == ["@telegram-alice:example.test", "@telegram-alice:example.test"]


def test_self_report_does_not_expose_user_breakdown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _source()
    _wire(monkeypatch, (source,), {source.path_label: (_row(source, _run()),)})
    payload = collect_self_usage(
        agent_name="code",
        requester_id="@alice:example.test",
        config=_config(),
        runtime_paths=_paths(tmp_path),
        execution_identity=_identity(),
    ).to_dict()
    assert "user_breakdown" not in payload
    assert "user_coverage" not in payload


def test_self_usage_is_requester_scoped_and_small(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _source()
    _wire(
        monkeypatch,
        (source,),
        {
            source.path_label: (
                _row(
                    source,
                    _run(requester_id="@telegram-alice:example.test", run_id="own"),
                    _run(
                        requester_id="@bob:example.test",
                        run_id="other",
                        total_tokens=50,
                        model="other-model",
                    ),
                ),
            ),
        },
    )

    report = collect_self_usage(
        agent_name="code",
        requester_id="@alice:example.test",
        config=_config(),
        runtime_paths=_paths(tmp_path),
        execution_identity=_identity(),
    )

    payload = report.to_dict()
    assert payload["scope"] == "self"
    assert payload["totals"]["total_tokens"] == 10  # type: ignore[index]
    assert payload["session_count"] == 1
    assert payload["breakdown"] == []
    assert payload["model_breakdown"] == [
        {
            "provider": "openai",
            "model": "gpt-6-astra",
            "totals": {
                **_metrics(),
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "reasoning_tokens": 0,
                "audio_input_tokens": 0,
                "audio_output_tokens": 0,
                "audio_total_tokens": 0,
            },
            "run_count": 1,
        },
    ]
    assert payload["model_coverage"]["scanned_sources"] == 1  # type: ignore[index]
    assert payload["model_coverage"]["unavailable_sources"] == 0  # type: ignore[index]
    assert "retained top-level runs" in payload["model_coverage"]["note"]  # type: ignore[index]
    assert "window" not in payload
    assert "run_count" not in payload
    assert "first_observed_at" not in payload
    assert "private_agent_breakdown" not in payload
    assert "private_agent_coverage" not in payload


def test_shared_self_reads_each_source_once_for_totals_and_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source()
    calls: list[tuple[str, str]] = []
    _wire(
        monkeypatch,
        (source,),
        {source.path_label: (_row(source, _run(), session_metrics_available=False),)},
        calls,
    )

    report = collect_self_usage(
        agent_name="code",
        requester_id="@alice:example.test",
        config=_config(),
        runtime_paths=_paths(tmp_path),
        execution_identity=_identity(),
    )

    assert calls == [(source.path_label, "runs")]
    assert report.totals.total_tokens == 10
    assert report.model_breakdown[0].totals.total_tokens == 10


def test_self_usage_accepts_missing_requester_in_exact_private_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(scope="private_agent", requester_isolated=True)
    _wire(monkeypatch, (source,), {source.path_label: (_row(source, session_metrics=_metrics()),)})

    report = collect_self_usage(
        agent_name="code",
        requester_id="@alice:example.test",
        config=_config(),
        runtime_paths=_paths(tmp_path),
        execution_identity=_identity(),
    )

    assert report.totals.total_tokens == 10


def test_private_self_usage_uses_compaction_safe_session_metrics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(scope="private_agent", requester_isolated=True)
    _wire(
        monkeypatch,
        (source,),
        {source.path_label: (_row(source, _run(total_tokens=10), session_metrics=_metrics(25)),)},
    )

    report = collect_self_usage(
        agent_name="code",
        requester_id="@alice:example.test",
        config=_config(),
        runtime_paths=_paths(tmp_path),
        execution_identity=_identity(),
    )

    assert report.totals.total_tokens == 25
    assert report.session_count == 1
    assert report.model_breakdown[0].totals.total_tokens == 10
    assert report.model_breakdown[0].run_count == 1
    payload = report.to_dict()
    assert "private_agent_breakdown" not in payload
    assert "private_agent_coverage" not in payload


def test_self_usage_marks_missing_shared_requester_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source()
    _wire(monkeypatch, (source,), {source.path_label: (_row(source, _run(requester_id=None)),)})

    report = collect_self_usage(
        agent_name="code",
        requester_id="@alice:example.test",
        config=_config(),
        runtime_paths=_paths(tmp_path),
        execution_identity=_identity(),
    )

    assert report.totals.total_tokens == 0
    assert report.coverage.unavailable_sources == 1


def test_admin_usage_uses_member_inclusive_session_metrics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_source = _source()
    team_source = _source(scope="team", agent_name=None)
    _wire(
        monkeypatch,
        (agent_source, team_source),
        {
            agent_source.path_label: (
                _row(
                    agent_source,
                    _run(total_tokens=7, model_provider="openai", model="gpt-6-astra"),
                    session_metrics=_metrics(10),
                ),
            ),
            team_source.path_label: (
                _row(
                    team_source,
                    _run(total_tokens=5, model_provider="vertexai", model="claude-opus-5"),
                    session_metrics=_metrics(30),
                ),
            ),
        },
    )

    report = collect_admin_usage(config=_config(), runtime_paths=_paths(tmp_path))

    assert report.totals.total_tokens == 40
    assert report.session_count == 2
    assert {(row.key, row.totals.total_tokens) for row in report.breakdown} == {
        ("code", 10),
        ("engineering", 30),
    }
    assert [
        (row.model_provider, row.model, row.totals.total_tokens, row.run_count) for row in report.model_breakdown
    ] == [
        ("openai", "gpt-6-astra", 7, 1),
        ("vertexai", "claude-opus-5", 5, 1),
    ]


def test_admin_reads_each_source_once_for_session_and_model_views(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source()
    calls: list[tuple[str, str]] = []
    _wire(
        monkeypatch,
        (source,),
        {source.path_label: (_row(source, _run(), session_metrics=_metrics()),)},
        calls,
    )

    collect_admin_usage(config=_config(), runtime_paths=_paths(tmp_path))

    assert calls == [(source.path_label, "both")]


def test_model_breakdown_groups_runs_and_uses_unknown_for_missing_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source()
    _wire(
        monkeypatch,
        (source,),
        {
            source.path_label: (
                _row(
                    source,
                    _run(run_id="run-1", total_tokens=8),
                    _run(run_id="run-2", total_tokens=12),
                    _run(run_id="run-3", total_tokens=5, model_provider=None, model=None),
                    session_metrics=_metrics(30),
                ),
            ),
        },
    )

    report = collect_admin_usage(config=_config(), runtime_paths=_paths(tmp_path))

    assert [
        (row.model_provider, row.model, row.totals.total_tokens, row.run_count) for row in report.model_breakdown
    ] == [
        ("openai", "gpt-6-astra", 20, 2),
        ("unknown", "unknown", 5, 1),
    ]


def test_model_breakdown_deduplicates_repeated_retained_run_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source()
    _wire(
        monkeypatch,
        (source,),
        {
            source.path_label: (
                _row(
                    source,
                    _run(run_id="duplicate", total_tokens=8),
                    _run(run_id="duplicate", total_tokens=8),
                    session_metrics=_metrics(8),
                ),
            ),
        },
    )

    report = collect_admin_usage(config=_config(), runtime_paths=_paths(tmp_path))

    assert [(row.totals.total_tokens, row.run_count) for row in report.model_breakdown] == [(8, 1)]


def test_model_breakdown_uses_provider_and_model_as_equal_token_tiebreakers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source()
    _wire(
        monkeypatch,
        (source,),
        {
            source.path_label: (
                _row(
                    source,
                    _run(run_id="z-model", model_provider="zeta", model="alpha"),
                    _run(run_id="a-model", model_provider="alpha", model="zeta"),
                    session_metrics=_metrics(20),
                ),
            ),
        },
    )

    report = collect_admin_usage(config=_config(), runtime_paths=_paths(tmp_path))

    assert [(row.model_provider, row.model) for row in report.model_breakdown] == [
        ("alpha", "zeta"),
        ("zeta", "alpha"),
    ]


def test_admin_usage_rejects_unconfigured_entity_attribution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(agent_name="rogue")
    _wire(monkeypatch, (source,), {source.path_label: (_row(source, session_metrics=_metrics()),)})

    report = collect_admin_usage(config=_config(), runtime_paths=_paths(tmp_path))

    assert report.totals.total_tokens == 0
    assert report.session_count == 0
    assert report.model_breakdown == ()


def test_invalid_admin_session_metrics_mark_source_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source()
    _wire(
        monkeypatch,
        (source,),
        {source.path_label: (_row(source, session_metrics={"total_tokens": -1}),)},
    )

    report = collect_admin_usage(config=_config(), runtime_paths=_paths(tmp_path))

    assert report.totals.total_tokens == 0
    assert report.coverage.unavailable_sources == 1


def test_invalid_model_run_does_not_discard_authoritative_admin_totals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source()
    invalid_run = UsageRunNode(
        team_id=None,
        requester_id="@alice:example.test",
        run_id="invalid",
        model_provider="openai",
        model="gpt-6-astra",
        metrics=MappingProxyType({"total_tokens": -1}),
    )
    _wire(
        monkeypatch,
        (source,),
        {source.path_label: (_row(source, invalid_run, session_metrics=_metrics(20)),)},
    )

    report = collect_admin_usage(config=_config(), runtime_paths=_paths(tmp_path))

    assert report.totals.total_tokens == 20
    assert report.coverage.unavailable_sources == 0
    assert report.model_breakdown == ()
    assert report.model_coverage.unavailable_sources == 1


def test_unavailable_model_payload_does_not_discard_authoritative_admin_totals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source()
    _wire(
        monkeypatch,
        (source,),
        {
            source.path_label: (
                _row(
                    source,
                    session_metrics=_metrics(20),
                    runs_available=False,
                ),
            ),
        },
    )

    report = collect_admin_usage(config=_config(), runtime_paths=_paths(tmp_path))

    assert report.totals.total_tokens == 20
    assert report.coverage.unavailable_sources == 0
    assert report.model_breakdown == ()
    assert report.model_coverage.unavailable_sources == 1


def test_unavailable_session_payload_does_not_discard_model_attribution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source()
    _wire(
        monkeypatch,
        (source,),
        {
            source.path_label: (
                _row(
                    source,
                    _run(total_tokens=12),
                    session_metrics_available=False,
                ),
            ),
        },
    )

    report = collect_admin_usage(config=_config(), runtime_paths=_paths(tmp_path))

    assert report.totals.total_tokens == 0
    assert report.coverage.unavailable_sources == 1
    assert report.model_breakdown[0].totals.total_tokens == 12
    assert report.model_coverage.unavailable_sources == 0


def test_admin_usage_reads_every_retained_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source()
    _wire(
        monkeypatch,
        (source,),
        {
            source.path_label: (
                _row(source, session_metrics=_metrics(), row_key="session-1"),
                _row(source, session_metrics=_metrics(), row_key="session-2"),
            ),
        },
    )
    report = collect_admin_usage(config=_config(), runtime_paths=_paths(tmp_path))

    assert report.totals.total_tokens == 20
    assert report.session_count == 2
    assert "truncated" not in report.to_dict()["coverage"]  # type: ignore[operator]


def test_unavailable_source_is_content_free(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    diagnostic = UsageStorageDiagnostic(path_label="secret/path.db", status="busy", detail="database busy")
    _wire(monkeypatch, (diagnostic,), {})

    payload = collect_admin_usage(config=_config(), runtime_paths=_paths(tmp_path)).to_dict()

    assert payload["coverage"]["unavailable_sources"] == 1  # type: ignore[index]
    assert "secret" not in str(payload)
