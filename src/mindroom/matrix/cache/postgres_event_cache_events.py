"""PostgreSQL event lookup, index, and redaction storage for the Matrix event cache."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from mindroom.logging_config import get_logger

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
from .postgres_cursor import fetchall, fetchone, rowcount
from .postgres_streaming_compaction import (
    archived_dependent_edit_ids,
    compact_superseded_streaming_edits,
    delete_archived_events,
    load_archived_event,
    load_archived_thread_id,
    load_latest_archived_edit,
)

if TYPE_CHECKING:
    from psycopg import AsyncConnection

logger = get_logger(__name__)


async def load_event(
    db: AsyncConnection,
    *,
    namespace: str,
    event_id: str,
) -> dict[str, Any] | None:
    """Return one cached event payload by event ID."""
    row = await fetchone(
        db,
        """
        SELECT event_json
        FROM mindroom_event_cache_events
        WHERE namespace = %s AND event_id = %s
        """,
        (namespace, event_id),
    )
    if row is not None:
        return json.loads(row[0])
    return await load_archived_event(db, namespace=namespace, event_id=event_id)


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
    active = await _load_latest_active_edit(
        db,
        namespace=namespace,
        room_id=room_id,
        original_event_id=original_event_id,
        sender=sender,
    )
    archived = await load_latest_archived_edit(
        db,
        namespace=namespace,
        room_id=room_id,
        original_event_id=original_event_id,
        sender=sender,
    )
    if active is None:
        return None if archived is None else archived.event_payload()
    if archived is None or active[1:3] >= (archived.origin_server_ts, archived.event_order):
        return active[0]
    return archived.event_payload()


async def load_latest_edit_row(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str,
    original_event_id: str,
    sender: str,
) -> CachedEventRow | None:
    """Return the latest cached edit event plus its lookup-row write time."""
    active = await _load_latest_active_edit(
        db,
        namespace=namespace,
        room_id=room_id,
        original_event_id=original_event_id,
        sender=sender,
    )
    archived = await load_latest_archived_edit(
        db,
        namespace=namespace,
        room_id=room_id,
        original_event_id=original_event_id,
        sender=sender,
    )
    if active is None:
        if archived is None:
            return None
        return CachedEventRow(event=archived.event_payload(), cached_at=archived.cached_at)
    if archived is not None and active[1:3] < (archived.origin_server_ts, archived.event_order):
        return CachedEventRow(event=archived.event_payload(), cached_at=archived.cached_at)
    return CachedEventRow(event=active[0], cached_at=active[3])


async def _load_latest_active_edit(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str,
    original_event_id: str,
    sender: str | None,
) -> tuple[dict[str, Any], int, int, float | None] | None:
    sender_predicate = "" if sender is None else "AND events.event_json::jsonb ->> 'sender' = %s"
    parameters = (namespace, room_id, original_event_id, *((sender,) if sender is not None else ()))
    row = await fetchone(
        db,
        f"""
        SELECT events.event_json, edits.origin_server_ts, events.write_seq, events.cached_at
        FROM mindroom_event_cache_event_edits AS edits
        JOIN mindroom_event_cache_events AS events
            ON events.namespace = edits.namespace
            AND events.event_id = edits.edit_event_id
        WHERE edits.namespace = %s
            AND edits.room_id = %s
            AND edits.original_event_id = %s
            {sender_predicate}
        ORDER BY edits.origin_server_ts DESC, events.write_seq DESC
        LIMIT 1
        """,  # noqa: S608
        parameters,
    )
    if row is None:
        return None
    return (
        json.loads(row[0]),
        int(row[1]),
        int(row[2]),
        None if row[3] is None else float(row[3]),
    )


async def load_mxc_text(
    db: AsyncConnection,
    *,
    namespace: str,
    mxc_url: str,
) -> str | None:
    """Return one durably cached MXC text payload when present."""
    row = await fetchone(
        db,
        """
        SELECT text_content
        FROM mindroom_event_cache_mxc_text
        WHERE namespace = %s AND mxc_url = %s
        """,
        (namespace, mxc_url),
    )
    return None if row is None else str(row[0])


async def persist_mxc_text(
    db: AsyncConnection,
    *,
    namespace: str,
    mxc_url: str,
    text: str,
    cached_at: float,
) -> None:
    """Insert or replace one durably cached MXC text payload."""
    await db.execute(
        """
        INSERT INTO mindroom_event_cache_mxc_text(namespace, mxc_url, text_content, cached_at)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT(namespace, mxc_url) DO UPDATE SET
            text_content = excluded.text_content,
            cached_at = excluded.cached_at
        """,
        (namespace, mxc_url, text, cached_at),
    )


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
    compacted = await compact_superseded_streaming_edits(
        db,
        namespace=namespace,
        room_id=room_id,
    )
    if compacted:
        logger.info(
            "Compacted superseded Matrix streaming edits",
            backend="postgres",
            namespace=namespace,
            room_id=room_id,
            compacted_event_count=compacted,
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
    if row is not None:
        return str(row[0])
    return await load_archived_thread_id(
        db,
        namespace=namespace,
        room_id=room_id,
        event_id=event_id,
    )


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
    archived_edit_ids = await archived_dependent_edit_ids(
        db,
        namespace=namespace,
        room_id=room_id,
        original_event_id=event_id,
    )
    removed_event_ids = redaction_removal_event_ids(event_id, [*dependent_edit_ids, *archived_edit_ids])
    deleted_thread_rows = await _delete_room_thread_events(
        db,
        namespace,
        room_id,
        event_ids=removed_event_ids,
    )
    deleted_event_rows = await delete_cached_events(db, namespace=namespace, event_ids=removed_event_ids)
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
    deleted_archived_rows = await delete_archived_events(
        db,
        namespace=namespace,
        room_id=room_id,
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
        deleted_archived_rows,
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
    await delete_archived_events(
        db,
        namespace=namespace,
        room_id=room_id,
        event_ids=[event.event_id for event in serialized_events],
    )
    for event in serialized_events:
        await db.execute(
            """
            INSERT INTO mindroom_event_cache_events(namespace, event_id, room_id, origin_server_ts, event_json, cached_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT(namespace, event_id) DO UPDATE SET
                room_id = excluded.room_id,
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

    edit_rows = event_edit_rows(room_id, serialized_events)
    for row in edit_rows:
        await db.execute(
            """
            INSERT INTO mindroom_event_cache_event_edits(namespace, edit_event_id, room_id, original_event_id, origin_server_ts)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT(namespace, edit_event_id) DO UPDATE SET
                room_id = excluded.room_id,
                original_event_id = excluded.original_event_id,
                origin_server_ts = excluded.origin_server_ts
            """,
            (namespace, row.edit_event_id, row.room_id, row.original_event_id, row.origin_server_ts),
        )

    thread_rows = event_thread_rows(room_id, serialized_events, thread_id=thread_id)
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
    event_ids: list[str],
) -> int:
    """Delete point-lookup cache rows for the provided event IDs."""
    if not event_ids:
        return 0
    return await rowcount(
        db,
        """
        DELETE FROM mindroom_event_cache_events
        WHERE namespace = %s AND event_id = ANY(%s)
        """,
        (namespace, event_ids),
    )


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
