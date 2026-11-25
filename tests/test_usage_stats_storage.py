"""Read-only Agno usage storage adapter."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, get_args

import pytest

from mindroom.config.agent import AgentConfig, AgentPrivateConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.history.types import HistoryScope
from mindroom.llm.runs import RunRecord, RunStatus, TeamRunRecord
from mindroom.llm.usage import HelperUsage, ModelMetrics, RunMetrics, TokenCounts
from mindroom.session_storage import usage_reader
from mindroom.session_storage.agent_storage import (
    create_agent_state_storage,
    create_state_storage,
    prepare_conversation_database,
)
from mindroom.session_storage.schema import LEDGER_TOTALS_VERSION, TableNames, quote_identifier, table_names
from mindroom.session_storage.store import begun, connect_conversation_database
from mindroom.session_storage.types import ConversationSession
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_key, worker_dir_name
from mindroom.usage.stats import collect_admin_usage
from mindroom.usage.stats_storage import (
    UsageRunNode,
    UsageSessionRow,
    UsageStorageDiagnostic,
    UsageStorageSource,
    discover_admin_usage_sources,
    discover_self_usage_sources,
    iter_usage_storage_rows,
)
from mindroom.usage.storage import (
    SYSTEM_USAGE_ENTITY,
    SYSTEM_USAGE_OWNER,
    SYSTEM_USAGE_STORAGE_NAME,
    USAGE_KINDS,
    IndependentUsageKind,
)
from tests.conftest import create_agno_2_sessions_db, persist_runs, persist_session
from tests.session_storage.agno_layout import recorded_owner

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

# Writes inside a transaction until its change reaches the file, then dies, leaving a hot journal beside the database.
_DIE_MID_WRITE = """
import os, sqlite3, sys
connection = sqlite3.connect(sys.argv[1], isolation_level=None)
connection.execute("PRAGMA cache_size = 1")
connection.execute("BEGIN IMMEDIATE")
connection.execute("UPDATE code_sessions SET metadata = ?", ("x" * 200_000,))
os._exit(0)
"""
# An Agno 2 sessions table: its runs blob column, and the metadata and summary columns the conversion reads.
_SESSION_COLUMNS = """
    session_id TEXT PRIMARY KEY,
    session_type TEXT NOT NULL,
    agent_id TEXT,
    team_id TEXT,
    user_id TEXT,
    session_data TEXT,
    runs TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER,
    metadata TEXT,
    summary TEXT
