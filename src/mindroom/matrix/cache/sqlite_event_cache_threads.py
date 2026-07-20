"""Thread snapshot and freshness storage helpers for the Matrix event cache.

Durable trust-state invariants (mirrored by ``postgres_event_cache_threads``):

1. Stale markers are monotonic: ``mark_thread_stale_locked`` and ``mark_room_stale_locked`` never let an
   older ``invalidated_at`` or its reason overwrite a newer one.
   A revalidatable incremental reason additionally never overwrites an active full-refetch reason, so a
   later clear mutation cannot weaken a fail-closed invalidation into one an append may clear.

2. Snapshot replacement is race-guarded: ``replace_thread_locked_if_not_newer`` refuses when
   ``validated_at``, ``invalidated_at``, or ``room_invalidated_at`` changed after the fetch began, so a
   slow fetch cannot bury an invalidation that landed mid-flight (PR #716).
   The concrete caches additionally clamp the stored ``validated_at`` to the fetch start time, so an
   invalidation that lands during the fetch still outranks the snapshot at read time.

3. Incremental revalidation is allowlisted: ``revalidate_thread_after_incremental_update_locked`` clears
   an invalidation only when the thread was previously validated, the invalidation reason is one of the
   incremental mutation reasons, and the room was not invalidated at or after that validation.
   Invalidations from any other reason can only be cleared by a full authoritative snapshot replacement.

4. Thread snapshot rows and the lookup, edit, and thread index rows are written and deleted together so
   point lookups can never resurrect rows the snapshot no longer contains.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any, Literal

from .event_cache_events import (
    event_id_for_cache,
    serialize_cacheable_events,
    serialize_cached_event,
)
from .event_normalization import normalize_event_source_for_cache
from .sqlite_event_cache_events import (
    allocate_write_sequences,
    delete_cached_events,
    delete_event_edit_rows,
    delete_event_thread_rows,
    event_or_original_is_redacted,
    filter_cacheable_events,
    write_lookup_index_rows,
)
from .thread_cache_state import (
    ThreadCacheStateRow,
    can_revalidate_after_incremental_update,
    incremental_thread_revalidation_reasons,
    is_incremental_thread_revalidation_reason,
    thread_cache_state_changed_after,
    thread_cache_state_row,
)

if TYPE_CHECKING:
    import aiosqlite

    from .event_cache import ThreadCacheState


async def load_thread_events(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
    room_id: str,
    thread_id: str,
) -> list[dict[str, Any]] | None:
    """Return cached events for one thread sorted by timestamp."""
    cursor = await db.execute(
        """
        SELECT thread_events.origin_server_ts, thread_events.write_seq, events.event_json
        FROM thread_events
        JOIN events
            ON events.principal_id = thread_events.principal_id
            AND events.room_id = thread_events.room_id
            AND events.event_id = thread_events.event_id
        WHERE thread_events.principal_id = ?
            AND thread_events.room_id = ?
            AND thread_events.thread_id = ?
        ORDER BY thread_events.origin_server_ts ASC, thread_events.write_seq ASC
        """,
        (principal_id, room_id, thread_id),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    if not rows:
        return None
    return [json.loads(row[2]) for row in rows]


async def load_recent_room_thread_ids(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
    room_id: str,
    limit: int,
) -> list[str]:
    """Return thread IDs for one room ordered by the newest locally cached event timestamp."""
    cursor = await db.execute(
        """
        SELECT thread_id
        FROM thread_events
        WHERE principal_id = ? AND room_id = ?
        GROUP BY thread_id
        ORDER BY MAX(origin_server_ts) DESC, thread_id ASC
        LIMIT ?
        """,
        (principal_id, room_id, limit),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return [str(row[0]) for row in rows]


async def _load_thread_cache_state_row(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
    room_id: str,
    thread_id: str,
) -> ThreadCacheStateRow | None:
    """Return one raw thread-cache-state row joined with room invalidation state."""
    cursor = await db.execute(
        """
        SELECT
            thread_cache_state.validated_at,
            thread_cache_state.invalidated_at,
            thread_cache_state.invalidation_reason,
            room_cache_state.invalidated_at,
            room_cache_state.invalidation_reason
        FROM (
            SELECT ? AS requested_principal_id, ? AS requested_room_id, ? AS requested_thread_id
        ) AS requested
        LEFT JOIN thread_cache_state
            ON thread_cache_state.principal_id = requested.requested_principal_id
            AND thread_cache_state.room_id = requested.requested_room_id
            AND thread_cache_state.thread_id = requested.requested_thread_id
        LEFT JOIN room_cache_state
            ON room_cache_state.principal_id = requested.requested_principal_id
            AND room_cache_state.room_id = requested.requested_room_id
        """,
        (principal_id, room_id, thread_id),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return thread_cache_state_row(row)


async def load_thread_cache_state(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
    room_id: str,
    thread_id: str,
) -> ThreadCacheState | None:
    """Return one thread cache state object joined with room invalidation state."""
    row = await _load_thread_cache_state_row(
        db,
        principal_id=principal_id,
        room_id=room_id,
        thread_id=thread_id,
    )
    if row is None:
        return None
    return row.as_public_state()


async def load_room_membership_locked(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
    room_id: str,
) -> tuple[str, int]:
    """Return the durable membership state and transition epoch for one principal-room."""
    cursor = await db.execute(
        """
        SELECT membership_state, membership_epoch
        FROM room_cache_state
        WHERE principal_id = ? AND room_id = ?
        """,
        (principal_id, room_id),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return ("joined", 0) if row is None else (str(row[0]), int(row[1]))


async def certify_room_membership_locked(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
    room_id: str,
) -> int:
    """Create a durable generation row and return its current epoch."""
    await db.execute(
        """
        INSERT OR IGNORE INTO room_cache_state(
            principal_id,
            room_id,
            membership_state,
            membership_epoch
        )
        VALUES (?, ?, 'joined', 0)
        """,
        (principal_id, room_id),
    )
    _membership_state, membership_epoch = await load_room_membership_locked(
        db,
        principal_id=principal_id,
        room_id=room_id,
    )
    return membership_epoch


async def set_room_membership_locked(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
    room_id: str,
    membership_state: Literal["joined", "departed"],
    reason: str,
) -> None:
    """Advance one durable room-membership transition and invalidate prior refills."""
    await mark_room_stale_locked(
        db,
        principal_id=principal_id,
        room_id=room_id,
        reason=reason,
    )
    await db.execute(
        """
        UPDATE room_cache_state
        SET membership_state = ?, membership_epoch = membership_epoch + 1
        WHERE principal_id = ? AND room_id = ?
        """,
        (membership_state, principal_id, room_id),
    )


async def _store_thread_events_locked(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
    room_id: str,
    thread_id: str,
    events: list[dict[str, Any]],
    validated_at: float,
) -> frozenset[str]:
    """Persist one authoritative thread snapshot within an existing DB transaction."""
    normalized_events = [normalize_event_source_for_cache(event) for event in events]
    cacheable_events = await filter_cacheable_events(
        db,
        principal_id,
        room_id,
        [(event_id_for_cache(event), event) for event in normalized_events],
    )
    serialized_events = serialize_cacheable_events(cacheable_events)
    if serialized_events:
        await write_lookup_index_rows(
            db,
            principal_id=principal_id,
            room_id=room_id,
            serialized_events=serialized_events,
            cached_at=validated_at,
            thread_id=thread_id,
        )
        write_sequences = await allocate_write_sequences(db, len(serialized_events))
        await db.executemany(
            """
            INSERT INTO thread_events(
                principal_id,
                room_id,
                thread_id,
                event_id,
                origin_server_ts,
                write_seq
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(principal_id, room_id, event_id) DO UPDATE SET
                thread_id = excluded.thread_id,
                origin_server_ts = excluded.origin_server_ts,
                write_seq = excluded.write_seq
            """,
            [
                (
                    principal_id,
                    room_id,
                    thread_id,
                    event.event_id,
                    event.origin_server_ts,
                    write_sequence,
                )
                for event, write_sequence in zip(serialized_events, write_sequences, strict=True)
            ],
        )
    await db.execute(
        """
        INSERT INTO thread_cache_state(
            principal_id,
            room_id,
            thread_id,
            validated_at,
            invalidated_at,
            invalidation_reason
        )
        VALUES (?, ?, ?, ?, NULL, NULL)
        ON CONFLICT(principal_id, room_id, thread_id) DO UPDATE SET
            validated_at = excluded.validated_at,
            invalidated_at = NULL,
            invalidation_reason = NULL
        """,
        (principal_id, room_id, thread_id, validated_at),
    )
    return frozenset(event.event_id for event in serialized_events)


async def _replace_thread_locked(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
    room_id: str,
    thread_id: str,
    events: list[dict[str, Any]],
    validated_at: float,
) -> None:
    """Replace one thread snapshot atomically within an existing DB transaction."""
    existing_event_ids = await _thread_event_ids_for_thread(
        db,
        principal_id=principal_id,
        room_id=room_id,
        thread_id=thread_id,
    )
    replacement_event_ids = await _store_thread_events_locked(
        db,
        principal_id=principal_id,
        room_id=room_id,
        thread_id=thread_id,
        events=events,
        validated_at=validated_at,
    )
    removed_event_ids = sorted(set(existing_event_ids) - replacement_event_ids)
    if removed_event_ids:
        await db.executemany(
            """
            DELETE FROM thread_events
            WHERE principal_id = ? AND room_id = ? AND event_id = ?
            """,
            [(principal_id, room_id, event_id) for event_id in removed_event_ids],
        )
        await delete_cached_events(
            db,
            principal_id=principal_id,
            room_id=room_id,
            event_ids=removed_event_ids,
        )
        await delete_event_edit_rows(
            db,
            principal_id,
            room_id,
            event_ids=removed_event_ids,
            original_event_id=None,
        )
        await delete_event_thread_rows(
            db,
            principal_id,
            room_id,
            event_ids=removed_event_ids,
        )


async def replace_thread_locked_if_not_newer(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
    room_id: str,
    thread_id: str,
    events: list[dict[str, Any]],
    fetch_started_at: float,
    validated_at: float,
) -> bool:
    """Replace one thread snapshot only when nothing newer touched this room after the fetch began."""
    cache_state_row = await _load_thread_cache_state_row(
        db,
        principal_id=principal_id,
        room_id=room_id,
        thread_id=thread_id,
    )
    if thread_cache_state_changed_after(cache_state_row, fetch_started_at=fetch_started_at):
        return False
    await _replace_thread_locked(
        db,
        principal_id=principal_id,
        room_id=room_id,
        thread_id=thread_id,
        events=events,
        validated_at=validated_at,
    )
    return True


async def invalidate_thread_locked(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
    room_id: str,
    thread_id: str,
) -> None:
    """Delete cached events and state for one thread within an existing transaction."""
    event_ids = await _thread_event_ids_for_thread(
        db,
        principal_id=principal_id,
        room_id=room_id,
        thread_id=thread_id,
    )
    await db.execute(
        """
        DELETE FROM thread_events
        WHERE principal_id = ? AND room_id = ? AND thread_id = ?
        """,
        (principal_id, room_id, thread_id),
    )
    if event_ids:
        await delete_cached_events(
            db,
            principal_id=principal_id,
            room_id=room_id,
            event_ids=event_ids,
        )
        await delete_event_edit_rows(
            db,
            principal_id,
            room_id,
            event_ids=event_ids,
            original_event_id=None,
        )
        await delete_event_thread_rows(
            db,
            principal_id,
            room_id,
            event_ids=event_ids,
        )
    await db.execute(
        """
        DELETE FROM thread_cache_state
        WHERE principal_id = ? AND room_id = ? AND thread_id = ?
        """,
        (principal_id, room_id, thread_id),
    )


async def invalidate_room_threads_locked(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
    room_id: str,
) -> None:
    """Delete every cached thread snapshot while preserving durable room membership."""
    event_ids = await _thread_event_ids_for_room(db, principal_id=principal_id, room_id=room_id)
    await db.execute(
        """
        DELETE FROM thread_events
        WHERE principal_id = ? AND room_id = ?
        """,
        (principal_id, room_id),
    )
    if event_ids:
        await delete_cached_events(
            db,
            principal_id=principal_id,
            room_id=room_id,
            event_ids=event_ids,
        )
        await delete_event_edit_rows(
            db,
            principal_id,
            room_id,
            event_ids=event_ids,
            original_event_id=None,
        )
        await delete_event_thread_rows(
            db,
            principal_id,
            room_id,
            event_ids=event_ids,
        )
    await db.execute(
        """
        DELETE FROM thread_cache_state
        WHERE principal_id = ? AND room_id = ?
        """,
        (principal_id, room_id),
    )


async def mark_thread_stale_locked(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
    room_id: str,
    thread_id: str,
    reason: str,
) -> None:
    """Persist a durable invalidate-and-refetch marker within an active transaction."""
    incremental_reasons_json = json.dumps(incremental_thread_revalidation_reasons())
    incoming_is_incremental = is_incremental_thread_revalidation_reason(reason)
    await db.execute(
        """
        INSERT INTO thread_cache_state(
            principal_id,
            room_id,
            thread_id,
            validated_at,
            invalidated_at,
            invalidation_reason
        )
        VALUES (?, ?, ?, NULL, ?, ?)
        ON CONFLICT(principal_id, room_id, thread_id) DO UPDATE SET
            invalidated_at = CASE
                WHEN thread_cache_state.invalidated_at IS NULL
                    OR excluded.invalidated_at >= thread_cache_state.invalidated_at
                    THEN excluded.invalidated_at
                ELSE thread_cache_state.invalidated_at
            END,
            invalidation_reason = CASE
                WHEN thread_cache_state.invalidated_at IS NULL
                    THEN excluded.invalidation_reason
                WHEN ?
                    AND NOT COALESCE(
                        thread_cache_state.invalidation_reason
                            IN (SELECT value FROM json_each(?)),
                        FALSE
                    )
                    THEN thread_cache_state.invalidation_reason
                WHEN NOT ?
                    AND COALESCE(
                        thread_cache_state.invalidation_reason
                            IN (SELECT value FROM json_each(?)),
                        FALSE
                    )
                    THEN excluded.invalidation_reason
                WHEN excluded.invalidated_at >= thread_cache_state.invalidated_at
                    THEN excluded.invalidation_reason
                ELSE thread_cache_state.invalidation_reason
            END
        """,
        (
            principal_id,
            room_id,
            thread_id,
            time.time(),
            reason,
            incoming_is_incremental,
            incremental_reasons_json,
            incoming_is_incremental,
            incremental_reasons_json,
        ),
    )


async def revalidate_thread_after_incremental_update_locked(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
    room_id: str,
    thread_id: str,
) -> bool:
    """Mark one thread cache fresh after a safe incremental update."""
    row = await _load_thread_cache_state_row(
        db,
        principal_id=principal_id,
        room_id=room_id,
        thread_id=thread_id,
    )
    if not can_revalidate_after_incremental_update(row):
        return False
    await db.execute(
        """
        UPDATE thread_cache_state
        SET validated_at = ?, invalidated_at = NULL, invalidation_reason = NULL
        WHERE principal_id = ? AND room_id = ? AND thread_id = ?
        """,
        (time.time(), principal_id, room_id, thread_id),
    )
    return True


async def mark_room_stale_locked(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
    room_id: str,
    reason: str,
) -> None:
    """Persist one durable room-scoped invalidate-and-refetch marker."""
    await db.execute(
        """
        INSERT INTO room_cache_state(
            principal_id,
            room_id,
            invalidated_at,
            invalidation_reason
        )
        VALUES (?, ?, ?, ?)
        ON CONFLICT(principal_id, room_id) DO UPDATE SET
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
        (principal_id, room_id, time.time(), reason),
    )


