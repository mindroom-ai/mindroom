"""PostgreSQL event lookup, index, and redaction storage for the Matrix event cache."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mindroom.matrix.replacements import ReplacementValidator, is_valid_replacement, ordered_replacements

from .event_cache_events import (
    CachedEventRow,
    SerializedCachedEvent,
    batch_redaction_candidate_ids,
    cache_rows_were_deleted,
    cached_event_owns_mxc,
    decode_cached_event,
    event_edit_rows,
    event_mxc_urls,
    event_thread_rows,
    filter_redacted_events,
    redaction_removal_event_ids,
    serialize_cacheable_events,
    validated_mxc_text_rows,
)
from .postgres_cursor import fetchall, fetchone, rowcount

if TYPE_CHECKING:
    from collections.abc import Collection

    from psycopg import AsyncConnection

_ROOM_CONTENT_TABLES = (
    "mindroom_event_cache_thread_events",
    "mindroom_event_cache_events",
    "mindroom_event_cache_event_edits",
    "mindroom_event_cache_event_threads",
    "mindroom_event_cache_redacted_events",
    "mindroom_event_cache_event_mxc_references",
    "mindroom_event_cache_mxc_text",
    "mindroom_event_cache_thread_state",
)

_ORPHAN_THREAD_INDEX_PREDICATE = """
    NOT EXISTS (
        SELECT 1
        FROM mindroom_event_cache_events AS events
        WHERE events.namespace = event_threads.namespace
            AND events.event_id = event_threads.event_id
            AND events.room_id = event_threads.room_id
    )
    AND NOT (
        event_threads.event_id = event_threads.thread_id
        AND (
            EXISTS (
                SELECT 1
                FROM mindroom_event_cache_event_threads AS child
                WHERE child.namespace = event_threads.namespace
                    AND child.room_id = event_threads.room_id
                    AND child.thread_id = event_threads.thread_id
                    AND child.event_id != child.thread_id
                    AND EXISTS (
                        SELECT 1
                        FROM mindroom_event_cache_events AS child_event
                        WHERE child_event.namespace = child.namespace
                            AND child_event.event_id = child.event_id
                            AND child_event.room_id = child.room_id
                    )
            )
            OR EXISTS (
                SELECT 1
                FROM mindroom_event_cache_thread_events AS child_membership
                WHERE child_membership.namespace = event_threads.namespace
                    AND child_membership.room_id = event_threads.room_id
                    AND child_membership.thread_id = event_threads.thread_id
                    AND child_membership.event_id != child_membership.thread_id
                    AND EXISTS (
                        SELECT 1
                        FROM mindroom_event_cache_events AS child_event
                        WHERE child_event.namespace = child_membership.namespace
                            AND child_event.event_id = child_membership.event_id
                            AND child_event.room_id = child_membership.room_id
                    )
            )
        )
    )
"""


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
        SELECT event_json, origin_server_ts
        FROM mindroom_event_cache_events
        WHERE namespace = %s AND room_id = %s AND event_id = %s
        """,
        (namespace, room_id, event_id),
    )
    decoded = (
        None
        if row is None
        else decode_cached_event(event_json=row[0], event_id=event_id, origin_server_ts=row[1], room_id=room_id)
    )
    return None if decoded is None else decoded.event


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
    # The limit is applied while streaming a server-side cursor rather than in SQL so a payload
    # that disagrees with its index cannot consume a limit slot and hide a later valid event.
    # `decode_cached_event` stays the only place that states the payload-versus-index rule.
    cursor = db.cursor(name="mindroom_recent_room_events")
    await cursor.execute(
        """
        SELECT event_json, event_id, origin_server_ts
        FROM mindroom_event_cache_events
        WHERE namespace = %s
            AND room_id = %s
            AND origin_server_ts >= %s
            AND event_json::jsonb ->> 'type' = %s
        ORDER BY origin_server_ts DESC, write_seq DESC
        """,
        (namespace, room_id, since_ts_ms, event_type),
    )
    events: list[dict[str, Any]] = []
    try:
        while len(events) < limit and (row := await cursor.fetchone()) is not None:
            decoded = decode_cached_event(
                event_json=row[0],
                event_id=row[1],
                origin_server_ts=row[2],
                room_id=room_id,
            )
            if decoded is not None:
                events.append(decoded.event)
        return events
    finally:
        await cursor.close()


