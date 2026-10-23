"""Runs Agno 2 kept in a JSON blob on the session row move into run rows once, when the store opens."""

from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from typing import TYPE_CHECKING, Any

import pytest

from mindroom.llm.runs import RunRecord
from mindroom.session_storage import store as store_module
from mindroom.session_storage.legacy_agno2_runs import move_blob_runs_to_rows, number_unindexed_runs
from mindroom.session_storage.schema import TableNames, run_log_sql
from mindroom.session_storage.store import open_conversation_store
from mindroom.session_storage.types import ConversationSession
from tests.conftest import create_agno_2_sessions_db
from tests.session_storage.goldens import golden_stores, stored_rows

if TYPE_CHECKING:
    from pathlib import Path

_NAMES = TableNames("code")


def _blob(db_file: Path) -> object:
    with closing(sqlite3.connect(db_file)) as connection, connection:
        return connection.execute("SELECT runs FROM code_sessions WHERE session_id = 'session-1'").fetchone()[0]


def _set_blob(db_file: Path, entries: list[object]) -> None:
    with closing(sqlite3.connect(db_file)) as connection, connection:
        connection.execute(
            "UPDATE code_sessions SET runs = ? WHERE session_id = 'session-1'",
            (json.dumps(json.dumps(entries)),),
        )


def _add_rows(db_file: Path, *rows: tuple[str, int, dict[str, Any]]) -> None:
    with closing(sqlite3.connect(db_file)) as connection, connection:
        for statement in run_log_sql(_NAMES):
            connection.execute(statement)
        connection.executemany(
            "INSERT INTO code_sessions_runs (run_id, session_id, run_type, agent_id, status, run_index, run_data, "
            "created_at) VALUES (?, 'session-1', 'agent', 'code', 'COMPLETED', ?, ?, ?)",
            [(run_id, index, json.dumps(data), data["created_at"]) for run_id, index, data in rows],
        )


def _entry(run_id: str, created_at: int, content: str = "blob") -> dict[str, Any]:
    return {"run_id": run_id, "agent_id": "code", "content": content, "created_at": created_at, "messages": []}


def _log(db_file: Path) -> list[tuple[str, int, str | None]]:
    rows = stored_rows(db_file, "code_sessions_runs", "session-1")
    return [(row["run_id"], row["run_index"], row["run_data"].get("content")) for row in rows]


def _comparable(row: dict[str, Any]) -> dict[str, Any]:
    """A run row minus its write time and its message ids, which the goldens renumber."""
    messages = [{**message, "id": None} for message in row["run_data"]["messages"]]
    return {**row, "updated_at": None, "run_data": {**row["run_data"], "messages": messages}}


def test_blob_runs_become_rows_when_the_store_opens(tmp_path: Path) -> None:
    """The rows equal the ones MindRoom's Agno-backed migration wrote from the same Agno 2.6.12 database."""
    db_file = create_agno_2_sessions_db(tmp_path / "code.db")
    [golden] = [store for store in golden_stores() if store.label == "agno2_migrated"]
    expected = stored_rows(golden.restore(tmp_path / "golden"), golden.names.runs, "session-1")

    store = open_conversation_store(db_file, _NAMES)

    rows = stored_rows(db_file, _NAMES.runs, "session-1")
    assert [_comparable(row) for row in rows] == [_comparable(row) for row in expected]
    assert _blob(db_file) is None
    session = store.load_session("session-1", "agent")
    assert session is not None
    assert session.session_state == {}
    assert [(run.run_id, run.status.value) for run in session.runs] == [
        ("run-1", "RUNNING"),
        ("run-2", "RUNNING"),
        ("run-3", "RUNNING"),
    ]


def test_blob_runs_keep_the_order_agno_read_them_in(tmp_path: Path) -> None:
    """Agno read blob entries first, with a run's table copy in its place, then runs only the table held."""
    db_file = create_agno_2_sessions_db(tmp_path / "code.db")
    _set_blob(db_file, [_entry("run-b", 2), _entry("run-a", 1)])
    _add_rows(db_file, ("run-c", 0, _entry("run-c", 3, "table")), ("run-a", 1, _entry("run-a", 1, "table")))

    open_conversation_store(db_file, _NAMES)

    assert _log(db_file) == [("run-b", 0, "blob"), ("run-a", 1, "table"), ("run-c", 2, "table")]


def test_blob_entries_a_read_could_not_address_are_dropped(tmp_path: Path) -> None:
    """Entries without a string run id, and later duplicates of an id, write no row."""
    db_file = create_agno_2_sessions_db(tmp_path / "code.db")
    _set_blob(
        db_file,
        [{"run_id": 5, "agent_id": "code"}, {"agent_id": "code"}, "junk", _entry("ok", 1), _entry("ok", 2, "dup")],
    )

    open_conversation_store(db_file, _NAMES)

    assert _log(db_file) == [("ok", 0, "blob")]
    assert _blob(db_file) is None


