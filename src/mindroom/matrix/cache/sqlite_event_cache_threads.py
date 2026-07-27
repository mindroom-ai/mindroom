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

if TYPE_CHECKING:
    import aiosqlite

    from .event_cache import ThreadCacheState

# The edits that survive a collapsed read: one per message, from the right sender.
#
# A thread stores every edit ever sent. Returning them all makes the caller's fold re-derive per
# message what one window function derives once, and hauls the whole superseded history across
# the wire to do it.
#
# Two real agent workloads measured 6x apart on edit density, and the homeserver is why. The
# mindroom-tuwunel fork collapses superseded m.replace events per (target, sender) on read
# (``collapse_superseded_edits``) and deletes them outright once they age past its edit-purge
# minimum, 86,400 seconds by default. So a cache whose threads were re-read after that carries
# ~1.07 edits per (original, sender) and loses under 2% of its rows here, for about 1.3x the plain
# read, while one accumulating live sync inside the purge window runs 53% edit rows at 6.30 per
# original, max 170, with a thread at 94.5% edits. Threads are small either way - p50 9 rows,
# max 538. Expect the cheap case against that fork and the expensive one against a stock
# homeserver; neither is the shape.
#
# That the fork groups by (target, sender) is worth saying twice. It is the same key this query
# uses, arrived at independently, which is why grouping by original alone was a real defect rather
# than a theoretical one. It also orders by ``event_id.cmp()``, bytewise, so the COLLATE "C" pin
# below has to hold for this read to agree with the homeserver as well as with SQLite and the
# fold.
#
# So be precise about what this buys, because two earlier revisions of this comment were not. It
# does not reduce writes - it is a read-side query, and every edit is still stored. It does not
# change what the fold produces either; the fold already picked one edit per message. What it buys
# is fewer rows off disk and over the wire, and less fold work, on a median thread of nine rows -
# paid for with a window function and three joins on every read. The correctness fixes that came
# with it are the substantial part. The slowest measured read was 126.6 ms warm, so no speedup
# is claimed, and the 2,021-row thread quoted here before was this repository's synthetic
# fixture (20 messages x 100 edits), not a real one.
#
# The root fix is upstream of this query - prune superseded edits at the source and there is
# nothing to collapse - and against the mindroom-tuwunel fork it is already built, so do not plan
# it again as an open question. Its edit-purge service deletes superseded edits once they pass the
# minimum age above, which also settles the trade this comment used to call undecided: redacting
# the current winning edit is contractually supposed to reveal the previous one
# (``test_redacting_latest_edit_falls_back_to_previous_cached_edit``), and past that age there is
# no previous one left to reveal, wherever the read is served from. It was already close to
# unreachable - redacting a MESSAGE removes the original and every dependent edit together, and
# mindroom-cinny's delete targets the original event ID, since ``MessageDeleteItem`` passes
# ``mEvent.getId()`` and a replacement is only ever reached through ``replacingEvent()``, never a
# redaction target there. Reaching the rollback path needs the raw API,
# ``/redact <edit-event-id>``, or moderation tooling. Element was not checked.
#
# What stays genuinely open is the case this cache must still handle alone: a stock homeserver that
# neither collapses nor purges, where every superseded edit arrives and stays.
#
# The contract to implement, if it is built: keep only the current legitimate edit per (original,
# sender); redacting an already-pruned edit tombstones it and is otherwise a no-op; redacting the
# retained winner deletes it, marks the thread stale and refetches full history, which
# ``invalidate_after_redaction`` in ``thread_writes`` already does on the live redaction path; if
# the homeserver is unreachable at that moment, fail or degrade explicitly rather than serving the
# pre-edit body as confirmed history; and keep tombstones, so out-of-order sync cannot resurrect a
# deleted edit.
#
# "Surviving" is per (original, sender): a replacement is only legitimate from the sender of the
# event it replaces, so keeping a single newest-overall edit lets any room member starve the fold
# of the author's own and pin the message at its pre-edit body. Membership is joined in here rather
# than filtered later, because ranking over edits the outer query will discard lets an
# out-of-thread edit suppress the in-thread runner-up.
#
# The sender comparison here is an optimization, not the security boundary. The fold re-checks
# every candidate against the JSON sender (``ThreadEditCandidates.winner_for``), so if this
# filter ever admits a foreign replacement the fold still finds nothing in the author's bucket
# and renders the pre-edit body - wrong, but not the attacker's text. Doing it in SQL keeps a
# foreign edit from being ranked as the survivor and hiding the author's own.
#
# The original is LEFT joined, not required. An edit can outlive the message it replaces -
# ``event_edits`` holds no foreign key to ``events`` - and the fold synthesizes a message from such
# an edit rather than dropping it, carrying the editor's own sender because an original nobody has
# seen cannot be impersonated. Requiring the original would delete those messages from the read
# outright. The sender filter is skipped exactly when there is no original to compare against,
# which is also when ``winner_for`` stops applying it, for the same reason.
#
# The original is read out of ``events`` alone, with no thread membership required. Two narrower
# lookups were tried first and both silently disabled the filter. Scoping it to this thread made an
# original cached in a sibling thread read as absent. Routing it through ``thread_events`` at all
# then did the same to any original cached by a point lookup, because ``store_event`` writes the
# payload with no membership row - so ``original_events`` came back NULL, the sender filter was
# skipped, and the newest edit across all senders won, which is the exact suppression this filter
# exists to prevent (``test_a_point_cached_original_still_scopes_edits_to_its_sender``). The
# comparison needs the payload and nothing else, so asking for more can only lose a sender it could
# have compared against.
#
# ROW_NUMBER over one pass rather than a correlated NOT EXISTS per candidate: 5.3 ms against
# 8.7 ms on a synthetic 2,021-event thread with current table statistics. Policy stays in Python; this is
# only "latest per group", which is what a window function is for. Splitting present-original and
# absent-original edits into two CTEs scans ``event_edits`` twice and timed out a 2,000-edit
# PostgreSQL test that one pass completes.
#
# MATERIALIZED is a hint, not a correctness requirement: measured 3.7 ms materialized against
# 4.1 ms inlinable. It is kept only to stop the planner re-deriving the survivors per row.
_SURVIVING_EDITS_CTE = """
WITH surviving_edits AS MATERIALIZED (
    SELECT edit_event_id
    FROM (
        SELECT event_edits.edit_event_id AS edit_event_id,
               ROW_NUMBER() OVER (
                   PARTITION BY event_edits.original_event_id
                   ORDER BY event_edits.origin_server_ts DESC, event_edits.edit_event_id DESC
               ) AS edit_rank
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
        LEFT JOIN events AS original_events
            ON original_events.principal_id = event_edits.principal_id
            AND original_events.room_id = event_edits.room_id
            AND original_events.event_id = event_edits.original_event_id
        WHERE event_edits.principal_id = :principal_id
            AND event_edits.room_id = :room_id
            AND (original_events.event_id IS NULL OR edit_events.sender = original_events.sender)
    )
    WHERE edit_rank = 1
)
"""

