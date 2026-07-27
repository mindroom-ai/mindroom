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

3. Incremental revalidation is allowlisted: ``append_keeps_thread_valid`` leaves a thread trusted only
   when it was previously validated, any invalidation reason is one of the incremental mutation reasons,
   and the room was not invalidated at or after that validation.
   Invalidations from any other reason can only be cleared by a full authoritative snapshot replacement.

4. Thread snapshot rows and the lookup, edit, and thread index rows are written and deleted together so
   point lookups can never resurrect rows the snapshot no longer contains.

5. One threaded mutation is one transaction: ``apply_thread_mutation_append_locked`` appends and settles
   trust together. Marking stale, appending, and revalidating as three separate operations reported a
   thread that was about to be perfectly appendable as invalid for the duration, so every read arriving
   in that window rejected a good snapshot and paid for a full history scan. In one transaction a reader
   observes either the state before the mutation or the state after it, and a crash rolls back rather
   than leaving a half-applied snapshot trusted.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any, Literal

from mindroom.logging_config import get_logger

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
from .thread_read_window import (
    UNBOUNDED_THREAD_READ,
    ThreadReadBudget,
    ThreadWindowCandidate,
    ThreadWindowRead,
    log_thread_window_selection,
    select_thread_window_event_ids,
)

logger = get_logger(__name__)

if TYPE_CHECKING:
    import aiosqlite

    from .event_cache import ThreadCacheState


_UNBOUNDED_THREAD_EVENTS_SQL = """
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
"""

# The winning edits for one thread, computed once.
#
# "Winning" is per (original, sender): a replacement is only legitimate from the sender of the
# event it replaces, so shipping a single newest-overall edit lets any room member starve the fold
# of the author's own and pin the message at its pre-edit body. Membership is joined in here rather
# than filtered later, because ranking over edits the outer query will discard lets an
# out-of-thread edit suppress the in-thread runner-up.
#
# ROW_NUMBER over one pass rather than a correlated NOT EXISTS per candidate: 5.3 ms against
# 8.7 ms on a 2,021-event thread with current table statistics. Policy stays in Python; this is
# only "latest per group", which is what a window function is for.
#
# Earlier revisions of this comment claimed the correlated shape cost 571 ms, or minutes. Those
# numbers were planner misestimation on a freshly seeded database with no statistics, not the query
# shape: on unanalyzed tables an unseen namespace estimates 1 row against 2,021 actual, every join
# degrades to a nested loop with a join filter, and BOTH shapes collapse - the unbounded read
# included, by 77x. Do not re-derive this query's cost without ANALYZE.
#
# MATERIALIZED is a hint, not a correctness requirement: measured 3.7 ms materialized against
# 4.1 ms inlinable. It is kept only to stop the planner re-deriving the winners per candidate row.
_WINNING_EDITS_CTE = """
WITH winning_edits AS MATERIALIZED (
    SELECT edit_event_id, original_event_id, event_bytes
    FROM (
        SELECT event_edits.edit_event_id AS edit_event_id,
               event_edits.original_event_id AS original_event_id,
               edit_events.event_bytes AS event_bytes,
               ROW_NUMBER() OVER (
                   PARTITION BY event_edits.original_event_id
                   ORDER BY event_edits.origin_server_ts DESC, event_edits.edit_event_id DESC
               ) AS sender_rank
        FROM event_edits
        JOIN thread_events AS edit_membership
            ON edit_membership.principal_id = event_edits.principal_id
            AND edit_membership.room_id = event_edits.room_id
            AND edit_membership.event_id = event_edits.edit_event_id
            AND edit_membership.thread_id = :thread_id
        JOIN events AS edit_events
            ON edit_events.principal_id = event_edits.principal_id
            AND edit_events.room_id = event_edits.room_id
            AND edit_events.event_id = event_edits.edit_event_id
        JOIN events AS original_events
            ON original_events.principal_id = event_edits.principal_id
            AND original_events.room_id = event_edits.room_id
            AND original_events.event_id = event_edits.original_event_id
        WHERE event_edits.principal_id = :principal_id
            AND event_edits.room_id = :room_id
            AND edit_events.sender = original_events.sender
    )
    WHERE sender_rank = 1
)
"""

