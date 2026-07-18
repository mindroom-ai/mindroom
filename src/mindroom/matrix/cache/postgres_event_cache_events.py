"""PostgreSQL event lookup, index, and redaction storage for the Matrix event cache."""

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
from .postgres_cursor import fetchall, fetchone, rowcount

if TYPE_CHECKING:
    from psycopg import AsyncConnection


async def load_event(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str,
    event_id: str,
) -> dict[str, Any] | None:
    """Return one cached event payload by event ID."""
    row = await fetchone(
        db,
        """
        SELECT event_json
        FROM mindroom_event_cache_events
        WHERE namespace = %s AND room_id = %s AND event_id = %s
        """,
        (namespace, room_id, event_id),
    )
    return None if row is None else json.loads(row[0])


async def load_recent_room_events(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str,
    event_type: str,
    since_ts_ms: int,
    limit: int,
) -> list[dict[str, Any]]:
    """Return recent cached room events of one type, newest first."""
    if limit <= 0:
        return []
    rows = await fetchall(
        db,
        """
        SELECT event_json
        FROM mindroom_event_cache_events
        WHERE namespace = %s
            AND room_id = %s
            AND origin_server_ts >= %s
            AND event_json::jsonb ->> 'type' = %s
        ORDER BY origin_server_ts DESC, write_seq DESC
        LIMIT %s
        """,
        (namespace, room_id, since_ts_ms, event_type, limit),
    )
    return [json.loads(row[0]) for row in rows]


async def load_latest_edit(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str,
    original_event_id: str,
    sender: str | None = None,
) -> dict[str, Any] | None:
    """Return the latest cached edit event for one original event."""
    if sender is None:
        row = await fetchone(
            db,
            """
            SELECT mindroom_event_cache_events.event_json
            FROM mindroom_event_cache_event_edits
            JOIN mindroom_event_cache_events
                ON mindroom_event_cache_events.namespace = mindroom_event_cache_event_edits.namespace
                AND mindroom_event_cache_events.room_id = mindroom_event_cache_event_edits.room_id
                AND mindroom_event_cache_events.event_id = mindroom_event_cache_event_edits.edit_event_id
            WHERE mindroom_event_cache_event_edits.namespace = %s
                AND mindroom_event_cache_event_edits.room_id = %s
                AND mindroom_event_cache_event_edits.original_event_id = %s
            ORDER BY mindroom_event_cache_event_edits.origin_server_ts DESC, mindroom_event_cache_events.write_seq DESC
            LIMIT 1
            """,
            (namespace, room_id, original_event_id),
        )
        return None if row is None else json.loads(row[0])

    row = await fetchone(
        db,
        """
        SELECT mindroom_event_cache_events.event_json
        FROM mindroom_event_cache_event_edits
        JOIN mindroom_event_cache_events
            ON mindroom_event_cache_events.namespace = mindroom_event_cache_event_edits.namespace
            AND mindroom_event_cache_events.room_id = mindroom_event_cache_event_edits.room_id
            AND mindroom_event_cache_events.event_id = mindroom_event_cache_event_edits.edit_event_id
        WHERE mindroom_event_cache_event_edits.namespace = %s
            AND mindroom_event_cache_event_edits.room_id = %s
            AND mindroom_event_cache_event_edits.original_event_id = %s
            AND mindroom_event_cache_events.event_json::jsonb ->> 'sender' = %s
        ORDER BY mindroom_event_cache_event_edits.origin_server_ts DESC, mindroom_event_cache_events.write_seq DESC
        LIMIT 1
        """,
        (namespace, room_id, original_event_id, sender),
    )
    return None if row is None else json.loads(row[0])


async def load_latest_edit_row(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str,
    original_event_id: str,
    sender: str,
) -> CachedEventRow | None:
    """Return the latest cached edit event plus its lookup-row write time."""
    row = await fetchone(
        db,
        """
        SELECT mindroom_event_cache_events.event_json, mindroom_event_cache_events.cached_at
        FROM mindroom_event_cache_event_edits
        JOIN mindroom_event_cache_events
            ON mindroom_event_cache_events.namespace = mindroom_event_cache_event_edits.namespace
            AND mindroom_event_cache_events.room_id = mindroom_event_cache_event_edits.room_id
            AND mindroom_event_cache_events.event_id = mindroom_event_cache_event_edits.edit_event_id
        WHERE mindroom_event_cache_event_edits.namespace = %s
            AND mindroom_event_cache_event_edits.room_id = %s
            AND mindroom_event_cache_event_edits.original_event_id = %s
            AND mindroom_event_cache_events.event_json::jsonb ->> 'sender' = %s
        ORDER BY mindroom_event_cache_event_edits.origin_server_ts DESC, mindroom_event_cache_events.write_seq DESC
        LIMIT 1
        """,
        (namespace, room_id, original_event_id, sender),
    )
    if row is None:
        return None
    return CachedEventRow(
        event=json.loads(row[0]),
        cached_at=None if row[1] is None else float(row[1]),
    )


