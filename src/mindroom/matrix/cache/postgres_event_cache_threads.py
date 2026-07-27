"""PostgreSQL thread snapshot and freshness storage helpers for the Matrix event cache."""

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
from .postgres_cursor import fetchall, fetchone
from .postgres_event_cache_events import (
    delete_cached_events,
    delete_event_edit_rows,
    delete_event_thread_rows,
    event_or_original_is_redacted,
    filter_cacheable_events,
    write_lookup_index_rows,
)
from .thread_cache_state import (
    ThreadAppendOutcome,
    ThreadCacheReplaceOutcome,
    ThreadCacheStateRow,
    append_keeps_thread_valid,
    guarded_thread_replacement_conflict,
    incremental_thread_revalidation_reasons,
    is_incremental_thread_revalidation_reason,
    thread_cache_state_changed_after,
    thread_cache_state_row,
)

if TYPE_CHECKING:
    from psycopg import AsyncConnection

    from .event_cache import ThreadCacheState

# The edits that survive a collapsed read: one per message, from the right sender.
#
# This query mirrors the SQLite one. What it selects and why - the per (original, sender) rule, why
# the sender comparison here is an optimization rather than the security boundary, why the original
# is LEFT joined out of ``events`` alone, why ROW_NUMBER rather than a correlated NOT EXISTS, why
# MATERIALIZED, and what a write-time prune would have to preserve - is recorded once above
# ``_SURVIVING_EDITS_CTE``
# in sqlite_event_cache_threads.py. Only the PostgreSQL-specific notes live here; keep it that way,
# because the two copies had already drifted when this was last deduplicated.
#
# Do not rewrite the ranking as ``DISTINCT ON (original_event_id)``: that shape materialises every
# row of the thread before it can pick winners.
#
# Do not re-derive this query's cost without ANALYZE. On unanalyzed tables an unseen namespace
# estimates 1 row against thousands actual, every join degrades to a nested loop with a join filter,
# and every shape collapses - a plain unfiltered read of the same thread included, by 77x. A
# comparison made in that state measures the planner, not the query.
#
# ``COLLATE "C"`` is load-bearing, not decoration. The tie-break has to agree with SQLite and
# with the fold, and both compare event IDs by byte: SQLite TEXT comparison is always BINARY and
# ``_edit_candidate_is_newer`` compares Python strings by code point. Without the override this
# ORDER BY uses the database's default collation, so on a glibc cluster ('a' < 'B') the read
# ships a different surviving edit than SQLite does for the same two edits sharing a timestamp -
# and the fold applies whichever single edit it is handed, so the message renders differently per
# backend. Matrix v4+ event IDs are mixed-case base64url, the input where the two orders diverge
# most.
#
# It is nearly free, not free. A different collation is a different OID, so this ORDER BY no
# longer matches idx_..._event_edits_room_original_ts and the plan gains an Incremental Sort
# above it - on a C-collation database too, since "C" (950) is not the default OID (100) even
# when datcollate is C. The presorted prefix survives, so the residual sort covers one
# (original, timestamp) group, which is a single row outside the tie this exists to fix.
# Measured end to end on a 540-row 94%-edit thread: 11.8 ms with, 10.7 ms without.
#
# The behavioural divergence is invisible to CI: the fixture pins postgres:15-alpine and musl
# has no real locale support, so every libc collation there behaves like C and a seeded read
# cannot fail whether or not this pin is present. test_edit_ranking_is_scoped_to_this_thread
# _and_this_sender therefore asserts the pin structurally, which is what can actually fail.
_SURVIVING_EDITS_CTE = """
WITH surviving_edits AS MATERIALIZED (
    SELECT edit_event_id
    FROM (
        SELECT event_edits.edit_event_id AS edit_event_id,
               ROW_NUMBER() OVER (
                   PARTITION BY event_edits.original_event_id
                   ORDER BY event_edits.origin_server_ts DESC,
                            event_edits.edit_event_id COLLATE "C" DESC
               ) AS edit_rank
        FROM mindroom_event_cache_event_edits AS event_edits
        JOIN mindroom_event_cache_thread_events AS edit_membership
            ON edit_membership.namespace = event_edits.namespace
            AND edit_membership.room_id = event_edits.room_id
            AND edit_membership.event_id = event_edits.edit_event_id
            AND edit_membership.thread_id = %(thread_id)s
        JOIN mindroom_event_cache_events AS edit_events
            ON edit_events.namespace = event_edits.namespace
            AND edit_events.room_id = event_edits.room_id
            AND edit_events.event_id = event_edits.edit_event_id
        LEFT JOIN mindroom_event_cache_events AS original_events
            ON original_events.namespace = event_edits.namespace
            AND original_events.room_id = event_edits.room_id
            AND original_events.event_id = event_edits.original_event_id
        WHERE event_edits.namespace = %(namespace)s
            AND event_edits.room_id = %(room_id)s
            AND (original_events.event_id IS NULL OR edit_events.sender = original_events.sender)
    ) AS ranked
    WHERE edit_rank = 1
)
"""

