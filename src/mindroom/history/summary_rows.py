"""Summary rows of the conversation log: appending compaction chunks, reading what they cover, and cutting.

A scope's history is Agno's runs table in ``run_index`` order with summary rows
between its runs. A row's key is ``(after_run_index, id)``; a top-level run's key
is ``(run_index, 0)``, and member runs follow their parent. Loading replays the
scope's latest summary row and the runs after it, so compaction only appends
rows and never moves or deletes a run. Edits and redactions cut: they delete
one element and everything after it, which also returns the runs a deleted
summary covered to replay.

Each row keeps the event ids of the runs its own chunk folded, so redaction can
check compacted history on every response without reading the folded runs.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

from agno.db.sqlite import SqliteDb

from mindroom.constants import (
    MATRIX_EVENT_ID_METADATA_KEY,
    MATRIX_RESPONSE_EVENT_ID_METADATA_KEY,
    MATRIX_SEEN_EVENT_IDS_METADATA_KEY,
    MATRIX_SOURCE_EVENT_IDS_METADATA_KEY,
    MATRIX_SOURCE_EVENT_REVISIONS_METADATA_KEY,
    MATRIX_TURN_DISCOVERY_EVENT_IDS_METADATA_KEY,
)
from mindroom.history.summary_rows_schema import (
    NON_VISIBLE_RUN_STATUS_VALUES,
    loads_sql,
    run_scope_key_sql,
    summary_rows_schema_sql,
    summary_rows_table_names,
)
from mindroom.session_storage import agno_compat_sqlite
from mindroom.usage.storage import quote_identifier

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence

    from agno.db.base import BaseDb
    from sqlalchemy import Connection

_ID_CHUNK_SIZE = 500

type _Key = tuple[int, int]


@dataclass(frozen=True)
class _SummaryRow:
    """One summary row of a scope."""

    id: int
    after_run_index: int
    summary: str | None
    legacy: bool
    event_ids: frozenset[str]


@dataclass(frozen=True)
class CoveredRun:
    """A run a compaction chunk read before its summary call, as it was stored then."""

    run_id: str
    run_index: int
    status: str | None

    @property
    def visible(self) -> bool:
        """Whether the run replays as model history, so a summary may fold it."""
        return self.status not in NON_VISIBLE_RUN_STATUS_VALUES


@dataclass(frozen=True)
class _CutResult:
    """What one cut removed."""

    # Whether a run the scope loads (not folded behind a summary) and that holds the event was removed.
    removed_loaded_holder: bool


@dataclass(frozen=True)
class _Tables:
    runs: str
    summaries: str
    removed: str


def source_event_ids(metadata: object) -> set[str]:
    """Return the Matrix event ids a run answered, from its metadata."""
    if not isinstance(metadata, dict):
        return set()
    metadata = cast("dict[str, Any]", metadata)
    event_ids: set[str] = set()
    event_id = metadata.get(MATRIX_EVENT_ID_METADATA_KEY)
    if isinstance(event_id, str) and event_id:
        event_ids.add(event_id)
    for key in (MATRIX_SOURCE_EVENT_IDS_METADATA_KEY, MATRIX_TURN_DISCOVERY_EVENT_IDS_METADATA_KEY):
        raw_ids = metadata.get(key)
        if isinstance(raw_ids, list):
            event_ids.update(candidate for candidate in raw_ids if isinstance(candidate, str) and candidate)
    return event_ids


def seen_event_ids(metadata: object) -> set[str]:
    """Return the Matrix event ids a run already represents, from its metadata."""
    if not isinstance(metadata, dict):
        return set()
    metadata = cast("dict[str, Any]", metadata)
    event_ids: set[str] = set()
    raw_seen_ids = metadata.get(MATRIX_SEEN_EVENT_IDS_METADATA_KEY)
    if isinstance(raw_seen_ids, list):
        event_ids.update(event_id for event_id in raw_seen_ids if isinstance(event_id, str) and event_id)
    raw_revisions = metadata.get(MATRIX_SOURCE_EVENT_REVISIONS_METADATA_KEY)
    if isinstance(raw_revisions, dict):
        event_ids.update(
            revision[1]
            for revision in raw_revisions.values()
            if isinstance(revision, list | tuple) and len(revision) == 2 and isinstance(revision[1], str)
        )
    response_event_id = metadata.get(MATRIX_RESPONSE_EVENT_ID_METADATA_KEY)
    if isinstance(response_event_id, str) and response_event_id:
        event_ids.add(response_event_id)
    return event_ids


def latest_row(storage: BaseDb, *, session_id: str, scope_key: str) -> _SummaryRow | None:
    """Return the scope's latest summary row, whose text replays."""
    db = _sqlite(storage)
    tables = _tables(db)
    with db.db_engine.begin() as connection:
        _ensure_tables(connection, db)
        rows = _rows(connection, tables, session_id=session_id, scope_key=scope_key, latest_only=True)
    return rows[-1] if rows else None


