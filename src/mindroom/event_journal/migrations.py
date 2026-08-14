"""Narrow one-time migrations for durable Matrix delivery ownership."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .backend import Transaction

_LEGACY_APPROVAL_EXPIRY_REASON = "Tool approval request expired during delivery upgrade."


def prepare_matrix_delivery_migration(transaction: Transaction, *, postgres: bool) -> bool:
    """Rename legacy tables before current DDL is installed.

    Returns whether approval-card rows must be copied after the new tables
    exist. The caller runs this and ``finish_matrix_delivery_migration`` in the
    same schema transaction, so no process can observe a half-migrated owner.
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
    return legacy_approvals


def finish_matrix_delivery_migration(transaction: Transaction, *, migrate_approvals: bool) -> None:
    """Expire pre-unification approvals without retaining a second delivery protocol."""
    if not migrate_approvals:
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
