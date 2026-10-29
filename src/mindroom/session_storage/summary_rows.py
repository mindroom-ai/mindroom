"""Summary rows of the conversation log: appending summaries, reading what they cover, and cutting.

A scope's history is its top-level runs in ``seq`` order with summary rows between them. A run sorts at ``(seq, 0)``
and a row at ``(through, id)``, so a row sorts after the newest run it covers and before every later run. A visible
run is covered once its ``seq`` is at most the ``through`` of its scope's latest row, a barrier included, and replay is
that row's text followed by the runs it does not cover. Compaction only appends rows. Edits and redactions cut: they
delete the earliest element holding the event and everything after it, which returns the runs a deleted summary
covered to replay.

Each row keeps the event ids of the runs it covers, so redaction checks compacted history without reading them. A
mid-turn fold's row also keeps its provenance, the event ids its running run had answered and read at the fold, so a
cut finds what the fold summarized even once that run's row is gone; what the fold took stays in its run's message
rows, flagged so it never replays.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from time import time
from typing import TYPE_CHECKING, Any, Literal

from mindroom.session_storage.schema import NON_VISIBLE_RUN_STATUS_SQL, NON_VISIBLE_RUN_STATUS_VALUES, quote_identifier
from mindroom.session_storage.store import (
    decoded_mapping,
    delete_runs_in_transaction,
    ensure_session_row,
    is_removed,
    report_created_row,
    seen_event_ids,
    source_event_ids,
    store_fold,
    summary_event_sets,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Collection, Iterable, Mapping, Sequence

    from mindroom.llm.messages import Message
    from mindroom.llm.runs import AnyRunRecord
    from mindroom.session_storage.schema import TableNames
    from mindroom.session_storage.store import ConversationStore
    from mindroom.session_storage.types import ConversationSession

type _Key = tuple[int, int]


@dataclass(frozen=True, slots=True)
class _SummaryRow:
    """One summary row of a scope; a barrier has no text, and only a fold's row names a run and its provenance."""

    id: int
    through: int
    summary: str | None
    legacy: bool
    event_ids: frozenset[str]
    fold_run_id: str | None
    fold_event_ids: frozenset[str]
    fold_seen_event_ids: frozenset[str]

    @property
    def key(self) -> _Key:
        """Where the row sorts among its scope's runs, each of which sorts at ``(seq, 0)``."""
        return (self.through, self.id)


@dataclass(frozen=True, slots=True)
class CoveredRun:
    """A run a summary read before its summary call, as it was stored then."""

    run_id: str
    seq: int
    status: str

    @property
    def visible(self) -> bool:
        """Whether the run replays as model history, so a summary may fold it."""
        return self.status not in NON_VISIBLE_RUN_STATUS_VALUES


@dataclass(frozen=True, slots=True)
class Fold:
    """What a mid-turn fold took: the live running record, the exchanges it summarized, and its ended member runs."""

    run: AnyRunRecord
    messages: tuple[Message, ...]
    member_run_ids: frozenset[str]


def latest_row(storage: ConversationStore, *, session_id: str, scope_key: str) -> _SummaryRow | None:
    """Return the scope's latest summary row, whose text replays."""
    with storage.transaction(write=False) as connection:
        rows = _rows(connection, storage.names, session_id=session_id, scope_key=scope_key, latest_only=True)
    return rows[-1] if rows else None


def covered_runs(storage: ConversationStore, *, session_id: str, run_ids: Sequence[str]) -> list[CoveredRun]:
    """Return how the given runs are stored now, in ``seq`` order, for a later append to check."""
    with storage.transaction(write=False) as connection:
        return [
            CoveredRun(run_id=run_id, seq=seq, status=status)
            for run_id, seq, status in connection.execute(
                f"SELECT run_id, seq, status FROM {quote_identifier(storage.names.log_runs)} "  # noqa: S608
                "WHERE session_id = ? AND run_id IN (SELECT value FROM json_each(?)) ORDER BY seq",
                (session_id, json.dumps(list(run_ids))),
            )
        ]


