"""Request exports preserve provider-call boundaries without retaining content."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from agno.db.sqlite import SqliteDb
from agno.metrics import MessageMetrics, ModelMetrics, RunMetrics
from agno.models.message import Message
from agno.run.agent import RunOutput
from agno.session.agent import AgentSession

from mindroom.agent_storage import create_state_storage
from mindroom.config.agent import AgentConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.legacy_usage_storage import migrate_usage_database
from mindroom.usage_stats import collect_admin_usage

if TYPE_CHECKING:
    from collections.abc import Iterator

    from mindroom.constants import RuntimePaths

_FIRST = {
    "input_tokens": 120_000,
    "output_tokens": 6,
    "total_tokens": 120_006,
    "cache_read_tokens": 100,
    "cache_write_tokens": 40,
    "reasoning_tokens": 3,
    "audio_input_tokens": 1,
    "audio_output_tokens": 2,
    "audio_total_tokens": 3,
}
_SECOND = {
    "input_tokens": 130_000,
    "output_tokens": 8,
    "total_tokens": 130_008,
    "cache_read_tokens": 110,
    "cache_write_tokens": 50,
    "reasoning_tokens": 4,
    "audio_input_tokens": 2,
    "audio_output_tokens": 3,
    "audio_total_tokens": 5,
}


def _run() -> RunOutput:
    totals = {name: value + _SECOND[name] for name, value in _FIRST.items()}
    return RunOutput(
        run_id="run-1",
        session_id="session",
        agent_id="code",
        user_id="@alice:example.test",
        metadata={"requester_id": "@alias:example.test", "secret": "private metadata"},
        model_provider="test-provider",
        model="test-model",
        created_at=1_700_000_000,
        content="private output",
        metrics=RunMetrics(
            **totals,
            details={"model": [ModelMetrics(id="test-model", provider="test-provider", **totals)]},
        ),
        messages=[
            Message(role="assistant", content="private history", from_history=True, metrics=MessageMetrics(**_FIRST)),
            Message(role="user", content="private prompt", metrics=MessageMetrics(**_FIRST)),
            Message(
                role="assistant",
                content="private first response",
                created_at=1_700_000_001,
                metrics=MessageMetrics(**_FIRST),
                provider_data={"secret": "private provider metadata"},
            ),
            Message(role="tool", content="private tool output", metrics=MessageMetrics(**_FIRST)),
            Message(role="assistant", content="private unmetered response"),
            Message(
                role="assistant",
                content="private second response",
                created_at=1_700_000_002,
                metrics=MessageMetrics(**_SECOND),
            ),
        ],
    )


@pytest.fixture
def request_usage(tmp_path: Path) -> Iterator[tuple[Config, RuntimePaths, SqliteDb]]:
    """Persist real run and session rows through the production storage owner."""
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env={})
    config = Config(
        agents={"code": AgentConfig(display_name="Code")},
        authorization=AuthorizationConfig(aliases={"@alice:example.test": ["@alias:example.test"]}),
    )
    storage = create_state_storage("code", tmp_path / "agents/code", subdir="sessions", session_table="code_sessions")
    assert isinstance(storage, SqliteDb)
    run = _run()
    assert run.metrics is not None
    storage.upsert_session(
        AgentSession(
            session_id="session",
            agent_id="code",
            user_id="@alice:example.test",
            session_data={"session_metrics": run.metrics.to_dict()},
        ),
    )
    storage.upsert_run(run, session_id="session")
    try:
        yield config, paths, storage
    finally:
        storage.close()


def test_request_projection_survives_repeated_save_without_content(
    request_usage: tuple[Config, RuntimePaths, SqliteDb],
) -> None:
    """Saving again replaces projected requests instead of appending or copying conversation data."""
    _, _, storage = request_usage
    storage.upsert_run(_run(), session_id="session")
    with sqlite3.connect(storage.db_file) as connection:
        payloads = connection.execute("SELECT usage_data FROM code_sessions_usage").fetchall()
    assert len(payloads) == 1
    payload = json.loads(payloads[0][0])
    assert payload.get("requests") == [
        {"created_at": 1_700_000_001, "metrics": _FIRST},
        {"created_at": 1_700_000_002, "metrics": _SECOND},
    ]
    assert "private" not in json.dumps(payload)


def test_request_export_keeps_short_calls_distinct_and_preserves_all_counters(
    request_usage: tuple[Config, RuntimePaths, SqliteDb],
) -> None:
    """A large run must not turn two smaller provider requests into one pricing input."""
    config, paths, _ = request_usage
    report = collect_admin_usage(config=config, runtime_paths=paths, include_requests=True)
    payload = report.to_dict()
    assert report.totals.input_tokens == 250_000
    assert payload["request_breakdown"] == [
        {
            "entity": "code",
            "user_id": "@alice:example.test",
            "provider": "test-provider",
            "model": "test-model",
            "kind": "run",
            "created_at": 1_700_000_001,
            "totals": _FIRST,
        },
        {
            "entity": "code",
            "user_id": "@alice:example.test",
            "provider": "test-provider",
            "model": "test-model",
            "kind": "run",
            "created_at": 1_700_000_002,
            "totals": _SECOND,
        },
    ]
    assert report.request_coverage is not None
    assert report.request_coverage.unavailable_sources == 0
    assert "run-1" not in json.dumps(payload)
    assert "private" not in json.dumps(payload["request_breakdown"])
    default = collect_admin_usage(config=config, runtime_paths=paths).to_dict()
    assert "request_breakdown" not in default
    assert "request_coverage" not in default


@pytest.mark.parametrize(
    "case",
    ["old", "malformed", "mixed_models", "unknown", "undated", *[f"{field}_mismatch" for field in _FIRST]],
)
def test_unreconciled_request_details_preserve_aggregates_and_mark_coverage(
    request_usage: tuple[Config, RuntimePaths, SqliteDb],
    case: str,
) -> None:
    """Incomplete or ambiguous request facts must never acquire a guessed provider/model."""
    config, paths, storage = request_usage
    with sqlite3.connect(storage.db_file) as connection:
        payload = json.loads(connection.execute("SELECT usage_data FROM code_sessions_usage").fetchone()[0])
        payload["requests"] = [
            {"created_at": 1_700_000_001, "metrics": dict(_FIRST)},
            {"created_at": 1_700_000_002, "metrics": dict(_SECOND)},
        ]
        if case == "old":
            del payload["requests"]
        elif case.endswith("_mismatch"):
            payload["requests"][0]["metrics"][case.removesuffix("_mismatch")] = 0
        elif case == "malformed":
            payload["requests"][0]["metrics"]["input_tokens"] = {"private": "content"}
        elif case == "mixed_models":
            payload["metrics"]["details"]["model"] = [
                {"id": "first-model", "provider": "test-provider", **_FIRST},
                {"id": "second-model", "provider": "test-provider", **_SECOND},
            ]
        elif case == "unknown":
            del payload["model_provider"]
            del payload["metrics"]["details"]
        elif case == "undated":
            del payload["requests"][0]["created_at"]
        connection.execute("UPDATE code_sessions_usage SET usage_data = ?", (json.dumps(payload),))
    report = collect_admin_usage(config=config, runtime_paths=paths, include_requests=True)
    assert report.totals.total_tokens == 250_014
    assert sum(row.totals.total_tokens for row in report.model_breakdown) == 250_014
    assert report.to_dict()["request_breakdown"] == []
    assert report.request_coverage is not None
    assert report.request_coverage.unavailable_sources == 1


def test_initial_migration_imports_available_requests_without_reseeding_old_ledgers(
    request_usage: tuple[Config, RuntimePaths, SqliteDb],
) -> None:
    """Initial seeding may use retained messages; an initialized ledger is never reconstructed."""
    config, paths, storage = request_usage
    with sqlite3.connect(storage.db_file) as connection:
        connection.execute("DROP TABLE code_sessions_usage")
    migrate_usage_database(Path(storage.db_file), "code_sessions")
    report = collect_admin_usage(config=config, runtime_paths=paths, include_requests=True)
    assert [row.totals.input_tokens for row in report.request_breakdown] == [120_000, 130_000]
    with sqlite3.connect(storage.db_file) as connection:
        payload = json.loads(connection.execute("SELECT usage_data FROM code_sessions_usage").fetchone()[0])
        del payload["requests"]
        connection.execute("UPDATE code_sessions_usage SET usage_data = ?", (json.dumps(payload),))
    migrate_usage_database(Path(storage.db_file), "code_sessions")
    report = collect_admin_usage(config=config, runtime_paths=paths, include_requests=True)
    assert report.totals.total_tokens == 250_014
    assert report.request_breakdown == ()
    assert report.request_coverage is not None
    assert report.request_coverage.unavailable_sources == 1


def test_history_without_retained_requests_marks_request_coverage(
    request_usage: tuple[Config, RuntimePaths, SqliteDb],
) -> None:
    """A readable aggregate cannot claim complete request detail after historical rows are lost."""
    config, paths, storage = request_usage
    with sqlite3.connect(storage.db_file) as connection:
        connection.execute("DELETE FROM code_sessions_usage")
    report = collect_admin_usage(config=config, runtime_paths=paths, include_requests=True)
    assert report.totals.total_tokens == 250_014
    assert report.to_dict()["request_breakdown"] == []
    assert report.request_coverage is not None
    assert report.request_coverage.unavailable_sources == 1
