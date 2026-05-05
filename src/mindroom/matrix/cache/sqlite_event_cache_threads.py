"""Thread snapshot and freshness storage helpers for the Matrix event cache."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

from .event_cache import ThreadCacheState
from .event_normalization import normalize_event_source_for_cache
from .sqlite_event_cache_events import (
    delete_cached_events,
    delete_event_edit_rows,
    delete_event_thread_rows,
    event_id_for_cache,
    event_or_original_is_redacted,
    filter_cacheable_events,
    serialize_cacheable_events,
    serialize_cached_event,
    write_lookup_index_rows,
)

if TYPE_CHECKING:
    import aiosqlite


_INCREMENTAL_THREAD_REVALIDATION_REASONS = frozenset(
    {
        "live_thread_mutation",
        "sync_thread_mutation",
        "outbound_thread_mutation",
    },
)


async def load_thread_events(
    db: aiosqlite.Connection,
    *,
    room_id: str,
    thread_id: str,
) -> list[dict[str, Any]] | None:
    """Return cached events for one thread sorted by timestamp."""
    cursor = await db.execute(
        """
        SELECT event_json
        FROM thread_events
        WHERE room_id = ? AND thread_id = ?
        ORDER BY origin_server_ts ASC, rowid ASC
        """,
        (room_id, thread_id),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    if not rows:
        return None
    return [json.loads(row[0]) for row in rows]


async def load_recent_room_thread_ids(
    db: aiosqlite.Connection,
    *,
    room_id: str,
    limit: int,
) -> list[str]:
    """Return thread IDs for one room ordered by the newest locally cached event timestamp."""
    cursor = await db.execute(
        """
        SELECT thread_id
        FROM thread_events
        WHERE room_id = ?
        GROUP BY thread_id
        ORDER BY MAX(origin_server_ts) DESC, thread_id ASC
        LIMIT ?
        """,
        (room_id, limit),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return [str(row[0]) for row in rows]


async def _load_thread_cache_state_row(
    db: aiosqlite.Connection,
    *,
    room_id: str,
    thread_id: str,
) -> tuple[float | None, float | None, str | None, float | None, str | None] | None:
    """Return one raw thread-cache-state row joined with room invalidation state."""
    cursor = await db.execute(
        """
        SELECT
            thread_cache_state.validated_at,
            thread_cache_state.invalidated_at,
            thread_cache_state.invalidation_reason,
            room_cache_state.invalidated_at,
            room_cache_state.invalidation_reason
        FROM (SELECT ? AS requested_room_id, ? AS requested_thread_id) AS requested
        LEFT JOIN thread_cache_state
            ON thread_cache_state.room_id = requested.requested_room_id
            AND thread_cache_state.thread_id = requested.requested_thread_id
        LEFT JOIN room_cache_state
            ON room_cache_state.room_id = requested.requested_room_id
        """,
        (room_id, thread_id),
    )
    row = await cursor.fetchone()
    await cursor.close()
    if row is None or all(value is None for value in row):
        return None
    return (
        None if row[0] is None else float(row[0]),
        None if row[1] is None else float(row[1]),
        row[2] if isinstance(row[2], str) else None,
        None if row[3] is None else float(row[3]),
        row[4] if isinstance(row[4], str) else None,
    )


async def load_thread_cache_state(
    db: aiosqlite.Connection,
    *,
    room_id: str,
    thread_id: str,
) -> ThreadCacheState | None:
    """Return one thread cache state object joined with room invalidation state."""
    row = await _load_thread_cache_state_row(
        db,
        room_id=room_id,
        thread_id=thread_id,
    )
    if row is None:
        return None
    return ThreadCacheState(
        validated_at=row[0],
        invalidated_at=row[1],
        invalidation_reason=row[2],
        room_invalidated_at=row[3],
        room_invalidation_reason=row[4],
    )


async def _store_thread_events_locked(
    db: aiosqlite.Connection,
    *,
    room_id: str,
    thread_id: str,
    events: list[dict[str, Any]],
    validated_at: float,
) -> None:
    """Persist one authoritative thread snapshot within an existing DB transaction."""
    if not events:
        await db.execute(
            """
            INSERT INTO thread_cache_state(
                room_id,
                thread_id,
                validated_at,
                invalidated_at,
                invalidation_reason
            )
            VALUES (?, ?, ?, NULL, NULL)
            ON CONFLICT(room_id, thread_id) DO UPDATE SET
                validated_at = excluded.validated_at,
                invalidated_at = NULL,
                invalidation_reason = NULL
            """,
            (room_id, thread_id, validated_at),
        )
        return

    normalized_events = [normalize_event_source_for_cache(event) for event in events]
    cacheable_events = await filter_cacheable_events(
        db,
        room_id,
        [(event_id_for_cache(event), event) for event in normalized_events],
    )
    serialized_events = serialize_cacheable_events(cacheable_events)
    if serialized_events:
        await db.executemany(
            """
            INSERT OR REPLACE INTO thread_events(room_id, thread_id, event_id, origin_server_ts, event_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    room_id,
                    thread_id,
                    event.event_id,
                    event.origin_server_ts,
                    event.event_json,
                )
                for event in serialized_events
            ],
        )
        await write_lookup_index_rows(
            db,
            room_id=room_id,
            serialized_events=serialized_events,
            cached_at=validated_at,
            thread_id=thread_id,
        )
    await db.execute(
        """
        INSERT INTO thread_cache_state(
            room_id,
            thread_id,
            validated_at,
            invalidated_at,
            invalidation_reason
        )
        VALUES (?, ?, ?, NULL, NULL)
        ON CONFLICT(room_id, thread_id) DO UPDATE SET
            validated_at = excluded.validated_at,
            invalidated_at = NULL,
            invalidation_reason = NULL
        """,
        (room_id, thread_id, validated_at),
    )


