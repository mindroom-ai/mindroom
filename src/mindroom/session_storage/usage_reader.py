"""Read-only reads of a conversation database's sessions and usage ledger, for usage reports.

A report reads databases worker code can write, so these reads check nothing they return: each value comes back as
stored for the caller to validate, and a table or column the reads need that is missing is reported, not failed on.
They never create, convert, or write a database.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import TYPE_CHECKING, Literal, NamedTuple

from mindroom.session_storage.schema import quote_identifier, table_columns, table_names

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mindroom.session_storage.schema import TableNames

type UnreadableLayout = Literal["session table unavailable", "session schema unsupported", "usage schema unsupported"]

_SESSION_COLUMNS = frozenset({"session_id", "user_id"})
_META_COLUMNS = frozenset({"version", "owner_kind", "owner_id"})
_USAGE_COLUMNS = frozenset({"id", "session_id", "run_id", "usage_data"})
# A dated read keeps snapshots last written in range, so a run resumed after ``since`` keeps its new requests.
# Malformed snapshots stay in a dated read, so the reader reports them instead of SQLite failing the whole store.
_USAGE_SINCE_CONDITION = (
    "CASE WHEN json_valid(usage_data) THEN "
    "COALESCE(json_extract(usage_data, '$.updated_at'), json_extract(usage_data, '$.created_at')) >= ? ELSE 1 END"
)


class StoredSession(NamedTuple):
    """A session row as stored: its id and its requester, neither of them checked."""

    session_id: object
    user_id: object


class RecordedMeta(NamedTuple):
    """What a database's meta row records, unchecked: its owner's kind and id, and its layout version."""

    owner_kind: object
    owner_id: object
    version: object


@contextmanager
def read_only_snapshot(path: Path) -> Iterator[sqlite3.Connection]:
    """Open the database file ``path`` read-only, as one snapshot that waits at most a second for another's lock."""
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&cache=private", uri=True, timeout=1.0)
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        yield connection
    finally:
        connection.close()


def unreadable_layout(connection: sqlite3.Connection, names: TableNames) -> UnreadableLayout | None:
    """Return why the database lacks a table or column these reads need, or None when it has them all.

    The sessions table is required; the usage ledger only when the database has one.
    """
    tables = table_names(connection)
    if names.sessions not in tables:
        return "session table unavailable"
    if not _SESSION_COLUMNS.issubset(table_columns(connection, names.sessions)):
        return "session schema unsupported"
    if names.usage in tables and not _USAGE_COLUMNS.issubset(table_columns(connection, names.usage)):
        return "usage schema unsupported"
    return None


def recorded_meta(connection: sqlite3.Connection, names: TableNames) -> RecordedMeta | None:
    """Return what the meta row records, or None when the database records no owner."""
    meta = names.log_meta
    if meta not in table_names(connection) or not _META_COLUMNS.issubset(table_columns(connection, meta)):
        return None
    row = connection.execute(
        f"SELECT owner_kind, owner_id, version FROM {quote_identifier(meta)} WHERE id = 1",  # noqa: S608
    ).fetchone()
    return None if row is None else RecordedMeta(*row)


def stored_sessions(
    connection: sqlite3.Connection,
    names: TableNames,
    *,
    since: float | None,
) -> Iterator[StoredSession]:
    """Yield the database's session rows, with ``since`` only those with a usage snapshot last written at or after it.

    ``since`` needs the usage ledger.
    """
    query = f"SELECT session_id, user_id FROM {quote_identifier(names.sessions)}"  # noqa: S608
    parameters: tuple[object, ...] = ()
    if since is not None:
        query += (
            f" WHERE session_id IN (SELECT session_id FROM {quote_identifier(names.usage)} "  # noqa: S608
            f"WHERE {_USAGE_SINCE_CONDITION})"
        )
        parameters = (since,)
    for row in connection.execute(query, parameters):
        yield StoredSession(*row)


def stored_usage(
    connection: sqlite3.Connection,
    names: TableNames,
    session_id: str,
    *,
    since: float | None,
) -> Iterator[object]:
    """Yield the ``usage_data`` of session ``session_id``'s usage snapshots in ledger order, unchecked.

    ``since`` keeps only snapshots last written at or after it; one whose data is not JSON is always kept.
    """
    query = f"SELECT usage_data FROM {quote_identifier(names.usage)} WHERE session_id = ?"  # noqa: S608
    parameters: tuple[object, ...] = (session_id,)
    if since is not None:
        query += f" AND {_USAGE_SINCE_CONDITION}"
        parameters = (session_id, since)
    for (payload,) in connection.execute(query + " ORDER BY id", parameters):
        yield payload


__all__ = [
    "RecordedMeta",
    "StoredSession",
    "UnreadableLayout",
    "read_only_snapshot",
    "recorded_meta",
    "stored_sessions",
    "stored_usage",
    "unreadable_layout",
]
