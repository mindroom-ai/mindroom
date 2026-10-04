"""Collect everything the backend stored about one reported conversation.

Every reader is read-only. The runtime's own openers create or migrate schema,
so they are never used here: inspecting an install must not change it.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, LiteralString, cast

if TYPE_CHECKING:
    from pathlib import Path

AI_RUN_KEY = "io.mindroom.ai_run"
JOURNAL_SOURCES = ("turn_records", "journal_events", "delivery_outbox")

Query = Callable[[str, Sequence[object]], list[dict[str, Any]]]


@dataclass(frozen=True)
class DebugReportIds:
    """Identifiers of one reported conversation."""

    room_id: str | None = None
    thread_id: str | None = None
    event_ids: frozenset[str] = frozenset()
    run_ids: frozenset[str] = frozenset()
    session_ids: frozenset[str] = frozenset()

    def is_empty(self) -> bool:
        """Return whether there is nothing to look up."""
        return not (self.room_id or self.thread_id or self.event_ids or self.run_ids)

    def to_json(self) -> dict[str, object]:
        """Return a JSON-ready view with sorted lists."""
        return {
            "roomId": self.room_id,
            "threadId": self.thread_id,
            "eventIds": sorted(self.event_ids),
            "runIds": sorted(self.run_ids),
            "sessionIds": sorted(self.session_ids),
        }


def _ai_runs(value: object) -> Iterator[Mapping[str, Any]]:
    """Yield every AI run block nested anywhere in an event; streaming edits carry it in m.new_content."""
    if isinstance(value, Mapping):
        for key, child in cast("Mapping[str, object]", value).items():
            if key == AI_RUN_KEY and isinstance(child, Mapping):
                yield cast("Mapping[str, Any]", child)
            yield from _ai_runs(child)
    elif isinstance(value, list):
        for child in value:
            yield from _ai_runs(child)


def _scan_event(raw: object, events: set[str], run_ids: set[str], session_ids: set[str]) -> None:
    """Add the event id and every nested AI run identifier of one raw event."""
    if not isinstance(raw, Mapping):
        return
    event = cast("Mapping[str, Any]", raw)
    if isinstance(event.get("event_id"), str):
        events.add(event["event_id"])
    for ai_run in _ai_runs(raw):
        if isinstance(ai_run.get("run_id"), str):
            run_ids.add(ai_run["run_id"])
        if isinstance(ai_run.get("session_id"), str):
            session_ids.add(ai_run["session_id"])


def collect_ids(
    report: Mapping[str, Any] | None,
    *,
    event_ids: Iterable[str] = (),
    room_id: str | None = None,
    thread_id: str | None = None,
) -> DebugReportIds:
    """Merge identifiers from a MindRoom Chat bug report and explicit flags."""
    events = set(event_ids)
    run_ids: set[str] = set()
    session_ids: set[str] = set()
    if report is not None:
        target = report.get("target") or {}
        room_id = room_id or target.get("roomId")
        thread_id = thread_id or target.get("threadId")
        if isinstance(target.get("eventId"), str):
            events.add(target["eventId"])
        for entry in report.get("events") or []:
            _scan_event(entry.get("event"), events, run_ids, session_ids)
            _scan_event(entry.get("latestEdit"), events, run_ids, session_ids)
    if thread_id:
        events.add(thread_id)
    if room_id:
        session_ids.add(f"{room_id}:{thread_id}" if thread_id else room_id)
    return DebugReportIds(
        room_id=room_id,
        thread_id=thread_id,
        # Local echoes ("~…") never reached the homeserver, so the backend cannot know them.
        event_ids=frozenset(event for event in events if event and not event.startswith("~")),
        run_ids=frozenset(run_ids),
        session_ids=frozenset(session_ids),
    )


@dataclass
class SourceResult:
    """What one storage source held for the identifiers."""

    status: str
    paths: list[str]
    items: list[Any] = field(default_factory=list)
    dropped: int = 0
    truncated: int = 0


def _decode_json_columns(row: Mapping[str, Any], extra: frozenset[str] = frozenset()) -> dict[str, Any]:
    """Decode JSON stored as text, so the report nests objects instead of escaped strings."""
    decoded: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, str) and (key.endswith("_json") or key in extra):
            try:
                decoded[key] = json.loads(value)
            except json.JSONDecodeError:
                decoded[key] = value
        else:
            decoded[key] = value
    return decoded


@contextmanager
def sqlite_query(path: Path) -> Iterator[Query]:
    """Open one SQLite database read-only and yield a query function returning dict rows."""
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=1.0)
    connection.row_factory = sqlite3.Row
    try:

        def query(sql: str, params: Sequence[object]) -> list[dict[str, Any]]:
            return [dict(row) for row in connection.execute(sql, tuple(params))]

        yield query
    finally:
        connection.close()


@contextmanager
def postgres_query(database_url: str) -> Iterator[Query]:
    """Open the PostgreSQL event journal read-only and yield a query function returning dict rows."""
    import psycopg  # noqa: PLC0415 - psycopg ships with the optional postgres extra
    from psycopg.rows import dict_row  # noqa: PLC0415

    # The row factory is chosen per cursor: that is where psycopg and the type checker agree on the row type.
    with psycopg.connect(database_url, autocommit=True) as connection:
        # `Connection.read_only` only shapes transactions psycopg begins, and autocommit begins none,
        # so it would leave every statement writable. The session default is what the server enforces.
        connection.execute("SET default_transaction_read_only = on")

        def query(sql: str, params: Sequence[object]) -> list[dict[str, Any]]:
            with connection.cursor(row_factory=dict_row) as cursor:
                # Statements here are written with "?" and contain no literal question marks.
                cursor.execute(cast("LiteralString", sql.replace("?", "%s")), tuple(params))
                return list(cursor.fetchall())

        yield query


def _in_list(values: Iterable[str]) -> tuple[str, list[str]]:
    ordered = sorted(values)
    return ", ".join("?" for _ in ordered), ordered


def read_journal(query: Query, ids: DebugReportIds, location: str) -> dict[str, SourceResult]:
    """Read turn records, admitted events, and outbound deliveries for the identifiers."""
    results = {name: SourceResult("ok", [location]) for name in JOURNAL_SOURCES}
    marks, event_ids = _in_list(ids.event_ids)
    thread_known = bool(ids.room_id and ids.thread_id)

    if event_ids:
        rows = query(
            f"SELECT * FROM turn_records WHERE index_event_id IN ({marks}) OR anchor_event_id IN ({marks}) "  # noqa: S608 - placeholders only
            "ORDER BY agent_name, index_event_id",
            [*event_ids, *event_ids],
        )
        results["turn_records"].items = [_decode_json_columns(row) for row in rows]

    conditions: list[str] = []
    params: list[object] = []
    if event_ids:
        conditions.append(f"event_id IN ({marks})")
        params.extend(event_ids)
    if thread_known:
        conditions.append("(room_id = ? AND thread_id = ?)")
        params.extend([ids.room_id, ids.thread_id])
    if conditions:
        rows = query(
            f"SELECT * FROM journal_events WHERE {' OR '.join(conditions)} ORDER BY receipt_order",  # noqa: S608 - placeholders only
            params,
        )
        results["journal_events"].items = [_decode_json_columns(row) for row in rows]

    if thread_known:
        rows = query(
            "SELECT * FROM matrix_delivery_outbox WHERE room_id = ? AND thread_id = ?",
            [ids.room_id, ids.thread_id],
        )
        results["delivery_outbox"].items = [_decode_json_columns(row) for row in rows]
    return results
