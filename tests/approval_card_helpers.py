"""Find and decide approval cards by the Matrix event a click names, as the approval manager does."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from mindroom.event_journal import ApprovalCardView, CallKey, DeliveryStage, RecordedApprovalDecision
from mindroom.event_journal.models import DURABLE_DELIVERY_ID_KEY

if TYPE_CHECKING:
    from mindroom.event_journal import ApprovalDecisionMetadata, PrincipalStore
    from mindroom.event_journal.approval_store import ApprovalStore


async def pending_card(approvals: ApprovalStore, *, room_id: str, card_event_id: str) -> ApprovalCardView | None:
    """Return the live request card an event shows under the card principal's current membership."""
    card = await approvals.approval_card(room_id=room_id, card_event_id=card_event_id)
    if card is not None and not card.terminal and card.current:
        return card
    return None


async def card_is_terminal(approvals: ApprovalStore, *, room_id: str, card_event_id: str) -> bool:
    """Return whether no click on the card an event shows can decide anything."""
    card = await approvals.approval_card(room_id=room_id, card_event_id=card_event_id)
    return card is not None and card.terminal


async def resolve_card_event(
    approvals: ApprovalStore,
    *,
    room_id: str,
    card_event_id: str,
    requested_status: Literal["approved", "denied", "expired"],
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


async def terminal_edit(store: PrincipalStore, delivery_id: str) -> dict[str, object]:
    """Return the terminal edit a card's delivery carries, without its private delivery marker."""
    final = await store.load_matrix_delivery(delivery_id=delivery_id, stage=DeliveryStage.FINAL)
    assert final is not None, f"{delivery_id!r} has no terminal edit"
    return {key: value for key, value in final.payload.items() if key != DURABLE_DELIVERY_ID_KEY}


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
    initial = await store.load_matrix_delivery(delivery_id=card.delivery_id, stage=DeliveryStage.INITIAL)
    assert initial is not None, f"{card.delivery_id!r} has no posted payload"
    return {key: value for key, value in initial.payload.items() if key != DURABLE_DELIVERY_ID_KEY}