"""
_INSERTED_COLUMNS = "session_id, session_type, agent_id, team_id, user_id, session_data, runs, created_at, updated_at"


def _source(path: Path) -> UsageStorageSource:
    # Existing-history fixtures pass through the same conversion as startup; a read of a missing file creates none.
    if path.is_file():
        prepare_conversation_database(path, TableNames.of_database(path), recorded_owner(path))
    return UsageStorageSource(
        path=path,
        path_label=path.name,
        scope="shared_agent",
        source_agent_id="code",
        allowed_agent_ids=frozenset({"code"}),
        requester_isolated=False,
    )


def _run(*, nested: bool = False, parent_run_id: str | None = None) -> dict[str, object]:
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
    run = {
        "run_id": "run-1",
        "user_id": "@alice:example.test",
        "created_at": 1_723_837_600,
        "model_provider": "openai",
        "model": "gpt-6-astra",
        "status": "completed",
        "metrics": {"input_tokens": 12, "output_tokens": 8, "total_tokens": 20},
        "member_responses": member_responses,
        "messages": [{"content": "secret prompt"}],
        "tools": [{"result": "secret output"}],
    }
    if parent_run_id is not None:
        run["parent_run_id"] = parent_run_id
    return run


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
            f'INSERT INTO "{table}" ({_INSERTED_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',  # noqa: S608 - fixture identifier.
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


def _insert_runs_row(path: Path, *, session_id: str, runs: object) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            f'INSERT INTO "code_sessions" ({_INSERTED_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',  # noqa: S608
            (
                session_id,
                "agent",
                "code",
                None,
                "@alice:example.test",
                "{}",
                json.dumps(runs),
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
    # The run node carries only these typed fields, so the prompt, the tool output, and the nested member's
    # 999-token metrics have nowhere to land.
    assert row.runs == (
        UsageRunNode(
            team_id=None,
            requester_id="@alice:example.test",
            run_id="run-1",
            model_provider="openai",
            model="gpt-6-astra",
            metrics={"input_tokens": 12, "output_tokens": 8, "total_tokens": 20},
            created_at=1_723_837_600,
        ),
    )
    assert row.runs_available is True
    assert row.totals_available is True


def test_reader_extracts_runs_written_by_the_conversation_store(tmp_path: Path) -> None:
    """The adapter reads the exact JSON representation the conversation store writes."""
    storage = create_agent_state_storage("code", tmp_path)
    persist_session(
        storage,
        ConversationSession(
            kind="agent",
            session_id="session-1",
            owner_id="code",
            runs=[
                RunRecord(
                    run_id="run-1",
                    agent_id="code",
                    user_id="@alice:example.test",
                    created_at=1_723_837_600,
                    model_provider="openai",
                    model="gpt-6-astra",
                    status=RunStatus.completed,
                    metrics=RunMetrics(tokens=TokenCounts(input_tokens=12, output_tokens=8, total_tokens=20)),
                ),
            ],
            created_at=1_723_837_600,
        ),
    )

    result = list(iter_usage_storage_rows(_source(tmp_path / "sessions" / "code.db")))

    assert len(result) == 1
    row = result[0]
    assert isinstance(row, UsageSessionRow)
    assert len(row.runs) == 1
    assert row.runs[0].metrics == {"input_tokens": 12, "output_tokens": 8, "total_tokens": 20}
    assert row.runs[0].model_provider == "openai"
    assert row.runs[0].model == "gpt-6-astra"
    assert row.runs[0].created_at == 1_723_837_600


@pytest.mark.parametrize("created_at", [None, True, "invalid", [], float("nan"), float("inf")])
def test_reader_keeps_usage_when_run_timestamp_is_unusable(tmp_path: Path, created_at: object) -> None:
    """Malformed or absent timestamps must not discard usable token metrics."""
    database = tmp_path / "code.db"
    _create_database(database)
    source = _source(database)
    with sqlite3.connect(database) as connection:
        (payload,) = connection.execute("SELECT usage_data FROM code_sessions_usage").fetchone()
        connection.execute(
            "UPDATE code_sessions_usage SET usage_data = ?",
            (json.dumps({**json.loads(payload), "created_at": created_at}),),
        )

    result = list(iter_usage_storage_rows(source))

    assert len(result) == 1
    row = result[0]
    assert isinstance(row, UsageSessionRow)
    assert row.runs[0].created_at is None
    assert row.runs[0].metrics["total_tokens"] == 20


def test_reader_since_skips_older_and_undated_usage_rows(tmp_path: Path) -> None:
    """A start timestamp limits the read to runs last updated at or after it."""
    database = tmp_path / "code.db"
    september = datetime(2026, 9, 29, tzinfo=UTC).timestamp()
    october = datetime(2026, 10, 2, tzinfo=UTC).timestamp()
    _create_database(
        database,
        runs=[
            {**_run(), "run_id": "sept", "created_at": september},
            {**_run(), "run_id": "oct", "created_at": october},
            {**_run(), "run_id": "undated", "created_at": None},
        ],
    )
    source = _source(database)

    since = datetime(2026, 9, 30, tzinfo=UTC).timestamp()
    rows = [row for row in iter_usage_storage_rows(source, since=since) if isinstance(row, UsageSessionRow)]

    assert [run.run_id for row in rows for run in row.runs] == ["oct"]
    assert all(row.totals_available for row in rows)
    unfiltered = [row for row in iter_usage_storage_rows(source) if isinstance(row, UsageSessionRow)]
    assert sorted(run.run_id or "" for row in unfiltered for run in row.runs) == ["oct", "sept", "undated"]


def test_reader_since_skips_sessions_without_recent_usage(tmp_path: Path) -> None:
    """A month scan reads only sessions with usage in range, so its cost follows recent activity."""
    database = tmp_path / "code.db"
    _create_database(
        database,
        runs=[{**_run(), "run_id": "recent", "created_at": datetime(2026, 10, 2, tzinfo=UTC).timestamp()}],
    )
    _insert_runs_row(
        database,
        session_id="old-session",
        runs=[{**_run(), "run_id": "old", "created_at": datetime(2026, 8, 2, tzinfo=UTC).timestamp()}],
    )
    source = _source(database)

    since = datetime(2026, 9, 30, tzinfo=UTC).timestamp()
    rows = [row for row in iter_usage_storage_rows(source, since=since) if isinstance(row, UsageSessionRow)]

    assert [row.row_key for row in rows] == ["session-1"]
    assert len([row for row in iter_usage_storage_rows(source) if isinstance(row, UsageSessionRow)]) == 2


@pytest.mark.parametrize("malformed_payload", ["{not json", None], ids=["invalid-json", "null"])
def test_reader_since_keeps_a_store_readable_beside_a_malformed_snapshot(
    tmp_path: Path,
    malformed_payload: str | None,
) -> None:
    """One corrupt or null usage row must not hide the rest of the store from a dated read."""
    database = tmp_path / "code.db"
    _create_database(
        database,
        runs=[{**_run(), "run_id": "recent", "created_at": datetime(2026, 10, 2, tzinfo=UTC).timestamp()}],
    )
    source = _source(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO code_sessions_usage (session_id, run_id, usage_data) VALUES (?, ?, ?)",
            ("session-1", "broken", malformed_payload),
        )

    rows = list(iter_usage_storage_rows(source, since=datetime(2026, 9, 30, tzinfo=UTC).timestamp()))

    (row,) = rows
    assert isinstance(row, UsageSessionRow)
    assert [run.run_id for run in row.runs] == ["recent"]
    assert row.runs_available is False


def test_reader_preserves_run_table_creation_date_after_updates(tmp_path: Path) -> None:
    """Saving a run again, even with a later creation time, keeps the run's original creation date in the ledger."""
    storage = create_agent_state_storage("code", tmp_path)
    session = ConversationSession(kind="agent", session_id="session-1", owner_id="code")
    run = RunRecord(
        run_id="run-1",
        user_id="@alice:example.test",
        created_at=1_723_837_600,
        model_provider="openai",
        model="gpt-6-astra",
        status=RunStatus.paused,
        metrics=RunMetrics(tokens=TokenCounts(input_tokens=12, output_tokens=8, total_tokens=20)),
    )
    # A paused run saves again when it continues; a finished one never changes.
    persist_runs(storage, session, run)
    persist_runs(storage, session, replace(run, created_at=1_723_924_000, status=RunStatus.completed))

    result = list(iter_usage_storage_rows(_source(tmp_path / "sessions" / "code.db")))

    assert len(result) == 1
    row = result[0]
    assert isinstance(row, UsageSessionRow)
    assert row.runs[0].created_at == 1_723_837_600
    assert row.runs[0].metrics["total_tokens"] == 20


