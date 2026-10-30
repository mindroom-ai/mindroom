"""Every table of a conversation database is named after its storage name, and storage v2's layout is the one it states."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from mindroom.history.types import HistoryScope
from mindroom.session_storage.legacy_agno3_schema import AgnoTableNames, agno_runs_sql, agno_schema_sql
from mindroom.session_storage.schema import (
    LOG_VERSION,
    TableNames,
    create_log_schema,
    log_schema_sql,
    quote_identifier,
    table_names,
)
from tests.session_storage.goldens import golden_stores

if TYPE_CHECKING:
    from tests.session_storage.goldens import GoldenStore


def test_a_databases_tables_are_all_named_after_its_storage_name() -> None:
    """``<name>.db`` holds ``<name>_sessions`` and the tables beside it, the Agno layout's too; the pairing has one owner."""
    names = AgnoTableNames("code")

    assert names.sessions == "code_sessions"
    assert names.runs == "code_sessions_runs"
    assert names.summaries == "code_sessions_summaries"
    assert names.removed_runs == "code_sessions_removed_runs"
    assert names.usage == "code_sessions_usage"
    assert names.log_meta == "code_sessions_log_meta"
    assert names.log_runs == "code_sessions_log_runs"
    assert names.log_messages == "code_sessions_log_messages"
    assert names.log_summaries == "code_sessions_log_summaries"
    assert names.log_removed == "code_sessions_log_removed"


def test_storage_v2_names_the_runs_table_of_agno_like_every_other() -> None:
    """The storage named ``agno`` keeps no runs table named differently once its log tables replace the old ones."""
    names = AgnoTableNames("agno")

    assert (names.runs, names.log_runs) == ("agno_runs", "agno_sessions_log_runs")


def test_a_quoted_identifier_names_one_table_whatever_characters_it_holds() -> None:
    """A name holding a double quote still names one table: the quote is doubled inside the quoted identifier."""
    name = 'say "hi"'
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.execute(f"CREATE TABLE {quote_identifier(name)} (value)")

        assert table_names(connection) == {name}
    assert quote_identifier(name) == '"say ""hi"""'


def test_a_databases_file_is_named_after_its_storage_name(tmp_path: Path) -> None:
    """``<name>.db`` is the file under ``sessions/`` of its state root, and its name gives the names back."""
    database = TableNames("team_squad_1f02af98668f").database_path(tmp_path)

    assert database == tmp_path / "sessions" / "team_squad_1f02af98668f.db"
    assert TableNames.of_database(database) == TableNames("team_squad_1f02af98668f")


@pytest.mark.parametrize("storage_name", ["", "nested/name", "../outside"])
def test_a_storage_name_must_name_a_file_directly_inside_its_sessions_directory(
    storage_name: str,
    tmp_path: Path,
) -> None:
    """A name that is empty or leaves the sessions directory has no database file."""
    with pytest.raises(ValueError, match="must identify a file directly inside its sessions directory"):
        TableNames(storage_name).database_path(tmp_path)


def test_the_agno_schema_creates_exactly_the_tables_the_names_give_it() -> None:
    """The Agno layout's DDL creates the tables ``AgnoTableNames`` names, so no table is named in two places."""
    names = AgnoTableNames("code")
    with closing(sqlite3.connect(":memory:")) as connection:
        for statement in agno_schema_sql(names):
            connection.execute(statement)
        tables = table_names(connection)

    assert tables == {names.sessions, names.runs, names.summaries, names.removed_runs, names.usage}


@pytest.mark.parametrize("store", golden_stores(), ids=lambda store: store.label)
def test_the_pairing_is_the_one_every_database_agno_wrote_follows(store: GoldenStore, tmp_path: Path) -> None:
    """Each golden file ``<name>.db`` lies where ``TableNames(<name>)`` puts it and holds its sessions and runs tables."""
    database = store.restore(tmp_path)
    with closing(sqlite3.connect(database)) as connection:
        tables = table_names(connection)

    assert {store.names.sessions, store.names.runs} <= tables
    assert store.names.database_path(tmp_path / Path(store.db_path).parent.parent) == database


@pytest.mark.parametrize(
    ("storage_name", "runs"),
    [("code", "code_sessions_runs"), ("agno", "agno_runs")],
)
def test_the_runs_table_keeps_the_name_agno_gave_it(storage_name: str, runs: str) -> None:
    """Agno 3 names the runs table beside its default ``agno_sessions`` table ``agno_runs``, else ``<sessions>_runs``."""
    names = AgnoTableNames(storage_name)
    with closing(sqlite3.connect(":memory:")) as connection:
        for statement in agno_runs_sql(names):
            connection.execute(statement)
        tables = table_names(connection)

    assert names.runs == runs
    assert tables == {names.sessions, runs}


