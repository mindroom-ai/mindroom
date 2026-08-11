"""Deciding when a room's derived state must stop being trusted.

Rejoining a room can expose a different slice of history than the bot saw
before, so the projection built under the old membership has to be dropped
rather than merged with the new view. `fence_departure` owns that invalidation.
This owns the harder half: deciding when to ask for it.

One departure reaches the bot twice -- once locally, the moment the bot leaves,
and again in the sync response reporting the leave. Both describe the same
departure and must fence once. Fencing twice is not merely wasteful: if the bot
rejoined in between, the second fence deletes the conversation it has already
hydrated under the new membership, along with any answer queued for it.

Which of the two got there first, and whether the other one has already been
accounted for, is durable state in the journal rather than state in this
object. It has to be: the debt is only meaningful if it was recorded by the
same transaction that committed the fence it pairs with, it has to survive the
restart that can happen between a local departure and its report, and it has to
be a count rather than a bit, because leave/rejoin/leave owes two reports.

The one thing this object does keep in memory is how much longer each owed
report can still arrive. That is deliberately not durable. It is measured in
sync responses, and a restart begins the count again from the checkpoint the
sync resumes at -- which is exactly the right window again.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from mindroom.logging_config import get_logger

from .models import DepartureOutcome, DepartureSource

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = get_logger(__name__)

# How many sync responses an owed report may fail to appear in before it is
# treated as one that will never arrive.
#
# A report that is owed forever is not the safe direction: it silently absorbs
# the next genuine departure of the same room, and the state the bot built
# under a membership that really did end survives into the next one. So the
# debt has to be retired, and the only honest reason to retire it is proof that
# the report can no longer come -- not that enough time has passed.
#
# The proof: the leave was committed on the server before the debt was
# recorded, and the sync loop has exactly one request in flight at a time. The
# first response processed afterwards may have been generated before the leave
# landed, so it proves nothing. The one after it was requested with a token the
# first response minted, and generated after the leave was already committed,
# so it covers the leave. If neither carried the report, the report was
# collapsed away -- a leave and a rejoin inside one gappy timeline -- and no
# later response will carry it either.
_OWED_REPORT_SYNC_RESPONSES = 2


class MembershipView(Protocol):
    """One room's departure bookkeeping, and nothing else."""

    async def fence_departure(self, room_id: str, *, source: DepartureSource) -> DepartureOutcome:
        """Apply one observation of a departure, invalidating at most once per departure."""
        ...

    async def note_membership_restarted(self, room_id: str) -> None:
        """Record a confirmed join, so the room's next departure fences again."""
        ...

    async def retire_owed_departure_reports(self, room_id: str) -> None:
        """Forget sync reports that can no longer arrive for one room."""
        ...

    async def rooms_owing_departure_reports(self) -> frozenset[str]:
        """Return every room whose local departure is still owed a sync report."""
        ...


@dataclass(slots=True)
class MembershipFence:
    """Advance a room's membership epoch exactly once per departure."""

    store: MembershipView
    # Rooms owing a sync report, and how many more sync responses that report
    # may still appear in. Recovered from the journal on the first sync
    # response, because a restart between a local departure and its report
    # leaves the debt durable and this countdown empty.
    _report_deadlines: dict[str, int] = field(default_factory=dict)
    _recovered_owed_reports: bool = False

    async def fence_local_departure(self, room_id: str) -> None:
        """Fence a room this bot has just left, ahead of the sync that reports it."""
        outcome = await self.store.fence_departure(room_id, source=DepartureSource.LOCAL)
        self._log(room_id, outcome)
        self._track(room_id, outcome)

    async def fence_reported_departures(self, room_ids: Iterable[str]) -> None:
        """Fence departures a sync reported, absorbing the report local ones are owed.

        One entry per departure, not per room. A room that was left, rejoined
        and left again inside one sync interval is two departures, and only the
        first of them is the report the local leave is owed; offered as a set
        it would be one observation, absorbed, and the second departure would
        never invalidate anything.
        """
        reported = tuple(room_ids)
        await self._recover_owed_reports()
        for room_id in reported:
            outcome = await self.store.fence_departure(room_id, source=DepartureSource.REPORTED)
            self._log(room_id, outcome)
            self._track(room_id, outcome)
        await self._expire_unarrived_reports()

    async def note_membership_restarted(self, room_id: str) -> None:
        """Record a confirmed join, so this room's next departure fences again."""
        await self.store.note_membership_restarted(room_id)

    def _track(self, room_id: str, outcome: DepartureOutcome) -> None:
        """Start, keep, or drop the window an owed report may still arrive in."""
        if outcome.owed_reports == 0:
            self._report_deadlines.pop(room_id, None)
        elif outcome.fenced:
            # A newly owed report resets the window: it is the newest departure
            # that needs it, and any older one owed for this room is bounded by
            # the same responses.
            self._report_deadlines[room_id] = _OWED_REPORT_SYNC_RESPONSES
        else:
            self._report_deadlines.setdefault(room_id, _OWED_REPORT_SYNC_RESPONSES)

    async def _recover_owed_reports(self) -> None:
        """Give reports owed by a previous process a window in this one.

        Marked done only once the read came back. A read that failed recovered
        nothing, and treating it as done would leave every debt this process
        inherited with no window to expire in -- durable debt that no longer
        has an in-memory countdown is never retired, and the next genuine
        departure of that room is absorbed by it instead of being fenced.
        """
        if self._recovered_owed_reports:
            return
        owed = await self.store.rooms_owing_departure_reports()
        self._recovered_owed_reports = True
        for room_id in owed:
            self._report_deadlines.setdefault(room_id, _OWED_REPORT_SYNC_RESPONSES)

    async def _expire_unarrived_reports(self) -> None:
        """Spend one sync response of every owed report's window.

        A window is dropped only after the durable retirement it pairs with
        commits. Dropping it first would leave the debt in the journal with
        nothing left to retire it, which is the same permanent absorption a
        failed recovery causes; keeping the expired window means the next sync
        response simply asks again.
        """
        expired = []
        for room_id, remaining in tuple(self._report_deadlines.items()):
            if remaining > 1:
                self._report_deadlines[room_id] = remaining - 1
                continue
            expired.append(room_id)
        for room_id in expired:
            await self.store.retire_owed_departure_reports(room_id)
            self._report_deadlines.pop(room_id, None)
            logger.info("journal_membership_report_retired", room_id=room_id)

    def _log(self, room_id: str, outcome: DepartureOutcome) -> None:
        if outcome.fenced:
            logger.info(
                "journal_membership_fenced",
                room_id=room_id,
                membership_epoch=outcome.membership_epoch,
            )
