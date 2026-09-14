"""One-time adoption of response ownership from released snapshots."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING, cast

from mindroom.handled_turns import TurnRecordCodec
from mindroom.legacy_delivery_payloads import decode_delivery_result
from mindroom.response_sources import ResponseAttempt, ResponseSources

from .response_attempts import load_response_attempt, register_response_attempt

if TYPE_CHECKING:
    from mindroom.turn_record import TurnRecord

    from .backend import Row, Transaction

# Legacy format: Approval context and prepared FINAL snapshots carry response source ownership.
# Last legacy release: v2026.9.137; replacement: the explicit response_attempts schema.
# Handling: Adopt stable identities once under the backend schema transaction; preserve pending and frozen debt.
# Coverage: tests/test_response_attempts_migration.py::test_literal_owners_survive_migration_and_reopen.


def _identity_error() -> ValueError:
    """Fail required ownership instead of fabricating or settling a live continuation."""
    return ValueError("Cannot migrate required response attempt identity")


def _required_text(value: object) -> str:
    """Validate a required historical identity field."""
    if not isinstance(value, str) or not value:
        raise _identity_error()
    return value


def _event_ids(value: object) -> tuple[str, ...]:
    """Validate literal historical source arrays before the tolerant turn decoder."""
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise _identity_error()
    return tuple(cast("list[str]", value))


def _prepared_sources(pending: tuple[str, ...], raw: object) -> tuple[ResponseSources, TurnRecord | None]:
    """Decode historical selection only at the migration boundary."""
    if raw is None:
        return ResponseSources(pending, pending), None
    if not isinstance(raw, dict):
        raise _identity_error()
    raw = cast("dict[str, object]", raw)
    prepared = TurnRecordCodec._from_ledger_record(str(raw.get("anchor_event_id", "")), raw)
    if prepared is None or prepared.latest_edit_receipt_order is None:
        raise _identity_error()
    sources = ResponseSources(
        pending,
        _event_ids(raw.get("source_event_ids")),
        _event_ids(raw.get("discovery_event_ids", [])),
        prepared.latest_edit_receipt_order,
    )
    if pending[0] not in {revision[1] for revision in (prepared.source_event_revisions or {}).values()}:
        raise _identity_error()
    return sources, prepared


def _adopt_continuations(transaction: Transaction) -> None:
    """Live continuation ownership is required, including all pending children."""
    rows = transaction.fetchall(
        "SELECT principal_id, approval_id, entity_name, context_json FROM approval_continuations",
    )
    for row in rows:
        principal = str(row["principal_id"])
        context = json.loads(str(row["context_json"]))
        if not isinstance(context, dict):
            raise _identity_error()
        room = _required_text(context.get("room_id"))
        response = _required_text(context.get("response_event_id"))
        entity = _required_text(row["entity_name"])
        children = transaction.fetchall(
            """SELECT sources.event_id, sources.source_ordinal, events.membership_epoch FROM approval_continuation_sources AS sources
            LEFT JOIN journal_events AS events ON events.principal_id = sources.principal_id AND events.event_id = sources.event_id
            WHERE sources.principal_id = ? AND sources.approval_id = ? ORDER BY sources.source_ordinal""",
            (principal, str(row["approval_id"])),
        )
        if not children or any(
            child["membership_epoch"] is None or int(child["source_ordinal"]) != ordinal
            for ordinal, child in enumerate(children)
        ):
            raise _identity_error()
        sources, prepared = _prepared_sources(
            tuple(str(child["event_id"]) for child in children),
            context.get("prepared_edit_record"),
        )
        if prepared is not None and (
            prepared.response_owner != entity
            or prepared.response_event_id != response
            or prepared.conversation_target is None
            or prepared.conversation_target.room_id != room
        ):
            raise _identity_error()
        register_response_attempt(
            transaction,
            principal,
            attempt=ResponseAttempt(entity, sources),
            room_id=room,
            membership_epoch=int(children[0]["membership_epoch"]),
            response_event_id=response,
        )


def _final_record(transaction: Transaction, row: Row, result: dict[str, object] | None) -> TurnRecord | None:
    """Prove a historical final's owner from its frozen edit or exact completed turn."""
    driving = str(row["delivery_id"])
    if result is not None and result.get("prepared_edit_record") is not None:
        _, record = _prepared_sources((driving,), result["prepared_edit_record"])
        return record
    candidates = transaction.fetchall("SELECT record_json FROM turn_records WHERE index_event_id = ?", (driving,))
    records = [
        record
        for candidate in candidates
        if (record := TurnRecordCodec._from_ledger_record(driving, json.loads(str(candidate["record_json"]))))
        is not None
        and driving in record.source_event_ids
        and record.completed
        and record.response_event_id == (row["edits_event_id"] or row["acknowledged_event_id"])
    ]
    return replace(records[0], latest_edit_receipt_order=None) if len(records) == 1 else None


