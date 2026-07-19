"""Event lookup, normalization, index, and redaction storage for the Matrix event cache."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from .event_cache_events import (
    CachedEventRow,
    SerializedCachedEvent,
    batch_redaction_candidate_ids,
    cache_rows_were_deleted,
    event_edit_rows,
    event_redaction_candidate_ids,
    event_thread_rows,
    filter_redacted_events,
    redaction_removal_event_ids,
    serialize_cacheable_events,
)

if TYPE_CHECKING:
    import aiosqlite

_ORPHAN_THREAD_INDEX_PREDICATE = """
    NOT EXISTS (
        SELECT 1
        FROM events
        WHERE events.event_id = event_threads.event_id
            AND events.room_id = event_threads.room_id
    )
    AND NOT (
        event_threads.event_id = event_threads.thread_id
        AND (
            EXISTS (
                SELECT 1
                FROM event_threads AS child
                WHERE child.room_id = event_threads.room_id
                    AND child.thread_id = event_threads.thread_id
                    AND child.event_id != child.thread_id
                    AND EXISTS (
                        SELECT 1
                        FROM events AS child_event
                        WHERE child_event.event_id = child.event_id
                            AND child_event.room_id = child.room_id
                    )
            )
            OR EXISTS (
                SELECT 1
                FROM thread_events AS child_membership
                WHERE child_membership.room_id = event_threads.room_id
                    AND child_membership.thread_id = event_threads.thread_id
                    AND child_membership.event_id != child_membership.thread_id
                    AND EXISTS (
                        SELECT 1
                        FROM events AS child_event
                        WHERE child_event.event_id = child_membership.event_id
                            AND child_event.room_id = child_membership.room_id
                    )
            )
        )
    )