async def append_existing_thread_event(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
    room_id: str,
    thread_id: str,
    normalized_event: dict[str, Any],
) -> bool:
    """Append one event to an existing cached thread.

    An opaque ``m.room.encrypted`` payload never replaces stored clear content for the same event ID.
    """
    event_id = event_id_for_cache(normalized_event)
    if await event_or_original_is_redacted(
        db,
        principal_id,
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
        JOIN events
            ON events.principal_id = thread_events.principal_id
            AND events.room_id = thread_events.room_id
            AND events.event_id = thread_events.event_id
        WHERE thread_events.principal_id = ?
            AND thread_events.room_id = ?
            AND thread_events.thread_id = ?
        LIMIT 1
        """,
        (principal_id, room_id, thread_id),
    )
    row = await cursor.fetchone()
    await cursor.close()
    await write_lookup_index_rows(
        db,
        principal_id=principal_id,
        room_id=room_id,
        serialized_events=[serialized_event],
        cached_at=time.time(),
        thread_id=thread_id,
    )
    if row is None:
        return False

    write_sequence = (await allocate_write_sequences(db, 1))[0]
    await db.execute(
        """
        INSERT INTO thread_events(
            principal_id,
            room_id,
            thread_id,
            event_id,
            origin_server_ts,
            write_seq
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(principal_id, room_id, event_id) DO UPDATE SET
            thread_id = excluded.thread_id,
            origin_server_ts = excluded.origin_server_ts,
            write_seq = excluded.write_seq
        """,
        (
            principal_id,
            room_id,
            thread_id,
            serialized_event.event_id,
            serialized_event.origin_server_ts,
            write_sequence,
        ),
    )
    return True


async def _thread_event_ids_for_thread(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
    room_id: str,
    thread_id: str,
) -> list[str]:
    """Return cached event IDs currently stored for one thread."""
    cursor = await db.execute(
        """
        SELECT event_id
        FROM thread_events
        WHERE principal_id = ? AND room_id = ? AND thread_id = ?
        """,
        (principal_id, room_id, thread_id),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return [str(row[0]) for row in rows]


async def _thread_event_ids_for_room(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
    room_id: str,
) -> list[str]:
    """Return cached event IDs currently stored for every thread in one room."""
    cursor = await db.execute(
        """
        SELECT event_id
        FROM thread_events
        WHERE principal_id = ? AND room_id = ?
        """,
        (principal_id, room_id),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return [str(row[0]) for row in rows]
