"""SQLite snapshot reads for the latest visible agent message in one cached scope."""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING, Any

from . import sqlite_event_cache_events, sqlite_event_cache_threads
from .agent_message_snapshot import AgentMessageSnapshot, AgentMessageSnapshotUnavailable
from .agent_message_snapshot_semantics import (
    SnapshotLookupResult,
    event_matches_snapshot_scope,
    snapshot_event_id,
    snapshot_lookup_result,
    thread_cache_has_no_snapshot,
)

if TYPE_CHECKING:
    import aiosqlite


async def _thread_scope_has_no_snapshot(
    db: aiosqlite.Connection,
    *,
    room_id: str,
    thread_id: str | None,
) -> bool:
    if thread_id is None:
        return False

    return thread_cache_has_no_snapshot(
        await sqlite_event_cache_threads.load_thread_cache_state(
            db,
            room_id=room_id,
            thread_id=thread_id,
        ),
    )


async def _snapshot_from_event(
    db: aiosqlite.Connection,
    *,
    room_id: str,
    thread_id: str | None,
    sender: str,
    event: dict[str, Any],
    cached_at: float | None,
    runtime_started_at: float | None,
) -> SnapshotLookupResult:
    event_id = snapshot_event_id(event)
    if event_id is None:
        return SnapshotLookupResult(snapshot=None)

    latest_edit = await sqlite_event_cache_events.load_latest_edit_row(
        db,
        room_id=room_id,
        original_event_id=event_id,
        sender=sender,
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
    room_id: str,
    thread_id: str | None,
) -> aiosqlite.Cursor:
    if thread_id is None:
        return await db.execute(
            """
            SELECT event_json, cached_at
            FROM events
            WHERE room_id = ?
            ORDER BY origin_server_ts DESC, rowid DESC
            """,
            (room_id,),
        )
    return await db.execute(
        """
        SELECT event_json, NULL AS cached_at
        FROM thread_events
        WHERE room_id = ? AND thread_id = ?
        ORDER BY origin_server_ts DESC, rowid DESC
        """,
        (room_id, thread_id),
    )


async def _load_scope_snapshot(
    db: aiosqlite.Connection,
    *,
    room_id: str,
    thread_id: str | None,
    sender: str,
    runtime_started_at: float | None,
) -> AgentMessageSnapshot | None:
    cursor = await _iter_scope_events(
        db,
        room_id=room_id,
        thread_id=thread_id,
    )
    try:
        while True:
            row = await cursor.fetchone()
            if row is None:
                return None
            event = json.loads(row[0])
            if not event_matches_snapshot_scope(
                event,
                thread_id=thread_id,
                sender=sender,
            ):
                continue
            result = await _snapshot_from_event(
                db,
                room_id=room_id,
                thread_id=thread_id,
                sender=sender,
                event=event,
                cached_at=None if row[1] is None else float(row[1]),
                runtime_started_at=runtime_started_at,
            )
            if result.stop_scanning:
                return None
            if result.snapshot is not None:
                return result.snapshot
    finally:
        await cursor.close()


async def load_sqlite_agent_message_snapshot(
    db: aiosqlite.Connection,
    *,
    room_id: str,
    thread_id: str | None,
    sender: str,
    runtime_started_at: float | None,
) -> AgentMessageSnapshot | None:
    """Return the latest visible message from ``sender`` in the given scope."""
    try:
        if await _thread_scope_has_no_snapshot(
            db,
            room_id=room_id,
            thread_id=thread_id,
        ):
            return None
        return await _load_scope_snapshot(
            db,
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
