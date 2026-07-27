"""Thread snapshot and gap storage for the Matrix event cache, written once for both backends.

Durable gap-state invariants:

1. Gap markers are monotonic: ``mark_thread_gap_locked`` never lets an older marker overwrite a
   newer one. There is no reason precedence, because every reason means the same thing — refetch.

2. ``mark_room_gap_locked`` is the room-scoped (wildcard-thread) form. It fans the marker out
   across the room's thread-state rows *and* records it once on the room-state row. The fan-out
   cannot reach a thread whose first fetch is still in flight, because that thread has no row yet;
   the room-level copy is what the replacement then consults.

3. A replacement clears the marker only when the marker predates the fetch that produced it.
   A gap detected mid-fetch is not covered by that fetch, so it survives and the next read refetches.

4. Thread snapshot rows and the lookup, edit, and thread index rows are written and deleted together
   so point lookups can never resurrect rows the snapshot no longer contains.

5. One threaded mutation is one transaction: ``apply_thread_mutation_append_locked`` appends and,
   when the append cannot land, records the gap marker in the same transaction. Marking and
   appending separately left a thread readable while it was missing the event, and a crash between
   them left a half-applied snapshot unmarked.

Everything here is backend-neutral. The statements come from ``event_cache_thread_statements``,
rendered through the dialect; the handful of operations that cannot be expressed as one shared
statement — the write-sequence strategy and bulk delete by event ID — sit behind ``ThreadBackend``.

What deliberately does *not* live here, because it is genuine backend semantics rather than
duplication: the PostgreSQL advisory lock, SQLite's ``BEGIN IMMEDIATE`` locking model and its
``disabled_result`` reader outcome, and the two schema-migration paths. Callers own those.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeVar

from .event_cache_events import (
    event_id_for_cache,
    serialize_cacheable_events,
    serialize_cached_event,
)
from .event_normalization import normalize_event_source_for_cache
from .thread_cache_state import (
    ThreadAppendOutcome,
    ThreadCacheGap,
    thread_cache_gap_row,
)

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence

    from .event_cache_events import SerializedCachedEvent
    from .event_cache_thread_statements import ThreadStatements

_ConnectionT = TypeVar("_ConnectionT")


class ThreadBackend(Protocol[_ConnectionT]):
    """One backend's connection plumbing and event-row helpers.

    The scope key is positional throughout: the two backends name the same column differently
    (``principal_id`` and ``namespace``), and threading that naming difference through every shared
    signature is what kept the two modules from being one.
    """

    @property
    def statements(self) -> ThreadStatements:
        """Return the statements rendered for this backend."""
        ...

    async def execute(
        self,
        db: _ConnectionT,
        sql: str,
        params: Mapping[str, object],
    ) -> None:
        """Run one statement that returns no rows."""
        ...

    async def fetchone(
        self,
        db: _ConnectionT,
        sql: str,
        params: Mapping[str, object],
    ) -> tuple[Any, ...] | None:
        """Run one query and return its first row, if any."""
        ...

    async def fetchall(
        self,
        db: _ConnectionT,
        sql: str,
        params: Mapping[str, object],
    ) -> list[tuple[Any, ...]]:
        """Run one query and return every row."""
        ...

    async def upsert_thread_membership_rows(
        self,
        db: _ConnectionT,
        rows: Sequence[Mapping[str, object]],
    ) -> None:
        """Write thread-membership rows, supplying each row's write sequence.

        The write-sequence strategy is the backend's own: SQLite hands out values from a counter
        row because it has no sequence object, while PostgreSQL draws from ``nextval``.
        """
        ...

    async def delete_thread_membership_by_event_ids(
        self,
        db: _ConnectionT,
        scope: str,
        room_id: str,
        event_ids: Sequence[str],
    ) -> None:
        """Delete thread-membership rows for an explicit list of event IDs."""
        ...

    async def filter_cacheable_events(
        self,
        db: _ConnectionT,
        scope: str,
        room_id: str,
        candidates: list[tuple[str, dict[str, Any]]],
    ) -> list[tuple[str, dict[str, Any]]]:
        """Drop candidates this cache must refuse to store."""
        ...

    async def write_lookup_index_rows(
        self,
        db: _ConnectionT,
        scope: str,
        room_id: str,
        *,
        serialized_events: list[SerializedCachedEvent],
        cached_at: float,
        thread_id: str | None = None,
    ) -> None:
        """Persist point-lookup, edit-index, and thread-index rows for cached events."""
        ...

    async def delete_cached_events(
        self,
        db: _ConnectionT,
        scope: str,
        room_id: str,
        event_ids: Sequence[str],
    ) -> None:
        """Delete point-lookup payload rows for these event IDs."""
        ...

    async def delete_event_edit_rows(
        self,
        db: _ConnectionT,
        scope: str,
        room_id: str,
        *,
        event_ids: Sequence[str],
        original_event_id: str | None,
    ) -> None:
        """Delete edit-index rows for these event IDs."""
        ...

    async def delete_event_thread_rows(
        self,
        db: _ConnectionT,
        scope: str,
        room_id: str,
        *,
        event_ids: Sequence[str],
        current_self_root_ids: Collection[str] = (),
    ) -> None:
        """Delete thread-index rows for these event IDs."""
        ...

    async def event_or_original_is_redacted(
        self,
        db: _ConnectionT,
        scope: str,
        room_id: str,
        *,
        event_id: str,
        event: dict[str, Any],
    ) -> bool:
        """Return whether this event, or the event it edits, is redacted."""
        ...


def _scoped(scope: str, room_id: str, **extra: object) -> dict[str, object]:
    """Return the bind parameters every scoped statement starts from."""
    return {"scope": scope, "room_id": room_id, **extra}


# ``load_thread_events`` returns the edits that survive a collapsed read: one per message, from the
# right sender.
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
# orders by ``event_id.cmp()``, bytewise, so the binary collation pinned in the dialect has to hold
# for this read to agree with the homeserver as well as with SQLite and the fold.
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
# of the author's own and pin the message at its pre-edit body. Membership is joined in there rather
# than filtered later, because ranking over edits the outer query will discard lets an
# out-of-thread edit suppress the in-thread runner-up.
#
# The sender comparison there is an optimization, not the security boundary. The fold re-checks
# every candidate against the JSON sender (``ThreadEditCandidates.winner_for``), so if this
# filter ever admits a foreign replacement the fold still finds nothing in the author's bucket
# and renders the pre-edit body - wrong, but not the attacker's text. Doing it in SQL keeps a
# foreign edit from being ranked as the survivor and hiding the author's own.
#
# The original is LEFT joined, not required. An edit can outlive the message it replaces -
# the edit index holds no foreign key to the payload table - and the fold synthesizes a message from
# such an edit rather than dropping it, carrying the editor's own sender because an original nobody
# has seen cannot be impersonated. Requiring the original would delete those messages from the read
# outright. The sender filter is skipped exactly when there is no original to compare against,
# which is also when ``winner_for`` stops applying it, for the same reason.
#
# The original is read out of the payload table alone, with no thread membership required. Two
# narrower lookups were tried first and both silently disabled the filter. Scoping it to this thread
# made an original cached in a sibling thread read as absent. Routing it through thread membership
# at all then did the same to any original cached by a point lookup, because ``store_event`` writes
# the payload with no membership row - so ``original_events`` came back NULL, the sender filter was
# skipped, and the newest edit across all senders won, which is the exact suppression this filter
# exists to prevent (``test_a_point_cached_original_still_scopes_edits_to_its_sender``). The
# comparison needs the payload and nothing else, so asking for more can only lose a sender it could
# have compared against.
#
# ROW_NUMBER over one pass rather than a correlated NOT EXISTS per candidate: 5.3 ms against
# 8.7 ms on a synthetic 2,021-event thread with current table statistics. Policy stays in Python;
# this is only "latest per group", which is what a window function is for. Splitting
# present-original and absent-original edits into two CTEs scans the edit index twice and timed out
# a 2,000-edit PostgreSQL test that one pass completes.
#
# MATERIALIZED is a hint, not a correctness requirement: measured 3.7 ms materialized against
# 4.1 ms inlinable. It is kept only to stop the planner re-deriving the survivors per row.
async def load_thread_events(
    backend: ThreadBackend[_ConnectionT],
    db: _ConnectionT,
    *,
    scope: str,
    room_id: str,
    thread_id: str,
) -> list[dict[str, Any]] | None:
    """Return one thread's cached events oldest first, collapsed to one edit per message."""
    rows = await backend.fetchall(
        db,
        backend.statements.thread_events,
        _scoped(scope, room_id, thread_id=thread_id),
    )
    return [json.loads(row[2]) for row in rows] if rows else None


