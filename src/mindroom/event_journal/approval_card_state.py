"""Shared approval-card value types and durable outbox primitives."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.tool_approval_grants import ApprovalOperation

    from .backend import Row, Transaction

from . import outbox
from .identity import decode_thread_id
from .models import DURABLE_DELIVERY_ID_KEY, DeliveryStage

__all__ = [
    "TIMEOUT_REASON",
    "ApprovalCardReservation",
    "ApprovalDecisionMetadata",
    "RecordedApprovalDecision",
    "decode_object_payload",
    "decode_resolution",
    "enqueue_resolution",
    "reserve_delivery",
    "stored_resolution",
    "terminal_content",
]

TIMEOUT_REASON = "Tool approval request timed out."


@dataclass(frozen=True, slots=True)
class ApprovalDecisionMetadata:
    """Authenticated decision facts; original request evidence belongs to the journal."""

    resolved_by: str | None = None
    resolved_at: str | None = None
    provenance: Mapping[str, Any] | None = None
    auto_approval: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ApprovalCardReservation:
    """One exact-call approval card and its frozen Matrix payload."""

    delivery_id: str
    tool_call_id: str
    event_type: str
    payload: Mapping[str, object]
    grant_operation: ApprovalOperation | None = None


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
    continuation_room_id: str | None = None
    source_event_ids: tuple[str, ...] = ()


def decode_object_payload(value: object, *, description: str) -> dict[str, Any]:
    """Decode one durable JSON object without its private delivery marker."""
    decoded = json.loads(str(value))
    if not isinstance(decoded, dict):
        msg = f"Stored {description} is not an object"
        raise TypeError(msg)
    decoded.pop(DURABLE_DELIVERY_ID_KEY, None)
    return decoded


def reserve_delivery(
    transaction: Transaction,
    principal_id: str,
    *,
    room_id: str,
    thread_id: str | None,
    membership_epoch: int,
    identity: tuple[str, int, str],
    card: ApprovalCardReservation,
) -> None:
    """Reserve one frozen card in the shared Matrix delivery lifecycle."""
    outbox.enqueue(
        transaction,
        principal_id,
        delivery_id=card.delivery_id,
        stage=DeliveryStage.INITIAL,
        event_type=card.event_type,
        room_id=room_id,
        membership_epoch=membership_epoch,
        thread_id=thread_id,
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
        (principal_id, card.delivery_id, *identity, membership_epoch),
    )


def stored_resolution(
    row: Row,
    *,
    metadata: ApprovalDecisionMetadata | None,
    requested_status: Literal["approved", "denied", "expired"],
    decision: Literal["approved", "denied", "expired"],
    reason: str | None,
    description: str,
) -> dict[str, Any]:
    """Project the actual journal decision from its frozen original request."""
    return terminal_content(
        decode_object_payload(row["payload_json"], description=description),
        status=decision,
        reason=reason,
        metadata=metadata if decision == requested_status else None,
    )


def enqueue_resolution(
    transaction: Transaction,
    principal_id: str,
    row: Row,
    resolution: Mapping[str, Any],
) -> None:
    """Enqueue one terminal edit through the shared Matrix outbox."""
    outbox.enqueue(
        transaction,
        principal_id,
        delivery_id=str(row["delivery_id"]),
        stage=DeliveryStage.FINAL,
        event_type=str(row["event_type"]),
        room_id=str(row["room_id"]),
        membership_epoch=int(row["membership_epoch"]),
        thread_id=decode_thread_id(str(row["thread_id"])),
        payload=resolution,
        edits_event_id=None if row["acknowledged_event_id"] is None else str(row["acknowledged_event_id"]),
        edit_target_pending=row["acknowledged_event_id"] is None,
    )


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
    excluded = {"auto_approve_options", "auto_approval", "approval_provenance", "resolved_at", "resolution_reason"}
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
    tool_name = resolution.get("tool_name")
    label = "Auto-approved" if publication == "receipt" and status == "approved" else status.title()
    resolution["body"] = f"{label}: {tool_name}" if isinstance(tool_name, str) else f"Approval {status}"
    return resolution


def decode_resolution(stored: str | None) -> dict[str, Any] | None:
    """Decode one durable terminal approval payload."""
    if stored is None:
        return None
    resolution = json.loads(stored)
    if not isinstance(resolution, dict):
        msg = "Stored approval resolution is not an object"
        raise TypeError(msg)
    resolution.pop(DURABLE_DELIVERY_ID_KEY, None)
    return resolution