async def load_mxc_text(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str,
    event_id: str,
    mxc_url: str,
) -> str | None:
    """Return one durably cached MXC text payload when present."""
    row = await fetchone(
        db,
        """
        SELECT plaintext.text_content
        FROM mindroom_event_cache_mxc_text AS plaintext
        JOIN mindroom_event_cache_event_mxc_references AS reference
          ON reference.namespace = plaintext.namespace
         AND reference.room_id = plaintext.room_id
         AND reference.mxc_url = plaintext.mxc_url
        JOIN mindroom_event_cache_events AS events
          ON events.namespace = reference.namespace
         AND events.room_id = reference.room_id
         AND events.event_id = reference.event_id
        WHERE plaintext.namespace = %s
          AND plaintext.room_id = %s
          AND reference.event_id = %s
          AND plaintext.mxc_url = %s
        """,
        (namespace, room_id, event_id, mxc_url),
    )
    return None if row is None else str(row[0])


async def persist_mxc_text(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str,
    event_id: str,
    mxc_url: str,
    text: str,
    cached_at: float,
) -> bool:
    """Persist plaintext only while the visible event reference survives."""
    owns_plaintext = await fetchone(
        db,
        """
        SELECT 1
        FROM mindroom_event_cache_events AS events
        JOIN mindroom_event_cache_event_mxc_references AS reference
          ON reference.namespace = events.namespace
         AND reference.room_id = events.room_id
         AND reference.event_id = events.event_id
        WHERE events.namespace = %s
          AND events.room_id = %s
          AND events.event_id = %s
          AND reference.mxc_url = %s
          AND NOT EXISTS (
              SELECT 1
              FROM mindroom_event_cache_redacted_events AS redacted
              WHERE redacted.namespace = events.namespace
                AND redacted.room_id = events.room_id
                AND redacted.event_id = events.event_id
          )
        """,
        (namespace, room_id, event_id, mxc_url),
    )
    if owns_plaintext is None:
        return False
    await db.execute(
        """
        INSERT INTO mindroom_event_cache_mxc_text(namespace, room_id, mxc_url, text_content, cached_at)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT(namespace, room_id, mxc_url) DO UPDATE SET
            text_content = excluded.text_content,
            cached_at = excluded.cached_at
        """,
        (namespace, room_id, mxc_url, text, cached_at),
    )
    return True


async def persist_lookup_events(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str,
    room_events: list[tuple[str, dict[str, Any]]],
    cached_at: float,
    thread_id: str | None = None,
) -> None:
    """Persist point-lookups and derived indexes for one room-scoped event batch."""
    cacheable_events = await filter_cacheable_events(db, namespace, room_id, room_events)
    await write_lookup_index_rows(
        db,
        namespace=namespace,
        room_id=room_id,
        serialized_events=serialize_cacheable_events(cacheable_events),
        cached_at=cached_at,
        thread_id=thread_id,
    )


async def load_thread_id_for_event(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str,
    event_id: str,
) -> str | None:
    """Return the cached thread ID for one event."""
    row = await fetchone(
        db,
        """
        SELECT thread_id
        FROM mindroom_event_cache_event_threads
        WHERE namespace = %s AND room_id = %s AND event_id = %s
        """,
        (namespace, room_id, event_id),
    )
    return None if row is None else str(row[0])


