"""Shared test helpers for Matrix tool approval flows."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from mindroom.event_journal import RecordedApprovalDecision, StoredApprovalCard

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.approval_manager import ApprovalActionResult, PendingApproval, _ApprovalManager


class FakeApprovalCards:
    """The cards a bot still owes work on, as the durable store keeps them.

    A decision is written before it is shown and the row is dropped once the
    room shows it, so a row carrying a resolution is an answer whose delivery
    is in doubt. The store only ever holds cards this bot authored, which is
    why nothing here can model a foreign edit: one cannot reach it.

    Recording a decision is a guarded update against a real table, and its
    interesting failures are silent: a card that was never stored updates no
    row, and a card that already carries a decision refuses to take another.
    A double that could only fail by raising would let either of those pass
    for a commit, which is exactly the confusion the real store must not have.
    """

    def __init__(self) -> None:
        self.cards: dict[str, tuple[str, dict[str, Any]]] = {}
        self.resolutions: dict[str, dict[str, Any]] = {}
        self.lookups: list[tuple[str, str]] = []
        # What the room shows, which is not the same thing as what has a row.
        self.projected: list[Any] = []
        # Cards this instance wrote a row for, so a test can see redundant writes.
        self.remembered: list[str] = []

    async def room_messages_from_sender(
        self,
        *,
        room_id: str,
        sender: str,
        limit: int = 256,
    ) -> tuple[Any, ...]:
        """Return this bot's visible messages in a room, as the projection holds them.

        Populated by sync echo rather than by the card bookkeeping, so a card
        can appear here while its row is missing. That divergence is the whole
        thing under test.
        """
        del limit
        return tuple(m for m in self.projected if m.room_id == room_id and m.sender == sender)

    async def remember_approval_card(
        self,
        *,
        room_id: str,
        card_event_id: str,
        card: Mapping[str, Any],
    ) -> None:
        """Record one sent card as pending, keeping the first body seen."""
        if card_event_id not in self.cards:
            self.remembered.append(card_event_id)
        self.cards.setdefault(card_event_id, (room_id, dict(card)))

    async def resolve_approval_card(
        self,
        *,
        card_event_id: str,
        resolution: Mapping[str, Any],
    ) -> RecordedApprovalDecision:
        """Commit one decision only against a stored card that has none yet."""
        if card_event_id not in self.cards:
            return RecordedApprovalDecision(resolution=None, recorded=False)
        stored = self.resolutions.get(card_event_id)
        if stored is not None:
            return RecordedApprovalDecision(resolution=dict(stored), recorded=False)
        self.resolutions[card_event_id] = dict(resolution)
        return RecordedApprovalDecision(resolution=dict(resolution), recorded=True)

    async def forget_approval_card(self, *, card_event_id: str) -> None:
        """Drop one card whose decision the room now shows."""
        self.cards.pop(card_event_id, None)
        self.resolutions.pop(card_event_id, None)

    async def pending_approval_card(self, *, room_id: str, card_event_id: str) -> StoredApprovalCard | None:
        """Return one stored card, recording that the point lookup was used."""
        self.lookups.append((room_id, card_event_id))
        entry = self.cards.get(card_event_id)
        if entry is None or entry[0] != room_id:
            return None
        return StoredApprovalCard(card=entry[1], resolution=self.resolutions.get(card_event_id))

    async def pending_approval_cards(self, *, room_id: str, limit: int = 256) -> tuple[StoredApprovalCard, ...]:
        """Return one room's stored cards."""
        return tuple(
            StoredApprovalCard(card=card, resolution=self.resolutions.get(card["event_id"]))
            for card_room, card in self.cards.values()
            if card_room == room_id
        )[:limit]

    async def store_card(self, card_event_id: str, room_id: str, card: dict[str, Any]) -> None:
        """Seed one card as if a previous process had sent it."""
        await self.remember_approval_card(room_id=room_id, card_event_id=card_event_id, card=card)


class UnwritableApprovalCards(FakeApprovalCards):
    """A store that remembers cards but raises instead of committing a decision."""

    async def resolve_approval_card(
        self,
        *,
        card_event_id: str,
        resolution: Mapping[str, Any],  # noqa: ARG002 - matches the view it stands in for
    ) -> RecordedApprovalDecision:
        """Fail loudly, the way a broken write does."""
        msg = f"cannot record a decision for {card_event_id!r}"
        raise RuntimeError(msg)


class UnrememberableApprovalCards(FakeApprovalCards):
    """A store whose write of a sent card fails, leaving nothing to recover."""

    async def remember_approval_card(
        self,
        *,
        room_id: str,  # noqa: ARG002 - matches the view it stands in for
        card_event_id: str,
        card: Mapping[str, Any],  # noqa: ARG002 - matches the view it stands in for
    ) -> None:
        """Fail loudly, leaving the card visible in the room and stored nowhere."""
        msg = f"cannot record the card {card_event_id!r}"
        raise RuntimeError(msg)


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