async def load_latest_edit(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str,
    original: dict[str, Any],
    validator: ReplacementValidator,
    excluded_event_ids: Collection[str] = (),
) -> dict[str, Any] | None:
    """Return the Matrix-latest replacement across the cached edit index and bundled metadata."""
    row = await load_latest_edit_row(
        db,
        namespace=namespace,
        room_id=room_id,
        original=original,
        validator=validator,
        excluded_event_ids=excluded_event_ids,
    )
    candidates = () if row is None else (row.event,)
    return next(
        iter(
            ordered_replacements(
                original,
                candidates,
                room_id=room_id,
                validator=validator,
                excluded_event_ids=excluded_event_ids,
            ),
        ),
        None,
    )


async def load_latest_edit_row(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str,
    original: dict[str, Any],
    validator: ReplacementValidator,
    excluded_event_ids: Collection[str] = (),
) -> CachedEventRow | None:
    """Return the latest cached edit event plus its lookup-row write time."""
    original_event_id = original.get("event_id")
    cursor = db.cursor(name="mindroom_latest_edit")
    await cursor.execute(
        """
        SELECT
            events.event_json,
            events.cached_at,
            edits.edit_event_id,
            edits.origin_server_ts
        FROM mindroom_event_cache_event_edits AS edits
        JOIN mindroom_event_cache_events AS events
            ON events.namespace = edits.namespace
            AND events.room_id = edits.room_id
            AND events.event_id = edits.edit_event_id
        WHERE edits.namespace = %s
            AND edits.room_id = %s
            AND edits.original_event_id = %s
        ORDER BY edits.origin_server_ts DESC, edits.edit_event_id COLLATE "C" DESC
        """,
        (namespace, room_id, original_event_id),
    )
    try:
        while (row := await cursor.fetchone()) is not None:
            decoded = decode_cached_event(
                event_json=row[0],
                cached_at=row[1],
                event_id=row[2],
                origin_server_ts=row[3],
                room_id=room_id,
            )
            if decoded is not None and is_valid_replacement(
                original,
                decoded.event,
                room_id=room_id,
                validator=validator,
                excluded_event_ids=excluded_event_ids,
            ):
                return decoded
        return None
    finally:
        await cursor.close()


async def load_mxc_text(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str,
    event_id: str,
    mxc_url: str,
) -> str | None:
    """Return one durably cached MXC text payload when present."""
    texts = await load_mxc_texts(
        db,
        namespace=namespace,
        room_id=room_id,
        references=((event_id, mxc_url),),
    )
    return texts.get((event_id, mxc_url))