# One thread, collapsed: every non-edit row, plus the one surviving edit per edited message.
_THREAD_EVENTS_SQL = (
    _SURVIVING_EDITS_CTE  # noqa: S608 - both operands are literals; params stay bound
    + """
SELECT thread_events.origin_server_ts, thread_events.write_seq, events.event_json
FROM mindroom_event_cache_thread_events AS thread_events
JOIN mindroom_event_cache_events AS events
    ON events.namespace = thread_events.namespace
    AND events.room_id = thread_events.room_id
    AND events.event_id = thread_events.event_id
WHERE thread_events.namespace = %(namespace)s
    AND thread_events.room_id = %(room_id)s
    AND thread_events.thread_id = %(thread_id)s
    AND (
        NOT EXISTS (
            SELECT 1
            FROM mindroom_event_cache_event_edits AS row_is_an_edit
            WHERE row_is_an_edit.namespace = thread_events.namespace
                AND row_is_an_edit.room_id = thread_events.room_id
                AND row_is_an_edit.edit_event_id = thread_events.event_id
        )
        OR thread_events.event_id IN (SELECT edit_event_id FROM surviving_edits)
    )
ORDER BY thread_events.origin_server_ts ASC, thread_events.write_seq ASC
"""
)


async def load_thread_events(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str,
    thread_id: str,
) -> list[dict[str, Any]] | None:
    """Return one thread's cached events oldest first, collapsed to one edit per message."""
    rows = await fetchall(
        db,
        _THREAD_EVENTS_SQL,
        {"namespace": namespace, "room_id": room_id, "thread_id": thread_id},
    )
    return [json.loads(row[2]) for row in rows] if rows else None


async def load_thread_event_ids(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str,
    thread_id: str,
) -> set[str]:
    """Return every raw event ID this thread holds, superseded edits included.

    Repair bookkeeping asks which rows are durably present; the visible read answers what the
    thread looks like. Collapsing made those different questions, and answering the first with the
    second reports every superseded edit as missing. A retained delta for such an edit can then
    never reconcile, so the read invalidates the thread it just served - on the paths where an
    append does not converge and its delta is deliberately kept.

    Joined to ``events`` rather than reading membership alone: a membership row whose payload is
    gone is not durably present, and reporting it as present would suppress a refill that should
    happen. That join is also what the pre-collapse code did implicitly, since it derived these IDs
    from a read that required the payload.
    """
    rows = await fetchall(
        db,
        """
        SELECT thread_events.event_id
        FROM mindroom_event_cache_thread_events AS thread_events
        JOIN mindroom_event_cache_events AS events
            ON events.namespace = thread_events.namespace
            AND events.room_id = thread_events.room_id
            AND events.event_id = thread_events.event_id
        WHERE thread_events.namespace = %s AND thread_events.room_id = %s AND thread_events.thread_id = %s
        """,
        (namespace, room_id, thread_id),
    )
    return {str(row[0]) for row in rows}


async def load_recent_room_thread_ids(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str,
    limit: int,
) -> list[str]:
    """Return thread IDs for one room ordered by the newest locally cached event timestamp."""
    rows = await fetchall(
        db,
        """
        SELECT thread_id
        FROM mindroom_event_cache_thread_events
        WHERE namespace = %s AND room_id = %s
        GROUP BY thread_id
        ORDER BY MAX(origin_server_ts) DESC, thread_id ASC
        LIMIT %s
        """,
        (namespace, room_id, limit),
    )
    return [str(row[0]) for row in rows]


