"""PostgreSQL event lookup, index, and redaction storage for the Matrix event cache."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, LiteralString

from mindroom.matrix.event_info import EventInfo

if TYPE_CHECKING:
    from psycopg import AsyncConnection


_EDITABLE_EVENT_TYPES = frozenset({"m.room.message", "io.mindroom.tool_approval"})


@dataclass(frozen=True, slots=True)
class SerializedCachedEvent:
    """One normalized cached event plus its serialized storage row."""

    event_id: str
    origin_server_ts: int
    event_json: str
    event: dict[str, Any]


@dataclass(frozen=True, slots=True)
class CachedEventRow:
    """One cached event payload plus the time its visible row was written."""

    event: dict[str, Any]
    cached_at: float | None


async def _fetchone(
    db: AsyncConnection,
    query: LiteralString,
    params: tuple[object, ...],
) -> tuple[Any, ...] | None:
    cursor = await db.execute(query, params)
    try:
        return await cursor.fetchone()
    finally:
        await cursor.close()


async def _fetchall(
    db: AsyncConnection,
    query: LiteralString,
    params: tuple[object, ...],
) -> list[tuple[Any, ...]]:
    cursor = await db.execute(query, params)
    try:
        rows = await cursor.fetchall()
        return [tuple(row) for row in rows]
    finally:
        await cursor.close()


async def _rowcount(
    db: AsyncConnection,
    query: LiteralString,
    params: tuple[object, ...],
) -> int:
    cursor = await db.execute(query, params)
    try:
        return 0 if cursor.rowcount is None else int(cursor.rowcount)
    finally:
        await cursor.close()


def event_id_for_cache(event: dict[str, Any]) -> str:
    """Return the required event ID from one normalized cached event."""
    event_id = event.get("event_id")
    if isinstance(event_id, str) and event_id:
        return event_id
    msg = "Cached Matrix event is missing event_id"
    raise ValueError(msg)


def event_timestamp_for_cache(event: dict[str, Any]) -> int:
    """Return the required origin-server timestamp from one normalized cached event."""
    timestamp = event.get("origin_server_ts")
    if isinstance(timestamp, int) and not isinstance(timestamp, bool):
        return timestamp
    msg = f"Cached Matrix event {event_id_for_cache(event)} is missing origin_server_ts"
    raise ValueError(msg)


def serialize_cached_event(event_id: str, event: dict[str, Any]) -> SerializedCachedEvent:
    """Serialize one normalized cached event for PostgreSQL writes."""
    return SerializedCachedEvent(
        event_id=event_id,
        origin_server_ts=event_timestamp_for_cache(event),
        event_json=json.dumps(event, separators=(",", ":")),
        event=event,
    )


def serialize_cacheable_events(
    cacheable_events: list[tuple[str, dict[str, Any]]],
) -> list[SerializedCachedEvent]:
    """Serialize one batch of normalized cacheable events."""
    return [serialize_cached_event(event_id, event) for event_id, event in cacheable_events]


async def load_event(
    db: AsyncConnection,
    *,
    namespace: str,
    event_id: str,
) -> dict[str, Any] | None:
    """Return one cached event payload by event ID."""
    row = await _fetchone(
        db,
        """
        SELECT event_json
        FROM mindroom_event_cache_events
        WHERE namespace = %s AND event_id = %s
        """,
        (namespace, event_id),
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
    rows = await _fetchall(
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
) -> dict[str, Any] | None:
    """Return the latest cached edit event for one original event."""
    row = await _fetchone(
        db,
        """
        SELECT mindroom_event_cache_events.event_json
        FROM mindroom_event_cache_event_edits
        JOIN mindroom_event_cache_events
            ON mindroom_event_cache_events.namespace = mindroom_event_cache_event_edits.namespace
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


