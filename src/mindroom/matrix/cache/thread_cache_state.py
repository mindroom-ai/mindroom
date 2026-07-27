"""Backend-neutral durable thread-cache gap state.

A cached thread snapshot is usable when its rows exist and no gap marker outranks the fetch that
installed them. There is no validation timestamp, no reason precedence, and no incremental
revalidation allowlist: a stale or incomplete snapshot is **detected and refetched**, not prevented.

Two rules, and only two:

1. A gap marker makes the snapshot unusable until a full refetch replaces it.
   ``mark_room_threads_stale`` is the room-scoped (wildcard-thread) form and fans the marker out
   across every thread the room has a snapshot for; a thread with no snapshot needs no marker
   because a read that finds no rows refetches anyway.

2. A replacement clears the marker only when the marker predates the fetch that produced the
   replacement (``gap_marked_at <= fetch_started_at``). A gap detected while the fetch was in
   flight is not covered by that fetch, so it survives and the next read refetches.
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


def replacement_clears_gap(gap: ThreadCacheGap | None, *, fetch_started_at: float) -> bool:
    """Return whether one replacement's fetch covers the recorded gap.

    A gap marked while the fetch was in flight describes events the fetch could not have seen, so
    the replacement leaves it in place and the next read refetches.
    """
    return gap is None or gap.gap_marked_at <= fetch_started_at
