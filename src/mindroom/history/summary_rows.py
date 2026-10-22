"""Summary rows of the conversation log: appending compaction chunks, reading what they cover, and cutting.

A scope's history is Agno's runs table in ``run_index`` order with summary rows
between its runs. A row's key is ``(after_run_index, id)``; a top-level run's key
is ``(run_index, 0)``, and member runs follow their parent. Loading replays the
scope's latest summary row and the runs after it, so compaction only appends
rows and never deletes a run; a mid-turn fold moves only its own stored run, a
paused or checkpointed copy, after its row. Edits and redactions cut: they delete
one element and everything after it, which also returns the runs a deleted
summary covered to replay.

Each row keeps the event ids of the runs its own chunk folded, so redaction can
check compacted history on every response without reading the folded runs.

A mid-turn fold's row also holds its segment: what the fold took out of the
running run. The run sorts after the row and loads without the segment's
messages, and a cut or retirement that removes either removes both.
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
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
    from collections.abc import Collection, Iterator, Mapping, Sequence

    from agno.db.base import BaseDb
    from sqlalchemy import Connection

type _Key = tuple[int, int]


@dataclass(frozen=True)
class _SummaryRow:
    """One summary row of a scope."""

    id: int
    after_run_index: int
    summary: str | None
    legacy: bool
    event_ids: frozenset[str]
    segment_run_id: str | None


@dataclass(frozen=True)
class Segment:
    """What a mid-turn fold took out of its running run, stored on the fold's summary row.

    ``metadata`` is the run's metadata at the fold, so the row counts the events the run read and
    answered even when the run never saves; the rest are the folded messages, the tool executions
    they hold, and the member responses attached so far, as Agno serializes them.
    """

    run_id: str
    metadata: Mapping[str, Any]
    messages: Sequence[Mapping[str, Any]]
    tools: Sequence[Mapping[str, Any]]
    member_responses: Sequence[Mapping[str, Any]]

    def payload(self) -> dict[str, Any]:
        """Return the row's ``segment`` column value, before encoding."""
        return {
            "metadata": self.metadata,
            "messages": self.messages,
            "tools": self.tools,
            "member_responses": self.member_responses,
        }


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
    with _transaction(storage) as (connection, tables):
        rows = _rows(connection, tables, session_id=session_id, scope_key=scope_key, latest_only=True)
    return rows[-1] if rows else None


def covered_runs(storage: BaseDb, *, session_id: str, run_ids: Sequence[str]) -> list[CoveredRun]:
    """Return how the given runs are stored now, in log order, for a later append to check."""
    # Only positions and statuses: the run payloads stay unread until the append under the write lock.
    with _transaction(storage) as (connection, tables):
        return [
            CoveredRun(run_id=run_id, run_index=run_index, status=status)
            for run_id, run_index, status in connection.exec_driver_sql(
                f"SELECT run_id, run_index, status FROM {tables.runs} "  # noqa: S608
                "WHERE session_id = ? AND run_id IN (SELECT value FROM json_each(?)) ORDER BY run_index",
                (session_id, json.dumps(list(run_ids))),
            )
        ]