"""


async def load_event(
    db: aiosqlite.Connection,
    *,
    event_id: str,
) -> dict[str, Any] | None:
    """Return one cached event payload by event ID."""
    cursor = await db.execute(
        """
        SELECT event_json
        FROM events
        WHERE event_id = ?
        """,
        (event_id,),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return None if row is None else json.loads(row[0])


async def load_recent_room_events(
    db: aiosqlite.Connection,
    *,
    room_id: str,
    event_type: str,
    since_ts_ms: int,
    limit: int,
) -> list[dict[str, Any]]:
    """Return recent cached room events of one type, newest first."""
    if limit <= 0:
        return []
    cursor = await db.execute(
        """
        SELECT event_json
        FROM events
        WHERE room_id = ?
            AND origin_server_ts >= ?
            AND json_extract(event_json, '$.type') = ?
        ORDER BY origin_server_ts DESC, write_seq DESC
        LIMIT ?
        """,
        (room_id, since_ts_ms, event_type, limit),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return [json.loads(row[0]) for row in rows]


async def load_latest_edit(
    db: aiosqlite.Connection,
    *,
    room_id: str,
    original_event_id: str,
    sender: str | None = None,
) -> dict[str, Any] | None:
    """Return the latest cached edit event for one original event."""
    row = await _load_latest_edit_row(
        db,
        room_id=room_id,
        original_event_id=original_event_id,
        sender=sender,
    )
    return None if row is None else row.event


async def load_latest_edit_row(
    db: aiosqlite.Connection,
    *,
    room_id: str,
    original_event_id: str,
    sender: str,
) -> CachedEventRow | None:
    """Return the latest cached edit event plus its lookup-row write time."""
    return await _load_latest_edit_row(
        db,
        room_id=room_id,
        original_event_id=original_event_id,
        sender=sender,
    )


async def _load_latest_edit_row(
    db: aiosqlite.Connection,
    *,
    room_id: str,
    original_event_id: str,
    sender: str | None,
) -> CachedEventRow | None:
    sender_predicate = "" if sender is None else "AND json_extract(events.event_json, '$.sender') = ?"
    parameters = (room_id, original_event_id, *((sender,) if sender is not None else ()))
    cursor = await db.execute(
        f"""
        SELECT events.event_json, events.cached_at
        FROM event_edits
        JOIN events ON events.event_id = event_edits.edit_event_id
        WHERE event_edits.room_id = ?
            AND event_edits.original_event_id = ?
            {sender_predicate}
        ORDER BY event_edits.origin_server_ts DESC, events.write_seq DESC
        LIMIT 1
        """,  # noqa: S608
        parameters,
    )
    row = await cursor.fetchone()
    await cursor.close()
    if row is None:
        return None
    return CachedEventRow(
        event=json.loads(row[0]),
        cached_at=None if row[1] is None else float(row[1]),
    )


async def load_mxc_text(
    db: aiosqlite.Connection,
    *,
    mxc_url: str,
) -> str | None:
    """Return one durably cached MXC text payload when present."""
    cursor = await db.execute(
        """
        SELECT text_content
        FROM mxc_text_cache
        WHERE mxc_url = ?
        """,
        (mxc_url,),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return None if row is None else str(row[0])


async def persist_mxc_text(
    db: aiosqlite.Connection,
    *,
    mxc_url: str,
    text: str,
    cached_at: float,
) -> None:
    """Insert or replace one durably cached MXC text payload."""
    await db.execute(
        """
        INSERT OR REPLACE INTO mxc_text_cache(mxc_url, text_content, cached_at)
        VALUES (?, ?, ?)
        """,
        (mxc_url, text, cached_at),
    )


async def persist_lookup_events(
    db: aiosqlite.Connection,
    *,
    room_id: str,
    room_events: list[tuple[str, dict[str, Any]]],
    cached_at: float,
    thread_id: str | None = None,
) -> None:
    """Persist point-lookups and derived indexes for one room-scoped event batch."""
    cacheable_events = await filter_cacheable_events(db, room_id, room_events)
    await write_lookup_index_rows(
        db,
        room_id=room_id,
        serialized_events=serialize_cacheable_events(cacheable_events),
        cached_at=cached_at,
        thread_id=thread_id,
    )


async def load_thread_id_for_event(
    db: aiosqlite.Connection,
    *,
    room_id: str,
    event_id: str,
) -> str | None:
    """Return the cached thread ID for one event."""
    cursor = await db.execute(
        """
        SELECT thread_id
        FROM event_threads
        WHERE room_id = ? AND event_id = ?
        """,
        (room_id, event_id),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return None if row is None else str(row[0])


async def redact_event_locked(
    db: aiosqlite.Connection,
    *,
    room_id: str,
    event_id: str,
) -> bool:
    """Delete one cached event after a redaction within an existing transaction."""
    dependent_edit_ids = await _dependent_edit_event_ids(db, room_id, original_event_id=event_id)
    removed_event_ids = redaction_removal_event_ids(event_id, dependent_edit_ids)
    affected_thread_ids = await _thread_ids_for_event_ids(db, room_id=room_id, event_ids=removed_event_ids)
    deleted_thread_rows = await _delete_room_thread_events(db, room_id, event_ids=removed_event_ids)
    deleted_event_rows = await delete_cached_events(db, event_ids=removed_event_ids)
    deleted_edit_rows = await delete_event_edit_rows(
        db,
        room_id,
        event_ids=removed_event_ids,
        original_event_id=event_id,
    )
    deleted_thread_index_rows = await delete_event_thread_rows(
        db,
        room_id,
        event_ids=removed_event_ids,
        affected_thread_ids=affected_thread_ids,
    )
    await _record_redacted_events(
        db,
        room_id,
        event_ids=removed_event_ids,
    )
    return cache_rows_were_deleted(
        deleted_thread_rows,
        deleted_event_rows,
        deleted_edit_rows,
        deleted_thread_index_rows,
    )


async def event_or_original_is_redacted(
    db: aiosqlite.Connection,
    room_id: str,
    *,
    event_id: str,
    event: dict[str, Any],
) -> bool:
    """Return whether this event or its edited original was durably redacted."""
    return bool(
        await _redacted_event_ids_for_candidates(
            db,
            room_id,
            event_ids=event_redaction_candidate_ids(event_id, event),
        ),
    )


async def filter_cacheable_events(
    db: aiosqlite.Connection,
    room_id: str,
    room_events: list[tuple[str, dict[str, Any]]],
) -> list[tuple[str, dict[str, Any]]]:
    """Drop events that target durable redaction tombstones before persisting them."""
    redacted_event_ids = await _redacted_event_ids_for_candidates(
        db,
        room_id,
        event_ids=batch_redaction_candidate_ids(room_events),
    )
    return filter_redacted_events(room_events, redacted_event_ids=redacted_event_ids)


async def write_lookup_index_rows(
    db: aiosqlite.Connection,
    *,
    room_id: str,
    serialized_events: list[SerializedCachedEvent],
    cached_at: float,
    thread_id: str | None = None,
) -> None:
    """Persist point-lookup, edit-index, and thread-index rows for cached events.

    Point payload quality is monotonic per event ID: clear content may replace a stored opaque
    ``m.room.encrypted`` payload, but an opaque payload never replaces stored clear content, and
    edit and thread index rows are derived only from payloads the upsert accepted.
    """
    if not serialized_events:
        return
    write_sequences = await allocate_write_sequences(db, len(serialized_events))
    accepted_events: list[SerializedCachedEvent] = []
    for event, write_sequence in zip(serialized_events, write_sequences, strict=True):
        cursor = await db.execute(
            """
            INSERT INTO events(event_id, room_id, origin_server_ts, event_json, cached_at, write_seq)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET
                room_id = excluded.room_id,
                origin_server_ts = excluded.origin_server_ts,
                event_json = excluded.event_json,
                cached_at = excluded.cached_at,
                write_seq = excluded.write_seq
            WHERE json_extract(events.event_json, '$.type') = 'm.room.encrypted'
                OR json_extract(excluded.event_json, '$.type') <> 'm.room.encrypted'
            RETURNING event_id
            """,
            (
                event.event_id,
                room_id,
                event.origin_server_ts,
                event.event_json,
                cached_at,
                write_sequence,
            ),
        )
        accepted = await cursor.fetchone() is not None
        await cursor.close()
        if accepted:
            accepted_events.append(event)
    edit_rows = event_edit_rows(room_id, accepted_events)
    if edit_rows:
        await db.executemany(
            """
            INSERT OR REPLACE INTO event_edits(edit_event_id, room_id, original_event_id, origin_server_ts)
            VALUES (?, ?, ?, ?)
            """,
            [(row.edit_event_id, row.room_id, row.original_event_id, row.origin_server_ts) for row in edit_rows],
        )
    thread_rows = event_thread_rows(room_id, accepted_events, thread_id=thread_id)
    if thread_rows:
        await db.executemany(
            """
            INSERT OR REPLACE INTO event_threads(room_id, event_id, thread_id)
            VALUES (?, ?, ?)
            """,
            [(row.room_id, row.event_id, row.thread_id) for row in thread_rows],
        )


async def allocate_write_sequences(
    db: aiosqlite.Connection,
    count: int,
) -> list[int]:
    """Reserve a durable monotonic SQLite write-sequence range."""
    if count <= 0:
        return []
    cursor = await db.execute(
        """
        INSERT INTO cache_metadata(key, value)
        VALUES ('write_sequence', ?)
        ON CONFLICT(key) DO UPDATE SET
            value = CAST(CAST(cache_metadata.value AS INTEGER) + CAST(excluded.value AS INTEGER) AS TEXT)
        RETURNING CAST(value AS INTEGER)
        """,
        (str(count),),
    )
    row = await cursor.fetchone()
    await cursor.close()
    if row is None:
        msg = "SQLite event cache write sequence was not allocated"
        raise RuntimeError(msg)
    last_sequence = int(row[0])
    return list(range(last_sequence - count + 1, last_sequence + 1))


async def _dependent_edit_event_ids(
    db: aiosqlite.Connection,
    room_id: str,
    *,
    original_event_id: str,
) -> list[str]:
    """Return cached edit event IDs that target one original event."""
    cursor = await db.execute(
        """
        SELECT edit_event_id
        FROM event_edits
        WHERE room_id = ? AND original_event_id = ?
        """,
        (room_id, original_event_id),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return [str(row[0]) for row in rows]


async def delete_cached_events(
    db: aiosqlite.Connection,
    *,
    event_ids: list[str],
) -> int:
    """Delete point-lookup cache rows for the provided event IDs."""
    if not event_ids:
        return 0
    cursor = await db.executemany(
        """
        DELETE FROM events
        WHERE event_id = ?
        """,
        [(event_id,) for event_id in event_ids],
    )
    return 0 if cursor.rowcount is None else int(cursor.rowcount)


async def delete_event_thread_rows(
    db: aiosqlite.Connection,
    room_id: str,
    *,
    event_ids: list[str],
    affected_thread_ids: list[str],
) -> int:
    """Delete event mappings and unsupported roots whose proof was removed."""
    if not event_ids:
        return 0
    cursor = await db.executemany(
        """
        DELETE FROM event_threads
        WHERE room_id = ? AND event_id = ?
        """,
        [(room_id, event_id) for event_id in event_ids],
    )
    deleted_rows = 0 if cursor.rowcount is None else int(cursor.rowcount)
    if not affected_thread_ids:
        return deleted_rows
    cursor = await db.executemany(
        f"""
        DELETE FROM event_threads
        WHERE room_id = ?
            AND event_id = ?
            AND thread_id = ?
            AND {_ORPHAN_THREAD_INDEX_PREDICATE}
        """,  # noqa: S608
        [(room_id, thread_id, thread_id) for thread_id in set(affected_thread_ids)],
    )
    return deleted_rows + (0 if cursor.rowcount is None else int(cursor.rowcount))


async def _thread_ids_for_event_ids(
    db: aiosqlite.Connection,
    *,
    room_id: str,
    event_ids: list[str],
) -> list[str]:
    """Return roots whose supporting rows will be removed."""
    encoded_event_ids = json.dumps(event_ids)
    cursor = await db.execute(
        """
        SELECT thread_id
        FROM event_threads
        WHERE room_id = ? AND event_id IN (SELECT value FROM json_each(?))
        """,
        (room_id, encoded_event_ids),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return [str(row[0]) for row in rows]


async def orphan_thread_index_count(db: aiosqlite.Connection) -> int:
    """Count unsupported event-to-thread rows."""
    cursor = await db.execute(
        f"SELECT COUNT(*) FROM event_threads WHERE {_ORPHAN_THREAD_INDEX_PREDICATE}",  # noqa: S608
    )
    row = await cursor.fetchone()
    await cursor.close()
    return 0 if row is None else int(row[0])


async def repair_orphan_thread_indexes(
    db: aiosqlite.Connection,
) -> int:
    """Remove every unsupported thread mapping during startup maintenance."""
    cursor = await db.execute(
        f"""
        DELETE FROM event_threads
        WHERE {_ORPHAN_THREAD_INDEX_PREDICATE}
        """,  # noqa: S608
    )
    repaired = 0 if cursor.rowcount is None else int(cursor.rowcount)
    await cursor.close()
    return repaired


async def delete_event_edit_rows(
    db: aiosqlite.Connection,
    room_id: str,
    *,
    event_ids: list[str],
    original_event_id: str | None,
) -> int:
    """Delete derived edit-index rows affected by one event redaction."""
    deleted_rows = 0
    for event_id in event_ids:
        cursor = await db.execute(
            """
            DELETE FROM event_edits
            WHERE room_id = ? AND edit_event_id = ?
            """,
            (room_id, event_id),
        )
        deleted_rows += 0 if cursor.rowcount is None else int(cursor.rowcount)
        await cursor.close()
    if original_event_id is not None:
        cursor = await db.execute(
            """
            DELETE FROM event_edits
            WHERE room_id = ? AND original_event_id = ?
            """,
            (room_id, original_event_id),
        )
        deleted_rows += 0 if cursor.rowcount is None else int(cursor.rowcount)
        await cursor.close()
    return deleted_rows


async def _delete_room_thread_events(
    db: aiosqlite.Connection,
    room_id: str,
    *,
    event_ids: list[str],
) -> int:
    """Delete cached thread rows for the provided event IDs within one room."""
    if not event_ids:
        return 0
    cursor = await db.executemany(
        """
        DELETE FROM thread_events
        WHERE room_id = ? AND event_id = ?
        """,
        [(room_id, event_id) for event_id in event_ids],
    )
    return 0 if cursor.rowcount is None else int(cursor.rowcount)


async def _record_redacted_events(
    db: aiosqlite.Connection,
    room_id: str,
    *,
    event_ids: list[str],
) -> None:
    """Persist durable tombstones for redacted event IDs."""
    if not event_ids:
        return
    await db.executemany(
        """
        INSERT OR REPLACE INTO redacted_events(room_id, event_id)
        VALUES (?, ?)
        """,
        [(room_id, event_id) for event_id in event_ids],
    )


async def _redacted_event_ids_for_candidates(
    db: aiosqlite.Connection,
    room_id: str,
    *,
    event_ids: frozenset[str],
) -> frozenset[str]:
    """Return the subset of candidate event IDs that are durably tombstoned."""
    if not event_ids:
        return frozenset()
    placeholders = ",".join("?" for _ in event_ids)
    query = f"""
        SELECT event_id
        FROM redacted_events
        WHERE room_id = ? AND event_id IN ({placeholders})
        """  # noqa: S608
    cursor = await db.execute(
        query,
        (room_id, *sorted(event_ids)),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return frozenset(str(row[0]) for row in rows)