def test_reader_lists_each_member_run_of_a_team_session(tmp_path: Path) -> None:
    """A team's source lists each member run beside its team run, so the session's totals include its members."""
    session = ConversationSession(
        kind="team",
        session_id="session-1",
        owner_id="engineering",
        created_at=1_723_837_600,
    )
    storage = create_state_storage("engineering", tmp_path, HistoryScope(kind="team", scope_id="engineering"))
    # The member saves before its team run, as a team turn saves them.
    persist_runs(
        storage,
        session,
        RunRecord(
            run_id="member-run",
            agent_id="code",
            parent_run_id="team-run",
            metrics=RunMetrics(
                tokens=TokenCounts(input_tokens=14, output_tokens=6, total_tokens=20),
                details={
                    "model": [
                        ModelMetrics(
                            tokens=TokenCounts(input_tokens=14, output_tokens=6, total_tokens=20),
                            id="model-b",
                            provider="provider-b",
                        ),
                    ],
                },
            ),
        ),
    )
    persist_runs(
        storage,
        session,
        TeamRunRecord(
            run_id="team-run",
            team_id="engineering",
            metrics=RunMetrics(
                tokens=TokenCounts(input_tokens=7, output_tokens=3, total_tokens=10),
                details={
                    "model": [
                        ModelMetrics(
                            tokens=TokenCounts(input_tokens=7, output_tokens=3, total_tokens=10),
                            id="model-a",
                            provider="provider-a",
                        ),
                    ],
                },
            ),
        ),
    )

    source = replace(_source(tmp_path / "sessions" / "engineering.db"), scope="team", source_agent_id=None)
    result = list(iter_usage_storage_rows(source))

    assert len(result) == 1
    row = result[0]
    assert isinstance(row, UsageSessionRow)
    assert [(run.run_id, run.parent_run_id, dict(run.metrics)) for run in row.runs] == [
        ("member-run", "team-run", {"input_tokens": 14, "output_tokens": 6, "total_tokens": 20}),
        ("team-run", None, {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}),
    ]
    assert [(entry.model_provider, entry.model) for run in row.runs for entry in run.model_metrics or ()] == [
        ("provider-b", "model-b"),
        ("provider-a", "model-a"),
    ]
    assert (row.runs_available, row.totals_available) == (True, True)


def test_reader_keeps_stored_totals_beside_runs_it_cannot_read(tmp_path: Path) -> None:
    """Runs an earlier release could not decode leave its stored totals as the session's pre-ledger usage."""
    database = tmp_path / "code.db"
    _create_database(database)
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            'UPDATE "code_sessions" SET runs = ?, session_data = ?',
            ("not-json", json.dumps({"session_metrics": {"total_tokens": 30}})),
        )
        connection.commit()
    finally:
        connection.close()

    result = list(iter_usage_storage_rows(_source(database)))

    assert len(result) == 1
    row = result[0]
    assert isinstance(row, UsageSessionRow)
    assert [(run.kind, dict(run.metrics)) for run in row.runs] == [("pre_ledger", {"total_tokens": 30})]
    assert row.runs_available is False
    assert row.totals_available is True


def test_unreadable_session_data_neither_stops_the_read_nor_changes_usage(tmp_path: Path) -> None:
    """Session data an earlier release left unreadable neither stops the read nor changes the session's usage."""
    database = tmp_path / "code.db"
    _create_database(database)
    connection = sqlite3.connect(database)
    try:
        connection.execute('UPDATE "code_sessions" SET session_data = ?', ("not-json",))
        connection.commit()
    finally:
        connection.close()

    result = list(iter_usage_storage_rows(_source(database)))

    assert len(result) == 1
    row = result[0]
    assert isinstance(row, UsageSessionRow)
    assert len(row.runs) == 1
    assert (row.runs_available, row.totals_available) == (True, True)


def test_a_database_below_the_ledger_totals_version_reads_without_whole_history_totals(tmp_path: Path) -> None:
    """Until its conversion keeps its pre-ledger usage as a row, a database's rows total only its dated reads."""
    database = tmp_path / "code.db"
    _create_database(database)
    source = _source(database)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("UPDATE code_sessions_log_meta SET version = ?", (LEDGER_TOTALS_VERSION - 1,))

    [row] = iter_usage_storage_rows(source)
    [dated] = iter_usage_storage_rows(source, since=0)

    assert isinstance(row, UsageSessionRow)
    assert isinstance(dated, UsageSessionRow)
    assert (row.totals_available, dated.totals_available) == (False, True)


def test_reader_excludes_persisted_child_runs(tmp_path: Path) -> None:
    """Delegated sibling runs must not be counted as top-level usage."""
    database = tmp_path / "code.db"
    _create_database(database, runs=[_run(), {**_run(parent_run_id="run-1"), "run_id": "child-run"}])
    source = _source(database)
    with closing(sqlite3.connect(database)) as connection:
        ledger = connection.execute("SELECT run_id FROM code_sessions_usage ORDER BY run_id").fetchall()
    assert ledger == [("child-run",), ("run-1",)]

    result = list(iter_usage_storage_rows(source))

    assert len(result) == 1
    row = result[0]
    assert isinstance(row, UsageSessionRow)
    assert [run.run_id for run in row.runs] == ["run-1"]


def test_reader_returns_every_retained_row(tmp_path: Path) -> None:
    """The parameter-free aggregate can inspect every retained session row."""
    database = tmp_path / "code.db"
    _create_database(database, runs=[_run()])
    _insert_runs_row(database, session_id="session-2", runs=[_run()])

    result = list(iter_usage_storage_rows(_source(database)))

    assert [row.row_key for row in result if isinstance(row, UsageSessionRow)] == ["session-1", "session-2"]


def test_reader_accepts_agno_null_runs(tmp_path: Path) -> None:
    """Agno's JSON null representation means a retained session has no runs."""
    database = tmp_path / "code.db"
    _create_database(database)
    connection = sqlite3.connect(database)
    try:
        connection.execute('UPDATE "code_sessions" SET runs = ?', ("null",))
        connection.commit()
    finally:
        connection.close()

    result = list(iter_usage_storage_rows(_source(database)))

    assert len(result) == 1
    row = result[0]
    assert isinstance(row, UsageSessionRow)
    assert row.runs == ()


