"""Shared test helpers for Matrix tool approval flows."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from mindroom.approval_manager import ApprovalActionResult, PendingApproval, _ApprovalManager


async def resolve_pending_approval(
    store: _ApprovalManager,
    pending: PendingApproval,
    *,
    status: Literal["approved", "denied", "expired", "cancelled"],
    reason: str | None = None,
) -> ApprovalActionResult:
    """Resolve a pending approval through the same card-response path users exercise."""
    return await store.handle_card_response(
        room_id=pending.room_id,
        sender_id=pending.approver_user_id,
        card_event_id=pending.card_event_id,
        status=status,
        reason=reason,
    )
