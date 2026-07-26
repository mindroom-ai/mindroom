"""PostgreSQL snapshot reads for the latest visible agent message in one cached scope."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import psycopg

from mindroom.matrix.media import valid_room_message_replacement

from . import postgres_event_cache_events, postgres_event_cache_threads
from .agent_message_snapshot import AgentMessageSnapshot, AgentMessageSnapshotUnavailable
from .agent_message_snapshot_semantics import load_agent_message_snapshot

if TYPE_CHECKING:
    from psycopg import AsyncConnection, AsyncCursor


async def _iter_scope_events(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str,
    thread_id: str | None,
) -> AsyncCursor[tuple[str, float | None]]:
    if thread_id is not None:
        return await db.execute(
            """
            SELECT events.event_json, events.cached_at
            FROM mindroom_event_cache_thread_events AS thread_events
            JOIN mindroom_event_cache_events AS events
                ON events.namespace = thread_events.namespace
                AND events.event_id = thread_events.event_id
                AND events.room_id = thread_events.room_id
            WHERE thread_events.namespace = %s
                AND thread_events.room_id = %s
                AND thread_events.thread_id = %s
            ORDER BY events.origin_server_ts DESC, thread_events.write_seq DESC
            """,
            (namespace, room_id, thread_id),
        )
    return await db.execute(
        """
        SELECT event_json, cached_at
        FROM mindroom_event_cache_events
        WHERE namespace = %s AND room_id = %s
        ORDER BY origin_server_ts DESC, write_seq DESC
        """,
        (namespace, room_id),
    )


async def load_postgres_agent_message_snapshot(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str,
    thread_id: str | None,
    sender: str,
    runtime_started_at: float | None,
) -> AgentMessageSnapshot | None:
    """Return the latest visible message from ``sender`` in the given scope."""
    try:
        cache_state = (
            await postgres_event_cache_threads.load_thread_cache_state(
                db,
                namespace=namespace,
                room_id=room_id,
                thread_id=thread_id,
            )
            if thread_id is not None
            else None
        )
        cursor = await _iter_scope_events(
            db,
            namespace=namespace,
            room_id=room_id,
            thread_id=thread_id,
        )
        try:
            return await load_agent_message_snapshot(
                cache_state=cache_state,
                latest_edit_lookup=lambda event: postgres_event_cache_events.load_latest_edit_row(
                    db,
                    namespace=namespace,
                    room_id=room_id,
                    original=event,
                    validator=valid_room_message_replacement,
                ),
                next_row=cursor.fetchone,
                room_id=room_id,
                thread_id=thread_id,
                sender=sender,
                runtime_started_at=runtime_started_at,
            )
        finally:
            await cursor.close()
    except json.JSONDecodeError as exc:
        msg = "Cached Matrix event JSON is corrupt"
        raise AgentMessageSnapshotUnavailable(msg) from exc
    except psycopg.Error as exc:
        msg = "Failed to read Matrix event cache snapshot"
        raise AgentMessageSnapshotUnavailable(msg) from exc
