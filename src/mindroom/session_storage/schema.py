"""File and table names and layout of a conversation database.

The sessions and runs tables keep the layout Agno 3 created, column for column and index for index, so
databases written before and after Agno's removal are the same files. The summary-row, removed-run, and
usage tables are MindRoom's own; their DDL lives with their owners.

Storage v2 (``log_schema_sql``) replaces the runs, summary-row, and removed-run tables with the five ``*_log_*``
tables, which hold a run's messages as rows of their own, and keeps the usage ledger as it is.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.history.summary_rows_schema import summary_rows_schema_sql, summary_rows_table_names
from mindroom.usage.storage import quote_identifier, usage_table_name, usage_table_sql

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

# The columns of Agno 3's sessions table and their types; the schema preflight requires them of an existing database.
_SESSION_COLUMN_TYPES = {
    "session_id": "VARCHAR NOT NULL",
    "session_type": "VARCHAR NOT NULL",
    "agent_id": "VARCHAR",
    "team_id": "VARCHAR",
    "workflow_id": "VARCHAR",
    "user_id": "VARCHAR",
    "session_data": "JSON",
    "agent_data": "JSON",
    "team_data": "JSON",
    "workflow_data": "JSON",
    "metadata": "JSON",
    "summary": "JSON",
    "created_at": "BIGINT NOT NULL",
    "updated_at": "BIGINT",
}
SESSION_COLUMNS = tuple(_SESSION_COLUMN_TYPES)
_SESSION_COLUMN_SQL = ", ".join(f"{column} {column_type}" for column, column_type in _SESSION_COLUMN_TYPES.items())
# The sessions-table columns storage v2 keeps, every one a current writer or reader uses.
_LOG_SESSION_COLUMNS = (
    "session_id",
    "session_type",
    "agent_id",
    "team_id",
    "user_id",
    "session_data",
    "metadata",
    "created_at",
    "updated_at",
)
_LOG_SESSION_COLUMN_SQL = ", ".join(f"{column} {_SESSION_COLUMN_TYPES[column]}" for column in _LOG_SESSION_COLUMNS)
# The layout version ``<sessions>_log_meta`` records; the Agno 3 layout has no marker.
LOG_VERSION = 2
_SESSIONS_DIRECTORY = "sessions"
_DATABASE_SUFFIX = ".db"
_RUNS_INDEXED_COLUMNS = (
    "session_id",
    "run_type",
    "agent_id",
    "team_id",
    "workflow_id",
    "user_id",
    "status",
    "created_at",
)


@dataclass(frozen=True, slots=True)
class TableNames:
    """The names of one conversation database: its file ``<storage_name>.db`` and its tables.

    The tables are named after the sessions table ``<storage_name>_sessions``.
    This is the one place that pairs a database's name with its file and with that table.
    """

    storage_name: str

    @classmethod
    def of_database(cls, database: Path) -> TableNames:
        """Return the names of the database held in the file ``database``, the inverse of ``database_path``."""
        return cls(database.name.removesuffix(_DATABASE_SUFFIX))

    def database_path(self, state_root: Path) -> Path:
        """Return the file ``<state_root>/sessions/<storage_name>.db`` of the database under ``state_root``."""
        directory = state_root / _SESSIONS_DIRECTORY
        database = directory / f"{self.storage_name}{_DATABASE_SUFFIX}"
        if not self.storage_name or database.parent != directory:
            msg = "Session storage name must identify a file directly inside its sessions directory"
            raise ValueError(msg)
        return database

    @property
    def sessions(self) -> str:
        """The sessions table."""
        return f"{self.storage_name}_sessions"

    @property
    def runs(self) -> str:
        """The runs table."""
        # TODO(simplify): only the storage name `agno` keeps a runs table that is not `<sessions>_runs`.
        # Name every runs table `<sessions>_runs`, renaming `agno_runs` once.
        # Stored data of an agent named `agno` would notice. (#3075)
        return "agno_runs" if self.storage_name == "agno" else f"{self.sessions}_runs"

    @property
    def summaries(self) -> str:
        """The summary-row table."""
        return summary_rows_table_names(self.sessions)[0]

    @property
    def removed_runs(self) -> str:
        """The table of run ids a cut removed."""
        return summary_rows_table_names(self.sessions)[1]

    @property
    def usage(self) -> str:
        """The usage ledger."""
        return usage_table_name(self.sessions)

    @property
    def log_meta(self) -> str:
        """Storage v2's one-row table of the layout version and the run counter."""
        return f"{self.sessions}_log_meta"

    @property
    def log_runs(self) -> str:
        """Storage v2's runs table, one row per run without its messages."""
        return f"{self.sessions}_log_runs"

    @property
    def log_messages(self) -> str:
        """Storage v2's messages table, one row per stored message of a run."""
        return f"{self.sessions}_log_messages"

    @property
    def log_summaries(self) -> str:
        """Storage v2's summary-row table."""
        return f"{self.sessions}_log_summaries"

    @property
    def log_removed(self) -> str:
        """Storage v2's table of run ids a cut removed."""
        return f"{self.sessions}_log_removed"


def schema_sql(names: TableNames) -> tuple[str, ...]:
    """Return the idempotent DDL that creates every table and index a conversation database has."""
    return (*run_log_sql(names), *summary_rows_schema_sql(names.sessions), usage_table_sql(names.sessions))


