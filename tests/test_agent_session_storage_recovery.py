"""Session schema recovery preserves old rows and unrelated agent state."""

from __future__ import annotations

import sqlite3
import stat
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from typing import TYPE_CHECKING

import pytest
from agno.db.base import SessionType
from agno.run.agent import RunOutput
from agno.run.team import TeamRunOutput
from agno.session.agent import AgentSession
from agno.session.team import TeamSession
from sqlalchemy import text

from mindroom.agent_storage import create_state_storage
from tests.conftest import create_agno_2_sessions_db

if TYPE_CHECKING:
    from pathlib import Path

    from agno.db.base import BaseDb


def _storage(state_root: Path, *, subdir: str = "sessions") -> BaseDb:
    return create_state_storage("general", state_root, subdir=subdir, session_table="general_sessions")


def _old_database(state_root: Path, *, subdir: str = "sessions") -> Path:
    directory = state_root / subdir
    directory.mkdir(parents=True)
    database = directory / "general.db"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE general_sessions (session_id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO general_sessions VALUES ('old-session')")
        connection.commit()
    return database


def _archived_database(state_root: Path) -> Path:
    archives = list(state_root.glob("sessions.incompatible-*"))
    assert len(archives) == 1
    return archives[0] / "general.db"


def _assert_old_row(database: Path) -> None:
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        assert connection.execute("SELECT session_id FROM general_sessions").fetchall() == [("old-session",)]


@pytest.mark.parametrize("session_type", [SessionType.AGENT, SessionType.TEAM])
def test_incompatible_session_schema_is_archived_before_real_session_use(
    tmp_path: Path,
    session_type: SessionType,
) -> None:
    """Returning an unopened Agno object cannot conceal an unusable session schema."""
    state_root = tmp_path / "agents" / "general"
    _old_database(state_root)
    (state_root / "sessions").chmod(0o700)
    untouched = {}
    for name in ("learning/data", "workspace/notes.md", "credentials/key", "encryption_keys/key", "culture/data"):
        path = state_root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name)
        untouched[path] = path.read_bytes()
    older_archive = state_root / "sessions.incompatible-retained"
    older_archive.mkdir()
    (older_archive / "retained").write_text("keep this archive")
    session = (
        AgentSession(session_id="new-session", agent_id="general", created_at=1)
        if session_type is SessionType.AGENT
        else TeamSession(session_id="new-session", team_id="general", created_at=1)
    )

    with closing(_storage(state_root)) as storage:
        assert storage.upsert_session(session) is not None
        run = (
            RunOutput(run_id="new-run", content="saved answer", agent_id="general")
            if session_type is SessionType.AGENT
            else TeamRunOutput(run_id="new-run", content="saved answer", team_id="general")
        )
        storage.upsert_run(run, session.session_id)
        if session_type is SessionType.TEAM:
            storage.upsert_run(
                RunOutput(run_id="member-run", parent_run_id="new-run", agent_id="member", content="member answer"),
                session.session_id,
            )

    with closing(_storage(state_root)) as storage:
        loaded = storage.get_session(session.session_id, session_type)
        assert isinstance(loaded, (AgentSession, TeamSession))
        expected_runs = [("new-run", "saved answer")]
        if session_type is SessionType.TEAM:
            expected_runs.append(("member-run", "member answer"))
        assert [(run.run_id, run.content) for run in loaded.runs or []] == expected_runs
        with storage.db_engine.connect() as connection:
            assert connection.execute(text("PRAGMA journal_mode")).scalar() == "delete"
            assert connection.execute(text("PRAGMA foreign_keys")).scalar() == 1
            assert connection.execute(text("PRAGMA busy_timeout")).scalar() == 30_000
    with closing(sqlite3.connect(state_root / "sessions" / "general.db")) as connection:
        assert connection.execute("SELECT run_id FROM general_sessions_runs ORDER BY run_id").fetchall() == (
            [("new-run",)] if session_type is SessionType.AGENT else [("member-run",), ("new-run",)]
        )

    archives = [path for path in state_root.glob("sessions.incompatible-*") if path != older_archive]
    assert len(archives) == 1
    _assert_old_row(archives[0] / "general.db")
    assert stat.S_IMODE(archives[0].stat().st_mode) == 0o700
    assert stat.S_IMODE((state_root / "sessions").stat().st_mode) == 0o700
    assert (older_archive / "retained").read_text() == "keep this archive"
    assert {path: path.read_bytes() for path in untouched} == untouched