def _decode_final_result(row: Row, driving: str) -> dict[str, object] | None:
    """Decode one historical FINAL while retaining legacy inline precedence."""
    payload = json.loads(str(row["payload_json"]))
    if not isinstance(payload, dict):
        msg = f"Outbox payload for delivery {driving!r} is not an object"
        raise TypeError(msg)
    return decode_delivery_result(payload, cast("str | None", row["result_json"]), delivery_id=driving)


def _adopt_finals(transaction: Transaction) -> None:
    """Keep unrelated malformed deliveries outside the response ownership relation."""
    rows = transaction.fetchall("SELECT * FROM matrix_delivery_outbox WHERE stage = 'final'")
    for row in rows:
        principal, driving = str(row["principal_id"]), str(row["delivery_id"])
        existing = load_response_attempt(transaction, principal, driving)
        try:
            result = _decode_final_result(row, driving)
        except (ValueError, TypeError, KeyError):
            if existing is not None:
                raise _identity_error() from None
            continue
        if existing is not None:
            if result is not None and row["result_json"] is None:
                _store_inline_result(transaction, principal, driving, result)
            continue
        try:
            record = _final_record(transaction, row, result)
        except (ValueError, TypeError, KeyError):
            continue
        response = row["edits_event_id"] or row["acknowledged_event_id"]
        if (
            record is None
            or record.response_owner is None
            or record.conversation_target is None
            or record.conversation_target.room_id != row["room_id"]
            or record.response_event_id != response
        ):
            continue
        pending = (
            (driving,)
            if record.latest_edit_receipt_order is not None
            else (driving, *(event_id for event_id in record.source_event_ids if event_id != driving))
        )
        if not _sources_are_admitted(transaction, row, pending):
            continue
        sources = ResponseSources(
            pending,
            record.source_event_ids,
            record.discovery_event_ids,
            record.latest_edit_receipt_order,
        )
        register_response_attempt(
            transaction,
            principal,
            attempt=ResponseAttempt(record.response_owner, sources),
            room_id=str(row["room_id"]),
            membership_epoch=int(row["membership_epoch"]),
            response_event_id=None if response is None else str(response),
        )
        if result is not None and row["result_json"] is None:
            _store_inline_result(transaction, principal, driving, result)


def _sources_are_admitted(transaction: Transaction, row: Row, pending: tuple[str, ...]) -> bool:
    """Validate every optional source before any ownership rows are inserted."""
    for event_id in pending:
        admitted = transaction.fetchone(
            "SELECT room_id, membership_epoch FROM journal_events WHERE principal_id = ? AND event_id = ?",
            (str(row["principal_id"]), event_id),
        )
        if (
            admitted is None
            or admitted["room_id"] != row["room_id"]
            or admitted["membership_epoch"] != row["membership_epoch"]
        ):
            return False
    initial = transaction.fetchone(
        """SELECT room_id, membership_epoch, edits_event_id, acknowledged_event_id FROM matrix_delivery_outbox
        WHERE principal_id = ? AND delivery_id = ? AND stage = 'initial'""",
        (str(row["principal_id"]), str(row["delivery_id"])),
    )
    return (
        initial is None
        or initial["acknowledged_event_id"] is None
        or (
            initial["room_id"] == row["room_id"]
            and initial["membership_epoch"] == row["membership_epoch"]
            and (initial["edits_event_id"] or initial["acknowledged_event_id"])
            == (row["edits_event_id"] or row["acknowledged_event_id"])
        )
    )


def _store_inline_result(transaction: Transaction, principal: str, driving: str, result: dict[str, object]) -> None:
    """Make existing successful result presence queryable without changing wire bytes."""
    transaction.execute(
        "UPDATE matrix_delivery_outbox SET result_json = ? WHERE principal_id = ? AND delivery_id = ? AND stage = 'final'",
        (json.dumps(result, separators=(",", ":"), ensure_ascii=True), principal, driving),
    )


def migrate_response_attempts(transaction: Transaction, existing_tables: frozenset[str]) -> None:
    """Backfill once after DDL, within the existing schema lock and transaction."""
    if (
        "response_attempts" in existing_tables
        or "matrix_sync_consumers" not in existing_tables
        or "approval_continuations" not in existing_tables
    ):
        return
    _adopt_continuations(transaction)
    _adopt_finals(transaction)
