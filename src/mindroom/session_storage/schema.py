"""File and table names and layout of a conversation database.

A database holds its sessions table, the five ``*_log_*`` tables of the run log, which hold a run's messages as rows
of their own, and the usage ledger, whose DDL lives with its owner. ``*_log_meta`` records the layout version, which
the Agno layout a conversion starts from (``legacy_agno3_schema``) lacks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.history.run_visibility import NON_VISIBLE_RUN_STATUSES
from mindroom.usage.storage import quote_identifier, usage_table_name, usage_table_sql

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

# The sessions table's columns and their types, each one a current writer or reader uses; the schema preflight
# requires them of an existing database.
_SESSION_COLUMN_TYPES = {
    "session_id": "VARCHAR NOT NULL",
    "session_type": "VARCHAR NOT NULL",
    "agent_id": "VARCHAR",
    "team_id": "VARCHAR",
    "user_id": "VARCHAR",
    "session_data": "JSON",
    "metadata": "JSON",
    "created_at": "BIGINT NOT NULL",
    "updated_at": "BIGINT",
}
SESSION_COLUMNS = tuple(_SESSION_COLUMN_TYPES)
_SESSION_COLUMN_SQL = ", ".join(f"{column} {column_type}" for column, column_type in _SESSION_COLUMN_TYPES.items())
# The layout version ``<sessions>_log_meta`` records.
LOG_VERSION = 2
# The statuses of a run that does not replay as model history, as stored, and as an SQL list.
NON_VISIBLE_RUN_STATUS_VALUES = tuple(sorted(status.value for status in NON_VISIBLE_RUN_STATUSES))
NON_VISIBLE_RUN_STATUS_SQL = ", ".join(f"'{status}'" for status in NON_VISIBLE_RUN_STATUS_VALUES)
_SESSIONS_DIRECTORY = "sessions"
_DATABASE_SUFFIX = ".db"


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
    def usage(self) -> str:
        """The usage ledger."""
        return usage_table_name(self.sessions)

    @property
    def log_meta(self) -> str:
        """The one-row table of the layout version and the run counter."""
        return f"{self.sessions}_log_meta"

    @property
    def log_runs(self) -> str:
        """The runs table, one row per run without its messages."""
        return f"{self.sessions}_log_runs"

    @property
    def log_messages(self) -> str:
        """The messages table, one row per stored message of a run."""
        return f"{self.sessions}_log_messages"

    @property
    def log_summaries(self) -> str:
        """The summary-row table."""
        return f"{self.sessions}_log_summaries"

    @property
    def log_removed(self) -> str:
        """The table of run ids a cut removed."""
        return f"{self.sessions}_log_removed"


def log_schema_sql(names: TableNames) -> tuple[str, ...]:
    """Return the idempotent DDL of a conversation database, new or converted, with its meta row.

    It creates the nine-column sessions table, the five log tables and their indexes, and the usage ledger, each
    only when missing, and the meta row (version 2, ``next_seq`` 1) only when the table has none, so it leaves a
    converted database's counter and existing tables alone.
    """
    sessions = quote_identifier(names.sessions)
    meta, runs = quote_identifier(names.log_meta), quote_identifier(names.log_runs)
    summaries = quote_identifier(names.log_summaries)
    return (
        f"CREATE TABLE IF NOT EXISTS {sessions} ({_SESSION_COLUMN_SQL}, PRIMARY KEY (session_id))",
        _index_sql(names.sessions, "session_type"),
        # The constant key keeps the table at one row.
        f"CREATE TABLE IF NOT EXISTS {meta} (id INTEGER PRIMARY KEY CHECK (id = 1), version INTEGER NOT NULL, "
        "next_seq INTEGER NOT NULL)",
        f"INSERT OR IGNORE INTO {meta} (id, version, next_seq) VALUES (1, {LOG_VERSION}, 1)",  # noqa: S608
        # The JSON columns come last, the metadata a cut scans first among them, so scans of the small columns and
        # of the metadata skip the overflow pages of the larger ones.
        f"CREATE TABLE IF NOT EXISTS {runs} (run_id TEXT NOT NULL PRIMARY KEY, "
        f"session_id TEXT NOT NULL REFERENCES {sessions}(session_id) ON DELETE CASCADE, parent_run_id TEXT, "
        "kind TEXT NOT NULL, scope_key TEXT NOT NULL, status TEXT NOT NULL, seq INTEGER NOT NULL, "
        "folded_by INTEGER, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, metadata TEXT, "
        "record TEXT NOT NULL, requirements TEXT, tools TEXT)",
        _index_sql(names.log_runs, "session_id", "seq"),
        _index_sql(names.log_runs, "parent_run_id"),
        # Not WITHOUT ROWID: SQLite recommends that only for rows under about a twentieth of a page, and messages are
        # often larger.
        f"CREATE TABLE IF NOT EXISTS {quote_identifier(names.log_messages)} ("
        f"run_id TEXT NOT NULL REFERENCES {runs}(run_id) ON DELETE CASCADE, message_id TEXT NOT NULL, "
        "ord INTEGER NOT NULL, folded_by INTEGER, data TEXT NOT NULL, PRIMARY KEY (run_id, message_id))",
        # Serves reading a run's messages in order without a sort, which spills a large run to a temp file.
        _index_sql(names.log_messages, "run_id", "ord"),
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


def owner_scope_key(session_type: object, agent_id: object, team_id: object) -> str | None:
    """Return the key of the scope that owns a session row and its replayed summary, if the row names one."""
    if session_type == "team" and isinstance(team_id, str) and team_id:
        return f"team:{team_id}"
    if isinstance(agent_id, str) and agent_id:
        return f"agent:{agent_id}"
    return None


def table_names(connection: sqlite3.Connection) -> set[str]:
    """Return the names of the database's tables; an older database lacks the ones its migrations add."""
    return {name for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _index_sql(table: str, *columns: str) -> str:
    name = quote_identifier(f"idx_{table}_{'_'.join(columns)}")
    return f"CREATE INDEX IF NOT EXISTS {name} ON {quote_identifier(table)} ({', '.join(columns)})"


__all__ = [
    "LOG_VERSION",
    "NON_VISIBLE_RUN_STATUS_SQL",
    "NON_VISIBLE_RUN_STATUS_VALUES",
    "SESSION_COLUMNS",
    "TableNames",
    "log_schema_sql",
    "owner_scope_key",
    "table_names",
]