def append_summary(
    storage: ConversationStore,
    *,
    session_id: str,
    scope_key: str,
    based_on: int | None,
    covered: Sequence[CoveredRun],
    summary: str,
    fold: Fold | None = None,
    session: ConversationSession | None = None,
    initial_session: ConversationSession | None = None,
) -> int | None:
    """Append one summary row after the newest covered run, unless its input went stale; return its id.

    The input is stale when the scope's latest row is no longer ``based_on``, a covered run sorts at or behind that
    row, a covered run is gone or has another ``seq`` or status than was read, or a cut removed the fold's running run
    or its team run. The row's event sets come from the covered runs as they are stored now, so metadata linked during
    the summary call counts, and for a fold also from the running run's live metadata, which the row keeps as its
    provenance. A fold then stores what it took out of its run (``store_fold``). ``initial_session``, when given, is
    the empty session row the append creates first when no write has yet, as a first turn's fold needs when its
    summary call reported no usage. ``session``, when given, then holds the runs and summary a load returns.
    """
    created, names = None, storage.names
    with storage.log_transaction(session_id, session=session) as connection:
        latest = _rows(connection, names, session_id=session_id, scope_key=scope_key, latest_only=True)
        through = latest[-1].through if latest else 0
        # A row never sorts behind the latest one: that would return what it folded to replay.
        if (latest[-1].id if latest else None) != based_on or any(run.seq <= through for run in covered):
            return None
        stored = connection.execute(
            f"SELECT run_id, seq, status, metadata FROM {quote_identifier(names.log_runs)} "  # noqa: S608
            "WHERE session_id = ? AND run_id IN (SELECT value FROM json_each(?))",
            (session_id, json.dumps([run.run_id for run in covered])),
        ).fetchall()
        current = {run_id: (seq, status) for run_id, seq, status, _ in stored}
        if any(current.get(run.run_id) != (run.seq, run.status) for run in covered):
            return None
        run_metadata = [decoded_mapping(metadata) for *_, metadata in stored]
        provenance: tuple[str | None, ...] = (None, None, None)
        if fold is not None:
            if is_removed(connection, names, session_id, fold.run.run_id, fold.run.parent_run_id):
                return None
            live = fold.run.metadata
            # The running run's events count even when it later ends paused, cancelled, or failed.
            run_metadata.append(live)
            provenance = (fold.run.run_id, _encoded(source_event_ids(live)), _encoded(seen_event_ids(live)))
        # The summary row references the session row, which a first turn's fold may be the first to need.
        if initial_session is not None and ensure_session_row(connection, names, initial_session) is not None:
            created = initial_session
        event_ids, seen = summary_event_sets(run_metadata)
        (row_id,) = connection.execute(
            f"INSERT INTO {quote_identifier(names.log_summaries)} (session_id, scope_key, through, summary, "  # noqa: S608
            "event_ids, seen_event_ids, created_at, fold_run_id, fold_event_ids, fold_seen_event_ids) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (
                session_id,
                scope_key,
                # With nothing covered, the row sorts where the latest one does.
                max((run.seq for run in covered), default=through),
                summary,
                _encoded(event_ids),
                _encoded(seen),
                int(time()),
                *provenance,
            ),
        ).fetchone()
        if fold is not None:
            store_fold(connection, names, session_id, fold.run, fold.messages, fold.member_run_ids, row_id)
    if created is not None:
        report_created_row(created)
    return row_id


