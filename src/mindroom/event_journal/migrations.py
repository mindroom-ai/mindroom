"""Narrow one-time migrations for durable Matrix delivery ownership."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from .identity import delivery_transaction_id, encode_thread_id

if TYPE_CHECKING:
    from .backend import Transaction

_MESSAGE_EVENT_TYPE = "m.room.message"
_APPROVAL_EVENT_TYPE = "io.mindroom.tool_approval"
_LEGACY_UNAVAILABLE_NOTICE_PREFIX = "approval-unavailable:"


def prepare_matrix_delivery_migration(transaction: Transaction, *, postgres: bool) -> bool:
    """Rename legacy tables before current DDL is installed.

    Returns whether approval-card rows must be copied after the new tables
    exist. The caller runs this and ``finish_matrix_delivery_migration`` in the
    same schema transaction, so no process can observe a half-migrated owner.
    """
    if _table_exists(transaction, "response_outbox", postgres=postgres):
        if _table_exists(transaction, "matrix_delivery_outbox", postgres=postgres):
            transaction.execute(
                """
                INSERT INTO matrix_delivery_outbox (
                    principal_id, delivery_id, stage, event_type, room_id, thread_id,
                    transaction_id, payload_json, edits_event_id, attempted,
                    sending_device_id, acknowledged_event_id, created_at_ns
                )
                SELECT principal_id, turn_id, stage, ?, room_id, thread_id,
                       transaction_id, payload_json, edits_event_id, attempted,
                       sending_device_id, acknowledged_event_id, created_at_ns
                FROM response_outbox
                WHERE true
                ON CONFLICT (principal_id, delivery_id, stage) DO NOTHING
                """,
                (_MESSAGE_EVENT_TYPE,),
            )
            transaction.execute("DROP TABLE response_outbox")
        else:
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
        context = _object_json(continuation["context_json"], description="approval continuation context")
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
    """Copy every current-format approval owner into the generic outbox."""
    if not migrate_approvals:
        return
    rows = transaction.fetchall(
        """
        SELECT principal_id, room_id, transaction_id, card_event_id, attempted,
               sending_device_id, card_json, resolution_json, continuation_id,
               continuation_generation, tool_call_id, membership_epoch, created_at_ns
        FROM approval_cards_legacy_delivery
        """,
    )
    for row in rows:
        card = _object_json(row["card_json"], description="approval card")
        content = card.get("content")
        if not isinstance(content, dict):
            msg = "Legacy approval card content is not an object"
            raise TypeError(msg)
        event_type = card.get("type")
        if not isinstance(event_type, str) or not event_type:
            event_type = _APPROVAL_EVENT_TYPE
        delivery_id = str(row["transaction_id"])
        thread_id = content.get("thread_id")
        transaction.execute(
            """
            INSERT INTO matrix_delivery_outbox (
                principal_id, delivery_id, stage, event_type, room_id, thread_id,
                transaction_id, payload_json, edits_event_id, attempted,
                sending_device_id, acknowledged_event_id, created_at_ns
            ) VALUES (?, ?, 'initial', ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)
            ON CONFLICT (principal_id, delivery_id, stage) DO NOTHING
            """,
            (
                str(row["principal_id"]),
                delivery_id,
                event_type,
                str(row["room_id"]),
                encode_thread_id(thread_id if isinstance(thread_id, str) and thread_id else None),
                delivery_id,
                _encoded(content),
                int(row["attempted"]),
                row["sending_device_id"],
                row["card_event_id"],
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
            (
                str(row["principal_id"]),
                delivery_id,
                str(row["continuation_id"]),
                int(row["continuation_generation"]),
                str(row["tool_call_id"]),
                int(row["membership_epoch"]),
            ),
        )
        if row["resolution_json"] is None:
            continue
        resolution = _object_json(row["resolution_json"], description="approval resolution")
        # A departure could decide a legacy card before its first send. Recovery
        # must publish that initial before this edit; ``final`` sorts before
        # ``initial`` when scan timestamps tie, so preserve their causal order.
        transaction.execute(
            """
            INSERT INTO matrix_delivery_outbox (
                principal_id, delivery_id, stage, event_type, room_id, thread_id,
                transaction_id, payload_json, edits_event_id, edit_target_pending, attempted,
                sending_device_id, acknowledged_event_id, created_at_ns
            ) VALUES (?, ?, 'final', ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, ?)
            ON CONFLICT (principal_id, delivery_id, stage) DO NOTHING
            """,
            (
                str(row["principal_id"]),
                delivery_id,
                event_type,
                str(row["room_id"]),
                encode_thread_id(thread_id if isinstance(thread_id, str) and thread_id else None),
                delivery_transaction_id(str(row["principal_id"]), delivery_id, "final"),
                _encoded(resolution),
                row["card_event_id"],
                int(row["card_event_id"] is None),
                int(row["created_at_ns"]) + 1,
            ),
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


def _object_json(value: object, *, description: str) -> dict[str, Any]:
    decoded = json.loads(str(value))
    if not isinstance(decoded, dict):
        msg = f"Stored {description} is not an object"
        raise TypeError(msg)
    return decoded


def _encoded(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