async def replace_thread_locked(
    db: aiosqlite.Connection,
    *,
    room_id: str,
    thread_id: str,
    events: list[dict[str, Any]],
    validated_at: float,
) -> None:
    """Replace one thread snapshot atomically within an existing DB transaction."""
    existing_event_ids = await _thread_event_ids_for_thread(db, room_id=room_id, thread_id=thread_id)
    await db.execute(
        """
        DELETE FROM thread_events
        WHERE room_id = ? AND thread_id = ?
        """,
        (room_id, thread_id),
    )
    if existing_event_ids:
        await delete_cached_events(db, event_ids=existing_event_ids)
        await delete_event_edit_rows(
            db,
            room_id,
            event_ids=existing_event_ids,
            original_event_id=None,
        )
        await delete_event_thread_rows(
            db,
            room_id,
            event_ids=existing_event_ids,
        )
    await _store_thread_events_locked(
        db,
        room_id=room_id,
        thread_id=thread_id,
        events=events,
        validated_at=validated_at,
    )


def _thread_cache_state_changed_after(
    cache_state_row: tuple[float | None, float | None, str | None, float | None, str | None] | None,
    *,
    fetch_started_at: float,
) -> bool:
    """Return whether this thread or room cache state changed after one fetch began."""
    if cache_state_row is None:
        return False
    validated_at, invalidated_at, _invalidation_reason, room_invalidated_at, _room_invalidation_reason = cache_state_row
    return any(
        timestamp is not None and timestamp > fetch_started_at
        for timestamp in (validated_at, invalidated_at, room_invalidated_at)
    )


