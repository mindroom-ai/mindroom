"""Find and decide approval cards by the Matrix event a click names, as the approval manager does."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from mindroom.event_journal import ApprovalCardView, CallKey, DeliveryStage, RecordedApprovalDecision
from mindroom.event_journal.models import DURABLE_DELIVERY_ID_KEY

if TYPE_CHECKING:
    from mindroom.event_journal import ApprovalDecisionMetadata, DecisionStatus, PrincipalStore
    from mindroom.event_journal.approval_store import ApprovalStore


async def pending_card(approvals: ApprovalStore, *, room_id: str, card_event_id: str) -> ApprovalCardView | None:
    """Return the live request card an event shows under the card principal's current membership."""
    card = await approvals.approval_card(room_id=room_id, card_event_id=card_event_id)
    if card is not None and not card.terminal and card.current:
        return card
    return None


async def wait_for_pending_card(approvals: ApprovalStore, *, room_id: str, card_event_id: str) -> ApprovalCardView:
    """Return the live request card an event shows once its posting is acknowledged."""
    for _attempt in range(1000):
        card = await pending_card(approvals, room_id=room_id, card_event_id=card_event_id)
        if card is not None:
            return card
        await asyncio.sleep(0.001)
    message = f"Approval card {card_event_id!r} was not posted."
    raise AssertionError(message)


async def card_is_terminal(approvals: ApprovalStore, *, room_id: str, card_event_id: str) -> bool:
    """Return whether no click on the card an event shows can decide anything."""
    card = await approvals.approval_card(room_id=room_id, card_event_id=card_event_id)
    return card is not None and card.terminal


async def resolve_card_event(
    approvals: ApprovalStore,
    *,
    room_id: str,
    card_event_id: str,
    requested_status: DecisionStatus,
    reason: str | None,
    metadata: ApprovalDecisionMetadata | None = None,
) -> RecordedApprovalDecision:
    """Decide the call the card an event shows, as a click does."""
    card = await approvals.approval_card(room_id=room_id, card_event_id=card_event_id)
    if not isinstance(card, ApprovalCardView):
        return RecordedApprovalDecision(resolution=None, recorded=False)
    return await approvals.resolve_approval_call(
        card.call,
        requested_status=requested_status,
        reason=reason,
        metadata=metadata,
    )


async def _payload(store: PrincipalStore, delivery_id: str, stage: DeliveryStage) -> dict[str, object]:
    delivery = await store.load_matrix_delivery(delivery_id=delivery_id, stage=stage)
    assert delivery is not None, f"{delivery_id!r} has no {stage.value} payload"
    return {key: value for key, value in delivery.payload.items() if key != DURABLE_DELIVERY_ID_KEY}


async def terminal_edit(store: PrincipalStore, delivery_id: str) -> dict[str, object]:
    """Return the terminal edit a card's delivery carries, without its private delivery marker."""
    return await _payload(store, delivery_id, DeliveryStage.FINAL)


def card_view(**changes: object) -> ApprovalCardView:
    """Build a live, undecided request card of a paused reply's call, as a manager test stubs the store's read."""
    defaults: dict[str, object] = {
        "delivery_id": "approval-card-1",
        "kind": "request",
        "call": CallKey("approval-1", 0, "call-1"),
        "room_id": "!room:localhost",
        "thread_id": "$thread",
        "card_event_id": "$approval",
        "created_at_ns": 1,
        "approvable": True,
        "retired": False,
        "current": True,
        "owner_kind": "reply",
        "approver_user_id": "@approver:localhost",
        "expires_at_ns": 4_102_444_800_000_000_000,
        "decided": False,
        "invoking_agent": "code",
        "responder_entity_name": "code",
        "offers_timed_approval": False,
        "any_arguments_offered": False,
    }
    return ApprovalCardView(**{**defaults, **changes})  # type: ignore[arg-type]


async def card_content(store: PrincipalStore, card: ApprovalCardView) -> dict[str, object]:
    """Return the frozen payload a card was posted with, without its private delivery marker."""
    return await _payload(store, card.delivery_id, DeliveryStage.INITIAL)
