"""Approval-domain ownership linked to generic durable Matrix deliveries.

This module owns exact-call decisions, continuation relationships, deadlines,
and action tombstones. Transaction IDs, frozen event payloads, attempts,
devices, acknowledgements, retries, and terminal edits live exclusively in
``matrix_delivery_outbox``.
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

from . import approval_continuations, outbox, reads
from .identity import decode_thread_id, encode_thread_id
from .models import DeliveryStage

_DEFAULT_ROOM_CARD_LIMIT = 256
logger = get_logger(__name__)
_CARD_COLUMNS = """
    cards.delivery_id AS delivery_id,
    initial.event_type AS event_type, initial.payload_json AS card_json, final.payload_json AS resolution_json,
    initial.acknowledged_event_id AS card_event_id,
    initial.created_at_ns AS created_at_ns,
    cards.continuation_id AS continuation_id,
    cards.continuation_generation AS continuation_generation,
    cards.tool_call_id AS tool_call_id
"""
_CARD_DELIVERY_JOINS = """
    JOIN matrix_delivery_outbox AS initial
      ON initial.principal_id = cards.principal_id
     AND initial.delivery_id = cards.delivery_id
     AND initial.stage = 'initial'
    LEFT JOIN matrix_delivery_outbox AS final
      ON final.principal_id = cards.principal_id
     AND final.delivery_id = cards.delivery_id
     AND final.stage = 'final'
"""
_TIMEOUT_REASON = "Tool approval request timed out."


@dataclass(frozen=True, slots=True)
class StoredApprovalCard:
    """One recorded card, and the decision it is already carrying if any."""

    card: dict[str, Any]
    # None while the card is genuinely unanswered. Once set, the decision was
    # made and only its delivery is in doubt.
    resolution: dict[str, Any] | None
    delivery_id: str
    card_event_id: str | None
    created_at_ns: int
    continuation_id: str
    continuation_generation: int
    tool_call_id: str


@dataclass(frozen=True, slots=True)
class LegacyApprovalDelivery:
    """One unacknowledged #1834 card awaiting one-way wire normalization."""

    delivery_id: str
    room_id: str
    thread_id: str | None
    payload: dict[str, Any]
    attempted: bool
    sending_device_id: str | None
    migration_owner: str
    migration_generation: int


@dataclass(frozen=True, slots=True)
class ApprovalCardReservation:
    """One exact-call approval card and its frozen Matrix payload."""

    delivery_id: str
    tool_call_id: str
    event_type: str
    payload: Mapping[str, object]


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


def legacy_delivery_pending(transaction: Transaction, principal_id: str, continuation_id: str | None = None) -> bool:
    """Return whether this principal still owns matching #1834 delivery migration debt."""
    return (
        transaction.fetchone(
            """
            SELECT 1 AS present FROM approval_cards_legacy_delivery
            WHERE principal_id = ?
              AND continuation_id IS NOT NULL
              AND continuation_generation IS NOT NULL
              AND tool_call_id IS NOT NULL
              AND continuation_id = COALESCE(?, continuation_id)
            LIMIT 1
            """,
            (principal_id, continuation_id),
        )
        is not None
    )


def claim_legacy_delivery(
    transaction: Transaction,
    principal_id: str,
    *,
    owner: str,
    now_ns: int,
    lease_until_ns: int,
) -> LegacyApprovalDelivery | None:
    """Lease the oldest available legacy row for one cross-process upgrader."""
    row = transaction.fetchone(
        """
        UPDATE approval_cards_legacy_delivery
        SET migration_owner = ?,
            migration_generation = migration_generation + 1,
            migration_lease_until_ns = ?
        WHERE principal_id = ?
          AND transaction_id = (
              SELECT transaction_id FROM approval_cards_legacy_delivery
              WHERE principal_id = ?
                AND continuation_id IS NOT NULL
                AND continuation_generation IS NOT NULL
                AND tool_call_id IS NOT NULL
                AND (migration_owner IS NULL OR migration_lease_until_ns <= ?)
              ORDER BY created_at_ns, transaction_id/*bytes*/
              LIMIT 1
          )
          AND (migration_owner IS NULL OR migration_lease_until_ns <= ?)
        RETURNING transaction_id, room_id, card_json, attempted,
                  sending_device_id, migration_generation
        """,
        (owner, lease_until_ns, principal_id, principal_id, now_ns, now_ns),
    )
    if row is None:
        return None
    return _legacy_delivery(
        row,
        migration_owner=owner,
        migration_generation=int(row["migration_generation"]),
    )


