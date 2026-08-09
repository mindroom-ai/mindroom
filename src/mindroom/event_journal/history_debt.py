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

A debt is one event, not a token range. Sync tokens are opaque and cannot be
compared, but the projection's newest message at the moment of the skip is a
real lower bound: everything the room received after it may be missing, so a
later backwards walk that reaches that message has provably covered the whole
gap. That is what discharges the debt -- coverage, not an attempt.

The anchor is an event and not its timestamp, and the difference is the whole
guarantee. ``origin_server_ts`` is the sending server's clock, which does not
have to agree with the order this server paginates in: federated skew and
bridges that rewrite timestamps both put an event older than its neighbours at
the tip of a timeline. Coverage judged as "the oldest timestamp seen anywhere is
old enough" is satisfied by one such event on the first page, and the walk then
stops with the gap still ahead of it and reports a repayment. Reaching the
anchor event is a statement about position, which is the thing being claimed.

A walk that reaches the beginning of what the server still holds without seeing
the anchor is the one case where accepting loss is honest, and it says so:
``history_lost`` is set and every completeness question about the room answers
no from then on. A walk that merely spent its cost ceiling clears the debt
without that flag -- the room is incomplete, which every hydration marker in it
is made to record, rather than incompletable.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .backend import Row, Transaction


@dataclass(frozen=True, slots=True)
class RoomHistoryDebt:
    """One room's outstanding history, and how far back a walk must reach.

    ``owed_through_event_id`` is what a walk has to reach; ``owed_through_ts``
    is that event's timestamp, which orders two anchors against each other and
    tells an operator how far back the hole starts. Only the first is coverage.
    """

    room_id: str
    owed_through_ts: int
    owed_through_event_id: str


class HistoryDebtOutcome(StrEnum):
    """What one finished history walk did to a room's outstanding debt."""

    # The walk reached the event the debt named, so the gap is covered.
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


def _newest_projected_message(
    transaction: Transaction,
    principal_id: str,
    room_id: str,
) -> tuple[int, str] | None:
    """Return the newest message this room's projection holds, if it holds one.

    As a ``(timestamp, event id)`` pair, ordered the way the projection orders
    itself, so the newest message is a single named event rather than whichever
    of several rows happens to share the highest timestamp.
    """
    row = transaction.fetchone(
        """
        SELECT created_ts, logical_event_id FROM visible_messages
        WHERE principal_id = ? AND room_id = ?
        ORDER BY created_ts DESC, logical_event_id DESC
        LIMIT 1
        """,
        (principal_id, room_id),
    )
    return None if row is None else (int(row["created_ts"]), str(row["logical_event_id"]))


def record(transaction: Transaction, principal_id: str, room_id: str) -> RoomHistoryDebt | None:
    """Record that a skipped sync gap left this room owing history.

    A room whose projection holds no visible message gets no debt, because a
    debt is an anchor event and there is no stored message to take one from.
    What it does not get either is the benefit of the doubt: an empty projection
    is not evidence that the room has nothing to miss, and the justification
    that a first hydration walk fills such a room in anyway is true only of a
    room nobody has read yet.

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

    A room that is already indebted keeps the older anchor. The two gaps are one
    hole from a reader's point of view, and reaching only the newer of them
    would leave the older one open while reporting the debt settled. Which
    anchor is older is decided on ``(timestamp, event id)``, the order the
    projection itself is read in, so two anchors sharing a millisecond still
    order.

    What an anchor taken from here cannot yet promise is that it sits on the
    right side of the hole, and that is a separate open defect rather than a
    detail of this one. The transport abandons a room's gap and dispatches that
    room's post-gap live tail in the same sync response, and admission projects
    the tail before certification reaches this function -- so the projection's
    newest message can be a message from *after* the hole, and reaching it
    proves nothing about the middle. Only the boundary the transport measured
    the gap from can prove that, and the sync response does not carry it to this
    layer: it names the rooms it gave up on and nothing else about their gaps.
    Proving coverage against the anchor is still strictly better than proving it
    against a clock, and it is all this layer can prove on its own.
    """
    anchor = _newest_projected_message(transaction, principal_id, room_id)
    if anchor is None:
        transaction.execute(
            "DELETE FROM conversation_hydration WHERE principal_id = ? AND room_id = ?",
            (principal_id, room_id),
        )
        return None
    outstanding_debt = outstanding(transaction, principal_id, room_id)
    if outstanding_debt is not None:
        anchor = min(anchor, (outstanding_debt.owed_through_ts, outstanding_debt.owed_through_event_id))
    owed_ts, owed_event_id = anchor
    transaction.execute(
        """
        INSERT INTO room_history_debt (principal_id, room_id, owed_through_ts, owed_through_event_id)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (principal_id, room_id) DO UPDATE SET
            owed_through_ts = excluded.owed_through_ts,
            owed_through_event_id = excluded.owed_through_event_id
        """,
        (principal_id, room_id, owed_ts, owed_event_id),
    )
    return RoomHistoryDebt(room_id=room_id, owed_through_ts=owed_ts, owed_through_event_id=owed_event_id)


