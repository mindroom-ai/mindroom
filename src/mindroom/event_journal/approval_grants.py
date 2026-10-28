"""Timed grants: the consent a card offers, the grant it originates, its batch, and recoverable revocation edits."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING
from uuid import uuid4

from mindroom.tool_approval_grants import (
    ApprovalGrant,
    ApprovalGrantRevocation,
    approval_timestamp,
    valid_auto_approve_seconds,
)

from . import approval_calls, approval_cards, outbox
from .models import DeliveryStage

if TYPE_CHECKING:
    from mindroom.tool_approval_grants import ApprovalGrantScope

    from .approval_calls import CallKey
    from .approval_cards import ApprovalCardReservation, ApprovalDecisionMetadata, RecordedApprovalDecision
    from .backend import Row, Transaction


@dataclass(frozen=True, slots=True)
class _CardScope:
    """The consent one card offers for a timed approval."""

    scope_key: str
    operation: str
    requester_id: str
    entity_name: str
    invoking_agent: str
    responder_principal_id: str
    responder_epoch: int
    # The card principal's membership epoch when the card was reserved.
    consent_epoch: int


@dataclass(frozen=True, slots=True)
class _ScopeOffer:
    """A card prepared for reservation, with the consent it offers, if any is still current."""

    card: ApprovalCardReservation
    scope: _CardScope | None


def lock(transaction: Transaction, principal_id: str) -> None:
    """Serialize grant changes and card reservation before paused-reply locks."""
    transaction.execute(
        "INSERT INTO approval_grant_locks (principal_id) VALUES (?) ON CONFLICT (principal_id) DO NOTHING",
        (principal_id,),
    )
    transaction.execute(
        "UPDATE approval_grant_locks SET principal_id = principal_id WHERE principal_id = ?",
        (principal_id,),
    )


def _epoch(transaction: Transaction, principal_id: str, room_id: str) -> int | None:
    row = transaction.fetchone(
        "SELECT membership_epoch, departure_fenced FROM room_membership WHERE principal_id = ? AND room_id = ?",
        (principal_id, room_id),
    )
    return 0 if row is None else None if row["departure_fenced"] else int(row["membership_epoch"])


def offer(
    transaction: Transaction,
    card: ApprovalCardReservation,
    scope: ApprovalGrantScope,
    *,
    membership_epoch: int,
) -> _ScopeOffer | None:
    """Bind a card's timed-approval offer to its conversation's current memberships, or withdraw the offer.

    None means the card offers no timed approval at all.
    """
    if card.grant_operation is None or not scope.thread_id:
        return None
    responder_epoch = _epoch(transaction, scope.responder_principal_id, scope.room_id)
    if responder_epoch is None:
        payload = dict(card.payload)
        payload.pop("approval_scope", None)
        payload.pop("auto_approve_options", None)
        return _ScopeOffer(card=replace(card, payload=payload, grant_operation=None), scope=None)
    scope_key = json.dumps(
        [
            scope.room_id,
            scope.thread_id,
            scope.requester_id,
            scope.invoking_agent,
            card.grant_operation.key,
            membership_epoch,
            scope.responder_principal_id,
            responder_epoch,
        ],
        separators=(",", ":"),
    )
    scope_id = hashlib.sha256(scope_key.encode()).hexdigest()
    scope_wire = card.grant_operation.scope_wire(scope_id, scope.entity_name, scope.invoking_agent)
    if card.payload.get("approval_scope") != {**scope_wire, "id": "0" * 64}:
        msg = "Prepared approval scope does not match its call owner"
        raise ValueError(msg)
    return _ScopeOffer(
        card=replace(card, payload={**card.payload, "approval_scope": scope_wire}),
        scope=_CardScope(
            scope_key=scope_id,
            operation=card.grant_operation.key,
            requester_id=scope.requester_id,
            entity_name=scope.entity_name,
            invoking_agent=scope.invoking_agent,
            responder_principal_id=scope.responder_principal_id,
            responder_epoch=responder_epoch,
            consent_epoch=membership_epoch,
        ),
    )


def insert_scope(transaction: Transaction, principal_id: str, *, delivery_id: str, scope: _CardScope) -> None:
    """Record the consent one reserved card offers."""
    transaction.execute(
        """
        INSERT INTO approval_card_scopes (
            principal_id, delivery_id, scope_key, operation, requester_id, entity_name,
            invoking_agent, responder_principal_id, responder_epoch, consent_epoch
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            principal_id,
            delivery_id,
            scope.scope_key,
            scope.operation,
            scope.requester_id,
            scope.entity_name,
            scope.invoking_agent,
            scope.responder_principal_id,
            scope.responder_epoch,
            scope.consent_epoch,
        ),
    )


