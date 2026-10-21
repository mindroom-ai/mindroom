"""History compacted before the archive existed becomes summary rows once, when its conversation database opens."""

from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from typing import TYPE_CHECKING

import pytest
from agno.db.sqlite import SqliteDb
from agno.run.agent import RunOutput
from agno.session.agent import AgentSession
from agno.session.summary import SessionSummary
from agno.session.team import TeamSession

from mindroom.constants import MINDROOM_COMPACTION_METADATA_KEY, MINDROOM_MATRIX_HISTORY_METADATA_KEY
from mindroom.history import legacy_compaction_state, summary_rows
from mindroom.history.legacy_compaction_state import _legacy_tombstones, migrate_to_summary_rows
from mindroom.history.types import HistoryScope
from mindroom.session_storage.agent_storage import create_state_storage, get_agent_session, get_team_session
from mindroom.session_storage.schema import TableNames
from mindroom.session_storage.store import open_conversation_store
from tests.conftest import create_agno_2_sessions_db, seed_session
from tests.history_helpers import StoredSummaryRow, stored_run_ids, summary_rows_of

if TYPE_CHECKING:
    from pathlib import Path

_SCOPE = HistoryScope(kind="agent", scope_id="code")
_NAMES = TableNames("code_sessions")


def _open(tmp_path: Path) -> SqliteDb:
    db = create_state_storage("code", tmp_path, subdir="sessions", session_table="code_sessions")
    assert isinstance(db, SqliteDb)
    return db


def _as_pre_archive(tmp_path: Path) -> None:
    """Drop the summary-row tables seeding created, since releases before the archive never had them."""
    with closing(sqlite3.connect(tmp_path / "sessions" / "code.db")) as connection, connection:
        connection.execute("DROP TABLE IF EXISTS code_sessions_summaries")
        connection.execute("DROP TABLE IF EXISTS code_sessions_removed_runs")


def _migrated(tmp_path: Path, session: AgentSession | TeamSession) -> SqliteDb:
    """Seed ``session`` the way an older release left it, then reopen so the migration runs."""
    seeding = _open(tmp_path)
    seed_session(seeding, session)
    seeding.close()
    _as_pre_archive(tmp_path)
    return _open(tmp_path)


def _compaction_metadata(scope_key: str, raw_state: dict[str, object]) -> dict[str, object]:
    return {MINDROOM_COMPACTION_METADATA_KEY: {"version": 2, "states": {scope_key: raw_state}}}


def _seen_metadata(scope_key: str, event_ids: list[str]) -> dict[str, object]:
    return {MINDROOM_MATRIX_HISTORY_METADATA_KEY: {"version": 1, "states": {scope_key: {"seen_event_ids": event_ids}}}}


def test_legacy_tombstones_recognize_only_the_destructive_compactor() -> None:
    """Only states carrying a retired compactor key are legacy."""
    assert _legacy_tombstones({"force_compact_before_next_run": True}) is None
    assert _legacy_tombstones({"last_compacted_at": "2026-09-01T00:00:00Z"}) == ()
    assert _legacy_tombstones({"compacted_run_ids": ["a", "", 3, "b", "a"]}) == ("a", "b")


def test_opening_storage_adopts_legacy_summary_tombstones_and_seen_ids_once(tmp_path: Path) -> None:
    """The summary keeps replaying, its seen ids move into a legacy summary row, tombstoned copies go, and so do the retired keys."""
    storage = _migrated(
        tmp_path,
        AgentSession(
            session_id="session",
            agent_id="code",
            runs=[RunOutput(run_id="gone", agent_id="code"), RunOutput(run_id="kept", agent_id="code")],
            summary=SessionSummary(summary="legacy summary"),
            metadata={
                **_compaction_metadata(
                    _SCOPE.key,
                    {
                        "compacted_run_ids": ["gone"],
                        "last_compacted_at": "2026-09-01T00:00:00Z",
                        "last_summary_model": "old-model",
                        "last_compacted_run_count": 1,
                        "force_compact_before_next_run": True,
                    },
                ),
                **_seen_metadata(_SCOPE.key, ["$legacy"]),
            },
        ),
    )
    storage.close()
    storage = _open(tmp_path)
    try:
        stored = get_agent_session(storage, "session")
        assert stored is not None
        assert stored.metadata == _compaction_metadata(_SCOPE.key, {"force_compact_before_next_run": True})
        assert stored.summary is not None
        assert stored.summary.summary == "legacy summary"
        assert summary_rows_of(storage, _SCOPE.key, "session") == [
            StoredSummaryRow(summary="legacy summary", summary_model=None, legacy=True),
        ]
        assert summary_rows.legacy_event_ids(storage, session_id="session", scope_key=_SCOPE.key) == {"$legacy"}
        assert stored_run_ids(storage, "session") == ["kept"]
        assert [run.run_id for run in stored.runs or []] == ["kept"]
    finally:
        storage.close()