async def redact_event_locked(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str,
    event_id: str,
) -> bool:
    """Delete one cached event after a redaction within an existing transaction."""
    dependent_edit_ids = await _dependent_edit_event_ids(
        db,
        namespace,
        room_id,
        original_event_id=event_id,
    )
    removed_event_ids = redaction_removal_event_ids(event_id, dependent_edit_ids)
    deleted_thread_rows = await _delete_room_thread_events(
        db,
        namespace,
        room_id,
        event_ids=removed_event_ids,
    )
    deleted_event_rows = await delete_cached_events(
        db,
        namespace=namespace,
        room_id=room_id,
        event_ids=removed_event_ids,
    )
    deleted_edit_rows = await delete_event_edit_rows(
        db,
        namespace,
        room_id,
        event_ids=removed_event_ids,
        original_event_id=event_id,
    )
    deleted_thread_index_rows = await delete_event_thread_rows(
        db,
        namespace,
        room_id,
        event_ids=removed_event_ids,
    )
    await _record_redacted_events(
        db,
        namespace,
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
    db: AsyncConnection,
    namespace: str,
    room_id: str,
    *,
    event_id: str,
    event: dict[str, Any],
) -> bool:
    """Return whether this event or its edited original was durably redacted."""
    return bool(
        await _redacted_event_ids_for_candidates(
            db,
            namespace,
            room_id,
            event_ids=event_redaction_candidate_ids(event_id, event),
        ),
    )


async def filter_cacheable_events(
    db: AsyncConnection,
    namespace: str,
    room_id: str,
    room_events: list[tuple[str, dict[str, Any]]],
) -> list[tuple[str, dict[str, Any]]]:
    """Drop events that target durable redaction tombstones before persisting them."""
    redacted_event_ids = await _redacted_event_ids_for_candidates(
        db,
        namespace,
        room_id,
        event_ids=batch_redaction_candidate_ids(room_events),
    )
    return filter_redacted_events(room_events, redacted_event_ids=redacted_event_ids)


async def write_lookup_index_rows(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str,
    serialized_events: list[SerializedCachedEvent],
    cached_at: float,
    thread_id: str | None = None,
) -> None:
    """Persist point-lookup, edit-index, and thread-index rows for cached events."""
    if not serialized_events:
        return
    event_ids = [event.event_id for event in serialized_events]
    previous_mxc_urls = await _mxc_urls_for_events(
        db,
        namespace,
        room_id,
        event_ids=event_ids,
    )
    for event in serialized_events:
        await db.execute(
            """
            INSERT INTO mindroom_event_cache_events(namespace, event_id, room_id, origin_server_ts, event_json, cached_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT(namespace, room_id, event_id) DO UPDATE SET
                origin_server_ts = excluded.origin_server_ts,
                event_json = excluded.event_json,
                cached_at = excluded.cached_at,
                write_seq = nextval('mindroom_event_cache_write_seq')
            """,
            (
                namespace,
                event.event_id,
                room_id,
                event.origin_server_ts,
                event.event_json,
                cached_at,
            ),
        )

    await db.execute(
        """
        DELETE FROM mindroom_event_cache_event_mxc_references
        WHERE namespace = %s AND room_id = %s AND event_id = ANY(%s)
        """,
        (namespace, room_id, event_ids),
    )
    for event in serialized_events:
        for mxc_url in event_mxc_urls(event.event):
            await db.execute(
                """
                INSERT INTO mindroom_event_cache_event_mxc_references(
                    namespace, room_id, event_id, mxc_url
                )
                VALUES (%s, %s, %s, %s)
                ON CONFLICT(namespace, room_id, event_id, mxc_url) DO NOTHING
                """,
                (namespace, room_id, event.event_id, mxc_url),
            )
    await _delete_orphaned_mxc_text(db, namespace, room_id, mxc_urls=previous_mxc_urls)

    await db.execute(
        """
        DELETE FROM mindroom_event_cache_event_edits
        WHERE namespace = %s AND room_id = %s AND edit_event_id = ANY(%s)
        """,
        (namespace, room_id, event_ids),
    )
    edit_rows = event_edit_rows(room_id, serialized_events)
    for row in edit_rows:
        await db.execute(
            """
            INSERT INTO mindroom_event_cache_event_edits(namespace, edit_event_id, room_id, original_event_id, origin_server_ts)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT(namespace, room_id, edit_event_id) DO UPDATE SET
                original_event_id = excluded.original_event_id,
                origin_server_ts = excluded.origin_server_ts
            """,
            (namespace, row.edit_event_id, row.room_id, row.original_event_id, row.origin_server_ts),
        )

    previous_thread_rows = await fetchall(
        db,
        """
        SELECT DISTINCT thread_id
        FROM mindroom_event_cache_event_threads
        WHERE namespace = %s AND room_id = %s AND event_id = ANY(%s)
        """,
        (namespace, room_id, event_ids),
    )
    previous_thread_ids = {str(row[0]) for row in previous_thread_rows}
    thread_rows = event_thread_rows(room_id, serialized_events, thread_id=thread_id)
    await db.execute(
        """
        DELETE FROM mindroom_event_cache_event_threads
        WHERE namespace = %s AND room_id = %s AND event_id = ANY(%s)
        """,
        (namespace, room_id, event_ids),
    )
    if thread_rows:
        for row in thread_rows:
            await db.execute(
                """
                INSERT INTO mindroom_event_cache_event_threads(namespace, room_id, event_id, thread_id)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT(namespace, room_id, event_id) DO UPDATE SET
                    thread_id = excluded.thread_id
                """,
                (namespace, row.room_id, row.event_id, row.thread_id),
            )
    current_self_root_ids = {row.thread_id for row in thread_rows if row.event_id == row.thread_id}
    for root_id in previous_thread_ids | {row.thread_id for row in thread_rows}:
        surviving_child = await fetchone(
            db,
            """
            SELECT 1
            FROM mindroom_event_cache_event_threads
            WHERE namespace = %s AND room_id = %s AND thread_id = %s AND event_id <> %s
            LIMIT 1
            """,
            (namespace, room_id, root_id, root_id),
        )
        if surviving_child is not None or root_id in current_self_root_ids:
            await db.execute(
                """
                INSERT INTO mindroom_event_cache_event_threads(namespace, room_id, event_id, thread_id)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT(namespace, room_id, event_id) DO NOTHING
                """,
                (namespace, room_id, root_id, root_id),
            )
            continue
        await db.execute(
            """
            DELETE FROM mindroom_event_cache_event_threads
            WHERE namespace = %s AND room_id = %s AND event_id = %s AND thread_id = %s
            """,
            (namespace, room_id, root_id, root_id),
        )