# Storage v2's layout for the storage name ``code``: every column as ``name TYPE [NOT NULL] [DEFAULT x] [PKn]``.
_LOG_COLUMNS = {
    "code_sessions": [
        "session_id VARCHAR NOT NULL PK1",
        "user_id VARCHAR",
        "session_data JSON",
        "metadata JSON",
        "created_at BIGINT NOT NULL",
        "updated_at BIGINT",
        "session_state JSON",
    ],
    "code_sessions_log_meta": [
        "id INTEGER PK1",
        "version INTEGER NOT NULL",
        "next_seq INTEGER NOT NULL",
        "owner_kind TEXT",
        "owner_id TEXT",
    ],
    "code_sessions_log_runs": [
        "run_id TEXT NOT NULL PK1",
        "session_id TEXT NOT NULL",
        "parent_run_id TEXT",
        "kind TEXT NOT NULL",
        "scope_key TEXT NOT NULL",
        "status TEXT NOT NULL",
        "seq INTEGER NOT NULL",
        "folded_by INTEGER",
        "has_tools INTEGER NOT NULL",
        "created_at INTEGER NOT NULL",
        "updated_at INTEGER NOT NULL",
        "metadata TEXT",
        "record TEXT NOT NULL",
        "requirements TEXT",
    ],
    "code_sessions_log_messages": [
        "run_id TEXT NOT NULL PK1",
        "message_id TEXT NOT NULL PK2",
        "ord INTEGER NOT NULL",
        "folded_by INTEGER",
        "data TEXT NOT NULL",
    ],
    "code_sessions_log_tools": ["run_id TEXT NOT NULL PK1", "ord INTEGER NOT NULL PK2", "data TEXT NOT NULL"],
    "code_sessions_log_summaries": [
        "id INTEGER PK1",
        "session_id TEXT NOT NULL",
        "scope_key TEXT NOT NULL",
        "through INTEGER NOT NULL",
        "legacy INTEGER NOT NULL DEFAULT 0",
        "created_at INTEGER NOT NULL",
        "fold_run_id TEXT",
        "summary TEXT",
        "event_ids TEXT NOT NULL DEFAULT '[]'",
        "seen_event_ids TEXT NOT NULL DEFAULT '[]'",
        "fold_event_ids TEXT",
        "fold_seen_event_ids TEXT",
    ],
    "code_sessions_log_removed": ["run_id TEXT NOT NULL PK1", "session_id TEXT NOT NULL"],
    "code_sessions_usage": ["id INTEGER PK1", "session_id TEXT NOT NULL", "run_id TEXT", "usage_data TEXT"],
}
_LOG_FOREIGN_KEYS = {
    "code_sessions_log_runs": ["session_id -> code_sessions.session_id ON DELETE CASCADE"],
    "code_sessions_log_messages": ["run_id -> code_sessions_log_runs.run_id ON DELETE CASCADE"],
    "code_sessions_log_tools": ["run_id -> code_sessions_log_runs.run_id ON DELETE CASCADE"],
    "code_sessions_log_summaries": ["session_id -> code_sessions.session_id ON DELETE CASCADE"],
    "code_sessions_log_removed": ["session_id -> code_sessions.session_id ON DELETE CASCADE"],
    "code_sessions_usage": ["session_id -> code_sessions.session_id ON DELETE CASCADE"],
}
# Every schema object as ``(type, name, table)``: besides the declared ones, SQLite's unique indexes for text primary
# keys and the ledger's ``UNIQUE(session_id, run_id)``, and its table of ``AUTOINCREMENT`` counters.
_LOG_OBJECTS = [
    ("table", "code_sessions", "code_sessions"),
    ("table", "code_sessions_log_messages", "code_sessions_log_messages"),
    ("table", "code_sessions_log_meta", "code_sessions_log_meta"),
    ("table", "code_sessions_log_removed", "code_sessions_log_removed"),
    ("table", "code_sessions_log_runs", "code_sessions_log_runs"),
    ("table", "code_sessions_log_summaries", "code_sessions_log_summaries"),
    ("table", "code_sessions_log_tools", "code_sessions_log_tools"),
    ("table", "code_sessions_usage", "code_sessions_usage"),
    ("table", "sqlite_sequence", "sqlite_sequence"),
    ("index", "idx_code_sessions_log_messages_run_id_ord", "code_sessions_log_messages"),
    ("index", "idx_code_sessions_log_runs_parent_run_id", "code_sessions_log_runs"),
    ("index", "idx_code_sessions_log_runs_session_id_seq", "code_sessions_log_runs"),
    ("index", "idx_code_sessions_log_summaries_session_id_scope_key_id", "code_sessions_log_summaries"),
    ("index", "sqlite_autoindex_code_sessions_1", "code_sessions"),
    ("index", "sqlite_autoindex_code_sessions_log_messages_1", "code_sessions_log_messages"),
    ("index", "sqlite_autoindex_code_sessions_log_removed_1", "code_sessions_log_removed"),
    ("index", "sqlite_autoindex_code_sessions_log_runs_1", "code_sessions_log_runs"),
    ("index", "sqlite_autoindex_code_sessions_log_tools_1", "code_sessions_log_tools"),
    ("index", "sqlite_autoindex_code_sessions_usage_1", "code_sessions_usage"),
]


