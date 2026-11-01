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
from typing import TYPE_CHECKING

import pytest

from mindroom.config.agent import AgentConfig, AgentPrivateConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.history.types import HistoryScope
from mindroom.llm.runs import RunRecord, RunStatus, TeamRunRecord
from mindroom.llm.usage import HelperUsage, ModelMetrics, RunMetrics, TokenCounts
from mindroom.session_storage.agent_storage import (
    create_agent_state_storage,
    create_state_storage,
    prepare_conversation_database,
)
from mindroom.session_storage.schema import LEDGER_TOTALS_VERSION, TableNames, quote_identifier, table_names
from mindroom.session_storage.store import begun, connect_conversation_database
from mindroom.session_storage.types import ConversationSession
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_key, worker_dir_name
from mindroom.usage import stats_storage
from mindroom.usage.stats_storage import (
    UsageRunNode,
    UsageSessionRow,
    UsageStorageDiagnostic,
    UsageStorageSource,
    discover_admin_usage_sources,
    discover_self_usage_sources,
    iter_usage_storage_rows,
)
from mindroom.usage.storage import SYSTEM_USAGE_ENTITY, SYSTEM_USAGE_STORAGE_NAME
from tests.conftest import create_agno_2_sessions_db, persist_runs, persist_session
from tests.session_storage.agno_layout import recorded_owner

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mindroom.session_storage.usage_reader import UnreadableLayout

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


def test_reader_since_keeps_a_store_readable_beside_a_malformed_snapshot(tmp_path: Path) -> None:
    """One corrupt usage row must not hide the rest of the store from a dated read."""
    database = tmp_path / "code.db"
    _create_database(
        database,
        runs=[{**_run(), "run_id": "recent", "created_at": datetime(2026, 10, 2, tzinfo=UTC).timestamp()}],
    )
    source = _source(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO code_sessions_usage (session_id, run_id, usage_data) VALUES (?, ?, ?)",
            ("session-1", "broken", "{not json"),
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


def test_a_read_is_one_snapshot_from_its_layout_check_to_its_last_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A writer that commits after the read checked the layout waits for the read, which never sees its change."""
    database = tmp_path / "code.db"
    _create_database(database)
    source = _source(database)
    writes: list[str] = []
    check_layout = stats_storage.unreadable_layout

    def check_then_write(connection: sqlite3.Connection, names: TableNames) -> UnreadableLayout | None:
        problem = check_layout(connection, names)
        with closing(sqlite3.connect(database, timeout=0.05)) as writer:
            try:
                with writer:
                    writer.execute("INSERT INTO code_sessions (session_id, created_at) VALUES ('session-2', 1)")
                writes.append("committed")
            except sqlite3.OperationalError as error:
                writes.append(str(error))
        return problem

    monkeypatch.setattr(stats_storage, "unreadable_layout", check_then_write)

    result = list(iter_usage_storage_rows(source))

    assert [row.row_key for row in result if isinstance(row, UsageSessionRow)] == ["session-1"]
    assert writes == ["database is locked"]


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