async def _dependent_edit_event_ids(
    db: AsyncConnection,
    namespace: str,
    room_id: str,
    *,
    original_event_id: str,
) -> list[str]:
    """Return cached edit event IDs that target one original event."""
    rows = await fetchall(
        db,
        """
        SELECT edit_event_id
        FROM mindroom_event_cache_event_edits
        WHERE namespace = %s AND room_id = %s AND original_event_id = %s
        """,
        (namespace, room_id, original_event_id),
    )
    return [str(row[0]) for row in rows]


async def delete_cached_events(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str,
    event_ids: list[str],
) -> int:
    """Delete point-lookup cache rows for the provided event IDs."""
    if not event_ids:
        return 0
    mxc_urls = await _mxc_urls_for_events(db, namespace, room_id, event_ids=event_ids)
    await db.execute(
        """
        DELETE FROM mindroom_event_cache_event_mxc_references
        WHERE namespace = %s AND room_id = %s AND event_id = ANY(%s)
        """,
        (namespace, room_id, event_ids),
    )
    deleted_rows = await rowcount(
        db,
        """
        DELETE FROM mindroom_event_cache_events
        WHERE namespace = %s AND room_id = %s AND event_id = ANY(%s)
        """,
        (namespace, room_id, event_ids),
    )
    await _delete_orphaned_mxc_text(db, namespace, room_id, mxc_urls=mxc_urls)
    return deleted_rows


async def delete_event_thread_rows(
    db: AsyncConnection,
    namespace: str,
    room_id: str,
    *,
    event_ids: list[str],
) -> int:
    """Delete durable event-to-thread rows for the provided event IDs."""
    if not event_ids:
        return 0
    return await rowcount(
        db,
        """
        DELETE FROM mindroom_event_cache_event_threads
        WHERE namespace = %s AND room_id = %s AND event_id = ANY(%s)
        """,
        (namespace, room_id, event_ids),
    )


async def delete_event_edit_rows(
    db: AsyncConnection,
    namespace: str,
    room_id: str,
    *,
    event_ids: list[str],
    original_event_id: str | None,
) -> int:
    """Delete derived edit-index rows affected by one event redaction."""
    deleted_rows = 0
    if event_ids:
        deleted_rows += await rowcount(
            db,
            """
            DELETE FROM mindroom_event_cache_event_edits
            WHERE namespace = %s AND room_id = %s AND edit_event_id = ANY(%s)
            """,
            (namespace, room_id, event_ids),
        )
    if original_event_id is not None:
        deleted_rows += await rowcount(
            db,
            """
            DELETE FROM mindroom_event_cache_event_edits
            WHERE namespace = %s AND room_id = %s AND original_event_id = %s
            """,
            (namespace, room_id, original_event_id),
        )
    return deleted_rows