async def _load_thread_cache_state_row(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str,
    thread_id: str,
) -> ThreadCacheStateRow | None:
    """Return one raw thread-cache-state row joined with room invalidation state."""
    row = await fetchone(
        db,
        """
        SELECT
            mindroom_event_cache_thread_state.validated_at,
            mindroom_event_cache_thread_state.invalidated_at,
            mindroom_event_cache_thread_state.invalidation_reason,
            mindroom_event_cache_room_state.invalidated_at,
            mindroom_event_cache_room_state.invalidation_reason
        FROM (SELECT %s AS requested_namespace, %s AS requested_room_id, %s AS requested_thread_id) AS requested
        LEFT JOIN mindroom_event_cache_thread_state
            ON mindroom_event_cache_thread_state.namespace = requested.requested_namespace
            AND mindroom_event_cache_thread_state.room_id = requested.requested_room_id
            AND mindroom_event_cache_thread_state.thread_id = requested.requested_thread_id
        LEFT JOIN mindroom_event_cache_room_state
            ON mindroom_event_cache_room_state.namespace = requested.requested_namespace
            AND mindroom_event_cache_room_state.room_id = requested.requested_room_id
        """,
        (namespace, room_id, thread_id),
    )
    return thread_cache_state_row(row)


async def load_thread_cache_state(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str,
    thread_id: str,
) -> ThreadCacheState | None:
    """Return one thread cache state object joined with room invalidation state."""
    row = await _load_thread_cache_state_row(
        db,
        namespace=namespace,
        room_id=room_id,
        thread_id=thread_id,
    )
    if row is None:
        return None
    return row.as_public_state()


async def load_room_membership_locked(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str,
) -> tuple[str, int]:
    """Return the durable membership state and transition epoch for one principal-room."""
    row = await fetchone(
        db,
        """
        SELECT membership_state, membership_epoch
        FROM mindroom_event_cache_room_state
        WHERE namespace = %s AND room_id = %s
        """,
        (namespace, room_id),
    )
    return ("joined", 0) if row is None else (str(row[0]), int(row[1]))


async def certify_room_membership_locked(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str,
) -> int:
    """Create a durable generation row and return its current epoch."""
    await db.execute(
        """
        INSERT INTO mindroom_event_cache_room_state(
            namespace,
            room_id,
            membership_state,
            membership_epoch
        )
        VALUES (%s, %s, 'joined', 0)
        ON CONFLICT(namespace, room_id) DO NOTHING
        """,
        (namespace, room_id),
    )
    _membership_state, membership_epoch = await load_room_membership_locked(
        db,
        namespace=namespace,
        room_id=room_id,
    )
    return membership_epoch


async def set_room_membership_locked(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str,
    membership_state: Literal["joined", "departed"],
    reason: str,
) -> None:
    """Advance one durable room-membership transition and invalidate prior refills."""
    await mark_room_stale_locked(
        db,
        namespace=namespace,
        room_id=room_id,
        reason=reason,
    )
    await db.execute(
        """
        UPDATE mindroom_event_cache_room_state
        SET membership_state = %s, membership_epoch = membership_epoch + 1
        WHERE namespace = %s AND room_id = %s
        """,
        (membership_state, namespace, room_id),
    )


async def _store_thread_events_locked(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str,
    thread_id: str,
    events: list[dict[str, Any]],
    validated_at: float,
) -> frozenset[str]:
    """Persist one authoritative thread snapshot within an existing DB transaction."""
    normalized_events = [normalize_event_source_for_cache(event) for event in events]
    cacheable_events = await filter_cacheable_events(
        db,
        namespace,
        room_id,
        [(event_id_for_cache(event), event) for event in normalized_events],
    )
    serialized_events = serialize_cacheable_events(cacheable_events)
    await write_lookup_index_rows(
        db,
        namespace=namespace,
        room_id=room_id,
        serialized_events=serialized_events,
        cached_at=validated_at,
        thread_id=thread_id,
    )
    for event in serialized_events:
        await db.execute(
            """
            INSERT INTO mindroom_event_cache_thread_events(namespace, room_id, thread_id, event_id, origin_server_ts)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT(namespace, room_id, event_id) DO UPDATE SET
                thread_id = excluded.thread_id,
                origin_server_ts = excluded.origin_server_ts,
                event_json = NULL,
                write_seq = nextval('mindroom_event_cache_write_seq')
            """,
            (
                namespace,
                room_id,
                thread_id,
                event.event_id,
                event.origin_server_ts,
            ),
        )
    await _upsert_thread_cache_state(
        db,
        namespace=namespace,
        room_id=room_id,
        thread_id=thread_id,
        validated_at=validated_at,
    )
    return frozenset(event.event_id for event in serialized_events)


