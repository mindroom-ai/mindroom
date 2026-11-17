"""Per-event room claims: one saved owner per room event and purpose, shared by every bot of the install."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .backend import Transaction


def claim(transaction: Transaction, room_id: str, event_id: str, purpose: str, owner: str) -> str:
    """Save ``owner`` for one room event's purpose unless one is saved, and return the saved owner."""
    transaction.execute(
        """
        INSERT INTO room_event_claims (room_id, event_id, purpose, owner, created_at_ns)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (room_id, event_id, purpose) DO NOTHING
        """,
        (room_id, event_id, purpose, owner, time.time_ns()),
    )
    return claims(transaction, room_id, (event_id,), purpose)[event_id]


def claims(transaction: Transaction, room_id: str, event_ids: Sequence[str], purpose: str) -> dict[str, str]:
    """Return the saved owner of each of these room events that has one for ``purpose``."""
    saved: dict[str, str] = {}
    for event_id in event_ids:
        row = transaction.fetchone(
            "SELECT owner FROM room_event_claims WHERE room_id = ? AND event_id = ? AND purpose = ?",
            (room_id, event_id, purpose),
        )
        if row is not None:
            saved[event_id] = str(row["owner"])
    return saved