def covered_runs(storage: BaseDb, *, session_id: str, run_ids: Sequence[str]) -> list[CoveredRun]:
    """Return how the given runs are stored now, in log order, for a later append to check."""
    db = _sqlite(storage)
    tables = _tables(db)
    wanted = sorted({run_id for run_id in run_ids if run_id})
    found: list[CoveredRun] = []
    with db.db_engine.connect() as connection:
        for start in range(0, len(wanted), _ID_CHUNK_SIZE):
            chunk = wanted[start : start + _ID_CHUNK_SIZE]
            found.extend(
                CoveredRun(run_id=run_id, run_index=run_index, status=status)
                for run_id, run_index, status in connection.exec_driver_sql(
                    f"SELECT run_id, run_index, status FROM {tables.runs} "  # noqa: S608
                    f"WHERE session_id = ? AND run_id IN ({', '.join('?' for _ in chunk)})",
                    (session_id, *chunk),
                )
            )
    return sorted(found, key=lambda run: run.run_index)


def append_summary(
    storage: BaseDb,
    *,
    session_id: str,
    scope_key: str,
    based_on: int | None,
    covered: Sequence[CoveredRun],
    summary: str,
    summary_model: str,
) -> int | None:
    """Append one summary row after the newest covered run, unless its input went stale; return its id.

    The input is stale when the scope's latest row is no longer ``based_on``, a covered run is gone or
    changed status or place since it was read, or a covered run sorts at or behind the latest row. The
    row's event sets come from the covered rows as they are stored now, so metadata written during the
    summary call, such as a reply's response event id, counts.
    """
    db = _sqlite(storage)
    tables = _tables(db)
    with db.db_engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        _ensure_tables(connection, db)
        rows = _rows(connection, tables, session_id=session_id, scope_key=scope_key)
        previous_index = rows[-1].after_run_index if rows else -1
        # A row never sorts behind the latest one: that would return what it folded to replay.
        if (rows[-1].id if rows else None) != based_on or any(run.run_index <= previous_index for run in covered):
            return None
        stored = {
            run_id: (run_index, status, metadata)
            for run_id, run_index, status, metadata in _run_metadata(
                connection,
                tables,
                session_id=session_id,
                run_ids=[run.run_id for run in covered],
            )
        }
        if any(stored.get(run.run_id, (None, None, None))[:2] != (run.run_index, run.status) for run in covered):
            return None
        event_ids: set[str] = set()
        seen: set[str] = set()
        for _run_index, _status, metadata in stored.values():
            seen |= seen_event_ids(metadata)
            event_ids |= seen_event_ids(metadata) | source_event_ids(metadata)
        # With nothing covered (a mid-turn fold of a first turn), the row sorts where the latest one does.
        after_run_index = max((run.run_index for run in covered), default=previous_index)
        row_id = connection.exec_driver_sql(
            f"INSERT INTO {tables.summaries} (session_id, scope_key, after_run_index, summary, summary_model, "  # noqa: S608
            "event_ids, seen_event_ids, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                scope_key,
                after_run_index,
                summary,
                summary_model,
                json.dumps(sorted(event_ids)),
                json.dumps(sorted(seen)),
                int(time.time()),
            ),
        ).lastrowid
        connection.commit()
    return row_id


def history_event_ids(
    storage: BaseDb,
    *,
    session_id: str,
    scope_key: str,
    kind: Literal["seen", "all"],
) -> set[str]:
    """Return the event ids the scope's summary rows after its latest barrier fold, as seen or as redaction matches them."""
    db = _sqlite(storage)
    tables = _tables(db)
    column = "seen_event_ids" if kind == "seen" else "event_ids"
    with db.db_engine.begin() as connection:
        _ensure_tables(connection, db)
        rows = connection.exec_driver_sql(
            f"SELECT value FROM {tables.summaries} AS row, json_each(row.{column}) "  # noqa: S608
            "WHERE row.session_id = ? AND row.scope_key = ? AND row.id > COALESCE("
            f"(SELECT MAX(id) FROM {tables.summaries} WHERE session_id = ? AND scope_key = ? AND summary IS NULL), 0)",
            (session_id, scope_key, session_id, scope_key),
        ).fetchall()
    return {row[0] for row in rows}


