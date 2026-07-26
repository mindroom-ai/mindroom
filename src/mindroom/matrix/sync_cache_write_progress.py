"""Publish the durable cache write that one sync response is waiting on.

The Matrix sync loop is sequential: the next ``/sync`` is only issued once the
response callback for the previous one returns, and that callback awaits the
durable event-cache writes for its response so a sync token is certified only
after its writes landed. A large write set can therefore hold the sync loop for
minutes while it is genuinely draining, and no sync traffic can refresh the sync
watchdog clock in the meantime.

This separates two questions the watchdog used to answer with one clock. The
clock is armed when a response comes off the wire, so it measures the transport.
Its going stale measures something else: that the loop is quiet. During a
certification write the loop is quiet *by construction* - no request is
outstanding, because the app has not returned control to the transport yet.
Nothing observable about the transport can distinguish healthy from wedged in
that window, so the only meaningful liveness question is whether certification
is progressing, and that answer has to come from the cache layer. This module is
where it comes from.

Cancelling such a write is strictly worse than waiting for it: the write set
fails with ``CancelledError``, cache certification fails, and the durable sync
token is dropped, which turns one slow sync into a cold sync over every room.
The watchdog therefore consults the in-flight write published here, and waits
for it, but only for a bounded grace window so a wedged write is still caught.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = [
    "MATRIX_SYNC_CACHE_WRITE_GRACE_SECONDS",
    "SyncCacheWriteProgress",
    "SyncCacheWriteTracker",
    "SyncCacheWriteWatchdogVerdict",
    "sync_cache_write_watchdog_verdict",
]

# How long the sync watchdog may keep waiting on one in-flight durable cache
# write. This is a hang backstop, not a timeout for normal work: while a write is
# in flight the steady-state watchdog timeout does not apply to it at all, so the
# grace only has to sit outside the tail of the cache-write wait distribution.
# Measured waits queue behind the event-cache write coordinator's room and thread
# barriers and run to a p90 of two to three minutes, with the worst observed
# waits around four; 600s leaves room above that while still bounding how long a
# write that never completes can hold the receive loop. Deployments whose cache
# writes are slower should raise this above their own p99 rather than let the
# backstop fire on healthy work.
MATRIX_SYNC_CACHE_WRITE_GRACE_SECONDS = 600.0

SyncCacheWriteWatchdogVerdict = Literal[
    "no_cache_write_in_flight",
    "cache_write_in_flight",
    "cache_write_grace_exhausted",
]


@dataclass(frozen=True, slots=True)
class SyncCacheWriteProgress:
    """One durable cache write the current sync response is still waiting on."""

    started_monotonic: float

    def seconds_in_flight(self, now_monotonic: float) -> float:
        """Return how long this cache write has been running."""
        return max(0.0, now_monotonic - self.started_monotonic)


@dataclass(slots=True)
class SyncCacheWriteTracker:
    """Publish whether a sync response is waiting on its durable cache write.

    Only the sync response callback tracks writes, and the sequential sync loop
    runs one of those at a time, so a single in-flight write is the whole state.
    """

    _started_monotonic: float | None = field(default=None, init=False)

    @contextmanager
    def track(self) -> Iterator[None]:
        """Publish an in-flight durable cache write for the duration of the block."""
        self._started_monotonic = time.monotonic()
        try:
            yield
        finally:
            self._started_monotonic = None

    def snapshot(self) -> SyncCacheWriteProgress | None:
        """Return the in-flight durable cache write, or ``None`` when there is none."""
        if self._started_monotonic is None:
            return None
        return SyncCacheWriteProgress(started_monotonic=self._started_monotonic)


def sync_cache_write_watchdog_verdict(
    progress: SyncCacheWriteProgress | None,
    *,
    now_monotonic: float,
    grace_seconds: float,
) -> SyncCacheWriteWatchdogVerdict:
    """Return how the sync watchdog must treat a sync clock that has gone stale."""
    if progress is None:
        return "no_cache_write_in_flight"
    if progress.seconds_in_flight(now_monotonic) > grace_seconds:
        return "cache_write_grace_exhausted"
    return "cache_write_in_flight"
