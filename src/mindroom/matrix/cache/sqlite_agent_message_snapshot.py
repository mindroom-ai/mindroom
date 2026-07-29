"""SQLite snapshot reads for the latest visible agent message in one cached scope."""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING, Any

from mindroom.matrix.media import valid_room_message_replacement

from . import sqlite_event_cache_events, sqlite_event_cache_threads
from .agent_message_snapshot import AgentMessageSnapshot, AgentMessageSnapshotUnavailable
from .agent_message_snapshot_semantics import (
    load_agent_message_snapshot,
    reject_snapshot_scope_with_gap,
)

if TYPE_CHECKING:
    import aiosqlite

    from .event_cache_events import CachedEventRow


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
            ORDER BY thread_events.origin_server_ts DESC, thread_events.write_seq DESC
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


async def _scope_snapshot(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
    room_id: str,
    thread_id: str | None,
    sender: str,
    runtime_started_at: float | None,
) -> AgentMessageSnapshot | None:
    """Run the shared snapshot scan over this backend's scope cursor."""
    cursor = await _iter_scope_events(db, principal_id=principal_id, room_id=room_id, thread_id=thread_id)

    async def next_row() -> tuple[dict[str, Any], float | None] | None:
        row = await cursor.fetchone()
        return None if row is None else (json.loads(row[0]), None if row[1] is None else float(row[1]))

    async def latest_edit_lookup(event: dict[str, Any]) -> CachedEventRow | None:
        return await sqlite_event_cache_events.load_latest_edit_row(
            db,
            principal_id=principal_id,
            room_id=room_id,
            original=event,
            validator=valid_room_message_replacement,
        )

    try:
        return await load_agent_message_snapshot(
            latest_edit_lookup=latest_edit_lookup,
            next_row=next_row,
            room_id=room_id,
            thread_id=thread_id,
            sender=sender,
            runtime_started_at=runtime_started_at,
        )
    finally:
        await cursor.close()


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
        return await _scope_snapshot(
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
