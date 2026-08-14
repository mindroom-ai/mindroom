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
_PRE_MEMBERSHIP_OUTBOX = "matrix_delivery_outbox_pre_membership"


@dataclass(frozen=True, slots=True)
class _MatrixDeliveryMigration:
    """Work current DDL must finish after creating the destination tables."""

    migrate_approvals: bool
    copy_outbox: bool


def prepare_matrix_delivery_migration(transaction: Transaction, *, postgres: bool) -> _MatrixDeliveryMigration:
    """Rename legacy tables before current DDL is installed.

    The caller runs this and ``finish_matrix_delivery_migration`` in one schema
    transaction, so no process can observe a half-migrated owner.
    """
    if _table_exists(transaction, "response_outbox", postgres=postgres):
        transaction.execute("ALTER TABLE response_outbox RENAME TO matrix_delivery_outbox")
        transaction.execute("ALTER TABLE matrix_delivery_outbox RENAME COLUMN turn_id TO delivery_id")
        transaction.execute("DROP INDEX IF EXISTS response_outbox_unacknowledged_scan")
        transaction.execute(
            "ALTER TABLE matrix_delivery_outbox ADD COLUMN event_type TEXT NOT NULL DEFAULT 'm.room.message'",
        )
        transaction.execute(
            "ALTER TABLE matrix_delivery_outbox ADD COLUMN edit_target_pending INTEGER NOT NULL DEFAULT 0",
        )
    legacy_approvals = _column_exists(transaction, "approval_cards", "transaction_id", postgres=postgres)
    if legacy_approvals:
        transaction.execute("ALTER TABLE approval_cards RENAME TO approval_cards_legacy_delivery")
    copy_outbox = _prepare_delivery_membership(transaction, postgres=postgres)
    return _MatrixDeliveryMigration(migrate_approvals=legacy_approvals, copy_outbox=copy_outbox)


def finish_matrix_delivery_migration(transaction: Transaction, *, migration: _MatrixDeliveryMigration) -> None:
    """Expire pre-unification approvals without retaining a second delivery protocol."""
    if migration.copy_outbox:
        transaction.execute(
            f"""
            INSERT INTO matrix_delivery_outbox (
                principal_id, delivery_id, stage, event_type, room_id, membership_epoch,
                thread_id, transaction_id, payload_json, edits_event_id,
                edit_target_pending, attempted, retired, sending_device_id,
                acknowledged_event_id, created_at_ns
            )
            SELECT principal_id, delivery_id, stage, event_type, room_id, membership_epoch,
                   thread_id, transaction_id, payload_json, edits_event_id,
                   edit_target_pending, attempted, retired, sending_device_id,
                   acknowledged_event_id, created_at_ns
            FROM {_PRE_MEMBERSHIP_OUTBOX}
            """,  # noqa: S608 - fixed private migration table
        )
        transaction.execute(f"DROP TABLE {_PRE_MEMBERSHIP_OUTBOX}")
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


def _prepare_delivery_membership(transaction: Transaction, *, postgres: bool) -> bool:
    """Backfill exact owners and make the generic outbox ownership non-null."""
    if not _table_exists(transaction, "matrix_delivery_outbox", postgres=postgres):
        return False
    membership_exists = _column_exists(
        transaction,
        "matrix_delivery_outbox",
        "membership_epoch",
        postgres=postgres,
    )
    membership_not_null = membership_exists and _column_is_not_null(
        transaction,
        "matrix_delivery_outbox",
        "membership_epoch",
        postgres=postgres,
    )
    if not membership_exists:
        transaction.execute("ALTER TABLE matrix_delivery_outbox ADD COLUMN membership_epoch BIGINT")
    if not _column_exists(transaction, "matrix_delivery_outbox", "retired", postgres=postgres):
        transaction.execute("ALTER TABLE matrix_delivery_outbox ADD COLUMN retired INTEGER NOT NULL DEFAULT 0")
    if membership_not_null:
        return False

    _backfill_delivery_memberships(transaction, postgres=postgres)
    ambiguous = transaction.fetchone(
        """
        SELECT delivery_id, stage FROM matrix_delivery_outbox
        WHERE attempted = 1 AND acknowledged_event_id IS NULL
        LIMIT 1
        """,
    )
    if ambiguous is not None:
        msg = (
            "Cannot upgrade attempted Matrix delivery "
            f"{ambiguous['delivery_id']!r}/{ambiguous['stage']!r}: the legacy payload has no stable "
            "delivery marker, so its visible event cannot be proven. Reset the event journal before restarting."
        )
        raise RuntimeError(msg)
    # An unattempted row never reached Matrix and has no externally visible
    # identity to preserve. An acknowledged row does: keep its event ID as a
    # retired tombstone even when the old schema cannot prove an epoch.
    transaction.execute(
        "DELETE FROM matrix_delivery_outbox WHERE membership_epoch IS NULL AND attempted = 0",
    )
    transaction.execute(
        """
        UPDATE matrix_delivery_outbox SET membership_epoch = 0, retired = 1
        WHERE membership_epoch IS NULL AND acknowledged_event_id IS NOT NULL
        """,
    )
    remaining = transaction.fetchone(
        "SELECT delivery_id FROM matrix_delivery_outbox WHERE membership_epoch IS NULL LIMIT 1",
    )
    if remaining is not None:
        msg = f"Matrix delivery {remaining['delivery_id']!r} has no provable membership owner"
        raise RuntimeError(msg)

    transaction.execute("DROP INDEX IF EXISTS matrix_delivery_outbox_unacknowledged_scan")
    if postgres:
        transaction.execute("ALTER TABLE matrix_delivery_outbox ALTER COLUMN membership_epoch SET NOT NULL")
        return False
    transaction.execute("DROP INDEX IF EXISTS matrix_delivery_outbox_room_scan")
    transaction.execute(f"ALTER TABLE matrix_delivery_outbox RENAME TO {_PRE_MEMBERSHIP_OUTBOX}")
    return True


