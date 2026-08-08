"""Shared test helpers for Matrix tool approval flows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from mindroom.event_journal import RecordedApprovalDecision, StoredApprovalCard

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.approval_manager import ApprovalActionResult, PendingApproval, _ApprovalManager

# The device a seeded claim was made from. A test that wants recovery to
# present the frozen transaction again has to run as this device, because that
# is the only one the homeserver would deduplicate it against.
CLAIMING_DEVICE_ID = "CLAIMINGDEVICE"


@dataclass
class _StoredRow:
    """One row of the card table, keyed by the transaction the send used."""

    room_id: str
    card: dict[str, Any]
    # None until the send comes back. Such a row is a card whose presence in
    # the room is unknown, which is a different thing from a card that is not
    # there, and the two must not be allowed to look alike here.
    card_event_id: str | None = None
    resolution: dict[str, Any] | None = None
    # Whether the send was ever reached. False is the one state that proves the
    # room holds nothing for this row, so recovery may drop it unasked.
    attempted: bool = False
    # The device the attempt used. Only that device's repeat of the frozen
    # transaction collapses onto the same event, so a row attempted elsewhere
    # is one recovery must reconcile against the room rather than present
    # again.
    sending_device_id: str | None = None
    # Claim order, which is the order the room scan reads in and therefore what
    # a paging caller resumes from.
    created_at_ns: int = 0


class FakeApprovalCards:
    """The cards a bot still owes work on, as the durable store keeps them.

    A decision is written before it is shown and the row is dropped once the
    room shows it, so a row carrying a resolution is an answer whose delivery
    is in doubt. The store only ever holds cards this bot authored, which is
    why nothing here can model a foreign edit: one cannot reach it.

    A row is claimed before its card is sent and keyed on the transaction, not
    on the event id, so the unacknowledged state is representable -- a double
    that could only hold sent cards would make the crash window this ordering
    exists to close impossible to write a test for.

    Recording a decision is a guarded update against a real table, and its
    interesting failures are silent: a card that was never stored updates no
    row, and a card that already carries a decision refuses to take another.
    A double that could only fail by raising would let either of those pass
    for a commit, which is exactly the confusion the real store must not have.
    """

    def __init__(self) -> None:
        self.rows: dict[str, _StoredRow] = {}
        self.lookups: list[tuple[str, str]] = []
        # Transactions this instance wrote a row for, so a test can see
        # redundant writes, and the ones a send outcome later settled.
        self.claimed: list[str] = []
        self.attempted: list[tuple[str, str | None]] = []
        self.acknowledged: list[tuple[str, str]] = []
        # Stands in for the claim timestamp the real table records, which is
        # what orders the room scan and what a page cursor is built from.
        self._claims = 0

    @property
    def resolutions(self) -> dict[str, dict[str, Any]]:
        """The decisions committed so far, by card event id."""
        return {
            row.card_event_id: row.resolution
            for row in self.rows.values()
            if row.card_event_id is not None and row.resolution is not None
        }

    def stored_event_ids(self) -> set[str]:
        """The cards the store can be asked about by event id.

        Excludes claims no send has come back from on purpose: those are rows,
        but not yet cards anything can look up or edit.
        """
        return {row.card_event_id for row in self.rows.values() if row.card_event_id is not None}

    def _row_for_event(self, card_event_id: str) -> _StoredRow | None:
        return next((row for row in self.rows.values() if row.card_event_id == card_event_id), None)

    async def claim_approval_card(
        self,
        *,
        room_id: str,
        transaction_id: str,
        card: Mapping[str, Any],
    ) -> None:
        """Record one card as pending before it is sent, keeping the first body seen."""
        if transaction_id in self.rows:
            return
        self.claimed.append(transaction_id)
        self._claims += 1
        self.rows[transaction_id] = _StoredRow(
            room_id=room_id,
            card=dict(card),
            created_at_ns=self._claims,
        )

    async def mark_approval_card_attempted(
        self,
        *,
        transaction_id: str,
        sending_device_id: str | None,
    ) -> bool:
        """Record that a send is about to be made, and from which device."""
        row = self.rows.get(transaction_id)
        if row is None:
            return False
        self.attempted.append((transaction_id, sending_device_id))
        row.attempted = True
        row.sending_device_id = sending_device_id
        return True

    async def acknowledge_approval_card(
        self,
        *,
        transaction_id: str,
        card_event_id: str,
        card: Mapping[str, Any],
    ) -> None:
        """Point one claimed row at the event its send produced, once only."""
        row = self.rows.get(transaction_id)
        if row is None or row.card_event_id is not None:
            return
        self.acknowledged.append((transaction_id, card_event_id))
        row.card_event_id = card_event_id
        row.card = dict(card)

    async def resolve_approval_card(
        self,
        *,
        card_event_id: str,
        resolution: Mapping[str, Any],
    ) -> RecordedApprovalDecision:
        """Commit one decision only against a stored card that has none yet."""
        row = self._row_for_event(card_event_id)
        if row is None:
            return RecordedApprovalDecision(resolution=None, recorded=False)
        if row.resolution is not None:
            return RecordedApprovalDecision(resolution=dict(row.resolution), recorded=False)
        row.resolution = dict(resolution)
        return RecordedApprovalDecision(resolution=dict(resolution), recorded=True)

    async def forget_approval_card(self, *, transaction_id: str) -> None:
        """Drop one card that is finished, shown or never sent."""
        self.rows.pop(transaction_id, None)

    async def pending_approval_card(self, *, room_id: str, card_event_id: str) -> StoredApprovalCard | None:
        """Return one stored card, recording that the point lookup was used."""
        self.lookups.append((room_id, card_event_id))
        entry = next(
            (
                (transaction_id, row)
                for transaction_id, row in self.rows.items()
                if row.card_event_id == card_event_id and row.room_id == room_id
            ),
            None,
        )
        return None if entry is None else _stored(*entry)

    async def pending_approval_cards(
        self,
        *,
        room_id: str,
        limit: int = 256,
        after: tuple[int, str] | None = None,
    ) -> tuple[StoredApprovalCard, ...]:
        """Return one page of a room's stored cards, acknowledged or not."""
        ordered = sorted(
            (_stored(transaction_id, row) for transaction_id, row in self.rows.items() if row.room_id == room_id),
            key=lambda card: (card.created_at_ns, card.transaction_id),
        )
        if after is not None:
            ordered = [card for card in ordered if (card.created_at_ns, card.transaction_id) > after]
        return tuple(ordered[:limit])

    async def store_card(self, card_event_id: str, room_id: str, card: dict[str, Any]) -> None:
        """Seed one card as if a previous process had sent it and recorded the event."""
        transaction_id = transaction_id_for(card_event_id)
        await self.claim_approval_card(room_id=room_id, transaction_id=transaction_id, card=card)
        await self.mark_approval_card_attempted(
            transaction_id=transaction_id,
            sending_device_id=CLAIMING_DEVICE_ID,
        )
        await self.acknowledge_approval_card(
            transaction_id=transaction_id,
            card_event_id=card_event_id,
            card=card,
        )

    async def store_unsent_card(
        self,
        transaction_id: str,
        room_id: str,
        card: dict[str, Any],
        *,
        sending_device_id: str | None = CLAIMING_DEVICE_ID,
        attempted: bool = True,
    ) -> None:
        """Seed one card as if a previous process had claimed it and then died.

        Attempted from ``CLAIMING_DEVICE_ID`` by default, which is the row a
        crash around the send leaves: something may be in the room. Pass a
        different device, or None, to seed the row whose transaction a recovery
        pass cannot present again; pass ``attempted=False`` for the narrower
        crash between the claim and the send, where nothing can have left.
        """
        await self.claim_approval_card(
            room_id=room_id,
            transaction_id=transaction_id,
            card=card,
        )
        if attempted:
            await self.mark_approval_card_attempted(
                transaction_id=transaction_id,
                sending_device_id=sending_device_id,
            )


def transaction_id_for(card_event_id: str) -> str:
    """Return a stable stand-in transaction for a card a test seeds by event id."""
    return f"txn-for-{card_event_id}"


def _stored(transaction_id: str, row: _StoredRow) -> StoredApprovalCard:
    return StoredApprovalCard(
        card=row.card,
        resolution=row.resolution,
        transaction_id=transaction_id,
        card_event_id=row.card_event_id,
        attempted=row.attempted,
        sending_device_id=row.sending_device_id,
        created_at_ns=row.created_at_ns,
    )


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


class UnclaimableApprovalCards(FakeApprovalCards):
    """A store whose claim on a card fails, so the card must never be sent."""

    async def claim_approval_card(
        self,
        *,
        room_id: str,  # noqa: ARG002 - matches the view it stands in for
        transaction_id: str,
        card: Mapping[str, Any],  # noqa: ARG002 - matches the view it stands in for
    ) -> None:
        """Fail loudly, the way a store that cannot take the row does."""
        msg = f"cannot claim the card {transaction_id!r}"
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