async def load_latest_edit_row(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str,
    original_event_id: str,
) -> CachedEventRow | None:
    """Return the latest cached edit event plus its lookup-row write time."""
    row = await _fetchone(
        db,
        """
        SELECT mindroom_event_cache_events.event_json, mindroom_event_cache_events.cached_at
        FROM mindroom_event_cache_event_edits
        JOIN mindroom_event_cache_events
            ON mindroom_event_cache_events.namespace = mindroom_event_cache_event_edits.namespace
            AND mindroom_event_cache_events.event_id = mindroom_event_cache_event_edits.edit_event_id
        WHERE mindroom_event_cache_event_edits.namespace = %s
            AND mindroom_event_cache_event_edits.room_id = %s
            AND mindroom_event_cache_event_edits.original_event_id = %s
        ORDER BY mindroom_event_cache_event_edits.origin_server_ts DESC, mindroom_event_cache_events.write_seq DESC
        LIMIT 1
        """,
        (namespace, room_id, original_event_id),
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
    mxc_url: str,
) -> str | None:
    """Return one durably cached MXC text payload when present."""
    row = await _fetchone(
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


async def load_thread_id_for_event(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str,
    event_id: str,
) -> str | None:
    """Return the cached thread ID for one event."""
    row = await _fetchone(
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
    dependent_edit_ids = await dependent_edit_event_ids(
        db,
        namespace,
        room_id,
        original_event_id=event_id,
    )
    removed_event_ids = list(dict.fromkeys([event_id, *dependent_edit_ids]))
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
    await _record_redacted_events(
        db,
        namespace,
        room_id,
        event_ids=removed_event_ids,
    )
    return deleted_thread_rows > 0 or deleted_event_rows > 0 or deleted_edit_rows > 0 or deleted_thread_index_rows > 0


async def event_or_original_is_redacted(
    db: AsyncConnection,
    namespace: str,
    room_id: str,
    *,
    event_id: str,
    event: dict[str, Any],
) -> bool:
    """Return whether this event or its edited original was durably redacted."""
    event_info = EventInfo.from_event(event)
    candidate_ids = {event_id}
    if event_info.is_edit and isinstance(event_info.original_event_id, str):
        candidate_ids.add(event_info.original_event_id)
    return bool(
        await _redacted_event_ids_for_candidates(
            db,
            namespace,
            room_id,
            event_ids=candidate_ids,
        ),
    )


async def filter_cacheable_events(
    db: AsyncConnection,
    namespace: str,
    room_id: str,
    room_events: list[tuple[str, dict[str, Any]]],
) -> list[tuple[str, dict[str, Any]]]:
    """Drop events that target durable redaction tombstones before persisting them."""
    candidate_ids: set[str] = set()
    for event_id, event_data in room_events:
        candidate_ids.add(event_id)
        event_info = EventInfo.from_event(event_data)
        if event_info.is_edit and isinstance(event_info.original_event_id, str):
            candidate_ids.add(event_info.original_event_id)
    redacted_event_ids = await _redacted_event_ids_for_candidates(
        db,
        namespace,
        room_id,
        event_ids=candidate_ids,
    )
    if not redacted_event_ids:
        return room_events

    cacheable_events: list[tuple[str, dict[str, Any]]] = []
    for event_id, event_data in room_events:
        event_info = EventInfo.from_event(event_data)
        original_event_id = event_info.original_event_id if event_info.is_edit else None
        if event_id in redacted_event_ids:
            continue
        if isinstance(original_event_id, str) and original_event_id in redacted_event_ids:
            continue
        cacheable_events.append((event_id, event_data))
    return cacheable_events


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

    edit_rows = [
        row
        for row in (_edit_cache_row(namespace, room_id, event.event) for event in serialized_events)
        if row is not None
    ]
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
            row,
        )

    thread_rows = (
        [(namespace, room_id, event.event_id, thread_id) for event in serialized_events]
        if thread_id is not None
        else [
            row
            for row in (_event_thread_row(namespace, room_id, event.event) for event in serialized_events)
            if row is not None
        ]
    )
    if thread_rows:
        for row in _with_thread_root_self_rows(thread_rows):
            await db.execute(
                """
                INSERT INTO mindroom_event_cache_event_threads(namespace, room_id, event_id, thread_id)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT(namespace, room_id, event_id) DO UPDATE SET
                    thread_id = excluded.thread_id
                """,
                row,
            )


async def dependent_edit_event_ids(
    db: AsyncConnection,
    namespace: str,
    room_id: str,
    *,
    original_event_id: str,
) -> list[str]:
    """Return cached edit event IDs that target one original event."""
    rows = await _fetchall(
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
    return await _rowcount(
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
    return await _rowcount(
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
        deleted_rows += await _rowcount(
            db,
            """
            DELETE FROM mindroom_event_cache_event_edits
            WHERE namespace = %s AND room_id = %s AND edit_event_id = ANY(%s)
            """,
            (namespace, room_id, event_ids),
        )
    if original_event_id is not None:
        deleted_rows += await _rowcount(
            db,
            """
            DELETE FROM mindroom_event_cache_event_edits
            WHERE namespace = %s AND room_id = %s AND original_event_id = %s
            """,
            (namespace, room_id, original_event_id),
        )
    return deleted_rows


def _event_thread_row(
    namespace: str,
    room_id: str,
    event: dict[str, Any],
) -> tuple[str, str, str, str] | None:
    """Return one durable event-to-thread mapping row when thread membership is explicit."""
    event_id = event.get("event_id")
    if not isinstance(event_id, str) or not event_id:
        return None
    event_info = EventInfo.from_event(event)
    thread_id = event_info.thread_id
    if not isinstance(thread_id, str):
        thread_id = event_info.thread_id_from_edit
    if not isinstance(thread_id, str) or not thread_id:
        return None
    return namespace, room_id, event_id, thread_id


def _with_thread_root_self_rows(
    thread_rows: list[tuple[str, str, str, str]],
) -> list[tuple[str, str, str, str]]:
    """Ensure any learned thread membership also records the root's own lookup row."""
    if not thread_rows:
        return thread_rows
    return list(
        dict.fromkeys(
            [
                *thread_rows,
                *(
                    (namespace, room_id, thread_id, thread_id)
                    for namespace, room_id, _event_id, thread_id in thread_rows
                ),
            ],
        ),
    )


def _edit_cache_row(namespace: str, room_id: str, event: dict[str, Any]) -> tuple[str, str, str, str, int] | None:
    """Return one edit-index row for a cached event when it is an edit."""
    if event.get("type") not in _EDITABLE_EVENT_TYPES:
        return None

    event_info = EventInfo.from_event(event)
    if not event_info.is_edit or not isinstance(event_info.original_event_id, str):
        return None

    return (
        namespace,
        event_id_for_cache(event),
        room_id,
        event_info.original_event_id,
        event_timestamp_for_cache(event),
    )


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
    return await _rowcount(
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
    event_ids: set[str],
) -> frozenset[str]:
    """Return the subset of candidate event IDs that are durably tombstoned."""
    if not event_ids:
        return frozenset()
    rows = await _fetchall(
        db,
        """
        SELECT event_id
        FROM mindroom_event_cache_redacted_events
        WHERE namespace = %s AND room_id = %s AND event_id = ANY(%s)
        """,
        (namespace, room_id, sorted(event_ids)),
    )
    return frozenset(str(row[0]) for row in rows)