def test_compatible_session_reopen_preserves_history_and_extra_columns(tmp_path: Path) -> None:
    """A compatible database is never archived simply because it already exists."""
    with closing(_storage(tmp_path)) as storage:
        assert storage.upsert_session(AgentSession(session_id="old-session", agent_id="general", created_at=1))
        storage.upsert_run(
            RunOutput(run_id="saved-run", content="saved answer", agent_id="general", session_id="old-session"),
            "old-session",
        )
    database = tmp_path / "sessions" / "general.db"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("ALTER TABLE general_sessions ADD COLUMN extra TEXT")
        connection.execute("UPDATE general_sessions SET extra = 'keep extra'")
        connection.execute("CREATE TABLE unrelated_old_sessions (session_id TEXT)")
        connection.execute("INSERT INTO unrelated_old_sessions VALUES ('unrelated')")
        connection.commit()

    with closing(_storage(tmp_path)) as storage:
        loaded = storage.get_session("old-session", SessionType.AGENT)
        assert isinstance(loaded, AgentSession)
        assert [(run.run_id, run.content) for run in loaded.runs or []] == [("saved-run", "saved answer")]

    assert not list(tmp_path.glob("sessions.incompatible-*"))
    _assert_old_row(database)
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT extra FROM general_sessions").fetchall() == [("keep extra",)]
        assert connection.execute("SELECT * FROM unrelated_old_sessions").fetchall() == [("unrelated",)]


def test_missing_session_table_does_not_archive_other_tables(tmp_path: Path) -> None:
    """An absent session table remains Agno's normal lazy initialization case."""
    database = _old_database(tmp_path)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("ALTER TABLE general_sessions RENAME TO other_sessions")
        connection.commit()

    with closing(_storage(tmp_path)) as storage:
        assert storage.upsert_session(AgentSession(session_id="new-session", agent_id="general", created_at=1))

    assert not list(tmp_path.glob("sessions.incompatible-*"))
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT session_id FROM other_sessions").fetchall() == [("old-session",)]


def test_concurrent_constructors_recover_once_and_share_current_store(tmp_path: Path) -> None:
    """Two arriving constructors must not each archive a freshly recovered directory."""
    _old_database(tmp_path)
    barrier = threading.Barrier(2)

    def construct() -> BaseDb:
        barrier.wait(timeout=10)
        return _storage(tmp_path)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(construct) for _ in range(2)]
        first, second = [future.result(timeout=10) for future in futures]
    with closing(first), closing(second):
        assert first.upsert_session(AgentSession(session_id="first", agent_id="general", created_at=1))
        assert second.get_session("first", SessionType.AGENT) is not None
        assert second.upsert_session(AgentSession(session_id="second", agent_id="general", created_at=1))
        assert first.get_session("second", SessionType.AGENT) is not None
    _assert_old_row(_archived_database(tmp_path))


@pytest.mark.parametrize("subdir", ["learning", "custom"])
def test_non_session_state_does_not_archive_incompatible_schema(tmp_path: Path, subdir: str) -> None:
    """Session recovery must never consume learning or caller-defined state."""
    database = _old_database(tmp_path, subdir=subdir)
    before = database.read_bytes()

    with closing(_storage(tmp_path, subdir=subdir)):
        pass

    assert database.read_bytes() == before
    assert list(tmp_path.iterdir()) == [database.parent]