def cut_at_event(
    storage: BaseDb,
    *,
    session_id: str,
    scope_key: str,
    event_id: str,
    match: Literal["answered", "any"],
) -> _CutResult | None:
    """Delete the earliest element of the scope's log that holds ``event_id`` and everything after it.

    An edit matches what a run answered; a redaction (``"any"``) also what it read or wrote. Returns
    ``None`` when no element holds the event.
    """
    db = _sqlite(storage)
    tables = _tables(db)
    with db.db_engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        _ensure_tables(connection, db)
        rows = _rows(connection, tables, session_id=session_id, scope_key=scope_key)
        boundary = rows[-1].after_run_index if rows else None
        loaded_holder = next(
            (
                run_index
                for run_index, metadata in _loaded_runs(connection, tables, session_id, scope_key, boundary)
                if _holds(metadata, event_id, match)
            ),
            None,
        )
        folded = _folded_holder(connection, tables, session_id, scope_key, rows, event_id, match)
        candidates: list[_Key] = [] if loaded_holder is None else [(loaded_holder, 0)]
        if folded is not None:
            candidates.append(folded)
        if not candidates:
            return None
        # The cut never starts after the first loaded holder, so that run always goes.
        _cut(connection, tables, session_id, scope_key, rows, min(candidates))
        connection.commit()
    return _CutResult(removed_loaded_holder=loaded_holder is not None)


# LEGACY_COMPAT: Content-free summaries of history compacted before the archive existed.
# Legacy format: A ``legacy`` summary row at ``after_run_index = -1`` adopted from a scope whose runs the
# destructive compactor deleted, leaving only ``session.summary`` and the seen ids it may contain (see
# ``history/legacy_compaction_state.py`` for provenance and the one-time migration).
# Last legacy release: v2026.9.314; replacement: v2026.9.315 kept compacted runs (now summary rows).
# Handling: ``legacy_event_ids`` reports what such a summary may contain while it is in force (it has
# text and no barrier follows it); ``cut_at_legacy_row`` removes it with every run and later row, since
# it cannot be split by run; ``retire`` appends a barrier that stops every summary of the scope from
# replaying or counting while runs stay stored, as ``history/storage.py`` decides for redactions.
# Coverage: tests/test_compaction_redaction.py::test_redacting_legacy_provenance_clears_summary_and_folded_runs,
# tests/test_compaction_redaction.py::test_removing_a_loaded_run_retires_a_legacy_summary, and
# tests/test_compaction_redaction.py::test_retirement_survives_a_later_cut.
def legacy_event_ids(storage: BaseDb, *, session_id: str, scope_key: str) -> frozenset[str] | None:
    """Return the event ids a legacy summary in force may contain, or ``None`` when none is in force."""
    db = _sqlite(storage)
    tables = _tables(db)
    with db.db_engine.begin() as connection:
        _ensure_tables(connection, db)
        rows = _rows(connection, tables, session_id=session_id, scope_key=scope_key)
    in_force = [row for row in _after_latest_barrier(rows) if row.legacy and row.summary is not None]
    return frozenset().union(*(row.event_ids for row in in_force)) if in_force else None


def cut_at_legacy_row(storage: BaseDb, *, session_id: str, scope_key: str) -> None:
    """Delete the legacy summary in force and everything after it: every run and later row of the scope."""
    db = _sqlite(storage)
    tables = _tables(db)
    with db.db_engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        _ensure_tables(connection, db)
        rows = _rows(connection, tables, session_id=session_id, scope_key=scope_key)
        legacy = [row for row in _after_latest_barrier(rows) if row.legacy and row.summary is not None]
        if legacy:
            _cut(connection, tables, session_id, scope_key, rows, (legacy[0].after_run_index, legacy[0].id))
        connection.commit()


