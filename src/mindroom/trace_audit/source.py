"""Strictly read-only SQLite reader for MindRoom v8 conversation logs and usage rows."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from mindroom.automations.steps import is_automation_hook_source
from mindroom.constants import HOOK_SOURCE_RUN_METADATA_KEY
from mindroom.llm.messages import Message, is_media_follow_up, is_queued_notice_message
from mindroom.llm.runs import FailingTool, ToolExecution
from mindroom.session_ids import session_in_room
from mindroom.session_storage.schema import TableNames, quote_identifier, table_names
from mindroom.trace_audit.models import AuditedRun, AuditedSession, RunMessageRecord, ToolExecutionRecord

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from collections.abc import Set as AbstractSet
    from pathlib import Path

_WORKFLOW_MARKER = ":dynamic_workflow:"
_OWN_THREAD_KEY = "mindroom_own_thread_message"
_COMPACTION_KEY = "mindroom_compaction_summary"
_CHUNK_SIZE = 500


@contextmanager
def _open_read_only(db_path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)
    try:
        connection.execute("PRAGMA query_only = ON")
        yield connection
    finally:
        connection.close()


def _json_dict(raw: object) -> dict[str, Any]:
    if not isinstance(raw, (str, bytes, bytearray)):
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _infer_room_id(session_id: str, session_meta: dict[str, Any], run_metas: list[dict[str, Any]]) -> str | None:
    for meta in (session_meta, *run_metas):
        if isinstance(room := meta.get("room_id"), str) and room.strip():
            return room.strip()
    if session_id.startswith("!"):
        if ":$" in session_id:
            return session_id.split(":$", 1)[0]
        parts = session_id.split(":")
        return ":".join(parts[:2]) if len(parts) >= 2 else session_id
    return None


def _matches_room(session_id: str, room_id: str | None, allowed_room_id: str | None) -> bool:
    if allowed_room_id is None:
        return True
    return room_id == allowed_room_id if room_id is not None else session_in_room(session_id, allowed_room_id)


def _fetch_chunked(
    connection: sqlite3.Connection,
    table: str,
    run_ids: list[str],
) -> Iterator[tuple[str, int, dict[str, Any]]]:
    quoted = quote_identifier(table)
    for start in range(0, len(run_ids), _CHUNK_SIZE):
        chunk = run_ids[start : start + _CHUNK_SIZE]
        holders = ", ".join("?" for _ in chunk)
        rows = connection.execute(
            f"SELECT run_id, ord, data FROM {quoted} WHERE run_id IN ({holders}) ORDER BY run_id, ord",  # noqa: S608
            chunk,
        ).fetchall()
        for run_id, ord_idx, raw_data in rows:
            if payload := _json_dict(raw_data):
                yield str(run_id), int(ord_idx), payload


def _load_tools_and_messages(
    connection: sqlite3.Connection,
    names: TableNames,
    run_ids: list[str],
) -> tuple[dict[str, list[ToolExecutionRecord]], dict[str, list[RunMessageRecord]]]:
    tools_by_run: dict[str, list[ToolExecutionRecord]] = defaultdict(list)
    for run_id, ord_idx, payload in _fetch_chunked(connection, names.log_tools, run_ids):
        try:
            te = ToolExecution.from_dict(payload)
        except (KeyError, TypeError, ValueError):
            continue
        res = te.result if isinstance(te.result, str) else ("" if te.result is None else str(te.result))
        tools_by_run[run_id].append(
            ToolExecutionRecord(
                ord=ord_idx,
                tool_call_id=te.tool_call_id,
                tool_name=te.tool_name,
                tool_args=dict(te.tool_args) if isinstance(te.tool_args, dict) else {},
                is_error=bool(te.is_error),
                result=res,
                confirmed=te.confirmed,
                requires_confirmation=bool(te.requires_confirmation),
            ),
        )

    messages_by_run: dict[str, list[RunMessageRecord]] = defaultdict(list)
    for run_id, ord_idx, payload in _fetch_chunked(connection, names.log_messages, run_ids):
        try:
            msg = Message.from_dict(payload)
        except (KeyError, TypeError, ValueError):
            continue
        pdata = msg.provider_data or {}
        synthetic = (
            is_queued_notice_message(msg)
            or is_media_follow_up(msg)
            or pdata.get(_OWN_THREAD_KEY) is True
            or pdata.get(_COMPACTION_KEY) is True
        )
        messages_by_run[run_id].append(
            RunMessageRecord(
                ord=ord_idx,
                role=msg.role,
                content=msg.get_content_string(),
                created_at=float(msg.created_at) if msg.created_at is not None else None,
                add_to_agent_memory=msg.add_to_agent_memory,
                from_history=msg.from_history,
                is_synthetic=synthetic,
            ),
        )
    return tools_by_run, messages_by_run


def _parse_failing_tools(raw: object) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    counts: dict[str, int] = {}
    for name, val in raw.items():
        if isinstance(name, str) and isinstance(val, dict):
            try:
                failing = FailingTool.from_dict(val)
            except (TypeError, ValueError):
                continue
            if failing.failures > 0:
                counts[name] = failing.failures
    return counts


def _build_run(
    row: tuple[str, str, str, int, float, float, dict[str, Any], dict[str, Any]],
    tools: list[ToolExecutionRecord],
    messages: list[RunMessageRecord],
) -> AuditedRun:
    rid, sid, status, seq, created, updated, meta, record = row
    raw_content = record.get("content")
    content = (
        raw_content
        if isinstance(raw_content, str) and raw_content.strip()
        else next(
            (
                m.content
                for m in reversed(messages)
                if m.role == "assistant" and not m.from_history and m.content.strip()
            ),
            "",
        )
    )
    metrics = record.get("metrics")
    raw_dur = metrics.get("duration") if isinstance(metrics, dict) else None
    duration = float(raw_dur) if isinstance(raw_dur, (int, float)) and not isinstance(raw_dur, bool) else None
    raw_error = record.get("error")
    return AuditedRun(
        run_id=rid,
        session_id=sid,
        seq=seq,
        status=status,
        created_at=created,
        updated_at=updated,
        duration_seconds=duration,
        error=raw_error if isinstance(raw_error, str) and raw_error.strip() else None,
        content=content,
        tools=tuple(tools),
        messages=tuple(messages),
        failing_tools=_parse_failing_tools(record.get("failing_tools")),
        metadata=meta,
    )


def _read_session_index(
    connection: sqlite3.Connection,
    names: TableNames,
    *,
    excluded_sessions: AbstractSet[str],
    contact_session_check: Callable[[str], bool] | None,
) -> dict[str, tuple[dict[str, Any], float, float]]:
    rows: dict[str, tuple[dict[str, Any], float, float]] = {}
    for session_id, raw_meta, created_at, updated_at in connection.execute(
        f"SELECT session_id, metadata, created_at, updated_at FROM {quote_identifier(names.sessions)}",  # noqa: S608
    ):
        sid = str(session_id)
        if _WORKFLOW_MARKER in sid or sid in excluded_sessions:
            continue
        if contact_session_check is not None and contact_session_check(sid):
            continue
        created = float(created_at or 0.0)
        rows[sid] = (_json_dict(raw_meta), created, float(updated_at) if updated_at is not None else created)
    return rows


def _read_terminal_runs(
    connection: sqlite3.Connection,
    names: TableNames,
    *,
    valid_sessions: AbstractSet[str],
    removed_ids: AbstractSet[str],
    since_ts: float | None,
    until_ts: float | None,
) -> dict[str, list[tuple[str, str, str, int, float, float, dict[str, Any], dict[str, Any]]]]:
    by_session: dict[str, list[tuple[str, str, str, int, float, float, dict[str, Any], dict[str, Any]]]] = defaultdict(
        list,
    )
    query = (
        f"SELECT run_id, session_id, status, seq, created_at, updated_at, metadata, record "  # noqa: S608
        f"FROM {quote_identifier(names.log_runs)} "
        "WHERE kind = 'agent' AND parent_run_id IS NULL "
        "AND status IN ('COMPLETED', 'ERROR', 'CANCELLED') ORDER BY session_id, seq"
    )
    for rid, sid, status, seq, created_at, updated_at, raw_meta, raw_record in connection.execute(query):
        sid_s, rid_s = str(sid), str(rid)
        if sid_s not in valid_sessions or rid_s in removed_ids:
            continue
        created = float(created_at or 0.0)
        if (since_ts is not None and created < since_ts) or (until_ts is not None and created > until_ts):
            continue
        meta = _json_dict(raw_meta)
        hook_source = meta.get(HOOK_SOURCE_RUN_METADATA_KEY)
        if isinstance(hook_source, str) and is_automation_hook_source(hook_source):
            continue
        updated = float(updated_at) if updated_at is not None else created
        by_session[sid_s].append((rid_s, sid_s, str(status), int(seq), created, updated, meta, _json_dict(raw_record)))
    return by_session


def load_audited_sessions(
    db_path: Path,
    storage_name: str,
    *,
    since_ts: float | None = None,
    until_ts: float | None = None,
    excluded_sessions: AbstractSet[str] = frozenset(),
    allowed_room_id: str | None = None,
    contact_session_check: Callable[[str], bool] | None = None,
) -> list[AuditedSession]:
    """Load terminal top-level agent runs per session from a v8 SQLite database strictly read-only."""
    if not db_path.exists():
        return []
    names = TableNames(storage_name)
    with _open_read_only(db_path) as conn:
        tables = table_names(conn)
        if not {names.sessions, names.log_runs, names.log_tools, names.log_messages}.issubset(tables):
            return []
        removed = (
            {str(rid) for (rid,) in conn.execute(f"SELECT run_id FROM {quote_identifier(names.log_removed)}")}  # noqa: S608
            if names.log_removed in tables
            else set()
        )
        session_rows = _read_session_index(
            conn,
            names,
            excluded_sessions=excluded_sessions,
            contact_session_check=contact_session_check,
        )
        if not session_rows:
            return []
        raw_runs = _read_terminal_runs(
            conn,
            names,
            valid_sessions=session_rows.keys(),
            removed_ids=removed,
            since_ts=since_ts,
            until_ts=until_ts,
        )
        kept: list[tuple[str, str | None, dict[str, Any], float, float]] = []
        kept_run_ids: list[str] = []
        for sid, runs in raw_runs.items():
            meta, created, updated = session_rows[sid]
            room_id = _infer_room_id(sid, meta, [r[6] for r in runs])
            if _matches_room(sid, room_id, allowed_room_id):
                kept.append((sid, room_id, meta, created, updated))
                kept_run_ids.extend(r[0] for r in runs)
        if not kept_run_ids:
            return []
        tools_by_run, messages_by_run = _load_tools_and_messages(conn, names, kept_run_ids)

    sessions = [
        AuditedSession(
            session_id=sid,
            room_id=room_id,
            created_at=min(created, built[0].created_at),
            last_updated_at=max(r.updated_at for r in built)
            if until_ts is not None
            else max(updated, *(r.updated_at for r in built)),
            runs=tuple(built),
            metadata=meta,
        )
        for sid, room_id, meta, created, updated in kept
        if (built := [_build_run(r, tools_by_run.get(r[0], []), messages_by_run.get(r[0], [])) for r in raw_runs[sid]])
    ]
    sessions.sort(key=lambda s: (s.last_updated_at, s.session_id))
    return sessions


def load_usage_rows(
    db_path: Path,
    storage_name: str,
    *,
    kind: str | None = None,
    since_ts: float | None = None,
    until_ts: float | None = None,
) -> list[dict[str, Any]]:
    """Read usage ledger snapshots from ``<storage_name>_sessions_usage`` strictly read-only."""
    if not db_path.exists():
        return []
    names = TableNames(storage_name)
    with _open_read_only(db_path) as conn:
        if names.usage not in table_names(conn):
            return []
        rows = conn.execute(
            f"SELECT session_id, run_id, usage_data FROM {quote_identifier(names.usage)} ORDER BY id",  # noqa: S608
        ).fetchall()

    results: list[dict[str, Any]] = []
    for session_id, run_id, raw_usage in rows:
        payload = _json_dict(raw_usage)
        if not payload or (kind is not None and (payload.get("kind") or "run") != kind):
            continue
        raw_ts = (
            payload.get("updated_at")
            if isinstance(payload.get("updated_at"), (int, float))
            else payload.get("created_at")
        )
        if since_ts is not None and (not isinstance(raw_ts, (int, float)) or float(raw_ts) < since_ts):
            continue
        if until_ts is not None and (not isinstance(raw_ts, (int, float)) or float(raw_ts) > until_ts):
            continue
        entry = {**payload, "session_id": payload.get("session_id", str(session_id))}
        if run_id is not None:
            entry.setdefault("run_id", str(run_id))
        results.append(entry)
    return results