def test_opening_storage_adopts_a_summary_without_compaction_state_under_the_row_scope(tmp_path: Path) -> None:
    """A replayed summary with no retired keys belongs to the scope that owns the session row."""
    team_scope = HistoryScope(kind="team", scope_id="squad")
    storage = _migrated(
        tmp_path,
        TeamSession(session_id="session", team_id="squad", summary=SessionSummary(summary="team summary")),
    )
    try:
        assert summary_rows_of(storage, team_scope.key, "session") == [
            StoredSummaryRow(summary="team summary", summary_model=None, legacy=True),
        ]
        stored = get_team_session(storage, "session")
        assert stored is not None
        assert stored.summary is not None
        assert stored.summary.summary == "team summary"
    finally:
        storage.close()


def test_opening_storage_leaves_uncompacted_scopes_and_their_seen_ids_alone(tmp_path: Path) -> None:
    """Seen ids a team recorded without compaction stay in session metadata."""
    metadata = _seen_metadata(_SCOPE.key, ["$consumed"])
    storage = _migrated(
        tmp_path,
        AgentSession(
            session_id="session",
            agent_id="code",
            runs=[RunOutput(run_id="kept", agent_id="code")],
            metadata=metadata,
        ),
    )
    try:
        assert summary_rows_of(storage, _SCOPE.key, "session") == []
        stored = get_agent_session(storage, "session")
        assert stored is not None
        assert stored.metadata == metadata
    finally:
        storage.close()


def test_opening_storage_ignores_malformed_legacy_metadata(tmp_path: Path) -> None:
    """Unreadable metadata is left as it is instead of failing the conversation database open."""
    seeding = _open(tmp_path)
    seed_session(seeding, AgentSession(session_id="session", agent_id="code"))
    seeding.close()
    _as_pre_archive(tmp_path)
    connection = sqlite3.connect(tmp_path / "sessions" / "code.db")
    try:
        connection.execute("UPDATE code_sessions SET metadata = 'not json' WHERE session_id = 'session'")
        connection.commit()
    finally:
        connection.close()

    storage = _open(tmp_path)
    try:
        assert summary_rows_of(storage, _SCOPE.key, "session") == []
    finally:
        storage.close()
    connection = sqlite3.connect(tmp_path / "sessions" / "code.db")
    try:
        assert connection.execute("SELECT metadata FROM code_sessions").fetchone() == ("not json",)
    finally:
        connection.close()


def test_opening_storage_keeps_seen_ids_of_a_scope_without_a_summary(tmp_path: Path) -> None:
    """Tombstones alone carry no replayed summary, so no row is written and the scope's seen ids stay in metadata."""
    seen = _seen_metadata(_SCOPE.key, ["$consumed"])
    storage = _migrated(
        tmp_path,
        AgentSession(
            session_id="session",
            agent_id="code",
            metadata={**_compaction_metadata(_SCOPE.key, {"compacted_run_ids": ["gone"]}), **seen},
        ),
    )
    try:
        assert summary_rows_of(storage, _SCOPE.key, "session") == []
        assert summary_rows.legacy_event_ids(storage, session_id="session", scope_key=_SCOPE.key) is None
        stored = get_agent_session(storage, "session")
        assert stored is not None
        assert stored.metadata == seen
    finally:
        storage.close()


def test_opening_storage_reads_double_encoded_legacy_metadata(tmp_path: Path) -> None:
    """Metadata an older Agno stored as a JSON string of JSON is adopted like plain JSON."""
    seeding = _open(tmp_path)
    seed_session(
        seeding,
        AgentSession(session_id="session", agent_id="code", summary=SessionSummary(summary="legacy summary")),
    )
    seeding.close()
    _as_pre_archive(tmp_path)
    metadata = {
        **_compaction_metadata(_SCOPE.key, {"compacted_run_ids": ["gone"]}),
        **_seen_metadata(_SCOPE.key, ["$legacy"]),
    }
    connection = sqlite3.connect(tmp_path / "sessions" / "code.db")
    try:
        connection.execute(
            "UPDATE code_sessions SET metadata = ? WHERE session_id = 'session'",
            (json.dumps(json.dumps(metadata)),),
        )
        connection.commit()
    finally:
        connection.close()

    storage = _open(tmp_path)
    try:
        assert summary_rows_of(storage, _SCOPE.key, "session") == [
            StoredSummaryRow(summary="legacy summary", summary_model=None, legacy=True),
        ]
        assert summary_rows.legacy_event_ids(storage, session_id="session", scope_key=_SCOPE.key) == {"$legacy"}
        stored = get_agent_session(storage, "session")
        assert stored is not None
        assert stored.metadata == {}
    finally:
        storage.close()