async def replace_thread_locked_if_not_newer(
    db: aiosqlite.Connection,
    *,
    room_id: str,
    thread_id: str,
    events: list[dict[str, Any]],
    fetch_started_at: float,
    validated_at: float,
) -> bool:
    """Replace one thread snapshot only when nothing newer touched this room after the fetch began."""
    cache_state_row = await _load_thread_cache_state_row(
        db,
        room_id=room_id,
        thread_id=thread_id,
    )
    if _thread_cache_state_changed_after(cache_state_row, fetch_started_at=fetch_started_at):
        return False
    await replace_thread_locked(
        db,
        room_id=room_id,
        thread_id=thread_id,
        events=events,
        validated_at=validated_at,
    )
    return True


async def invalidate_thread_locked(
    db: aiosqlite.Connection,
    *,
    room_id: str,
    thread_id: str,
) -> None:
    """Delete cached events and state for one thread within an existing transaction."""
    event_ids = await _thread_event_ids_for_thread(db, room_id=room_id, thread_id=thread_id)
    await db.execute(
        """
        DELETE FROM thread_events
        WHERE room_id = ? AND thread_id = ?
        """,
        (room_id, thread_id),
    )
    if event_ids:
        await delete_cached_events(db, event_ids=event_ids)
        await delete_event_edit_rows(
            db,
            room_id,
            event_ids=event_ids,
            original_event_id=None,
        )
        await delete_event_thread_rows(
            db,
            room_id,
            event_ids=event_ids,
        )
    await db.execute(
        """
        DELETE FROM thread_cache_state
        WHERE room_id = ? AND thread_id = ?
        """,
        (room_id, thread_id),
    )


async def invalidate_room_threads_locked(
    db: aiosqlite.Connection,
    *,
    room_id: str,
) -> None:
    """Delete every cached thread snapshot and room state for one room."""
    event_ids = await _thread_event_ids_for_room(db, room_id=room_id)
    await db.execute(
        """
        DELETE FROM thread_events
        WHERE room_id = ?
        """,
        (room_id,),
    )
    if event_ids:
        await delete_cached_events(db, event_ids=event_ids)
        await delete_event_edit_rows(
            db,
            room_id,
            event_ids=event_ids,
            original_event_id=None,
        )
        await delete_event_thread_rows(
            db,
            room_id,
            event_ids=event_ids,
        )
    await db.execute(
        """
        DELETE FROM thread_cache_state
        WHERE room_id = ?
        """,
        (room_id,),
    )
    await db.execute(
        """
        DELETE FROM room_cache_state
        WHERE room_id = ?
        """,
        (room_id,),
    )


async def mark_thread_stale_locked(
    db: aiosqlite.Connection,
    *,
    room_id: str,
    thread_id: str,
    reason: str,
) -> None:
    """Persist a durable invalidate-and-refetch marker within an active transaction."""
    await db.execute(
        """
        INSERT INTO thread_cache_state(
            room_id,
            thread_id,
            validated_at,
            invalidated_at,
            invalidation_reason
        )
        VALUES (?, ?, NULL, ?, ?)
        ON CONFLICT(room_id, thread_id) DO UPDATE SET
            invalidated_at = CASE
                WHEN thread_cache_state.invalidated_at IS NULL
                    OR excluded.invalidated_at >= thread_cache_state.invalidated_at
                    THEN excluded.invalidated_at
                ELSE thread_cache_state.invalidated_at
            END,
            invalidation_reason = CASE
                WHEN thread_cache_state.invalidated_at IS NULL
                    OR excluded.invalidated_at >= thread_cache_state.invalidated_at
                    THEN excluded.invalidation_reason
                ELSE thread_cache_state.invalidation_reason
            END
        """,
        (room_id, thread_id, time.time(), reason),
    )


