"""SQLite snapshot reads for the latest visible agent message in one cached scope."""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING, Any

from mindroom.matrix.media import valid_room_message_replacement

from . import sqlite_event_cache_events, sqlite_event_cache_threads
from .agent_message_snapshot import AgentMessageSnapshot, AgentMessageSnapshotUnavailable
from .agent_message_snapshot_semantics import (
    SnapshotLookupResult,
    event_matches_snapshot_scope,
    reject_snapshot_scope_with_gap,
    resolved_snapshot_thread_event_ids,
    snapshot_event_id,
    snapshot_lookup_result,
)

if TYPE_CHECKING:
    import aiosqlite


async def _reject_thread_scope_with_gap(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
    room_id: str,
    thread_id: str | None,
) -> None:
    if thread_id is None:
        return

    reject_snapshot_scope_with_gap(
        await sqlite_event_cache_threads.load_thread_cache_gap(
            db,
            principal_id=principal_id,
            room_id=room_id,
            thread_id=thread_id,
        ),
    )


async def _snapshot_from_event(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
    room_id: str,
    thread_id: str | None,
    event: dict[str, Any],
    cached_at: float | None,
    runtime_started_at: float | None,
) -> SnapshotLookupResult:
    if snapshot_event_id(event) is None:
        return SnapshotLookupResult(snapshot=None)

    latest_edit = await sqlite_event_cache_events.load_latest_edit_row(
        db,
        principal_id=principal_id,
        room_id=room_id,
        original=event,
        validator=valid_room_message_replacement,
    )
    return snapshot_lookup_result(
        event,
        latest_edit=latest_edit,
        thread_id=thread_id,
        cached_at=cached_at,
        runtime_started_at=runtime_started_at,
    )


async def _iter_scope_events(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
    room_id: str,
    thread_id: str | None,
) -> aiosqlite.Cursor:
    if thread_id is not None:
        return await db.execute(
            """
            SELECT events.event_json, events.cached_at
            FROM thread_events
            JOIN events
                ON events.principal_id = thread_events.principal_id
                AND events.room_id = thread_events.room_id
                AND events.event_id = thread_events.event_id
            WHERE thread_events.principal_id = ?
                AND thread_events.room_id = ?
                AND thread_events.thread_id = ?
            ORDER BY events.origin_server_ts DESC, thread_events.write_seq DESC
            """,
            (principal_id, room_id, thread_id),
        )
    return await db.execute(
        """
        SELECT event_json, cached_at
        FROM events
        WHERE principal_id = ? AND room_id = ?
        ORDER BY origin_server_ts DESC, write_seq DESC
        """,
        (principal_id, room_id),
    )


async def _scope_snapshot_for_event(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
    room_id: str,
    thread_id: str | None,
    sender: str,
    event: dict[str, Any],
    cached_at: float | None,
    runtime_started_at: float | None,
) -> SnapshotLookupResult | None:
    """Return one candidate row's snapshot outcome, or None when it is out of scope."""
    if not event_matches_snapshot_scope(event, room_id=room_id, thread_id=thread_id, sender=sender):
        return None
    return await _snapshot_from_event(
        db,
        principal_id=principal_id,
        room_id=room_id,
        thread_id=thread_id,
        event=event,
        cached_at=cached_at,
        runtime_started_at=runtime_started_at,
    )


async def _load_room_scope_snapshot(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
    room_id: str,
    sender: str,
    runtime_started_at: float | None,
) -> AgentMessageSnapshot | None:
    """Stream the room-scoped query, stopping at the first row that answers it."""
    cursor = await _iter_scope_events(db, principal_id=principal_id, room_id=room_id, thread_id=None)
    try:
        while (row := await cursor.fetchone()) is not None:
            result = await _scope_snapshot_for_event(
                db,
                principal_id=principal_id,
                room_id=room_id,
                thread_id=None,
                sender=sender,
                event=json.loads(row[0]),
                cached_at=None if row[1] is None else float(row[1]),
                runtime_started_at=runtime_started_at,
            )
            if result is None:
                continue
            if result.stop_scanning:
                return None
            if result.snapshot is not None:
                return result.snapshot
        return None
    finally:
        await cursor.close()


async def _load_thread_scope_snapshot(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
    room_id: str,
    thread_id: str,
    sender: str,
    runtime_started_at: float | None,
) -> AgentMessageSnapshot | None:
    """Resolve one whole indexed thread graph, then take its newest proven member.

    This cannot stream like the room scope: membership can be indirect, and the ancestors that
    prove it are older than the candidate they prove, so they arrive later in a newest-first scan.
    Materializing is bounded by one thread.
    """
    cursor = await _iter_scope_events(db, principal_id=principal_id, room_id=room_id, thread_id=thread_id)
    try:
        rows = [(json.loads(row[0]), None if row[1] is None else float(row[1])) for row in await cursor.fetchall()]
    finally:
        await cursor.close()

    thread_event_ids = await resolved_snapshot_thread_event_ids(
        [event for event, _cached_at in rows],
        room_id=room_id,
        thread_id=thread_id,
    )
    for event, cached_at in rows:
        if event.get("event_id") not in thread_event_ids:
            continue
        result = await _scope_snapshot_for_event(
            db,
            principal_id=principal_id,
            room_id=room_id,
            thread_id=thread_id,
            sender=sender,
            event=event,
            cached_at=cached_at,
            runtime_started_at=runtime_started_at,
        )
        if result is None:
            continue
        if result.stop_scanning:
            return None
        if result.snapshot is not None:
            return result.snapshot
    return None


async def load_sqlite_agent_message_snapshot(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
    room_id: str,
    thread_id: str | None,
    sender: str,
    runtime_started_at: float | None,
) -> AgentMessageSnapshot | None:
    """Return the latest visible message from ``sender`` in the given scope."""
    try:
        await _reject_thread_scope_with_gap(
            db,
            principal_id=principal_id,
            room_id=room_id,
            thread_id=thread_id,
        )
        if thread_id is None:
            return await _load_room_scope_snapshot(
                db,
                principal_id=principal_id,
                room_id=room_id,
                sender=sender,
                runtime_started_at=runtime_started_at,
            )
        return await _load_thread_scope_snapshot(
            db,
            principal_id=principal_id,
            room_id=room_id,
            thread_id=thread_id,
            sender=sender,
            runtime_started_at=runtime_started_at,
        )
    except json.JSONDecodeError as exc:
        msg = "Cached Matrix event JSON is corrupt"
        raise AgentMessageSnapshotUnavailable(msg) from exc
    except sqlite3.Error as exc:
        msg = "Failed to read Matrix event cache snapshot"
        raise AgentMessageSnapshotUnavailable(msg) from exc