def _current(
    transaction: Transaction,
    principal_id: str,
    *,
    room_id: str,
    consent_epoch: int,
    responder_principal_id: str,
    responder_epoch: int,
) -> bool:
    return _epoch(transaction, principal_id, room_id) == consent_epoch and (
        _epoch(transaction, responder_principal_id, room_id) == responder_epoch
    )


def _row_current(transaction: Transaction, principal_id: str, row: Row) -> bool:
    return _current(
        transaction,
        principal_id,
        room_id=str(row["room_id"]),
        consent_epoch=int(row["consent_epoch"]),
        responder_principal_id=str(row["responder_principal_id"]),
        responder_epoch=int(row["responder_epoch"]),
    )


def _grant_select(extra_columns: str = "") -> str:
    """Return the SELECT of a grant with its originating card's scope, room, thread, and event, plus ``extra_columns``."""
    return f"""
    SELECT grants.grant_id, grants.delivery_id, grants.expires_at_ns, grants.revoked_at_ns, grants.resolution_json,
           scope.scope_key, scope.operation, scope.requester_id, scope.entity_name, scope.invoking_agent,
           scope.responder_principal_id, scope.responder_epoch, scope.consent_epoch,
           origin.room_id, origin.thread_id, {approval_cards.card_event_of("grants")} AS card_event_id{extra_columns}
    FROM approval_grants AS grants
    JOIN approval_card_scopes AS scope
      ON scope.principal_id = grants.principal_id AND scope.delivery_id = grants.delivery_id
    JOIN approval_cards AS origin
      ON origin.principal_id = grants.principal_id AND origin.delivery_id = grants.delivery_id
"""  # noqa: S608 - fixed SQL fragments, not input


def _grant(row: Row) -> ApprovalGrant:
    return ApprovalGrant(
        grant_id=str(row["grant_id"]),
        room_id=str(row["room_id"]),
        thread_id=str(row["thread_id"]),
        card_event_id=str(row["card_event_id"]),
        requester_id=str(row["requester_id"]),
        entity_name=str(row["entity_name"]),
        invoking_agent=str(row["invoking_agent"]),
        operation=str(row["operation"]),
        expires_at_ns=int(row["expires_at_ns"]),
        revoked_at_ns=None if row["revoked_at_ns"] is None else int(row["revoked_at_ns"]),
    )


def _for_event(transaction: Transaction, principal_id: str, *, room_id: str, card_event_id: str) -> Row | None:
    return transaction.fetchone(
        _grant_select()
        + """
        JOIN approval_card_events AS clicked
          ON clicked.principal_id = grants.principal_id AND clicked.delivery_id = grants.delivery_id
        WHERE grants.principal_id = ? AND origin.room_id = ? AND clicked.card_event_id = ?
        """,
        (principal_id, room_id, card_event_id),
    )


def for_card(transaction: Transaction, principal_id: str, *, room_id: str, card_event_id: str) -> ApprovalGrant | None:
    """Find the grant a card originated, even after the card and its paused reply have retired."""
    row = _for_event(transaction, principal_id, room_id=room_id, card_event_id=card_event_id)
    return None if row is None else _grant(row)