# One thread, collapsed: every non-edit row, plus the one surviving edit per edited message.
_THREAD_EVENTS_SQL = (
    _SURVIVING_EDITS_CTE  # noqa: S608 - both operands are literals; params stay bound
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
        NOT EXISTS (
            SELECT 1
            FROM event_edits AS row_is_an_edit
            WHERE row_is_an_edit.principal_id = thread_events.principal_id
                AND row_is_an_edit.room_id = thread_events.room_id
                AND row_is_an_edit.edit_event_id = thread_events.event_id
        )
        OR thread_events.event_id IN (SELECT edit_event_id FROM surviving_edits)
    )
ORDER BY thread_events.origin_server_ts ASC, thread_events.write_seq ASC
"""
)


async def load_thread_events(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
    room_id: str,
    thread_id: str,
) -> list[dict[str, Any]] | None:
    """Return one thread's cached events oldest first, collapsed to one edit per message."""
    cursor = await db.execute(
        _THREAD_EVENTS_SQL,
        {"principal_id": principal_id, "room_id": room_id, "thread_id": thread_id},
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return [json.loads(row[2]) for row in rows] if rows else None


async def load_thread_event_ids(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
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
    cursor = await db.execute(
        """
        SELECT thread_events.event_id
        FROM thread_events
        JOIN events
            ON events.principal_id = thread_events.principal_id
            AND events.room_id = thread_events.room_id
            AND events.event_id = thread_events.event_id
        WHERE thread_events.principal_id = ? AND thread_events.room_id = ? AND thread_events.thread_id = ?
        """,
        (principal_id, room_id, thread_id),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return {str(row[0]) for row in rows}


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