def test_opening_storage_adopts_a_team_row_with_tombstones(tmp_path: Path) -> None:
    """A team scope's tombstones and summary are adopted under the team scope."""
    team_scope = HistoryScope(kind="team", scope_id="squad")
    storage = _migrated(
        tmp_path,
        TeamSession(
            session_id="session",
            team_id="squad",
            summary=SessionSummary(summary="team summary"),
            metadata=_compaction_metadata(team_scope.key, {"compacted_run_ids": ["gone"]}),
        ),
    )
    try:
        assert summary_rows_of(storage, team_scope.key, "session") == [
            StoredSummaryRow(summary="team summary", summary_model=None, legacy=True),
        ]
        stored = get_team_session(storage, "session")
        assert stored is not None
        assert stored.metadata == {}
    finally:
        storage.close()


def test_opening_storage_turns_pre_archive_blob_runs_into_rows_without_tombstoned_ones(tmp_path: Path) -> None:
    """Agno 2 blob runs stay in the conversation, except the copy of a run the destructive compactor deleted."""
    seeding = _open(tmp_path)
    seed_session(
        seeding,
        AgentSession(
            session_id="session",
            agent_id="code",
            summary=SessionSummary(summary="legacy summary"),
            metadata=_compaction_metadata(_SCOPE.key, {"compacted_run_ids": ["gone"]}),
        ),
    )
    seeding.close()
    _as_pre_archive(tmp_path)
    blob = [{"run_id": run_id, "agent_id": "code", "session_id": "session"} for run_id in ("gone", "kept")]
    with closing(sqlite3.connect(tmp_path / "sessions" / "code.db")) as connection, connection:
        connection.execute("ALTER TABLE code_sessions ADD COLUMN runs JSON")
        connection.execute("UPDATE code_sessions SET runs = ?", (json.dumps(json.dumps(blob)),))

    storage = _open(tmp_path)
    try:
        stored = get_agent_session(storage, "session")
        assert stored is not None
        assert stored_run_ids(storage, "session") == ["kept"]
        assert [run.run_id for run in stored.runs or []] == ["kept"]
        assert stored.summary is not None
        assert stored.summary.summary == "legacy summary"
    finally:
        storage.close()


def test_opening_storage_numbers_rows_an_old_writer_stored_without_an_index(tmp_path: Path) -> None:
    """Rows without an index keep the first places a read gave them, and every row gets an index."""
    seeding = _open(tmp_path)
    seed_session(
        seeding,
        AgentSession(
            session_id="session",
            agent_id="code",
            runs=[RunOutput(run_id=run_id, agent_id="code") for run_id in ("r0", "r1", "r2")],
        ),
    )
    seeding.close()
    _as_pre_archive(tmp_path)
    with closing(sqlite3.connect(tmp_path / "sessions" / "code.db")) as connection, connection:
        connection.execute("UPDATE code_sessions_runs SET run_index = NULL WHERE run_id = 'r2'")

    storage = _open(tmp_path)
    storage.close()

    with closing(sqlite3.connect(tmp_path / "sessions" / "code.db")) as connection, connection:
        indexes = connection.execute("SELECT run_id, run_index FROM code_sessions_runs ORDER BY run_index").fetchall()
    assert indexes == [("r2", 0), ("r0", 1), ("r1", 2)]