def append_summary(
    storage: BaseDb,
    *,
    session_id: str,
    scope_key: str,
    based_on: int | None,
    covered: Sequence[CoveredRun],
    summary: str,
    summary_model: str,
    segment: Segment | None = None,
) -> int | None:
    """Append one summary row after the newest covered run, unless its input went stale; return its id.

    The input is stale when the scope's latest row is no longer ``based_on``, a covered run is gone or
    changed status or place since it was read, a covered run sorts at or behind the latest row, or a cut
    removed the segment's run. The row's event sets come from the covered rows as they are stored now, so
    metadata written during the summary call, such as a reply's response event id, counts. A segment's
    run, when stored, moves after the new row, since what it still holds follows what the row folded.
    """
    with _transaction(storage, write=True) as (connection, tables):
        rows = _rows(connection, tables, session_id=session_id, scope_key=scope_key, latest_only=True)
        previous_index = rows[-1].after_run_index if rows else -1
        # A row never sorts behind the latest one: that would return what it folded to replay.
        if (rows[-1].id if rows else None) != based_on or any(run.run_index <= previous_index for run in covered):
            return None
        if segment is not None and _is_removed(connection, tables, segment.run_id):
            return None
        stored = {
            run_id: (run_index, status, json.loads(metadata) if metadata is not None else None)
            for run_id, run_index, status, metadata in connection.exec_driver_sql(
                f"SELECT run_id, run_index, status, json_extract(run_data, '$.metadata') FROM {tables.runs} "  # noqa: S608
                "WHERE session_id = ? AND run_id IN (SELECT value FROM json_each(?))",
                (session_id, json.dumps([run.run_id for run in covered])),
            )
        }
        if any(stored.get(run.run_id, (None, None, None))[:2] != (run.run_index, run.status) for run in covered):
            return None
        event_ids: set[str] = set()
        seen: set[str] = set()
        metadata_sets = [metadata for _run_index, _status, metadata in stored.values()]
        if segment is not None:
            # The running run's events count even when it later ends paused, cancelled, or failed.
            metadata_sets.append(segment.metadata)
        for metadata in metadata_sets:
            seen |= seen_event_ids(metadata)
            event_ids |= seen_event_ids(metadata) | source_event_ids(metadata)
        # With nothing covered, the row sorts where the latest one does.
        after_run_index = max((run.run_index for run in covered), default=previous_index)
        row_id = connection.exec_driver_sql(
            f"INSERT INTO {tables.summaries} (session_id, scope_key, after_run_index, summary, summary_model, "  # noqa: S608
            "event_ids, seen_event_ids, created_at, segment_run_id, segment) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                scope_key,
                after_run_index,
                summary,
                summary_model,
                json.dumps(sorted(event_ids)),
                json.dumps(sorted(seen)),
                int(time.time()),
                segment.run_id if segment is not None else None,
                json.dumps(segment.payload()) if segment is not None else None,
            ),
        ).lastrowid
        if segment is not None:
            # A resumed approval or a CLI checkpoint stored the run before this fold.
            connection.exec_driver_sql(
                f"UPDATE {tables.runs} SET run_index = (SELECT MAX(COALESCE((SELECT MAX(run_index) FROM {tables.runs} "  # noqa: S608
                f"WHERE session_id = ?), -1), (SELECT MAX(after_run_index) FROM {tables.summaries} "
                "WHERE session_id = ?)) + 1) WHERE session_id = ? AND run_id = ?",
                (session_id, session_id, session_id, segment.run_id),
            )
    return row_id


def segment_tool_call_ids(storage: BaseDb, *, session_id: str, run_id: str) -> set[str]:
    """Return the tool calls every mid-turn fold of ``run_id`` folded, from their summary rows."""
    with _transaction(storage) as (connection, tables):
        segments = connection.exec_driver_sql(
            f"SELECT segment FROM {tables.summaries} WHERE session_id = ? AND segment_run_id = ?",  # noqa: S608
            (session_id, run_id),
        ).fetchall()
    return set().union(*(folded_tool_call_ids(json.loads(segment)) for (segment,) in segments))


def folded_tool_call_ids(segment: Mapping[str, Any]) -> set[str]:
    """Return the tool calls one segment folded: its messages' calls and executions, and its members' tools.

    The whole turn folds, so each member response attached so far goes with its delegation, nested members too.
    """
    call_ids = {
        call["id"]
        for message in segment.get("messages") or []
        for call in message.get("tool_calls") or []
        if isinstance(call.get("id"), str)
    }
    call_ids |= {
        tool["tool_call_id"] for tool in segment.get("tools") or [] if isinstance(tool.get("tool_call_id"), str)
    }
    members = list(segment.get("member_responses") or [])
    while members:
        member = members.pop()
        call_ids |= {
            tool["tool_call_id"] for tool in member.get("tools") or [] if isinstance(tool.get("tool_call_id"), str)
        }
        members.extend(member.get("member_responses") or [])
    return call_ids