@pytest.mark.parametrize("raw_runs", ["", "0"])
def test_reader_rejects_falsey_non_json_list_runs(tmp_path: Path, raw_runs: str) -> None:
    """Empty text and numeric zero cannot masquerade as an empty run list."""
    database = tmp_path / "code.db"
    _create_database(database)
    connection = sqlite3.connect(database)
    try:
        connection.execute('UPDATE "code_sessions" SET runs = ?', (raw_runs,))
        connection.commit()
    finally:
        connection.close()

    result = list(iter_usage_storage_rows(_source(database)))

    assert len(result) == 1
    assert isinstance(result[0], UsageSessionRow)
    assert result[0].runs == ()
    assert result[0].runs_available is False
    assert "do not expose" not in repr(result)


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
    source = _source(database)
    before = database.read_bytes()

    result = list(iter_usage_storage_rows(source))

    assert len(result) == 1
    assert database.read_bytes() == before


def _seed_usage_rows(database: Path, *, session_id: str, rows: list[tuple[str, float]]) -> None:
    with closing(connect_conversation_database(database, timeout=1.0)) as writer, begun(writer, write=True):
        writer.execute("INSERT OR IGNORE INTO code_sessions (session_id, created_at) VALUES (?, 1)", (session_id,))
        writer.executemany(
            "INSERT INTO code_sessions_usage (session_id, run_id, usage_data) VALUES (?, ?, ?)",
            [
                (session_id, run_id, json.dumps({**_run(), "run_id": run_id, "created_at": created_at}))
                for run_id, created_at in rows
            ],
        )


def _intercept_usage_chunks_and_commit_after_first(
    monkeypatch: pytest.MonkeyPatch,
    database: Path,
    usage_table: str,
) -> tuple[list[tuple[int, int]], list[bool]]:
    """Record ``(scanned_rows, matched_rows)`` per usage chunk and commit a second DELETE-mode write after chunk 1."""
    chunks: list[tuple[int, int]] = []
    committed: list[bool] = []
    fetch_chunk = usage_reader._fetch_chunk

    def fetch_then_write(
        connection: sqlite3.Connection,
        query: str,
        parameters: tuple[object, ...],
    ) -> list[tuple[object, ...]]:
        batch = fetch_chunk(connection, query, parameters)
        if usage_table in query:
            chunks.append((len(batch), sum(1 for row in batch if row[3])))
            if len(chunks) == 1:
                with (
                    closing(connect_conversation_database(database, timeout=0.05)) as writer,
                    begun(writer, write=True),
                ):
                    assert writer.execute("PRAGMA journal_mode").fetchone() == ("delete",)
                    writer.execute("INSERT INTO code_sessions (session_id, created_at) VALUES ('session-mid', 2)")
                committed.append(True)
        return batch

    monkeypatch.setattr(usage_reader, "_fetch_chunk", fetch_then_write)
    return chunks, committed


def test_a_writer_commits_between_usage_chunks_without_lock_contention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A writer in DELETE journal mode commits between usage chunks while a scan is in progress."""
    storage = create_agent_state_storage("code", tmp_path)
    total_rows = usage_reader.USAGE_CHUNK_SIZE + 5
    _seed_usage_rows(
        storage.path,
        session_id="session-1",
        rows=[(f"run-{i}", 1_723_837_600.0 + i) for i in range(total_rows)],
    )
    source = _source(storage.path)
    usage_chunks, committed = _intercept_usage_chunks_and_commit_after_first(
        monkeypatch,
        storage.path,
        source.names.usage,
    )

    result = list(iter_usage_storage_rows(source))

    assert usage_chunks == [(usage_reader.USAGE_CHUNK_SIZE, usage_reader.USAGE_CHUNK_SIZE), (5, 5)]
    assert committed == [True]
    (row,) = result
    assert isinstance(row, UsageSessionRow)
    assert row.row_key == "session-1"
    assert len(row.runs) == total_rows
    with closing(sqlite3.connect(storage.path)) as connection:
        assert connection.execute(
            "SELECT session_id FROM code_sessions WHERE session_id = 'session-mid'",
        ).fetchall() == [("session-mid",)]


def test_dated_read_pages_raw_rows_and_lets_writers_commit_across_non_matching_chunks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dated read bounds raw scanned rows per chunk even when early chunks match nothing."""
    storage = create_agent_state_storage("code", tmp_path)
    old_ts = datetime(2026, 8, 1, tzinfo=UTC).timestamp()
    since_ts = datetime(2026, 9, 30, tzinfo=UTC).timestamp()
    recent_ts = datetime(2026, 10, 2, tzinfo=UTC).timestamp()
    old_count = usage_reader.USAGE_CHUNK_SIZE * 2 + 10
    _seed_usage_rows(
        storage.path,
        session_id="session-1",
        rows=[*[(f"old-{i}", old_ts) for i in range(old_count)], ("recent", recent_ts)],
    )
    source = _source(storage.path)
    usage_chunks, committed = _intercept_usage_chunks_and_commit_after_first(
        monkeypatch,
        storage.path,
        source.names.usage,
    )

    result = list(iter_usage_storage_rows(source, since=since_ts))

    assert usage_chunks == [(usage_reader.USAGE_CHUNK_SIZE, 0), (usage_reader.USAGE_CHUNK_SIZE, 0), (11, 1)]
    assert committed == [True]
    (row,) = result
    assert isinstance(row, UsageSessionRow)
    assert [run.run_id for run in row.runs] == ["recent"]
    assert row.runs_available is True