def run_log_sql(names: TableNames) -> tuple[str, ...]:
    """Return the idempotent DDL of the sessions and runs tables alone, in Agno 3's layout.

    Migrations that recognize an older database by a missing summary-row or usage table create only these.
    """
    sessions, runs = quote_identifier(names.sessions), quote_identifier(names.runs)
    return (
        f"CREATE TABLE IF NOT EXISTS {sessions} ({_SESSION_COLUMN_SQL}, PRIMARY KEY (session_id))",
        _index_sql(names.sessions, "session_type"),
        _index_sql(names.sessions, "created_at"),
        f"CREATE TABLE IF NOT EXISTS {runs} (run_id VARCHAR NOT NULL, session_id VARCHAR NOT NULL, "
        "run_type VARCHAR NOT NULL, agent_id VARCHAR, team_id VARCHAR, workflow_id VARCHAR, user_id VARCHAR, "
        "parent_run_id VARCHAR, status VARCHAR, run_index BIGINT, run_data JSON NOT NULL, "
        "created_at BIGINT NOT NULL, updated_at BIGINT, PRIMARY KEY (run_id), "
        f"FOREIGN KEY(session_id) REFERENCES {sessions} (session_id) ON DELETE CASCADE)",
        *(_index_sql(names.runs, column) for column in _RUNS_INDEXED_COLUMNS),
        _index_sql(names.runs, "session_id", "run_index"),
    )


def log_schema_sql(names: TableNames) -> tuple[str, ...]:
    """Return the idempotent DDL of a storage v2 database, new or converted, with its meta row.

    It creates the nine-column sessions table, the five log tables and their indexes, and the usage ledger, each
    only when missing, and the meta row (version 2, ``next_seq`` 1) only when the table has none, so it leaves a
    converted database's counter and existing tables alone.
    """
    sessions = quote_identifier(names.sessions)
    meta, runs = quote_identifier(names.log_meta), quote_identifier(names.log_runs)
    summaries = quote_identifier(names.log_summaries)
    return (
        f"CREATE TABLE IF NOT EXISTS {sessions} ({_LOG_SESSION_COLUMN_SQL}, PRIMARY KEY (session_id))",
        _index_sql(names.sessions, "session_type"),
        # The constant key keeps the table at one row.
        f"CREATE TABLE IF NOT EXISTS {meta} (id INTEGER PRIMARY KEY CHECK (id = 1), version INTEGER NOT NULL, "
        "next_seq INTEGER NOT NULL)",
        f"INSERT OR IGNORE INTO {meta} (id, version, next_seq) VALUES (1, {LOG_VERSION}, 1)",  # noqa: S608
        # The JSON columns come last, the metadata a cut scans first, so scans of the small columns skip the
        # overflow pages of the large ones.
        f"CREATE TABLE IF NOT EXISTS {runs} (run_id TEXT NOT NULL PRIMARY KEY, "
        f"session_id TEXT NOT NULL REFERENCES {sessions}(session_id) ON DELETE CASCADE, parent_run_id TEXT, "
        "kind TEXT NOT NULL, scope_key TEXT NOT NULL, status TEXT NOT NULL, seq INTEGER NOT NULL, "
        "folded_by INTEGER, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, metadata TEXT, "
        "record TEXT NOT NULL, requirements TEXT, tools TEXT)",
        _index_sql(names.log_runs, "session_id", "seq"),
        _index_sql(names.log_runs, "parent_run_id"),
        # A rowid table: messages are often larger than the 1/20 of a page up to which SQLite advises WITHOUT ROWID.
        f"CREATE TABLE IF NOT EXISTS {quote_identifier(names.log_messages)} ("
        f"run_id TEXT NOT NULL REFERENCES {runs}(run_id) ON DELETE CASCADE, message_id TEXT NOT NULL, "
        "ord INTEGER NOT NULL, folded_by INTEGER, data TEXT NOT NULL, PRIMARY KEY (run_id, message_id))",
        # AUTOINCREMENT never hands out the id of a deleted newest row again, which an append's compare relies on.
        f"CREATE TABLE IF NOT EXISTS {summaries} (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        f"session_id TEXT NOT NULL REFERENCES {sessions}(session_id) ON DELETE CASCADE, scope_key TEXT NOT NULL, "
        "through INTEGER NOT NULL, legacy INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL, fold_run_id TEXT, "
        "summary TEXT, event_ids TEXT NOT NULL DEFAULT '[]', seen_event_ids TEXT NOT NULL DEFAULT '[]', "
        "fold_event_ids TEXT, fold_seen_event_ids TEXT)",
        _index_sql(names.log_summaries, "session_id", "scope_key", "id"),
        f"CREATE TABLE IF NOT EXISTS {quote_identifier(names.log_removed)} (run_id TEXT NOT NULL PRIMARY KEY, "
        f"session_id TEXT NOT NULL REFERENCES {sessions}(session_id) ON DELETE CASCADE)",
        usage_table_sql(names.sessions),
    )


def table_names(connection: sqlite3.Connection) -> set[str]:
    """Return the names of the database's tables; an older database lacks the ones its migrations add."""
    return {name for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _index_sql(table: str, *columns: str) -> str:
    name = quote_identifier(f"idx_{table}_{'_'.join(columns)}")
    return f"CREATE INDEX IF NOT EXISTS {name} ON {quote_identifier(table)} ({', '.join(columns)})"


__all__ = [
    "LOG_VERSION",
    "SESSION_COLUMNS",
    "TableNames",
    "log_schema_sql",
    "run_log_sql",
    "schema_sql",
    "table_names",
]