def apply_active(
    transaction: Transaction,
    principal_id: str,
    *,
    call: CallKey,
    offer: _ScopeOffer,
    room_id: str,
    thread_id: str | None,
    membership_epoch: int,
) -> bool:
    """Approve a new call under an active grant of its scope and reserve its receipt in the offered card's place."""
    card, scope = offer.card, offer.scope
    if scope is None or not _current(
        transaction,
        principal_id,
        room_id=room_id,
        consent_epoch=scope.consent_epoch,
        responder_principal_id=scope.responder_principal_id,
        responder_epoch=scope.responder_epoch,
    ):
        return False
    now = time.time_ns()
    grant_row = transaction.fetchone(
        _grant_select()
        + """
        WHERE grants.principal_id = ? AND scope.scope_key = ? AND grants.revoked_at_ns IS NULL AND grants.expires_at_ns > ?
        ORDER BY grants.expires_at_ns DESC LIMIT 1
        """,
        (principal_id, scope.scope_key, now),
    )
    if grant_row is None or not _row_current(transaction, principal_id, grant_row):
        return False
    expires = transaction.fetchone(
        "SELECT expires_at_ns FROM approval_calls WHERE owner_id = ? AND generation = ? AND tool_call_id = ?",
        (call.owner_id, call.generation, call.tool_call_id),
    )
    if expires is None or int(expires["expires_at_ns"]) <= now:
        return False
    grant_resolution = approval_cards.decode_object_payload(
        grant_row["resolution_json"],
        description="grant resolution",
    )
    metadata = approval_cards.ApprovalDecisionMetadata(
        resolved_by=str(grant_row["requester_id"]),
        resolved_at=approval_timestamp(now),
        provenance=grant_resolution.get("approval_provenance")
        or {
            "kind": "timed_grant",
            "grant_id": str(grant_row["grant_id"]),
            "grant_card_event_id": str(grant_row["card_event_id"]),
            "granted_by": str(grant_row["requester_id"]),
            "granted_at": grant_resolution.get("resolved_at"),
            "expires_at": approval_timestamp(int(grant_row["expires_at_ns"])),
        },
    )
    decided = approval_calls.resolve_call(
        transaction,
        call,
        requested="approved",
        reason=None,
        metadata=metadata,
        grant_id=str(grant_row["grant_id"]),
        # The deadline is judged at the instant the grant was found to cover the call, so its receipt and its
        # decision agree.
        now_ns=now,
    )
    if not decided.recorded:
        return False
    approval_cards.reserve_receipt(
        transaction,
        principal_id,
        kind="grant_receipt",
        call=call,
        delivery_id=card.delivery_id,
        event_type=card.event_type,
        room_id=room_id,
        thread_id=thread_id,
        membership_epoch=membership_epoch,
        payload=approval_cards.terminal_content(
            card.payload,
            status="approved",
            reason=None,
            metadata=metadata,
            publication="receipt",
        ),
    )
    return True


# The live request cards that offer a timed approval, with their call and consent.
_OFFERING_CARDS = """
    SELECT cards.delivery_id, cards.room_id, cards.thread_id,
           cards.owner_id, cards.generation, cards.tool_call_id,
           scope.scope_key, scope.operation, scope.requester_id, scope.entity_name, scope.invoking_agent,
           scope.responder_principal_id, scope.responder_epoch, scope.consent_epoch
    FROM approval_cards AS cards
    JOIN approval_card_scopes AS scope
      ON scope.principal_id = cards.principal_id AND scope.delivery_id = cards.delivery_id
    JOIN approval_calls AS calls
      ON calls.owner_id = cards.owner_id AND calls.generation = cards.generation
     AND calls.tool_call_id = cards.tool_call_id
"""