def test_a_caller_holding_a_row_does_not_keep_writers_waiting(tmp_path: Path) -> None:
    """A writer commits while the caller works on a row, and the rows still come from the snapshot before its write."""
    storage = create_agent_state_storage("code", tmp_path)
    for session_id in ("session-1", "session-2"):
        persist_runs(
            storage,
            ConversationSession(kind="agent", session_id=session_id, owner_id="code"),
            RunRecord(
                run_id=f"{session_id}-run",
                user_id="@alice:example.test",
                created_at=1_723_837_600,
                status=RunStatus.completed,
                metrics=RunMetrics(tokens=TokenCounts(total_tokens=20)),
            ),
        )
    source = _source(storage.path)
    rows = iter_usage_storage_rows(source)
    first = next(rows)

    # The store's own write path, with a busy timeout short enough to fail while a reader holds its lock.
    with closing(connect_conversation_database(storage.path, timeout=0.2)) as writer, begun(writer, write=True):
        assert writer.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        writer.execute(f"DELETE FROM {quote_identifier(source.names.sessions)}")  # noqa: S608

    read = [first, *rows]
    assert sorted(row.row_key for row in read if isinstance(row, UsageSessionRow)) == ["session-1", "session-2"]


def test_a_read_leaves_a_crashed_writers_journal_for_the_store_to_roll_back(tmp_path: Path) -> None:
    """A read never rolls back a write a crashed process left half done; the source reads as unavailable until then."""
    database = tmp_path / "code.db"
    _create_database(database)
    source = _source(database)
    subprocess.run([sys.executable, "-I", "-c", _DIE_MID_WRITE, str(database)], check=True)
    journal = database.with_name(f"{database.name}-journal")
    before = (database.read_bytes(), journal.read_bytes())

    result = list(iter_usage_storage_rows(source))

    assert result == [UsageStorageDiagnostic(path_label="code.db", status="partial", detail="database unavailable")]
    assert (database.read_bytes(), journal.read_bytes()) == before


def test_a_database_without_its_sessions_table_is_not_read(tmp_path: Path) -> None:
    """A file that lacks the sessions table its name gives reads as having none, whatever else it holds."""
    database = tmp_path / "code.db"
    # Named before the file exists, so the source reads the file as written, without the conversion an open runs.
    source = _source(database)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE other_sessions (session_id TEXT PRIMARY KEY, user_id TEXT, session_data TEXT)")

    result = list(iter_usage_storage_rows(source))

    assert result == [
        UsageStorageDiagnostic(path_label="code.db", status="unsupported_schema", detail="session table unavailable"),
    ]


def test_a_database_not_named_by_a_plain_identifier_is_not_read(tmp_path: Path) -> None:
    """A source whose file name is not a plain identifier reads as having no sessions table, whatever the file holds."""
    database = tmp_path / "code-x.db"
    _create_database(database, table="code-x_sessions")
    source = _source(database)
    with closing(sqlite3.connect(database)) as connection:
        assert TableNames.of_database(database).sessions in table_names(connection)

    result = list(iter_usage_storage_rows(source))

    assert result == [
        UsageStorageDiagnostic(path_label="code-x.db", status="unsupported_schema", detail="session table unavailable"),
    ]


# The tables a usage read reads, each with only the columns it reads.
_READ_TABLES = {
    "code_sessions": "session_id TEXT PRIMARY KEY, user_id TEXT",
    "code_sessions_usage": "id INTEGER PRIMARY KEY, session_id TEXT, run_id TEXT, usage_data TEXT",
    "code_sessions_log_meta": "id INTEGER PRIMARY KEY, version INTEGER, owner_kind TEXT, owner_id TEXT",
}


@pytest.mark.parametrize(
    ("table", "columns", "detail"),
    [
        ("code_sessions", "session_id TEXT PRIMARY KEY", "session schema unsupported"),
        ("code_sessions_usage", "id INTEGER PRIMARY KEY, session_id TEXT, run_id TEXT", "usage schema unsupported"),
        ("code_sessions_log_meta", "id INTEGER PRIMARY KEY, owner_id TEXT", "session owner unavailable"),
    ],
    ids=["sessions", "usage", "owner"],
)
def test_a_table_missing_a_column_the_read_needs_is_reported(
    tmp_path: Path,
    table: str,
    columns: str,
    detail: str,
) -> None:
    """A table worker code left without a column the read selects is reported as such instead of failing the read."""
    database = tmp_path / "code.db"
    # Named before the file exists, so the source reads the file as written, without the conversion an open runs.
    source = _source(database)
    with closing(sqlite3.connect(database)) as connection, connection:
        for name, definition in {**_READ_TABLES, table: columns}.items():
            connection.execute(f'CREATE TABLE "{name}" ({definition})')
        connection.execute("INSERT INTO code_sessions (session_id) VALUES ('session-1')")
        connection.execute("INSERT INTO code_sessions_log_meta (id, owner_id) VALUES (1, 'code')")

    result = list(iter_usage_storage_rows(source))

    assert result == [UsageStorageDiagnostic(path_label="code.db", status="unsupported_schema", detail=detail)]


