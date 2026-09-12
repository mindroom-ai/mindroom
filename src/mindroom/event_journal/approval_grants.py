"""Atomic timed grants, exact-call matching, and recoverable revocation edits."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from mindroom.tool_approval_grants import (
    ApprovalGrant,
    ApprovalGrantRevocation,
    approval_timestamp,
    valid_auto_approve_seconds,
)

from . import approval_card_state, outbox
from .models import DeliveryStage

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .approval_card_state import ApprovalCardReservation, RecordedApprovalDecision
    from .approval_continuations import ApprovalContinuation
    from .backend import Row, Transaction


def lock(transaction: Transaction, principal_id: str) -> None:
    """Serialize grant changes and card reservation before continuation locks."""
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


def reserve_identity(
    transaction: Transaction,
    principal_id: str,
    continuation_principal_id: str,
    continuation: ApprovalContinuation,
    card: ApprovalCardReservation,
    membership_epoch: int,
) -> ApprovalCardReservation:
    """Persist eligible card scope independently of its eventual retirement."""
    if card.grant_operation is None or not continuation.thread_id:
        return card
    call = next(call for call in continuation.calls if call.tool_call_id == card.tool_call_id)
    responder_epoch = _epoch(transaction, continuation_principal_id, continuation.room_id)
    if responder_epoch is None:
        payload = dict(card.payload)
        payload.pop("approval_scope", None)
        payload.pop("auto_approve_options", None)
        return replace(card, payload=payload, grant_operation=None)
    scope = json.dumps(
        [
            continuation.room_id,
            continuation.thread_id,
            continuation.requester_id,
            call.invoking_agent,
            card.grant_operation.key,
            membership_epoch,
            continuation_principal_id,
            responder_epoch,
        ],
        separators=(",", ":"),
    )
    scope_id = hashlib.sha256(scope.encode()).hexdigest()
    scope_wire = card.grant_operation.scope_wire(scope_id, continuation.entity_name, call.invoking_agent)
    if card.payload.get("approval_scope") != {**scope_wire, "id": "0" * 64}:
        msg = "Prepared approval scope does not match its continuation"
        raise ValueError(msg)
    transaction.execute(
        """
        INSERT INTO approval_grant_cards (
            principal_id, delivery_id, scope_key, room_id, thread_id, requester_id,
            entity_name, invoking_agent, operation, responder_principal_id,
            responder_epoch, membership_epoch, continuation_id, continuation_generation, tool_call_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            principal_id,
            card.delivery_id,
            scope_id,
            continuation.room_id,
            continuation.thread_id,
            continuation.requester_id,
            continuation.entity_name,
            call.invoking_agent,
            card.grant_operation.key,
            continuation_principal_id,
            responder_epoch,
            membership_epoch,
            continuation.approval_id,
            continuation.generation,
            card.tool_call_id,
        ),
    )
    return replace(card, payload={**card.payload, "approval_scope": scope_wire})


def _current(transaction: Transaction, principal_id: str, row: Row) -> bool:
    return _epoch(transaction, principal_id, str(row["room_id"])) == int(row["membership_epoch"]) and _epoch(
        transaction,
        str(row["responder_principal_id"]),
        str(row["room_id"]),
    ) == int(row["responder_epoch"])


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


def for_card(transaction: Transaction, principal_id: str, *, room_id: str, card_event_id: str) -> ApprovalGrant | None:
    """Find a grant even after its original card and continuation have retired."""
    row = transaction.fetchone(
        "SELECT * FROM approval_grants WHERE principal_id = ? AND room_id = ? AND card_event_id = ?",
        (principal_id, room_id, card_event_id),
    )
    return None if row is None else _grant(row)


def _decide(
    transaction: Transaction,
    principal_id: str,
    row: Row,
    grant: ApprovalGrant,
    provenance: Mapping[str, Any],
    metadata: approval_card_state.ApprovalDecisionMetadata | None = None,
    reason: str | None = None,
) -> RecordedApprovalDecision:
    from . import approvals  # noqa: PLC0415 - grant reservation and exact-call decisions share a transaction

    decision = replace(
        metadata
        or approval_card_state.ApprovalDecisionMetadata(
            resolved_by=grant.requester_id,
            resolved_at=approval_timestamp(time.time_ns()),
        ),
        auto_approval=grant.wire() if metadata is not None else None,
        provenance=provenance,
    )
    result = approvals.resolve_card(
        transaction,
        principal_id,
        card_event_id=None if row["acknowledged_event_id"] is None else str(row["acknowledged_event_id"]),
        delivery_id=str(row["delivery_id"]),
        requested_status="approved",
        reason=reason,
        metadata=decision,
    )
    if result.recorded and result.resolution is not None and result.resolution["status"] == "approved":
        transaction.execute(
            "UPDATE approval_grant_cards SET grant_id = ? WHERE principal_id = ? AND delivery_id = ?",
            (grant.grant_id, principal_id, str(row["delivery_id"])),
        )
    return result