def history_event_ids(
    storage: BaseDb,
    *,
    session_id: str,
    scope_key: str,
    kind: Literal["seen", "all"],
) -> set[str]:
    """Return the event ids the scope's summary rows after its latest barrier fold, as seen or as redaction matches them."""
    column = "seen_event_ids" if kind == "seen" else "event_ids"
    with _transaction(storage) as (connection, tables):
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
) -> bool:
    """Delete the earliest element of the scope's log that holds ``event_id`` and everything after it.

    An edit matches what a run answered; a redaction (``"any"``) also what it read or wrote. Returns
    whether any element held the event.
    """
    with _transaction(storage, write=True) as (connection, tables):
        rows = _rows(connection, tables, session_id=session_id, scope_key=scope_key)
        boundary = rows[-1].after_run_index if rows else None
        loaded_holder = next(
            (
                run_index
                for run_index, metadata in _top_level_runs(
                    connection,
                    tables,
                    session_id,
                    scope_key,
                    loads_sql("run", "?"),
                    boundary,
                )
                if _holds(metadata, event_id, match)
            ),
            None,
        )
        folded = _folded_holder(connection, tables, session_id, scope_key, rows, event_id, match)
        candidates: list[_Key] = [] if loaded_holder is None else [(loaded_holder, 0)]
        if folded is not None:
            candidates.append(folded)
        if not candidates:
            return False
        # The cut never starts after the first loaded holder, so that run always goes.
        _cut(connection, tables, session_id, scope_key, rows, min(candidates))
    return True


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
    with _transaction(storage) as (connection, tables):
        rows = _rows(connection, tables, session_id=session_id, scope_key=scope_key)
    in_force = [row for row in _after_latest_barrier(rows) if row.legacy and row.summary is not None]
    return frozenset().union(*(row.event_ids for row in in_force)) if in_force else None


def cut_at_legacy_row(storage: BaseDb, *, session_id: str, scope_key: str) -> None:
    """Delete the legacy summary in force and everything after it: every run and later row of the scope."""
    with _transaction(storage, write=True) as (connection, tables):
        rows = _rows(connection, tables, session_id=session_id, scope_key=scope_key)
        legacy = [row for row in _after_latest_barrier(rows) if row.legacy and row.summary is not None]
        if legacy:
            _cut(connection, tables, session_id, scope_key, rows, (legacy[0].after_run_index, legacy[0].id))


def retire(storage: BaseDb, *, session_id: str, scope_key: str) -> None:
    """Stop every summary of the scope from replaying or counting as seen, keeping the runs stored.

    A run that folded mid-turn goes with its descendants, since its stored part would replay without
    what it built on.
    """
    with _transaction(storage, write=True) as (connection, tables):
        rows = _rows(connection, tables, session_id=session_id, scope_key=scope_key)
        connection.exec_driver_sql(
            f"INSERT INTO {tables.summaries} (session_id, scope_key, after_run_index, created_at) "  # noqa: S608
            "VALUES (?, ?, ?, ?)",
            (session_id, scope_key, rows[-1].after_run_index if rows else -1, int(time.time())),
        )
        owners = {row.segment_run_id for row in rows if row.segment_run_id is not None}
        removed = agno_compat_sqlite.delete_run_subtrees(connection, tables.runs, owners, session_id=session_id)
        _record_removed(connection, tables, session_id, owners | removed)


def _cut(
    connection: Connection,
    tables: _Tables,
    session_id: str,
    scope_key: str,
    rows: Sequence[_SummaryRow],
    cut: _Key,
) -> None:
    """Delete every top-level run and non-barrier row at or after ``cut``, with descendants, and record the removed runs.

    A run goes whole: when a deleted run, or the run of a deleted segment, holds a segment on an earlier
    row, the cut starts at that row instead. The run of every deleted segment counts as removed, which
    fences a running run that has not stored a row yet.
    """
    while True:
        top_level = set(_top_level_from(connection, tables, session_id, scope_key, cut))
        deleted_rows = [row for row in rows if row.summary is not None and (row.after_run_index, row.id) >= cut]
        owners = top_level | {row.segment_run_id for row in deleted_rows if row.segment_run_id is not None}
        earlier = [
            (row.after_run_index, row.id)
            for row in rows
            if row.segment_run_id in owners and (row.after_run_index, row.id) < cut
        ]
        if not earlier:
            break
        cut = min(earlier)
    removed = agno_compat_sqlite.delete_run_subtrees(connection, tables.runs, top_level, session_id=session_id)
    _record_removed(connection, tables, session_id, removed | owners)
    connection.exec_driver_sql(
        f"DELETE FROM {tables.summaries} WHERE id IN (SELECT value FROM json_each(?))",  # noqa: S608
        (json.dumps([row.id for row in deleted_rows]),),
    )


