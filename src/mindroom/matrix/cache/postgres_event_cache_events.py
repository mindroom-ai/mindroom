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
    from collections.abc import Callable, Collection

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
    row = await _load_latest_edit_row(
        db,
        namespace=namespace,
        room_id=room_id,
        original_event_id=original_event_id,
        sender=sender,
    )
    return None if row is None else row.event


async def load_latest_edit_row(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str,
    original_event_id: str,
    sender: str,
) -> CachedEventRow | None:
    """Return the latest cached edit event plus its lookup-row write time."""
    return await _load_latest_edit_row(
        db,
        namespace=namespace,
        room_id=room_id,
        original_event_id=original_event_id,
        sender=sender,
    )


async def _load_latest_edit_row(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str,
    original_event_id: str,
    sender: str | None,
) -> CachedEventRow | None:
    sender_predicate = "" if sender is None else "AND events.sender = %s"
    parameters = (namespace, room_id, original_event_id, *((sender,) if sender is not None else ()))
    row = await fetchone(
        db,
        f"""
        SELECT events.event_json, events.cached_at
        FROM mindroom_event_cache_event_edits AS edits
        JOIN mindroom_event_cache_events AS events
            ON events.namespace = edits.namespace
            AND events.room_id = edits.room_id
            AND events.event_id = edits.edit_event_id
        WHERE edits.namespace = %s
            AND edits.room_id = %s
            AND edits.original_event_id = %s
            {sender_predicate}
        ORDER BY edits.origin_server_ts DESC, edits.edit_event_id COLLATE "C" DESC
        LIMIT 1
        """,  # noqa: S608
        parameters,
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
        SELECT reference.event_id, plaintext.mxc_url, plaintext.text_content
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
    return {(str(event_id), str(mxc_url)): str(text_content) for event_id, mxc_url, text_content in rows}


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


def last_row_per_key[RowT](rows: list[RowT], key: Callable[[RowT], str]) -> list[RowT]:
    """Return one row per key, each at the position of its last occurrence.

    ``ON CONFLICT DO UPDATE`` refuses to touch the same row twice in one statement, so batched
    upserts must first collapse repeated keys the way the row-at-a-time loop resolved them.

    Position matters as much as the value. A row that carries ``nextval`` is ordered by the
    sequence it draws, so the collapsed list has to arrive in the order the sequential loop last
    wrote each key -- for ``A, B, A-last`` the loop left ``A`` newer than ``B``. Assigning the key
    its first position instead, which is what a plain dict insert does, would rank ``A`` before
    ``B`` and diverge from SQLite whenever the timestamps tie.
    """
    latest: dict[str, RowT] = {}
    for row in rows:
        row_key = key(row)
        latest.pop(row_key, None)
        latest[row_key] = row
    return list(latest.values())


async def _reconcile_thread_root_self_rows(
    db: AsyncConnection,
    namespace: str,
    room_id: str,
    *,
    candidate_root_ids: set[str],
    current_self_root_ids: set[str],
) -> None:
    """Keep root self-mappings exactly while a current row still proves them.

    Each root's decision reads only rows whose ``thread_id`` is that root and writes only that
    root's self row, so the roots never observe one another and resolve as one set.
    """
    if not candidate_root_ids:
        return
    # ``EXISTS`` rather than ``SELECT DISTINCT thread_id``: the semi-join stops at each root's first
    # surviving child, the way the row-at-a-time ``LIMIT 1`` probe did, while ``DISTINCT`` has to
    # read every row of every candidate thread before it can dedupe. This runs on every write while
    # the principal lock is held, and long agent threads are exactly where it runs.
    proven_rows = await fetchall(
        db,
        """
        SELECT candidate.root_id
        FROM unnest(%s::text[]) AS candidate(root_id)
        WHERE EXISTS (
            SELECT 1
            FROM mindroom_event_cache_event_threads AS child
            WHERE child.namespace = %s
                AND child.room_id = %s
                AND child.thread_id = candidate.root_id
                AND child.event_id <> candidate.root_id
        )
        """,
        (sorted(candidate_root_ids), namespace, room_id),
    )
    proven_root_ids = {str(row[0]) for row in proven_rows} | (candidate_root_ids & current_self_root_ids)
    if proven_root_ids:
        await db.execute(
            """
            INSERT INTO mindroom_event_cache_event_threads(namespace, room_id, event_id, thread_id)
            SELECT %s::text, %s::text, incoming.root_id, incoming.root_id
            FROM unnest(%s::text[]) AS incoming(root_id)
            ON CONFLICT(namespace, room_id, event_id) DO NOTHING
            """,
            (namespace, room_id, sorted(proven_root_ids)),
        )
    unproven_root_ids = sorted(candidate_root_ids - proven_root_ids)
    if unproven_root_ids:
        # Match on ``event_id`` so the primary key stays usable. ``thread_id = ANY(...)`` with a
        # bare ``event_id = thread_id`` is a column-to-column comparison the planner cannot turn
        # into an index qual, and it degrades to a sequential scan of the whole table.
        await db.execute(
            """
            DELETE FROM mindroom_event_cache_event_threads
            WHERE namespace = %s AND room_id = %s AND event_id = ANY(%s) AND thread_id = event_id
            """,
            (namespace, room_id, unproven_root_ids),
        )


_POINT_LOOKUP_UPSERT = """
    INSERT INTO mindroom_event_cache_events(
        namespace, event_id, room_id, origin_server_ts, event_json, sender, cached_at
    )
    SELECT
        %s::text,
        incoming.event_id,
        %s::text,
        incoming.origin_server_ts,
        incoming.event_json,
        incoming.sender,
        %s::float8
    FROM unnest(%s::text[], %s::bigint[], %s::text[], %s::text[])
        AS incoming(event_id, origin_server_ts, event_json, sender)
    ON CONFLICT(namespace, room_id, event_id) DO UPDATE SET
        origin_server_ts = excluded.origin_server_ts,
        event_json = excluded.event_json,
        sender = excluded.sender,
        cached_at = excluded.cached_at,
        write_seq = nextval('mindroom_event_cache_write_seq')
    WHERE mindroom_event_cache_events.event_json::jsonb ->> 'type' = 'm.room.encrypted'
        OR excluded.event_json::jsonb ->> 'type' <> 'm.room.encrypted'
    RETURNING event_id
"""


async def _upsert_point_lookup_rows(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str,
    serialized_events: list[SerializedCachedEvent],
    cached_at: float,
) -> list[SerializedCachedEvent]:
    """Persist point payloads and return the accepted events in input order.

    Payload quality is monotonic per event ID, so a repeated ID inside one batch must keep the
    sequential outcome: ``ON CONFLICT DO UPDATE`` cannot touch the same row twice in a single
    statement, and collapsing the duplicates first would change which payload survives. A batch
    holding a repeated ID therefore degrades to one statement per event, which is what the
    row-at-a-time path always did.
    """
    event_ids = [event.event_id for event in serialized_events]
    duplicate_free_groups = (
        [serialized_events] if len(set(event_ids)) == len(event_ids) else [[event] for event in serialized_events]
    )
    accepted_events: list[SerializedCachedEvent] = []
    for group in duplicate_free_groups:
        accepted_rows = await fetchall(
            db,
            _POINT_LOOKUP_UPSERT,
            (
                namespace,
                room_id,
                cached_at,
                [event.event_id for event in group],
                [event.origin_server_ts for event in group],
                [event.event_json for event in group],
                [event.sender for event in group],
            ),
        )
        accepted_event_ids = {str(row[0]) for row in accepted_rows}
        accepted_events.extend(event for event in group if event.event_id in accepted_event_ids)
    return accepted_events


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
    accepted_events = await _upsert_point_lookup_rows(
        db,
        namespace=namespace,
        room_id=room_id,
        serialized_events=serialized_events,
        cached_at=cached_at,
    )

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
    mxc_reference_rows = list(
        dict.fromkeys(
            (event.event_id, mxc_url) for event in accepted_events for mxc_url in event_mxc_urls(event.event)
        ),
    )
    if mxc_reference_rows:
        await db.execute(
            """
            INSERT INTO mindroom_event_cache_event_mxc_references(
                namespace, room_id, event_id, mxc_url
            )
            SELECT %s::text, %s::text, incoming.event_id, incoming.mxc_url
            FROM unnest(%s::text[], %s::text[]) AS incoming(event_id, mxc_url)
            ON CONFLICT(namespace, room_id, event_id, mxc_url) DO NOTHING
            """,
            (
                namespace,
                room_id,
                [event_id for event_id, _mxc_url in mxc_reference_rows],
                [mxc_url for _event_id, mxc_url in mxc_reference_rows],
            ),
        )
    await _delete_orphaned_mxc_text(db, namespace, room_id, mxc_urls=previous_mxc_urls)

    await db.execute(
        """
        DELETE FROM mindroom_event_cache_event_edits
        WHERE namespace = %s AND room_id = %s AND edit_event_id = ANY(%s)
        """,
        (namespace, room_id, accepted_event_ids),
    )
    edit_rows = last_row_per_key(event_edit_rows(room_id, accepted_events), lambda row: row.edit_event_id)
    if edit_rows:
        await db.execute(
            """
            INSERT INTO mindroom_event_cache_event_edits(
                namespace, edit_event_id, room_id, original_event_id, origin_server_ts
            )
            SELECT %s::text, incoming.edit_event_id, %s::text, incoming.original_event_id, incoming.origin_server_ts
            FROM unnest(%s::text[], %s::text[], %s::bigint[])
                AS incoming(edit_event_id, original_event_id, origin_server_ts)
            ON CONFLICT(namespace, room_id, edit_event_id) DO UPDATE SET
                original_event_id = excluded.original_event_id,
                origin_server_ts = excluded.origin_server_ts
            """,
            (
                namespace,
                room_id,
                [row.edit_event_id for row in edit_rows],
                [row.original_event_id for row in edit_rows],
                [row.origin_server_ts for row in edit_rows],
            ),
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
    # A root self-row can repeat an event ID already mapped to another thread, and the
    # row-at-a-time upsert let the last row win. Only the last row per event ID is sent; the
    # untrimmed rows still drive root reconciliation below.
    inserted_thread_rows = last_row_per_key(thread_rows, lambda row: row.event_id)
    if inserted_thread_rows:
        await db.execute(
            """
            INSERT INTO mindroom_event_cache_event_threads(namespace, room_id, event_id, thread_id)
            SELECT %s::text, %s::text, incoming.event_id, incoming.thread_id
            FROM unnest(%s::text[], %s::text[]) AS incoming(event_id, thread_id)
            ON CONFLICT(namespace, room_id, event_id) DO UPDATE SET
                thread_id = excluded.thread_id
            """,
            (
                namespace,
                room_id,
                [row.event_id for row in inserted_thread_rows],
                [row.thread_id for row in inserted_thread_rows],
            ),
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
    if not event_ids:
        return
    await db.execute(
        """
        INSERT INTO mindroom_event_cache_redacted_events(namespace, room_id, event_id)
        SELECT %s::text, %s::text, incoming.event_id
        FROM unnest(%s::text[]) AS incoming(event_id)
        ON CONFLICT(namespace, room_id, event_id) DO NOTHING
        """,
        (namespace, room_id, list(dict.fromkeys(event_ids))),
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