def _crash_writer(database: Path, *, wal: bool = False) -> None:
    code = """
import os
import sqlite3
import sys

connection = sqlite3.connect(sys.argv[1])
if sys.argv[2] == "wal":
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("INSERT INTO general_sessions VALUES ('old-session')")
    connection.commit()
else:
    connection.execute("PRAGMA cache_size=8")
    connection.execute("BEGIN IMMEDIATE")
    connection.execute("UPDATE spill SET payload = ?", ("uncommitted-" + "B" * 2048,))
os._exit(0)
"""
    subprocess.run(
        ["uv", "run", "--no-sync", "python", "-c", code, str(database), "wal" if wal else "rollback"],
        check=True,
        timeout=30,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("compatible", [True, False])
def test_hot_rollback_recovers_committed_state_before_schema_decision(tmp_path: Path, *, compatible: bool) -> None:
    """A hot journal needs native rollback before schema inspection can classify it."""
    if compatible:
        with closing(_storage(tmp_path)) as storage:
            assert storage.upsert_session(AgentSession(session_id="old-session", agent_id="general", created_at=1))
            storage.upsert_run(
                RunOutput(
                    run_id="committed-run",
                    content="committed answer",
                    agent_id="general",
                    session_id="old-session",
                ),
                "old-session",
            )
        database = tmp_path / "sessions" / "general.db"
    else:
        database = _old_database(tmp_path)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE spill (payload TEXT)")
        connection.executemany("INSERT INTO spill VALUES (?)", [("committed-" + "A" * 2048,)] * 400)
        connection.commit()
    _crash_writer(database)

    assert database.with_suffix(".db-journal").is_file()
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        with pytest.raises(sqlite3.OperationalError) as raised:
            connection.execute("SELECT name FROM sqlite_schema").fetchall()
        assert raised.value.sqlite_errorcode == sqlite3.SQLITE_READONLY_ROLLBACK

    with closing(_storage(tmp_path)) as storage:
        loaded = storage.get_session("old-session", SessionType.AGENT)
        assert (loaded is not None) is compatible
        if compatible:
            assert isinstance(loaded, AgentSession)
            assert [(run.run_id, run.content) for run in loaded.runs or []] == [("committed-run", "committed answer")]
        assert storage.upsert_session(AgentSession(session_id="new-session", agent_id="general", created_at=2))
        assert storage.get_session("new-session", SessionType.AGENT) is not None

    retained_database = database if compatible else _archived_database(tmp_path)
    with closing(sqlite3.connect(retained_database.as_uri() + "?mode=ro", uri=True)) as connection:
        assert connection.execute("SELECT count(*) FROM spill WHERE payload LIKE 'committed-%'").fetchone() == (400,)
    if compatible:
        assert not list(tmp_path.glob("sessions.incompatible-*"))
    else:
        _assert_old_row(retained_database)


def test_wal_only_committed_rows_survive_archive(tmp_path: Path) -> None:
    """Archiving just the main database would lose this committed session."""
    database = _old_database(tmp_path)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("DELETE FROM general_sessions")
        connection.commit()
    _crash_writer(database, wal=True)
    before = {suffix: database.with_suffix(suffix).read_bytes() for suffix in (".db", ".db-wal")}
    assert database.with_suffix(".db-shm").is_file()

    with closing(_storage(tmp_path)) as storage:
        assert storage.upsert_session(AgentSession(session_id="new-session", agent_id="general", created_at=1))
        assert storage.get_session("new-session", SessionType.AGENT) is not None

    archived = _archived_database(tmp_path)
    assert {suffix: archived.with_suffix(suffix).read_bytes() for suffix in before} == before
    assert archived.with_suffix(".db-shm").is_file()
    _assert_old_row(archived)


def test_malformed_database_never_authorizes_archive(tmp_path: Path) -> None:
    """Malformed bytes are a storage error, not evidence of missing schema columns."""
    database = _old_database(tmp_path)
    database.write_bytes(b"not a SQLite database")

    with pytest.raises(sqlite3.DatabaseError) as raised:
        _storage(tmp_path)

    assert raised.value.sqlite_errorcode == sqlite3.SQLITE_NOTADB
    assert database.read_bytes() == b"not a SQLite database"
    assert not list(tmp_path.glob("sessions.incompatible-*"))


@pytest.mark.parametrize(
    "error",
    [
        PermissionError("session probe permission denied"),
        sqlite3.SQLITE_IOERR,
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_READONLY,
        sqlite3.SQLITE_READONLY_RECOVERY,
    ],
)
def test_probe_errors_leave_original_files_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: PermissionError | int,
) -> None:
    """Failure to inspect a store must not be converted into reset permission."""
    database = _old_database(tmp_path)
    before = database.read_bytes()
    failure: PermissionError | sqlite3.OperationalError
    if isinstance(error, int):
        failure = sqlite3.OperationalError("session probe failed")
        failure.sqlite_errorcode = error
    else:
        failure = error
    original_connect = sqlite3.connect

    def fail_connect(database: str, *, uri: bool, timeout: float) -> sqlite3.Connection:
        if database.endswith("?mode=ro"):
            raise failure
        return original_connect(database, uri=uri, timeout=timeout)

    monkeypatch.setattr(sqlite3, "connect", fail_connect)
    with pytest.raises(type(failure), match="session probe"):
        _storage(tmp_path)

    assert database.read_bytes() == before
    assert not list(tmp_path.glob("sessions.incompatible-*"))


@pytest.mark.parametrize("link_directory", [True, False])
def test_session_recovery_refuses_symlinked_storage(
    tmp_path: Path,
    *,
    link_directory: bool,
) -> None:
    """A sessions-shaped symlink does not grant ownership of its target."""
    external_database = _old_database(tmp_path / "external")
    before = external_database.read_bytes()
    state_root = tmp_path / "owner"
    state_root.mkdir()
    if link_directory:
        (state_root / "sessions").symlink_to(external_database.parent, target_is_directory=True)
    else:
        (state_root / "sessions").mkdir()
        (state_root / "sessions" / "general.db").symlink_to(external_database)

    with pytest.raises(ValueError, match="session"):
        _storage(state_root)

    assert external_database.read_bytes() == before
    assert not list(state_root.glob("sessions.incompatible-*"))