def _record_removed(connection: Connection, tables: _Tables, session_id: str, run_ids: Collection[str]) -> None:
    connection.exec_driver_sql(
        f"INSERT OR IGNORE INTO {tables.removed} (run_id, session_id) SELECT value, ? FROM json_each(?)",  # noqa: S608
        (session_id, json.dumps(sorted(run_ids))),
    )


def _is_removed(connection: Connection, tables: _Tables, run_id: str) -> bool:
    return (
        connection.exec_driver_sql(f"SELECT 1 FROM {tables.removed} WHERE run_id = ?", (run_id,)).first()  # noqa: S608
        is not None
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


def _folded_holder(
    connection: Connection,
    tables: _Tables,
    session_id: str,
    scope_key: str,
    rows: Sequence[_SummaryRow],
    event_id: str,
    match: Literal["answered", "any"],
) -> _Key | None:
    """Return the key of the earliest folded element holding the event, searching rows after the latest barrier.

    A row's segment sorts after the runs the row folded, so a segment holding the event cuts at its row.
    """
    barrier = max((row.id for row in rows if row.summary is None), default=0)
    previous_index: int | None = None
    for row in rows:
        start = previous_index
        previous_index = row.after_run_index
        if row.id <= barrier or event_id not in row.event_ids:
            continue
        for run_index, metadata in _top_level_runs(
            connection,
            tables,
            session_id,
            scope_key,
            "run.run_index > ? AND run.run_index <= ?",
            -2 if start is None else start,
            row.after_run_index,
        ):
            if _holds(metadata, event_id, match):
                return (run_index, 0)
        if row.segment_run_id is not None and _holds(_segment_metadata(connection, tables, row.id), event_id, match):
            return (row.after_run_index, row.id)
    return None


def _segment_metadata(connection: Connection, tables: _Tables, row_id: int) -> Mapping[str, Any] | None:
    (metadata,) = connection.exec_driver_sql(
        f"SELECT json_extract(segment, '$.metadata') FROM {tables.summaries} WHERE id = ?",  # noqa: S608
        (row_id,),
    ).one()
    return json.loads(metadata) if metadata is not None else None


def _top_level_runs(
    connection: Connection,
    tables: _Tables,
    session_id: str,
    scope_key: str,
    condition: str,
    *parameters: object,
) -> list[tuple[int, Any]]:
    """Return the index and metadata of the scope's top-level runs meeting ``condition``, in log order."""
    return [
        (run_index, json.loads(metadata) if metadata is not None else None)
        for run_index, metadata in connection.exec_driver_sql(
            f"SELECT run.run_index, json_extract(run.run_data, '$.metadata') FROM {tables.runs} AS run "  # noqa: S608
            f"WHERE run.session_id = ? AND run.parent_run_id IS NULL AND {run_scope_key_sql('run')} = ? "
            f"AND {condition} ORDER BY run.run_index",
            (session_id, scope_key, *parameters),
        )
    ]


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
            segment_run_id=segment_run_id,
        )
        for row_id, after_run_index, summary, legacy, event_ids, segment_run_id in connection.exec_driver_sql(
            f"SELECT id, after_run_index, summary, legacy, event_ids, segment_run_id FROM {tables.summaries} "  # noqa: S608
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


@contextmanager
def _transaction(storage: BaseDb, *, write: bool = False) -> Iterator[tuple[Connection, _Tables]]:
    """Run one transaction on the log, creating its tables first; a write takes the write lock up front."""
    db = _sqlite(storage)
    tables = _tables(db)
    with db.db_engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE" if write else "BEGIN")
        for statement in summary_rows_schema_sql(db.session_table_name):
            connection.exec_driver_sql(statement)
        yield connection, tables
        connection.commit()
