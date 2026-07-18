"""Event, index, redaction, and plaintext ownership storage for SQLite caches."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from .event_cache_events import (
    CachedEventRow,
    SerializedCachedEvent,
    batch_redaction_candidate_ids,
    cache_rows_were_deleted,
    event_edit_rows,
    event_mxc_urls,
    event_redaction_candidate_ids,
    event_thread_rows,
    filter_redacted_events,
    redaction_removal_event_ids,
    serialize_cacheable_events,
)

if TYPE_CHECKING:
    import aiosqlite


async def load_event(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
    room_id: str,
    event_id: str,
) -> dict[str, Any] | None:
    """Return one event only from its principal and room."""
    cursor = await db.execute(
        """
        SELECT event_json
        FROM events
        WHERE principal_id = ? AND room_id = ? AND event_id = ?
        """,
        (principal_id, room_id, event_id),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return None if row is None else json.loads(row[0])


async def load_recent_room_events(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
    room_id: str,
    event_type: str,
    since_ts_ms: int,
    limit: int,
) -> list[dict[str, Any]]:
    """Return recent events from one principal-owned room."""
    if limit <= 0:
        return []
    cursor = await db.execute(
        """
        SELECT event_json
        FROM events
        WHERE principal_id = ?
            AND room_id = ?
            AND origin_server_ts >= ?
            AND json_extract(event_json, '$.type') = ?
        ORDER BY origin_server_ts DESC, rowid DESC
        LIMIT ?
        """,
        (principal_id, room_id, since_ts_ms, event_type, limit),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return [json.loads(row[0]) for row in rows]


async def load_latest_edit(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
    room_id: str,
    original_event_id: str,
    sender: str | None = None,
) -> dict[str, Any] | None:
    """Return the latest principal- and room-scoped edit."""
    sender_clause = "" if sender is None else "AND json_extract(events.event_json, '$.sender') = ?"
    parameters: tuple[object, ...] = (principal_id, room_id, original_event_id)
    if sender is not None:
        parameters = (*parameters, sender)
    cursor = await db.execute(
        f"""
        SELECT events.event_json
        FROM event_edits
        JOIN events
          ON events.principal_id = event_edits.principal_id
         AND events.room_id = event_edits.room_id
         AND events.event_id = event_edits.edit_event_id
        WHERE event_edits.principal_id = ?
          AND event_edits.room_id = ?
          AND event_edits.original_event_id = ?
          {sender_clause}
        ORDER BY event_edits.origin_server_ts DESC, events.rowid DESC
        LIMIT 1
        """,  # noqa: S608
        parameters,
    )
    row = await cursor.fetchone()
    await cursor.close()
    return None if row is None else json.loads(row[0])


async def load_latest_edit_row(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
    room_id: str,
    original_event_id: str,
    sender: str,
) -> CachedEventRow | None:
    """Return the latest edit and its write time within one ownership scope."""
    cursor = await db.execute(
        """
        SELECT events.event_json, events.cached_at
        FROM event_edits
        JOIN events
          ON events.principal_id = event_edits.principal_id
         AND events.room_id = event_edits.room_id
         AND events.event_id = event_edits.edit_event_id
        WHERE event_edits.principal_id = ?
          AND event_edits.room_id = ?
          AND event_edits.original_event_id = ?
          AND json_extract(events.event_json, '$.sender') = ?
        ORDER BY event_edits.origin_server_ts DESC, events.rowid DESC
        LIMIT 1
        """,
        (principal_id, room_id, original_event_id, sender),
    )
    row = await cursor.fetchone()
    await cursor.close()
    if row is None:
        return None
    return CachedEventRow(event=json.loads(row[0]), cached_at=None if row[1] is None else float(row[1]))


async def load_mxc_text(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
    room_id: str,
    event_id: str,
    mxc_url: str,
) -> str | None:
    """Return plaintext only through a surviving visible event reference."""
    cursor = await db.execute(
        """
        SELECT plaintext.text_content
        FROM mxc_text_cache AS plaintext
        JOIN event_mxc_references AS reference
          ON reference.principal_id = plaintext.principal_id
         AND reference.room_id = plaintext.room_id
         AND reference.mxc_url = plaintext.mxc_url
        JOIN events
          ON events.principal_id = reference.principal_id
         AND events.room_id = reference.room_id
         AND events.event_id = reference.event_id
        WHERE plaintext.principal_id = ?
          AND plaintext.room_id = ?
          AND reference.event_id = ?
          AND plaintext.mxc_url = ?
        """,
        (principal_id, room_id, event_id, mxc_url),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return None if row is None else str(row[0])


async def _event_owns_mxc_text(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
    room_id: str,
    event_id: str,
    mxc_url: str,
) -> bool:
    """Return whether one visible event currently owns the room-scoped MXC."""
    cursor = await db.execute(
        """
        SELECT 1
        FROM events
        JOIN event_mxc_references AS reference
          ON reference.principal_id = events.principal_id
         AND reference.room_id = events.room_id
         AND reference.event_id = events.event_id
        WHERE events.principal_id = ?
          AND events.room_id = ?
          AND events.event_id = ?
          AND reference.mxc_url = ?
          AND NOT EXISTS (
              SELECT 1
              FROM redacted_events
              WHERE principal_id = events.principal_id
                AND room_id = events.room_id
                AND event_id = events.event_id
          )
        """,
        (principal_id, room_id, event_id, mxc_url),
    )
    owns_plaintext = await cursor.fetchone()
    await cursor.close()
    return owns_plaintext is not None


async def persist_mxc_text(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
    room_id: str,
    event_id: str,
    mxc_url: str,
    text: str,
    cached_at: float,
) -> bool:
    """Persist plaintext only if its visible event ownership still exists."""
    if not await _event_owns_mxc_text(
        db,
        principal_id=principal_id,
        room_id=room_id,
        event_id=event_id,
        mxc_url=mxc_url,
    ):
        return False
    await db.execute(
        """
        INSERT INTO mxc_text_cache(principal_id, room_id, mxc_url, text_content, cached_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(principal_id, room_id, mxc_url) DO UPDATE SET
            text_content = excluded.text_content,
            cached_at = excluded.cached_at
        """,
        (principal_id, room_id, mxc_url, text, cached_at),
    )
    return True


async def persist_lookup_events(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
    room_id: str,
    room_events: list[tuple[str, dict[str, Any]]],
    cached_at: float,
    thread_id: str | None = None,
) -> None:
    """Persist lookup rows and all derived ownership indexes."""
    cacheable_events = await filter_cacheable_events(db, principal_id, room_id, room_events)
    await write_lookup_index_rows(
        db,
        principal_id=principal_id,
        room_id=room_id,
        serialized_events=serialize_cacheable_events(cacheable_events),
        cached_at=cached_at,
        thread_id=thread_id,
    )


async def load_thread_id_for_event(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
    room_id: str,
    event_id: str,
) -> str | None:
    """Return one event's thread within its ownership scope."""
    cursor = await db.execute(
        """
        SELECT thread_id
        FROM event_threads
        WHERE principal_id = ? AND room_id = ? AND event_id = ?
        """,
        (principal_id, room_id, event_id),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return None if row is None else str(row[0])


async def redact_event_locked(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
    room_id: str,
    event_id: str,
) -> bool:
    """Atomically tombstone and remove one event and its dependent edits."""
    dependent_edit_ids = await _dependent_edit_event_ids(
        db,
        principal_id,
        room_id,
        original_event_id=event_id,
    )
    removed_event_ids = redaction_removal_event_ids(event_id, dependent_edit_ids)
    deleted_thread_rows = await _delete_room_thread_events(
        db,
        principal_id,
        room_id,
        event_ids=removed_event_ids,
    )
    deleted_event_rows = await delete_cached_events(
        db,
        principal_id=principal_id,
        room_id=room_id,
        event_ids=removed_event_ids,
    )
    deleted_edit_rows = await delete_event_edit_rows(
        db,
        principal_id,
        room_id,
        event_ids=removed_event_ids,
        original_event_id=event_id,
    )
    deleted_thread_index_rows = await delete_event_thread_rows(
        db,
        principal_id,
        room_id,
        event_ids=removed_event_ids,
    )
    await _record_redacted_events(db, principal_id, room_id, event_ids=removed_event_ids)
    return cache_rows_were_deleted(
        deleted_thread_rows,
        deleted_event_rows,
        deleted_edit_rows,
        deleted_thread_index_rows,
    )


async def event_or_original_is_redacted(
    db: aiosqlite.Connection,
    principal_id: str,
    room_id: str,
    *,
    event_id: str,
    event: dict[str, Any],
) -> bool:
    """Return whether this event or its edited original has a tombstone."""
    return bool(
        await _redacted_event_ids_for_candidates(
            db,
            principal_id,
            room_id,
            event_ids=event_redaction_candidate_ids(event_id, event),
        ),
    )


async def filter_cacheable_events(
    db: aiosqlite.Connection,
    principal_id: str,
    room_id: str,
    room_events: list[tuple[str, dict[str, Any]]],
) -> list[tuple[str, dict[str, Any]]]:
    """Drop late events covered by durable ownership-scoped tombstones."""
    redacted_event_ids = await _redacted_event_ids_for_candidates(
        db,
        principal_id,
        room_id,
        event_ids=batch_redaction_candidate_ids(room_events),
    )
    return filter_redacted_events(room_events, redacted_event_ids=redacted_event_ids)


async def _thread_ids_for_events(
    db: aiosqlite.Connection,
    principal_id: str,
    room_id: str,
    *,
    event_ids: list[str],
) -> set[str]:
    """Return thread IDs currently mapped from one event set."""
    thread_ids: set[str] = set()
    for event_id in event_ids:
        cursor = await db.execute(
            """
            SELECT thread_id
            FROM event_threads
            WHERE principal_id = ? AND room_id = ? AND event_id = ?
            """,
            (principal_id, room_id, event_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is not None:
            thread_ids.add(str(row[0]))
    return thread_ids


async def _reconcile_thread_root_self_rows(
    db: aiosqlite.Connection,
    principal_id: str,
    room_id: str,
    *,
    candidate_root_ids: set[str],
    current_self_root_ids: set[str],
) -> None:
    """Keep root self-mappings exactly while a current row still proves them."""
    for root_id in candidate_root_ids:
        cursor = await db.execute(
            """
            SELECT 1
            FROM event_threads
            WHERE principal_id = ? AND room_id = ? AND thread_id = ? AND event_id <> ?
            LIMIT 1
            """,
            (principal_id, room_id, root_id, root_id),
        )
        has_surviving_child = await cursor.fetchone() is not None
        await cursor.close()
        if has_surviving_child or root_id in current_self_root_ids:
            await db.execute(
                """
                INSERT OR IGNORE INTO event_threads(principal_id, room_id, event_id, thread_id)
                VALUES (?, ?, ?, ?)
                """,
                (principal_id, room_id, root_id, root_id),
            )
            continue
        await db.execute(
            """
            DELETE FROM event_threads
            WHERE principal_id = ? AND room_id = ? AND event_id = ? AND thread_id = ?
            """,
            (principal_id, room_id, root_id, root_id),
        )


async def write_lookup_index_rows(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
    room_id: str,
    serialized_events: list[SerializedCachedEvent],
    cached_at: float,
    thread_id: str | None = None,
) -> None:
    """Write visible events and reconcile edit, thread, and MXC references."""
    if not serialized_events:
        return
    event_ids = [event.event_id for event in serialized_events]
    previous_mxc_urls = await _mxc_urls_for_events(
        db,
        principal_id,
        room_id,
        event_ids=event_ids,
    )
    await db.executemany(
        """
        INSERT INTO events(principal_id, event_id, room_id, origin_server_ts, event_json, cached_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(principal_id, room_id, event_id) DO UPDATE SET
            origin_server_ts = excluded.origin_server_ts,
            event_json = excluded.event_json,
            cached_at = excluded.cached_at
        """,
        [
            (principal_id, event.event_id, room_id, event.origin_server_ts, event.event_json, cached_at)
            for event in serialized_events
        ],
    )
    await db.executemany(
        """
        DELETE FROM event_mxc_references
        WHERE principal_id = ? AND room_id = ? AND event_id = ?
        """,
        [(principal_id, room_id, event_id) for event_id in event_ids],
    )
    reference_rows = [
        (principal_id, room_id, event.event_id, mxc_url)
        for event in serialized_events
        for mxc_url in event_mxc_urls(event.event)
    ]
    if reference_rows:
        await db.executemany(
            """
            INSERT OR IGNORE INTO event_mxc_references(principal_id, room_id, event_id, mxc_url)
            VALUES (?, ?, ?, ?)
            """,
            reference_rows,
        )
    await _delete_orphaned_mxc_text(db, principal_id, room_id, mxc_urls=previous_mxc_urls)

    await db.executemany(
        """
        DELETE FROM event_edits
        WHERE principal_id = ? AND room_id = ? AND edit_event_id = ?
        """,
        [(principal_id, room_id, event_id) for event_id in event_ids],
    )
    edit_rows = event_edit_rows(room_id, serialized_events)
    if edit_rows:
        await db.executemany(
            """
            INSERT INTO event_edits(
                principal_id, edit_event_id, room_id, original_event_id, origin_server_ts
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(principal_id, room_id, edit_event_id) DO UPDATE SET
                original_event_id = excluded.original_event_id,
                origin_server_ts = excluded.origin_server_ts
            """,
            [
                (principal_id, row.edit_event_id, row.room_id, row.original_event_id, row.origin_server_ts)
                for row in edit_rows
            ],
        )

    previous_thread_ids = await _thread_ids_for_events(
        db,
        principal_id,
        room_id,
        event_ids=event_ids,
    )
    thread_rows = event_thread_rows(room_id, serialized_events, thread_id=thread_id)
    await db.executemany(
        """
        DELETE FROM event_threads
        WHERE principal_id = ? AND room_id = ? AND event_id = ?
        """,
        [(principal_id, room_id, event_id) for event_id in event_ids],
    )
    if thread_rows:
        await db.executemany(
            """
            INSERT INTO event_threads(principal_id, room_id, event_id, thread_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(principal_id, room_id, event_id) DO UPDATE SET
                thread_id = excluded.thread_id
            """,
            [(principal_id, row.room_id, row.event_id, row.thread_id) for row in thread_rows],
        )
    await _reconcile_thread_root_self_rows(
        db,
        principal_id,
        room_id,
        candidate_root_ids=previous_thread_ids | {row.thread_id for row in thread_rows},
        current_self_root_ids={row.thread_id for row in thread_rows if row.event_id == row.thread_id},
    )


async def delete_cached_events(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
    room_id: str,
    event_ids: list[str],
) -> int:
    """Delete visible rows and atomically prune newly orphaned plaintext."""
    if not event_ids:
        return 0
    mxc_urls = await _mxc_urls_for_events(db, principal_id, room_id, event_ids=event_ids)
    await db.executemany(
        """
        DELETE FROM event_mxc_references
        WHERE principal_id = ? AND room_id = ? AND event_id = ?
        """,
        [(principal_id, room_id, event_id) for event_id in event_ids],
    )
    deleted_rows = 0
    for event_id in event_ids:
        cursor = await db.execute(
            """
            DELETE FROM events
            WHERE principal_id = ? AND room_id = ? AND event_id = ?
            """,
            (principal_id, room_id, event_id),
        )
        deleted_rows += 0 if cursor.rowcount is None else int(cursor.rowcount)
        await cursor.close()
    await _delete_orphaned_mxc_text(db, principal_id, room_id, mxc_urls=mxc_urls)
    return deleted_rows


async def delete_event_thread_rows(
    db: aiosqlite.Connection,
    principal_id: str,
    room_id: str,
    *,
    event_ids: list[str],
) -> int:
    """Delete event-to-thread rows within one owner and room."""
    affected_thread_ids = await _thread_ids_for_events(
        db,
        principal_id,
        room_id,
        event_ids=event_ids,
    )
    deleted_rows = await _delete_scoped_event_rows(
        db,
        "event_threads",
        "event_id",
        principal_id,
        room_id,
        event_ids,
    )
    await _reconcile_thread_root_self_rows(
        db,
        principal_id,
        room_id,
        candidate_root_ids=affected_thread_ids,
        current_self_root_ids=set(),
    )
    return deleted_rows


async def delete_event_edit_rows(
    db: aiosqlite.Connection,
    principal_id: str,
    room_id: str,
    *,
    event_ids: list[str],
    original_event_id: str | None,
) -> int:
    """Delete edit indexes within one owner and room."""
    deleted_rows = await _delete_scoped_event_rows(
        db,
        "event_edits",
        "edit_event_id",
        principal_id,
        room_id,
        event_ids,
    )
    if original_event_id is None:
        return deleted_rows
    cursor = await db.execute(
        """
        DELETE FROM event_edits
        WHERE principal_id = ? AND room_id = ? AND original_event_id = ?
        """,
        (principal_id, room_id, original_event_id),
    )
    deleted_rows += 0 if cursor.rowcount is None else int(cursor.rowcount)
    await cursor.close()
    return deleted_rows


async def purge_room_locked(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
    room_id: str,
) -> None:
    """Remove every row owned by one principal in one departed room."""
    for table_name in (
        "thread_events",
        "events",
        "event_edits",
        "event_threads",
        "redacted_events",
        "event_mxc_references",
        "mxc_text_cache",
        "thread_cache_state",
        "room_cache_state",
    ):
        await db.execute(
            f"DELETE FROM {table_name} WHERE principal_id = ? AND room_id = ?",  # noqa: S608
            (principal_id, room_id),
        )


async def purge_principal_locked(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
) -> None:
    """Delete all cache rows owned by one Matrix principal."""
    for table_name in (
        "thread_events",
        "events",
        "event_edits",
        "event_threads",
        "redacted_events",
        "event_mxc_references",
        "mxc_text_cache",
        "thread_cache_state",
        "room_cache_state",
    ):
        await db.execute(
            f"DELETE FROM {table_name} WHERE principal_id = ?",  # noqa: S608
            (principal_id,),
        )


async def _dependent_edit_event_ids(
    db: aiosqlite.Connection,
    principal_id: str,
    room_id: str,
    *,
    original_event_id: str,
) -> list[str]:
    cursor = await db.execute(
        """
        SELECT edit_event_id
        FROM event_edits
        WHERE principal_id = ? AND room_id = ? AND original_event_id = ?
        """,
        (principal_id, room_id, original_event_id),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return [str(row[0]) for row in rows]


async def _delete_room_thread_events(
    db: aiosqlite.Connection,
    principal_id: str,
    room_id: str,
    *,
    event_ids: list[str],
) -> int:
    return await _delete_scoped_event_rows(
        db,
        "thread_events",
        "event_id",
        principal_id,
        room_id,
        event_ids,
    )


async def _delete_scoped_event_rows(
    db: aiosqlite.Connection,
    table_name: str,
    event_column: str,
    principal_id: str,
    room_id: str,
    event_ids: list[str],
) -> int:
    deleted_rows = 0
    for event_id in event_ids:
        cursor = await db.execute(
            f"""
            DELETE FROM {table_name}
            WHERE principal_id = ? AND room_id = ? AND {event_column} = ?
            """,  # noqa: S608
            (principal_id, room_id, event_id),
        )
        deleted_rows += 0 if cursor.rowcount is None else int(cursor.rowcount)
        await cursor.close()
    return deleted_rows


async def _record_redacted_events(
    db: aiosqlite.Connection,
    principal_id: str,
    room_id: str,
    *,
    event_ids: list[str],
) -> None:
    if not event_ids:
        return
    await db.executemany(
        """
        INSERT OR IGNORE INTO redacted_events(principal_id, room_id, event_id)
        VALUES (?, ?, ?)
        """,
        [(principal_id, room_id, event_id) for event_id in event_ids],
    )


async def _redacted_event_ids_for_candidates(
    db: aiosqlite.Connection,
    principal_id: str,
    room_id: str,
    *,
    event_ids: frozenset[str],
) -> frozenset[str]:
    if not event_ids:
        return frozenset()
    placeholders = ",".join("?" for _ in event_ids)
    cursor = await db.execute(
        f"""
        SELECT event_id
        FROM redacted_events
        WHERE principal_id = ? AND room_id = ? AND event_id IN ({placeholders})
        """,  # noqa: S608
        (principal_id, room_id, *sorted(event_ids)),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return frozenset(str(row[0]) for row in rows)


async def _mxc_urls_for_events(
    db: aiosqlite.Connection,
    principal_id: str,
    room_id: str,
    *,
    event_ids: list[str],
) -> frozenset[str]:
    if not event_ids:
        return frozenset()
    placeholders = ",".join("?" for _ in event_ids)
    cursor = await db.execute(
        f"""
        SELECT DISTINCT mxc_url
        FROM event_mxc_references
        WHERE principal_id = ? AND room_id = ? AND event_id IN ({placeholders})
        """,  # noqa: S608
        (principal_id, room_id, *event_ids),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return frozenset(str(row[0]) for row in rows)


async def _delete_orphaned_mxc_text(
    db: aiosqlite.Connection,
    principal_id: str,
    room_id: str,
    *,
    mxc_urls: frozenset[str],
) -> None:
    for mxc_url in mxc_urls:
        await db.execute(
            """
            DELETE FROM mxc_text_cache
            WHERE principal_id = ?
              AND room_id = ?
              AND mxc_url = ?
              AND NOT EXISTS (
                  SELECT 1
                  FROM event_mxc_references
                  WHERE principal_id = ?
                    AND room_id = ?
                    AND mxc_url = ?
              )
            """,
            (principal_id, room_id, mxc_url, principal_id, room_id, mxc_url),
        )