async def load_thread_event_ids(
    backend: ThreadBackend[_ConnectionT],
    db: _ConnectionT,
    *,
    scope: str,
    room_id: str,
    thread_id: str,
) -> set[str]:
    """Return every raw event ID this thread holds, superseded edits included.

    Membership and visibility are different questions, and collapsing is what made them differ:
    the visible read shows one edit per message, while this returns every row the thread owns. The
    repair bookkeeping this was written for is gone; the surviving caller is the edit-sender rule's
    coverage, which needs the membership set to state its precondition.

    Joined to the payload table rather than reading membership alone: a membership row whose payload
    is gone is not durably present, and reporting it as present would suppress a refill that should
    happen. That join is also what the pre-collapse code did implicitly, since it derived these IDs
    from a read that required the payload.
    """
    rows = await backend.fetchall(
        db,
        backend.statements.thread_event_ids,
        _scoped(scope, room_id, thread_id=thread_id),
    )
    return {str(row[0]) for row in rows}


async def load_recent_room_thread_ids(
    backend: ThreadBackend[_ConnectionT],
    db: _ConnectionT,
    *,
    scope: str,
    room_id: str,
    limit: int,
) -> list[str]:
    """Return thread IDs for one room ordered by the newest locally cached event timestamp."""
    rows = await backend.fetchall(
        db,
        backend.statements.recent_room_thread_ids,
        _scoped(scope, room_id, limit=limit),
    )
    return [str(row[0]) for row in rows]