def _backfill_delivery_memberships(transaction: Transaction, *, postgres: bool) -> None:
    """Copy only exact journal or approval ownership into pre-upgrade rows."""
    rows = transaction.fetchall(
        """
        SELECT principal_id, delivery_id, stage, room_id, payload_json,
               attempted, acknowledged_event_id
        FROM matrix_delivery_outbox WHERE membership_epoch IS NULL
        """,
    )
    for row in rows:
        principal_id = str(row["principal_id"])
        delivery_id = str(row["delivery_id"])
        stage = DeliveryStage(str(row["stage"]))
        room_id = str(row["room_id"])
        epoch = _exact_delivery_epoch(
            transaction,
            principal_id=principal_id,
            delivery_id=delivery_id,
            room_id=room_id,
            postgres=postgres,
        )
        if epoch is None:
            continue
        payload = _object_json(row["payload_json"])
        if payload is None:
            msg = f"Matrix delivery {delivery_id!r}/{stage.value!r} has a non-object payload"
            raise RuntimeError(msg)
        transaction.execute(
            """
            UPDATE matrix_delivery_outbox SET membership_epoch = ?, payload_json = ?
            WHERE principal_id = ? AND delivery_id = ? AND stage = ?
            """,
            (
                epoch,
                outbox.delivery_payload_json(principal_id, delivery_id, stage, payload),
                principal_id,
                delivery_id,
                stage.value,
            ),
        )


def _exact_delivery_epoch(
    transaction: Transaction,
    *,
    principal_id: str,
    delivery_id: str,
    room_id: str,
    postgres: bool,
) -> int | None:
    """Return an exact same-room journal or approval membership owner."""
    event = (
        transaction.fetchone(
            """
            SELECT membership_epoch FROM journal_events
            WHERE principal_id = ? AND event_id = ? AND room_id = ?
            """,
            (principal_id, delivery_id, room_id),
        )
        if _table_exists(transaction, "journal_events", postgres=postgres)
        else None
    )
    if event is not None:
        return int(event["membership_epoch"])
    for table, identity_column in (
        ("approval_cards", "delivery_id"),
        ("approval_cards_legacy_delivery", "transaction_id"),
    ):
        row = (
            transaction.fetchone(
                f"SELECT membership_epoch FROM {table} WHERE principal_id = ? AND {identity_column} = ?",  # noqa: S608 - fixed migration tables
                (principal_id, delivery_id),
            )
            if _table_exists(transaction, table, postgres=postgres)
            else None
        )
        if row is not None:
            return int(row["membership_epoch"])
    return None


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


def _column_is_not_null(transaction: Transaction, table: str, column: str, *, postgres: bool) -> bool:
    """Return whether one existing column enforces non-null values."""
    if postgres:
        row = transaction.fetchone(
            """
            SELECT is_nullable FROM information_schema.columns
            WHERE table_schema = current_schema() AND table_name = ? AND column_name = ?
            """,
            (table, column),
        )
        return row is not None and row["is_nullable"] == "NO"
    row = next(
        (item for item in transaction.fetchall(f"PRAGMA table_info({table})") if str(item["name"]) == column),
        None,
    )
    return row is not None and bool(row["notnull"])
