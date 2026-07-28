"""Backend-neutral durable thread-cache gap state.

A cached thread snapshot is usable when its rows exist and no gap marker outranks the fetch that
installed them. There is no validation timestamp, no reason precedence, and no incremental
revalidation allowlist: a stale or incomplete snapshot is **detected and refetched**, not prevented.

Two rules, and only two:

1. A gap marker makes the snapshot unusable until a full refetch replaces it.
   ``mark_room_threads_gap`` is the room-scoped (wildcard-thread) form. It fans the marker out
   across every thread the room already has a ``thread_state`` row for, *and* records it once on
   the room. The fan-out alone is not the whole room: a thread whose first fetch is still in flight
   has no row to update, and the replacement that lands afterwards would insert a clean one. The
   room-level copy is a watermark that only replacement reads, so reads stay free of the join.

2. A replacement keeps whichever gap its fetch does not cover, at either scope. A marker predating
   the fetch (``gap_marked_at <= fetch_started_at``) describes events the fetch did see, so it is
   cleared; one recorded while the fetch was in flight is not covered by it and survives, and the
   next read refetches.

The wall clock is load-bearing here, and knowingly so: ``fetch_started_at`` is captured before the
homeserver round-trip, so it cannot come from a database sequence without an extra round-trip per
fetch. A backward clock step, or skew between two workers sharing one PostgreSQL namespace, can
therefore let a fetch clear a gap recorded after it began. Ordering by wall clock predates the gap
rework - the trust algebra compared the same ``time.time()`` values - and narrowing it would need a
per-``(principal, room)`` logical clock, which is not this change.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

THREAD_HISTORY_TRUST_METADATA_KEY = "thread_history_trust_version"
# Bumping this empties the durable thread tables on startup. The gap-marker rework changed what a
# stored ``thread_state`` row means, and the cache refills from the homeserver, so old rows go.
THREAD_HISTORY_TRUST_VERSION = "thread_gap_markers_v1"


class ThreadAppendOutcome(StrEnum):
    """Describe what one atomic threaded-mutation append did to a cached thread."""

    APPENDED = "appended"
    # No rows to append into: only a full history scan can make this thread readable again. A
    # refused append records a gap marker instead of extending a snapshot that does not exist.
    SNAPSHOT_MISSING = "snapshot_missing"
    APPEND_REFUSED = "append_refused"
    WRITES_UNAVAILABLE = "writes_unavailable"

    @property
    def wrote_event(self) -> bool:
        """Return whether the mutation landed in the cached snapshot."""
        return self is ThreadAppendOutcome.APPENDED


@dataclass(frozen=True, slots=True)
class ThreadCacheGap:
    """The durable gap marker recorded against one cached thread, if any."""

    gap_marked_at: float
    gap_reason: str | None


# What a backend returns when it cannot answer whether this thread carries a gap - a disabled cache,
# a departed room, or a SQLite reader that lost the database to a writer.
#
# It is a gap, not ``None``, and that is the whole point. "No gap recorded" and "could not find out"
# are opposite answers, and collapsing them into ``None`` makes an unreadable marker mean the
# snapshot is clean. The trust algebra failed closed here - a missing state row rejected the read
# with ``no_cache_state`` - and the gap rework has to keep failing closed or a thread stays readable
# through exactly the contention that was trying to mark it.
#
# ``gap_marked_at`` is 0.0 because this marker is never ordered against a fetch: it is not durable,
# it never reaches ``_clear_thread_gap_covered_by_fetch``, and the only consumer is the read that
# refuses the snapshot on the spot.
CACHE_GAP_UNAVAILABLE = ThreadCacheGap(gap_marked_at=0.0, gap_reason="cache_gap_read_unavailable")


def thread_cache_gap_row(values: Sequence[float | str | None] | None) -> ThreadCacheGap | None:
    """Normalize one backend storage row into a backend-neutral gap marker."""
    if values is None:
        return None
    if len(values) != 2:
        msg = f"Thread cache gap row must contain exactly 2 values, got {len(values)}"
        raise ValueError(msg)
    gap_marked_at = values[0]
    if gap_marked_at is None:
        return None
    return ThreadCacheGap(
        gap_marked_at=float(gap_marked_at),
        gap_reason=values[1] if isinstance(values[1], str) else None,
    )
