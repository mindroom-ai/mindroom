"""Read-only Agno usage storage adapter."""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

from mindroom.config.agent import AgentConfig, AgentPrivateConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_key, worker_dir_name
from mindroom.usage_stats_storage import (
    UsageSessionRow,
    UsageStorageDiagnostic,
    UsageStorageSource,
    discover_admin_usage_sources,
    discover_self_usage_sources,
    iter_usage_storage_rows,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_SESSION_COLUMNS = """
    session_id TEXT PRIMARY KEY,
    session_type TEXT NOT NULL,
    agent_id TEXT,
    team_id TEXT,
    user_id TEXT,
    session_data TEXT,
    runs TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER
"""


def _source(path: Path, *, table: str = "code_sessions") -> UsageStorageSource:
    return UsageStorageSource(
        path=path,
        path_label=path.name,
        scope="shared_agent",
        expected_session_table=table,
        source_agent_id="code",
        allowed_agent_ids=frozenset({"code"}),
        allowed_team_ids=frozenset({"engineering"}),
        requester_isolated=False,
    )


def _run(*, nested: bool = False) -> dict[str, object]:
    member_responses = (
        [
            {
                "run_id": "member-run",
                "agent_id": "other",
                "metrics": {"total_tokens": 999},
                "content": "nested secret",
            },
        ]
        if nested
        else []
    )
    return {
        "run_id": "run-1",
        "user_id": "@alice:example.test",
        "created_at": 1_723_837_600,
        "model_provider": "openai",
        "model": "gpt-5.6",
        "status": "completed",
        "metrics": {"input_tokens": 12, "output_tokens": 8, "total_tokens": 20},
        "member_responses": member_responses,
        "messages": [{"content": "secret prompt"}],
        "tools": [{"result": "secret output"}],
    }


def _create_database(
    path: Path,
    *,
    table: str = "code_sessions",
    runs: object | None = None,
    session_type: str = "agent",
    agent_id: str | None = "code",
    team_id: str | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute(f'CREATE TABLE "{table}" ({_SESSION_COLUMNS})')
        connection.execute(
            f'INSERT INTO "{table}" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',  # noqa: S608 - fixture identifier.
            (
                "session-1",
                session_type,
                agent_id,
                team_id,
                "@alice:example.test",
                "{}",
                json.dumps([_run()] if runs is None else runs),
                1_723_837_600,
                1_723_837_600,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _config() -> Config:
    return Config(
        agents={
            "code": AgentConfig(display_name="Code", private=AgentPrivateConfig(per="user")),
            "shared": AgentConfig(display_name="Shared"),
        },
        teams={
            "engineering": TeamConfig(display_name="Engineering", role="Team", agents=["shared"]),
        },
    )


def _paths(tmp_path: Path) -> RuntimePaths:
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"MINDROOM_SESSION_STORAGE_PATH": str(tmp_path / "sessions")},
    )


def _identity(requester_id: str) -> ToolExecutionIdentity:
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id=requester_id,
        room_id="!room:example.test",
        thread_id=None,
        resolved_thread_id=None,
        session_id="session",
    )


def _private_database(runtime_paths: RuntimePaths, identity: ToolExecutionIdentity) -> Path:
    worker_key = resolve_worker_key("user", identity, agent_name="code")
    assert worker_key is not None
    return (
        runtime_paths.config_dir
        / "sessions"
        / "private_instances"
        / worker_dir_name(worker_key)
        / "code"
        / "sessions"
        / "code.db"
    )


def test_reader_extracts_only_top_level_usage_fields(tmp_path: Path) -> None:
    """Prompts, tool output, and nested member payloads never enter the typed reader result."""
    database = tmp_path / "code.db"
    _create_database(database, runs=[_run(nested=True)])

    result = list(iter_usage_storage_rows(_source(database)))

    assert len(result) == 1
    row = result[0]
    assert isinstance(row, UsageSessionRow)
    assert len(row.runs) == 1
    assert row.runs[0].metrics == {"input_tokens": 12, "output_tokens": 8, "total_tokens": 20}
    assert row.runs[0].requester_id == "@alice:example.test"
    assert not hasattr(row.runs[0], "member_responses")
    assert "secret" not in repr(row)
    assert "999" not in repr(row)


def test_missing_database_is_reported_without_creation(tmp_path: Path) -> None:
    """A read cannot create an absent SQLite file."""
    database = tmp_path / "missing.db"

    result = list(iter_usage_storage_rows(_source(database)))

    assert result == [
        UsageStorageDiagnostic(path_label="missing.db", status="absent", detail="database absent"),
    ]
    assert not database.exists()


def test_reader_does_not_change_database_bytes(tmp_path: Path) -> None:
    """A successful read leaves the durable database bytes unchanged."""
    database = tmp_path / "code.db"
    _create_database(database)
    before = database.read_bytes()

    result = list(iter_usage_storage_rows(_source(database)))

    assert len(result) == 1
    assert database.read_bytes() == before


def test_malformed_runs_return_content_free_diagnostic(tmp_path: Path) -> None:
    """Malformed JSON produces a stable diagnostic without persisted content."""
    database = tmp_path / "code.db"
    _create_database(database, runs={"prompt": "do not expose"})

    result = list(iter_usage_storage_rows(_source(database)))

    assert result == [
        UsageStorageDiagnostic(
            path_label="code.db",
            status="partial",
            detail="malformed retained session",
        ),
    ]


def test_self_discovery_returns_only_current_private_database(tmp_path: Path) -> None:
    """Private self discovery cannot enumerate another requester's database."""
    config = _config()
    runtime_paths = _paths(tmp_path)
    alice = _identity("@alice:example.test")
    bob = _identity("@bob:example.test")
    alice_database = _private_database(runtime_paths, alice)
    bob_database = _private_database(runtime_paths, bob)
    _create_database(alice_database)
    _create_database(bob_database)

    sources = discover_self_usage_sources(
        agent_name="code",
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=alice,
    )

    assert len(sources) == 1
    source = sources[0]
    assert isinstance(source, UsageStorageSource)
    assert source.path == alice_database.resolve()
    assert source.requester_isolated is True
    assert source.path != bob_database.resolve()


def test_admin_discovery_finds_shared_private_and_team_databases(tmp_path: Path) -> None:
    """Admin discovery covers each supported fixed storage layout."""
    config = _config()
    runtime_paths = _paths(tmp_path)
    root = runtime_paths.config_dir / "sessions"
    private_database = _private_database(runtime_paths, _identity("@alice:example.test"))
    team_database = root / "teams" / "team_engineering_123" / "sessions" / "team_engineering_123.db"
    _create_database(private_database)
    _create_database(
        team_database,
        table="team_engineering_123_sessions",
        session_type="team",
        agent_id=None,
        team_id="engineering",
    )

    sources = discover_admin_usage_sources(config=config, runtime_paths=runtime_paths)
    labels = {source.path_label for source in sources if isinstance(source, UsageStorageSource)}

    assert "agents/shared/sessions/shared.db" in labels
    assert private_database.resolve().relative_to(root.resolve()).as_posix() in labels
    assert team_database.resolve().relative_to(root.resolve()).as_posix() in labels


def test_admin_discovery_reports_source_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A discovery cap must not silently omit inspectable databases."""
    config = _config()
    runtime_paths = _paths(tmp_path)
    private_database = _private_database(runtime_paths, _identity("@alice:example.test"))
    _create_database(private_database)
    monkeypatch.setattr("mindroom.usage_stats_storage._MAX_SOURCES", 1)

    sources = discover_admin_usage_sources(config=config, runtime_paths=runtime_paths)

    assert any(
        isinstance(source, UsageStorageDiagnostic)
        and source.status == "resource_limit"
        and source.detail == "source discovery limit exceeded"
        for source in sources
    )


def test_admin_discovery_reports_directory_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A directory-entry cap must not silently omit private instances."""
    config = _config()
    runtime_paths = _paths(tmp_path)
    _create_database(_private_database(runtime_paths, _identity("@alice:example.test")))
    _create_database(_private_database(runtime_paths, _identity("@bob:example.test")))
    monkeypatch.setattr("mindroom.usage_stats_storage._MAX_DIRECTORY_ENTRIES", 1)

    sources = discover_admin_usage_sources(config=config, runtime_paths=runtime_paths)

    assert any(isinstance(source, UsageStorageDiagnostic) and source.status == "resource_limit" for source in sources)


def test_admin_discovery_reports_candidate_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A private-instance candidate cap must not silently omit databases."""
    config = _config()
    runtime_paths = _paths(tmp_path)
    _create_database(_private_database(runtime_paths, _identity("@alice:example.test")))
    _create_database(_private_database(runtime_paths, _identity("@bob:example.test")))
    monkeypatch.setattr("mindroom.usage_stats_storage._MAX_CANDIDATES", 1)

    sources = discover_admin_usage_sources(config=config, runtime_paths=runtime_paths)

    assert any(isinstance(source, UsageStorageDiagnostic) and source.status == "resource_limit" for source in sources)
