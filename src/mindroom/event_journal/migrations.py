"""Narrow one-time migrations for durable Matrix delivery ownership."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import outbox
from .models import DeliveryStage

if TYPE_CHECKING:
    from .backend import Transaction

_LEGACY_APPROVAL_EXPIRY_REASON = "Tool approval request expired during delivery upgrade."
_LEGACY_RESPONSE_OUTBOX = "response_outbox_legacy_delivery"


@dataclass(frozen=True, slots=True)
class _MatrixDeliveryMigration:
    """Legacy tables current DDL must consume after creating their replacements."""

    migrate_approvals: bool
    migrate_responses: bool


def prepare_matrix_delivery_migration(transaction: Transaction, *, postgres: bool) -> _MatrixDeliveryMigration:
    """Move released legacy tables aside before current DDL is installed."""
    if _table_exists(transaction, "matrix_delivery_outbox", postgres=postgres) and (
        not _column_exists(transaction, "matrix_delivery_outbox", "membership_epoch", postgres=postgres)
        or not _column_exists(transaction, "matrix_delivery_outbox", "retired", postgres=postgres)
    ):
        msg = (
            "The generic Matrix delivery schema predates membership fencing and cannot prove which room "
            "membership owns its existing deliveries. Reset the event journal before restarting."
        )
        raise RuntimeError(msg)
    migrate_responses = _table_exists(transaction, "response_outbox", postgres=postgres)
    if migrate_responses:
        transaction.execute("DROP INDEX IF EXISTS response_outbox_unacknowledged_scan")
        transaction.execute(
            "ALTER TABLE response_outbox RENAME TO response_outbox_legacy_delivery",
        )
    migrate_approvals = _column_exists(transaction, "approval_cards", "transaction_id", postgres=postgres)
    if migrate_approvals:
        transaction.execute("ALTER TABLE approval_cards RENAME TO approval_cards_legacy_delivery")
    return _MatrixDeliveryMigration(
        migrate_approvals=migrate_approvals,
        migrate_responses=migrate_responses,
    )


def finish_matrix_delivery_migration(transaction: Transaction, *, migration: _MatrixDeliveryMigration) -> None:
    """Move released delivery facts into the final generic ownership schema."""
    if migration.migrate_responses:
        _migrate_response_outbox(transaction)
    if not migration.migrate_approvals:
        return
    transaction.execute(
        """
        UPDATE approval_continuation_calls
        SET decision = 'expired', reason = ?
        WHERE decision IS NULL
        """,
        (_LEGACY_APPROVAL_EXPIRY_REASON,),
    )
    transaction.execute(
        """
        UPDATE approval_continuations
        SET state = 'ready', runtime_generation = NULL
        WHERE state = 'waiting'
        """,
    )
    transaction.execute(
        """
        INSERT INTO approval_action_tombstones (principal_id, room_id, card_event_id)
        SELECT principal_id, room_id, card_event_id
        FROM approval_cards_legacy_delivery
        WHERE card_event_id IS NOT NULL AND card_event_id != ''
        ON CONFLICT (principal_id, card_event_id) DO NOTHING
        """,
    )
    transaction.execute("DROP TABLE approval_cards_legacy_delivery")


def _migrate_response_outbox(transaction: Transaction) -> None:
    """Copy the released response outbox directly into the final generic table."""
    ambiguous = transaction.fetchone(
        f"""
        SELECT turn_id, stage FROM {_LEGACY_RESPONSE_OUTBOX}
        WHERE attempted = 1 AND acknowledged_event_id IS NULL LIMIT 1
        """,  # noqa: S608 - fixed private migration table
    )
    if ambiguous is not None:
        msg = (
            "Cannot upgrade attempted Matrix delivery "
            f"{ambiguous['turn_id']!r}/{ambiguous['stage']!r}: the legacy payload has no stable "
            "delivery marker, so its visible event cannot be proven. Reset the event journal before restarting."
        )
        raise RuntimeError(msg)

    rows = transaction.fetchall(
        f"SELECT * FROM {_LEGACY_RESPONSE_OUTBOX}",  # noqa: S608 - fixed private migration table
    )
    for row in rows:
        principal_id = str(row["principal_id"])
        delivery_id = str(row["turn_id"])
        stage = DeliveryStage(str(row["stage"]))
        room_id = str(row["room_id"])
        event = transaction.fetchone(
            """
            SELECT membership_epoch FROM journal_events
            WHERE principal_id = ? AND event_id = ? AND room_id = ?
            """,
            (principal_id, delivery_id, room_id),
        )
        acknowledged_event_id = row["acknowledged_event_id"]
        if event is None and acknowledged_event_id is None:
            continue
        membership_epoch = 0 if event is None else int(event["membership_epoch"])
        retired = int(event is None)
        payload_json = str(row["payload_json"])
        if event is not None:
            payload = _object_json(payload_json)
            if payload is None:
                msg = f"Matrix delivery {delivery_id!r}/{stage.value!r} has a non-object payload"
                raise RuntimeError(msg)
            payload_json = outbox.delivery_payload_json(principal_id, delivery_id, stage, payload)
        transaction.execute(
            """
            INSERT INTO matrix_delivery_outbox (
                principal_id, delivery_id, stage, event_type, room_id, membership_epoch,
                thread_id, transaction_id, payload_json, edits_event_id,
                edit_target_pending, attempted, retired, sending_device_id,
                acknowledged_event_id, created_at_ns
            ) VALUES (?, ?, ?, 'm.room.message', ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
            """,
            (
                principal_id,
                delivery_id,
                stage.value,
                room_id,
                membership_epoch,
                str(row["thread_id"]),
                str(row["transaction_id"]),
                payload_json,
                row["edits_event_id"],
                int(row["attempted"]),
                retired,
                row["sending_device_id"],
                acknowledged_event_id,
                int(row["created_at_ns"]),
            ),
        )
    transaction.execute("DROP TABLE response_outbox_legacy_delivery")


def _object_json(value: object) -> dict[str, Any] | None:
    try:
        decoded = json.loads(str(value))
    except json.JSONDecodeError:
        return None
    return decoded if isinstance(decoded, dict) else None


def _table_exists(transaction: Transaction, table: str, *, postgres: bool) -> bool:
    if postgres:
        row = transaction.fetchone("SELECT to_regclass(?) AS table_name", (table,))
        return row is not None and row["table_name"] is not None
    return (
        transaction.fetchone("SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)) is not None
    )


def _column_exists(transaction: Transaction, table: str, column: str, *, postgres: bool) -> bool:
    if not _table_exists(transaction, table, postgres=postgres):
        return False
    if postgres:
        return (
            transaction.fetchone(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = current_schema() AND table_name = ? AND column_name = ?
                """,
                (table, column),
            )
            is not None
        )
    return any(str(row["name"]) == column for row in transaction.fetchall(f"PRAGMA table_info({table})"))