def _debt_from_row(room_id: str, row: Row | None) -> RoomHistoryDebt | None:
    """Return the outstanding debt represented by one storage row."""
    if row is None or row["owed_through_event_id"] is None:
        return None
    return RoomHistoryDebt(
        room_id=room_id,
        owed_through_ts=int(row["owed_through_ts"]),
        owed_through_event_id=str(row["owed_through_event_id"]),
    )


def outstanding(transaction: Transaction, principal_id: str, room_id: str) -> RoomHistoryDebt | None:
    """Return the history this room still owes a walk, or nothing.

    The anchor is what says a debt is outstanding, because it is the only part
    of one a walk can settle. A row carrying a timestamp and no anchor owes
    nothing: it predates anchors, and no walk could ever prove it covered.
    """
    return _debt_from_row(
        room_id,
        transaction.fetchone(
            """
            SELECT owed_through_ts, owed_through_event_id FROM room_history_debt
            WHERE principal_id = ? AND room_id = ?
            """,
            (principal_id, room_id),
        ),
    )


def claim_outstanding(transaction: Transaction, principal_id: str, room_id: str) -> RoomHistoryDebt | None:
    """Lock and return the exact debt that a final recovery may settle.

    A self-update is portable across both backends and prevents another process
    from replacing the row after this comparison but before settlement.
    """
    return _debt_from_row(
        room_id,
        transaction.fetchone(
            """
            UPDATE room_history_debt
            SET owed_through_ts = owed_through_ts
            WHERE principal_id = ? AND room_id = ?
            RETURNING owed_through_ts, owed_through_event_id
            """,
            (principal_id, room_id),
        ),
    )