async def load_thread_cache_gap(
    backend: ThreadBackend[_ConnectionT],
    db: _ConnectionT,
    *,
    scope: str,
    room_id: str,
    thread_id: str,
) -> ThreadCacheGap | None:
    """Return the durable gap marker recorded against one cached thread, if any.

    Room-scoped gaps are fanned out across the room's thread rows when they are marked, so this is
    a single-table read with no room-state join.
    """
    row = await backend.fetchone(
        db,
        backend.statements.thread_cache_gap,
        _scoped(scope, room_id, thread_id=thread_id),
    )
    return thread_cache_gap_row(row)


async def load_room_membership_locked(
    backend: ThreadBackend[_ConnectionT],
    db: _ConnectionT,
    *,
    scope: str,
    room_id: str,
) -> tuple[str, int]:
    """Return the durable membership state and transition epoch for one scope-room."""
    row = await backend.fetchone(
        db,
        backend.statements.room_membership,
        _scoped(scope, room_id),
    )
    return ("joined", 0) if row is None else (str(row[0]), int(row[1]))


async def certify_room_membership_locked(
    backend: ThreadBackend[_ConnectionT],
    db: _ConnectionT,
    *,
    scope: str,
    room_id: str,
) -> int:
    """Create a durable generation row and return its current epoch."""
    await backend.execute(
        db,
        backend.statements.certify_room_membership,
        _scoped(scope, room_id),
    )
    _membership_state, membership_epoch = await load_room_membership_locked(
        backend,
        db,
        scope=scope,
        room_id=room_id,
    )
    return membership_epoch


async def set_room_membership_locked(
    backend: ThreadBackend[_ConnectionT],
    db: _ConnectionT,
    *,
    scope: str,
    room_id: str,
    membership_state: Literal["joined", "departed"],
    reason: str,
) -> None:
    """Advance one durable room-membership transition and gap-mark prior refills."""
    await mark_room_gap_locked(
        backend,
        db,
        scope=scope,
        room_id=room_id,
        reason=reason,
    )
    # 🔒 ``mark_room_gap_locked`` has already upserted the row, so the epoch below always has
    # something to advance. A missing row and a fresh one both read as ``('joined', 0)``.
    await backend.execute(
        db,
        backend.statements.set_room_membership,
        _scoped(scope, room_id, membership_state=membership_state),
    )


