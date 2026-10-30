"""File and table names and layout of a conversation database.

A database holds its sessions table, the six ``*_log_*`` tables of the run log, which hold a run's messages and tool
executions as rows of their own, and the usage ledger. ``*_log_meta`` records the layout version and the database's
owner, which the Agno layout a conversion starts from (``legacy_agno3_schema``) lacks.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.history.run_visibility import NON_VISIBLE_RUN_STATUSES

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

    from mindroom.history.types import HistoryScope

# The sessions table's columns and their types, in the order a database converted from an older layout holds them.
# ``session_data`` held the session's usage totals and no current writer fills it; it stays because older releases'
# preflight requires it on a rollback, and the step to layout version 7 reads the totals stored there once.
_SESSION_COLUMN_TYPES = {
    "session_id": "VARCHAR NOT NULL",
    "user_id": "VARCHAR",
    "session_data": "JSON",
    "metadata": "JSON",
    "created_at": "BIGINT NOT NULL",
    "updated_at": "BIGINT",
    "session_state": "JSON",
}
SESSION_COLUMNS = tuple(_SESSION_COLUMN_TYPES)
_SESSION_COLUMN_SQL = ", ".join(f"{column} {column_type}" for column, column_type in _SESSION_COLUMN_TYPES.items())
# The layout version ``<sessions>_log_meta`` records: 2 for storage v2, 3 once an agent's session rows name that agent
# (``legacy_agent_session_owners``), 4 once the meta row names the database's owner and each session row holds its
# state in a column of its own (``legacy_session_columns``), 5 once every delegation child its runs' metadata holds has
# the current shape (``delegation.legacy_snapshots``), 6 once the paused runs earlier releases stored are rewritten
# (``legacy_paused_runs``), and 7 once each session row holds its owner's history state in one metadata value
# (``legacy_scope_metadata``) and the usage ledger holds the usage each session's stored totals counted beyond it
# (``legacy_usage_totals``).
LOG_VERSION = 7
# The first version whose meta row names the database's owner.
OWNER_VERSION = 4
# The first version whose ledger rows sum to each session's totals: the step to it keeps what a session's stored totals
# counted beyond its run rows as a row of its own (``legacy_usage_totals``).
LEDGER_TOTALS_VERSION = 7
_TEAM_STORAGE_NAME_PATTERN = re.compile(r"[^a-zA-Z0-9_]+")
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
    def of_team(cls, scope: HistoryScope) -> TableNames:
        """Return the names of team scope ``scope``'s database, ``team_<id>_<digest of the scope key>``, one per scope."""
        normalized_scope_id = _TEAM_STORAGE_NAME_PATTERN.sub("_", scope.scope_id).strip("_") or "team"
        return cls(f"team_{normalized_scope_id}_{hashlib.sha256(scope.key.encode()).hexdigest()[:12]}")

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
        return f"{self.sessions}_usage"

    @property
    def log_meta(self) -> str:
        """The one-row table of the layout version, the run counter, and the database's owner."""
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
    def log_tools(self) -> str:
        """The tool-execution table, one row per tool execution of a run."""
        return f"{self.sessions}_log_tools"

    @property
    def log_summaries(self) -> str:
        """The summary-row table."""
        return f"{self.sessions}_log_summaries"

    @property
    def log_removed(self) -> str:
        """The table of run ids a cut removed."""
        return f"{self.sessions}_log_removed"


def log_schema_sql(names: TableNames) -> tuple[str, ...]:
    """Return the idempotent DDL of a conversation database, new or converted, without its meta row.

    It creates the seven-column sessions table, the six log tables and their indexes, and the usage ledger, each only
    when missing, so it leaves a converted database's existing tables alone. ``create_log_schema`` adds the meta row.
    """
    sessions = quote_identifier(names.sessions)
    meta, runs = quote_identifier(names.log_meta), quote_identifier(names.log_runs)
    summaries = quote_identifier(names.log_summaries)
    return (
        f"CREATE TABLE IF NOT EXISTS {sessions} ({_SESSION_COLUMN_SQL}, PRIMARY KEY (session_id))",
        # The constant key keeps the table at one row. The owner columns take NULL only because a database converted
        # from an older layout adds them to the table it has; every write names the owner.
        f"CREATE TABLE IF NOT EXISTS {meta} (id INTEGER PRIMARY KEY CHECK (id = 1), version INTEGER NOT NULL, "
        "next_seq INTEGER NOT NULL, owner_kind TEXT, owner_id TEXT)",
        # The JSON columns come last, the metadata a cut scans first among them, so scans of the small columns and
        # of the metadata skip the overflow pages of the larger ones. ``has_tools`` keeps a record's empty tool list
        # apart from none; its entries are rows of their own.
        f"CREATE TABLE IF NOT EXISTS {runs} (run_id TEXT NOT NULL PRIMARY KEY, "
        f"session_id TEXT NOT NULL REFERENCES {sessions}(session_id) ON DELETE CASCADE, parent_run_id TEXT, "
        "kind TEXT NOT NULL, scope_key TEXT NOT NULL, status TEXT NOT NULL, seq INTEGER NOT NULL, "
        "folded_by INTEGER, has_tools INTEGER NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, "
        "metadata TEXT, record TEXT NOT NULL, requirements TEXT)",
        index_sql(names.log_runs, "session_id", "seq"),
        index_sql(names.log_runs, "parent_run_id"),
        # Not WITHOUT ROWID: SQLite recommends that only for rows under about a twentieth of a page, and messages are
        # often larger.
        f"CREATE TABLE IF NOT EXISTS {quote_identifier(names.log_messages)} ("
        f"run_id TEXT NOT NULL REFERENCES {runs}(run_id) ON DELETE CASCADE, message_id TEXT NOT NULL, "
        "ord INTEGER NOT NULL, folded_by INTEGER, data TEXT NOT NULL, PRIMARY KEY (run_id, message_id))",
        # Serves reading a run's messages in order without a sort, which spills a large run to a temp file.
        index_sql(names.log_messages, "run_id", "ord"),
        # Rows of their own, so a save that adds a tool call writes that call alone rather than the run's whole list.
        f"CREATE TABLE IF NOT EXISTS {quote_identifier(names.log_tools)} ("
        f"run_id TEXT NOT NULL REFERENCES {runs}(run_id) ON DELETE CASCADE, ord INTEGER NOT NULL, "
        "data TEXT NOT NULL, PRIMARY KEY (run_id, ord))",
        # AUTOINCREMENT never hands out the id of a deleted newest row again, which an append's compare relies on.
        f"CREATE TABLE IF NOT EXISTS {summaries} (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        f"session_id TEXT NOT NULL REFERENCES {sessions}(session_id) ON DELETE CASCADE, scope_key TEXT NOT NULL, "
        "through INTEGER NOT NULL, legacy INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL, fold_run_id TEXT, "
        "summary TEXT, event_ids TEXT NOT NULL DEFAULT '[]', seen_event_ids TEXT NOT NULL DEFAULT '[]', "
        "fold_event_ids TEXT, fold_seen_event_ids TEXT)",
        index_sql(names.log_summaries, "session_id", "scope_key", "id"),
        f"CREATE TABLE IF NOT EXISTS {quote_identifier(names.log_removed)} (run_id TEXT NOT NULL PRIMARY KEY, "
        f"session_id TEXT NOT NULL REFERENCES {sessions}(session_id) ON DELETE CASCADE)",
        usage_table_sql(names),
    )


