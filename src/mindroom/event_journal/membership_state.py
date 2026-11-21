"""Low-level room-membership claims shared by journal writers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .backend import Transaction


def claim_active_membership_epoch(
    transaction: Transaction,
    principal_id: str,
    *,
    room_id: str,
) -> int | None:
    """Lock the membership row and return its epoch only while the membership is active."""
    row = transaction.fetchone(
        """
        INSERT INTO room_membership (principal_id, room_id, membership_epoch)
        VALUES (?, ?, 0)
        ON CONFLICT (principal_id, room_id) DO UPDATE
            SET membership_epoch = room_membership.membership_epoch
        RETURNING membership_epoch AS epoch, departure_fenced
        """,
        (principal_id, room_id),
    )
    if row is None:
        msg = f"Room membership for {room_id!r} is missing immediately after it was claimed"
        raise RuntimeError(msg)
    return None if row["departure_fenced"] else int(row["epoch"])


def current_membership_epoch(
    transaction: Transaction,
    principal_id: str,
    room_id: str,
) -> int:
    """Return the room's current membership epoch, starting at zero."""
    row = transaction.fetchone(
        "SELECT membership_epoch FROM room_membership WHERE principal_id = ? AND room_id = ?",
        (principal_id, room_id),
    )
    return 0 if row is None else int(row["membership_epoch"])


def projection_generation(transaction: Transaction, principal_id: str, room_id: str) -> int:
    """Return how often this principal left the room; each departure dropped what it had projected there."""
    row = transaction.fetchone(
        "SELECT membership_epoch FROM matrix_ingestion_membership WHERE principal_id = ? AND room_id = ?",
        (principal_id, room_id),
    )
    return 0 if row is None else int(row["membership_epoch"])


def claim_membership_epoch(
    transaction: Transaction,
    principal_id: str,
    *,
    room_id: str,
    expected_membership_epoch: int,
    expected_projection_generation: int | None = None,
) -> bool:
    """Materialize and claim the expected active room membership for one write.

    A write claim rather than a ``SELECT`` takes the same row lock as the
    membership fence. The two operations therefore have a total order: work
    committed before a fence is removed or retired by it, while a writer after
    the fence refuses work until the room has rejoined, even if it captured the
    advanced epoch while the departure cleanup was still pending.

    Materializing epoch zero matters because a lock on a row that does not yet
    exist orders nothing. The inserted row says exactly what absence said while
    giving the first departure a durable row to fence against.
    """
    active_epoch = claim_active_membership_epoch(transaction, principal_id, room_id=room_id)
    # The owner account's epoch never advances, so a history walk also claims the projection it started under.
    generation = expected_projection_generation
    if generation is not None and generation != projection_generation(transaction, principal_id, room_id):
        return False
    return active_epoch == expected_membership_epoch