def create(
    transaction: Transaction,
    principal_id: str,
    *,
    room_id: str,
    card_event_id: str,
    sender_id: str,
    seconds: int,
    metadata: ApprovalDecisionMetadata,
    reason: str | None = None,
    current_binding: str | None = None,
) -> tuple[RecordedApprovalDecision, ...]:
    """Accept a grant and every pending call of its scope in one commit."""
    if not valid_auto_approve_seconds(seconds):
        return ()
    lock(transaction, principal_id)
    row = transaction.fetchone(
        _OFFERING_CARDS
        + """
        JOIN approval_card_events AS clicked
          ON clicked.principal_id = cards.principal_id AND clicked.delivery_id = cards.delivery_id
        WHERE cards.principal_id = ? AND cards.room_id = ? AND clicked.card_event_id = ?
          AND cards.kind = 'request' AND cards.retired = 0 AND calls.decision IS NULL
        """,
        (principal_id, room_id, card_event_id),
    )
    if (
        row is None
        or str(row["requester_id"]) != sender_id
        or not _row_current(transaction, principal_id, row)
        or (current_binding is not None and not str(row["operation"]).startswith(current_binding + ":"))
    ):
        return ()
    now = time.time_ns()
    grant = ApprovalGrant(
        grant_id=str(uuid4()),
        room_id=room_id,
        thread_id=str(row["thread_id"]),
        card_event_id=card_event_id,
        requester_id=sender_id,
        entity_name=str(row["entity_name"]),
        invoking_agent=str(row["invoking_agent"]),
        operation=str(row["operation"]),
        expires_at_ns=now + seconds * 1_000_000_000,
        revoked_at_ns=None,
    )
    provenance = {
        "kind": "timed_grant",
        "grant_id": grant.grant_id,
        "grant_card_event_id": card_event_id,
        "granted_by": sender_id,
        "granted_at": approval_timestamp(now),
        "duration_seconds": seconds,
        "expires_at": approval_timestamp(grant.expires_at_ns),
    }
    first = approval_calls.resolve_call(
        transaction,
        approval_calls.CallKey.of_row(row),
        requested="approved",
        reason=reason,
        metadata=replace(metadata, auto_approval=grant.wire(), provenance=provenance),
        grant_id=grant.grant_id,
    )
    if not first.recorded or first.resolution is None or first.resolution["status"] != "approved":
        return (first,)
    transaction.execute(
        """
        INSERT INTO approval_grants (principal_id, grant_id, delivery_id, expires_at_ns, resolution_json)
        VALUES (?, ?, ?, ?, ?)
        """,
        (principal_id, grant.grant_id, row["delivery_id"], grant.expires_at_ns, json.dumps(first.resolution)),
    )
    candidates = transaction.fetchall(
        _OFFERING_CARDS
        + """
        WHERE cards.principal_id = ? AND scope.scope_key = ?
          AND cards.kind = 'request' AND cards.retired = 0 AND calls.decision IS NULL
        ORDER BY cards.delivery_id
        """,
        (principal_id, row["scope_key"]),
    )
    batch = approval_cards.ApprovalDecisionMetadata(
        resolved_by=grant.requester_id,
        resolved_at=approval_timestamp(now),
        provenance=provenance,
    )
    return (
        first,
        *(
            approval_calls.resolve_call(
                transaction,
                approval_calls.CallKey.of_row(candidate),
                requested="approved",
                reason=None,
                metadata=batch,
                grant_id=grant.grant_id,
            )
            for candidate in candidates
        ),
    )


