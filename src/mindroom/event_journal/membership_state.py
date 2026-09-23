"""Low-level room-membership claims shared by journal writers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .backend import Transaction


def _claim_membership_row(
    transaction: Transaction,
    principal_id: str,
    room_id: str,
) -> tuple[int, bool]:
    """Materialize and lock one membership row, returning its epoch and fence."""
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
    return int(row["epoch"]), bool(row["departure_fenced"])


def claim_active_membership_epoch(
    transaction: Transaction,
    principal_id: str,
    *,
    room_id: str,
) -> int | None:
    """Return the current epoch only while the room membership is active."""
    epoch, departure_fenced = _claim_membership_row(transaction, principal_id, room_id)
    return None if departure_fenced else epoch


def claim_membership_epoch(
    transaction: Transaction,
    principal_id: str,
    *,
    room_id: str,
    expected_membership_epoch: int,
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
    current_epoch, departure_fenced = _claim_membership_row(transaction, principal_id, room_id)
    return current_epoch == expected_membership_epoch and not departure_fenced