async def load_mxc_texts(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str,
    references: Collection[tuple[str, str]],
) -> dict[tuple[str, str], str]:
    """Return plaintext for exact visible references in one database round trip."""
    unique_references = tuple(dict.fromkeys(references))
    if not unique_references:
        return {}
    rows = await fetchall(
        db,
        """
        WITH requested(event_id, mxc_url) AS (
            SELECT *
            FROM unnest(%s::text[], %s::text[])
        )
        SELECT reference.event_id, plaintext.mxc_url, plaintext.text_content,
               events.event_json, events.origin_server_ts
        FROM requested
        JOIN mindroom_event_cache_event_mxc_references AS reference
          ON reference.event_id = requested.event_id
         AND reference.mxc_url = requested.mxc_url
        JOIN mindroom_event_cache_mxc_text AS plaintext
          ON plaintext.namespace = reference.namespace
         AND plaintext.room_id = reference.room_id
         AND plaintext.mxc_url = reference.mxc_url
        JOIN mindroom_event_cache_events AS events
          ON events.namespace = reference.namespace
         AND events.room_id = reference.room_id
         AND events.event_id = reference.event_id
        WHERE plaintext.namespace = %s
          AND plaintext.room_id = %s
        """,
        (
            [event_id for event_id, _mxc_url in unique_references],
            [mxc_url for _event_id, mxc_url in unique_references],
            namespace,
            room_id,
        ),
    )
    return validated_mxc_text_rows(rows, room_id=room_id)


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
        SELECT events.event_json, events.origin_server_ts
        FROM mindroom_event_cache_events AS events
        JOIN mindroom_event_cache_event_mxc_references AS reference
          ON reference.namespace = events.namespace
         AND reference.room_id = events.room_id
         AND reference.event_id = events.event_id
        WHERE events.namespace = %s
          AND events.room_id = %s
          AND events.event_id = %s
          AND reference.mxc_url = %s
        """,
        (namespace, room_id, event_id, mxc_url),
    )
    if owns_plaintext is None or not cached_event_owns_mxc(
        event_json=owns_plaintext[0],
        event_id=event_id,
        origin_server_ts=owns_plaintext[1],
        room_id=room_id,
        mxc_url=mxc_url,
    ):
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
    deleted_thread_rows = await rowcount(
        db,
        """
        DELETE FROM mindroom_event_cache_thread_events
        WHERE namespace = %s AND room_id = %s AND event_id = ANY(%s)
        """,
        (namespace, room_id, removed_event_ids),
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
    await _record_redacted_events(db, namespace, room_id, event_ids=removed_event_ids)
    scrubbed_rows = await rowcount(
        db,
        """UPDATE mindroom_event_cache_events
        SET event_json = (event_json::jsonb #- '{unsigned,m.relations,m.replace}')::text
        WHERE namespace = %s AND room_id = %s AND %s = ANY(ARRAY[
            event_json::jsonb #>> '{unsigned,m.relations,m.replace,event_id}',
            event_json::jsonb #>> '{unsigned,m.relations,m.replace,latest_event,event_id}',
            event_json::jsonb #>> '{unsigned,m.relations,m.replace,event,event_id}'
        ])""",
        (namespace, room_id, event_id),
    )
    return cache_rows_were_deleted(
        deleted_thread_rows,
        deleted_event_rows,
        deleted_edit_rows,
        deleted_thread_index_rows,
        scrubbed_rows,
    )


async def filter_cacheable_events(
    db: AsyncConnection,
    namespace: str,
    room_id: str,
    room_events: list[tuple[str, dict[str, Any]]],
) -> list[tuple[str, dict[str, Any]]]:
    """Drop events that target durable redaction tombstones before persisting them."""
    tombstoned_event_ids = await redacted_event_ids(
        db,
        namespace,
        room_id,
        event_ids=batch_redaction_candidate_ids(room_events, room_id),
    )
    return filter_redacted_events(room_events, room_id=room_id, redacted_event_ids=tombstoned_event_ids)


async def _thread_ids_for_events(
    db: AsyncConnection,
    namespace: str,
    room_id: str,
    *,
    event_ids: list[str],
) -> set[str]:
    """Return thread IDs currently mapped from one event set."""
    rows = await fetchall(
        db,
        """
        SELECT DISTINCT thread_id
        FROM mindroom_event_cache_event_threads
        WHERE namespace = %s AND room_id = %s AND event_id = ANY(%s)
        """,
        (namespace, room_id, event_ids),
    )
    return {str(row[0]) for row in rows}


async def _reconcile_thread_root_self_rows(
    db: AsyncConnection,
    namespace: str,
    room_id: str,
    *,
    candidate_root_ids: set[str],
    current_self_root_ids: set[str],
) -> None:
    """Keep root self-mappings exactly while a current row still proves them."""
    for root_id in candidate_root_ids:
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


async def write_lookup_index_rows(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str,
    serialized_events: list[SerializedCachedEvent],
    cached_at: float,
    thread_id: str | None = None,
) -> None:
    """Persist point-lookup, edit-index, and thread-index rows for cached events.

    Point payload quality is monotonic per event ID: clear content may replace a stored opaque
    ``m.room.encrypted`` payload, but an opaque payload never replaces stored clear content.
    Payload-derived indexes use only accepted payloads; explicit snapshots always record their
    authoritative event-to-thread membership.
    """
    if not serialized_events:
        return
    accepted_events: list[SerializedCachedEvent] = []
    for event in serialized_events:
        accepted_row = await fetchone(
            db,
            """
            INSERT INTO mindroom_event_cache_events(namespace, event_id, room_id, origin_server_ts, event_json, cached_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT(namespace, room_id, event_id) DO UPDATE SET
                origin_server_ts = excluded.origin_server_ts,
                event_json = excluded.event_json,
                cached_at = excluded.cached_at,
                write_seq = nextval('mindroom_event_cache_write_seq')
            WHERE mindroom_event_cache_events.event_json::jsonb ->> 'type' = 'm.room.encrypted'
                OR excluded.event_json::jsonb ->> 'type' <> 'm.room.encrypted'
            RETURNING event_id
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
        if accepted_row is not None:
            accepted_events.append(event)

    accepted_event_ids = [event.event_id for event in accepted_events]
    previous_mxc_urls = await _mxc_urls_for_events(
        db,
        namespace,
        room_id,
        event_ids=accepted_event_ids,
    )
    await db.execute(
        """
        DELETE FROM mindroom_event_cache_event_mxc_references
        WHERE namespace = %s AND room_id = %s AND event_id = ANY(%s)
        """,
        (namespace, room_id, accepted_event_ids),
    )
    for event in accepted_events:
        for mxc_url in event_mxc_urls(event.event, room_id=room_id):
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
        (namespace, room_id, accepted_event_ids),
    )
    edit_rows = event_edit_rows(room_id, accepted_events)
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

    thread_index_events = serialized_events if thread_id is not None else accepted_events
    thread_index_event_ids = [event.event_id for event in thread_index_events]
    previous_thread_ids = await _thread_ids_for_events(
        db,
        namespace,
        room_id,
        event_ids=thread_index_event_ids,
    )
    thread_rows = event_thread_rows(room_id, thread_index_events, thread_id=thread_id)
    await db.execute(
        """
        DELETE FROM mindroom_event_cache_event_threads
        WHERE namespace = %s AND room_id = %s AND event_id = ANY(%s)
        """,
        (namespace, room_id, thread_index_event_ids),
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
    await _reconcile_thread_root_self_rows(
        db,
        namespace,
        room_id,
        candidate_root_ids=previous_thread_ids | {row.thread_id for row in thread_rows},
        current_self_root_ids={row.thread_id for row in thread_rows if row.event_id == row.thread_id},
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
    current_self_root_ids: Collection[str] = (),
) -> int:
    """Delete event mappings while preserving roots proven by the current snapshot."""
    if not event_ids:
        return 0
    affected_thread_ids = await _thread_ids_for_events(
        db,
        namespace,
        room_id,
        event_ids=event_ids,
    )
    deleted_rows = await rowcount(
        db,
        """
        DELETE FROM mindroom_event_cache_event_threads
        WHERE namespace = %s AND room_id = %s AND event_id = ANY(%s)
        """,
        (namespace, room_id, event_ids),
    )
    await _reconcile_thread_root_self_rows(
        db,
        namespace,
        room_id,
        candidate_root_ids=affected_thread_ids,
        current_self_root_ids=set(current_self_root_ids),
    )
    return deleted_rows


async def orphan_thread_index_count(
    db: AsyncConnection,
    *,
    namespace: str,
) -> int:
    """Count unsupported event-to-thread rows."""
    row = await fetchone(
        db,
        f"""
        SELECT COUNT(*)
        FROM mindroom_event_cache_event_threads AS event_threads
        WHERE event_threads.namespace = %s
            AND {_ORPHAN_THREAD_INDEX_PREDICATE}
        """,  # noqa: S608
        (namespace,),
    )
    return 0 if row is None else int(row[0])


async def repair_orphan_thread_indexes(
    db: AsyncConnection,
    *,
    namespace: str,
) -> int:
    """Remove every unsupported thread mapping during startup maintenance."""
    return await rowcount(
        db,
        f"""
        DELETE FROM mindroom_event_cache_event_threads AS event_threads
        WHERE event_threads.namespace = %s
            AND {_ORPHAN_THREAD_INDEX_PREDICATE}
        """,  # noqa: S608
        (namespace,),
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


async def redacted_event_ids(
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
    for table_name in _ROOM_CONTENT_TABLES:
        await db.execute(
            f"DELETE FROM {table_name} WHERE namespace = %s AND room_id = %s",  # noqa: S608
            (namespace, room_id),
        )


async def purge_principal_locked(
    db: AsyncConnection,
    *,
    namespace: str,
) -> None:
    """Delete principal content and invalidate every certified in-flight refill."""
    for table_name in _ROOM_CONTENT_TABLES:
        await db.execute(
            f"DELETE FROM {table_name} WHERE namespace = %s",  # noqa: S608
            (namespace,),
        )
    await db.execute(
        """
        UPDATE mindroom_event_cache_room_state
        SET membership_epoch = membership_epoch + 1
        WHERE namespace = %s
        """,
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