def revoke(
    transaction: Transaction,
    principal_id: str,
    *,
    room_id: str,
    card_event_id: str,
    sender_id: str,
    grant_id: str,
) -> ApprovalGrantRevocation | None:
    """Record revocation debt without rewriting the original terminal edit."""
    lock(transaction, principal_id)
    row = _for_event(transaction, principal_id, room_id=room_id, card_event_id=card_event_id)
    if (
        row is None
        or str(row["grant_id"]) != grant_id
        or row["requester_id"] != sender_id
        or not _row_current(transaction, principal_id, row)
    ):
        return None
    grant = _grant(row)
    revocation = ApprovalGrantRevocation(str(row["delivery_id"]), "approval-grant-revoked:" + grant_id)
    if grant.revoked_at_ns is not None:
        return revocation
    now = time.time_ns()
    if grant.expires_at_ns <= now:
        return None
    transaction.execute(
        "UPDATE approval_grants SET revoked_at_ns = ? WHERE principal_id = ? AND grant_id = ?",
        (now, principal_id, grant_id),
    )
    return revocation


def maintain(transaction: Transaction, principal_id: str, *, grant_id: str | None = None) -> tuple[str, ...]:
    """Release spent grant payloads and prepare newly deliverable revocations.

    Grant identity and its originating card survive payload retirement.
    Revoked grants retain acknowledgement debt past expiry until delivery is
    acknowledged, or membership ends. The original terminal edit must settle
    first so a delayed acceptance cannot overwrite the later revoked state.
    """
    lock(transaction, principal_id)
    rows = transaction.fetchall(
        _grant_select(
            """,
           original.delivery_id AS original_id, original.acknowledged_event_id AS original_event_id,
           acknowledgement.delivery_id AS revocation_id, acknowledgement.acknowledged_event_id AS revocation_event_id""",
        )
        + """
        LEFT JOIN matrix_delivery_outbox AS original
          ON original.principal_id = grants.principal_id
         AND original.delivery_id = grants.delivery_id AND original.stage = 'final'
        LEFT JOIN matrix_delivery_outbox AS acknowledgement
          ON acknowledgement.principal_id = grants.principal_id
         AND acknowledgement.delivery_id = 'approval-grant-revoked:' || grants.grant_id
         AND acknowledgement.stage = 'final'
        WHERE grants.principal_id = ? AND grants.resolution_json <> ''
          AND (? OR grants.grant_id = ?)
        ORDER BY grants.grant_id
        """,
        (principal_id, grant_id is None, grant_id),
    )
    deliveries = []
    now = time.time_ns()
    for row in rows:
        grant = _grant(row)
        if (
            not _row_current(transaction, principal_id, row)
            or (grant.revoked_at_ns is None and grant.expires_at_ns <= now)
            or row["revocation_event_id"] is not None
        ):
            transaction.execute(
                "UPDATE approval_grants SET resolution_json = '' WHERE principal_id = ? AND grant_id = ?",
                (principal_id, grant.grant_id),
            )
            continue
        if grant.revoked_at_ns is None or (row["original_id"] is not None and row["original_event_id"] is None):
            continue
        delivery_id = "approval-grant-revoked:" + grant.grant_id
        if row["revocation_id"] is not None:
            deliveries.append(delivery_id)
            continue
        content = json.loads(str(row["resolution_json"]))
        content["auto_approval"] = grant.wire()
        outbox.enqueue(
            transaction,
            principal_id,
            delivery_id=delivery_id,
            stage=DeliveryStage.FINAL,
            event_type="io.mindroom.tool_approval",
            room_id=grant.room_id,
            thread_id=grant.thread_id,
            membership_epoch=int(row["consent_epoch"]),
            payload=content,
            edits_event_id=grant.card_event_id,
        )
        deliveries.append(delivery_id)
    if grant_id is None:
        transaction.execute(
            """
            DELETE FROM matrix_delivery_outbox
            WHERE principal_id = ? AND stage = 'final' AND acknowledged_event_id IS NOT NULL
              AND EXISTS (
                  SELECT 1 FROM approval_grants AS grants
                  WHERE grants.principal_id = matrix_delivery_outbox.principal_id
                    AND 'approval-grant-revoked:' || grants.grant_id = matrix_delivery_outbox.delivery_id
                    AND grants.resolution_json = ''
              )
            """,
            (principal_id,),
        )
    return tuple(deliveries)
