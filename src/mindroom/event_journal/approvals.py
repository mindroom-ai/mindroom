"""The approval cards this bot sent and is still waiting on.

A tool-approval card outlives the process that sent it. The user may click it
after a restart, and the router has to expire the ones nobody answered, so the
card event has to be recoverable from somewhere. Neither of the other owners
here can carry it: ``visible_messages`` models conversation messages, and the
journal clears a settled event's payload on purpose, so by the time anyone
asked the card would be gone.

The table is deliberately narrow, and the narrowness is the point -- this is
the alternative to keeping a general event cache alive for one consumer.

A card is answered the moment the bot commits to a decision, which is before
it can know whether the edit carrying that decision reached the room. The
decision is therefore written down first and the row is dropped only once the
room shows it. A row that survives with a decision on it is not a card anyone
still owes an answer to; it is an answer that may not have been delivered, and
resending the identical edit is what settles it.

The same ordering governs the card's own arrival. A row is claimed before the
card is sent, keyed on a transaction ID this bot chose, because the alternative
-- keying on the event ID the homeserver hands back -- makes a row impossible
until after the card is already clickable. A crash in that window used to leave
a card visible with nothing durable behind it: no startup could expire it and
no click could resolve it. Claiming first turns the dangerous case into a
harmless one, a row for a card that may not exist, and the frozen transaction
ID is what tells the two apart -- presenting it again collapses onto the event
the homeserver already accepted, or creates the card if it never landed.

That last step has a boundary, and the row records where it ends. A transaction
ID is idempotent only within the device that used it, so the marker for "a send
was reached, from this device" is written separately from the claim and only by
the path about to send. An unattempted row is proof the room holds nothing and
can simply be dropped; an attempted one whose device cannot be matched has to be
reconciled against the room, because presenting it again would ask a human the
same question twice.

Only cards this bot authored are ever stored, because only those are ever
recovered; a card another sender wrote is not this bot's to resolve.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal, cast

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .backend import Row, Transaction

_DEFAULT_ROOM_CARD_LIMIT = 256
_CARD_COLUMNS = """
    cards.card_json AS card_json, cards.resolution_json AS resolution_json,
    cards.transaction_id AS transaction_id, cards.card_event_id AS card_event_id,
    cards.attempted AS attempted, cards.sending_device_id AS sending_device_id,
    cards.created_at_ns AS created_at_ns
