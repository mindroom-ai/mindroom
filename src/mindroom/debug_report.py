"""Collect everything the backend stored about one reported conversation.

Every reader is read-only. The runtime's own openers create or migrate schema,
so they are never used here: inspecting an install must not change it.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, LiteralString, cast

if TYPE_CHECKING:
    from pathlib import Path

_AI_RUN_KEY = "io.mindroom.ai_run"
_JOURNAL_SOURCES = ("turn_records", "journal_events", "delivery_outbox")
_AGNO_JSON_COLUMNS = frozenset({"run_data"})
_MAX_JSONL_RECORDS = 1000
_MAX_LOG_LINES = 2000
_MAX_LOG_LINE_CHARS = 4000
_TOOL_CALL_ROTATIONS = 5

_Query = Callable[[str, Sequence[object]], list[dict[str, Any]]]


@dataclass(frozen=True)
class _DebugReportIds:
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
            if key == _AI_RUN_KEY and isinstance(child, Mapping):
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
) -> _DebugReportIds:
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
    return _DebugReportIds(
        room_id=room_id,
        thread_id=thread_id,
        # Local echoes ("~…") never reached the homeserver, so the backend cannot know them.
        event_ids=frozenset(event for event in events if event and not event.startswith("~")),
        run_ids=frozenset(run_ids),
        session_ids=frozenset(session_ids),
    )


@dataclass
class _SourceResult:
    """What one storage source held for the identifiers."""

    status: str
    paths: list[str]
    items: list[Any] = field(default_factory=list)
    dropped: int = 0
    truncated: int = 0
    error: str | None = None


def _describe_error(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"


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
def _sqlite_query(path: Path) -> Iterator[_Query]:
    """Open one SQLite database read-only and yield a query function returning dict rows."""
    connection = sqlite3.connect(f"{path.absolute().as_uri()}?mode=ro", uri=True, timeout=1.0)
    connection.row_factory = sqlite3.Row
    try:

        def query(sql: str, params: Sequence[object]) -> list[dict[str, Any]]:
            return [dict(row) for row in connection.execute(sql, tuple(params))]

        yield query
    finally:
        connection.close()


@contextmanager
def _postgres_query(database_url: str) -> Iterator[_Query]:
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


def _read_journal(query: _Query, ids: _DebugReportIds, location: str) -> dict[str, _SourceResult]:
    """Read turn records, admitted events, and outbound deliveries for the identifiers."""
    results = {name: _SourceResult("ok", [location]) for name in _JOURNAL_SOURCES}
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


def _read_agno_runs(session_root: Path, ids: _DebugReportIds) -> _SourceResult:
    """Read Agno runs by run ID or session ID from every session database under the session root.

    Databases are found by path rather than from the config so deleted agents' history is included.
    One unreadable database (locked by a live run, corrupt) is isolated: the runs of the others are kept and
    the failure is recorded as `<path>: <error>` in `error`. The status is "error" only when every database
    failed, and a database that fails part-way contributes none of its rows.
    """
    databases = sorted(session_root.glob("**/sessions/*.db"))
    if not databases:
        return _SourceResult("missing", [str(session_root)])
    result = _SourceResult("ok", [str(database) for database in databases])
    run_marks, run_ids = _in_list(ids.run_ids)
    session_marks, session_ids = _in_list(ids.session_ids)
    conditions = [
        condition
        for condition, values in (
            (f"run_id IN ({run_marks})", run_ids),
            (f"session_id IN ({session_marks})", session_ids),
        )
        if values
    ]
    if not conditions:
        return result
    failures: list[str] = []
    for database in databases:
        try:
            result.items.extend(_read_agno_database(database, conditions, [*run_ids, *session_ids]))
        except (sqlite3.Error, OSError) as error:
            failures.append(f"{database}: {_describe_error(error)}")
    if failures:
        result.error = "; ".join(failures)
        if len(failures) == len(databases):
            result.status = "error"
    return result


def _read_agno_database(database: Path, conditions: Sequence[str], params: Sequence[object]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    with _sqlite_query(database) as query:
        tables = [row["name"] for row in query("SELECT name FROM sqlite_master WHERE type = 'table'", [])]
        for table in sorted(name for name in tables if name.endswith("_runs")):
            columns = {row["name"] for row in query(f'PRAGMA table_info("{table}")', [])}
            if not {"run_id", "session_id", "run_data"} <= columns:
                continue
            rows = query(
                f'SELECT * FROM "{table}" WHERE {" OR ".join(conditions)} ORDER BY rowid',  # noqa: S608 - table names come from sqlite_master
                params,
            )
            items.extend(
                {"database": str(database), "table": table, **_decode_json_columns(row, _AGNO_JSON_COLUMNS)}
                for row in rows
            )
    return items


def _record_matches(record: Mapping[str, Any], ids: _DebugReportIds) -> bool:
    return (
        record.get("correlation_id") in ids.event_ids
        or record.get("reply_to_event_id") in ids.event_ids
        or record.get("session_id") in ids.session_ids
    )


def _read_jsonl_records(paths: Sequence[Path], ids: _DebugReportIds, *, location: Path) -> _SourceResult:
    """Read JSONL records whose correlation, reply target, or session matches, oldest file first."""
    existing = [path for path in paths if path.is_file()]
    if not existing:
        return _SourceResult("missing", [str(location)])
    result = _SourceResult("ok", [str(path) for path in existing])
    needles = tuple(ids.event_ids | ids.session_ids)
    for path in existing:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                # Cheap prefilter: request logs embed whole prompts, so parse only candidate lines.
                if not any(needle in line for needle in needles):
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict) or not _record_matches(record, ids):
                    continue
                if len(result.items) >= _MAX_JSONL_RECORDS:
                    result.dropped += 1
                    continue
                result.items.append(record)
    return result


def _read_log_lines(paths: Sequence[Path], ids: _DebugReportIds, *, location: Path) -> _SourceResult:
    """Read log lines that mention an event or run ID, bounded because lines can embed whole prompts.

    Room IDs are not matched: every line about the room would match and bury the turn.
    """
    existing = [path for path in paths if path.is_file()]
    if not existing:
        return _SourceResult("missing", [str(location)])
    result = _SourceResult("ok", [str(path) for path in existing])
    needles = tuple(ids.event_ids | ids.run_ids)
    if not needles:
        return result
    for path in existing:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for number, line in enumerate(handle, start=1):
                if not any(needle in line for needle in needles):
                    continue
                if len(result.items) >= _MAX_LOG_LINES:
                    result.dropped += 1
                    continue
                text = line.rstrip("\n")
                if len(text) > _MAX_LOG_LINE_CHARS:
                    text = text[:_MAX_LOG_LINE_CHARS]
                    result.truncated += 1
                result.items.append({"file": path.name, "line": number, "text": text})
    return result


@dataclass(frozen=True)
class DebugReportSources:
    """Where one install keeps the data a debug report reads."""

    storage_root: Path
    session_root: Path
    journal_sqlite_path: Path | None
    journal_postgres_url: str | None
    llm_request_log_dir: Path
    # Why the journal could not be located (for example no PostgreSQL URL in this shell).
    # It is reported instead of falling back to the SQLite path, which would read the wrong database.
    journal_error: str | None = None


def _failed(location: str, error: Exception) -> _SourceResult:
    return _SourceResult("error", [location], error=_describe_error(error))


def _read_journal_source(sources: DebugReportSources, ids: _DebugReportIds) -> dict[str, _SourceResult]:
    """Read the journal group, or mark all of it failed: one unreadable journal says nothing about the others."""
    if sources.journal_error is not None:
        return {name: _SourceResult("error", ["postgres"], error=sources.journal_error) for name in _JOURNAL_SOURCES}
    if sources.journal_postgres_url is not None:
        import psycopg  # noqa: PLC0415 - psycopg ships with the optional postgres extra

        try:
            with _postgres_query(sources.journal_postgres_url) as query:
                return _read_journal(query, ids, "postgres")
        except (psycopg.Error, OSError) as error:
            return {name: _failed("postgres", error) for name in _JOURNAL_SOURCES}
    path = sources.journal_sqlite_path
    try:
        # `is_file()` raises PermissionError when a parent directory is unreadable, so it belongs in the guard.
        if path is None or not path.is_file():
            return {name: _SourceResult("missing", [str(path)]) for name in _JOURNAL_SOURCES}
        with _sqlite_query(path) as query:
            return _read_journal(query, ids, str(path))
    except (sqlite3.Error, OSError) as error:
        return {name: _failed(str(path), error) for name in _JOURNAL_SOURCES}


def _read_guarded(read: Callable[[], _SourceResult], location: Path) -> _SourceResult:
    """Run one file or SQLite source read; a locked or corrupt store must not abort the other sources."""
    try:
        return read()
    except (sqlite3.Error, OSError) as error:
        return _failed(str(location), error)


def build_debug_report(sources: DebugReportSources, ids: _DebugReportIds, *, generated_at: str) -> dict[str, Any]:
    """Collect every backend source for the identifiers into one JSON-ready document."""
    tracking = sources.storage_root / "tracking"
    tool_call_logs = [tracking / f"tool_calls.jsonl.{n}" for n in range(_TOOL_CALL_ROTATIONS, 0, -1)]
    tool_call_logs.append(tracking / "tool_calls.jsonl")
    logs_dir = sources.storage_root / "logs"
    results = _read_journal_source(sources, ids)
    results["agno_runs"] = _read_guarded(lambda: _read_agno_runs(sources.session_root, ids), sources.session_root)
    results["tool_calls"] = _read_guarded(
        lambda: _read_jsonl_records(tool_call_logs, ids, location=tracking / "tool_calls.jsonl"),
        tracking / "tool_calls.jsonl",
    )
    results["llm_requests"] = _read_guarded(
        lambda: _read_jsonl_records(
            sorted(sources.llm_request_log_dir.glob("llm-requests-*.jsonl")),
            ids,
            location=sources.llm_request_log_dir,
        ),
        sources.llm_request_log_dir,
    )
    results["log_lines"] = _read_guarded(
        lambda: _read_log_lines(sorted(logs_dir.glob("mindroom_*.log")), ids, location=logs_dir),
        logs_dir,
    )
    return {
        "type": "io.mindroom.debug_report",
        "version": 1,
        "generatedAt": generated_at,
        "storageRoot": str(sources.storage_root),
        "identifiers": ids.to_json(),
        "sources": {name: asdict(result) for name, result in results.items()},
    }
