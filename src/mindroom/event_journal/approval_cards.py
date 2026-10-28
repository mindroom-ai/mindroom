"""Approval cards and receipts: their rows, events, reservation, terminal edits, retirement, and reads.

A card is one Matrix event showing one call: a ``request`` that asks for its
decision, or a receipt that shows a call a timed grant or a scheduled approval
already decided. Transaction IDs, frozen payloads, membership tenure, attempts,
devices, and acknowledgements live in ``matrix_delivery_outbox``; a card's
decision is its call's, in ``approval_calls``. Every Matrix event known to show
a card has a row in ``approval_card_events``, which is how a click finds its
card, even after the card retired.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

from mindroom.tool_approval_grants import ApprovalGrantScope

from . import approval_calls, approval_continuations, approval_grants, membership_state, outbox
from .identity import decode_thread_id, encode_thread_id
from .models import DURABLE_DELIVERY_ID_KEY, DeliveryStage

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.tool_approval_grants import ApprovalOperation

    from .approval_calls import CallKey, OwnerKind
    from .backend import Row, Transaction

type _CardKind = Literal["request", "grant_receipt", "scheduled_receipt"]

TIMEOUT_REASON = "Tool approval request timed out."


@dataclass(frozen=True, slots=True)
class ApprovalDecisionMetadata:
    """Authenticated decision facts; original request evidence belongs to the journal."""

    resolved_by: str | None = None
    resolved_at: str | None = None
    provenance: Mapping[str, Any] | None = None
    auto_approval: Mapping[str, Any] | None = None
    scheduled_scope: str | None = None


@dataclass(frozen=True, slots=True)
class ApprovalCardReservation:
    """One exact-call approval card and its frozen Matrix payload."""

    delivery_id: str
    tool_call_id: str
    event_type: str
    payload: Mapping[str, object]
    # Who may decide the call the card shows.
    approver_user_id: str
    # Whether an approval can be honored: the arguments were reviewable when the card was prepared.
    approvable: bool
    grant_operation: ApprovalOperation | None = None


@dataclass(frozen=True, slots=True)
class RecordedApprovalDecision:
    """What the journal holds after one attempt to decide a call."""

    # The terminal edit its request card carries, which is not always the one
    # just offered: a decided call keeps its first decision. None when no card
    # shows the call.
    resolution: dict[str, Any] | None
    # Whether this attempt is what decided the call.
    recorded: bool
    delivery_id: str | None = None
    card_event_id: str | None = None
    continuation_ready: bool = False
    continuation_entity_name: str | None = None
    continuation_room_id: str | None = None
    source_event_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ApprovalCardView:
    """One card with the facts of its call that a click, a deadline, or recovery decides by."""

    delivery_id: str
    kind: _CardKind
    call: CallKey
    room_id: str
    thread_id: str | None
    card_event_id: str | None
    created_at_ns: int
    approvable: bool
    retired: bool
    # Whether the card principal's current membership of the room owns the card's deliveries; a retired card has none.
    current: bool
    # The call's facts; None once a retired card's call is gone.
    owner_kind: OwnerKind | None
    approver_user_id: str | None
    expires_at_ns: int | None
    decided: bool
    invoking_agent: str | None
    # The entity of the paused reply that owns the call, while that reply exists.
    responder_entity_name: str | None
    offers_timed_approval: bool
    any_arguments_offered: bool

    @property
    def terminal(self) -> bool:
        """Return whether no click on this card can decide anything: it retired or is a receipt."""
        return self.retired or self.kind != "request"


@dataclass(frozen=True, slots=True)
class _RequestCard:
    """The live request card of one call, with what its terminal edit needs."""

    principal_id: str
    delivery_id: str
    event_type: str
    room_id: str
    thread_id: str | None
    membership_epoch: int
    payload_json: str
    card_event_id: str | None


def decode_object_payload(value: object, *, description: str) -> dict[str, Any]:
    """Decode one durable JSON object without its private delivery marker."""
    decoded = json.loads(str(value))
    if not isinstance(decoded, dict):
        msg = f"Stored {description} is not an object"
        raise TypeError(msg)
    decoded.pop(DURABLE_DELIVERY_ID_KEY, None)
    return decoded


def terminal_content(
    content: Mapping[str, Any],
    *,
    status: Literal["approved", "denied", "expired"],
    reason: str | None,
    metadata: ApprovalDecisionMetadata | None = None,
    publication: Literal["edit", "receipt"] = "edit",
) -> dict[str, Any]:
    """Pure terminal projection; automatic originals retain complete argument evidence."""
    if status == "approved" and metadata is None:
        msg = "An approved card requires its authenticated decision metadata."
        raise ValueError(msg)
    excluded = {
        "auto_approve_options",
        "auto_approval",
        "approval_provenance",
        "resolved_at",
        "resolution_reason",
        "scheduled_scope_options",
    }
    if publication == "edit":
        excluded.add("full_arguments")
    resolution = {key: value for key, value in content.items() if key not in excluded}
    resolution.update(status=status, approvable=False, resolved_by=None if metadata is None else metadata.resolved_by)
    if metadata is not None and metadata.resolved_at is not None:
        resolution["resolved_at"] = metadata.resolved_at
    if reason is not None:
        resolution["resolution_reason"] = reason
    if status == "approved" and metadata is not None:
        resolution["approval_provenance"] = dict(metadata.provenance or {"kind": "once"})
        if publication == "edit" and metadata.auto_approval is not None:
            resolution["auto_approval"] = dict(metadata.auto_approval)
        if metadata.scheduled_scope is not None:
            resolution["scheduled_scope"] = metadata.scheduled_scope
    tool_name = resolution.get("tool_name")
    label = "Auto-approved" if publication == "receipt" and status == "approved" else status.title()
    resolution["body"] = f"{label}: {tool_name}" if isinstance(tool_name, str) else f"Approval {status}"
    return resolution


def _reserve(
    transaction: Transaction,
    principal_id: str,
    *,
    kind: _CardKind,
    call: CallKey,
    delivery_id: str,
    event_type: str,
    payload: Mapping[str, object],
    room_id: str,
    thread_id: str | None,
    membership_epoch: int,
    approvable: bool,
) -> None:
    """Reserve one card's frozen INITIAL delivery in the shared Matrix delivery lifecycle, with its card row."""
    outbox.enqueue(
        transaction,
        principal_id,
        delivery_id=delivery_id,
        stage=DeliveryStage.INITIAL,
        event_type=event_type,
        room_id=room_id,
        membership_epoch=membership_epoch,
        thread_id=thread_id,
        payload=payload,
        edits_event_id=None,
    )
    transaction.execute(
        """
        INSERT INTO approval_cards (
            principal_id, delivery_id, kind, owner_id, generation, tool_call_id,
            room_id, thread_id, approvable
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            principal_id,
            delivery_id,
            kind,
            call.owner_id,
            call.generation,
            call.tool_call_id,
            room_id,
            encode_thread_id(thread_id),
            int(approvable),
        ),
    )


def reserve_receipt(
    transaction: Transaction,
    principal_id: str,
    *,
    kind: Literal["grant_receipt", "scheduled_receipt"],
    call: CallKey,
    delivery_id: str,
    event_type: str,
    room_id: str,
    thread_id: str | None,
    membership_epoch: int,
    payload: Mapping[str, object],
) -> None:
    """Reserve the receipt that shows a call an automatic approval decided."""
    _reserve(
        transaction,
        principal_id,
        kind=kind,
        call=call,
        delivery_id=delivery_id,
        event_type=event_type,
        payload=payload,
        room_id=room_id,
        thread_id=thread_id,
        membership_epoch=membership_epoch,
        approvable=False,
    )


def _reserve_card(
    transaction: Transaction,
    principal_id: str,
    *,
    call: CallKey,
    card: ApprovalCardReservation,
    scope: ApprovalGrantScope | None,
    room_id: str,
    thread_id: str | None,
    membership_epoch: int,
) -> None:
    """Reserve one call's request card, or the receipt of an active timed grant that approves it at once."""
    offer = (
        None if scope is None else approval_grants.offer(transaction, card, scope, membership_epoch=membership_epoch)
    )
    if offer is not None and approval_grants.apply_active(
        transaction,
        principal_id,
        call=call,
        offer=offer,
        room_id=room_id,
        thread_id=thread_id,
        membership_epoch=membership_epoch,
    ):
        return
    reserved = card if offer is None else offer.card
    _reserve(
        transaction,
        principal_id,
        kind="request",
        call=call,
        delivery_id=reserved.delivery_id,
        event_type=reserved.event_type,
        payload=reserved.payload,
        room_id=room_id,
        thread_id=thread_id,
        membership_epoch=membership_epoch,
        approvable=reserved.approvable,
    )
    if offer is not None and offer.scope is not None:
        approval_grants.insert_scope(transaction, principal_id, delivery_id=card.delivery_id, scope=offer.scope)


def reserve_reply_cards(
    transaction: Transaction,
    card_principal_id: str,
    *,
    continuation_principal_id: str,
    continuation_id: str,
    expected_generation: int,
    cards: tuple[ApprovalCardReservation, ...],
) -> bool:
    """Reserve every undecided call's card of one paused generation and release its publication lease, atomically."""
    approval_grants.lock(transaction, card_principal_id)
    # Reservation and a failure fence can arrive through different stores.
    # Lock the paused reply before reading its publication lease so either
    # every card and activation wins, or the failure wins without orphan deliveries.
    transaction.execute(
        "UPDATE approval_continuations SET state = state WHERE principal_id = ? AND approval_id = ?",
        (continuation_principal_id, continuation_id),
    )
    continuation = approval_continuations.get(transaction, continuation_principal_id, approval_id=continuation_id)
    if (
        continuation is None
        or continuation.state != "waiting"
        or continuation.generation != expected_generation
        or continuation.runtime_generation is None
        or not cards
    ):
        return False
    required = approval_calls.undecided_tool_call_ids(
        transaction,
        approval_id=continuation_id,
        generation=expected_generation,
    )
    reserved = tuple(card.tool_call_id for card in cards)
    if len(set(reserved)) != len(reserved) or set(reserved) != set(required):
        return False
    for card in cards:
        if _reply_identity(card.payload) != (continuation_id, expected_generation, card.tool_call_id):
            msg = f"Approval delivery {card.delivery_id!r} changed exact-call identity"
            raise ValueError(msg)
    membership_epoch = _claim_membership(transaction, card_principal_id, continuation.room_id)
    if membership_epoch is None:
        return False
    invoking_agents = {call.tool_call_id: call.invoking_agent for call in continuation.calls}
    for card in cards:
        call = approval_calls.CallKey(continuation_id, expected_generation, card.tool_call_id)
        approval_calls.set_approver(transaction, call, card.approver_user_id)
        _reserve_card(
            transaction,
            card_principal_id,
            call=call,
            card=card,
            scope=ApprovalGrantScope(
                room_id=continuation.room_id,
                thread_id=continuation.thread_id,
                requester_id=continuation.requester_id,
                entity_name=continuation.entity_name,
                responder_principal_id=continuation_principal_id,
                invoking_agent=invoking_agents[card.tool_call_id],
            ),
            room_id=continuation.room_id,
            thread_id=continuation.thread_id,
            membership_epoch=membership_epoch,
        )
    return (
        approval_continuations.activate(
            transaction,
            continuation_principal_id,
            approval_id=continuation_id,
            expected_generation=expected_generation,
        )
        is not None
    )


def _reply_identity(payload: Mapping[str, object]) -> tuple[str, int, str] | None:
    """Return the paused call a request card's payload names, which its reservation checks against the call."""
    continuation_id = payload.get("continuation_id")
    generation = payload.get("continuation_generation")
    tool_call_id = payload.get("tool_call_id")
    if (
        not isinstance(continuation_id, str)
        or not isinstance(generation, int)
        or isinstance(generation, bool)
        or not isinstance(tool_call_id, str)
    ):
        return None
    return continuation_id, generation, tool_call_id


def _claim_membership(transaction: Transaction, principal_id: str, room_id: str) -> int | None:
    """Hold the card principal's current membership of a room for a reservation, or None after a departure fence."""
    epoch = transaction.fetchone(
        "SELECT membership_epoch FROM room_membership WHERE principal_id = ? AND room_id = ?",
        (principal_id, room_id),
    )
    membership_epoch = 0 if epoch is None else int(epoch["membership_epoch"])
    if not membership_state.claim_membership_epoch(
        transaction,
        principal_id,
        room_id=room_id,
        expected_membership_epoch=membership_epoch,
    ):
        return None
    return membership_epoch


def reserve_detached_card(
    transaction: Transaction,
    principal_id: str,
    *,
    call: CallKey,
    owner_kind: OwnerKind,
    tool_name: str,
    toolkit_name: str | None,
    invoking_agent: str,
    room_id: str,
    thread_id: str | None,
    expires_at_ns: int,
    card: ApprovalCardReservation,
    grant_scope: ApprovalGrantScope | None = None,
) -> bool:
    """Reserve one detached call with its card, or apply an active grant in the card's place.

    A call already reserved reserves nothing more and still succeeds; a departed membership refuses.
    """
    if card.tool_call_id != call.tool_call_id:
        msg = f"Detached approval delivery {card.delivery_id!r} changed exact-call identity"
        raise ValueError(msg)
    if grant_scope is not None:
        approval_grants.lock(transaction, principal_id)
    membership_epoch = _claim_membership(transaction, principal_id, room_id)
    if membership_epoch is None:
        return False
    if not approval_calls.insert_detached(
        transaction,
        principal_id,
        call=call,
        owner_kind=owner_kind,
        tool_name=tool_name,
        invoking_agent=invoking_agent,
        toolkit_name=toolkit_name,
        approver_user_id=card.approver_user_id,
        expires_at_ns=expires_at_ns,
    ):
        return True
    _reserve_card(
        transaction,
        principal_id,
        call=call,
        card=card,
        scope=grant_scope,
        room_id=room_id,
        thread_id=thread_id,
        membership_epoch=membership_epoch,
    )
    return True


def request_card(transaction: Transaction, call: CallKey) -> _RequestCard | None:
    """Return the live request card that shows one call, if it has one."""
    row = transaction.fetchone(
        f"""
        SELECT cards.principal_id, cards.delivery_id, initial.event_type, initial.room_id, initial.thread_id,
               initial.membership_epoch, initial.payload_json,
               {card_event_of("cards")} AS card_event_id
        FROM approval_cards AS cards
        JOIN matrix_delivery_outbox AS initial
          ON initial.principal_id = cards.principal_id
         AND initial.delivery_id = cards.delivery_id
         AND initial.stage = 'initial'
        WHERE cards.owner_id = ? AND cards.generation = ? AND cards.tool_call_id = ?
          AND cards.kind = 'request' AND cards.retired = 0
        """,  # noqa: S608 - a fixed SQL fragment, not input
        (call.owner_id, call.generation, call.tool_call_id),
    )
    if row is None:
        return None
    return _RequestCard(
        principal_id=str(row["principal_id"]),
        delivery_id=str(row["delivery_id"]),
        event_type=str(row["event_type"]),
        room_id=str(row["room_id"]),
        thread_id=decode_thread_id(str(row["thread_id"])),
        membership_epoch=int(row["membership_epoch"]),
        payload_json=str(row["payload_json"]),
        card_event_id=cast("str | None", row["card_event_id"]),
    )


def terminal_resolution(transaction: Transaction, card: _RequestCard) -> dict[str, Any] | None:
    """Return the terminal edit a decided call's card carries."""
    final = transaction.fetchone(
        "SELECT payload_json FROM matrix_delivery_outbox WHERE principal_id = ? AND delivery_id = ? AND stage = 'final'",
        (card.principal_id, card.delivery_id),
    )
    return None if final is None else decode_object_payload(final["payload_json"], description="approval resolution")


def queue_terminal_edit(
    transaction: Transaction,
    card: _RequestCard,
    *,
    status: Literal["approved", "denied", "expired"],
    reason: str | None,
    metadata: ApprovalDecisionMetadata | None,
) -> dict[str, Any]:
    """Queue a request card's terminal edit, built from its frozen original, and return it."""
    resolution = terminal_content(
        decode_object_payload(card.payload_json, description="approval payload"),
        status=status,
        reason=reason,
        metadata=metadata,
    )
    outbox.enqueue(
        transaction,
        card.principal_id,
        delivery_id=card.delivery_id,
        stage=DeliveryStage.FINAL,
        event_type=card.event_type,
        room_id=card.room_id,
        membership_epoch=card.membership_epoch,
        thread_id=card.thread_id,
        payload=resolution,
        edits_event_id=card.card_event_id,
        edit_target_pending=card.card_event_id is None,
    )
    return resolution


def remember_alias(
    transaction: Transaction,
    principal_id: str,
    *,
    room_id: str,
    card_event_id: str,
    delivery_id: str,
) -> None:
    """Remember a transport-verified copy of an automatic receipt as another event of that receipt."""
    transaction.execute(
        """
        INSERT INTO approval_card_events (principal_id, card_event_id, room_id, delivery_id)
        SELECT principal_id, ?, room_id, delivery_id FROM approval_cards
        WHERE principal_id = ? AND delivery_id = ? AND room_id = ? AND kind <> 'request'
        ON CONFLICT (principal_id, card_event_id) DO NOTHING
        """,
        (card_event_id, principal_id, delivery_id, room_id),
    )


def _retire(transaction: Transaction, principal_id: str, delivery_id: str) -> None:
    """Delete a card's outbox rows and keep the card for click recognition and grant revocation.

    A card that did not originate a grant no longer offers anything, so its scope
    row goes; a paused reply's call goes once its reply is gone and no live card
    shows it.
    """
    transaction.execute(
        "DELETE FROM matrix_delivery_outbox WHERE principal_id = ? AND delivery_id = ?",
        (principal_id, delivery_id),
    )
    transaction.execute(
        "UPDATE approval_cards SET retired = 1 WHERE principal_id = ? AND delivery_id = ?",
        (principal_id, delivery_id),
    )
    transaction.execute(
        """
        DELETE FROM approval_card_scopes
        WHERE principal_id = ? AND delivery_id = ?
          AND NOT EXISTS (
              SELECT 1 FROM approval_grants AS grants
              WHERE grants.principal_id = approval_card_scopes.principal_id
                AND grants.delivery_id = approval_card_scopes.delivery_id
          )
        """,
        (principal_id, delivery_id),
    )
    transaction.execute(
        f"""
        DELETE FROM approval_calls
        WHERE owner_kind = 'reply'
          AND EXISTS (
              SELECT 1 FROM approval_cards AS retired
              WHERE retired.principal_id = ? AND retired.delivery_id = ?
                AND retired.owner_id = approval_calls.owner_id
                AND retired.generation = approval_calls.generation
                AND retired.tool_call_id = approval_calls.tool_call_id
          )
          AND NOT EXISTS (
              SELECT 1 FROM approval_continuations AS continuations
              WHERE continuations.approval_id = approval_calls.owner_id
          )
          AND NOT {approval_calls.LIVE_CARD.format(calls="approval_calls")}
        """,  # noqa: S608 - a fixed predicate, not input
        (principal_id, delivery_id),
    )


def retire(transaction: Transaction, principal_id: str, *, delivery_id: str) -> bool:
    """Retire a decided request card Matrix is done with; return whether it is retired.

    Matrix is done with it once its terminal edit is acknowledged or refused for good, or once its card itself was
    refused for good, so no edit can ever reach it; a card whose delivery Matrix refuses must not keep the paused
    reply it shows from settling.
    """
    row = transaction.fetchone(
        """
        SELECT cards.retired, final.acknowledged_event_id AS final_event_id,
               final.permanent_failure_reason AS final_refused, initial.permanent_failure_reason AS initial_refused,
               calls.owner_id IS NOT NULL AND calls.decision IS NULL AS undecided
        FROM approval_cards AS cards
        LEFT JOIN approval_calls AS calls
          ON calls.owner_id = cards.owner_id
         AND calls.generation = cards.generation
         AND calls.tool_call_id = cards.tool_call_id
        LEFT JOIN matrix_delivery_outbox AS initial
          ON initial.principal_id = cards.principal_id
         AND initial.delivery_id = cards.delivery_id
         AND initial.stage = 'initial'
        LEFT JOIN matrix_delivery_outbox AS final
          ON final.principal_id = cards.principal_id
         AND final.delivery_id = cards.delivery_id
         AND final.stage = 'final'
        WHERE cards.principal_id = ? AND cards.delivery_id = ? AND cards.kind = 'request'
        """,
        (principal_id, delivery_id),
    )
    if row is None:
        return False
    if bool(row["retired"]):
        return True
    if bool(row["undecided"]) or (
        row["final_event_id"] is None and row["final_refused"] is None and row["initial_refused"] is None
    ):
        return False
    _retire(transaction, principal_id, delivery_id)
    return True


def retire_receipts(transaction: Transaction, principal_id: str) -> None:
    """Retire every receipt whose delivery Matrix acknowledged or refused for good."""
    rows = transaction.fetchall(
        """
        SELECT cards.delivery_id FROM approval_cards AS cards
        LEFT JOIN matrix_delivery_outbox AS initial
          ON initial.principal_id = cards.principal_id
         AND initial.delivery_id = cards.delivery_id
         AND initial.stage = 'initial'
        WHERE cards.principal_id = ? AND cards.kind <> 'request' AND cards.retired = 0
          AND (
              initial.delivery_id IS NULL OR initial.acknowledged_event_id IS NOT NULL
              OR initial.permanent_failure_reason IS NOT NULL OR initial.retired = 1
          )
        """,
        (principal_id,),
    )
    for row in rows:
        _retire(transaction, principal_id, str(row["delivery_id"]))


# A card's outbox rows belong to the card principal's current, unfenced membership of the room.
_CURRENT_MEMBERSHIP = """(
    initial.membership_epoch = COALESCE(membership.membership_epoch, 0) AND COALESCE(membership.departure_fenced, 0) = 0
)"""


def card_event_of(alias: str) -> str:
    """Return the SQL that reads the first Matrix event showing the card ``alias``'s principal and delivery name."""
    return f"""(
        SELECT MIN(events.card_event_id) FROM approval_card_events AS events
        WHERE events.principal_id = {alias}.principal_id AND events.delivery_id = {alias}.delivery_id
    )"""  # noqa: S608 - a table alias the module chooses, not input


_VIEW_COLUMNS = f"""
    cards.delivery_id AS delivery_id, cards.kind AS kind,
    cards.owner_id AS owner_id, cards.generation AS generation, cards.tool_call_id AS tool_call_id,
    cards.room_id AS room_id, cards.thread_id AS thread_id,
    cards.approvable AS approvable, cards.retired AS retired,
    COALESCE(initial.created_at_ns, 0) AS created_at_ns,
    {_CURRENT_MEMBERSHIP} AS current_membership,
    calls.owner_kind AS owner_kind, calls.approver_user_id AS approver_user_id,
    calls.expires_at_ns AS expires_at_ns, calls.decision AS decision, calls.invoking_agent AS invoking_agent,
    paused_reply.entity_name AS responder_entity_name,
    scope.delivery_id AS scope_delivery_id,
    scheduled.any_arguments_offered AS any_arguments_offered
"""
_VIEW_JOINS = """
    LEFT JOIN room_membership AS membership
      ON membership.principal_id = cards.principal_id
     AND membership.room_id = cards.room_id
    LEFT JOIN approval_calls AS calls
      ON calls.owner_id = cards.owner_id
     AND calls.generation = cards.generation
     AND calls.tool_call_id = cards.tool_call_id
    LEFT JOIN approval_continuations AS continuations
      ON calls.owner_kind = 'reply' AND continuations.approval_id = calls.owner_id
    LEFT JOIN reply_spans AS paused_span
      ON paused_span.principal_id = continuations.principal_id
     AND paused_span.span_id = continuations.span_id
    LEFT JOIN reply_messages AS paused_reply
      ON paused_reply.principal_id = paused_span.principal_id
     AND paused_reply.reply_id = paused_span.reply_id
    LEFT JOIN approval_card_scopes AS scope
      ON scope.principal_id = cards.principal_id
     AND scope.delivery_id = cards.delivery_id
    LEFT JOIN scheduled_call_approvals AS scheduled
      ON calls.owner_kind = 'scheduled'
     AND scheduled.principal_id = calls.principal_id
     AND scheduled.task_id = calls.tool_call_id
"""


def _view(row: Row, card_event_id: str | None) -> ApprovalCardView:
    return ApprovalCardView(
        delivery_id=str(row["delivery_id"]),
        kind=cast("_CardKind", str(row["kind"])),
        call=approval_calls.CallKey.of_row(row),
        room_id=str(row["room_id"]),
        thread_id=decode_thread_id(str(row["thread_id"])),
        card_event_id=card_event_id,
        created_at_ns=int(row["created_at_ns"]),
        approvable=bool(row["approvable"]),
        retired=bool(row["retired"]),
        current=bool(row["current_membership"]),
        owner_kind=cast("OwnerKind | None", row["owner_kind"]),
        approver_user_id=cast("str | None", row["approver_user_id"]),
        expires_at_ns=None if row["expires_at_ns"] is None else int(row["expires_at_ns"]),
        decided=row["decision"] is not None,
        invoking_agent=cast("str | None", row["invoking_agent"]),
        responder_entity_name=cast("str | None", row["responder_entity_name"]),
        offers_timed_approval=row["scope_delivery_id"] is not None,
        any_arguments_offered=bool(row["any_arguments_offered"]),
    )


def card_for_event(
    transaction: Transaction,
    principal_id: str,
    *,
    room_id: str,
    card_event_id: str,
) -> ApprovalCardView | None:
    """Return the card a Matrix event shows, if the event shows one."""
    row = transaction.fetchone(
        f"""
        SELECT {_VIEW_COLUMNS}
        FROM approval_card_events AS events
        JOIN approval_cards AS cards
          ON cards.principal_id = events.principal_id
         AND cards.delivery_id = events.delivery_id
        LEFT JOIN matrix_delivery_outbox AS initial
          ON initial.principal_id = cards.principal_id
         AND initial.delivery_id = cards.delivery_id
         AND initial.stage = 'initial'
        {_VIEW_JOINS}
        WHERE events.principal_id = ? AND events.room_id = ? AND events.card_event_id = ?
        """,  # noqa: S608 - a fixed column list and joins, not input
        (principal_id, room_id, card_event_id),
    )
    return None if row is None else _view(row, card_event_id)


_LIVE_REQUESTS = f"""
    cards.principal_id = ? AND cards.kind = 'request' AND cards.retired = 0 AND {_CURRENT_MEMBERSHIP}
"""


def pending_room_ids(transaction: Transaction, principal_id: str) -> tuple[str, ...]:
    """Return every current-membership room where this principal has a live request card."""
    rows = transaction.fetchall(
        f"""
        SELECT DISTINCT cards.room_id/*bytes*/ AS room_id
        FROM approval_cards AS cards
        JOIN matrix_delivery_outbox AS initial
          ON initial.principal_id = cards.principal_id
         AND initial.delivery_id = cards.delivery_id
         AND initial.stage = 'initial'
        LEFT JOIN room_membership AS membership
          ON membership.principal_id = cards.principal_id
         AND membership.room_id = cards.room_id
        WHERE {_LIVE_REQUESTS}
        ORDER BY cards.room_id/*bytes*/
        """,  # noqa: S608 - a fixed predicate, not input
        (principal_id,),
    )
    return tuple(str(row["room_id"]) for row in rows)


def pending_cards(
    transaction: Transaction,
    principal_id: str,
    *,
    room_id: str,
    limit: int,
    after: tuple[int, str] | None = None,
) -> tuple[ApprovalCardView, ...]:
    """Return one room's live request cards, oldest first.

    Includes cards no send has come back from. Those are the ones a crash is
    most likely to have stranded, and leaving them out would restore exactly
    the blind spot claiming before sending exists to close.

    ``after`` resumes past a row already visited, in the same order the scan
    uses. A card whose settlement failed keeps its row on purpose, so without a
    cursor a page of them is re-read forever and every card behind it starves.
    """
    # delivery_id shipped as unpinned TEXT, so the byte-order pin goes on the
    # comparison itself, or a server whose collation is not byte order would
    # order the rows differently from the cursor that walks them.
    cursor_clause = "" if after is None else " AND (initial.created_at_ns, initial.delivery_id/*bytes*/) > (?, ?)"
    cursor_params: tuple[object, ...] = () if after is None else after
    rows = transaction.fetchall(
        f"""
        SELECT {_VIEW_COLUMNS}, {card_event_of("cards")} AS card_event_id
        FROM matrix_delivery_outbox AS initial
        JOIN approval_cards AS cards
          ON cards.principal_id = initial.principal_id
         AND cards.delivery_id = initial.delivery_id
        {_VIEW_JOINS}
        WHERE initial.principal_id = ? AND initial.room_id = ? AND initial.stage = 'initial'
          AND {_LIVE_REQUESTS}{cursor_clause}
        -- Two cards sent in the same nanosecond would otherwise come back in
        -- whatever order each backend felt like, and the caller expires them
        -- in the order it reads them.
        ORDER BY initial.created_at_ns, initial.delivery_id/*bytes*/
        LIMIT ?
        """,  # noqa: S608 - a fixed column list and clauses, not input
        (principal_id, room_id, principal_id, *cursor_params, limit),
    )
    return tuple(_view(row, cast("str | None", row["card_event_id"])) for row in rows)


def retire_completed_for_departure(transaction: Transaction, principal_id: str, *, room_id: str) -> None:
    """Finish the retirement a crash left after a terminal edit's acknowledgement, before departure fences the room."""
    rows = transaction.fetchall(
        f"""
        SELECT cards.principal_id, cards.delivery_id
        FROM approval_cards AS cards
        JOIN matrix_delivery_outbox AS final
          ON final.principal_id = cards.principal_id
         AND final.delivery_id = cards.delivery_id
         AND final.stage = 'final'
        LEFT JOIN approval_calls AS calls
          ON calls.owner_id = cards.owner_id
         AND calls.generation = cards.generation
         AND calls.tool_call_id = cards.tool_call_id
        LEFT JOIN approval_continuations AS continuations
          ON calls.owner_kind = 'reply' AND continuations.approval_id = calls.owner_id
        WHERE cards.kind = 'request' AND cards.retired = 0 AND final.acknowledged_event_id IS NOT NULL
          AND (
              (cards.principal_id = ? AND cards.room_id = ?)
              OR (
                  continuations.principal_id = ?
                  AND {approval_continuations.holds_source_in_room("continuations")}
              )
          )
        """,  # noqa: S608 - a fixed SQL fragment, not input
        (principal_id, room_id, principal_id, room_id),
    )
    for row in rows:
        _retire(transaction, str(row["principal_id"]), str(row["delivery_id"]))


def deny_for_departed_card_owner(
    transaction: Transaction,
    card_principal_id: str,
    *,
    room_id: str,
    reason: str,
) -> None:
    """Fail the paused replies a departed card principal's cards wait on, and deny every call those cards show.

    A card never attempted is deleted outright; an attempted one keeps its
    terminal-edit debt under the membership that inherited the room.
    """
    transaction.execute(
        """
        UPDATE approval_continuations
        SET state = 'failing', failure_kind = 'error', failure_detail = ?, runtime_generation = NULL
        WHERE state IN ('waiting', 'ready')
          AND EXISTS (
              SELECT 1 FROM approval_cards AS cards
              JOIN approval_calls AS calls
                ON calls.owner_id = cards.owner_id
               AND calls.generation = cards.generation
               AND calls.tool_call_id = cards.tool_call_id
              WHERE cards.principal_id = ? AND cards.room_id = ?
                AND cards.kind = 'request' AND cards.retired = 0
                AND calls.owner_kind = 'reply' AND calls.owner_id = approval_continuations.approval_id
                AND calls.decision IS NULL
          )
        """,
        (reason, card_principal_id, room_id),
    )
    rows = transaction.fetchall(
        """
        SELECT cards.delivery_id, cards.owner_id, cards.generation, cards.tool_call_id,
               initial.attempted, membership.membership_epoch AS current_membership_epoch
        FROM approval_cards AS cards
        JOIN matrix_delivery_outbox AS initial
          ON initial.principal_id = cards.principal_id
         AND initial.delivery_id = cards.delivery_id
         AND initial.stage = 'initial'
        JOIN room_membership AS membership
          ON membership.principal_id = cards.principal_id
         AND membership.room_id = cards.room_id
        WHERE cards.principal_id = ? AND cards.room_id = ? AND cards.kind = 'request' AND cards.retired = 0
        """,
        (card_principal_id, room_id),
    )
    for row in rows:
        delivery_id = str(row["delivery_id"])
        approval_calls.resolve_call(
            transaction,
            approval_calls.CallKey.of_row(row),
            requested="denied",
            reason=reason,
            metadata=None,
        )
        if not bool(row["attempted"]):
            _delete_unattempted(transaction, card_principal_id, delivery_id)
            continue
        transaction.execute(
            "UPDATE matrix_delivery_outbox SET membership_epoch = ? WHERE principal_id = ? AND delivery_id = ?",
            (int(row["current_membership_epoch"]), card_principal_id, delivery_id),
        )


def expire_for_departed_responder(
    transaction: Transaction,
    continuation_principal_id: str,
    *,
    room_id: str,
    reason: str,
) -> None:
    """Expire every call a departed responder's paused replies still wait on, keeping the router's terminal-edit debt."""
    # A decision locks its paused reply before the card delivery shared with
    # this cleanup. Keep the same order before fencing the deliveries and
    # deleting the paused reply, otherwise the two transactions can deadlock.
    transaction.execute(
        f"""
        UPDATE approval_continuations SET state = state
        WHERE principal_id = ?
          AND {approval_continuations.holds_source_in_room("approval_continuations")}
        """,  # noqa: S608 - a fixed SQL fragment, not input
        (continuation_principal_id, room_id),
    )
    rows = transaction.fetchall(
        f"""
        SELECT cards.principal_id, cards.delivery_id, cards.owner_id, cards.generation, cards.tool_call_id,
               initial.attempted
        FROM approval_cards AS cards
        JOIN approval_calls AS calls
          ON calls.owner_id = cards.owner_id
         AND calls.generation = cards.generation
         AND calls.tool_call_id = cards.tool_call_id
        JOIN approval_continuations AS continuations
          ON continuations.approval_id = calls.owner_id
        JOIN matrix_delivery_outbox AS initial
          ON initial.principal_id = cards.principal_id
         AND initial.delivery_id = cards.delivery_id
         AND initial.stage = 'initial'
        WHERE cards.kind = 'request' AND cards.retired = 0 AND calls.owner_kind = 'reply'
          AND continuations.principal_id = ?
          AND {approval_continuations.holds_source_in_room("continuations")}
        """,  # noqa: S608 - a fixed SQL fragment, not input
        (continuation_principal_id, room_id),
    )
    for row in rows:
        if not bool(row["attempted"]):
            _delete_unattempted(transaction, str(row["principal_id"]), str(row["delivery_id"]))
            continue
        approval_calls.resolve_call(
            transaction,
            approval_calls.CallKey.of_row(row),
            requested="expired",
            reason=reason,
            metadata=None,
        )


def _delete_unattempted(transaction: Transaction, principal_id: str, delivery_id: str) -> None:
    """Delete a card, its scope, and every stage that provably never reached Matrix."""
    for table in ("approval_cards", "approval_card_scopes"):
        transaction.execute(
            f"DELETE FROM {table} WHERE principal_id = ? AND delivery_id = ?",  # noqa: S608 - a fixed table list
            (principal_id, delivery_id),
        )
    transaction.execute(
        "DELETE FROM matrix_delivery_outbox WHERE principal_id = ? AND delivery_id = ? AND attempted = 0",
        (principal_id, delivery_id),
    )