"""

type ApprovalContinuationDecision = Literal["approved", "denied", "expired"]


class ApprovalContinuationState(StrEnum):
    """Durable execution ownership for one suspended approval."""

    WAITING_FOR_DECISION = "waiting_for_decision"
    READY = "ready"
    CLAIMED = "claimed"
    COMPLETED = "completed"
    TERMINAL_FAILURE = "terminal_failure"


@dataclass(frozen=True, slots=True)
class StoredApprovalContinuation:
    """Everything needed to resume one exact paused Agno tool call."""

    approval_id: str
    card_transaction_id: str
    room_id: str
    thread_id: str | None
    response_event_id: str
    source_event_ids: tuple[str, ...]
    entity_kind: Literal["agent", "team"]
    entity_name: str
    session_id: str
    run_id: str
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    requester_id: str
    execution_identity: dict[str, object]
    expires_at: datetime
    decision: ApprovalContinuationDecision | None = None
    state: ApprovalContinuationState = ApprovalContinuationState.WAITING_FOR_DECISION
    claimant_id: str | None = None
    failure_reason: str | None = None
    created_at_ns: int = 0


def create_continuation(
    transaction: Transaction,
    principal_id: str,
    continuation: StoredApprovalContinuation,
) -> bool:
    """Persist one suspended call without replacing an existing identity."""
    inserted = transaction.fetchone(
        """
        INSERT INTO approval_continuations (
            principal_id, approval_id, card_transaction_id, room_id, thread_id,
            response_event_id, source_event_ids_json, entity_kind, entity_name,
            session_id, run_id, tool_call_id, tool_name, arguments_json,
            requester_id, execution_identity_json, expires_at, decision, state,
            claimant_id, failure_reason, created_at_ns
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, NULL, ?)
        ON CONFLICT (principal_id, approval_id) DO NOTHING
        RETURNING approval_id
        """,
        (
            principal_id,
            continuation.approval_id,
            continuation.card_transaction_id,
            continuation.room_id,
            continuation.thread_id,
            continuation.response_event_id,
            _json(list(continuation.source_event_ids)),
            continuation.entity_kind,
            continuation.entity_name,
            continuation.session_id,
            continuation.run_id,
            continuation.tool_call_id,
            continuation.tool_name,
            _json(continuation.arguments),
            continuation.requester_id,
            _json(continuation.execution_identity),
            continuation.expires_at.isoformat(),
            ApprovalContinuationState.WAITING_FOR_DECISION.value,
            time.time_ns(),
        ),
    )
    return inserted is not None


def resolve_continuation(
    transaction: Transaction,
    principal_id: str,
    approval_id: str,
    decision: ApprovalContinuationDecision,
) -> bool:
    """Commit the first decision for one suspended call."""
    resolved = transaction.fetchone(
        """
        UPDATE approval_continuations SET decision = ?, state = ?
        WHERE principal_id = ? AND approval_id = ? AND state = ? AND decision IS NULL
        RETURNING approval_id
        """,
        (
            decision,
            ApprovalContinuationState.READY.value,
            principal_id,
            approval_id,
            ApprovalContinuationState.WAITING_FOR_DECISION.value,
        ),
    )
    return resolved is not None


def claim_continuation(
    transaction: Transaction,
    principal_id: str,
    approval_id: str,
    claimant_id: str,
) -> bool:
    """Give one ready continuation to exactly one execution owner."""
    claimed = transaction.fetchone(
        """
        UPDATE approval_continuations SET state = ?, claimant_id = ?
        WHERE principal_id = ? AND approval_id = ? AND state = ?
        RETURNING approval_id
        """,
        (
            ApprovalContinuationState.CLAIMED.value,
            claimant_id,
            principal_id,
            approval_id,
            ApprovalContinuationState.READY.value,
        ),
    )
    return claimed is not None


def complete_continuation(
    transaction: Transaction,
    principal_id: str,
    approval_id: str,
    claimant_id: str,
) -> bool:
    """Finish only the continuation owned by the named execution worker."""
    completed = transaction.fetchone(
        """
        UPDATE approval_continuations SET state = ?
        WHERE principal_id = ? AND approval_id = ? AND state = ? AND claimant_id = ?
        RETURNING approval_id
        """,
        (
            ApprovalContinuationState.COMPLETED.value,
            principal_id,
            approval_id,
            ApprovalContinuationState.CLAIMED.value,
            claimant_id,
        ),
    )
    return completed is not None


def fail_continuation(
    transaction: Transaction,
    principal_id: str,
    approval_id: str,
    reason: str,
) -> bool:
    """Settle nonterminal continuation work without making it executable."""
    failed = transaction.fetchone(
        """
        UPDATE approval_continuations SET state = ?, failure_reason = ?
        WHERE principal_id = ? AND approval_id = ? AND state IN (?, ?, ?)
        RETURNING approval_id
        """,
        (
            ApprovalContinuationState.TERMINAL_FAILURE.value,
            reason,
            principal_id,
            approval_id,
            ApprovalContinuationState.WAITING_FOR_DECISION.value,
            ApprovalContinuationState.READY.value,
            ApprovalContinuationState.CLAIMED.value,
        ),
    )
    return failed is not None


def recoverable_continuations(
    transaction: Transaction,
    principal_id: str,
) -> tuple[StoredApprovalContinuation, ...]:
    """Return every nonterminal continuation startup must reconcile."""
    rows = transaction.fetchall(
        """
        SELECT * FROM approval_continuations
        WHERE principal_id = ? AND state IN (?, ?, ?)
        ORDER BY created_at_ns, approval_id/*bytes*/
        """,
        (
            principal_id,
            ApprovalContinuationState.WAITING_FOR_DECISION.value,
            ApprovalContinuationState.READY.value,
            ApprovalContinuationState.CLAIMED.value,
        ),
    )
    return tuple(_continuation(row) for row in rows)


def continuation(
    transaction: Transaction,
    principal_id: str,
    approval_id: str,
) -> StoredApprovalContinuation | None:
    """Return one suspended approval continuation by identity."""
    row = transaction.fetchone(
        "SELECT * FROM approval_continuations WHERE principal_id = ? AND approval_id = ?",
        (principal_id, approval_id),
    )
    return None if row is None else _continuation(row)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _object(stored: str, *, field: str) -> dict[str, Any]:
    value = json.loads(stored)
    if not isinstance(value, dict):
        msg = f"Stored approval continuation {field} is not an object"
        raise TypeError(msg)
    return value


def _string_tuple(stored: str) -> tuple[str, ...]:
    value = json.loads(stored)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        msg = "Stored approval continuation source event IDs are not strings"
        raise TypeError(msg)
    return tuple(value)


def _continuation(row: Row) -> StoredApprovalContinuation:
    entity_kind = str(row["entity_kind"])
    if entity_kind not in ("agent", "team"):
        msg = "Stored approval continuation entity kind is invalid"
        raise TypeError(msg)
    decision = row["decision"]
    if decision not in (None, "approved", "denied", "expired"):
        msg = "Stored approval continuation decision is invalid"
        raise TypeError(msg)
    return StoredApprovalContinuation(
        approval_id=str(row["approval_id"]),
        card_transaction_id=str(row["card_transaction_id"]),
        room_id=str(row["room_id"]),
        thread_id=row["thread_id"],
        response_event_id=str(row["response_event_id"]),
        source_event_ids=_string_tuple(str(row["source_event_ids_json"])),
        entity_kind=cast("Literal['agent', 'team']", entity_kind),
        entity_name=str(row["entity_name"]),
        session_id=str(row["session_id"]),
        run_id=str(row["run_id"]),
        tool_call_id=str(row["tool_call_id"]),
        tool_name=str(row["tool_name"]),
        arguments=_object(str(row["arguments_json"]), field="arguments"),
        requester_id=str(row["requester_id"]),
        execution_identity=cast(
            "dict[str, object]",
            _object(str(row["execution_identity_json"]), field="execution identity"),
        ),
        expires_at=datetime.fromisoformat(str(row["expires_at"])),
        decision=cast("ApprovalContinuationDecision | None", decision),
        state=ApprovalContinuationState(str(row["state"])),
        claimant_id=row["claimant_id"],
        failure_reason=row["failure_reason"],
        created_at_ns=int(row["created_at_ns"]),
    )


@dataclass(frozen=True, slots=True)
class StoredApprovalCard:
    """One recorded card, and the decision it is already carrying if any."""

    card: dict[str, Any]
    # None while the card is genuinely unanswered. Once set, the decision was
    # made and only its delivery is in doubt.
    resolution: dict[str, Any] | None
    # This bot's own name for the card, and the Matrix transaction the send
    # used. Stable across restarts, which is what makes a repeat send converge.
    transaction_id: str
    # None while nothing has come back from the homeserver. The card may still
    # be in the room -- an unacknowledged row says the outcome is unknown, not
    # that the send failed -- so the event ID has to be established before the
    # card can be edited at all.
    card_event_id: str | None
    # Whether the send was ever reached. False is the one state that proves the
    # room holds nothing, which is what lets recovery drop such a row without
    # asking the homeserver about it.
    attempted: bool
    # The device the transaction ID belongs to, recorded with the attempt. Only
    # that device can present it again and get the same event back; None on an
    # attempted row means no device was recorded and none can be proven.
    sending_device_id: str | None
    # When the row was claimed. Half of the room scan's ordering, and therefore
    # half of the cursor a caller resumes that scan from.
    created_at_ns: int


@dataclass(frozen=True, slots=True)
class RecordedApprovalDecision:
    """What the durable row carries after one attempt to record a decision."""

    # The decision the row now holds, which is not always the one just
    # offered: a card already carrying a decision keeps its first one. None
    # when no row exists at all, so nothing durable agrees with any decision
    # and nothing will ever redeliver or expire the card.
    resolution: dict[str, Any] | None
    # Whether this call is what committed the decision it offered. False both
    # when there was no row to write and when the row refused the write.
    recorded: bool


def resolve(
    transaction: Transaction,
    principal_id: str,
    *,
    card_event_id: str,
    resolution: Mapping[str, Any],
) -> RecordedApprovalDecision:
    """Record the decision this bot is about to show, before it shows it.

    Written before the Matrix edit, so a crash between the two leaves an
    answered card rather than a pending one. Startup then redelivers this exact
    decision instead of expiring a card the room may already show as approved.

    The update can match nothing, and the caller cannot act on a decision the
    store did not take, so what the row actually ends up carrying is reported
    rather than assumed.
    """
    offered = dict(resolution)
    row = transaction.fetchone(
        """
        UPDATE approval_cards SET resolution_json = ?
        WHERE principal_id = ? AND card_event_id = ? AND resolution_json IS NULL
        RETURNING card_event_id
        """,
        (
            json.dumps(offered, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
            principal_id,
            card_event_id,
        ),
    )
    if row is not None:
        return RecordedApprovalDecision(resolution=offered, recorded=True)
    # Matching nothing has two causes that are not the same fact. Either no
    # such card was ever stored, or the card already carries a decision that
    # the ``resolution_json IS NULL`` guard exists to protect. Only the second
    # leaves something a later startup will redeliver.
    existing = transaction.fetchone(
        "SELECT resolution_json FROM approval_cards WHERE principal_id = ? AND card_event_id = ?",
        (principal_id, card_event_id),
    )
    stored = None if existing is None else _resolution(existing["resolution_json"])
    return RecordedApprovalDecision(resolution=stored, recorded=False)


def claim(
    transaction: Transaction,
    principal_id: str,
    *,
    room_id: str,
    transaction_id: str,
    card: Mapping[str, Any],
) -> None:
    """Record one card as pending under the current membership, before sending it.

    Committed before any network I/O, so no card can reach the room ahead of
    the row that accounts for it. The body written here is the body a repeat
    send would present, and it stays frozen for exactly as long as a repeat is
    still possible.

    Written unattempted, and no device is recorded, because neither is true
    yet. Claiming says this bot intends to ask; it does not say the ask
    happened, and it certainly does not say which device made it -- a re-login
    between here and the send would make that a lie in the one direction that
    matters, since a device recorded but never used reads as "a repeat from
    this device is safe" for a transaction the homeserver has never seen.
    ``mark_attempted`` records both, once the send is actually about to run.

    Doing nothing on conflict keeps that promise across a retried claim: a row
    whose send may already have been attempted must not have its body replaced
    under a transaction ID the homeserver could be holding, nor be walked back
    to unattempted.
    """
    epoch = transaction.fetchone(
        "SELECT membership_epoch FROM room_membership WHERE principal_id = ? AND room_id = ?",
        (principal_id, room_id),
    )
    transaction.execute(
        """
        INSERT INTO approval_cards (
            principal_id, room_id, transaction_id, attempted, sending_device_id,
            card_json, membership_epoch, created_at_ns
        ) VALUES (?, ?, ?, 0, NULL, ?, ?, ?)
        ON CONFLICT (principal_id, transaction_id) DO NOTHING
        """,
        (
            principal_id,
            room_id,
            transaction_id,
            json.dumps(dict(card), ensure_ascii=True, separators=(",", ":"), sort_keys=True),
            0 if epoch is None else int(epoch["membership_epoch"]),
            time.time_ns(),
        ),
    )


def mark_attempted(
    transaction: Transaction,
    principal_id: str,
    *,
    transaction_id: str,
    sending_device_id: str | None,
) -> bool:
    """Record that this device is about to offer one claimed card, before it does.

    Committed ahead of the send for the reason the claim is: a crash mid-send
    has to leave behind the fact that something may already be in the room
    under this transaction, and which device's namespace it was posted in.
    Written together because they are one fact -- an attempt nobody can
    attribute to a device is an attempt no repeat can be proven safe against,
    and recovery reads it exactly that way.

    Returns whether a row was there to mark. A membership fence can delete the
    row between the claim and here, and a caller that sent anyway would put a
    card in a room that no longer accounts for it.
    """
    marked = transaction.fetchone(
        """
        UPDATE approval_cards SET attempted = 1, sending_device_id = ?
        WHERE principal_id = ? AND transaction_id = ?
        RETURNING transaction_id
        """,
        (sending_device_id, principal_id, transaction_id),
    )
    return marked is not None


def acknowledge(
    transaction: Transaction,
    principal_id: str,
    *,
    transaction_id: str,
    card_event_id: str,
    card: Mapping[str, Any],
) -> None:
    """Record the Matrix event one claimed card became.

    The body is rewritten here and only here. Up to this point it had to stay
    frozen because a repeat send would have presented it again; once the event
    ID is known no repeat can happen, and what the room actually shows is the
    better thing to keep -- the transport may have replaced an oversized
    payload with a sidecar reference, and every later read compares the stored
    card against the room.

    Guarded on the row still being unacknowledged so a second pass cannot move
    a card onto a different event. Two event IDs for one transaction means the
    homeserver did not collapse the repeat, and the first one is the card the
    user is looking at.
    """
    transaction.execute(
        """
        UPDATE approval_cards SET card_event_id = ?, card_json = ?
        WHERE principal_id = ? AND transaction_id = ? AND card_event_id IS NULL
        """,
        (
            card_event_id,
            json.dumps(dict(card), ensure_ascii=True, separators=(",", ":"), sort_keys=True),
            principal_id,
            transaction_id,
        ),
    )


def forget(
    transaction: Transaction,
    principal_id: str,
    *,
    transaction_id: str,
) -> None:
    """Drop one card that has reached a terminal state, sent or not.

    Keyed on the transaction rather than the event, because a card whose send
    definitively failed has no event and still has a row. Dropping by the
    event ID would need a second statement for that case and would silently
    match nothing when handed a card the homeserver never accepted.
    """
    transaction.execute(
        "DELETE FROM approval_cards WHERE principal_id = ? AND transaction_id = ?",
        (principal_id, transaction_id),
    )


def pending_card(
    transaction: Transaction,
    principal_id: str,
    *,
    room_id: str,
    card_event_id: str,
) -> StoredApprovalCard | None:
    """Return one card this bot still owes work on, or nothing if it is fenced."""
    row = transaction.fetchone(
        f"""
        SELECT {_CARD_COLUMNS}
        FROM approval_cards AS cards
        LEFT JOIN room_membership AS membership
          ON membership.principal_id = cards.principal_id
         AND membership.room_id = cards.room_id
        WHERE cards.principal_id = ?
          AND cards.room_id = ?
          AND cards.card_event_id = ?
          AND cards.membership_epoch = COALESCE(membership.membership_epoch, 0)
        """,  # noqa: S608 - a fixed column list, not interpolated input
        (principal_id, room_id, card_event_id),
    )
    return None if row is None else _card(row)


def pending_cards(
    transaction: Transaction,
    principal_id: str,
    *,
    room_id: str,
    limit: int = _DEFAULT_ROOM_CARD_LIMIT,
    after: tuple[int, str] | None = None,
) -> tuple[StoredApprovalCard, ...]:
    """Return one room's unfinished cards, oldest first.

    Includes cards no send has come back from. Those are the ones a crash is
    most likely to have stranded, and leaving them out would restore exactly
    the blind spot claiming before sending exists to close.

    ``after`` resumes past a row already visited, in the same order the scan
    uses. A card whose settlement failed keeps its row on purpose, so without a
    cursor a page of them is re-read forever and every card behind it starves.
    """
    # transaction_id shipped as unpinned TEXT, so the byte-order pin goes on
    # the comparison itself. A server whose collation is not byte order would
    # otherwise order the rows differently from the cursor that walks them, and
    # the scan would skip rows or revisit them.
    cursor_clause = "" if after is None else " AND (cards.created_at_ns, cards.transaction_id/*bytes*/) > (?, ?)"
    cursor_params: tuple[object, ...] = () if after is None else after
    rows = transaction.fetchall(
        f"""
        SELECT {_CARD_COLUMNS}
        FROM approval_cards AS cards
        LEFT JOIN room_membership AS membership
          ON membership.principal_id = cards.principal_id
         AND membership.room_id = cards.room_id
        WHERE cards.principal_id = ?
          AND cards.room_id = ?
          AND cards.membership_epoch = COALESCE(membership.membership_epoch, 0){cursor_clause}
        -- Two cards sent in the same nanosecond would otherwise come back in
        -- whatever order each backend felt like, and the caller expires them
        -- in the order it reads them.
        ORDER BY cards.created_at_ns, cards.transaction_id/*bytes*/
        LIMIT ?
        """,  # noqa: S608 - a fixed column list and a fixed clause, not input
        (principal_id, room_id, *cursor_params, limit),
    )
    return tuple(_card(row) for row in rows)


def _card(row: Row) -> StoredApprovalCard:
    card = json.loads(row["card_json"])
    if not isinstance(card, dict):
        msg = "Stored approval card is not an object"
        raise TypeError(msg)
    return StoredApprovalCard(
        card=card,
        resolution=_resolution(row["resolution_json"]),
        transaction_id=str(row["transaction_id"]),
        card_event_id=row["card_event_id"],
        attempted=bool(row["attempted"]),
        sending_device_id=row["sending_device_id"],
        created_at_ns=int(row["created_at_ns"]),
    )


def _resolution(stored: str | None) -> dict[str, Any] | None:
    if stored is None:
        return None
    resolution = json.loads(stored)
    if not isinstance(resolution, dict):
        msg = "Stored approval resolution is not an object"
        raise TypeError(msg)
    return resolution