@pytest.mark.parametrize(
    ("table", "columns", "insert_sql", "detail"),
    [
        (
            "code_sessions",
            "rowid TEXT, session_id TEXT PRIMARY KEY, user_id TEXT",
            "INSERT INTO code_sessions (rowid, session_id) VALUES ('not-an-int', 'session-1')",
            "session schema unsupported",
        ),
        (
            "code_sessions",
            "rowid INTEGER, session_id TEXT PRIMARY KEY, user_id TEXT",
            "INSERT INTO code_sessions (rowid, session_id) VALUES (1, 'session-1'), (1, 'session-2')",
            "session schema unsupported",
        ),
        (
            "code_sessions",
            "rowid TEXT GENERATED ALWAYS AS ('x') VIRTUAL, session_id TEXT PRIMARY KEY, user_id TEXT",
            "INSERT INTO code_sessions (session_id) VALUES ('session-1')",
            "session schema unsupported",
        ),
        (
            "code_sessions_usage",
            "rowid TEXT, id INTEGER PRIMARY KEY, session_id TEXT, run_id TEXT, usage_data TEXT",
            "INSERT INTO code_sessions_usage (rowid, id, session_id, run_id, usage_data) VALUES ('x', 1, 'session-1', 'r1', '{}')",
            "usage schema unsupported",
        ),
        (
            "code_sessions_usage",
            "OID INTEGER, id INTEGER PRIMARY KEY, session_id TEXT, run_id TEXT, usage_data TEXT",
            "INSERT INTO code_sessions_usage (OID, id, session_id, run_id, usage_data) VALUES (1, 1, 'session-1', 'r1', '{}'), (1, 2, 'session-1', 'r2', '{}')",
            "usage schema unsupported",
        ),
    ],
    ids=[
        "sessions-text-rowid",
        "sessions-duplicate-int-rowid",
        "sessions-generated-rowid",
        "usage-text-rowid",
        "usage-oid-alias",
    ],
)
def test_shadowed_rowid_column_is_reported_as_unsupported_schema(
    tmp_path: Path,
    table: str,
    columns: str,
    insert_sql: str,
    detail: str,
) -> None:
    """Tables declaring a shadowed rowid alias fail with unsupported_schema instead of crashing or skipping rows."""
    database = tmp_path / "code.db"
    source = _source(database)
    with closing(sqlite3.connect(database)) as connection, connection:
        for name, definition in {**_READ_TABLES, table: columns}.items():
            connection.execute(f'CREATE TABLE "{name}" ({definition})')
        connection.execute("INSERT INTO code_sessions_log_meta VALUES (1, 8, 'agent', 'code')")
        if table != "code_sessions":
            connection.execute("INSERT INTO code_sessions (session_id) VALUES ('session-1')")
        connection.execute(insert_sql)

    assert list(iter_usage_storage_rows(source)) == [
        UsageStorageDiagnostic(path_label="code.db", status="unsupported_schema", detail=detail),
    ]


def test_admin_usage_keeps_healthy_agent_beside_shadowed_rowid_database(tmp_path: Path) -> None:
    """A worker-created shadowed rowid table reports a diagnostic without aborting healthy sources."""
    config = Config(
        agents={"broken": AgentConfig(display_name="Broken"), "healthy": AgentConfig(display_name="Healthy")},
    )
    runtime_paths = _paths(tmp_path)
    root = runtime_paths.config_dir / "sessions"
    broken_db = root / "agents" / "broken" / "sessions" / "broken.db"
    broken_db.parent.mkdir(parents=True)
    with closing(sqlite3.connect(broken_db)) as connection, connection:
        connection.execute("CREATE TABLE broken_sessions (rowid TEXT, session_id TEXT PRIMARY KEY, user_id TEXT)")
        connection.execute(
            "CREATE TABLE broken_sessions_log_meta (id INTEGER PRIMARY KEY, version INTEGER, owner_kind TEXT, owner_id TEXT)",
        )
        connection.execute("INSERT INTO broken_sessions_log_meta VALUES (1, 8, 'agent', 'broken')")
        connection.execute("INSERT INTO broken_sessions (rowid, session_id) VALUES ('bad', 'session-1')")

    healthy_storage = create_agent_state_storage("healthy", root / "agents" / "healthy")
    persist_runs(
        healthy_storage,
        ConversationSession(kind="agent", session_id="session-1", owner_id="healthy"),
        RunRecord(
            run_id="run-1",
            user_id="@alice:example.test",
            created_at=1_723_837_600,
            status=RunStatus.completed,
            metrics=RunMetrics(tokens=TokenCounts(input_tokens=12, output_tokens=8, total_tokens=20)),
        ),
    )

    report = collect_admin_usage(config=config, runtime_paths=runtime_paths)

    assert report.totals.total_tokens == 20
    assert [(row.key, row.totals.total_tokens) for row in report.breakdown] == [("healthy", 20)]
    assert (report.coverage.scanned_sources, report.coverage.unavailable_sources) == (2, 1)


@pytest.mark.parametrize(
    ("table", "detail"),
    [
        ("code_sessions", "session schema unsupported"),
        ("code_sessions_usage", "usage schema unsupported"),
    ],
    ids=["sessions", "usage"],
)
def test_without_rowid_table_is_reported_as_unsupported_schema(
    tmp_path: Path,
    table: str,
    detail: str,
) -> None:
    """WITHOUT ROWID tables report unsupported_schema rather than failing later with database unavailable."""
    database = tmp_path / "code.db"
    source = _source(database)
    with closing(sqlite3.connect(database)) as connection, connection:
        for name, definition in _READ_TABLES.items():
            suffix = " WITHOUT ROWID" if name == table else ""
            connection.execute(f'CREATE TABLE "{name}" ({definition}){suffix}')
        connection.execute("INSERT INTO code_sessions (session_id) VALUES ('session-1')")
        connection.execute("INSERT INTO code_sessions_log_meta VALUES (1, 8, 'agent', 'code')")

    assert list(iter_usage_storage_rows(source)) == [
        UsageStorageDiagnostic(path_label="code.db", status="unsupported_schema", detail=detail),
    ]