def folded_tool_call_ids(storage: ConversationStore, *, session_id: str, run_id: str) -> set[str]:
    """Return the tool calls the mid-turn folds of ``run_id`` summarized, from the rows they flagged.

    They are the calls and results of the run's folded messages and the calls of its folded member runs, since the
    whole delegation folds with each member run that ended before the fold.
    """
    names = storage.names
    runs = quote_identifier(names.log_runs)
    with storage.transaction(write=False) as connection:
        messages = connection.execute(
            f"SELECT data FROM {quote_identifier(names.log_messages)} WHERE folded_by IS NOT NULL AND run_id = "  # noqa: S608
            f"(SELECT run_id FROM {runs} WHERE session_id = ? AND run_id = ?)",
            (session_id, run_id),
        ).fetchall()
        member_tools = connection.execute(
            f"SELECT tool.data FROM {quote_identifier(names.log_tools)} AS tool JOIN {runs} AS run "  # noqa: S608
            "ON run.run_id = tool.run_id "
            "WHERE run.session_id = ? AND run.parent_run_id = ? AND run.folded_by IS NOT NULL",
            (session_id, run_id),
        ).fetchall()
    return summarized_tool_call_ids(
        [json.loads(data) for (data,) in messages],
        [json.loads(data) for (data,) in member_tools],
    )


def summarized_tool_call_ids(
    messages: Iterable[Mapping[str, Any]],
    member_tools: Iterable[Mapping[str, Any]],
) -> set[str]:
    """Return the tool calls a fold summarized, from the stored dicts of its messages and its members' tool executions.

    They are the messages' calls and results and the members' calls. The turn recorder reads a live fold through it
    and ``folded_tool_call_ids`` the rows folds flagged, so an interrupted record leaves out the same calls either way.
    """
    call_ids = {call.get("id") for message in messages for call in message.get("tool_calls") or []}
    call_ids |= {message.get("tool_call_id") for message in messages}
    call_ids |= {tool.get("tool_call_id") for tool in member_tools}
    return {call_id for call_id in call_ids if isinstance(call_id, str)}


def history_event_ids(
    storage: ConversationStore,
    *,
    session_id: str,
    scope_key: str,
    kind: Literal["seen", "all"],
) -> set[str]:
    """Return the event ids the scope's summary rows after its latest barrier fold, as seen or as redaction matches them."""
    column = "seen_event_ids" if kind == "seen" else "event_ids"
    summaries = quote_identifier(storage.names.log_summaries)
    with storage.transaction(write=False) as connection:
        rows = connection.execute(
            f"SELECT value FROM {summaries} AS row, json_each(row.{column}) "  # noqa: S608
            "WHERE row.session_id = ? AND row.scope_key = ? AND row.id > COALESCE("
            f"(SELECT MAX(id) FROM {summaries} WHERE session_id = ? AND scope_key = ? AND summary IS NULL), 0)",
            (session_id, scope_key, session_id, scope_key),
        ).fetchall()
    return {row[0] for row in rows}


def cut_at_event(
    storage: ConversationStore,
    *,
    session_id: str,
    scope_key: str,
    event_id: str,
    match: Literal["answered", "any"],
    session: ConversationSession | None = None,
) -> bool:
    """Delete the earliest element of the scope's log that holds ``event_id`` and everything after it.

    An edit matches what a run answered; a redaction (``"any"``) also what it read or wrote. A top-level run holds the
    event through its stored metadata unless the latest barrier retired it, and a fold's row through its provenance, so
    the fold's summary is cut even once its run's row is gone. Returns whether any element held the event.
    ``session``, when given, is the loaded session whose log is cut, named by ``session_id``; it then holds the runs
    and summary a load returns.
    """
    names = storage.names
    with storage.log_transaction(session_id, session=session) as connection:
        rows = _rows(connection, names, session_id=session_id, scope_key=scope_key)
        retired_through, barrier_id = _barrier_key(rows)
        runs = connection.execute(
            f"SELECT seq, metadata FROM {quote_identifier(names.log_runs)} WHERE session_id = ? "  # noqa: S608
            "AND parent_run_id IS NULL AND scope_key = ? "
            f"AND (status IN ({NON_VISIBLE_RUN_STATUS_SQL}) OR seq > ?) ORDER BY seq",
            (session_id, scope_key, retired_through),
        ).fetchall()
        holders = [
            row.key
            for row in rows
            if row.id > barrier_id and _holds(row.fold_event_ids, row.fold_seen_event_ids, event_id, match)
        ]
        for seq, raw_metadata in runs:
            metadata = decoded_mapping(raw_metadata)
            if _holds(source_event_ids(metadata), seen_event_ids(metadata), event_id, match):
                holders.append((seq, 0))
                break
        if not holders:
            return False
        _cut(connection, names, session_id, scope_key, rows, min(holders))
    return True