def retire(storage: BaseDb, *, session_id: str, scope_key: str) -> None:
    """Stop every summary of the scope from replaying or counting as seen, keeping the runs stored."""
    db = _sqlite(storage)
    tables = _tables(db)
    with db.db_engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        _ensure_tables(connection, db)
        rows = _rows(connection, tables, session_id=session_id, scope_key=scope_key)
        connection.exec_driver_sql(
            f"INSERT INTO {tables.summaries} (session_id, scope_key, after_run_index, created_at) "  # noqa: S608
            "VALUES (?, ?, ?, ?)",
            (session_id, scope_key, rows[-1].after_run_index if rows else -1, int(time.time())),
        )
        connection.commit()


def _cut(
    connection: Connection,
    tables: _Tables,
    session_id: str,
    scope_key: str,
    rows: Sequence[_SummaryRow],
    cut: _Key,
) -> None:
    """Delete every top-level run and non-barrier row at or after ``cut``, with descendants, and record the removed runs."""
    top_level = set(_top_level_from(connection, tables, session_id, scope_key, cut))
    deleted_rows = [row for row in rows if row.summary is not None and (row.after_run_index, row.id) >= cut]
    removed = top_level | _descendants(connection, tables, session_id, top_level)
    for start in range(0, len(sorted(removed)), _ID_CHUNK_SIZE):
        chunk = sorted(removed)[start : start + _ID_CHUNK_SIZE]
        placeholders = ", ".join("?" for _ in chunk)
        connection.exec_driver_sql(
            f"DELETE FROM {tables.runs} WHERE session_id = ? AND run_id IN ({placeholders})",  # noqa: S608
            (session_id, *chunk),
        )
        connection.exec_driver_sql(
            f"INSERT OR IGNORE INTO {tables.removed} (run_id, session_id) VALUES {', '.join('(?, ?)' for _ in chunk)}",  # noqa: S608
            tuple(value for run_id in chunk for value in (run_id, session_id)),
        )
    if deleted_rows:
        connection.exec_driver_sql(
            f"DELETE FROM {tables.summaries} WHERE id IN ({', '.join('?' for _ in deleted_rows)})",  # noqa: S608
            tuple(row.id for row in deleted_rows),
        )


def _top_level_from(connection: Connection, tables: _Tables, session_id: str, scope_key: str, cut: _Key) -> list[str]:
    """Return the scope's top-level runs whose key is at or after ``cut``."""
    run_index, row_id = cut
    # A run sorts before a summary row at its own index, so a cut at a row starts after that index.
    first_index = run_index if row_id == 0 else run_index + 1
    return [
        run_id
        for (run_id,) in connection.exec_driver_sql(
            f"SELECT run.run_id FROM {tables.runs} AS run WHERE run.session_id = ? AND run.parent_run_id IS NULL "  # noqa: S608
            f"AND {run_scope_key_sql('run')} = ? AND run.run_index >= ?",
            (session_id, scope_key, first_index),
        )
    ]


def _descendants(connection: Connection, tables: _Tables, session_id: str, run_ids: set[str]) -> set[str]:
    found: set[str] = set()
    frontier = sorted(run_ids)
    while frontier:
        chunk = frontier[:_ID_CHUNK_SIZE]
        frontier = frontier[_ID_CHUNK_SIZE:]
        children = [
            run_id
            for (run_id,) in connection.exec_driver_sql(
                f"SELECT run_id FROM {tables.runs} WHERE session_id = ? "  # noqa: S608
                f"AND parent_run_id IN ({', '.join('?' for _ in chunk)})",
                (session_id, *chunk),
            )
            if run_id not in found and run_id not in run_ids
        ]
        found.update(children)
        frontier.extend(children)
    return found


def _folded_holder(
    connection: Connection,
    tables: _Tables,
    session_id: str,
    scope_key: str,
    rows: Sequence[_SummaryRow],
    event_id: str,
    match: Literal["answered", "any"],
) -> _Key | None:
    """Return the key of the earliest folded element holding the event, searching rows after the latest barrier."""
    barrier = max((row.id for row in rows if row.summary is None), default=0)
    previous_index: int | None = None
    for row in rows:
        start = previous_index
        previous_index = row.after_run_index
        if row.id <= barrier or event_id not in row.event_ids:
            continue
        for run_index, metadata in _range_runs(connection, tables, session_id, scope_key, start, row.after_run_index):
            if _holds(metadata, event_id, match):
                return (run_index, 0)
    return None