def test_dated_read_ignores_out_of_range_session_with_invalid_utf8_user_id(tmp_path: Path) -> None:
    """A dated read ignores invalid UTF-8 in out-of-range session user_id and unmatched usage session_id."""
    storage = create_agent_state_storage("code", tmp_path)
    old_ts = datetime(2026, 8, 1, tzinfo=UTC).timestamp()
    since_ts = datetime(2026, 9, 30, tzinfo=UTC).timestamp()
    recent_ts = datetime(2026, 10, 2, tzinfo=UTC).timestamp()
    _seed_usage_rows(storage.path, session_id="old-session", rows=[("old-run", old_ts)])
    _seed_usage_rows(storage.path, session_id="recent-session", rows=[("recent-run", recent_ts)])
    with closing(connect_conversation_database(storage.path, timeout=1.0)) as writer, begun(writer, write=True):
        writer.execute("UPDATE code_sessions SET user_id = CAST(X'80' AS TEXT) WHERE session_id = 'old-session'")
        writer.execute("INSERT INTO code_sessions (session_id, created_at) VALUES (CAST(X'80' AS TEXT), 1)")
        writer.execute(
            "INSERT INTO code_sessions_usage (session_id, run_id, usage_data) VALUES (CAST(X'80' AS TEXT), ?, ?)",
            ("old-bad-sid-run", json.dumps({**_run(), "run_id": "old-bad-sid-run", "created_at": old_ts})),
        )
        writer.execute("UPDATE code_sessions SET user_id = '@alice:example.test' WHERE session_id = 'recent-session'")
        assert writer.execute(
            "SELECT typeof(user_id), hex(CAST(user_id AS BLOB)) FROM code_sessions WHERE session_id = 'old-session'",
        ).fetchone() == ("text", "80")
        assert writer.execute(
            "SELECT typeof(session_id), hex(CAST(session_id AS BLOB)) FROM code_sessions_usage WHERE run_id = 'old-bad-sid-run'",
        ).fetchone() == ("text", "80")

    (row,) = iter_usage_storage_rows(_source(storage.path), since=since_ts)

    assert isinstance(row, UsageSessionRow)
    assert row.row_key == "recent-session"
    assert row.requester_id == "@alice:example.test"
    assert [run.run_id for run in row.runs] == ["recent-run"]


@pytest.mark.parametrize("since", [None, 0.0], ids=["undated", "dated"])
def test_a_database_without_a_usage_ledger_reads_its_sessions_without_runs(tmp_path: Path, since: float | None) -> None:
    """Without a ledger every session is read, a dated read too, and its runs are reported unavailable."""
    database = tmp_path / "code.db"
    # Named before the file exists, so the source reads the file as written, without the conversion an open runs.
    source = _source(database)
    with closing(sqlite3.connect(database)) as connection, connection:
        for name in ("code_sessions", "code_sessions_log_meta"):
            connection.execute(f'CREATE TABLE "{name}" ({_READ_TABLES[name]})')
        connection.execute("INSERT INTO code_sessions (session_id) VALUES ('session-1')")
        connection.execute("INSERT INTO code_sessions_log_meta VALUES (1, 6, 'agent', 'code')")

    [row] = iter_usage_storage_rows(source, since=since)

    assert isinstance(row, UsageSessionRow)
    assert (row.row_key, row.runs, row.runs_available) == ("session-1", (), False)


def test_a_system_source_another_owner_owns_reads_as_malformed(tmp_path: Path) -> None:
    """The system scope reads only rows of the database the system usage ledger owns, whatever the rows say."""
    storage = create_state_storage(
        SYSTEM_USAGE_STORAGE_NAME,
        tmp_path / SYSTEM_USAGE_STORAGE_NAME,
        HistoryScope(kind="agent", scope_id="code"),
    )
    storage.save_independent_usage(
        session_id="routing",
        usage_id="routing:1",
        kind="routing",
        requester_id=None,
        usage=HelperUsage(run_id="1", model=None, provider=None, metrics=RunMetrics()),
        initial_session=ConversationSession(session_id="routing", kind="agent", owner_id=SYSTEM_USAGE_ENTITY),
    )
    source = UsageStorageSource(
        path=storage.path,
        path_label=storage.path.name,
        scope="system",
        source_agent_id=None,
        allowed_agent_ids=frozenset(),
        requester_isolated=False,
    )

    [result] = iter_usage_storage_rows(source)

    assert isinstance(result, UsageStorageDiagnostic)
    assert (result.status, result.detail) == ("partial", "malformed retained session")


def test_malformed_runs_return_content_free_diagnostic(tmp_path: Path) -> None:
    """Malformed JSON produces a stable diagnostic without persisted content."""
    database = tmp_path / "code.db"
    _create_database(database, runs={"prompt": "do not expose"})

    result = list(iter_usage_storage_rows(_source(database)))

    assert len(result) == 1
    assert isinstance(result[0], UsageSessionRow)
    assert result[0].runs == ()
    assert result[0].runs_available is False
    assert "do not expose" not in repr(result)


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
    isolation = {
        source.scope: source.requester_isolated for source in sources if isinstance(source, UsageStorageSource)
    }
    assert isolation == {"shared_agent": False, "private_agent": True, "team": False}


@pytest.mark.parametrize("link_level", [0, 1, 2], ids=["database", "sessions", "team"])
def test_admin_discovery_reports_rejected_team_symlinks(tmp_path: Path, link_level: int) -> None:
    """Rejected team paths remain visible in coverage without hiding valid neighbors."""
    runtime_paths = _paths(tmp_path)
    root = runtime_paths.config_dir / "sessions"
    rejected_database = root / "teams" / "team_rejected" / "sessions" / "team_rejected.db"
    valid_database = root / "teams" / "team_valid" / "sessions" / "team_valid.db"
    for database in (rejected_database, valid_database):
        database.parent.mkdir(parents=True)
        database.touch()
    rejected_path = (rejected_database, rejected_database.parent, rejected_database.parent.parent)[link_level]
    target = rejected_path.rename(tmp_path / "outside-team-source")
    rejected_path.symlink_to(target, target_is_directory=link_level != 0)

    sources = discover_admin_usage_sources(config=_config(), runtime_paths=runtime_paths)

    assert (
        UsageStorageDiagnostic(
            path_label=rejected_database.relative_to(root).as_posix(),
            status="partial",
            detail="source discovery unavailable",
            scope="team",
        )
        in sources
    )
    team_paths = {
        source.path for source in sources if isinstance(source, UsageStorageSource) and source.scope == "team"
    }
    assert team_paths == {valid_database.resolve()}