def _layout(connection: sqlite3.Connection) -> dict[str, object]:
    """Return what the database's schema holds: its objects, its tables' columns and foreign keys, and the meta row."""
    objects = connection.execute("SELECT type, name, tbl_name FROM sqlite_master ORDER BY type DESC, name").fetchall()
    tables = [name for kind, name, _ in objects if kind == "table" and not name.startswith("sqlite_")]
    return {
        "objects": objects,
        "columns": {
            table: [
                f"{name} {kind}{' NOT NULL' if not_null else ''}{f' DEFAULT {default}' if default else ''}"
                f"{f' PK{key}' if key else ''}"
                for name, kind, not_null, default, key in connection.execute(
                    'SELECT name, type, "notnull", dflt_value, pk FROM pragma_table_info(?)',
                    (table,),
                )
            ]
            for table in tables
        },
        "foreign_keys": {
            table: keys
            for table in tables
            if (
                keys := [
                    f"{column} -> {parent}.{parent_column} ON DELETE {on_delete}"
                    for column, parent, parent_column, on_delete in connection.execute(
                        'SELECT "from", "table", "to", on_delete FROM pragma_foreign_key_list(?)',
                        (table,),
                    )
                ]
            )
        },
        "meta": connection.execute("SELECT * FROM code_sessions_log_meta").fetchall(),
    }


_OWNER = HistoryScope(kind="agent", scope_id="code")


def _log_database(path: Path | str = ":memory:") -> sqlite3.Connection:
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute("PRAGMA foreign_keys = ON")
    create_log_schema(connection, TableNames("code"), _OWNER)
    return connection


def test_the_schema_creates_exactly_its_tables_indexes_and_meta_row(tmp_path: Path) -> None:
    """An empty file gets the seven-column sessions table, the six log tables, the ledger, and a meta row naming its owner."""
    with closing(_log_database(tmp_path / "code.db")) as connection:
        layout = _layout(connection)

    assert layout == {
        "objects": _LOG_OBJECTS,
        "columns": _LOG_COLUMNS,
        "foreign_keys": _LOG_FOREIGN_KEYS,
        "meta": [(1, LOG_VERSION, 1, "agent", "code")],
    }
    assert LOG_VERSION == 7


def test_the_schema_changes_nothing_when_run_again() -> None:
    """A second run leaves every schema object and a counter that moved since alone, as a converted file needs."""
    with closing(_log_database()) as connection:
        connection.execute("UPDATE code_sessions_log_meta SET next_seq = 7")
        before = connection.execute("SELECT * FROM sqlite_master ORDER BY name").fetchall()
        create_log_schema(connection, TableNames("code"), HistoryScope(kind="team", scope_id="other"))
        after = connection.execute("SELECT * FROM sqlite_master ORDER BY name").fetchall()
        meta = connection.execute("SELECT * FROM code_sessions_log_meta").fetchall()

    assert after == before
    assert meta == [(1, LOG_VERSION, 7, "agent", "code")]


@pytest.mark.parametrize("row_id", [1, 2])
def test_the_meta_table_holds_one_row(row_id: int) -> None:
    """Its constant primary key refuses a second row, under the same key or another."""
    with closing(_log_database()) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute("INSERT INTO code_sessions_log_meta VALUES (?, 2, 1, 'agent', 'code')", (row_id,))


def test_a_summary_id_is_never_handed_out_again() -> None:
    """Deleting the newest summary row does not let the next one reuse its id, which an append's compare reads."""
    with closing(_log_database()) as connection:
        connection.execute("INSERT INTO code_sessions (session_id, created_at) VALUES ('s', 1)")
        insert = (
            "INSERT INTO code_sessions_log_summaries (session_id, scope_key, through, created_at) VALUES (?, ?, ?, ?)"
        )
        connection.execute(insert, ("s", "agent:code", 1, 1))
        (newest,) = connection.execute(insert + " RETURNING id", ("s", "agent:code", 2, 1)).fetchone()
        connection.execute("DELETE FROM code_sessions_log_summaries WHERE id = ?", (newest,))
        (next_id,) = connection.execute(insert + " RETURNING id", ("s", "agent:code", 2, 1)).fetchone()

    assert next_id == newest + 1


@pytest.mark.parametrize("store", golden_stores(), ids=lambda store: store.label)
def test_the_v2_tables_sit_beside_the_agno_tables_of_every_golden(store: GoldenStore, tmp_path: Path) -> None:
    """On a database Agno wrote, the v2 schema adds its tables and leaves every existing object as it was."""
    database = store.restore(tmp_path)
    with closing(sqlite3.connect(database, isolation_level=None)) as connection:
        before = set(connection.execute("SELECT * FROM sqlite_master").fetchall())
        for statement in log_schema_sql(store.names):
            connection.execute(statement)
        after = set(connection.execute("SELECT * FROM sqlite_master").fetchall())
        added = {name for kind, name, *_ in after - before if kind == "table"}

    assert before <= after
    assert added == {
        store.names.log_meta,
        store.names.log_runs,
        store.names.log_messages,
        store.names.log_tools,
        store.names.log_summaries,
        store.names.log_removed,
        "sqlite_sequence",
    }