async def revalidate_thread_after_incremental_update_locked(
    db: aiosqlite.Connection,
    *,
    room_id: str,
    thread_id: str,
) -> bool:
    """Mark one thread cache fresh after a safe incremental update."""
    row = await _load_thread_cache_state_row(
        db,
        room_id=room_id,
        thread_id=thread_id,
    )
    if row is None:
        return False
    validated_at, invalidated_at, invalidation_reason, room_invalidated_at, _room_invalidation_reason = row
    can_revalidate = (
        validated_at is not None
        and invalidated_at is not None
        and invalidation_reason in _INCREMENTAL_THREAD_REVALIDATION_REASONS
        and not (room_invalidated_at is not None and room_invalidated_at >= validated_at)
    )
    if not can_revalidate:
        return False
    await db.execute(
        """
        UPDATE thread_cache_state
        SET validated_at = ?, invalidated_at = NULL, invalidation_reason = NULL
        WHERE room_id = ? AND thread_id = ?
        """,
        (time.time(), room_id, thread_id),
    )
    return True


async def mark_room_stale_locked(
    db: aiosqlite.Connection,
    *,
    room_id: str,
    reason: str,
) -> None:
    """Persist one durable room-scoped invalidate-and-refetch marker."""
    await db.execute(
        """
        INSERT INTO room_cache_state(
            room_id,
            invalidated_at,
            invalidation_reason
        )
        VALUES (?, ?, ?)
        ON CONFLICT(room_id) DO UPDATE SET
            invalidated_at = CASE
                WHEN room_cache_state.invalidated_at IS NULL
                    OR excluded.invalidated_at >= room_cache_state.invalidated_at
                    THEN excluded.invalidated_at
                ELSE room_cache_state.invalidated_at
            END,
            invalidation_reason = CASE
                WHEN room_cache_state.invalidated_at IS NULL
                    OR excluded.invalidated_at >= room_cache_state.invalidated_at
                    THEN excluded.invalidation_reason
                ELSE room_cache_state.invalidation_reason
            END
        """,
        (room_id, time.time(), reason),
    )


async def append_existing_thread_event(
    db: aiosqlite.Connection,
    *,
    room_id: str,
    thread_id: str,
    normalized_event: dict[str, Any],
) -> bool:
    """Append one event to an existing cached thread."""
    event_id = event_id_for_cache(normalized_event)
    if await event_or_original_is_redacted(
        db,
        room_id,
        event_id=event_id,
        event=normalized_event,
    ):
        return False

    serialized_event = serialize_cached_event(event_id, normalized_event)
    cursor = await db.execute(
        """
        SELECT 1
        FROM thread_events
        WHERE room_id = ? AND thread_id = ?
        LIMIT 1
        """,
        (room_id, thread_id),
    )
    row = await cursor.fetchone()
    await cursor.close()
    if row is None:
        await write_lookup_index_rows(
            db,
            room_id=room_id,
            serialized_events=[serialized_event],
            cached_at=time.time(),
            thread_id=thread_id,
        )
        return False

    await db.execute(
        """
        INSERT OR REPLACE INTO thread_events(room_id, thread_id, event_id, origin_server_ts, event_json)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            room_id,
            thread_id,
            serialized_event.event_id,
            serialized_event.origin_server_ts,
            serialized_event.event_json,
        ),
    )
    await write_lookup_index_rows(
        db,
        room_id=room_id,
        serialized_events=[serialized_event],
        cached_at=time.time(),
        thread_id=thread_id,
    )
    return True


async def _thread_event_ids_for_thread(
    db: aiosqlite.Connection,
    *,
    room_id: str,
    thread_id: str,
) -> list[str]:
    """Return cached event IDs currently stored for one thread."""
    cursor = await db.execute(
        """
        SELECT event_id
        FROM thread_events
        WHERE room_id = ? AND thread_id = ?
        """,
        (room_id, thread_id),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return [str(row[0]) for row in rows]


async def _thread_event_ids_for_room(
    db: aiosqlite.Connection,
    *,
    room_id: str,
) -> list[str]:
    """Return cached event IDs currently stored for every thread in one room."""
    cursor = await db.execute(
        """
        SELECT event_id
        FROM thread_events
        WHERE room_id = ?
        """,
        (room_id,),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return [str(row[0]) for row in rows]