async def _store_thread_events_locked(
    backend: ThreadBackend[_ConnectionT],
    db: _ConnectionT,
    *,
    scope: str,
    room_id: str,
    thread_id: str,
    events: list[dict[str, Any]],
    stored_at: float,
    fetch_started_at: float,
) -> frozenset[str]:
    """Persist one authoritative thread snapshot within an existing DB transaction."""
    normalized_events = [normalize_event_source_for_cache(event) for event in events]
    cacheable_events = await backend.filter_cacheable_events(
        db,
        scope,
        room_id,
        [(event_id_for_cache(event), event) for event in normalized_events],
    )
    serialized_events = serialize_cacheable_events(cacheable_events)
    if serialized_events:
        await backend.write_lookup_index_rows(
            db,
            scope,
            room_id,
            serialized_events=serialized_events,
            cached_at=stored_at,
            thread_id=thread_id,
        )
        await backend.upsert_thread_membership_rows(
            db,
            [
                _scoped(
                    scope,
                    room_id,
                    thread_id=thread_id,
                    event_id=event.event_id,
                    origin_server_ts=event.origin_server_ts,
                )
                for event in serialized_events
            ],
        )
    await _clear_thread_gap_covered_by_fetch(
        backend,
        db,
        scope=scope,
        room_id=room_id,
        thread_id=thread_id,
        fetch_started_at=fetch_started_at,
    )
    return frozenset(event.event_id for event in serialized_events)


async def _thread_snapshot_is_newer_than_fetch(
    backend: ThreadBackend[_ConnectionT],
    db: _ConnectionT,
    *,
    scope: str,
    room_id: str,
    thread_id: str,
    fetch_started_at: float,
) -> bool:
    """Return whether an installed snapshot came from a strictly newer fetch than this one."""
    row = await backend.fetchone(
        db,
        backend.statements.snapshot_fetch_started_at,
        _scoped(scope, room_id, thread_id=thread_id),
    )
    if row is None or row[0] is None:
        return False
    return float(row[0]) > fetch_started_at


async def _uncovered_room_gap(
    backend: ThreadBackend[_ConnectionT],
    db: _ConnectionT,
    *,
    scope: str,
    room_id: str,
    fetch_started_at: float,
) -> tuple[float, str | None] | None:
    """Return the room-scoped gap one fetch does not cover, if there is one."""
    row = await backend.fetchone(db, backend.statements.room_gap, _scoped(scope, room_id))
    if row is None or row[0] is None or float(row[0]) <= fetch_started_at:
        return None
    return (float(row[0]), row[1] if isinstance(row[1], str) else None)


async def _clear_thread_gap_covered_by_fetch(
    backend: ThreadBackend[_ConnectionT],
    db: _ConnectionT,
    *,
    scope: str,
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
        backend,
        db,
        scope=scope,
        room_id=room_id,
        fetch_started_at=fetch_started_at,
    )
    room_gap_marked_at, room_gap_reason = room_gap if room_gap is not None else (None, None)
    await backend.execute(
        db,
        backend.statements.record_snapshot_keeping_uncovered_gap,
        _scoped(
            scope,
            room_id,
            thread_id=thread_id,
            room_gap_marked_at=room_gap_marked_at,
            room_gap_reason=room_gap_reason,
            fetch_started_at=fetch_started_at,
        ),
    )


async def replace_thread_locked(
    backend: ThreadBackend[_ConnectionT],
    db: _ConnectionT,
    *,
    scope: str,
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
        backend,
        db,
        scope=scope,
        room_id=room_id,
        thread_id=thread_id,
        fetch_started_at=fetch_started_at,
    ):
        return
    existing_event_ids = await _thread_event_ids_for_thread(
        backend,
        db,
        scope=scope,
        room_id=room_id,
        thread_id=thread_id,
    )
    replacement_event_ids = await _store_thread_events_locked(
        backend,
        db,
        scope=scope,
        room_id=room_id,
        thread_id=thread_id,
        events=events,
        stored_at=stored_at,
        fetch_started_at=fetch_started_at,
    )
    removed_event_ids = sorted(set(existing_event_ids) - replacement_event_ids)
    if removed_event_ids:
        await backend.delete_thread_membership_by_event_ids(db, scope, room_id, removed_event_ids)
        await backend.delete_cached_events(db, scope, room_id, removed_event_ids)
        await backend.delete_event_edit_rows(
            db,
            scope,
            room_id,
            event_ids=removed_event_ids,
            original_event_id=None,
        )
        await backend.delete_event_thread_rows(
            db,
            scope,
            room_id,
            event_ids=removed_event_ids,
            current_self_root_ids={thread_id} if thread_id in replacement_event_ids else (),
        )