def test_admin_discovery_reports_directory_read_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreadable storage directory must not look absent."""
    runtime_paths = _paths(tmp_path)
    private_root = runtime_paths.config_dir / "sessions" / "private_instances"
    private_root.mkdir(parents=True)
    original_iterdir = private_root.__class__.iterdir

    def fail_private_directory(path: Path) -> Iterator[Path]:
        if path == private_root:
            raise OSError
        return original_iterdir(path)

    monkeypatch.setattr(private_root.__class__, "iterdir", fail_private_directory)

    sources = discover_admin_usage_sources(config=_config(), runtime_paths=runtime_paths)

    assert any(
        isinstance(source, UsageStorageDiagnostic)
        and source.status == "partial"
        and source.detail == "source discovery unavailable"
        for source in sources
    )


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
@pytest.mark.parametrize("blocked", ["agents/broken", "agents/broken/sessions"])
def test_admin_discovery_reports_unsearchable_shared_agent_directory(tmp_path: Path, blocked: str) -> None:
    """One unsearchable agent directory becomes a coverage gap instead of failing discovery for every source."""
    config = Config(agents={"broken": AgentConfig(display_name="Broken"), "shared": AgentConfig(display_name="Shared")})
    runtime_paths = _paths(tmp_path)
    root = runtime_paths.config_dir / "sessions"
    (root / "agents" / "broken" / "sessions").mkdir(parents=True)
    blocked_directory = root / blocked
    blocked_directory.chmod(0)
    try:
        sources = discover_admin_usage_sources(config=config, runtime_paths=runtime_paths)
    finally:
        blocked_directory.chmod(0o755)

    assert (
        UsageStorageDiagnostic(
            path_label="agents/broken/sessions/broken.db",
            status="partial",
            detail="source discovery unavailable",
            scope="shared_agent",
        )
        in sources
    )
    assert [source.path_label for source in sources if isinstance(source, UsageStorageSource)] == [
        "agents/shared/sessions/shared.db",
    ]


def test_reader_merges_legacy_blob_with_runs_table(tmp_path: Path) -> None:
    """Run-table rows win on run_id; legacy-only runs are appended, matching Agno's read merge."""
    database = create_agno_2_sessions_db(tmp_path / "code.db")
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "CREATE TABLE code_sessions_runs (run_id TEXT PRIMARY KEY, session_id TEXT, run_type TEXT, "
            "agent_id TEXT, team_id TEXT, workflow_id TEXT, user_id TEXT, parent_run_id TEXT, status TEXT, "
            "run_index INTEGER, run_data TEXT, created_at INTEGER NOT NULL, updated_at INTEGER)",
        )
        connection.execute(
            "INSERT INTO code_sessions_runs (run_id, session_id, run_type, run_index, run_data, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("run-1", "session-1", "agent", 0, json.dumps({**_run(), "metrics": {"total_tokens": 99}}), 1),
        )
        connection.commit()
    finally:
        connection.close()

    result = list(iter_usage_storage_rows(_source(database)))

    assert len(result) == 1
    row = result[0]
    assert isinstance(row, UsageSessionRow)
    assert [run.run_id for run in row.runs] == ["run-1", "run-2", "run-3"]
    assert row.runs[0].metrics == {"total_tokens": 99}
    assert row.runs[1].metrics == {"input_tokens": 2, "output_tokens": 2, "total_tokens": 4}


def test_retired_schedule_parse_usage_rows_still_read_and_aggregate(tmp_path: Path) -> None:
    """Historical `schedule_parse` helper rows in `system.db` still load and aggregate cleanly."""
    assert "schedule_parse" not in get_args(IndependentUsageKind.__value__)
    assert "schedule_parse" in USAGE_KINDS

    paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={},
    )
    database = paths.storage_root / SYSTEM_USAGE_STORAGE_NAME / "sessions" / f"{SYSTEM_USAGE_STORAGE_NAME}.db"
    database.parent.mkdir(parents=True, exist_ok=True)
    names = TableNames(SYSTEM_USAGE_STORAGE_NAME)
    prepare_conversation_database(database, names, owner=SYSTEM_USAGE_OWNER)
    run_payload = json.dumps(
        {
            "run_id": "schedule_parse:legacy-1",
            "parent_run_id": None,
            "team_id": None,
            "user_id": None,
            "model_provider": "OpenAI",
            "model": "gpt-test",
            "created_at": 1790000000,
            "metrics": {
                "input_tokens": 20,
                "output_tokens": 5,
                "total_tokens": 25,
                "details": {
                    "model": [
                        {
                            "input_tokens": 20,
                            "output_tokens": 5,
                            "total_tokens": 25,
                            "id": "gpt-test",
                            "provider": "OpenAI",
                        },
                    ],
                },
            },
            "requests": [
                {
                    "created_at": 1790000000,
                    "metrics": {"input_tokens": 20, "output_tokens": 5, "total_tokens": 25},
                    "model": "gpt-test",
                    "model_provider": "OpenAI",
                },
            ],
            "kind": "schedule_parse",
        },
    )
    with closing(connect_conversation_database(database)) as connection, begun(connection, write=True):
        connection.execute(
            f"INSERT INTO {quote_identifier(names.sessions)} "  # noqa: S608
            "(session_id, created_at, updated_at) VALUES (?, ?, ?)",
            ("schedule_parse", 1790000000, 1790000000),
        )
        connection.execute(
            f"INSERT INTO {quote_identifier(names.usage)} "  # noqa: S608
            "(session_id, run_id, usage_data) VALUES (?, ?, ?)",
            ("schedule_parse", "schedule_parse:legacy-1", run_payload),
        )

    report = collect_admin_usage(config=Config(), runtime_paths=paths, include_requests=True)
    assert report.totals.total_tokens == 25
    assert report.totals.input_tokens == 20
    assert report.totals.output_tokens == 5
    assert report.request_breakdown is not None
    assert [(row.kind, row.totals.total_tokens) for row in report.request_breakdown] == [("schedule_parse", 25)]
