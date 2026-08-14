"""Narrow one-time migrations for durable Matrix delivery ownership."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from . import approvals

if TYPE_CHECKING:
    from .backend import Transaction

_LEGACY_UNAVAILABLE_NOTICE_PREFIX = "approval-unavailable:"


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
    if _table_exists(transaction, "matrix_delivery_outbox", postgres=postgres) and not _column_exists(
        transaction,
        "matrix_delivery_outbox",
        "edit_target_pending",
        postgres=postgres,
    ):
        transaction.execute(
            "ALTER TABLE matrix_delivery_outbox ADD COLUMN edit_target_pending INTEGER NOT NULL DEFAULT 0",
        )
    if _table_exists(transaction, "matrix_delivery_outbox", postgres=postgres) and _table_exists(
        transaction,
        "approval_continuations",
        postgres=postgres,
    ):
        _migrate_unavailable_notice_delivery_ids(transaction)
    legacy_approvals = _column_exists(transaction, "approval_cards", "transaction_id", postgres=postgres)
    if legacy_approvals:
        transaction.execute("ALTER TABLE approval_cards RENAME TO approval_cards_legacy_delivery")
        for column, definition in (
            ("continuation_id", "TEXT"),
            ("continuation_generation", "BIGINT"),
            ("tool_call_id", "TEXT"),
            ("migration_owner", "TEXT"),
            ("migration_generation", "BIGINT NOT NULL DEFAULT 0"),
            ("migration_lease_until_ns", "BIGINT"),
        ):
            if not _column_exists(
                transaction,
                "approval_cards_legacy_delivery",
                column,
                postgres=postgres,
            ):
                transaction.execute(
                    f"ALTER TABLE approval_cards_legacy_delivery ADD COLUMN {column} {definition}",
                )
    return legacy_approvals


def _migrate_unavailable_notice_delivery_ids(transaction: Transaction) -> None:
    """Move #1834 notice debt onto the response event ID used by the generic flow."""
    rows = transaction.fetchall(
        """
        SELECT principal_id, delivery_id
        FROM matrix_delivery_outbox
        WHERE stage = 'final' AND delivery_id LIKE ?
        """,
        (f"{_LEGACY_UNAVAILABLE_NOTICE_PREFIX}%",),
    )
    for row in rows:
        old_delivery_id = str(row["delivery_id"])
        approval_id = old_delivery_id.removeprefix(_LEGACY_UNAVAILABLE_NOTICE_PREFIX)
        continuation = transaction.fetchone(
            "SELECT context_json FROM approval_continuations WHERE approval_id = ?",
            (approval_id,),
        )
        if continuation is None:
            continue
        context = json.loads(str(continuation["context_json"]))
        if not isinstance(context, dict):
            msg = "Stored approval continuation context is not an object"
            raise TypeError(msg)
        response_event_id = context.get("response_event_id")
        if not isinstance(response_event_id, str) or not response_event_id:
            msg = f"Legacy approval continuation {approval_id!r} has no response event ID"
            raise ValueError(msg)
        transaction.execute(
            """
            UPDATE matrix_delivery_outbox SET delivery_id = ?
            WHERE principal_id = ? AND delivery_id = ? AND stage = 'final'
            """,
            (response_event_id, str(row["principal_id"]), old_delivery_id),
        )


def finish_matrix_delivery_migration(transaction: Transaction, *, migrate_approvals: bool) -> None:
    """Promote legacy rows whose stored payload is already the acknowledged wire content."""
    if not migrate_approvals:
        return
    rows = transaction.fetchall(
        """
        SELECT principal_id, room_id, transaction_id, card_event_id, attempted,
               sending_device_id, card_json, resolution_json, continuation_id,
               continuation_generation, tool_call_id, membership_epoch, created_at_ns
        FROM approval_cards_legacy_delivery
        WHERE continuation_id IS NOT NULL
          AND continuation_generation IS NOT NULL
          AND tool_call_id IS NOT NULL
        """,
    )
    for row in rows:
        if row["card_event_id"] is None:
            continue
        approvals.promote_legacy_delivery(
            transaction,
            str(row["principal_id"]),
            delivery_id=str(row["transaction_id"]),
            payload=None,
            acknowledged_event_id=str(row["card_event_id"]),
        )


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
