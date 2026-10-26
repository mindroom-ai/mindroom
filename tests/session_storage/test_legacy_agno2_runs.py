"""Runs Agno 2 kept in a JSON blob on the session row become run rows once, when the database converts to storage v2."""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from typing import TYPE_CHECKING, Any

import pytest

from mindroom.history import legacy_compaction_state
from mindroom.history.legacy_compaction_state import adopt_summary_rows
from mindroom.session_storage.legacy_agno2_runs import move_blob_runs_to_rows, number_unindexed_runs
from mindroom.session_storage.legacy_agno3_conversion import migrate_conversation_database
from mindroom.session_storage.legacy_agno3_schema import AgnoTableNames, agno_runs_sql
from mindroom.session_storage.store import ConversationStore
from tests.conftest import create_agno_2_sessions_db
from tests.session_storage.goldens import golden_stores

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.llm.runs import AnyRunRecord

_NAMES = AgnoTableNames("code")
# JSON nested deeper than the decoder recurses.
_TOO_DEEP = "[" * 100_000 + "]" * 100_000
# Values worker code can write in a blob entry where Agno wrote text or a timestamp.
_ODD_VALUES = {
    "nan": math.nan,
    "infinity": math.inf,
    "beyond_64_bits": 2**70,
    "negative": -5,
    "fraction": 1.5,
    "text": "yesterday",
    "object": {"at": 1},
    "list": [1],
    "bool": True,
}
_TEXT_FIELDS = ("agent_id", "team_id", "workflow_id", "user_id", "parent_run_id", "status")


def _agno_2_database(tmp_path: Path) -> Path:
    return create_agno_2_sessions_db(_NAMES.database_path(tmp_path))


def _converted(db_file: Path) -> ConversationStore:
    migrate_conversation_database(db_file, _NAMES)
    return ConversationStore(db_file)


def _has_blob_column(db_file: Path) -> bool:
    with closing(sqlite3.connect(db_file)) as connection:
        return "runs" in {row[1] for row in connection.execute("PRAGMA table_info(code_sessions)")}


def _set_blob(db_file: Path, entries: list[object]) -> None:
    with closing(sqlite3.connect(db_file)) as connection, connection:
        connection.execute(
            "UPDATE code_sessions SET runs = ? WHERE session_id = 'session-1'",
            (json.dumps(json.dumps(entries)),),
        )


def _add_rows(db_file: Path, *rows: tuple[str, int, dict[str, Any]]) -> None:
    with closing(sqlite3.connect(db_file)) as connection, connection:
        for statement in agno_runs_sql(_NAMES):
            connection.execute(statement)
        connection.executemany(
            "INSERT INTO code_sessions_runs (run_id, session_id, run_type, agent_id, status, run_index, run_data, "
            "created_at) VALUES (?, 'session-1', 'agent', 'code', 'COMPLETED', ?, ?, ?)",
            [(run_id, index, json.dumps(data), data["created_at"]) for run_id, index, data in rows],
        )


def _entry(run_id: str, created_at: int, content: str = "blob") -> dict[str, Any]:
    return {"run_id": run_id, "agent_id": "code", "content": content, "created_at": created_at, "messages": []}


def _log(db_file: Path) -> list[tuple[str, int, str | None]]:
    """Return the converted session's runs as ``(run_id, seq, content)`` in log order."""
    with closing(sqlite3.connect(db_file)) as connection:
        rows = connection.execute(
            "SELECT run_id, seq, record FROM code_sessions_log_runs WHERE session_id = 'session-1' ORDER BY seq",
        ).fetchall()
    return [(run_id, seq, json.loads(record).get("content")) for run_id, seq, record in rows]


def _comparable(run: AnyRunRecord) -> dict[str, Any]:
    """A run's stored dict minus its message ids, which the goldens renumber."""
    data = run.to_dict()
    return {**data, "messages": [{**message, "id": None} for message in data["messages"]]}


def test_blob_runs_become_rows_when_the_summary_rows_migrate(tmp_path: Path) -> None:
    """The runs equal those of the rows MindRoom's Agno-backed migration wrote from the same Agno 2.6.12 database."""
    db_file = _agno_2_database(tmp_path)
    [golden] = [store for store in golden_stores() if store.label == "agno2_migrated"]
    expected = golden.open(tmp_path / "golden").list_runs("session-1")

    store = _converted(db_file)

    assert [_comparable(run) for run in store.list_runs("session-1")] == [_comparable(run) for run in expected]
    assert not _has_blob_column(db_file)
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
    db_file = _agno_2_database(tmp_path)
    _set_blob(db_file, [_entry("run-b", 2), _entry("run-a", 1)])
    _add_rows(db_file, ("run-c", 0, _entry("run-c", 3, "table")), ("run-a", 1, _entry("run-a", 1, "table")))

    _converted(db_file)

    assert _log(db_file) == [("run-b", 1, "blob"), ("run-a", 2, "table"), ("run-c", 3, "table")]


def test_blob_entries_a_read_could_not_address_are_dropped(tmp_path: Path) -> None:
    """Entries without a string run id, and later duplicates of an id, write no row."""
    db_file = _agno_2_database(tmp_path)
    _set_blob(
        db_file,
        [{"run_id": 5, "agent_id": "code"}, {"agent_id": "code"}, "junk", _entry("ok", 1), _entry("ok", 2, "dup")],
    )

    _converted(db_file)

    assert _log(db_file) == [("ok", 1, "blob")]
    assert not _has_blob_column(db_file)


