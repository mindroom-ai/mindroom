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
from typing import TYPE_CHECKING, Any, Literal, cast

from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .backend import Row, Transaction

_DEFAULT_ROOM_CARD_LIMIT = 256
logger = get_logger(__name__)
_CARD_COLUMNS = """
    cards.card_json AS card_json, cards.resolution_json AS resolution_json,
    cards.transaction_id AS transaction_id, cards.card_event_id AS card_event_id,
    cards.attempted AS attempted, cards.sending_device_id AS sending_device_id,
    cards.created_at_ns AS created_at_ns,
    cards.continuation_id AS continuation_id,
    cards.continuation_generation AS continuation_generation,
    cards.tool_call_id AS tool_call_id
"""
_TIMEOUT_REASON = "Tool approval request timed out."


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
    continuation_id: str
    continuation_generation: int
    tool_call_id: str


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
    continuation_ready: bool = False
    continuation_entity_name: str | None = None
    source_event_ids: tuple[str, ...] = ()


def _native_identity(card: Mapping[str, Any]) -> tuple[str, int, str]:
    """Extract strict native-continuation identity from one Matrix card body."""
    content = card.get("content")
    if not isinstance(content, dict):
        msg = "Approval card is missing native continuation identity."
        raise TypeError(msg)
    continuation_id = content.get("continuation_id")
    generation = content.get("continuation_generation")
    tool_call_id = content.get("tool_call_id")
    if (
        not isinstance(continuation_id, str)
        or not continuation_id
        or not isinstance(generation, int)
        or isinstance(generation, bool)
        or not isinstance(tool_call_id, str)
        or not tool_call_id
    ):
        msg = "Approval card is missing native continuation identity."
        raise ValueError(msg)
    return continuation_id, generation, tool_call_id