# LEGACY_COMPAT: Content-free summaries of history compacted before the archive existed.
# Legacy format: A ``legacy`` summary row at ``through = 0`` that a conversion adopted from a scope whose runs the
# destructive compactor deleted, leaving only ``session.summary`` and the seen ids it may contain (see
# ``history/legacy_compaction_state.py`` for provenance and the one-time migration).
# Last legacy release: v2026.9.314; replacement: v2026.9.315 kept compacted runs (now summary rows).
# Handling: ``legacy_event_ids`` reports what such a summary may contain while it is in force (it has
# text and no barrier follows it); ``cut_at_legacy_row`` removes it with every run and later row, since
# it cannot be split by run; ``retire`` appends a barrier that stops every summary of the scope from
# replaying or counting while runs stay stored, as ``history/storage.py`` decides for redactions.
# Coverage: tests/test_summary_rows.py::test_legacy_event_ids_report_a_legacy_summary_while_it_is_in_force,
# tests/test_summary_rows.py::test_cutting_at_the_legacy_row_removes_the_whole_scope, and
# tests/test_summary_rows.py::test_a_retired_legacy_summary_stays_retired_after_a_later_cut.
def legacy_event_ids(storage: ConversationStore, *, session_id: str, scope_key: str) -> frozenset[str] | None:
    """Return the event ids a legacy summary in force may contain, or ``None`` when none is in force."""
    with storage.transaction(write=False) as connection:
        in_force = _legacy_in_force(_rows(connection, storage.names, session_id=session_id, scope_key=scope_key))
    return frozenset().union(*(row.event_ids for row in in_force)) if in_force else None


def cut_at_legacy_row(storage: ConversationStore, *, session: ConversationSession, scope_key: str) -> None:
    """Delete the legacy summary in force and everything after it: every run and later row of the scope.

    ``session`` is the loaded session whose log is cut; it then holds the runs and summary a load returns.
    """
    session_id, names = session.session_id, storage.names
    with storage.log_transaction(session_id, session=session) as connection:
        rows = _rows(connection, names, session_id=session_id, scope_key=scope_key)
        if legacy := _legacy_in_force(rows):
            _cut(connection, names, session_id, scope_key, rows, legacy[0].key)


def _legacy_in_force(rows: Sequence[_SummaryRow]) -> list[_SummaryRow]:
    """Return the legacy summary rows after the scope's latest barrier, each of which has text."""
    _, barrier_id = _barrier_key(rows)
    return [row for row in rows if row.id > barrier_id and row.legacy]


def retire(storage: ConversationStore, *, session: ConversationSession, scope_key: str) -> None:
    """Stop every summary of the scope from replaying or counting as seen, keeping the runs stored.

    A run that folded mid-turn goes with its member runs, since its stored part would replay without what it built
    on. ``session`` is the loaded session whose summaries retire; it then holds the runs and summary a load returns.
    """
    session_id, names = session.session_id, storage.names
    with storage.log_transaction(session_id, session=session) as connection:
        rows = _rows(connection, names, session_id=session_id, scope_key=scope_key)
        connection.execute(
            f"INSERT INTO {quote_identifier(names.log_summaries)} (session_id, scope_key, through, created_at) "  # noqa: S608
            "VALUES (?, ?, ?, ?)",
            (session_id, scope_key, rows[-1].through if rows else 0, int(time())),
        )
        owners = {row.fold_run_id for row in rows if row.fold_run_id is not None}
        removed = delete_runs_in_transaction(connection, names, owners, session_id=session_id)
        _record_removed(connection, names, session_id, owners | removed)


