"""Normalized stable response ownership; delivery and turn state retain their owners."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from . import outbox
from .models import DeliveryStage

if TYPE_CHECKING:
    from mindroom.response_sources import ResponseAttempt
    from mindroom.turn_record import TurnRecord

    from .backend import Row, Transaction


@dataclass(frozen=True, slots=True)
class StoredResponseAttempt:
    """Stable identity that survives deletion of pending approval source children."""

    driving_event_id: str
    entity_name: str
    room_id: str
    membership_epoch: int
    response_event_id: str | None
    logical_source_event_ids: tuple[str, ...]
    discovery_event_ids: tuple[str, ...]
    selected_receipt_order: int
    edit_receipt_order: int | None


class ApprovalFailureDisposition(StrEnum):
    """The publication obligation of a failed approval attempt."""

    PUBLISH = "publish"
    RETIRE = "retire"
    RECOVER_FINAL = "recover_final"


def _source_key(event_ids: tuple[str, ...]) -> str:
    """Canonicalize complete membership while retaining ordering in child rows."""
    return json.dumps(sorted(event_ids), ensure_ascii=True, separators=(",", ":"))


def load_response_attempt(
    transaction: Transaction,
    principal_id: str,
    driving_event_id: str,
) -> StoredResponseAttempt | None:
    """Read stable ownership without inventing pending settlement sources."""
    return load_response_attempts(transaction, ((principal_id, driving_event_id),)).get(
        (principal_id, driving_event_id),
    )


def load_response_attempts(
    transaction: Transaction,
    identities: tuple[tuple[str, str], ...],
) -> dict[tuple[str, str], StoredResponseAttempt]:
    """Load a bounded owner page and its children with two queries."""
    if not identities:
        return {}
    placeholders = ", ".join("(?, ?)" for _identity in identities)
    predicates = f"(principal_id, driving_event_id) IN ({placeholders})"
    parameters = tuple(value for identity in identities for value in identity)
    rows = transaction.fetchall(f"SELECT * FROM response_attempts WHERE {predicates}", parameters)  # noqa: S608
    children = transaction.fetchall(
        f"SELECT * FROM response_attempt_sources WHERE {predicates} ORDER BY source_kind, source_ordinal",  # noqa: S608
        parameters,
    )
    grouped: dict[tuple[str, str], list[Row]] = {identity: [] for identity in identities}
    for child in children:
        grouped[(str(child["principal_id"]), str(child["driving_event_id"]))].append(child)
    return {
        (str(row["principal_id"]), str(row["driving_event_id"])): _from_rows(
            row,
            grouped[(str(row["principal_id"]), str(row["driving_event_id"]))],
        )
        for row in rows
    }


def _from_rows(row: Row, children: list[Row]) -> StoredResponseAttempt:
    """Restore a stable value from normalized rows."""
    return StoredResponseAttempt(
        driving_event_id=str(row["driving_event_id"]),
        entity_name=str(row["entity_name"]),
        room_id=str(row["room_id"]),
        membership_epoch=int(row["membership_epoch"]),
        response_event_id=None if row["response_event_id"] is None else str(row["response_event_id"]),
        logical_source_event_ids=tuple(
            str(child["event_id"]) for child in children if child["source_kind"] == "logical"
        ),
        discovery_event_ids=tuple(str(child["event_id"]) for child in children if child["source_kind"] == "discovery"),
        selected_receipt_order=int(row["selected_receipt_order"]),
        edit_receipt_order=None if row["edit_receipt_order"] is None else int(row["edit_receipt_order"]),
    )


def bind_response_target(
    transaction: Transaction,
    principal_id: str,
    driving_event_id: str,
    response_event_id: str,
) -> None:
    """Bind once; a conflicting visible identity rolls back the enclosing write."""
    transaction.execute(
        """UPDATE response_attempts SET response_event_id = ?
        WHERE principal_id = ? AND driving_event_id = ? AND response_event_id IS NULL""",
        (response_event_id, principal_id, driving_event_id),
    )
    row = transaction.fetchone(
        "SELECT response_event_id FROM response_attempts WHERE principal_id = ? AND driving_event_id = ?",
        (principal_id, driving_event_id),
    )
    if row is not None and row["response_event_id"] != response_event_id:
        message = "Conflicting response attempt identity: visible response"
        raise ValueError(message)


def register_response_attempt(
    transaction: Transaction,
    principal_id: str,
    *,
    attempt: ResponseAttempt,
    room_id: str,
    membership_epoch: int,
    response_event_id: str | None,
) -> None:
    """Register admitted source identity in its caller's approval or delivery transaction."""
    sources = attempt.sources
    driving = sources.pending_event_ids[0]
    orders = []
    for event_id in sources.pending_event_ids:
        event = transaction.fetchone(
            "SELECT room_id, membership_epoch, receipt_order FROM journal_events WHERE principal_id = ? AND event_id = ?",
            (principal_id, event_id),
        )
        if event is None or event["room_id"] != room_id or int(event["membership_epoch"]) != membership_epoch:
            message = "Conflicting response attempt identity: admitted source"
            raise ValueError(message)
        orders.append(int(event["receipt_order"]))
    selected = max(orders)
    inserted = transaction.fetchone(
        """INSERT INTO response_attempts (
            principal_id, driving_event_id, entity_name, room_id, membership_epoch, response_event_id,
            logical_source_key, selected_receipt_order, edit_receipt_order
        ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)
        ON CONFLICT (principal_id, driving_event_id) DO NOTHING RETURNING driving_event_id""",
        (
            principal_id,
            driving,
            attempt.entity_name,
            room_id,
            membership_epoch,
            _source_key(sources.logical_source_event_ids),
            selected,
            sources.edit_receipt_order,
        ),
    )
    if inserted is not None:
        for kind, event_ids in (
            ("logical", sources.logical_source_event_ids),
            ("discovery", sources.discovery_event_ids),
        ):
            for ordinal, event_id in enumerate(event_ids):
                transaction.execute(
                    """INSERT INTO response_attempt_sources (principal_id, driving_event_id, event_id, source_ordinal, source_kind)
                    VALUES (?, ?, ?, ?, ?) ON CONFLICT (principal_id, driving_event_id, source_kind, source_ordinal) DO NOTHING""",
                    (principal_id, driving, event_id, ordinal, kind),
                )
    stored = load_response_attempt(transaction, principal_id, driving)
    if stored is None or (
        stored.entity_name,
        stored.room_id,
        stored.membership_epoch,
        stored.logical_source_event_ids,
        stored.discovery_event_ids,
        stored.selected_receipt_order,
        stored.edit_receipt_order,
    ) != (
        attempt.entity_name,
        room_id,
        membership_epoch,
        sources.logical_source_event_ids,
        sources.discovery_event_ids,
        selected,
        sources.edit_receipt_order,
    ):
        message = "Conflicting response attempt identity"
        raise ValueError(message)
    initial = transaction.fetchone(
        """SELECT room_id, membership_epoch, edits_event_id, acknowledged_event_id FROM matrix_delivery_outbox
        WHERE principal_id = ? AND delivery_id = ? AND stage = 'initial'""",
        (principal_id, driving),
    )
    if initial is not None and initial["acknowledged_event_id"] is not None:
        if initial["room_id"] != room_id or int(initial["membership_epoch"]) != membership_epoch:
            message = "Conflicting response attempt identity: INITIAL membership"
            raise ValueError(message)
        bind_response_target(
            transaction,
            principal_id,
            driving,
            str(initial["edits_event_id"] or initial["acknowledged_event_id"]),
        )
    if response_event_id is not None:
        bind_response_target(transaction, principal_id, driving, response_event_id)