async def invalidate_thread_locked(
    backend: ThreadBackend[_ConnectionT],
    db: _ConnectionT,
    *,
    scope: str,
    room_id: str,
    thread_id: str,
) -> None:
    """Delete cached events and state for one thread within an existing transaction."""
    event_ids = await _thread_event_ids_for_thread(
        backend,
        db,
        scope=scope,
        room_id=room_id,
        thread_id=thread_id,
    )
    await backend.execute(
        db,
        backend.statements.delete_thread_membership_by_thread,
        _scoped(scope, room_id, thread_id=thread_id),
    )
    if event_ids:
        await _delete_event_rows(backend, db, scope=scope, room_id=room_id, event_ids=event_ids)
    await backend.execute(
        db,
        backend.statements.delete_thread_state_by_thread,
        _scoped(scope, room_id, thread_id=thread_id),
    )


async def invalidate_room_threads_locked(
    backend: ThreadBackend[_ConnectionT],
    db: _ConnectionT,
    *,
    scope: str,
    room_id: str,
) -> None:
    """Delete every cached thread snapshot while preserving durable room membership."""
    event_ids = await _thread_event_ids_for_room(backend, db, scope=scope, room_id=room_id)
    await backend.execute(
        db,
        backend.statements.delete_thread_membership_by_room,
        _scoped(scope, room_id),
    )
    if event_ids:
        await _delete_event_rows(backend, db, scope=scope, room_id=room_id, event_ids=event_ids)
    await backend.execute(
        db,
        backend.statements.delete_thread_state_by_room,
        _scoped(scope, room_id),
    )


async def _delete_event_rows(
    backend: ThreadBackend[_ConnectionT],
    db: _ConnectionT,
    *,
    scope: str,
    room_id: str,
    event_ids: list[str],
) -> None:
    """Delete the payload, edit-index, and thread-index rows for events an invalidation removed."""
    await backend.delete_cached_events(db, scope, room_id, event_ids)
    await backend.delete_event_edit_rows(
        db,
        scope,
        room_id,
        event_ids=event_ids,
        original_event_id=None,
    )
    await backend.delete_event_thread_rows(db, scope, room_id, event_ids=event_ids)


async def mark_thread_gap_locked(
    backend: ThreadBackend[_ConnectionT],
    db: _ConnectionT,
    *,
    scope: str,
    room_id: str,
    thread_id: str,
    reason: str,
    gap_marked_at: float | None = None,
) -> None:
    """Record one durable thread gap marker within an active transaction.

    The marker is monotonic: a later gap never loses to an earlier one. There is no reason
    precedence — every reason means the same thing, that this snapshot must be refetched.

    ``gap_marked_at`` replays a marker a caller already stamped, which is how a pending marker
    buffered while durable writes were unavailable keeps its original time instead of being
    back-dated to the flush. Left unset it stamps now.
    """
    marked_at = time.time() if gap_marked_at is None else gap_marked_at
    await backend.execute(
        db,
        backend.statements.mark_thread_gap,
        _scoped(
            scope,
            room_id,
            thread_id=thread_id,
            gap_marked_at=marked_at,
            gap_reason=reason,
        ),
    )


async def apply_thread_mutation_append_locked(
    backend: ThreadBackend[_ConnectionT],
    db: _ConnectionT,
    *,
    scope: str,
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
        backend,
        db,
        scope=scope,
        room_id=room_id,
        thread_id=thread_id,
        normalized_event=normalized_event,
    )
    if outcome is not ThreadAppendOutcome.APPENDED:
        await mark_thread_gap_locked(
            backend,
            db,
            scope=scope,
            room_id=room_id,
            thread_id=thread_id,
            reason=append_failed_reason,
        )
    return outcome