@pytest.mark.parametrize("blob", ["not json", _TOO_DEEP], ids=["not-json", "too-deep"])
def test_an_unreadable_blob_is_cleared_without_rows(tmp_path: Path, blob: str) -> None:
    """A blob that is not JSON, or that no decoder can read, holds nothing a read could return."""
    db_file = _agno_2_database(tmp_path)
    with closing(sqlite3.connect(db_file)) as connection, connection:
        connection.execute("UPDATE code_sessions SET runs = ?", (blob,))

    _converted(db_file)

    assert _log(db_file) == []
    assert not _has_blob_column(db_file)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        *(("created_at", value) for value in _ODD_VALUES.values()),
        *((field, value) for field in _TEXT_FIELDS for value in (_ODD_VALUES["object"], _ODD_VALUES["list"], 2**70)),
    ],
    ids=[
        *(f"created_at-{name}" for name in _ODD_VALUES),
        *(f"{field}-{name}" for field in _TEXT_FIELDS for name in ("object", "list", "beyond_64_bits")),
    ],
)
def test_a_blob_entry_with_an_unusable_value_converts_alone(tmp_path: Path, field: str, value: object) -> None:
    """A value Agno never wrote in an entry is left out of its row, so the database still converts with every other run."""
    db_file = _agno_2_database(tmp_path)
    _set_blob(db_file, [{**_entry("odd", 1), field: value}, _entry("ok", 2)])
    started = int(time.time())

    store = _converted(db_file)

    assert "ok" in [run_id for run_id, _, _ in _log(db_file)]
    with closing(sqlite3.connect(db_file)) as connection:
        created = dict(connection.execute("SELECT run_id, created_at FROM code_sessions_log_runs").fetchall())
    assert all(isinstance(created_at, int) and created_at > 0 for created_at in created.values())
    if field == "created_at":
        # The conversion's time stands in for one Agno never wrote.
        assert created["odd"] >= started
    assert store.load_session("session-1", "agent") is not None


def test_a_database_with_summary_rows_is_never_migrated_again(tmp_path: Path) -> None:
    """Once an older release gave the database summary rows, a blob is no longer Agno 2 history and converts to no run."""
    db_file = _agno_2_database(tmp_path)
    with closing(sqlite3.connect(db_file)) as connection, connection:
        adopt_summary_rows(connection, _NAMES)
    _add_rows(db_file, ("run-4", 3, _entry("run-4", 4, "table")))
    _set_blob(db_file, [_entry("late", 5)])

    _converted(db_file)

    assert [run_id for run_id, _, _ in _log(db_file)] == ["run-1", "run-2", "run-3", "run-4"]


def test_databases_agno_3_created_are_left_untouched(tmp_path: Path) -> None:
    """A database Agno 3 created already has summary rows, so the conversion's adoption step writes nothing."""
    [golden] = [store for store in golden_stores() if store.label == "agent_code"]
    db_file = golden.restore(tmp_path)
    before = db_file.read_bytes()

    with closing(sqlite3.connect(db_file)) as connection, connection:
        adopt_summary_rows(connection, golden.names)

    assert db_file.read_bytes() == before


def test_owners_migrating_at_once_move_the_blob_runs_once(tmp_path: Path) -> None:
    """Owners racing to convert one Agno 2 database write each run row once."""
    db_file = _agno_2_database(tmp_path)
    start = threading.Barrier(4)

    def migrate(_index: int) -> None:
        start.wait()
        migrate_conversation_database(db_file, _NAMES)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(migrate, range(4)))

    assert [(run_id, seq) for run_id, seq, _ in _log(db_file)] == [("run-1", 1), ("run-2", 2), ("run-3", 3)]


def test_a_move_that_fails_part_way_leaves_the_database_as_agno_2_left_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure after the rows are written and the blob cleared commits nothing, so the next conversion moves them."""
    db_file = _agno_2_database(tmp_path)
    with closing(sqlite3.connect(db_file)) as connection:
        before = list(connection.iterdump())

    def move_then_fail(connection: sqlite3.Connection, names: AgnoTableNames) -> None:
        move_blob_runs_to_rows(connection, names)
        msg = "database or disk is full"
        raise sqlite3.OperationalError(msg)

    with monkeypatch.context() as patch:
        patch.setattr(legacy_compaction_state, "move_blob_runs_to_rows", move_then_fail)
        with pytest.raises(sqlite3.OperationalError, match="disk is full"):
            migrate_conversation_database(db_file, _NAMES)

    with closing(sqlite3.connect(db_file)) as connection:
        assert list(connection.iterdump()) == before
    _converted(db_file)
    assert [(run_id, seq) for run_id, seq, _ in _log(db_file)] == [("run-1", 1), ("run-2", 2), ("run-3", 3)]
    assert not _has_blob_column(db_file)


def test_rows_without_an_index_are_numbered_in_read_order(tmp_path: Path) -> None:
    """Agno read rows without an index first, oldest first; other sessions keep their indexes, gaps included."""
    with closing(sqlite3.connect(tmp_path / "code.db")) as connection:
        for statement in agno_runs_sql(_NAMES):
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
