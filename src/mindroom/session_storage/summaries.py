"""The summary rows and removed runs of a conversation log, each read or written in the caller's transaction.

``history/summary_rows.py`` decides when a summary appends and what an edit or redaction cuts; these are the
statements its rules run. A row's event sets are stored as sorted JSON arrays, and a barrier is a row without text.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from time import time
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

from mindroom.session_storage.schema import NON_VISIBLE_RUN_STATUS_SQL, quote_identifier
from mindroom.session_storage.store import decoded_mapping

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Collection, Sequence

    from mindroom.session_storage.schema import TableNames


@dataclass(frozen=True, slots=True)
class SummaryRow:
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
    def key(self) -> tuple[int, int]:
        """Where the row sorts among its scope's runs, each of which sorts at ``(seq, 0)``."""
        return (self.through, self.id)


class StoredRun(NamedTuple):
    """What a summary reads of a stored run: its place in the log, its status, and its metadata."""

    run_id: str
    seq: int
    status: str
    metadata: dict[str, Any] | None


class FoldProvenance(NamedTuple):
    """The running run of a mid-turn fold, and the event ids it had answered and read at the fold."""

    run_id: str
    event_ids: Collection[str]
    seen_event_ids: Collection[str]


def scope_rows(
    connection: sqlite3.Connection,
    names: TableNames,
    *,
    session_id: str,
    scope_key: str,
    latest_only: bool = False,
) -> list[SummaryRow]:
    """Return the scope's summary rows, oldest first, or only its latest one."""
    rows = [
        SummaryRow(
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


def stored_runs(
    connection: sqlite3.Connection,
    names: TableNames,
    *,
    session_id: str,
    run_ids: Sequence[str],
) -> list[StoredRun]:
    """Return how session ``session_id``'s runs ``run_ids`` are stored, in ``seq`` order; a run that is gone is left out."""
    return [
        StoredRun(run_id, seq, status, decoded_mapping(metadata))
        for run_id, seq, status, metadata in connection.execute(
            f"SELECT run_id, seq, status, metadata FROM {quote_identifier(names.log_runs)} "  # noqa: S608
            "WHERE session_id = ? AND run_id IN (SELECT value FROM json_each(?)) ORDER BY seq",
            (session_id, json.dumps(list(run_ids))),
        )
    ]


def unretired_runs(
    connection: sqlite3.Connection,
    names: TableNames,
    *,
    session_id: str,
    scope_key: str,
    retired_through: int,
) -> list[StoredRun]:
    """Return the scope's top-level runs a barrier at ``retired_through`` did not retire, in ``seq`` order.

    They are every paused, cancelled, or errored run and every run after ``retired_through``.
    """
    return [
        StoredRun(run_id, seq, status, decoded_mapping(metadata))
        for run_id, seq, status, metadata in connection.execute(
            f"SELECT run_id, seq, status, metadata FROM {quote_identifier(names.log_runs)} "  # noqa: S608
            "WHERE session_id = ? AND parent_run_id IS NULL AND scope_key = ? "
            f"AND (status IN ({NON_VISIBLE_RUN_STATUS_SQL}) OR seq > ?) ORDER BY seq",
            (session_id, scope_key, retired_through),
        )
    ]


def top_level_runs(
    connection: sqlite3.Connection,
    names: TableNames,
    *,
    session_id: str,
    scope_key: str,
) -> list[tuple[str, int]]:
    """Return the run id and ``seq`` of every top-level run of the scope."""
    return connection.execute(
        f"SELECT run_id, seq FROM {quote_identifier(names.log_runs)} "  # noqa: S608
        "WHERE session_id = ? AND parent_run_id IS NULL AND scope_key = ?",
        (session_id, scope_key),
    ).fetchall()


def append_row(
    connection: sqlite3.Connection,
    names: TableNames,
    *,
    session_id: str,
    scope_key: str,
    through: int,
    summary: str | None,
    event_ids: Collection[str] = (),
    seen_event_ids: Collection[str] = (),
    provenance: FoldProvenance | None = None,
) -> int:
    """Append one summary row to the scope, a barrier when ``summary`` is None, and return its id.

    The session row must exist. ``provenance``, given for a mid-turn fold's row, is what the row keeps of its fold.
    """
    fold = (
        (provenance.run_id, _encoded(provenance.event_ids), _encoded(provenance.seen_event_ids))
        if provenance is not None
        else (None, None, None)
    )
    (row_id,) = connection.execute(
        f"INSERT INTO {quote_identifier(names.log_summaries)} (session_id, scope_key, through, summary, event_ids, "  # noqa: S608
        "seen_event_ids, created_at, fold_run_id, fold_event_ids, fold_seen_event_ids) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
        (
            session_id,
            scope_key,
            through,
            summary,
            _encoded(event_ids),
            _encoded(seen_event_ids),
            int(time()),
            *fold,
        ),
    ).fetchone()
    return row_id


def delete_rows(connection: sqlite3.Connection, names: TableNames, row_ids: Collection[int]) -> None:
    """Delete the summary rows ``row_ids``."""
    connection.execute(
        f"DELETE FROM {quote_identifier(names.log_summaries)} WHERE id IN (SELECT value FROM json_each(?))",  # noqa: S608
        (json.dumps(list(row_ids)),),
    )


def record_removed(
    connection: sqlite3.Connection,
    names: TableNames,
    *,
    session_id: str,
    run_ids: Collection[str],
) -> None:
    """Record that a cut removed session ``session_id``'s runs ``run_ids``, so no later save writes them back."""
    connection.execute(
        f"INSERT OR IGNORE INTO {quote_identifier(names.log_removed)} (run_id, session_id) "  # noqa: S608
        "SELECT value, ? FROM json_each(?)",
        (session_id, json.dumps(sorted(run_ids))),
    )


def folded_messages(
    connection: sqlite3.Connection,
    names: TableNames,
    *,
    session_id: str,
    run_id: str,
) -> list[dict[str, Any]]:
    """Return the stored dicts of the messages of session ``session_id``'s run ``run_id`` that a fold took."""
    return [
        json.loads(data)
        for (data,) in connection.execute(
            f"SELECT data FROM {quote_identifier(names.log_messages)} WHERE folded_by IS NOT NULL AND run_id = "  # noqa: S608
            f"(SELECT run_id FROM {quote_identifier(names.log_runs)} WHERE session_id = ? AND run_id = ?)",
            (session_id, run_id),
        ).fetchall()
    ]


def folded_member_tools(
    connection: sqlite3.Connection,
    names: TableNames,
    *,
    session_id: str,
    run_id: str,
) -> list[dict[str, Any]]:
    """Return the stored dicts of the tool executions of run ``run_id``'s member runs that a fold took."""
    return [
        json.loads(data)
        for (data,) in connection.execute(
            f"SELECT tool.data FROM {quote_identifier(names.log_tools)} AS tool "  # noqa: S608
            f"JOIN {quote_identifier(names.log_runs)} AS run ON run.run_id = tool.run_id "
            "WHERE run.session_id = ? AND run.parent_run_id = ? AND run.folded_by IS NOT NULL",
            (session_id, run_id),
        ).fetchall()
    ]


def event_ids_after_barrier(
    connection: sqlite3.Connection,
    names: TableNames,
    *,
    session_id: str,
    scope_key: str,
    kind: Literal["seen", "all"],
) -> set[str]:
    """Return the ``seen_event_ids`` (``"seen"``) or ``event_ids`` (``"all"``) of the scope's rows after its latest barrier."""
    column = "seen_event_ids" if kind == "seen" else "event_ids"
    summaries = quote_identifier(names.log_summaries)
    rows = connection.execute(
        f"SELECT value FROM {summaries} AS row, json_each(row.{column}) "  # noqa: S608
        "WHERE row.session_id = ? AND row.scope_key = ? AND row.id > COALESCE("
        f"(SELECT MAX(id) FROM {summaries} WHERE session_id = ? AND scope_key = ? AND summary IS NULL), 0)",
        (session_id, scope_key, session_id, scope_key),
    ).fetchall()
    return {row[0] for row in rows}


def _encoded(event_ids: Collection[str]) -> str:
    return json.dumps(sorted(event_ids))


def _decoded(raw_event_ids: str | None) -> frozenset[str]:
    return frozenset(json.loads(raw_event_ids)) if raw_event_ids is not None else frozenset()


__all__ = [
    "FoldProvenance",
    "StoredRun",
    "SummaryRow",
    "append_row",
    "delete_rows",
    "event_ids_after_barrier",
    "folded_member_tools",
    "folded_messages",
    "record_removed",
    "scope_rows",
    "stored_runs",
    "top_level_runs",
    "unretired_runs",
]