def edited_attempt_sources_before_stop(
    transaction: Transaction,
    principal_id: str,
    *,
    room_id: str,
    response_event_id: str,
    source_event_id: str,
    stop_receipt_order: int,
) -> tuple[str, ...]:
    """Find captured edited owners by indexed source, including completed FINALs."""
    rows = transaction.fetchall(
        """SELECT DISTINCT attempt.driving_event_id, attempt.selected_receipt_order
        FROM response_attempts AS attempt
        JOIN response_attempt_sources AS source
          ON source.principal_id = attempt.principal_id AND source.driving_event_id = attempt.driving_event_id
        WHERE attempt.principal_id = ? AND attempt.room_id = ? AND attempt.response_event_id = ?
          AND source.event_id = ? AND attempt.selected_receipt_order <= ?
          AND attempt.edit_receipt_order <= ?
        ORDER BY attempt.selected_receipt_order""",
        (principal_id, room_id, response_event_id, source_event_id, stop_receipt_order, stop_receipt_order),
    )
    return tuple(str(row["driving_event_id"]) for row in rows)


def approval_failure_disposition(
    transaction: Transaction,
    principal_id: str,
    *,
    attempt: StoredResponseAttempt,
    current_record: TurnRecord | None,
    failure_reason: str | None,
) -> ApprovalFailureDisposition:
    """Preserve successful debt and retire only an exactly superseded failure."""
    final = outbox.load(transaction, principal_id, delivery_id=attempt.driving_event_id, stage=DeliveryStage.FINAL)
    if final is not None and final.result is not None:
        return ApprovalFailureDisposition.RECOVER_FINAL
    current = current_record
    if current is None or (
        set(current.source_event_ids) != set(attempt.logical_source_event_ids)
        or current.response_owner != attempt.entity_name
        or current.conversation_target is None
        or current.conversation_target.room_id != attempt.room_id
        or current.response_event_id != attempt.response_event_id
    ):
        return ApprovalFailureDisposition.PUBLISH
    if final is not None and (
        final.room_id != attempt.room_id
        or final.membership_epoch != attempt.membership_epoch
        or final.edits_event_id != attempt.response_event_id
    ):
        return ApprovalFailureDisposition.PUBLISH
    selected = attempt.edit_receipt_order or attempt.selected_receipt_order
    if (
        failure_reason == "cancelled_by_user"
        and selected <= (current.user_stop_receipt_order or 0)
        and (current.latest_edit_receipt_order or 0) > (current.user_stop_receipt_order or 0)
    ):
        return ApprovalFailureDisposition.RETIRE
    rows = transaction.fetchall(
        """SELECT delivery.delivery_id FROM response_attempts AS newer
        JOIN matrix_delivery_outbox AS delivery
          ON delivery.principal_id = newer.principal_id AND delivery.delivery_id = newer.driving_event_id
         AND delivery.room_id = newer.room_id AND delivery.membership_epoch = newer.membership_epoch
         AND delivery.edits_event_id = newer.response_event_id
        WHERE newer.principal_id = ? AND newer.room_id = ? AND newer.membership_epoch = ?
          AND newer.response_event_id = ? AND newer.entity_name = ? AND newer.logical_source_key = ?
          AND newer.edit_receipt_order > ? AND delivery.stage = 'final'
          AND delivery.acknowledged_event_id IS NOT NULL AND delivery.result_json IS NOT NULL
          AND delivery.retired = 0 AND delivery.permanent_failure_reason IS NULL""",
        (
            principal_id,
            attempt.room_id,
            attempt.membership_epoch,
            attempt.response_event_id,
            attempt.entity_name,
            _source_key(attempt.logical_source_event_ids),
            selected,
        ),
    )
    return ApprovalFailureDisposition.RETIRE if rows else ApprovalFailureDisposition.PUBLISH