_CARD_SELECT = """
    SELECT scope.*, initial.payload_json, initial.acknowledged_event_id
    FROM approval_grant_cards AS scope
    JOIN approval_cards AS cards ON cards.principal_id = scope.principal_id AND cards.delivery_id = scope.delivery_id
    JOIN matrix_delivery_outbox AS initial
      ON initial.principal_id = scope.principal_id AND initial.delivery_id = scope.delivery_id AND initial.stage = 'initial'
    LEFT JOIN matrix_delivery_outbox AS final
      ON final.principal_id = scope.principal_id AND final.delivery_id = scope.delivery_id AND final.stage = 'final'
"""


def apply_active(transaction: Transaction, principal_id: str, *, card: ApprovalCardReservation) -> bool:
    """Approve a new call and atomically reserve its terminal-only receipt."""
    delivery_id = card.delivery_id
    row = transaction.fetchone(
        "SELECT * FROM approval_grant_cards WHERE principal_id = ? AND delivery_id = ?",
        (principal_id, delivery_id),
    )
    if row is None or not _current(transaction, principal_id, row):
        return False
    now = time.time_ns()
    grant_row = transaction.fetchone(
        """
        SELECT * FROM approval_grants
        WHERE principal_id = ? AND scope_key = ? AND revoked_at_ns IS NULL AND expires_at_ns > ?
        ORDER BY expires_at_ns DESC LIMIT 1
        """,
        (principal_id, str(row["scope_key"]), now),
    )
    if grant_row is None or not _current(transaction, principal_id, grant_row):
        return False
    decided = transaction.fetchone(
        """
        UPDATE approval_continuation_calls SET decision = 'approved'
        WHERE principal_id = ? AND approval_id = ? AND generation = ? AND tool_call_id = ?
          AND decision IS NULL AND expires_at_ns > ?
        RETURNING tool_call_id
        """,
        (
            row["responder_principal_id"],
            row["continuation_id"],
            row["continuation_generation"],
            row["tool_call_id"],
            now,
        ),
    )
    if decided is None:
        return False
    transaction.execute(
        "UPDATE approval_grant_cards SET grant_id = ? WHERE principal_id = ? AND delivery_id = ?",
        (grant_row["grant_id"], principal_id, delivery_id),
    )
    grant_resolution = approval_card_state.decode_object_payload(
        grant_row["resolution_json"],
        description="grant resolution",
    )
    receipt = approval_card_state.terminal_content(
        card.payload,
        status="approved",
        reason=None,
        metadata=approval_card_state.ApprovalDecisionMetadata(
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
        ),
        publication="receipt",
    )
    outbox.enqueue(
        transaction,
        principal_id,
        delivery_id=delivery_id,
        stage=DeliveryStage.INITIAL,
        event_type=card.event_type,
        room_id=str(row["room_id"]),
        thread_id=str(row["thread_id"]),
        membership_epoch=int(row["membership_epoch"]),
        payload=receipt,
        edits_event_id=None,
    )
    return True