def release_legacy_delivery(
    transaction: Transaction,
    principal_id: str,
    *,
    claim: LegacyApprovalDelivery,
) -> None:
    """Release one exact migration lease without disturbing a newer claimant."""
    transaction.execute(
        """
        UPDATE approval_cards_legacy_delivery
        SET migration_owner = NULL, migration_lease_until_ns = NULL
        WHERE principal_id = ? AND transaction_id = ?
          AND migration_owner = ? AND migration_generation = ?
        """,
        (principal_id, claim.delivery_id, claim.migration_owner, claim.migration_generation),
    )


def promote_legacy_delivery(
    transaction: Transaction,
    principal_id: str,
    *,
    delivery_id: str,
    payload: Mapping[str, object],
    acknowledged_event_id: str | None,
    claim: LegacyApprovalDelivery | None = None,
) -> bool:
    """Atomically move one normalized #1834 card into generic ownership."""
    claim_clause = "" if claim is None else " AND migration_owner = ? AND migration_generation = ?"
    claim_params: tuple[object, ...] = () if claim is None else (claim.migration_owner, claim.migration_generation)
    row = transaction.fetchone(
        f"""
        DELETE FROM approval_cards_legacy_delivery
        WHERE principal_id = ? AND transaction_id = ?{claim_clause}
        RETURNING room_id, transaction_id, card_json, resolution_json, attempted,
                  sending_device_id, continuation_id, continuation_generation,
                  tool_call_id, membership_epoch, created_at_ns
        """,  # noqa: S608 - fixed claim clause only
        (principal_id, delivery_id, *claim_params),
    )
    if row is None:
        return False
    card = _object_json(row["card_json"], description="legacy approval card")
    event_type = card.get("type")
    if not isinstance(event_type, str) or not event_type:
        event_type = "io.mindroom.tool_approval"
    normalized_payload = dict(payload)
    normalized_identity = _native_identity({"content": normalized_payload})
    stored_identity = (
        str(row["continuation_id"]),
        int(row["continuation_generation"]),
        str(row["tool_call_id"]),
    )
    if normalized_identity != stored_identity:
        msg = f"Legacy approval delivery {delivery_id!r} changed exact-call identity"
        raise ValueError(msg)
    thread_id = normalized_payload.get("thread_id")
    transaction.execute(
        """
        INSERT INTO matrix_delivery_outbox (
            principal_id, delivery_id, stage, event_type, room_id, thread_id,
            transaction_id, payload_json, edits_event_id, edit_target_pending,
            attempted, sending_device_id, acknowledged_event_id, created_at_ns
        ) VALUES (?, ?, 'initial', ?, ?, ?, ?, ?, NULL, 0, ?, ?, ?, ?)
        ON CONFLICT (principal_id, delivery_id, stage) DO NOTHING
        """,
        (
            principal_id,
            delivery_id,
            event_type,
            str(row["room_id"]),
            encode_thread_id(thread_id if isinstance(thread_id, str) and thread_id else None),
            str(row["transaction_id"]),
            _encoded(normalized_payload),
            int(row["attempted"]),
            row["sending_device_id"],
            acknowledged_event_id,
            int(row["created_at_ns"]),
        ),
    )
    transaction.execute(
        """
        INSERT INTO approval_cards (
            principal_id, delivery_id, continuation_id,
            continuation_generation, tool_call_id, membership_epoch
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (principal_id, delivery_id) DO NOTHING
        """,
        (principal_id, delivery_id, *stored_identity, int(row["membership_epoch"])),
    )
    if row["resolution_json"] is not None:
        resolution = _object_json(row["resolution_json"], description="legacy approval resolution")
        for transport_key in (
            "full_arguments",
            "full_arguments_file",
            "full_arguments_info",
            "full_arguments_url",
            "m.relates_to",
        ):
            resolution.pop(transport_key, None)
        outbox.enqueue(
            transaction,
            principal_id,
            delivery_id=delivery_id,
            stage=DeliveryStage.FINAL,
            event_type=event_type,
            room_id=str(row["room_id"]),
            thread_id=thread_id if isinstance(thread_id, str) and thread_id else None,
            payload=resolution,
            edits_event_id=acknowledged_event_id,
            edit_target_pending=acknowledged_event_id is None,
        )
    return True


