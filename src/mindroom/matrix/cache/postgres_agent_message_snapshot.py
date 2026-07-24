"""PostgreSQL snapshot reads for the latest visible agent message in one cached scope."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import psycopg

from . import postgres_event_cache_events, postgres_event_cache_threads
from .agent_message_snapshot import AgentMessageSnapshot, AgentMessageSnapshotUnavailable
from .agent_message_snapshot_semantics import (
    SnapshotLookupResult,
    event_matches_snapshot_scope,
    snapshot_event_id,
    snapshot_lookup_result,
    thread_cache_has_no_snapshot,
)

if TYPE_CHECKING:
    from psycopg import AsyncConnection, AsyncCursor


async def _thread_scope_has_no_snapshot(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str,
    thread_id: str | None,
) -> bool:
    if thread_id is None:
        return False

    return thread_cache_has_no_snapshot(
        await postgres_event_cache_threads.load_thread_cache_state(
            db,
            namespace=namespace,
            room_id=room_id,
            thread_id=thread_id,
        ),
    )


async def _snapshot_from_event(
    db: AsyncConnection,
    *,
    namespace: str,
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

    latest_edit = await postgres_event_cache_events.load_latest_edit_row(
        db,
        namespace=namespace,
        room_id=room_id,
        original_event_id=event_id,
        sender=sender,
        event_type="m.room.message",
    )
    return snapshot_lookup_result(
        event,
        latest_edit=latest_edit,
        thread_id=thread_id,
        cached_at=cached_at,
        runtime_started_at=runtime_started_at,
    )


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
            ORDER BY thread_events.origin_server_ts DESC, thread_events.write_seq DESC
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


async def _load_scope_snapshot(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str,
    thread_id: str | None,
    sender: str,
    runtime_started_at: float | None,
) -> AgentMessageSnapshot | None:
    cursor = await _iter_scope_events(
        db,
        namespace=namespace,
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
                room_id=room_id,
                thread_id=thread_id,
                sender=sender,
            ):
                continue
            result = await _snapshot_from_event(
                db,
                namespace=namespace,
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
        if await _thread_scope_has_no_snapshot(
            db,
            namespace=namespace,
            room_id=room_id,
            thread_id=thread_id,
        ):
            return None
        return await _load_scope_snapshot(
            db,
            namespace=namespace,
            room_id=room_id,
            thread_id=thread_id,
            sender=sender,
            runtime_started_at=runtime_started_at,
        )
    except json.JSONDecodeError as exc:
        msg = "Cached Matrix event JSON is corrupt"
        raise AgentMessageSnapshotUnavailable(msg) from exc
    except psycopg.Error as exc:
        msg = "Failed to read Matrix event cache snapshot"
        raise AgentMessageSnapshotUnavailable(msg) from exc