def test_an_unreadable_blob_is_cleared_without_rows(tmp_path: Path) -> None:
    """A blob that is not JSON holds nothing a read could return."""
    db_file = create_agno_2_sessions_db(tmp_path / "code.db")
    with closing(sqlite3.connect(db_file)) as connection, connection:
        connection.execute("UPDATE code_sessions SET runs = 'not json'")

    open_conversation_store(db_file, _NAMES)

    assert _log(db_file) == []
    assert _blob(db_file) is None


def test_a_database_with_summary_rows_is_never_migrated_again(tmp_path: Path) -> None:
    """Once the conversation log has summary rows, a blob is no longer Agno 2 history and stays untouched."""
    db_file = create_agno_2_sessions_db(tmp_path / "code.db")
    store = open_conversation_store(db_file, _NAMES)
    session = ConversationSession(session_id="session-1", kind="agent", owner_id="code")
    store.save_run(session, RunRecord(run_id="run-4", agent_id="code", created_at=4), record_usage=False)
    _set_blob(db_file, [_entry("late", 5)])

    open_conversation_store(db_file, _NAMES)

    assert [run_id for run_id, _, _ in _log(db_file)] == ["run-1", "run-2", "run-3", "run-4"]
    assert _blob(db_file) is not None


def test_databases_agno_3_created_open_untouched(tmp_path: Path) -> None:
    """A database Agno 3 created has no blob column, so opening it writes nothing."""
    [golden] = [store for store in golden_stores() if store.label == "agent_code"]
    db_file = golden.restore(tmp_path)
    before = db_file.read_bytes()

    open_conversation_store(db_file, golden.names)

    assert db_file.read_bytes() == before


def test_owners_opening_at_once_move_the_blob_runs_once(tmp_path: Path) -> None:
    """Owners racing to open one Agno 2 database write each run row once."""
    db_file = create_agno_2_sessions_db(tmp_path / "code.db")
    start = threading.Barrier(4)

    def open_store(_index: int) -> None:
        start.wait()
        open_conversation_store(db_file, _NAMES)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(open_store, range(4)))

    assert [(run_id, index) for run_id, index, _ in _log(db_file)] == [("run-1", 0), ("run-2", 1), ("run-3", 2)]


def test_a_move_that_fails_part_way_leaves_the_database_as_agno_2_left_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure after the rows are written and the blob cleared commits nothing, so the next open moves them."""
    db_file = create_agno_2_sessions_db(tmp_path / "code.db")
    with closing(sqlite3.connect(db_file)) as connection:
        before = list(connection.iterdump())

    def move_then_fail(connection: sqlite3.Connection, names: TableNames) -> None:
        move_blob_runs_to_rows(connection, names)
        msg = "database or disk is full"
        raise sqlite3.OperationalError(msg)

    with monkeypatch.context() as patch:
        patch.setattr(store_module, "move_blob_runs_to_rows", move_then_fail)
        with pytest.raises(sqlite3.OperationalError, match="disk is full"):
            open_conversation_store(db_file, _NAMES)

    with closing(sqlite3.connect(db_file)) as connection:
        assert list(connection.iterdump()) == before
    open_conversation_store(db_file, _NAMES)
    assert [(run_id, index) for run_id, index, _ in _log(db_file)] == [("run-1", 0), ("run-2", 1), ("run-3", 2)]
    assert _blob(db_file) is None


def test_rows_without_an_index_are_numbered_in_read_order(tmp_path: Path) -> None:
    """Agno read rows without an index first, oldest first; other sessions keep their indexes, gaps included."""
    with closing(sqlite3.connect(tmp_path / "code.db")) as connection:
        for statement in run_log_sql(_NAMES):
            connection.execute(statement)
        connection.executemany(
            "INSERT INTO code_sessions (session_id, session_type, agent_id, created_at) VALUES (?, 'agent', 'code', 1)",
            [("session-1",), ("session-2",)],
        )
        connection.executemany(
            "INSERT INTO code_sessions_runs (run_id, session_id, run_type, agent_id, run_index, run_data, created_at) "
            "VALUES (?, ?, 'agent', 'code', ?, '{}', ?)",
            [
                ("newer", "session-1", None, 9),
                ("older", "session-1", None, 3),
                ("first", "session-1", 4, 1),
                ("second", "session-1", 7, 2),
                ("kept-a", "session-2", 2, 1),
                ("kept-b", "session-2", 5, 2),
            ],
        )

        number_unindexed_runs(connection, _NAMES)

        rows = connection.execute(
            "SELECT session_id, run_id, run_index FROM code_sessions_runs ORDER BY session_id, run_index",
        ).fetchall()
    assert rows == [
        ("session-1", "older", 0),
        ("session-1", "newer", 1),
        ("session-1", "first", 2),
        ("session-1", "second", 3),
        ("session-2", "kept-a", 2),
        ("session-2", "kept-b", 5),
    ]