def settle(
    transaction: Transaction,
    principal_id: str,
    debt: RoomHistoryDebt,
    *,
    saw_anchor: bool,
    walk_exhausted_server: bool,
) -> HistoryDebtOutcome:
    """Settle one room's debt against the walk that just finished.

    ``saw_anchor`` says the anchor event appeared in a chunk the walk fetched,
    counted over everything it fetched rather than everything it kept. A page of
    redactions and state events carries the walk just as far back as a page of
    messages, and a redacted anchor is still the anchor, so judging this by what
    survived projection would report a walk as short when it was merely
    uneventful.

    It is the anchor and not a timestamp because a timestamp is not a position.
    ``origin_server_ts`` comes from the sending server's clock, so a federated
    or bridged event can carry a reading older than everything around it and sit
    at the tip of the timeline; coverage measured as "the oldest reading seen so
    far is old enough" is then satisfied on the first page of a walk that never
    went near the hole.

    Why the walk stopped decides what an uncovered gap means, and the two
    reasons are not equivalent. ``walk_exhausted_server`` says the walk ran to
    the beginning of what the homeserver still holds without seeing the anchor:
    the history is genuinely gone -- or the anchor itself has been purged, which
    is the same statement about what can still be read -- and treating "the
    server had no more" as repayment would file that as success.

    Spending the cost ceiling is a different statement, and an earlier version
    of this conflated them. The argument for calling it permanent was that a
    later walk starts from a tip that has only moved forward -- true of the tip,
    but the quantity that matters is the interval between the tip and the
    anchor, and that can shrink. The servers MindRoom runs against collapse
    superseded ``m.replace`` events out of pagination, and a streamed answer is
    an original followed by a long tail of them, so the same allowance can carry
    a later walk past an anchor it fell short of today. Only reaching the anchor
    is coverage, but only exhaustion is loss.

    That is also what keeps a debt from becoming an unbounded read tax. An
    outstanding debt withholds the room's hydration marker, so leaving one open
    would re-walk the room on every read forever -- for a strictly worse answer
    each time. Settling happens exactly once per walk in either direction, so a
    room can never be left owing history no later read will look for. Loss is
    sticky: a hole nothing can fill does not stop existing because a later,
    shallower debt was repaid over the top of it.
    """
    if not saw_anchor and not walk_exhausted_server:
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
        # which every hydration marker in it is made to record, rather than
        # incompletable for as long as this membership lasts.
        _retract_completeness(transaction, principal_id, debt.room_id)
        _clear(transaction, principal_id, debt.room_id)
        return HistoryDebtOutcome.TRUNCATED
    if saw_anchor:
        _clear(transaction, principal_id, debt.room_id)
        return HistoryDebtOutcome.REPAID
    transaction.execute(
        """
        UPDATE room_history_debt
        SET owed_through_ts = NULL, owed_through_event_id = NULL, history_lost = 1
        WHERE principal_id = ? AND room_id = ?
        """,
        (principal_id, debt.room_id),
    )
    return HistoryDebtOutcome.LOST


def _retract_completeness(transaction: Transaction, principal_id: str, room_id: str) -> None:
    """Withdraw every claim that a conversation in this room is whole.

    Clearing the anchor is what lifts the gate the debt held over the room's
    hydration markers, and every one of them goes back to answering. The
    repayment installed a marker for the room conversation and nothing else, so
    the markers that come back are the ones written before anyone knew about the
    hole -- threads whose walk reached the start of a conversation that has since
    grown a gap in the middle. Left alone they answer `complete` over it, which
    is the one answer the whole mechanism exists to prevent.

    The claim is retracted rather than the marker deleted, and the difference is
    the read tax. Deleting would send the next read of every thread in the room
    back to the server, which is the unbounded re-walk clearing the debt was
    meant to avoid; `complete = 0` leaves the conversation hydrated and merely
    stops it calling itself whole. It also lands on the row the repayment just
    wrote, whose own `complete = False` the monotonic carry-forward in
    `mark_conversation_hydrated` would otherwise discard in favour of the
    pre-gap value -- within one epoch that clause only ever lets completeness
    grow, and this is the one event that has to shrink it.

    Nothing here is sticky. A later walk that genuinely reaches the start of a
    conversation writes `complete = 1` over this and is believed, which is
    exactly what separates a ceiling from lost history.
    """
    transaction.execute(
        "UPDATE conversation_hydration SET complete = 0 WHERE principal_id = ? AND room_id = ?",
        (principal_id, room_id),
    )


def _clear(transaction: Transaction, principal_id: str, room_id: str) -> None:
    """Drop a settled debt's anchor, which is what says the room owes nothing."""
    transaction.execute(
        """
        UPDATE room_history_debt SET owed_through_ts = NULL, owed_through_event_id = NULL
        WHERE principal_id = ? AND room_id = ?
        """,
        (principal_id, room_id),
    )