# Selection query. Prices every message in the thread from inline columns alone: no ``event_json``
# is referenced, so no payload outside the window is read or parsed. Rows that are themselves edits
# are anti-joined out, because a bound over raw thread rows selects one message and a pile of its
# own edits. A candidate costs its own payload plus every edit the payload query will ship with it.
_THREAD_WINDOW_CANDIDATES_SQL = (
    _WINNING_EDITS_CTE  # noqa: S608 - both operands are literals; params stay bound
    + """
SELECT thread_events.event_id,
       events.event_bytes + COALESCE(edit_cost.total_bytes, 0) AS window_bytes
FROM thread_events
JOIN events
    ON events.principal_id = thread_events.principal_id
    AND events.room_id = thread_events.room_id
    AND events.event_id = thread_events.event_id
LEFT JOIN (
    SELECT original_event_id, SUM(event_bytes) AS total_bytes
    FROM winning_edits
    GROUP BY original_event_id
) AS edit_cost ON edit_cost.original_event_id = thread_events.event_id
WHERE thread_events.principal_id = :principal_id
    AND thread_events.room_id = :room_id
    AND thread_events.thread_id = :thread_id
    AND NOT EXISTS (
        SELECT 1
        FROM event_edits AS candidate_is_edit
        WHERE candidate_is_edit.principal_id = thread_events.principal_id
            AND candidate_is_edit.room_id = thread_events.room_id
            AND candidate_is_edit.edit_event_id = thread_events.event_id
    )
ORDER BY thread_events.origin_server_ts DESC, thread_events.write_seq DESC
"""
)

# Payload query. Fetches the selected originals, the thread root, and each one's winning edits.
_THREAD_WINDOW_PAYLOAD_SQL = (
    _WINNING_EDITS_CTE  # noqa: S608 - both operands are literals; params stay bound
    + """
SELECT thread_events.origin_server_ts, thread_events.write_seq, events.event_json
FROM thread_events
JOIN events
    ON events.principal_id = thread_events.principal_id
    AND events.room_id = thread_events.room_id
    AND events.event_id = thread_events.event_id
WHERE thread_events.principal_id = :principal_id
    AND thread_events.room_id = :room_id
    AND thread_events.thread_id = :thread_id
    AND (
        thread_events.event_id IN (SELECT value FROM json_each(:selected_event_ids))
        OR thread_events.event_id = :thread_id
        OR thread_events.event_id IN (
            SELECT edit_event_id
            FROM winning_edits
            WHERE original_event_id IN (SELECT value FROM json_each(:selected_event_ids))
                OR original_event_id = :thread_id
        )
    )
ORDER BY thread_events.origin_server_ts ASC, thread_events.write_seq ASC
"""
)