def _range_runs(
    connection: Connection,
    tables: _Tables,
    session_id: str,
    scope_key: str,
    after: int | None,
    through: int,
) -> list[tuple[int, Any]]:
    """Return the index and metadata of the scope's top-level runs a row at ``through`` sorts after, from ``after`` on."""
    return [
        (run_index, json.loads(metadata) if metadata is not None else None)
        for run_index, metadata in connection.exec_driver_sql(
            f"SELECT run.run_index, json_extract(run.run_data, '$.metadata') FROM {tables.runs} AS run "  # noqa: S608
            f"WHERE run.session_id = ? AND run.parent_run_id IS NULL AND {run_scope_key_sql('run')} = ? "
            "AND run.run_index > ? AND run.run_index <= ? ORDER BY run.run_index",
            (session_id, scope_key, -2 if after is None else after, through),
        )
    ]


def _loaded_runs(
    connection: Connection,
    tables: _Tables,
    session_id: str,
    scope_key: str,
    boundary: int | None,
) -> list[tuple[int, Any]]:
    """Return the index and metadata of the scope's top-level runs a load returns, in log order."""
    return [
        (run_index, json.loads(metadata) if metadata is not None else None)
        for run_index, metadata in connection.exec_driver_sql(
            f"SELECT run.run_index, json_extract(run.run_data, '$.metadata') FROM {tables.runs} AS run "  # noqa: S608
            f"WHERE run.session_id = ? AND run.parent_run_id IS NULL AND {run_scope_key_sql('run')} = ? "
            f"AND {loads_sql('run', '?')} ORDER BY run.run_index",
            (session_id, scope_key, boundary),
        )
    ]


def _run_metadata(
    connection: Connection,
    tables: _Tables,
    *,
    session_id: str,
    run_ids: Collection[str],
) -> list[tuple[str, int, str | None, Any]]:
    wanted = sorted(set(run_ids))
    found: list[tuple[str, int, str | None, Any]] = []
    for start in range(0, len(wanted), _ID_CHUNK_SIZE):
        chunk = wanted[start : start + _ID_CHUNK_SIZE]
        found.extend(
            (run_id, run_index, status, json.loads(metadata) if metadata is not None else None)
            for run_id, run_index, status, metadata in connection.exec_driver_sql(
                f"SELECT run_id, run_index, status, json_extract(run_data, '$.metadata') FROM {tables.runs} "  # noqa: S608
                f"WHERE session_id = ? AND run_id IN ({', '.join('?' for _ in chunk)})",
                (session_id, *chunk),
            )
        )
    return found


def _holds(metadata: Mapping[str, Any] | None, event_id: str, match: Literal["answered", "any"]) -> bool:
    if event_id in source_event_ids(metadata):
        return True
    return match == "any" and event_id in seen_event_ids(metadata)


def _rows(
    connection: Connection,
    tables: _Tables,
    *,
    session_id: str,
    scope_key: str,
    latest_only: bool = False,
) -> list[_SummaryRow]:
    """Return the scope's summary rows, oldest first, or only its latest one."""
    rows = [
        _SummaryRow(
            id=row_id,
            after_run_index=after_run_index,
            summary=summary,
            legacy=bool(legacy),
            event_ids=frozenset(json.loads(event_ids)),
        )
        for row_id, after_run_index, summary, legacy, event_ids in connection.exec_driver_sql(
            f"SELECT id, after_run_index, summary, legacy, event_ids FROM {tables.summaries} "  # noqa: S608
            f"WHERE session_id = ? AND scope_key = ? ORDER BY id DESC{' LIMIT 1' if latest_only else ''}",
            (session_id, scope_key),
        )
    ]
    return rows[::-1]


def _after_latest_barrier(rows: Sequence[_SummaryRow]) -> list[_SummaryRow]:
    barrier = max((row.id for row in rows if row.summary is None), default=0)
    return [row for row in rows if row.id > barrier]


def _sqlite(storage: BaseDb) -> SqliteDb:
    if not isinstance(storage, SqliteDb):
        msg = "Summary rows require SQLite session storage"
        raise TypeError(msg)
    return storage


def _tables(db: SqliteDb) -> _Tables:
    """Return the log's table names, creating Agno's runs table first, as a session that never saved a run lacks it."""
    summaries, removed = summary_rows_table_names(db.session_table_name)
    return _Tables(
        runs=quote_identifier(agno_compat_sqlite.owned_table(db, "runs").name),
        summaries=quote_identifier(summaries),
        removed=quote_identifier(removed),
    )


def _ensure_tables(connection: Connection, db: SqliteDb) -> None:
    for statement in summary_rows_schema_sql(db.session_table_name):
        connection.exec_driver_sql(statement)