async def mark_room_gap_locked(
    backend: ThreadBackend[_ConnectionT],
    db: _ConnectionT,
    *,
    scope: str,
    room_id: str,
    reason: str,
    gap_marked_at: float | None = None,
) -> None:
    """Record one room-scoped (wildcard-thread) gap across the room's threads and on the room itself.

    The fan-out reaches every thread that already holds a thread-state row. That is not all of
    them: a thread whose first fetch is still in flight has no row yet, so the fan-out skips it and
    the replacement that lands afterwards would insert a clean row for a snapshot fetched from before
    the gap. The room-level copy is what that replacement consults, so the two together cover the room
    whether or not a thread's row existed when the gap was recorded.
    """
    marked_at = time.time() if gap_marked_at is None else gap_marked_at
    gap_params = _scoped(scope, room_id, gap_marked_at=marked_at, gap_reason=reason)
    await backend.execute(db, backend.statements.fan_out_room_gap, gap_params)
    await backend.execute(db, backend.statements.upsert_room_gap, gap_params)


async def _append_existing_thread_event(
    backend: ThreadBackend[_ConnectionT],
    db: _ConnectionT,
    *,
    scope: str,
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
    if await backend.event_or_original_is_redacted(
        db,
        scope,
        room_id,
        event_id=event_id,
        event=normalized_event,
    ):
        return ThreadAppendOutcome.APPEND_REFUSED

    serialized_event = serialize_cached_event(event_id, normalized_event)
    row = await backend.fetchone(
        db,
        backend.statements.thread_has_readable_event,
        _scoped(scope, room_id, thread_id=thread_id),
    )
    thread_exists = row is not None
    await backend.write_lookup_index_rows(
        db,
        scope,
        room_id,
        serialized_events=[serialized_event],
        cached_at=time.time(),
        thread_id=thread_id,
    )
    if not thread_exists:
        # Only lookup-index rows are recorded: there is no snapshot to extend, so only a full
        # history scan can make this thread readable again.
        return ThreadAppendOutcome.SNAPSHOT_MISSING

    await backend.upsert_thread_membership_rows(
        db,
        [
            _scoped(
                scope,
                room_id,
                thread_id=thread_id,
                event_id=serialized_event.event_id,
                origin_server_ts=serialized_event.origin_server_ts,
            ),
        ],
    )
    await _advance_snapshot_watermark(
        backend,
        db,
        scope=scope,
        room_id=room_id,
        thread_id=thread_id,
        reflected_at=time.time(),
    )
    return ThreadAppendOutcome.APPENDED


async def _advance_snapshot_watermark(
    backend: ThreadBackend[_ConnectionT],
    db: _ConnectionT,
    *,
    scope: str,
    room_id: str,
    thread_id: str,
    reflected_at: float,
) -> None:
    """Record that this snapshot now reflects the thread as of ``reflected_at``.

    An append mutates the snapshot, so a fetch that started before it cannot represent the thread
    any more. Moving the watermark forward makes ``replace_thread_locked`` refuse such a fetch,
    which is what stops a slow scan from deleting a live event that landed while it was running.
    """
    await backend.execute(
        db,
        backend.statements.advance_snapshot_watermark,
        _scoped(scope, room_id, thread_id=thread_id, reflected_at=reflected_at),
    )


async def _thread_event_ids_for_thread(
    backend: ThreadBackend[_ConnectionT],
    db: _ConnectionT,
    *,
    scope: str,
    room_id: str,
    thread_id: str,
) -> list[str]:
    """Return cached event IDs currently stored for one thread."""
    rows = await backend.fetchall(
        db,
        backend.statements.event_ids_for_thread,
        _scoped(scope, room_id, thread_id=thread_id),
    )
    return [str(row[0]) for row in rows]


async def thread_snapshot_exists(
    backend: ThreadBackend[_ConnectionT],
    db: _ConnectionT,
    *,
    scope: str,
    room_id: str,
    thread_id: str,
) -> bool:
    """Return whether one thread has at least one durably present snapshot row.

    Joined to the payload table rather than reading membership alone: a membership row whose payload
    is gone is not durably present, and answering yes for one reports a thread as cached that no read
    can serve, which is how startup prewarm silently skips it.
    """
    row = await backend.fetchone(
        db,
        backend.statements.thread_has_readable_event,
        _scoped(scope, room_id, thread_id=thread_id),
    )
    return row is not None


async def _thread_event_ids_for_room(
    backend: ThreadBackend[_ConnectionT],
    db: _ConnectionT,
    *,
    scope: str,
    room_id: str,
) -> list[str]:
    """Return cached event IDs currently stored for every thread in one room."""
    rows = await backend.fetchall(
        db,
        backend.statements.event_ids_for_room,
        _scoped(scope, room_id),
    )
    return [str(row[0]) for row in rows]
