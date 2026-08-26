"""Read-only retained usage aggregation."""
# ruff: noqa: D103

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from mindroom.usage_stats import collect_admin_usage, collect_self_usage
from mindroom.usage_stats_storage import (
    UsageRunNode,
    UsageSessionRow,
    UsageStorageDiagnostic,
    UsageStorageSource,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    import pytest


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
    model: str | None = "gpt-5.6",
) -> UsageRunNode:
    return UsageRunNode(
        team_id=None,
        requester_id=requester_id,
        run_id=run_id,
        model_provider=model_provider,
        model=model,
        metrics=_metrics(total_tokens),
    )


def _row(
    source: UsageStorageSource,
    *runs: UsageRunNode,
    entity_id: str | None = None,
    session_metrics: Mapping[str, object] | None = None,
    row_key: str = "session-1",
    payload_bytes: int = 0,
) -> UsageSessionRow:
    is_team = source.scope == "team"
    return UsageSessionRow(
        source=source,
        entity_id=entity_id or ("engineering" if is_team else source.source_agent_id or "unknown"),
        entity_kind="team" if is_team else "agent",
        row_key=row_key,
        runs=tuple(runs),
        session_metrics=MappingProxyType(dict(session_metrics or {})),
        payload_bytes=payload_bytes,
    )


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    sources: tuple[UsageStorageSource | UsageStorageDiagnostic, ...],
    rows: dict[str, tuple[UsageSessionRow | UsageStorageDiagnostic, ...]],
) -> None:
    monkeypatch.setattr("mindroom.usage_stats.discover_self_usage_sources", lambda **_: sources)
    monkeypatch.setattr("mindroom.usage_stats.discover_admin_usage_sources", lambda **_: sources)

    def iter_rows(
        source: UsageStorageSource,
        *,
        mode: str = "runs",
    ) -> Iterator[UsageSessionRow | UsageStorageDiagnostic]:
        del mode
        yield from rows.get(source.path_label, ())

    monkeypatch.setattr("mindroom.usage_stats.iter_usage_storage_rows", iter_rows)


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
            "model": "gpt-5.6",
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
                    _run(total_tokens=7, model_provider="openai", model="gpt-5.6"),
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
        ("openai", "gpt-5.6", 7, 1),
        ("vertexai", "claude-opus-5", 5, 1),
    ]


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
        ("openai", "gpt-5.6", 20, 2),
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
        model="gpt-5.6",
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
