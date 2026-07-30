"""Thread snapshot and gap storage helpers for the Matrix event cache.

Durable gap-state invariants (mirrored by ``postgres_event_cache_threads``):

1. Gap markers are monotonic: ``mark_thread_gap_locked`` never lets an older marker overwrite a
   newer one. There is no reason precedence, because every reason means the same thing — refetch.

2. ``mark_room_gap_locked`` is the room-scoped (wildcard-thread) form. It fans the marker out
   across the room's ``thread_cache_state`` rows *and* records it once on ``room_cache_state``.
   The fan-out cannot reach a thread whose first fetch is still in flight, because that thread has
   no row yet; the room-level copy is what the replacement then consults.

3. A replacement clears the marker only when the marker predates the fetch that produced it.
   A gap detected mid-fetch is not covered by that fetch, so it survives and the next read refetches.

4. Thread snapshot rows and the lookup, edit, and thread index rows are written and deleted together so
   point lookups can never resurrect rows the snapshot no longer contains.

5. One threaded mutation is one transaction: ``apply_thread_mutation_append_locked`` appends and, when
   the append cannot land, records the gap marker in the same transaction. Marking and appending
   separately left a thread readable while it was missing the event, and a crash between them left a
   half-applied snapshot unmarked.
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
    ThreadCacheGap,
    thread_cache_gap_row,
)

if TYPE_CHECKING:
    import aiosqlite


# The edits that survive a collapsed read: one per message, from the right sender.
#
# A thread stores every edit ever sent. Returning them all makes the caller's fold re-derive per
# message what one window function derives once, and hauls the whole superseded history across
# the wire to do it.
#
# Edit density depends partly on homeserver policy. The mindroom-tuwunel fork can collapse
# superseded m.replace events in sync responses with ``mindroom_compact_edits_enabled`` and can
# delete aged superseded edits with ``mindroom_edit_purge_enabled``. Both flags default to false,
# so a default-configured fork can expose the same accumulated edit history as a stock homeserver.
#
# The fork's opt-in collapse groups by (target, sender), the same key this query uses. It also
# orders by ``event_id.cmp()``, bytewise, so the COLLATE "C" pin below has to hold for this read to
# agree with the homeserver as well as with SQLite and the fold.
#
# So be precise about what this buys, because two earlier revisions of this comment were not. It
# does not reduce writes - it is a read-side query, and every edit is still stored. It does not
# change what the fold produces either; the fold already picked one edit per message. What it buys
# is fewer rows off disk and over the wire, and less fold work, paid for with a window function and
# three joins on every read. The correctness fixes that came with it are the substantial part; no
# universal speedup is claimed because the result depends on edit density and database statistics.
#
# Upstream pruning is available in the fork but remains opt-in. Where edit purge is enabled,
# deleting superseded edits after the configured minimum age settles the rollback trade at that
# boundary: redacting the current winning edit is contractually supposed to reveal the previous one
# (``test_redacting_latest_edit_falls_back_to_previous_cached_edit``), and past that age there is
# no previous one left to reveal, wherever the read is served from. It was already close to
# unreachable - redacting a MESSAGE removes the original and every dependent edit together, and
# mindroom-cinny's delete targets the original event ID, since ``MessageDeleteItem`` passes
# ``mEvent.getId()`` and a replacement is only ever reached through ``replacingEvent()``, never a
# redaction target there. Reaching the rollback path needs the raw API,
# ``/redact <edit-event-id>``, or moderation tooling. Element was not checked.
#
# This cache must still handle deployments where those opt-in controls are disabled, including a
# default-configured fork or a stock homeserver where superseded edits arrive and stay.
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

    Membership and visibility are different questions, and collapsing is what made them differ:
    the visible read shows one edit per message, while this returns every row the thread owns. The
    repair bookkeeping this was written for is gone; the surviving caller is the edit-sender rule's
    coverage, which needs the membership set to state its precondition.

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


async def load_thread_cache_gap(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
    room_id: str,
    thread_id: str,
) -> ThreadCacheGap | None:
    """Return the durable gap marker recorded against one cached thread, if any.

    Room-scoped gaps are fanned out across the room's thread rows when they are marked, so this is
    a single-table read with no room-state join.
    """
    cursor = await db.execute(
        """
        SELECT gap_marked_at, gap_reason
        FROM thread_cache_state
        WHERE principal_id = ? AND room_id = ? AND thread_id = ?
        """,
        (principal_id, room_id, thread_id),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return thread_cache_gap_row(row)


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
    """Advance one durable room-membership transition and gap-mark prior refills."""
    await mark_room_gap_locked(
        db,
        principal_id=principal_id,
        room_id=room_id,
        reason=reason,
    )
    # 🔒 ``mark_room_gap_locked`` has already upserted the row, so the epoch below always has
    # something to advance. A missing row and a fresh one both read as ``('joined', 0)``.
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
    stored_at: float,
    fetch_started_at: float,
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
            cached_at=stored_at,
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
    await _clear_thread_gap_covered_by_fetch(
        db,
        principal_id=principal_id,
        room_id=room_id,
        thread_id=thread_id,
        fetch_started_at=fetch_started_at,
    )
    return frozenset(event.event_id for event in serialized_events)


async def _thread_snapshot_is_newer_than_fetch(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
    room_id: str,
    thread_id: str,
    fetch_started_at: float,
) -> bool:
    """Return whether an installed snapshot came from a strictly newer fetch than this one."""
    async with db.execute(
        """
        SELECT snapshot_fetch_started_at
        FROM thread_cache_state
        WHERE principal_id = ? AND room_id = ? AND thread_id = ?
        """,
        (principal_id, room_id, thread_id),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None or row[0] is None:
        return False
    return float(row[0]) > fetch_started_at


async def _uncovered_room_gap(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
    room_id: str,
    fetch_started_at: float,
) -> tuple[float, str | None] | None:
    """Return the room-scoped gap one fetch does not cover, if there is one."""
    async with db.execute(
        """
        SELECT room_gap_marked_at, room_gap_reason
        FROM room_cache_state
        WHERE principal_id = ? AND room_id = ?
        """,
        (principal_id, room_id),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None or row[0] is None or float(row[0]) <= fetch_started_at:
        return None
    return (float(row[0]), row[1] if isinstance(row[1], str) else None)


async def _clear_thread_gap_covered_by_fetch(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
    room_id: str,
    thread_id: str,
    fetch_started_at: float,
) -> None:
    """Record this thread's snapshot, keeping any gap the replacing fetch does not cover.

    A gap marked after the fetch began describes events the fetch could not have seen, so it
    survives and the next read refetches. Both scopes have to be asked about, and the room one
    cannot be answered by the fan-out alone: a thread whose first fetch was in flight when the room
    was gapped has no row for the fan-out to update, so without the room-level copy this insert
    would record a clean snapshot for events fetched from before the gap.
    """
    room_gap = await _uncovered_room_gap(
        db,
        principal_id=principal_id,
        room_id=room_id,
        fetch_started_at=fetch_started_at,
    )
    room_gap_marked_at, room_gap_reason = room_gap if room_gap is not None else (None, None)
    await db.execute(
        """
        INSERT INTO thread_cache_state(
            principal_id,
            room_id,
            thread_id,
            gap_marked_at,
            gap_reason,
            snapshot_fetch_started_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(principal_id, room_id, thread_id) DO UPDATE SET
            snapshot_fetch_started_at = excluded.snapshot_fetch_started_at,
            gap_marked_at = CASE
                WHEN thread_cache_state.gap_marked_at IS NULL
                    OR thread_cache_state.gap_marked_at <= ?
                    THEN excluded.gap_marked_at
                ELSE thread_cache_state.gap_marked_at
            END,
            gap_reason = CASE
                WHEN thread_cache_state.gap_marked_at IS NULL
                    OR thread_cache_state.gap_marked_at <= ?
                    THEN excluded.gap_reason
                ELSE thread_cache_state.gap_reason
            END
        """,
        (
            principal_id,
            room_id,
            thread_id,
            room_gap_marked_at,
            room_gap_reason,
            fetch_started_at,
            fetch_started_at,
            fetch_started_at,
        ),
    )


async def replace_thread_locked(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
    room_id: str,
    thread_id: str,
    events: list[dict[str, Any]],
    stored_at: float,
    fetch_started_at: float,
) -> None:
    """Replace one thread snapshot atomically within an existing DB transaction.

    Replacement is ordered by ``fetch_started_at``, not by arrival: because installing a snapshot
    deletes the events it omits, a slow fetch landing after a newer one would otherwise bury the
    newer thread contents and leave no gap marker behind to force a refetch. An older fetch is
    therefore skipped outright. This is one comparison, not a conflict classifier — the loser has
    nothing to retry, since the snapshot already installed is strictly fresher than its own.

    The gap marker is separately conditional — see ``_clear_thread_gap_covered_by_fetch``.
    """
    if await _thread_snapshot_is_newer_than_fetch(
        db,
        principal_id=principal_id,
        room_id=room_id,
        thread_id=thread_id,
        fetch_started_at=fetch_started_at,
    ):
        return
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
        stored_at=stored_at,
        fetch_started_at=fetch_started_at,
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


async def mark_thread_gap_locked(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
    room_id: str,
    thread_id: str,
    reason: str,
) -> None:
    """Record one durable thread gap marker within an active transaction.

    The marker is monotonic: a later gap never loses to an earlier one. There is no reason
    precedence — every reason means the same thing, that this snapshot must be refetched.
    """
    await db.execute(
        """
        INSERT INTO thread_cache_state(
            principal_id,
            room_id,
            thread_id,
            gap_marked_at,
            gap_reason
        )
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(principal_id, room_id, thread_id) DO UPDATE SET
            gap_marked_at = CASE
                WHEN thread_cache_state.gap_marked_at IS NULL
                    OR excluded.gap_marked_at >= thread_cache_state.gap_marked_at
                    THEN excluded.gap_marked_at
                ELSE thread_cache_state.gap_marked_at
            END,
            gap_reason = CASE
                WHEN thread_cache_state.gap_marked_at IS NULL
                    OR excluded.gap_marked_at >= thread_cache_state.gap_marked_at
                    THEN excluded.gap_reason
                ELSE thread_cache_state.gap_reason
            END
        """,
        (principal_id, room_id, thread_id, time.time(), reason),
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
    """Append one threaded mutation, recording a gap marker in the same transaction when it cannot land.

    See invariant 5 in this module's docstring for why it is one transaction. A successful append
    clears nothing: an append extends a snapshot, it does not prove the snapshot complete.
    """
    outcome = await _append_existing_thread_event(
        db,
        principal_id=principal_id,
        room_id=room_id,
        thread_id=thread_id,
        normalized_event=normalized_event,
    )
    if outcome is not ThreadAppendOutcome.APPENDED:
        await mark_thread_gap_locked(
            db,
            principal_id=principal_id,
            room_id=room_id,
            thread_id=thread_id,
            reason=append_failed_reason,
        )
    return outcome


async def mark_room_gap_locked(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
    room_id: str,
    reason: str,
) -> None:
    """Record one room-scoped (wildcard-thread) gap across the room's threads and on the room itself.

    The fan-out reaches every thread that already holds a ``thread_cache_state`` row. That is not all
    of them: a thread whose first fetch is still in flight has no row yet, so the fan-out skips it and
    the replacement that lands afterwards would insert a clean row for a snapshot fetched from before
    the gap. The room-level copy is what that replacement consults, so the two together cover the room
    whether or not a thread's row existed when the gap was recorded.
    """
    gap_marked_at = time.time()
    await db.execute(
        """
        UPDATE thread_cache_state
        SET gap_reason = CASE
                WHEN gap_marked_at IS NULL OR ? >= gap_marked_at THEN ?
                ELSE gap_reason
            END,
            gap_marked_at = CASE
                WHEN gap_marked_at IS NULL OR ? >= gap_marked_at THEN ?
                ELSE gap_marked_at
            END
        WHERE principal_id = ? AND room_id = ?
        """,
        (gap_marked_at, reason, gap_marked_at, gap_marked_at, principal_id, room_id),
    )
    await db.execute(
        """
        INSERT INTO room_cache_state(principal_id, room_id, room_gap_marked_at, room_gap_reason)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(principal_id, room_id) DO UPDATE SET
            room_gap_reason = CASE
                WHEN room_cache_state.room_gap_marked_at IS NULL
                    OR excluded.room_gap_marked_at >= room_cache_state.room_gap_marked_at
                    THEN excluded.room_gap_reason
                ELSE room_cache_state.room_gap_reason
            END,
            room_gap_marked_at = MAX(
                COALESCE(room_cache_state.room_gap_marked_at, excluded.room_gap_marked_at),
                excluded.room_gap_marked_at
            )
        """,
        (principal_id, room_id, gap_marked_at, reason),
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
    reflected_at = time.time()
    await write_lookup_index_rows(
        db,
        principal_id=principal_id,
        room_id=room_id,
        serialized_events=[serialized_event],
        cached_at=reflected_at,
        thread_id=thread_id,
    )
    if row is None:
        # Only lookup-index rows are recorded: there is no snapshot to extend, so only a full
        # history scan can make this thread readable again. Advance the watermark anyway: a fetch
        # already in flight when this event landed cannot represent the thread, so it must not be
        # allowed to install a snapshot that predates the event. The runtime coordinator normally
        # serializes same-thread refills and appends; this watermark also protects off-lane startup,
        # prewarm, and cross-process races.
        await _advance_snapshot_watermark(
            db,
            principal_id=principal_id,
            room_id=room_id,
            thread_id=thread_id,
            reflected_at=reflected_at,
        )
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
    await _advance_snapshot_watermark(
        db,
        principal_id=principal_id,
        room_id=room_id,
        thread_id=thread_id,
        reflected_at=reflected_at,
    )
    return ThreadAppendOutcome.APPENDED


async def _advance_snapshot_watermark(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
    room_id: str,
    thread_id: str,
    reflected_at: float,
) -> None:
    """Record that this snapshot now reflects the thread as of ``reflected_at``.

    An append mutates the snapshot, so a fetch that started before it cannot represent the thread
    any more. Moving the watermark forward makes ``replace_thread_locked`` refuse such a fetch,
    which is what stops a slow scan from deleting a live event that landed while it was running.
    """
    await db.execute(
        """
        INSERT INTO thread_cache_state(
            principal_id,
            room_id,
            thread_id,
            snapshot_fetch_started_at
        )
        VALUES (?, ?, ?, ?)
        ON CONFLICT(principal_id, room_id, thread_id) DO UPDATE SET
            snapshot_fetch_started_at = MAX(
                COALESCE(thread_cache_state.snapshot_fetch_started_at, ?),
                ?
            )
        """,
        (principal_id, room_id, thread_id, reflected_at, reflected_at, reflected_at),
    )


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


async def thread_snapshot_exists(
    db: aiosqlite.Connection,
    *,
    principal_id: str,
    room_id: str,
    thread_id: str,
) -> bool:
    """Return whether one thread has at least one durably present snapshot row.

    Joined to ``events`` rather than reading membership alone: a membership row whose payload is
    gone is not durably present, and answering yes for one reports a thread as cached that no read
    can serve, which is how startup prewarm silently skips it.
    """
    async with db.execute(
        """
        SELECT 1
        FROM thread_events
        JOIN events
            ON events.principal_id = thread_events.principal_id
            AND events.room_id = thread_events.room_id
            AND events.event_id = thread_events.event_id
        WHERE thread_events.principal_id = ? AND thread_events.room_id = ? AND thread_events.thread_id = ?
        LIMIT 1
        """,
        (principal_id, room_id, thread_id),
    ) as cursor:
        return await cursor.fetchone() is not None


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