async def _load_thread_window_candidates(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
    room_id: str,
    thread_id: str,
) -> list[ThreadWindowCandidate]:
    """Return this thread's messages newest first with the bytes each would cost."""
    cursor = await db.execute(
        _THREAD_WINDOW_CANDIDATES_SQL,
        {"principal_id": principal_id, "room_id": room_id, "thread_id": thread_id},
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return [ThreadWindowCandidate(event_id=str(row[0]), window_bytes=int(row[1])) for row in rows]


async def load_thread_window(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
    room_id: str,
    thread_id: str,
    budget: ThreadReadBudget = UNBOUNDED_THREAD_READ,
) -> ThreadWindowRead:
    """Return cached events for one thread sorted by timestamp.

    An unbounded budget returns every stored row. A bounded budget selects the newest messages that
    fit from inline columns, then fetches only those payloads, each with its latest edit, plus the
    thread root.
    """
    if not budget.is_bounded:
        cursor = await db.execute(_UNBOUNDED_THREAD_EVENTS_SQL, (principal_id, room_id, thread_id))
        rows = await cursor.fetchall()
        await cursor.close()
        return ThreadWindowRead(
            events=[json.loads(row[2]) for row in rows] if rows else None,
            truncated=False,
        )

    candidates = await _load_thread_window_candidates(
        db,
        principal_id=principal_id,
        room_id=room_id,
        thread_id=thread_id,
    )
    if not candidates:
        return ThreadWindowRead(events=None, truncated=False)
    # The payload query may return fewer rows than were selected: redaction hard-deletes, so an
    # event removed between the two phases is correctly absent. A short result is normal here and
    # must never be asserted against the selected count.
    selection = select_thread_window_event_ids(candidates, budget=budget)
    log_thread_window_selection(
        selection,
        budget=budget,
        logger=logger,
        room_id=room_id,
        thread_id=thread_id,
    )
    cursor = await db.execute(
        _THREAD_WINDOW_PAYLOAD_SQL,
        {
            "principal_id": principal_id,
            "room_id": room_id,
            "thread_id": thread_id,
            "selected_event_ids": json.dumps(selection.event_ids),
        },
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return ThreadWindowRead(
        events=[json.loads(row[2]) for row in rows] if rows else None,
        truncated=selection.truncated,
    )


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
            current_self_root_ids={thread_id} if thread_id in replacement_event_ids else (),
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
) -> ThreadCacheReplaceOutcome:
    """Replace one thread snapshot or classify the newer state that won."""
    cache_state_row = await _load_thread_cache_state_row(
        db,
        principal_id=principal_id,
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
                principal_id=principal_id,
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
        principal_id=principal_id,
        room_id=room_id,
        thread_id=thread_id,
        events=events,
        validated_at=validated_at,
    )
    return ThreadCacheReplaceOutcome.STORED


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


async def apply_thread_mutation_append_locked(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
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
        principal_id=principal_id,
        room_id=room_id,
        thread_id=thread_id,
        normalized_event=normalized_event,
    )
    if outcome is not ThreadAppendOutcome.APPENDED:
        await mark_thread_stale_locked(
            db,
            principal_id=principal_id,
            room_id=room_id,
            thread_id=thread_id,
            reason=append_failed_reason,
        )
        return outcome

    # Read after the append: it touches no trust column, and the failure paths above never need it.
    state_row = await _load_thread_cache_state_row(
        db,
        principal_id=principal_id,
        room_id=room_id,
        thread_id=thread_id,
    )
    if not append_keeps_thread_valid(state_row):
        return ThreadAppendOutcome.APPENDED_STALE

    await db.execute(
        """
        UPDATE thread_cache_state
        SET validated_at = ?, invalidated_at = NULL, invalidation_reason = NULL
        WHERE principal_id = ? AND room_id = ? AND thread_id = ?
        """,
        (time.time(), principal_id, room_id, thread_id),
    )
    return ThreadAppendOutcome.APPENDED


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


async def _append_existing_thread_event(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
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
        principal_id,
        room_id,
        event_id=event_id,
        event=normalized_event,
    ):
        return ThreadAppendOutcome.APPEND_REFUSED

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
        # Only lookup-index rows are recorded: there is no snapshot to extend, so only a full
        # history scan can make this thread readable again.
        return ThreadAppendOutcome.SNAPSHOT_MISSING

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
    return ThreadAppendOutcome.APPENDED


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


async def _thread_has_snapshot_rows_for_thread(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
    room_id: str,
    thread_id: str,
) -> bool:
    """Return whether one thread has at least one cached snapshot row."""
    cursor = await db.execute(
        """
        SELECT 1
        FROM thread_events
        WHERE principal_id = ? AND room_id = ? AND thread_id = ?
        LIMIT 1
        """,
        (principal_id, room_id, thread_id),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return row is not None


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