async def _replace_thread_locked(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str,
    thread_id: str,
    events: list[dict[str, Any]],
    validated_at: float,
) -> None:
    """Replace one thread snapshot atomically within an existing DB transaction."""
    existing_event_ids = await _thread_event_ids_for_thread(
        db,
        namespace=namespace,
        room_id=room_id,
        thread_id=thread_id,
    )
    replacement_event_ids = await _store_thread_events_locked(
        db,
        namespace=namespace,
        room_id=room_id,
        thread_id=thread_id,
        events=events,
        validated_at=validated_at,
    )
    removed_event_ids = sorted(set(existing_event_ids) - replacement_event_ids)
    if removed_event_ids:
        await db.execute(
            """
            DELETE FROM mindroom_event_cache_thread_events
            WHERE namespace = %s AND room_id = %s AND event_id = ANY(%s)
            """,
            (namespace, room_id, removed_event_ids),
        )
        await delete_cached_events(
            db,
            namespace=namespace,
            room_id=room_id,
            event_ids=removed_event_ids,
        )
        await delete_event_edit_rows(
            db,
            namespace,
            room_id,
            event_ids=removed_event_ids,
            original_event_id=None,
        )
        await delete_event_thread_rows(
            db,
            namespace,
            room_id,
            event_ids=removed_event_ids,
            current_self_root_ids={thread_id} if thread_id in replacement_event_ids else (),
        )


async def replace_thread_locked_if_not_newer(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str,
    thread_id: str,
    events: list[dict[str, Any]],
    fetch_started_at: float,
    validated_at: float,
) -> ThreadCacheReplaceOutcome:
    """Replace one thread snapshot or classify the newer state that won."""
    cache_state_row = await _load_thread_cache_state_row(
        db,
        namespace=namespace,
        room_id=room_id,
        thread_id=thread_id,
    )
    # The snapshot-row lookup only distinguishes conflict outcomes, so unchanged state skips it.
    conflict = (
        guarded_thread_replacement_conflict(
            cache_state_row,
            fetch_started_at=fetch_started_at,
            has_snapshot_rows=await _thread_has_snapshot_rows_for_thread(
                db,
                namespace=namespace,
                room_id=room_id,
                thread_id=thread_id,
            ),
        )
        if thread_cache_state_changed_after(cache_state_row, fetch_started_at=fetch_started_at)
        else None
    )
    if conflict is not None:
        return conflict
    await _replace_thread_locked(
        db,
        namespace=namespace,
        room_id=room_id,
        thread_id=thread_id,
        events=events,
        validated_at=validated_at,
    )
    return ThreadCacheReplaceOutcome.STORED


async def invalidate_thread_locked(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str,
    thread_id: str,
) -> None:
    """Delete cached events and state for one thread within an existing transaction."""
    event_ids = await _thread_event_ids_for_thread(
        db,
        namespace=namespace,
        room_id=room_id,
        thread_id=thread_id,
    )
    await db.execute(
        """
        DELETE FROM mindroom_event_cache_thread_events
        WHERE namespace = %s AND room_id = %s AND thread_id = %s
        """,
        (namespace, room_id, thread_id),
    )
    if event_ids:
        await delete_cached_events(db, namespace=namespace, room_id=room_id, event_ids=event_ids)
        await delete_event_edit_rows(
            db,
            namespace,
            room_id,
            event_ids=event_ids,
            original_event_id=None,
        )
        await delete_event_thread_rows(
            db,
            namespace,
            room_id,
            event_ids=event_ids,
        )
    await db.execute(
        """
        DELETE FROM mindroom_event_cache_thread_state
        WHERE namespace = %s AND room_id = %s AND thread_id = %s
        """,
        (namespace, room_id, thread_id),
    )


async def invalidate_room_threads_locked(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str,
) -> None:
    """Delete every cached thread snapshot while preserving durable room membership."""
    event_ids = await _thread_event_ids_for_room(db, namespace=namespace, room_id=room_id)
    await db.execute(
        """
        DELETE FROM mindroom_event_cache_thread_events
        WHERE namespace = %s AND room_id = %s
        """,
        (namespace, room_id),
    )
    if event_ids:
        await delete_cached_events(db, namespace=namespace, room_id=room_id, event_ids=event_ids)
        await delete_event_edit_rows(
            db,
            namespace,
            room_id,
            event_ids=event_ids,
            original_event_id=None,
        )
        await delete_event_thread_rows(
            db,
            namespace,
            room_id,
            event_ids=event_ids,
        )
    await db.execute(
        """
        DELETE FROM mindroom_event_cache_thread_state
        WHERE namespace = %s AND room_id = %s
        """,
        (namespace, room_id),
    )


