"""Incremental room-member hook baselines and completed delivery markers."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .backend import Transaction


def record_baseline(transaction: Transaction, principal_id: str, room_id: str, user_id: str) -> None:
    """Record the current receipt frontier without backdating duplicate observations."""
    transaction.execute(
        """
        INSERT INTO room_member_joins (principal_id, room_id, user_id, baseline_receipt_order)
        SELECT ?, ?, ?, MAX(receipt_order) FROM journal_events WHERE 1 = 1
        ON CONFLICT (principal_id, room_id, user_id) DO UPDATE SET
            baseline_receipt_order = CASE
                WHEN room_member_joins.baseline_receipt_order IS NULL
                  OR excluded.baseline_receipt_order < room_member_joins.baseline_receipt_order
                THEN excluded.baseline_receipt_order
                ELSE room_member_joins.baseline_receipt_order
            END
        """,
        (principal_id, room_id, user_id),
    )


def is_suppressed(
    transaction: Transaction,
    principal_id: str,
    room_id: str,
    event_id: str,
    user_id: str,
) -> bool:
    """Check the exact admitted join against earlier baselines and completed hooks."""
    row = transaction.fetchone(
        """
        SELECT event.receipt_order, event.source_json, marker.baseline_receipt_order, marker.completed
        FROM journal_events AS event
        LEFT JOIN room_member_joins AS marker
          ON marker.principal_id = event.principal_id AND marker.room_id = event.room_id AND marker.user_id = ?
        WHERE event.principal_id = ? AND event.room_id = ? AND event.event_id = ? AND event.kind = 'room_lifecycle'
        """,
        (user_id, principal_id, room_id, event_id),
    )
    if row is None or json.loads(row["source_json"]).get("state_key") != user_id:
        message = "Membership hook source is missing or does not match the requested room and user"
        raise ValueError(message)
    baseline = row["baseline_receipt_order"]
    return bool(row["completed"]) or (baseline is not None and baseline < row["receipt_order"])


def mark_completed(transaction: Transaction, principal_id: str, room_id: str, user_id: str) -> None:
    """Persist completion only after the hook returns successfully."""
    transaction.execute(
        """
        INSERT INTO room_member_joins (principal_id, room_id, user_id, completed)
        VALUES (?, ?, ?, 1)
        ON CONFLICT (principal_id, room_id, user_id) DO UPDATE SET completed = 1
        """,
        (principal_id, room_id, user_id),
    )