def _legacy_delivery(
    row: Row,
    *,
    migration_owner: str,
    migration_generation: int,
) -> LegacyApprovalDelivery:
    card = _object_json(row["card_json"], description="legacy approval card")
    content = card.get("content")
    if not isinstance(content, dict):
        msg = "Legacy approval card content is not an object"
        raise TypeError(msg)
    thread_id = content.get("thread_id")
    return LegacyApprovalDelivery(
        delivery_id=str(row["transaction_id"]),
        room_id=str(row["room_id"]),
        thread_id=thread_id if isinstance(thread_id, str) and thread_id else None,
        payload=content,
        attempted=bool(row["attempted"]),
        sending_device_id=None if row["sending_device_id"] is None else str(row["sending_device_id"]),
        migration_owner=migration_owner,
        migration_generation=migration_generation,
    )


def _object_json(value: object, *, description: str) -> dict[str, Any]:
    decoded = json.loads(str(value))
    if not isinstance(decoded, dict):
        msg = f"Stored {description} is not an object"
        raise TypeError(msg)
    return decoded


def _encoded(value: Mapping[str, object]) -> str:
    return json.dumps(dict(value), ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def reserve_deliveries(
    transaction: Transaction,
    card_principal_id: str,
    *,
    continuation_principal_id: str,
    continuation_id: str,
    expected_generation: int,
    cards: tuple[ApprovalCardReservation, ...],
) -> bool:
    """Atomically own every exact call, frozen card, and publication lease."""
    # Reservation and a failure fence can arrive through different processes.
    # Lock the aggregate before reading its publication lease so either every
    # card and activation wins, or the failure wins without orphan deliveries.
    transaction.execute(
        """
        UPDATE approval_continuations SET state = state
        WHERE principal_id = ? AND approval_id = ?
        """,
        (continuation_principal_id, continuation_id),
    )
    continuation = approval_continuations.get(
        transaction,
        continuation_principal_id,
        approval_id=continuation_id,
    )
    if (
        continuation is None
        or continuation.state != "waiting"
        or continuation.generation != expected_generation
        or continuation.runtime_generation is None
        or not cards
    ):
        return False
    undecided = transaction.fetchall(
        """
        SELECT tool_call_id FROM approval_continuation_calls
        WHERE principal_id = ? AND approval_id = ? AND generation = ? AND decision IS NULL
        ORDER BY call_ordinal
        """,
        (continuation_principal_id, continuation_id, expected_generation),
    )
    required_call_ids = tuple(str(row["tool_call_id"]) for row in undecided)
    reserved_call_ids = tuple(card.tool_call_id for card in cards)
    if len(set(reserved_call_ids)) != len(reserved_call_ids) or set(reserved_call_ids) != set(required_call_ids):
        return False
    epoch = transaction.fetchone(
        "SELECT membership_epoch FROM room_membership WHERE principal_id = ? AND room_id = ?",
        (card_principal_id, continuation.room_id),
    )
    membership_epoch = 0 if epoch is None else int(epoch["membership_epoch"])
    if not reads.claim_membership_epoch(
        transaction,
        card_principal_id,
        room_id=continuation.room_id,
        expected_membership_epoch=membership_epoch,
    ):
        return False
    for card in cards:
        outbox.enqueue(
            transaction,
            card_principal_id,
            delivery_id=card.delivery_id,
            stage=DeliveryStage.INITIAL,
            event_type=card.event_type,
            room_id=continuation.room_id,
            thread_id=continuation.thread_id,
            payload=card.payload,
            edits_event_id=None,
        )
        transaction.execute(
            """
            INSERT INTO approval_cards (
                principal_id, delivery_id, continuation_id,
                continuation_generation, tool_call_id, membership_epoch
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                card_principal_id,
                card.delivery_id,
                continuation_id,
                expected_generation,
                card.tool_call_id,
                membership_epoch,
            ),
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
        SELECT cards.delivery_id, cards.continuation_id, cards.continuation_generation,
               cards.tool_call_id, initial.event_type, initial.room_id, initial.thread_id
        FROM approval_cards AS cards
        JOIN matrix_delivery_outbox AS initial
          ON initial.principal_id = cards.principal_id
         AND initial.delivery_id = cards.delivery_id
         AND initial.stage = 'initial'
        WHERE cards.principal_id = ? AND initial.acknowledged_event_id = ?
        """,
        (principal_id, card_event_id),
    )
    if card is None:
        return RecordedApprovalDecision(resolution=None, recorded=False)
    continuation_id = cast("str", card["continuation_id"])
    generation_value = card["continuation_generation"]
    tool_call_id = cast("str", card["tool_call_id"])
    # A failure fence and a click can arrive through different processes. Lock
    # their shared aggregate before trusting its state, then refresh the final
    # row in case another decision committed while this transaction waited.
    transaction.execute(
        "UPDATE approval_continuations SET state = state WHERE approval_id = ?",
        (continuation_id,),
    )
    settled = transaction.fetchone(
        """
        SELECT payload_json FROM matrix_delivery_outbox
        WHERE principal_id = ? AND delivery_id = ? AND stage = 'final'
        """,
        (principal_id, str(card["delivery_id"])),
    )
    existing = _resolution(None if settled is None else cast("str", settled["payload_json"]))
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
    outbox.enqueue(
        transaction,
        principal_id,
        delivery_id=str(card["delivery_id"]),
        stage=DeliveryStage.FINAL,
        event_type=str(card["event_type"]),
        room_id=str(card["room_id"]),
        thread_id=decode_thread_id(str(card["thread_id"])),
        payload=stored_resolution,
        edits_event_id=card_event_id,
    )
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


def retire_completed_cards_for_departure(
    transaction: Transaction,
    principal_id: str,
    *,
    room_id: str,
) -> None:
    """Finish domain retirement that crashed after a terminal Matrix acknowledgement."""
    rows = transaction.fetchall(
        """
        SELECT cards.principal_id, cards.delivery_id, initial.acknowledged_event_id
        FROM approval_cards AS cards
        JOIN matrix_delivery_outbox AS initial
          ON initial.principal_id = cards.principal_id
         AND initial.delivery_id = cards.delivery_id
         AND initial.stage = 'initial'
        JOIN matrix_delivery_outbox AS final
          ON final.principal_id = cards.principal_id
         AND final.delivery_id = cards.delivery_id
         AND final.stage = 'final'
        LEFT JOIN approval_continuations AS continuations
          ON continuations.approval_id = cards.continuation_id
        WHERE initial.acknowledged_event_id IS NOT NULL
          AND final.acknowledged_event_id IS NOT NULL
          AND (
              (cards.principal_id = ? AND initial.room_id = ?)
              OR (
                  continuations.principal_id = ?
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
              )
          )
        """,
        (principal_id, room_id, principal_id, room_id),
    )
    for row in rows:
        retire(
            transaction,
            str(row["principal_id"]),
            delivery_id=str(row["delivery_id"]),
            card_event_id=str(row["acknowledged_event_id"]),
        )


def expire_cards_for_departed_continuations(
    transaction: Transaction,
    continuation_principal_id: str,
    *,
    room_id: str,
    reason: str,
) -> None:
    """Preserve router-owned Matrix cleanup when a responder leaves the room."""
    _expire_legacy_cards_for_departed_continuations(
        transaction,
        continuation_principal_id,
        room_id=room_id,
        reason=reason,
    )
    rows = transaction.fetchall(
        """
        SELECT cards.principal_id, cards.delivery_id, initial.event_type,
               initial.room_id, initial.thread_id, initial.payload_json,
               initial.attempted, initial.acknowledged_event_id,
               final.delivery_id AS final_delivery_id
        FROM approval_cards AS cards
        JOIN approval_continuations AS continuations
          ON continuations.approval_id = cards.continuation_id
        JOIN matrix_delivery_outbox AS initial
          ON initial.principal_id = cards.principal_id
         AND initial.delivery_id = cards.delivery_id
         AND initial.stage = 'initial'
        LEFT JOIN matrix_delivery_outbox AS final
          ON final.principal_id = cards.principal_id
         AND final.delivery_id = cards.delivery_id
         AND final.stage = 'final'
        WHERE continuations.principal_id = ?
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
        card_principal_id = str(row["principal_id"])
        delivery_id = str(row["delivery_id"])
        if not bool(row["attempted"]):
            _delete_unattempted_card_delivery(transaction, card_principal_id, delivery_id)
            continue
        if row["final_delivery_id"] is not None:
            continue
        try:
            content = _object_json(row["payload_json"], description="approval payload")
        except (json.JSONDecodeError, TypeError):
            continue
        resolution = _terminal_content(content, status="expired", reason=reason)
        outbox.enqueue(
            transaction,
            card_principal_id,
            delivery_id=delivery_id,
            stage=DeliveryStage.FINAL,
            event_type=str(row["event_type"]),
            room_id=str(row["room_id"]),
            thread_id=decode_thread_id(str(row["thread_id"])),
            payload=resolution,
            edits_event_id=None if row["acknowledged_event_id"] is None else str(row["acknowledged_event_id"]),
            edit_target_pending=row["acknowledged_event_id"] is None,
        )


def fail_continuations_for_departed_card_owner(
    transaction: Transaction,
    card_principal_id: str,
    *,
    room_id: str,
    reason: str,
) -> None:
    """Fail unresolved pauses and preserve attempted cards through terminal recovery."""
    _fail_legacy_cards_for_departed_owner(
        transaction,
        card_principal_id,
        room_id=room_id,
        reason=reason,
    )
    transaction.execute(
        """
        UPDATE approval_continuations
        SET state = 'failing', failure_reason = ?, runtime_generation = NULL
        WHERE state IN ('waiting', 'ready')
          AND EXISTS (
              SELECT 1 FROM approval_cards AS cards
              JOIN matrix_delivery_outbox AS initial
                ON initial.principal_id = cards.principal_id
               AND initial.delivery_id = cards.delivery_id
               AND initial.stage = 'initial'
              LEFT JOIN matrix_delivery_outbox AS final
                ON final.principal_id = cards.principal_id
               AND final.delivery_id = cards.delivery_id
               AND final.stage = 'final'
              WHERE cards.principal_id = ? AND initial.room_id = ?
                AND cards.continuation_id = approval_continuations.approval_id
                AND final.delivery_id IS NULL
          )
        """,
        (reason, card_principal_id, room_id),
    )
    rows = transaction.fetchall(
        """
        SELECT cards.delivery_id, cards.continuation_id, cards.continuation_generation,
               cards.tool_call_id, initial.event_type, initial.room_id, initial.thread_id,
               initial.payload_json, initial.attempted, initial.acknowledged_event_id,
               membership.membership_epoch, final.delivery_id AS final_delivery_id
        FROM approval_cards AS cards
        JOIN matrix_delivery_outbox AS initial
          ON initial.principal_id = cards.principal_id
         AND initial.delivery_id = cards.delivery_id
         AND initial.stage = 'initial'
        JOIN room_membership AS membership
          ON membership.principal_id = cards.principal_id
         AND membership.room_id = initial.room_id
        LEFT JOIN matrix_delivery_outbox AS final
          ON final.principal_id = cards.principal_id
         AND final.delivery_id = cards.delivery_id
         AND final.stage = 'final'
        WHERE cards.principal_id = ? AND initial.room_id = ?
        """,
        (card_principal_id, room_id),
    )
    for row in rows:
        delivery_id = str(row["delivery_id"])
        if row["final_delivery_id"] is None:
            _deny_call_if_undecided(transaction, row, reason=reason)
        if not bool(row["attempted"]):
            _delete_unattempted_card_delivery(transaction, card_principal_id, delivery_id)
            continue
        if row["final_delivery_id"] is not None:
            transaction.execute(
                """
                UPDATE approval_cards SET membership_epoch = ?
                WHERE principal_id = ? AND delivery_id = ?
                """,
                (int(row["membership_epoch"]), card_principal_id, delivery_id),
            )
            continue
        try:
            content = _object_json(row["payload_json"], description="approval payload")
        except (json.JSONDecodeError, TypeError):
            continue
        resolution = _terminal_content(content, status="denied", reason=reason)
        outbox.enqueue(
            transaction,
            card_principal_id,
            delivery_id=delivery_id,
            stage=DeliveryStage.FINAL,
            event_type=str(row["event_type"]),
            room_id=str(row["room_id"]),
            thread_id=decode_thread_id(str(row["thread_id"])),
            payload=resolution,
            edits_event_id=(None if row["acknowledged_event_id"] is None else str(row["acknowledged_event_id"])),
            edit_target_pending=row["acknowledged_event_id"] is None,
        )
        transaction.execute(
            """
            UPDATE approval_cards SET membership_epoch = ?
            WHERE principal_id = ? AND delivery_id = ?
            """,
            (int(row["membership_epoch"]), card_principal_id, delivery_id),
        )


def _delete_unattempted_card_delivery(
    transaction: Transaction,
    principal_id: str,
    delivery_id: str,
) -> None:
    """Retire a card and every stage that provably never reached Matrix."""
    transaction.execute(
        "DELETE FROM approval_cards WHERE principal_id = ? AND delivery_id = ?",
        (principal_id, delivery_id),
    )
    transaction.execute(
        """
        DELETE FROM matrix_delivery_outbox
        WHERE principal_id = ? AND delivery_id = ? AND attempted = 0
        """,
        (principal_id, delivery_id),
    )


def _expire_legacy_cards_for_departed_continuations(
    transaction: Transaction,
    continuation_principal_id: str,
    *,
    room_id: str,
    reason: str,
) -> None:
    """Keep pre-upgrade attempted cards terminalizable after their continuation is deleted."""
    rows = transaction.fetchall(
        """
        SELECT legacy.principal_id, legacy.transaction_id, legacy.attempted,
               legacy.card_json, legacy.resolution_json
        FROM approval_cards_legacy_delivery AS legacy
        JOIN approval_continuations AS continuations
          ON continuations.approval_id = legacy.continuation_id
        WHERE continuations.principal_id = ?
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
        principal_id = str(row["principal_id"])
        delivery_id = str(row["transaction_id"])
        if not bool(row["attempted"]):
            transaction.execute(
                "DELETE FROM approval_cards_legacy_delivery WHERE principal_id = ? AND transaction_id = ?",
                (principal_id, delivery_id),
            )
            continue
        if row["resolution_json"] is None:
            resolution = _legacy_terminal_content(row["card_json"], status="expired", reason=reason)
            transaction.execute(
                """
                UPDATE approval_cards_legacy_delivery SET resolution_json = ?
                WHERE principal_id = ? AND transaction_id = ? AND resolution_json IS NULL
                """,
                (_encoded(resolution), principal_id, delivery_id),
            )


def _fail_legacy_cards_for_departed_owner(
    transaction: Transaction,
    card_principal_id: str,
    *,
    room_id: str,
    reason: str,
) -> None:
    """Fence pre-upgrade card decisions and retain only sends Matrix may have accepted."""
    transaction.execute(
        """
        UPDATE approval_continuations
        SET state = 'failing', failure_reason = ?, runtime_generation = NULL
        WHERE state IN ('waiting', 'ready')
          AND EXISTS (
              SELECT 1 FROM approval_cards_legacy_delivery AS legacy
              WHERE legacy.principal_id = ? AND legacy.room_id = ?
                AND legacy.continuation_id = approval_continuations.approval_id
          )
        """,
        (reason, card_principal_id, room_id),
    )
    rows = transaction.fetchall(
        """
        SELECT transaction_id, attempted, card_json, resolution_json,
               continuation_id, continuation_generation, tool_call_id,
               membership.membership_epoch
        FROM approval_cards_legacy_delivery AS legacy
        JOIN room_membership AS membership
          ON membership.principal_id = legacy.principal_id
         AND membership.room_id = legacy.room_id
        WHERE legacy.principal_id = ? AND legacy.room_id = ?
          AND legacy.continuation_id IS NOT NULL
        """,
        (card_principal_id, room_id),
    )
    for row in rows:
        delivery_id = str(row["transaction_id"])
        if row["resolution_json"] is None:
            _deny_call_if_undecided(transaction, row, reason=reason)
        if not bool(row["attempted"]):
            transaction.execute(
                "DELETE FROM approval_cards_legacy_delivery WHERE principal_id = ? AND transaction_id = ?",
                (card_principal_id, delivery_id),
            )
            continue
        resolution_json = row["resolution_json"]
        if resolution_json is None:
            resolution = _legacy_terminal_content(row["card_json"], status="denied", reason=reason)
            resolution_json = _encoded(resolution)
        transaction.execute(
            """
            UPDATE approval_cards_legacy_delivery
            SET resolution_json = ?, membership_epoch = ?
            WHERE principal_id = ? AND transaction_id = ?
            """,
            (resolution_json, int(row["membership_epoch"]), card_principal_id, delivery_id),
        )


def _deny_call_if_undecided(transaction: Transaction, row: Row, *, reason: str) -> None:
    transaction.execute(
        """
        UPDATE approval_continuation_calls SET decision = 'denied', reason = ?
        WHERE approval_id = ? AND generation = ? AND tool_call_id = ? AND decision IS NULL
        """,
        (reason, row["continuation_id"], row["continuation_generation"], row["tool_call_id"]),
    )


def _legacy_terminal_content(
    stored_card: object,
    *,
    status: Literal["denied", "expired"],
    reason: str,
) -> dict[str, Any]:
    card = _object_json(stored_card, description="legacy approval card")
    content = card.get("content")
    if not isinstance(content, dict):
        msg = "Legacy approval card content is not an object"
        raise TypeError(msg)
    return _terminal_content(content, status=status, reason=reason)


def _terminal_content(
    content: Mapping[str, Any],
    *,
    status: Literal["denied", "expired"],
    reason: str,
) -> dict[str, Any]:
    resolution = {
        **content,
        "status": status,
        "approvable": False,
        "resolution_reason": reason,
        "resolved_by": None,
    }
    tool_name = resolution.get("tool_name")
    resolution["body"] = f"{status.title()}: {tool_name}" if isinstance(tool_name, str) else f"Approval {status}"
    return resolution


def retire(
    transaction: Transaction,
    principal_id: str,
    *,
    delivery_id: str,
    card_event_id: str,
) -> bool:
    """Retire delivered card payload while keeping its shared approval-only identity."""
    remembered = transaction.fetchone(
        """
        INSERT INTO approval_action_tombstones (principal_id, room_id, card_event_id)
        SELECT cards.principal_id, initial.room_id, initial.acknowledged_event_id
        FROM approval_cards AS cards
        JOIN matrix_delivery_outbox AS initial
          ON initial.principal_id = cards.principal_id
         AND initial.delivery_id = cards.delivery_id
         AND initial.stage = 'initial'
        JOIN matrix_delivery_outbox AS final
          ON final.principal_id = cards.principal_id
         AND final.delivery_id = cards.delivery_id
         AND final.stage = 'final'
        WHERE cards.principal_id = ? AND cards.delivery_id = ?
          AND initial.acknowledged_event_id = ?
          AND final.acknowledged_event_id IS NOT NULL
        ON CONFLICT (principal_id, card_event_id) DO NOTHING
        RETURNING card_event_id
        """,
        (principal_id, delivery_id, card_event_id),
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
        "DELETE FROM approval_cards WHERE principal_id = ? AND delivery_id = ?",
        (principal_id, delivery_id),
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
        {_CARD_DELIVERY_JOINS}
        LEFT JOIN room_membership AS membership
          ON membership.principal_id = cards.principal_id
         AND membership.room_id = initial.room_id
        WHERE cards.principal_id = ?
          AND initial.room_id = ?
          AND initial.acknowledged_event_id = ?
          AND cards.membership_epoch = COALESCE(membership.membership_epoch, 0)
          AND COALESCE(membership.departure_fenced, 0) = 0
        """,  # noqa: S608 - a fixed column list, not interpolated input
        (principal_id, room_id, card_event_id),
    )
    return None if row is None else _card(row)


def pending_room_ids(transaction: Transaction, principal_id: str) -> tuple[str, ...]:
    """Return every current-membership room with recoverable approval cards."""
    rows = transaction.fetchall(
        """
        SELECT DISTINCT initial.room_id/*bytes*/ AS room_id
        FROM approval_cards AS cards
        JOIN matrix_delivery_outbox AS initial
          ON initial.principal_id = cards.principal_id
         AND initial.delivery_id = cards.delivery_id
         AND initial.stage = 'initial'
        LEFT JOIN room_membership AS membership
          ON membership.principal_id = cards.principal_id
         AND membership.room_id = initial.room_id
        WHERE cards.principal_id = ?
          AND cards.membership_epoch = COALESCE(membership.membership_epoch, 0)
          AND COALESCE(membership.departure_fenced, 0) = 0
        ORDER BY initial.room_id/*bytes*/
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
            "" if scan_after is None else " AND (initial.created_at_ns, initial.delivery_id/*bytes*/) > (?, ?)"
        )
        cursor_params: tuple[object, ...] = () if scan_after is None else scan_after
        raw_limit = limit - len(cards)
        rows = transaction.fetchall(
            f"""
            SELECT {_CARD_COLUMNS}
            FROM approval_cards AS cards
            {_CARD_DELIVERY_JOINS}
            LEFT JOIN room_membership AS membership
              ON membership.principal_id = cards.principal_id
             AND membership.room_id = initial.room_id
            WHERE cards.principal_id = ?
              AND initial.room_id = ?
              AND cards.membership_epoch = COALESCE(membership.membership_epoch, 0){cursor_clause}
              AND COALESCE(membership.departure_fenced, 0) = 0
            -- Two cards sent in the same nanosecond would otherwise come back in
            -- whatever order each backend felt like, and the caller expires them
            -- in the order it reads them.
            ORDER BY initial.created_at_ns, initial.delivery_id/*bytes*/
            LIMIT ?
            """,  # noqa: S608 - a fixed column list and a fixed clause, not input
            (principal_id, room_id, *cursor_params, raw_limit),
        )
        if not rows:
            break
        last = rows[-1]
        scan_after = (int(last["created_at_ns"]), str(last["delivery_id"]))
        cards.extend(card for row in rows if (card := _card(row)) is not None)
        if len(rows) < raw_limit:
            break
    return tuple(cards)


def _card(row: Row) -> StoredApprovalCard | None:
    """Decode one durable native card, skipping corrupt rows fail-closed."""
    try:
        content = json.loads(row["card_json"])
    except (json.JSONDecodeError, TypeError):
        _log_unreadable_card(row)
        return None
    if not isinstance(content, dict):
        _log_unreadable_card(row)
        return None
    card: dict[str, Any] = {
        "type": str(row["event_type"]),
        "content": content,
    }
    if row["card_event_id"] is not None:
        card["event_id"] = str(row["card_event_id"])
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
        delivery_id=str(row["delivery_id"]),
        card_event_id=row["card_event_id"],
        created_at_ns=int(row["created_at_ns"]),
        continuation_id=stored_identity[0],
        continuation_generation=stored_identity[1],
        tool_call_id=stored_identity[2],
    )


def _log_unreadable_card(row: Row) -> None:
    logger.warning(
        "approval_card_row_unreadable",
        delivery_id=str(row["delivery_id"]),
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