def resolve_continuation(
    transaction: Transaction,
    principal_id: str,
    *,
    card_event_id: str,
    requested_status: Literal["approved", "denied", "expired"],
    reason: str | None,
    resolution: Mapping[str, Any],
) -> RecordedApprovalDecision:
    """Commit one current-format card and exact-call decision atomically."""
    card = transaction.fetchone(
        """
        SELECT resolution_json, continuation_id, continuation_generation, tool_call_id
        FROM approval_cards
        WHERE principal_id = ? AND card_event_id = ?
        """,
        (principal_id, card_event_id),
    )
    if card is None:
        return RecordedApprovalDecision(resolution=None, recorded=False)
    existing = _resolution(cast("str | None", card["resolution_json"]))
    continuation_id = cast("str", card["continuation_id"])
    generation_value = card["continuation_generation"]
    tool_call_id = cast("str", card["tool_call_id"])
    if existing is not None:
        return RecordedApprovalDecision(resolution=existing, recorded=False)
    generation = int(generation_value)
    continuation = transaction.fetchone(
        """
        SELECT principal_id, entity_name, state, generation, failure_reason
        FROM approval_continuations WHERE approval_id = ?
        """,
        (continuation_id,),
    )
    if continuation is None or int(continuation["generation"]) != generation:
        return RecordedApprovalDecision(resolution=None, recorded=False)
    continuation_principal_id = str(continuation["principal_id"])
    entity_name = str(continuation["entity_name"])
    call = transaction.fetchone(
        """
        SELECT calls.expires_at_ns
        FROM approval_continuation_calls AS calls
        JOIN approval_continuations AS continuations
          ON continuations.principal_id = calls.principal_id
         AND continuations.approval_id = calls.approval_id
        WHERE calls.principal_id = ? AND calls.approval_id = ?
          AND calls.generation = ? AND calls.tool_call_id = ?
          AND calls.decision IS NULL
          AND continuations.state IN ('waiting', 'failing')
          AND continuations.generation = ?
        """,
        (continuation_principal_id, continuation_id, generation, tool_call_id, generation),
    )
    if call is None:
        return RecordedApprovalDecision(resolution=None, recorded=False)
    failure_reason = cast("str | None", continuation["failure_reason"])
    decision, decision_reason = _effective_continuation_decision(
        requested_status=requested_status,
        requested_reason=reason,
        expired=time.time_ns() >= int(call["expires_at_ns"]),
        failure_reason=(failure_reason or "Tool approval continuation failed safely.")
        if continuation["state"] == "failing"
        else None,
    )
    stored_resolution = _resolved_continuation_content(
        resolution,
        requested_status=requested_status,
        decision=decision,
        reason=decision_reason,
    )
    decided = transaction.fetchone(
        """
        UPDATE approval_continuation_calls
        SET decision = ?, reason = ?
        WHERE principal_id = ? AND approval_id = ? AND generation = ?
          AND tool_call_id = ? AND decision IS NULL
        RETURNING tool_call_id
        """,
        (decision, decision_reason, continuation_principal_id, continuation_id, generation, tool_call_id),
    )
    if decided is None:
        msg = f"Approval call {tool_call_id!r} changed during its exact-call decision"
        raise RuntimeError(msg)
    encoded = json.dumps(stored_resolution, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    recorded = transaction.fetchone(
        """
        UPDATE approval_cards SET resolution_json = ?
        WHERE principal_id = ? AND card_event_id = ? AND resolution_json IS NULL
        RETURNING card_event_id
        """,
        (encoded, principal_id, card_event_id),
    )
    if recorded is None:
        msg = f"Approval card {card_event_id!r} changed during its exact-call decision"
        raise RuntimeError(msg)
    transaction.execute(
        """
        UPDATE approval_continuations SET state = 'ready'
        WHERE principal_id = ? AND approval_id = ? AND generation = ? AND state = 'waiting'
          AND runtime_generation IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM approval_continuation_calls
              WHERE principal_id = ? AND approval_id = ? AND generation = ? AND decision IS NULL
          )
        """,
        (
            continuation_principal_id,
            continuation_id,
            generation,
            continuation_principal_id,
            continuation_id,
            generation,
        ),
    )
    state = transaction.fetchone(
        """
        SELECT state FROM approval_continuations
        WHERE principal_id = ? AND approval_id = ? AND generation = ?
        """,
        (continuation_principal_id, continuation_id, generation),
    )
    source_rows = transaction.fetchall(
        """
        SELECT event_id FROM approval_continuation_sources
        WHERE principal_id = ? AND approval_id = ? ORDER BY source_ordinal
        """,
        (continuation_principal_id, continuation_id),
    )
    return RecordedApprovalDecision(
        resolution=stored_resolution,
        recorded=True,
        continuation_ready=state is not None and state["state"] == "ready",
        continuation_entity_name=entity_name,
        source_event_ids=tuple(str(row["event_id"]) for row in source_rows),
    )


def _effective_continuation_decision(
    *,
    requested_status: Literal["approved", "denied", "expired"],
    requested_reason: str | None,
    expired: bool,
    failure_reason: str | None,
) -> tuple[Literal["approved", "denied", "expired"], str | None]:
    """Apply deadline and failure fences to one requested decision."""
    if requested_status == "expired" or expired:
        return "expired", _TIMEOUT_REASON
    if requested_status == "approved" and failure_reason is not None:
        return "denied", failure_reason
    return requested_status, requested_reason


def _resolved_continuation_content(
    resolution: Mapping[str, Any],
    *,
    requested_status: Literal["approved", "denied", "expired"],
    decision: Literal["approved", "denied", "expired"],
    reason: str | None,
) -> dict[str, Any]:
    """Rewrite visible content when a durable fence overrides an approval."""
    stored = dict(resolution)
    if decision == requested_status:
        return stored
    stored["status"] = decision
    stored["resolution_reason"] = reason
    stored["resolved_by"] = None
    body = stored.get("body")
    requested_prefix = f"{requested_status.title()}:"
    if isinstance(body, str) and body.startswith(requested_prefix):
        stored["body"] = f"{decision.title()}:{body.removeprefix(requested_prefix)}"
    return stored


def expire_cards_for_departed_continuations(
    transaction: Transaction,
    continuation_principal_id: str,
    *,
    room_id: str,
    reason: str,
) -> None:
    """Preserve router-owned Matrix cleanup when a responder leaves the room."""
    rows = transaction.fetchall(
        """
        SELECT cards.principal_id, cards.transaction_id, cards.card_json
        FROM approval_cards AS cards
        JOIN approval_continuations AS continuations
          ON continuations.approval_id = cards.continuation_id
        WHERE continuations.principal_id = ? AND cards.resolution_json IS NULL
          AND EXISTS (
              SELECT 1
              FROM approval_continuation_sources AS sources
              JOIN journal_events AS events
                ON events.principal_id = sources.principal_id
               AND events.event_id = sources.event_id
              WHERE sources.principal_id = continuations.principal_id
                AND sources.approval_id = continuations.approval_id
                AND events.room_id = ?
          )
        """,
        (continuation_principal_id, room_id),
    )
    for row in rows:
        try:
            card = json.loads(str(row["card_json"]))
        except (json.JSONDecodeError, TypeError):
            continue
        content = card.get("content") if isinstance(card, dict) else None
        if not isinstance(content, dict):
            continue
        resolution = {
            **content,
            "status": "expired",
            "approvable": False,
            "resolution_reason": reason,
            "resolved_by": None,
        }
        tool_name = resolution.get("tool_name")
        resolution["body"] = f"Expired: {tool_name}" if isinstance(tool_name, str) else "Approval expired"
        transaction.execute(
            """
            UPDATE approval_cards SET resolution_json = ?
            WHERE principal_id = ? AND transaction_id = ? AND resolution_json IS NULL
            """,
            (
                json.dumps(resolution, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
                str(row["principal_id"]),
                str(row["transaction_id"]),
            ),
        )


def fail_continuations_for_departed_card_owner(
    transaction: Transaction,
    card_principal_id: str,
    *,
    room_id: str,
    reason: str,
) -> None:
    """Wake responder-owned pauses before their departed transport's cards disappear."""
    transaction.execute(
        """
        UPDATE approval_continuations
        SET state = 'failing', failure_reason = ?, runtime_generation = NULL
        WHERE state IN ('waiting', 'ready')
          AND EXISTS (
              SELECT 1 FROM approval_cards AS cards
              WHERE cards.principal_id = ? AND cards.room_id = ?
                AND cards.continuation_id = approval_continuations.approval_id
                AND cards.resolution_json IS NULL
          )
        """,
        (reason, card_principal_id, room_id),
    )


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
    continuation_id, continuation_generation, tool_call_id = _native_identity(card)
    transaction.execute(
        """
        INSERT INTO approval_cards (
            principal_id, room_id, transaction_id, attempted, sending_device_id,
            card_json, continuation_id, continuation_generation, tool_call_id,
            membership_epoch, created_at_ns
        ) VALUES (?, ?, ?, 0, NULL, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (principal_id, transaction_id) DO NOTHING
        """,
        (
            principal_id,
            room_id,
            transaction_id,
            json.dumps(dict(card), ensure_ascii=True, separators=(",", ":"), sort_keys=True),
            continuation_id,
            continuation_generation,
            tool_call_id,
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


def finish(
    transaction: Transaction,
    principal_id: str,
    *,
    transaction_id: str,
    card_event_id: str,
) -> bool:
    """Retire delivered card payload while keeping its shared approval-only identity."""
    remembered = transaction.fetchone(
        """
        INSERT INTO approval_action_tombstones (principal_id, room_id, card_event_id)
        SELECT principal_id, room_id, card_event_id
        FROM approval_cards
        WHERE principal_id = ? AND transaction_id = ? AND card_event_id = ?
        ON CONFLICT (principal_id, card_event_id) DO NOTHING
        RETURNING card_event_id
        """,
        (principal_id, transaction_id, card_event_id),
    )
    existing = transaction.fetchone(
        """
        SELECT 1 AS present FROM approval_action_tombstones
        WHERE principal_id = ? AND card_event_id = ?
        """,
        (principal_id, card_event_id),
    )
    if remembered is None and existing is None:
        return False
    transaction.execute(
        "DELETE FROM approval_cards WHERE principal_id = ? AND transaction_id = ? AND card_event_id = ?",
        (principal_id, transaction_id, card_event_id),
    )
    return True


def is_terminal_card(
    transaction: Transaction,
    principal_id: str,
    *,
    room_id: str,
    card_event_id: str,
) -> bool:
    """Return whether a delivered terminal approval owns this Matrix event."""
    row = transaction.fetchone(
        """
        SELECT 1 AS present FROM approval_action_tombstones
        WHERE principal_id = ? AND room_id = ? AND card_event_id = ?
        """,
        (principal_id, room_id, card_event_id),
    )
    return row is not None


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


def pending_room_ids(transaction: Transaction, principal_id: str) -> tuple[str, ...]:
    """Return every current-membership room with recoverable approval cards."""
    rows = transaction.fetchall(
        """
        SELECT DISTINCT cards.room_id/*bytes*/ AS room_id
        FROM approval_cards AS cards
        LEFT JOIN room_membership AS membership
          ON membership.principal_id = cards.principal_id
         AND membership.room_id = cards.room_id
        WHERE cards.principal_id = ?
          AND cards.membership_epoch = COALESCE(membership.membership_epoch, 0)
        ORDER BY cards.room_id/*bytes*/
        """,
        (principal_id,),
    )
    return tuple(str(row["room_id"]) for row in rows)


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
    cards: list[StoredApprovalCard] = []
    scan_after = after
    while len(cards) < limit:
        cursor_clause = (
            "" if scan_after is None else " AND (cards.created_at_ns, cards.transaction_id/*bytes*/) > (?, ?)"
        )
        cursor_params: tuple[object, ...] = () if scan_after is None else scan_after
        raw_limit = limit - len(cards)
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
            (principal_id, room_id, *cursor_params, raw_limit),
        )
        if not rows:
            break
        last = rows[-1]
        scan_after = (int(last["created_at_ns"]), str(last["transaction_id"]))
        cards.extend(card for row in rows if (card := _card(row)) is not None)
        if len(rows) < raw_limit:
            break
    return tuple(cards)


def _card(row: Row) -> StoredApprovalCard | None:
    """Decode one durable native card, skipping corrupt rows fail-closed."""
    try:
        card = json.loads(row["card_json"])
    except (json.JSONDecodeError, TypeError):
        _log_unreadable_card(row)
        return None
    if not isinstance(card, dict):
        _log_unreadable_card(row)
        return None
    try:
        stored_identity = (
            cast("str", row["continuation_id"]),
            int(row["continuation_generation"]),
            cast("str", row["tool_call_id"]),
        )
        card_identity = _native_identity(card)
        resolution = _resolution(row["resolution_json"])
    except (json.JSONDecodeError, TypeError, ValueError):
        _log_unreadable_card(row)
        return None
    if card_identity != stored_identity:
        _log_unreadable_card(row)
        return None
    return StoredApprovalCard(
        card=card,
        resolution=resolution,
        transaction_id=str(row["transaction_id"]),
        card_event_id=row["card_event_id"],
        attempted=bool(row["attempted"]),
        sending_device_id=row["sending_device_id"],
        created_at_ns=int(row["created_at_ns"]),
        continuation_id=stored_identity[0],
        continuation_generation=stored_identity[1],
        tool_call_id=stored_identity[2],
    )


def _log_unreadable_card(row: Row) -> None:
    logger.warning(
        "approval_card_row_unreadable",
        transaction_id=str(row["transaction_id"]),
        card_event_id=row["card_event_id"],
    )


def _resolution(stored: str | None) -> dict[str, Any] | None:
    if stored is None:
        return None
    resolution = json.loads(stored)
    if not isinstance(resolution, dict):
        msg = "Stored approval resolution is not an object"
        raise TypeError(msg)
    return resolution