@pytest.mark.parametrize("null_blob", [False, True])
def test_compatible_old_fixture_and_current_run_rows_survive_reopen(tmp_path: Path, *, null_blob: bool) -> None:
    """Old versions and optional legacy blobs do not justify discarding compatible history."""
    database = create_agno_2_sessions_db(tmp_path / "sessions" / "code.db")
    with closing(sqlite3.connect(database)) as connection:
        if null_blob:
            connection.execute("UPDATE code_sessions SET runs = NULL")
            connection.commit()
        original_blob = connection.execute("SELECT runs FROM code_sessions").fetchone()

    def open_storage() -> BaseDb:
        return create_state_storage("code", tmp_path, subdir="sessions", session_table="code_sessions")

    with closing(open_storage()) as storage:
        storage.upsert_run(
            RunOutput(run_id="row-only", content="new answer", agent_id="code", session_id="session-1"),
            "session-1",
        )
        assert storage.upsert_session(AgentSession(session_id="current-session", agent_id="code", created_at=2))
        storage.upsert_run(
            RunOutput(run_id="current-run", content="current answer", agent_id="code", session_id="current-session"),
            "current-session",
        )

    with closing(open_storage()) as storage:
        loaded = storage.get_session("session-1", SessionType.AGENT)
        assert isinstance(loaded, AgentSession)
        expected_ids = {"row-only"} if null_blob else {"run-1", "run-2", "run-3", "row-only"}
        assert {run.run_id for run in loaded.runs or []} == expected_ids
        assert next(run.content for run in loaded.runs or [] if run.run_id == "row-only") == "new answer"
        current = storage.get_session("current-session", SessionType.AGENT)
        assert isinstance(current, AgentSession)
        assert [(run.run_id, run.content) for run in current.runs or []] == [("current-run", "current answer")]
    with closing(sqlite3.connect(database)) as connection:
        assert (
            connection.execute("SELECT runs FROM code_sessions WHERE session_id = 'session-1'").fetchone()
            == original_blob
        )
        assert connection.execute(
            "SELECT version FROM agno_schema_versions WHERE table_name = 'code_sessions'",
        ).fetchone() == ("2.5.6",)
        assert connection.execute("SELECT run_id FROM code_sessions_runs ORDER BY run_id").fetchall() == [
            ("current-run",),
            ("row-only",),
        ]
    assert not list(tmp_path.glob("sessions.incompatible-*"))


def test_missing_runs_table_stays_lazy(tmp_path: Path) -> None:
    """A session written before its first run must not be archived on reopen."""
    with closing(_storage(tmp_path)) as storage:
        assert storage.upsert_session(AgentSession(session_id="waiting", agent_id="general", created_at=1))
    database = tmp_path / "sessions" / "general.db"
    with closing(sqlite3.connect(database)) as connection:
        assert (
            connection.execute("SELECT name FROM sqlite_schema WHERE name = 'general_sessions_runs'").fetchone() is None
        )
    with closing(_storage(tmp_path)) as storage:
        storage.upsert_run(
            RunOutput(run_id="first-run", content="first answer", agent_id="general", session_id="waiting"),
            "waiting",
        )
        loaded = storage.get_session("waiting", SessionType.AGENT)
        assert isinstance(loaded, AgentSession)
        assert [(run.run_id, run.content) for run in loaded.runs or []] == [("first-run", "first answer")]
    assert not list(tmp_path.glob("sessions.incompatible-*"))


def test_session_recovery_preserves_real_learning_records(tmp_path: Path) -> None:
    """Archiving sessions leaves independent learning rows byte-for-byte intact."""
    with closing(_storage(tmp_path, subdir="learning")) as storage:
        storage.upsert_learning(id="preference", learning_type="user_profile", content={"style": "concise"})
    learning_file = tmp_path / "learning" / "general.db"
    before = learning_file.read_bytes()
    _old_database(tmp_path)
    with closing(_storage(tmp_path)) as storage:
        assert storage.upsert_session(AgentSession(session_id="new-session", agent_id="general", created_at=1))
    with closing(_storage(tmp_path, subdir="learning")) as storage:
        learning = storage.get_learning_by_id("preference")
        assert learning is not None
        assert learning["content"] == {"style": "concise"}
    assert learning_file.read_bytes() == before
    _assert_old_row(_archived_database(tmp_path))