async def mark_thread_stale_locked(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str,
    thread_id: str,
    reason: str,
    invalidated_at: float | None = None,
) -> None:
    """Persist a durable invalidate-and-refetch marker within an active transaction."""
    stale_at = time.time() if invalidated_at is None else invalidated_at
    incremental_reasons = list(incremental_thread_revalidation_reasons())
    incoming_is_incremental = is_incremental_thread_revalidation_reason(reason)
    await db.execute(
        """
        INSERT INTO mindroom_event_cache_thread_state(
            namespace,
            room_id,
            thread_id,
            validated_at,
            invalidated_at,
            invalidation_reason
        )
        VALUES (%s, %s, %s, NULL, %s, %s)
        ON CONFLICT(namespace, room_id, thread_id) DO UPDATE SET
            invalidated_at = CASE
                WHEN mindroom_event_cache_thread_state.invalidated_at IS NULL
                    OR excluded.invalidated_at >= mindroom_event_cache_thread_state.invalidated_at
                    THEN excluded.invalidated_at
                ELSE mindroom_event_cache_thread_state.invalidated_at
            END,
            invalidation_reason = CASE
                WHEN mindroom_event_cache_thread_state.invalidated_at IS NULL
                    THEN excluded.invalidation_reason
                WHEN %s
                    AND NOT COALESCE(
                        mindroom_event_cache_thread_state.invalidation_reason = ANY(%s),
                        FALSE
                    )
                    THEN mindroom_event_cache_thread_state.invalidation_reason
                WHEN NOT %s
                    AND COALESCE(
                        mindroom_event_cache_thread_state.invalidation_reason = ANY(%s),
                        FALSE
                    )
                    THEN excluded.invalidation_reason
                WHEN excluded.invalidated_at >= mindroom_event_cache_thread_state.invalidated_at
                    THEN excluded.invalidation_reason
                ELSE mindroom_event_cache_thread_state.invalidation_reason
            END
        """,
        (
            namespace,
            room_id,
            thread_id,
            stale_at,
            reason,
            incoming_is_incremental,
            incremental_reasons,
            incoming_is_incremental,
            incremental_reasons,
        ),
    )


async def apply_thread_mutation_append_locked(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str,
    thread_id: str,
    normalized_event: dict[str, Any],
    append_failed_reason: str,
) -> ThreadAppendOutcome:
    """Append one threaded mutation and settle this thread's trust in the same transaction.

    See invariant 5 in the module docstring of ``sqlite_event_cache_threads`` for why it is one.
    """
    outcome = await _append_existing_thread_event(
        db,
        namespace=namespace,
        room_id=room_id,
        thread_id=thread_id,
        normalized_event=normalized_event,
    )
    if outcome is not ThreadAppendOutcome.APPENDED:
        await mark_thread_stale_locked(
            db,
            namespace=namespace,
            room_id=room_id,
            thread_id=thread_id,
            reason=append_failed_reason,
        )
        return outcome

    # Read after the append: it touches no trust column, and the failure paths above never need it.
    state_row = await _load_thread_cache_state_row(
        db,
        namespace=namespace,
        room_id=room_id,
        thread_id=thread_id,
    )
    if not append_keeps_thread_valid(state_row):
        return ThreadAppendOutcome.APPENDED_STALE

    await db.execute(
        """
        UPDATE mindroom_event_cache_thread_state
        SET validated_at = %s, invalidated_at = NULL, invalidation_reason = NULL
        WHERE namespace = %s AND room_id = %s AND thread_id = %s
        """,
        (time.time(), namespace, room_id, thread_id),
    )
    return ThreadAppendOutcome.APPENDED


async def mark_room_stale_locked(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str,
    reason: str,
    invalidated_at: float | None = None,
) -> None:
    """Persist one durable room-scoped invalidate-and-refetch marker."""
    stale_at = time.time() if invalidated_at is None else invalidated_at
    await db.execute(
        """
        INSERT INTO mindroom_event_cache_room_state(namespace, room_id, invalidated_at, invalidation_reason)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT(namespace, room_id) DO UPDATE SET
            invalidated_at = CASE
                WHEN mindroom_event_cache_room_state.invalidated_at IS NULL
                    OR excluded.invalidated_at >= mindroom_event_cache_room_state.invalidated_at
                    THEN excluded.invalidated_at
                ELSE mindroom_event_cache_room_state.invalidated_at
            END,
            invalidation_reason = CASE
                WHEN mindroom_event_cache_room_state.invalidated_at IS NULL
                    OR excluded.invalidated_at >= mindroom_event_cache_room_state.invalidated_at
                    THEN excluded.invalidation_reason
                ELSE mindroom_event_cache_room_state.invalidation_reason
            END
        """,
        (namespace, room_id, stale_at, reason),
    )


