"""Read-only reads of a conversation database's sessions and usage ledger, for usage reports.

A report reads databases worker code can write, so these reads check nothing they return: each value comes back as
stored for the caller to validate, and a table or column the reads need that is missing is reported, not failed on.
They never create, convert, or write a database.

``read_stored_usage`` scans sessions and usage in bounded ``rowid`` chunks using short-lived read transactions
rather than one database-wide snapshot, releasing SQLite's ``SHARED`` lock between chunks so writers in ``DELETE``
journal mode can commit without waiting on a full scan.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING, Literal, NamedTuple

from mindroom.session_storage.schema import quote_identifier, table_columns, table_names
from mindroom.sqlite_utils import begun

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

    from mindroom.session_storage.schema import TableNames

type UnreadableLayout = Literal["session table unavailable", "session schema unsupported", "usage schema unsupported"]

SESSION_CHUNK_SIZE = 500
USAGE_CHUNK_SIZE = 200

_SESSION_COLUMNS = frozenset({"session_id", "user_id"})
_META_COLUMNS = frozenset({"version", "owner_kind", "owner_id"})
_USAGE_COLUMNS = frozenset({"id", "session_id", "run_id", "usage_data"})
_ROWID_ALIASES = frozenset({"rowid", "oid", "_rowid_"})
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


class StoredUsageRead(NamedTuple):
    """The raw metadata, sessions, and per-session usage payloads collected by one rolling chunked read."""

    meta: RecordedMeta | None
    has_usage: bool
    sessions: list[StoredSession]
    usage_by_session: dict[object, list[object]]


def _has_rowid(connection: sqlite3.Connection, table: str) -> bool:
    try:
        connection.execute(f"SELECT rowid FROM {quote_identifier(table)} LIMIT 0")  # noqa: S608
    except sqlite3.OperationalError as error:
        if "no such column" in str(error).lower():
            return False
        raise
    return True


def _has_supported_columns(connection: sqlite3.Connection, table: str, required: frozenset[str]) -> bool:
    columns = table_columns(connection, table)
    return (
        required.issubset(columns)
        and not ({column.lower() for column in columns} & _ROWID_ALIASES)
        and _has_rowid(connection, table)
    )


def unreadable_layout(connection: sqlite3.Connection, names: TableNames) -> UnreadableLayout | None:
    """Return why the database lacks a table or column these reads need, or None when it has them all.

    The sessions table is required; the usage ledger only when the database has one. Tables that shadow SQLite's
    built-in ``rowid`` or use ``WITHOUT ROWID`` are reported as unsupported so chunked ``rowid`` paging stays sound.
    """
    tables = table_names(connection)
    if names.sessions not in tables:
        return "session table unavailable"
    if not _has_supported_columns(connection, names.sessions, _SESSION_COLUMNS):
        return "session schema unsupported"
    if names.usage in tables and not _has_supported_columns(connection, names.usage, _USAGE_COLUMNS):
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


def _fetch_chunk(
    connection: sqlite3.Connection,
    query: str,
    parameters: tuple[object, ...],
) -> list[tuple[object, ...]]:
    with begun(connection, write=False):
        return connection.execute(query, parameters).fetchall()


def _iter_rowid_chunks(
    connection: sqlite3.Connection,
    query_template: str,
    *,
    max_rowid: int,
    chunk_size: int,
    prefix_params: tuple[object, ...] = (),
) -> Iterator[list[tuple[object, ...]]]:
    last_rowid: int | None = None
    while True:
        bound_sql = "rowid <= ?" if last_rowid is None else "rowid > ? AND rowid <= ?"
        bound_params: tuple[int, ...] = (max_rowid,) if last_rowid is None else (last_rowid, max_rowid)
        batch = _fetch_chunk(
            connection,
            query_template.format(bound=bound_sql),
            (*prefix_params, *bound_params, chunk_size),
        )
        if not batch:
            return
        yield batch
        next_rowid = batch[-1][0]
        if not isinstance(next_rowid, int) or (last_rowid is not None and next_rowid <= last_rowid):
            msg = "unexpected non-integer rowid cursor"
            raise sqlite3.DatabaseError(msg)
        last_rowid = next_rowid
        if len(batch) < chunk_size or last_rowid >= max_rowid:
            return


def _usage_chunk_template(usage_table: str, *, since: float | None) -> tuple[str, tuple[object, ...]]:
    if since is None:
        return (
            f"SELECT rowid, session_id, usage_data, 1 FROM {usage_table} WHERE {{bound}} ORDER BY rowid LIMIT ?",  # noqa: S608
            (),
        )
    # Bound raw scanned rows by rowid before evaluating ``since`` so sparse date ranges release the read lock.
    return (
        "SELECT rowid, session_id, CASE WHEN matched THEN usage_data ELSE NULL END, matched FROM ("  # noqa: S608
        f"SELECT rowid, session_id, usage_data, CASE WHEN {_USAGE_SINCE_CONDITION} THEN 1 ELSE 0 END AS matched "
        f"FROM {usage_table} WHERE {{bound}} ORDER BY rowid LIMIT ?) ORDER BY rowid",
        (since,),
    )


def _read_all_sessions(
    connection: sqlite3.Connection,
    sessions_table: str,
    max_session_rowid: int | None,
) -> list[StoredSession]:
    if max_session_rowid is None:
        return []
    session_sql = f"SELECT rowid, session_id, user_id FROM {sessions_table} WHERE {{bound}} ORDER BY rowid LIMIT ?"  # noqa: S608
    return [
        StoredSession(session_id, user_id)
        for batch in _iter_rowid_chunks(
            connection,
            session_sql,
            max_rowid=max_session_rowid,
            chunk_size=SESSION_CHUNK_SIZE,
        )
        for _rowid, session_id, user_id in batch
    ]


def _read_sessions_by_ids(
    connection: sqlite3.Connection,
    sessions_table: str,
    session_ids: Sequence[object],
) -> list[StoredSession]:
    matched_rows: list[tuple[int, StoredSession]] = []
    for offset in range(0, len(session_ids), SESSION_CHUNK_SIZE):
        id_batch = session_ids[offset : offset + SESSION_CHUNK_SIZE]
        placeholders = ", ".join("?" for _ in id_batch)
        query = (
            f"SELECT rowid, session_id, user_id FROM {sessions_table} "  # noqa: S608
            f"WHERE session_id IN ({placeholders}) ORDER BY rowid"
        )
        for rowid, session_id, user_id in _fetch_chunk(connection, query, tuple(id_batch)):
            if not isinstance(rowid, int):
                msg = "unexpected non-integer rowid cursor"
                raise sqlite3.DatabaseError(msg)
            matched_rows.append((rowid, StoredSession(session_id, user_id)))
    if len(session_ids) > SESSION_CHUNK_SIZE:
        matched_rows.sort(key=lambda item: item[0])
    return [session for _rowid, session in matched_rows]


def _read_usage_by_session(
    connection: sqlite3.Connection,
    usage_table: str,
    *,
    max_usage_rowid: int,
    since: float | None,
) -> dict[object, list[object]]:
    usage_sql, prefix_params = _usage_chunk_template(usage_table, since=since)
    usage_by_session: dict[object, list[object]] = {}
    for batch in _iter_rowid_chunks(
        connection,
        usage_sql,
        max_rowid=max_usage_rowid,
        chunk_size=USAGE_CHUNK_SIZE,
        prefix_params=prefix_params,
    ):
        for _rowid, session_id, payload, matched in batch:
            if matched:
                usage_by_session.setdefault(session_id, []).append(payload)
    return usage_by_session


def read_stored_usage(
    path: Path,
    names: TableNames,
    *,
    since: float | None,
) -> StoredUsageRead | UnreadableLayout:
    """Read a database's sessions and usage ledger in bounded chunks without holding one long read lock.

    Each chunk commits its short read transaction before the next chunk starts, so concurrent writers in ``DELETE``
    journal mode can commit between chunks. ``since`` keeps only usage snapshots last written at or after that
    epoch time; snapshots whose data is not valid JSON stay included for the caller to report.
    """
    sessions_table = quote_identifier(names.sessions)
    usage_table = quote_identifier(names.usage)
    uri = f"{path.resolve().as_uri()}?mode=ro&cache=private"
    with closing(sqlite3.connect(uri, uri=True, timeout=1.0, isolation_level=None)) as connection:
        connection.execute("PRAGMA query_only = ON")
        with begun(connection, write=False):
            problem = unreadable_layout(connection, names)
            if problem is not None:
                return problem
            has_usage = names.usage in table_names(connection)
            meta = recorded_meta(connection, names)
            (max_session_rowid,) = connection.execute(f"SELECT MAX(rowid) FROM {sessions_table}").fetchone()  # noqa: S608
            max_usage_rowid = (
                connection.execute(f"SELECT MAX(rowid) FROM {usage_table}").fetchone()[0]  # noqa: S608
                if has_usage
                else None
            )

        if since is not None and has_usage:
            if max_session_rowid is None or max_usage_rowid is None:
                return StoredUsageRead(meta=meta, has_usage=has_usage, sessions=[], usage_by_session={})
            usage_by_session = _read_usage_by_session(
                connection,
                usage_table,
                max_usage_rowid=max_usage_rowid,
                since=since,
            )
            session_ids = [sid for sid in usage_by_session if sid is not None]
            sessions = _read_sessions_by_ids(connection, sessions_table, session_ids) if session_ids else []
            return StoredUsageRead(meta=meta, has_usage=has_usage, sessions=sessions, usage_by_session=usage_by_session)

        sessions = _read_all_sessions(connection, sessions_table, max_session_rowid)
        usage_by_session = (
            _read_usage_by_session(connection, usage_table, max_usage_rowid=max_usage_rowid, since=None)
            if has_usage and max_usage_rowid is not None and sessions
            else {}
        )

    return StoredUsageRead(meta=meta, has_usage=has_usage, sessions=sessions, usage_by_session=usage_by_session)


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
    "SESSION_CHUNK_SIZE",
    "USAGE_CHUNK_SIZE",
    "RecordedMeta",
    "StoredSession",
    "StoredUsageRead",
    "UnreadableLayout",
    "read_stored_usage",
    "recorded_meta",
    "stored_usage",
    "unreadable_layout",
]