def _cut(
    connection: sqlite3.Connection,
    names: TableNames,
    session_id: str,
    scope_key: str,
    rows: Sequence[_SummaryRow],
    cut: _Key,
) -> None:
    """Delete every top-level run and non-barrier row at or after ``cut``, with member runs, and record the removed runs.

    A run goes whole: when a deleted run, or the running run of a deleted fold row, owns a fold row earlier in the
    log, the cut starts at that row instead. Every deleted run and the running run of every deleted fold row count as
    removed, which also fences a running run whose row is gone.
    """
    runs = connection.execute(
        f"SELECT run_id, seq FROM {quote_identifier(names.log_runs)} "  # noqa: S608
        "WHERE session_id = ? AND parent_run_id IS NULL AND scope_key = ?",
        (session_id, scope_key),
    ).fetchall()
    while True:
        top_level = {run_id for run_id, seq in runs if (seq, 0) >= cut}
        deleted_rows = [row for row in rows if row.summary is not None and row.key >= cut]
        owners = top_level | {row.fold_run_id for row in deleted_rows if row.fold_run_id is not None}
        earlier = [row.key for row in rows if row.fold_run_id in owners and row.key < cut]
        if not earlier:
            break
        cut = min(earlier)
    removed = delete_runs_in_transaction(connection, names, top_level, session_id=session_id)
    _record_removed(connection, names, session_id, owners | removed)
    connection.execute(
        f"DELETE FROM {quote_identifier(names.log_summaries)} "  # noqa: S608
        "WHERE id IN (SELECT value FROM json_each(?))",
        (json.dumps([row.id for row in deleted_rows]),),
    )


def _record_removed(
    connection: sqlite3.Connection,
    names: TableNames,
    session_id: str,
    run_ids: Collection[str],
) -> None:
    connection.execute(
        f"INSERT OR IGNORE INTO {quote_identifier(names.log_removed)} (run_id, session_id) "  # noqa: S608
        "SELECT value, ? FROM json_each(?)",
        (session_id, json.dumps(sorted(run_ids))),
    )


def _holds(answered: Collection[str], seen: Collection[str], event_id: str, match: Literal["answered", "any"]) -> bool:
    """Return whether what answered ``answered`` and read ``seen`` holds the event, for an edit or a redaction."""
    return event_id in answered or (match == "any" and event_id in seen)


def _barrier_key(rows: Sequence[_SummaryRow]) -> _Key:
    """Return the key of the scope's latest barrier, or ``(0, 0)`` without one; nothing behind it counts."""
    return next((row.key for row in reversed(rows) if row.summary is None), (0, 0))


def _encoded(event_ids: Collection[str]) -> str:
    return json.dumps(sorted(event_ids))


def _decoded(raw_event_ids: str | None) -> frozenset[str]:
    return frozenset(json.loads(raw_event_ids)) if raw_event_ids is not None else frozenset()


def _rows(
    connection: sqlite3.Connection,
    names: TableNames,
    *,
    session_id: str,
    scope_key: str,
    latest_only: bool = False,
) -> list[_SummaryRow]:
    """Return the scope's summary rows, oldest first, or only its latest one."""
    rows = [
        _SummaryRow(
            row_id,
            through,
            summary,
            bool(legacy),
            _decoded(ids),
            fold_run_id,
            _decoded(answered),
            _decoded(seen),
        )
        for row_id, through, summary, legacy, ids, fold_run_id, answered, seen in connection.execute(
            "SELECT id, through, summary, legacy, event_ids, fold_run_id, fold_event_ids, fold_seen_event_ids "  # noqa: S608
            f"FROM {quote_identifier(names.log_summaries)} WHERE session_id = ? AND scope_key = ? "
            f"ORDER BY id DESC{' LIMIT 1' if latest_only else ''}",
            (session_id, scope_key),
        )
    ]
    return rows[::-1]
