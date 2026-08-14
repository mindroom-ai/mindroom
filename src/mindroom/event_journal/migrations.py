"""Narrow one-time migrations for durable Matrix delivery ownership."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from . import outbox
from .models import DeliveryStage

if TYPE_CHECKING:
    from .backend import Transaction

_LEGACY_APPROVAL_EXPIRY_REASON = "Tool approval request expired during delivery upgrade."
_APPROVAL_EVENT_TYPE = "io.mindroom.tool_approval"


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
          AND NOT EXISTS (
              SELECT 1 FROM approval_continuation_calls AS calls
              WHERE calls.principal_id = approval_continuations.principal_id
                AND calls.approval_id = approval_continuations.approval_id
                AND calls.generation = approval_continuations.generation
                AND calls.decision IS NULL
          )
        """,
    )
    rows = transaction.fetchall(
        """
        SELECT principal_id, room_id, transaction_id, card_event_id,
               card_json, resolution_json
        FROM approval_cards_legacy_delivery
        WHERE card_event_id IS NOT NULL
        """,
    )
    for row in rows:
        principal_id = str(row["principal_id"])
        room_id = str(row["room_id"])
        card_event_id = str(row["card_event_id"])
        if not card_event_id:
            continue
        transaction.execute(
            """
            INSERT INTO approval_action_tombstones (principal_id, room_id, card_event_id)
            VALUES (?, ?, ?)
            ON CONFLICT (principal_id, card_event_id) DO NOTHING
            """,
            (principal_id, room_id, card_event_id),
        )
        card = _object_json(row["card_json"])
        if card is None:
            continue
        content = card.get("content")
        if not isinstance(content, dict):
            continue
        if row["resolution_json"] is None:
            resolution = _expired_content(content)
        else:
            resolution = _object_json(row["resolution_json"])
            if resolution is None:
                continue
        event_type = card.get("type")
        thread_id = content.get("thread_id")
        outbox.enqueue(
            transaction,
            principal_id,
            delivery_id=str(row["transaction_id"]),
            stage=DeliveryStage.FINAL,
            event_type=event_type if isinstance(event_type, str) and event_type else _APPROVAL_EVENT_TYPE,
            room_id=room_id,
            thread_id=thread_id if isinstance(thread_id, str) and thread_id else None,
            payload=resolution,
            edits_event_id=card_event_id,
        )
    transaction.execute("DROP TABLE approval_cards_legacy_delivery")


def _object_json(value: object) -> dict[str, Any] | None:
    try:
        decoded = json.loads(str(value))
    except json.JSONDecodeError:
        return None
    return decoded if isinstance(decoded, dict) else None


def _expired_content(content: dict[str, Any]) -> dict[str, Any]:
    expired = {
        **content,
        "status": "expired",
        "approvable": False,
        "resolution_reason": _LEGACY_APPROVAL_EXPIRY_REASON,
        "resolved_by": None,
    }
    tool_name = content.get("tool_name")
    expired["body"] = f"Expired: {tool_name}" if isinstance(tool_name, str) else "Approval expired"
    return expired


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