async def _append_existing_thread_event(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str,
    thread_id: str,
    normalized_event: dict[str, Any],
) -> ThreadAppendOutcome:
    """Append one event to an existing cached thread and classify what happened.

    An opaque ``m.room.encrypted`` payload never replaces stored clear content for the same event ID.
    A redacted event (or one whose edit target is redacted) is refused before anything is written, so
    its payload never reaches the point-lookup table.
    """
    event_id = event_id_for_cache(normalized_event)
    if await event_or_original_is_redacted(
        db,
        namespace,
        room_id,
        event_id=event_id,
        event=normalized_event,
    ):
        return ThreadAppendOutcome.APPEND_REFUSED

    serialized_event = serialize_cached_event(event_id, normalized_event)
    row = await fetchone(
        db,
        """
        SELECT 1
        FROM mindroom_event_cache_thread_events AS membership
        JOIN mindroom_event_cache_events AS events
            ON events.namespace = membership.namespace
            AND events.event_id = membership.event_id
            AND events.room_id = membership.room_id
        WHERE membership.namespace = %s
            AND membership.room_id = %s
            AND membership.thread_id = %s
        LIMIT 1
        """,
        (namespace, room_id, thread_id),
    )
    thread_exists = row is not None
    await write_lookup_index_rows(
        db,
        namespace=namespace,
        room_id=room_id,
        serialized_events=[serialized_event],
        cached_at=time.time(),
        thread_id=thread_id,
    )
    if not thread_exists:
        # Only lookup-index rows are recorded: there is no snapshot to extend, so only a full
        # history scan can make this thread readable again.
        return ThreadAppendOutcome.SNAPSHOT_MISSING
    await db.execute(
        """
        INSERT INTO mindroom_event_cache_thread_events(namespace, room_id, thread_id, event_id, origin_server_ts)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT(namespace, room_id, event_id) DO UPDATE SET
            thread_id = excluded.thread_id,
            origin_server_ts = excluded.origin_server_ts,
            event_json = NULL,
            write_seq = nextval('mindroom_event_cache_write_seq')
        """,
        (
            namespace,
            room_id,
            thread_id,
            serialized_event.event_id,
            serialized_event.origin_server_ts,
        ),
    )
    return ThreadAppendOutcome.APPENDED


async def _upsert_thread_cache_state(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str,
    thread_id: str,
    validated_at: float,
) -> None:
    await db.execute(
        """
        INSERT INTO mindroom_event_cache_thread_state(
            namespace,
            room_id,
            thread_id,
            validated_at,
            invalidated_at,
            invalidation_reason
        )
        VALUES (%s, %s, %s, %s, NULL, NULL)
        ON CONFLICT(namespace, room_id, thread_id) DO UPDATE SET
            validated_at = excluded.validated_at,
            invalidated_at = NULL,
            invalidation_reason = NULL
        """,
        (namespace, room_id, thread_id, validated_at),
    )


async def _thread_event_ids_for_thread(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str,
    thread_id: str,
) -> list[str]:
    """Return cached event IDs currently stored for one thread."""
    rows = await fetchall(
        db,
        """
        SELECT event_id
        FROM mindroom_event_cache_thread_events
        WHERE namespace = %s AND room_id = %s AND thread_id = %s
        """,
        (namespace, room_id, thread_id),
    )
    return [str(row[0]) for row in rows]


async def _thread_has_snapshot_rows_for_thread(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str,
    thread_id: str,
) -> bool:
    """Return whether one thread has at least one cached snapshot row."""
    row = await fetchone(
        db,
        """
        SELECT 1
        FROM mindroom_event_cache_thread_events
        WHERE namespace = %s AND room_id = %s AND thread_id = %s
        LIMIT 1
        """,
        (namespace, room_id, thread_id),
    )
    return row is not None


async def _thread_event_ids_for_room(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str,
) -> list[str]:
    """Return cached event IDs currently stored for every thread in one room."""
    rows = await fetchall(
        db,
        """
        SELECT event_id
        FROM mindroom_event_cache_thread_events
        WHERE namespace = %s AND room_id = %s
        """,
        (namespace, room_id),
    )
    return [str(row[0]) for row in rows]