def usage_table_sql(names: TableNames) -> str:
    """Create the usage ledger; its publication also marks completed initial seeding."""
    return (
        f"CREATE TABLE IF NOT EXISTS {quote_identifier(names.usage)} ("
        "id INTEGER PRIMARY KEY, "
        f"session_id TEXT NOT NULL REFERENCES {quote_identifier(names.sessions)}(session_id) ON DELETE CASCADE, "
        "run_id TEXT, usage_data TEXT, UNIQUE(session_id, run_id))"
    )


def usage_upsert_sql(names: TableNames) -> str:
    """Replace a saved run's snapshot; missing historical IDs remain distinct rows."""
    return (
        f"INSERT INTO {quote_identifier(names.usage)} (session_id, run_id, usage_data) "  # noqa: S608
        "VALUES (?, ?, ?) ON CONFLICT(session_id, run_id) DO UPDATE SET usage_data = excluded.usage_data"
    )


def create_log_schema(connection: sqlite3.Connection, names: TableNames, owner: HistoryScope) -> None:
    """Create every missing table of a conversation database and, when it has none, the meta row naming ``owner``.

    A new meta row records version ``LOG_VERSION`` and ``next_seq`` 1; a database that has one keeps it.
    """
    for statement in log_schema_sql(names):
        connection.execute(statement)
    connection.execute(
        f"INSERT OR IGNORE INTO {quote_identifier(names.log_meta)} (id, version, next_seq, owner_kind, owner_id) "  # noqa: S608
        "VALUES (1, ?, 1, ?, ?)",
        (LOG_VERSION, owner.kind, owner.scope_id),
    )


def layout_version(connection: sqlite3.Connection, names: TableNames) -> int | None:
    """Return the layout version the database's meta row records, or None for a database in an older layout."""
    return meta_version(connection, names) if is_current_layout(connection, names) else None


def meta_version(connection: sqlite3.Connection, names: TableNames) -> int:
    """Return the layout version the meta row of a database in the current layout records."""
    (version,) = connection.execute(f"SELECT version FROM {quote_identifier(names.log_meta)} WHERE id = 1").fetchone()  # noqa: S608
    return version


def table_names(connection: sqlite3.Connection) -> set[str]:
    """Return the names of the database's tables; an older database lacks the ones its migrations add."""
    return {name for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    """Return the names of ``table``'s columns, or none when the database lacks the table."""
    return {row[1] for row in connection.execute(f"PRAGMA table_info({quote_identifier(table)})")}


def is_current_layout(connection: sqlite3.Connection, names: TableNames) -> bool:
    """Return whether the database holds ``*_log_meta``, the marker of the current layout."""
    query = "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?"
    return connection.execute(query, (names.log_meta,)).fetchone() is not None


def quote_identifier(value: str) -> str:
    """Quote a SQLite identifier supplied by the storage owner."""
    return '"' + value.replace('"', '""') + '"'


def index_name(table: str, *columns: str) -> str:
    """Return the name of ``table``'s index over ``columns``, the one rule both layouts' indexes follow."""
    return f"idx_{table}_{'_'.join(columns)}"


def index_sql(table: str, *columns: str) -> str:
    """Return the idempotent DDL of ``table``'s index over ``columns``."""
    name = quote_identifier(index_name(table, *columns))
    return f"CREATE INDEX IF NOT EXISTS {name} ON {quote_identifier(table)} ({', '.join(columns)})"


__all__ = [
    "LEDGER_TOTALS_VERSION",
    "LOG_VERSION",
    "NON_VISIBLE_RUN_STATUS_SQL",
    "NON_VISIBLE_RUN_STATUS_VALUES",
    "OWNER_VERSION",
    "SESSION_COLUMNS",
    "TableNames",
    "create_log_schema",
    "index_name",
    "index_sql",
    "is_current_layout",
    "layout_version",
    "log_schema_sql",
    "meta_version",
    "quote_identifier",
    "table_columns",
    "table_names",
    "usage_table_sql",
    "usage_upsert_sql",
]
