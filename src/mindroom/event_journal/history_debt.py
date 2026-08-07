"""History a room owes because sync gave up on rebuilding one of its gaps.

Sync certification is all-or-nothing per response: a room whose limited
timeline nio cannot close blocks the checkpoint for every other room, and the
cursor rewinds to the position the failure was measured from. Retrying from an
unchanging checkpoint asks for a strictly larger gap each time, so a room that
stops converging freezes the whole principal behind it.

The escape from that used to be to certify past the gap and log the loss. This
is the other half of that trade, and it is what makes it honest: the gap is
written down before the checkpoint moves, so liveness costs a deferred read
rather than a deleted conversation.

A debt is one timestamp, not a token range. Sync tokens are opaque and cannot
be compared, but the projection's newest message at the moment of the skip is a
real lower bound: everything the room received after it may be missing, so a
later backwards walk that reaches that timestamp has provably covered the whole
gap. That is what discharges the debt -- coverage, not an attempt.

A walk that finishes without reaching it is the one case where accepting loss
is honest, and it says so: ``history_lost`` is set, the debt stops gating
reads, and every completeness question about that room answers no from then on.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .backend import Transaction


@dataclass(frozen=True, slots=True)
class RoomHistoryDebt:
    """One room's outstanding history, and how far back a walk must reach."""

    room_id: str
    owed_through_ts: int


class HistoryDebtOutcome(StrEnum):
    """What one finished history walk did to a room's outstanding debt."""

    # The walk reached the timestamp the debt named, so the gap is covered.
    REPAID = "repaid"
    # The walk finished without reaching it. The history is gone as far as this
    # bot can ever see, and the room is marked as having lost it.
    LOST = "lost"
    # Membership moved while the walk was in flight, so its view belongs to a
    # relationship with the room that has already ended.
    SUPERSEDED = "superseded"


def _newest_projected_ts(transaction: Transaction, principal_id: str, room_id: str) -> int | None:
    """Return the newest message this room's projection holds, if it holds one."""
    row = transaction.fetchone(
        "SELECT MAX(created_ts) AS newest FROM visible_messages WHERE principal_id = ? AND room_id = ?",
        (principal_id, room_id),
    )
    newest = None if row is None else row["newest"]
    return None if newest is None else int(newest)


def record(transaction: Transaction, principal_id: str, room_id: str) -> RoomHistoryDebt | None:
    """Record that a skipped sync gap left this room owing history.

    Nothing is recorded for a room whose projection is empty, and that is not a
    shortcut. A debt exists to name a hole between what is already stored and
    what arrives after the skip; with nothing stored there is no hole, only a
    conversation that starts later, which is what every unread room looks like
    and what a first hydration walk fills in anyway.

    A room that is already indebted keeps the older timestamp. The two gaps are
    one hole from a reader's point of view, and reaching only the newer of them
    would leave the older one open while reporting the debt settled.
    """
    anchor = _newest_projected_ts(transaction, principal_id, room_id)
    if anchor is None:
        return None
    outstanding_debt = outstanding(transaction, principal_id, room_id)
    owed = anchor if outstanding_debt is None else min(outstanding_debt.owed_through_ts, anchor)
    transaction.execute(
        """
        INSERT INTO room_history_debt (principal_id, room_id, owed_through_ts)
        VALUES (?, ?, ?)
        ON CONFLICT (principal_id, room_id) DO UPDATE SET owed_through_ts = excluded.owed_through_ts
        """,
        (principal_id, room_id, owed),
    )
    return RoomHistoryDebt(room_id=room_id, owed_through_ts=owed)


def outstanding(transaction: Transaction, principal_id: str, room_id: str) -> RoomHistoryDebt | None:
    """Return the history this room still owes a walk, or nothing."""
    row = transaction.fetchone(
        "SELECT owed_through_ts FROM room_history_debt WHERE principal_id = ? AND room_id = ?",
        (principal_id, room_id),
    )
    if row is None or row["owed_through_ts"] is None:
        return None
    return RoomHistoryDebt(room_id=room_id, owed_through_ts=int(row["owed_through_ts"]))


def settle(
    transaction: Transaction,
    principal_id: str,
    debt: RoomHistoryDebt,
    *,
    reached_ts: int | None,
) -> HistoryDebtOutcome:
    """Settle one room's debt against the walk that just finished.

    ``reached_ts`` is the oldest event the walk saw, counted over everything it
    fetched rather than everything it kept. A page of redactions and state
    events carries the walk just as far back as a page of messages, and judging
    coverage by what survived projection would report a walk as short when it
    was merely uneventful.

    Whether the walk ran out of room or out of allowance does not enter into
    it, and that is deliberate, but it only holds because the walk is bounded
    for this job rather than for a prompt. Both of its remaining stopping
    reasons are real loss. A server that has purged the history a debt names
    runs a walk to the very beginning without ever reaching the timestamp, and
    treating "the server had no more" as repayment would file that as success.
    A gap deeper than the walk's cost ceiling is the other, and it is loss for a
    different reason: a later walk starts from a tip that has only moved
    forward, so the same allowance carries it less far back, not further. There
    is no answer to wait for. Only reaching the timestamp is coverage.

    That is also what keeps a debt from becoming an unbounded read tax. An
    outstanding debt withholds the room's hydration marker, so leaving one open
    would re-walk the room on every read forever -- for a strictly worse answer
    each time. Settling happens exactly once per walk in either direction, so a
    room can never be left owing history no later read will look for. Loss is
    sticky: a hole nothing can fill does not stop existing because a later,
    shallower debt was repaid over the top of it.
    """
    covered = reached_ts is not None and reached_ts <= debt.owed_through_ts
    if covered:
        transaction.execute(
            "UPDATE room_history_debt SET owed_through_ts = NULL WHERE principal_id = ? AND room_id = ?",
            (principal_id, debt.room_id),
        )
        return HistoryDebtOutcome.REPAID
    transaction.execute(
        """
        UPDATE room_history_debt
        SET owed_through_ts = NULL, history_lost = 1
        WHERE principal_id = ? AND room_id = ?
        """,
        (principal_id, debt.room_id),
    )
    return HistoryDebtOutcome.LOST
