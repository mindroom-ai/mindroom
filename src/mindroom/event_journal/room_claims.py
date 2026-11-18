"""Per-event room claims: one saved owner per room event and purpose, shared by every bot of the install."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .backend import Transaction

_RETENTION_NS = 7 * 24 * 60 * 60 * 10**9


def claim(transaction: Transaction, room_id: str, event_id: str, purpose: str, owner: str, *, now_ns: int) -> str:
    """Save ``owner`` for one room event's purpose unless one is saved, and return the saved owner.

    Each write first removes the claims saved more than seven days before it.
    """
    transaction.execute("DELETE FROM room_event_claims WHERE created_at_ns < ?", (now_ns - _RETENTION_NS,))
    transaction.execute(
        """
        INSERT INTO room_event_claims (room_id, event_id, purpose, owner, created_at_ns)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (room_id, event_id, purpose) DO NOTHING
        """,
        (room_id, event_id, purpose, owner, now_ns),
    )
    row = transaction.fetchone(
        "SELECT owner FROM room_event_claims WHERE room_id = ? AND event_id = ? AND purpose = ?",
        (room_id, event_id, purpose),
    )
    assert row is not None
    return str(row["owner"])