def test_an_agent_named_agno_migrates_the_agno_runs_table(tmp_path: Path) -> None:
    """Agno names the runs table beside ``agno_sessions`` ``agno_runs``; the migration works on it and adds no other."""
    scope = HistoryScope(kind="agent", scope_id="agno")
    seeding = create_state_storage("agno", tmp_path, subdir="sessions", session_table="agno_sessions")
    seed_session(
        seeding,
        AgentSession(
            session_id="session",
            agent_id="agno",
            runs=[RunOutput(run_id=run_id, agent_id="agno") for run_id in ("gone", "r1", "r2")],
            summary=SessionSummary(summary="legacy summary"),
            metadata=_compaction_metadata(scope.key, {"compacted_run_ids": ["gone"]}),
        ),
    )
    seeding.close()
    db_file = tmp_path / "sessions" / "agno.db"
    with closing(sqlite3.connect(db_file)) as connection, connection:
        connection.execute("DROP TABLE agno_sessions_summaries")
        connection.execute("DROP TABLE agno_sessions_removed_runs")
        connection.execute("UPDATE agno_runs SET run_index = NULL WHERE run_id = 'r2'")

    storage = create_state_storage("agno", tmp_path, subdir="sessions", session_table="agno_sessions")
    try:
        assert stored_run_ids(storage, "session") == ["r2", "r1"]
        assert summary_rows_of(storage, scope.key, "session") == [
            StoredSummaryRow(summary="legacy summary", summary_model=None, legacy=True),
        ]
    finally:
        storage.close()
    with closing(sqlite3.connect(db_file)) as connection:
        runs_tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE '%runs' ORDER BY name",
        ).fetchall()
        indexes = connection.execute("SELECT run_id, run_index FROM agno_runs ORDER BY run_index").fetchall()
    assert runs_tables == [("agno_runs",), ("agno_sessions_removed_runs",)]
    assert indexes == [("r2", 0), ("r1", 2)]


def _agno_2_database_with_summary(tmp_path: Path) -> Path:
    """Write the Agno 2.6.12 fixture with a replayed ``session.summary``, as a destructive compactor left it."""
    db_file = create_agno_2_sessions_db(tmp_path / "code.db")
    with closing(sqlite3.connect(db_file)) as connection, connection:
        connection.execute("UPDATE code_sessions SET summary = ?", (json.dumps({"summary": "legacy summary"}),))
    return db_file


def _migrated_log(db_file: Path) -> tuple[list[tuple[str, int]], list[tuple[str, str, int]], object]:
    """Return the run positions, summary rows, and Agno 2 blob of the migrated fixture's session."""
    with closing(sqlite3.connect(db_file)) as connection:
        runs = connection.execute("SELECT run_id, run_index FROM code_sessions_runs ORDER BY run_index").fetchall()
        rows = connection.execute(
            "SELECT scope_key, summary, legacy FROM code_sessions_summaries ORDER BY id",
        ).fetchall()
        (blob,) = connection.execute("SELECT runs FROM code_sessions").fetchone()
    return runs, rows, blob


def _dump(db_file: Path) -> list[str]:
    with closing(sqlite3.connect(db_file)) as connection:
        return list(connection.iterdump())


_MIGRATED_LOG = ([("run-1", 0), ("run-2", 1), ("run-3", 2)], [(_SCOPE.key, "legacy summary", 1)], None)


def test_an_agno_2_database_migrates_before_the_owned_store_opens_it(tmp_path: Path) -> None:
    """The migration needs no Agno storage, and the owned store then loads the runs after the adopted summary."""
    db_file = _agno_2_database_with_summary(tmp_path)

    migrate_to_summary_rows(db_file, _NAMES)

    assert _migrated_log(db_file) == _MIGRATED_LOG
    session = open_conversation_store(db_file, _NAMES).load_session("session-1", "agent")
    assert session is not None
    assert [run.run_id for run in session.runs] == ["run-1", "run-2", "run-3"]
    assert session.summary is not None
    assert session.summary.text == "legacy summary"


def test_an_interrupted_migration_changes_nothing_and_runs_again(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure after the blob moved and the summary row was written leaves the database as it was."""
    db_file = _agno_2_database_with_summary(tmp_path)
    before = _dump(db_file)

    def interrupt(*_args: object, **_kwargs: object) -> set[str]:
        message = "interrupted adoption"
        raise RuntimeError(message)

    with monkeypatch.context() as patch:
        patch.setattr(legacy_compaction_state, "delete_run_subtrees_in_transaction", interrupt)
        with pytest.raises(RuntimeError, match="interrupted adoption"):
            migrate_to_summary_rows(db_file, _NAMES)

    assert _dump(db_file) == before
    migrate_to_summary_rows(db_file, _NAMES)
    assert _migrated_log(db_file) == _MIGRATED_LOG


def test_owners_migrating_at_once_adopt_the_summary_once(tmp_path: Path) -> None:
    """Startups racing on one database migrate it once: one legacy summary row and one row per run."""
    db_file = _agno_2_database_with_summary(tmp_path)
    start = threading.Barrier(4)

    def migrate(_index: int) -> None:
        start.wait()
        migrate_to_summary_rows(db_file, _NAMES)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(migrate, range(4)))

    assert _migrated_log(db_file) == _MIGRATED_LOG