def create(
    transaction: Transaction,
    principal_id: str,
    *,
    room_id: str,
    card_event_id: str,
    sender_id: str,
    seconds: int,
    metadata: approval_card_state.ApprovalDecisionMetadata,
    reason: str | None = None,
    current_binding: str | None = None,
) -> tuple[RecordedApprovalDecision, ...]:
    """Accept a grant and all eligible pending calls in one commit."""
    if not valid_auto_approve_seconds(seconds):
        return ()
    lock(transaction, principal_id)
    row = transaction.fetchone(
        _CARD_SELECT
        + " WHERE scope.principal_id = ? AND scope.room_id = ? AND initial.acknowledged_event_id = ? AND final.delivery_id IS NULL",
        (principal_id, room_id, card_event_id),
    )
    if (
        row is None
        or str(row["requester_id"]) != sender_id
        or not _current(transaction, principal_id, row)
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
    first = _decide(transaction, principal_id, row, grant, provenance, metadata, reason)
    if not first.recorded or first.resolution is None or first.resolution["status"] != "approved":
        return (first,)
    transaction.execute(
        """
        INSERT INTO approval_grants (
            principal_id, grant_id, scope_key, room_id, thread_id, card_event_id,
            requester_id, entity_name, invoking_agent, operation, responder_principal_id,
            responder_epoch, membership_epoch, expires_at_ns, resolution_json, original_delivery_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            principal_id,
            grant.grant_id,
            row["scope_key"],
            room_id,
            grant.thread_id,
            card_event_id,
            sender_id,
            grant.entity_name,
            grant.invoking_agent,
            grant.operation,
            row["responder_principal_id"],
            row["responder_epoch"],
            row["membership_epoch"],
            grant.expires_at_ns,
            json.dumps(first.resolution),
            row["delivery_id"],
        ),
    )
    candidates = transaction.fetchall(
        _CARD_SELECT
        + " WHERE scope.principal_id = ? AND scope.scope_key = ? AND final.delivery_id IS NULL ORDER BY scope.delivery_id",
        (principal_id, row["scope_key"]),
    )
    return (first, *(_decide(transaction, principal_id, candidate, grant, provenance) for candidate in candidates))


def revoke(
    transaction: Transaction,
    principal_id: str,
    *,
    room_id: str,
    card_event_id: str,
    sender_id: str,
    grant_id: str,
) -> ApprovalGrantRevocation | None:
    """Record revocation debt without rewriting the original FINAL delivery."""
    lock(transaction, principal_id)
    row = transaction.fetchone(
        "SELECT * FROM approval_grants WHERE principal_id = ? AND grant_id = ? AND room_id = ? AND card_event_id = ?",
        (principal_id, grant_id, room_id, card_event_id),
    )
    if row is None or row["requester_id"] != sender_id or not _current(transaction, principal_id, row):
        return None
    grant = _grant(row)
    revocation = ApprovalGrantRevocation(str(row["original_delivery_id"]), "approval-grant-revoked:" + grant_id)
    if grant.revoked_at_ns is not None:
        return revocation
    now = time.time_ns()
    if grant.expires_at_ns <= now:
        return None
    grant = replace(grant, revoked_at_ns=now)
    transaction.execute(
        "UPDATE approval_grants SET revoked_at_ns = ? WHERE principal_id = ? AND grant_id = ?",
        (grant.revoked_at_ns, principal_id, grant_id),
    )
    return revocation


def _retire_receipts(transaction: Transaction, principal_id: str) -> None:
    """Release acknowledged terminal originals while retaining their action identity."""
    transaction.execute(
        """
        INSERT INTO approval_action_tombstones (principal_id, room_id, card_event_id)
        SELECT initial.principal_id, initial.room_id, initial.acknowledged_event_id
        FROM matrix_delivery_outbox AS initial
        JOIN approval_grant_cards AS audit
          ON audit.principal_id = initial.principal_id AND audit.delivery_id = initial.delivery_id
        WHERE initial.principal_id = ? AND initial.stage = 'initial'
          AND initial.acknowledged_event_id IS NOT NULL AND audit.grant_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM approval_cards AS cards
              WHERE cards.principal_id = initial.principal_id AND cards.delivery_id = initial.delivery_id
          )
        ON CONFLICT (principal_id, card_event_id) DO NOTHING
        """,
        (principal_id,),
    )
    transaction.execute(
        """
        DELETE FROM matrix_delivery_outbox
        WHERE principal_id = ? AND stage = 'initial'
          AND EXISTS (
              SELECT 1 FROM approval_grant_cards AS audit
              WHERE audit.principal_id = matrix_delivery_outbox.principal_id
                AND audit.delivery_id = matrix_delivery_outbox.delivery_id AND audit.grant_id IS NOT NULL
          )
          AND EXISTS (
              SELECT 1 FROM approval_action_tombstones AS terminal
              WHERE terminal.principal_id = matrix_delivery_outbox.principal_id
                AND terminal.room_id = matrix_delivery_outbox.room_id
                AND terminal.card_event_id = matrix_delivery_outbox.acknowledged_event_id
          )
          AND NOT EXISTS (
              SELECT 1 FROM approval_cards AS cards
              WHERE cards.principal_id = matrix_delivery_outbox.principal_id
                AND cards.delivery_id = matrix_delivery_outbox.delivery_id
          )
        """,
        (principal_id,),
    )


def maintain(transaction: Transaction, principal_id: str, *, grant_id: str | None = None) -> tuple[str, ...]:
    """Release spent grant payloads and prepare newly deliverable revocations.

    Grant identity and applied-call audit facts survive payload retirement.
    Revoked grants retain acknowledgement debt past expiry until delivery is
    acknowledged, or membership ends. The original FINAL must settle first so
    a delayed acceptance cannot overwrite the later revoked state.
    """
    lock(transaction, principal_id)
    if grant_id is None:
        _retire_receipts(transaction, principal_id)
        transaction.execute(
            """
            DELETE FROM approval_grant_cards
            WHERE principal_id = ? AND grant_id IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM approval_cards AS cards
                  WHERE cards.principal_id = approval_grant_cards.principal_id
                    AND cards.delivery_id = approval_grant_cards.delivery_id
              )
            """,
            (principal_id,),
        )
    rows = transaction.fetchall(
        """
        SELECT grants.*, original.delivery_id AS original_id,
               original.acknowledged_event_id AS original_event_id,
               acknowledgement.delivery_id AS revocation_id,
               acknowledgement.acknowledged_event_id AS revocation_event_id
        FROM approval_grants AS grants
        LEFT JOIN matrix_delivery_outbox AS original
          ON original.principal_id = grants.principal_id
         AND original.delivery_id = grants.original_delivery_id AND original.stage = 'final'
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
            not _current(transaction, principal_id, row)
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
            membership_epoch=int(row["membership_epoch"]),
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
