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

A walk that reaches the beginning of what the server still holds without
reaching that timestamp is the one case where accepting loss is honest, and it
says so: ``history_lost`` is set and every completeness question about the room
answers no from then on. A walk that merely spent its cost ceiling clears the
debt without that flag -- the room is incomplete, which its hydration row
already records, rather than incompletable.
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
    # The walk spent its cost ceiling without reaching the anchor. The history
    # is out of reach for now and is not declared gone: superseded edits are
    # collapsed out of pagination over time, so the same allowance can carry a
    # later walk further back than it reaches today.
    TRUNCATED = "truncated"


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

    A room whose projection holds no visible message gets no debt, because a
    debt is a timestamp and there is no stored message to take one from. What it
    does not get either is the benefit of the doubt: an empty projection is not
    evidence that the room has nothing to miss, and the justification that a
    first hydration walk fills such a room in anyway is true only of a room
    nobody has read yet.

    The ordinary case is the other one. A room the homeserver holds real history
    for can project nothing at all -- undecryptable events, redactions,
    reactions, state -- and the walk that found only those is complete over zero
    visible messages. Leaving that marker in place while the checkpoint moves
    past the gap is what makes the next strict read answer from a conversation
    missing everything sent during the skip, without asking the server anything.

    So the room's hydration markers are dropped instead, in the transaction that
    already orders this write ahead of the checkpoint. No anchor is invented and
    the debt semantics are untouched; the cost is one re-walk of a room that owes
    nothing, rather than a hole certified as a whole conversation.

    A room that is already indebted keeps the older timestamp. The two gaps are
    one hole from a reader's point of view, and reaching only the newer of them
    would leave the older one open while reporting the debt settled.
    """
    anchor = _newest_projected_ts(transaction, principal_id, room_id)
    if anchor is None:
        transaction.execute(
            "DELETE FROM conversation_hydration WHERE principal_id = ? AND room_id = ?",
            (principal_id, room_id),
        )
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
    walk_exhausted_server: bool,
) -> HistoryDebtOutcome:
    """Settle one room's debt against the walk that just finished.

    ``reached_ts`` is the oldest event the walk saw, counted over everything it
    fetched rather than everything it kept. A page of redactions and state
    events carries the walk just as far back as a page of messages, and judging
    coverage by what survived projection would report a walk as short when it
    was merely uneventful.

    Why the walk stopped decides what an uncovered gap means, and the two
    reasons are not equivalent. ``walk_exhausted_server`` says the walk ran to
    the beginning of what the homeserver still holds without reaching the
    timestamp: the history is genuinely gone, and treating "the server had no
    more" as repayment would file that as success.

    Spending the cost ceiling is a different statement, and an earlier version
    of this conflated them. The argument for calling it permanent was that a
    later walk starts from a tip that has only moved forward -- true of the tip,
    but the quantity that matters is the interval between the tip and the
    anchor, and that can shrink. The servers MindRoom runs against collapse
    superseded ``m.replace`` events out of pagination, and a streamed answer is
    an original followed by a long tail of them, so the same allowance can carry
    a later walk past an anchor it fell short of today. Only reaching the
    timestamp is coverage, but only exhaustion is loss.

    That is also what keeps a debt from becoming an unbounded read tax. An
    outstanding debt withholds the room's hydration marker, so leaving one open
    would re-walk the room on every read forever -- for a strictly worse answer
    each time. Settling happens exactly once per walk in either direction, so a
    room can never be left owing history no later read will look for. Loss is
    sticky: a hole nothing can fill does not stop existing because a later,
    shallower debt was repaid over the top of it.
    """
    covered = reached_ts is not None and reached_ts <= debt.owed_through_ts
    if not covered and not walk_exhausted_server:
        # The walk ran out of allowance, not out of history. Calling that
        # permanent loss assumed the interval between the tip and the anchor can
        # only grow -- true of the tip, but not of the interval: the servers
        # MindRoom runs against collapse superseded `m.replace` events out of
        # pagination, and a streamed answer is mostly superseded edits. So the
        # same ceiling can carry a later walk past an anchor it missed today.
        #
        # The debt is still cleared, because leaving it outstanding re-walks the
        # ceiling on every read forever for a strictly worse answer each time.
        # What is not written is the sticky flag: this room is incomplete now,
        # which the hydration row already records, rather than incompletable
        # for as long as this membership lasts.
        transaction.execute(
            "UPDATE room_history_debt SET owed_through_ts = NULL WHERE principal_id = ? AND room_id = ?",
            (principal_id, debt.room_id),
        )
        return HistoryDebtOutcome.TRUNCATED
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