async def _delete_room_thread_events(
    db: AsyncConnection,
    namespace: str,
    room_id: str,
    *,
    event_ids: list[str],
) -> int:
    """Delete cached thread rows for the provided event IDs within one room."""
    if not event_ids:
        return 0
    return await rowcount(
        db,
        """
        DELETE FROM mindroom_event_cache_thread_events
        WHERE namespace = %s AND room_id = %s AND event_id = ANY(%s)
        """,
        (namespace, room_id, event_ids),
    )


async def _record_redacted_events(
    db: AsyncConnection,
    namespace: str,
    room_id: str,
    *,
    event_ids: list[str],
) -> None:
    """Persist durable tombstones for redacted event IDs."""
    for event_id in event_ids:
        await db.execute(
            """
            INSERT INTO mindroom_event_cache_redacted_events(namespace, room_id, event_id)
            VALUES (%s, %s, %s)
            ON CONFLICT(namespace, room_id, event_id) DO NOTHING
            """,
            (namespace, room_id, event_id),
        )


async def _redacted_event_ids_for_candidates(
    db: AsyncConnection,
    namespace: str,
    room_id: str,
    *,
    event_ids: frozenset[str],
) -> frozenset[str]:
    """Return the subset of candidate event IDs that are durably tombstoned."""
    if not event_ids:
        return frozenset()
    rows = await fetchall(
        db,
        """
        SELECT event_id
        FROM mindroom_event_cache_redacted_events
        WHERE namespace = %s AND room_id = %s AND event_id = ANY(%s)
        """,
        (namespace, room_id, sorted(event_ids)),
    )
    return frozenset(str(row[0]) for row in rows)


async def purge_room_locked(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str,
) -> None:
    """Delete all cache rows in one departed principal namespace and room."""
    for table_name in (
        "mindroom_event_cache_thread_events",
        "mindroom_event_cache_events",
        "mindroom_event_cache_event_edits",
        "mindroom_event_cache_event_threads",
        "mindroom_event_cache_redacted_events",
        "mindroom_event_cache_event_mxc_references",
        "mindroom_event_cache_mxc_text",
        "mindroom_event_cache_thread_state",
        "mindroom_event_cache_room_state",
    ):
        await db.execute(
            f"DELETE FROM {table_name} WHERE namespace = %s AND room_id = %s",  # noqa: S608
            (namespace, room_id),
        )


async def purge_principal_locked(
    db: AsyncConnection,
    *,
    namespace: str,
) -> None:
    """Delete all cache rows owned by one principal namespace."""
    for table_name in (
        "mindroom_event_cache_thread_events",
        "mindroom_event_cache_events",
        "mindroom_event_cache_event_edits",
        "mindroom_event_cache_event_threads",
        "mindroom_event_cache_redacted_events",
        "mindroom_event_cache_event_mxc_references",
        "mindroom_event_cache_mxc_text",
        "mindroom_event_cache_thread_state",
        "mindroom_event_cache_room_state",
    ):
        await db.execute(
            f"DELETE FROM {table_name} WHERE namespace = %s",  # noqa: S608
            (namespace,),
        )


async def _mxc_urls_for_events(
    db: AsyncConnection,
    namespace: str,
    room_id: str,
    *,
    event_ids: list[str],
) -> frozenset[str]:
    """Return candidate plaintext keys referenced by a visible event set."""
    if not event_ids:
        return frozenset()
    rows = await fetchall(
        db,
        """
        SELECT DISTINCT mxc_url
        FROM mindroom_event_cache_event_mxc_references
        WHERE namespace = %s AND room_id = %s AND event_id = ANY(%s)
        """,
        (namespace, room_id, event_ids),
    )
    return frozenset(str(row[0]) for row in rows)


async def _delete_orphaned_mxc_text(
    db: AsyncConnection,
    namespace: str,
    room_id: str,
    *,
    mxc_urls: frozenset[str],
) -> None:
    """Delete plaintext candidates that no surviving visible event references."""
    if not mxc_urls:
        return
    await db.execute(
        """
        DELETE FROM mindroom_event_cache_mxc_text AS plaintext
        WHERE plaintext.namespace = %s
          AND plaintext.room_id = %s
          AND plaintext.mxc_url = ANY(%s)
          AND NOT EXISTS (
              SELECT 1
              FROM mindroom_event_cache_event_mxc_references AS reference
              WHERE reference.namespace = plaintext.namespace
                AND reference.room_id = plaintext.room_id
                AND reference.mxc_url = plaintext.mxc_url
          )
        """,
        (namespace, room_id, sorted(mxc_urls)),
    )
