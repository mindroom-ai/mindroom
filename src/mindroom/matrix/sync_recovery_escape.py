"""Escape a Classic-sync rebuild that repeats from an unchanging checkpoint.

When nio cannot close a limited-timeline gap it marks the room unrecovered,
certification fails closed, and the Classic cursor rewinds to the last
certified checkpoint. The next attempt then asks for the same gap measured
against a live position that has moved on, so the gap it must close is strictly
larger than the one that just failed. Nothing in that cycle shrinks the gap, so
a room that fails from an unchanging checkpoint keeps failing forever while its
principal falls further behind.

This module decides when to stop rewinding for one room and accept the bounded
history loss instead. It counts failures per room against the checkpoint they
were measured from, so a checkpoint that advances between attempts is forward
progress and starts the count over. A recovery that is merely slow therefore
never gets cut short; only one that cannot converge does.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Retrying from the same checkpoint cannot shrink the gap, so a second failure
# already proves the loop will not converge on its own. The extra attempt buys
# grace for a transient /messages failure (429, 5xx, or a pump timeout), which
# leaves a room unrecovered for reasons a plain retry really can fix, while
# still bounding the stall at three attempts of exponential backoff.
_CLASSIC_SYNC_RECOVERY_STALL_LIMIT = 3


@dataclass(frozen=True)
class SkippedRecoveryGap:
    """One room's unrecoverable gap, given up on so sync can move forward."""

    room_id: str
    skipped_from_token: str | None
    failed_attempts: int


@dataclass
class _RoomStall:
    """How often one room failed to recover from one unchanging checkpoint."""

    checkpoint_token: str | None
    failed_attempts: int = 0


@dataclass
class SyncRecoveryStallTracker:
    """Track per-room recovery failures measured from an unchanging checkpoint.

    One tracker belongs to one principal's sync continuity, so a wedged room on
    one account can never make another account skip history.
    """

    _stalls: dict[str, _RoomStall] = field(default_factory=dict, init=False, repr=False)

    def observe(
        self,
        *,
        unrecovered_room_ids: frozenset[str],
        checkpoint_token: str | None,
    ) -> tuple[SkippedRecoveryGap, ...]:
        """Return the rooms whose gap must be skipped to restore forward progress.

        Call this once per sync response that actually settles recovery, with
        the checkpoint a rejected response would rewind to. Rooms absent from
        ``unrecovered_room_ids`` recovered and lose their recorded stall.
        """
        self._forget_recovered(unrecovered_room_ids)
        return tuple(
            skipped
            for room_id in sorted(unrecovered_room_ids)
            if (skipped := self._record_failure(room_id, checkpoint_token)) is not None
        )

    def _forget_recovered(self, unrecovered_room_ids: frozenset[str]) -> None:
        """Drop stalls for rooms this response proved recovered."""
        for room_id in tuple(self._stalls):
            if room_id not in unrecovered_room_ids:
                del self._stalls[room_id]

    def _record_failure(self, room_id: str, checkpoint_token: str | None) -> SkippedRecoveryGap | None:
        """Count one failure and retain skip eligibility until progress resolves it."""
        stall = self._stalls.get(room_id)
        if stall is None or stall.checkpoint_token != checkpoint_token:
            # The checkpoint moved, so the previous failures were measured
            # against a position this room has since advanced past.
            stall = _RoomStall(checkpoint_token=checkpoint_token)
            self._stalls[room_id] = stall
        stall.failed_attempts = min(
            stall.failed_attempts + 1,
            _CLASSIC_SYNC_RECOVERY_STALL_LIMIT,
        )
        if stall.failed_attempts < _CLASSIC_SYNC_RECOVERY_STALL_LIMIT:
            return None
        return SkippedRecoveryGap(
            room_id=room_id,
            skipped_from_token=checkpoint_token,
            failed_attempts=stall.failed_attempts,
        )
