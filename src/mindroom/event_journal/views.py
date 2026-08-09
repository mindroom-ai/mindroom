"""The narrow slices of a principal's store that collaborators actually use.

`PrincipalStore` is one object with twenty-eight methods covering admission,
replay, membership, conversation reads, hydration, refetch, and delivery. That
is the shape of the universal cache dependency this design exists to remove:
once every collaborator holds the whole surface, any of them can reach for any
part of it, and the boundaries stop being real.

These protocols are what each collaborator is actually allowed to do. They are
structural, so `PrincipalStore` satisfies them without declaring anything, and
they cost nothing at runtime -- the enforcement is the type checker refusing a
call the annotation does not permit. The point is not to hide the store; it is
that a hydrator reaching into the outbox should fail review by failing to
type-check.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.history_recovery import (
        HistoryRecoveryOutcome,
        RoomHistoryRecovery,
    )

    from .approvals import RecordedApprovalDecision, StoredApprovalCard
    from .models import (
        AdmissionResult,
        ConversationCursor,
        ConversationPage,
        DeliveryAcknowledgement,
        DeliveryStage,
        EventKind,
        HydrationCoverage,
        InboundEvent,
        JournalEvent,
        OutboxDelivery,
        PendingPage,
        RefreshRequest,
        SemanticConsumer,
        TerminalTurnWrite,
    )
    from .projection import ProjectedEvent


class AdmissionView(Protocol):
    """Accepting one inbound event durably, and nothing else."""

    async def admit(
        self,
        event: InboundEvent,
        projected: ProjectedEvent | None = None,
    ) -> AdmissionResult:
        """Admit one event and update the projection in a single transaction."""
        ...


class ReplayView(Protocol):
    """Draining and settling the work the journal still owes."""

    async def pending(
        self,
        *,
        limit: int = ...,
        after_receipt_order: int | None = None,
    ) -> PendingPage:
        """Return actionable events awaiting semantic work, in receipt order."""
        ...

    async def is_pending(self, event_id: str) -> bool:
        """Return whether one event still owes semantic work."""
        ...

    async def settle(self, event_id: str) -> None:
        """Mark one event's semantic work terminal."""
        ...


class DispatchView(ReplayView, AdmissionView, Protocol):
    """Everything the dispatcher coordinates: admission, replay, and claims."""

    async def settle_many(self, event_ids: tuple[str, ...]) -> None:
        """Settle every event that one terminal turn accounted for."""
        ...

    async def unsettled_event_ids(self) -> frozenset[str]:
        """Return every event that still owes semantic work."""
        ...

    async def load_event(self, event_id: str) -> JournalEvent | None:
        """Return one admitted event."""
        ...

    async def pending_of_kind(
        self,
        kind: EventKind,
        *,
        limit: int = ...,
        after_receipt_order: int | None = None,
    ) -> PendingPage:
        """Return pending events of one kind, in receipt order."""
        ...

    async def claim_semantic_consumer(
        self,
        event_id: str,
        consumer: SemanticConsumer,
    ) -> SemanticConsumer:
        """Record the sole consumer of one event, returning whoever holds it."""
        ...


class PendingTurnView(Protocol):
    """Asking what unfinished work one conversation is already holding.

    Deliberately not part of ``ReplayView``. The caller is a guard deciding
    whether an older turn is still worth running, and it must be able to see
    the pending set without being able to settle any of it.
    """

    async def pending_thread_events_after(
        self,
        *,
        room_id: str,
        thread_id: str,
        after_origin_server_ts: int,
        excluding_event_id: str,
        limit: int = ...,
    ) -> tuple[JournalEvent, ...]:
        """Return unsettled events in one thread newer than a timestamp."""
        ...


class RelationView(Protocol):
    """Asking what the journal already knows about one event's place in a thread."""

    async def admitted_thread_id(self, *, room_id: str, event_id: str) -> tuple[bool, str | None]:
        """Return whether one event was admitted, and which thread it belongs to."""
        ...


class ConversationReadView(Protocol):
    """Reading a conversation, plus the evidence needed to judge completeness.

    Nothing here can change a conversation, which is the point: a reader that
    could write one is a reader that can be made to.
    """

    async def read_conversation(
        self,
        *,
        room_id: str,
        thread_id: str | None,
        limit: int,
        before: ConversationCursor | None = None,
    ) -> ConversationPage:
        """Return one bounded page of a conversation."""
        ...

    async def latest_visible_event_id(self, *, room_id: str, thread_id: str) -> str | None:
        """Return the newest visible event in one thread, or nothing."""
        ...

    async def conversation_is_hydrated(self, *, room_id: str, thread_id: str | None) -> bool:
        """Return whether this conversation was hydrated under current membership."""
        ...

    async def conversation_hydration_was_truncated(self, *, room_id: str, thread_id: str | None) -> bool:
        """Return whether a walk ran for this conversation and stopped at a ceiling."""
        ...


class HistoryRecoveryRecordView(Protocol):
    """Writing down a skipped room-history interval, and nothing else.

    Deliberately one method. The writer is sync certification, which has to make
    the hole durable before it moves the checkpoint past it; giving that caller
    anything it could read would let a decision about the transport start
    depending on the state of the projection.
    """

    async def record_room_history_recovery(
        self,
        room_id: str,
    ) -> RoomHistoryRecovery | None:
        """Write down that a room's history could not be proven continuous."""
        ...


class HydrationView(Protocol):
    """Building a conversation from the server and repairing one message of it."""

    async def membership_epoch(self, room_id: str) -> int:
        """Return the current membership epoch for one room."""
        ...

    async def room_history_recovery(self, room_id: str) -> RoomHistoryRecovery | None:
        """Return one room's current history-recovery obligation, if any."""
        ...

    async def settle_room_history_recovery(
        self,
        recovery: RoomHistoryRecovery,
        *,
        events: tuple[ProjectedEvent, ...],
        exhausted_server: bool,
        attempted_policy_rank: int,
        expected_membership_epoch: int,
    ) -> HistoryRecoveryOutcome:
        """Install a recovery in chunks, then publish and settle atomically."""
        ...

    async def conversation_is_hydrated(self, *, room_id: str, thread_id: str | None) -> bool:
        """Return whether this conversation was hydrated under current membership."""
        ...

    async def conversation_hydration_coverage(
        self,
        *,
        room_id: str,
        thread_id: str | None,
    ) -> HydrationCoverage | None:
        """Return what walks under this membership proved here, or nothing if none did."""
        ...

    async def install_hydrated_conversation(
        self,
        *,
        room_id: str,
        thread_id: str | None,
        events: tuple[ProjectedEvent, ...],
        complete: bool,
        attempted_policy_rank: int = 0,
        expected_membership_epoch: int,
    ) -> bool:
        """Install hydration chunks, then publish their completed marker."""
        ...

    async def install_refetched_revision(
        self,
        request: RefreshRequest,
        *,
        revision_event_id: str,
        revision_ts: int,
        content: Mapping[str, object],
    ) -> bool:
        """Install a point-refetched revision if its refresh token still holds."""
        ...

    async def drop_refetched_message(self, request: RefreshRequest) -> bool:
        """Remove a logical message the server has no remaining revision of."""
        ...


class OutboxView(Protocol):
    """Delivering what was generated, plus the one journal fact delivery owns.

    That exception is the handoff, and it is narrower than it looks: the only
    thing this view may say about the journal is "these sources are answered
    now", and it may only say it in the same breath as recording the answer.
    Nothing here can read the journal, replay it, or settle a source that no
    delivery accounts for.
    """

    async def enqueue_delivery(
        self,
        *,
        turn_id: str,
        stage: DeliveryStage,
        room_id: str,
        thread_id: str | None,
        payload: Mapping[str, object],
        edits_event_id: str | None = None,
        settle_source_event_ids: tuple[str, ...] = (),
    ) -> str | None:
        """Record delivery intent and settle what it answers, or refuse both."""
        ...

    async def turn_membership_is_current(self, *, turn_id: str, room_id: str) -> bool:
        """Return whether a turn still speaks for the room's current membership."""
        ...

    async def claim_delivery(self, *, turn_id: str, stage: DeliveryStage) -> OutboxDelivery | None:
        """Freeze one delivery before network I/O and return the row as it stood."""
        ...

    async def record_sending_device(
        self,
        *,
        turn_id: str,
        stage: DeliveryStage,
        device_id: str | None,
    ) -> None:
        """Record the device namespace this delivery is about to send under."""
        ...

    async def load_delivery(self, *, turn_id: str, stage: DeliveryStage) -> OutboxDelivery | None:
        """Return one delivery without claiming it."""
        ...

    async def acknowledge_delivery(
        self,
        *,
        turn_id: str,
        stage: DeliveryStage,
        event_id: str,
        terminal_turn: TerminalTurnWrite | None = None,
    ) -> DeliveryAcknowledgement:
        """Record the delivery's event and the turn it completes; name the event and who bound it."""
        ...

    async def unacknowledged_deliveries(
        self,
        *,
        limit: int = ...,
        after: tuple[int, str, str] | None = None,
    ) -> tuple[OutboxDelivery, ...]:
        """Return deliveries whose Matrix outcome is unknown, oldest first."""
        ...


class ApprovalView(Protocol):
    """The approval cards this bot owes a decision on, and nothing else."""

    async def claim_approval_card(
        self,
        *,
        room_id: str,
        transaction_id: str,
        card: Mapping[str, Any],
    ) -> None:
        """Record one approval card as awaiting a decision, before it is sent.

        Committed ahead of the send so that nothing clickable can exist without
        a row that accounts for it. The row is unattempted until the send is
        actually about to run, because a claim alone proves nothing reached the
        room.
        """
        ...

    async def mark_approval_card_attempted(
        self,
        *,
        transaction_id: str,
        sending_device_id: str | None,
    ) -> bool:
        """Record that one claimed card is about to be offered, and from which device.

        Committed before the send, because the fact that has to survive a crash
        is that something may already be in the room under this transaction.
        Returns whether a row was still there to mark; nothing may be sent for
        one that has gone.
        """
        ...

    async def acknowledge_approval_card(
        self,
        *,
        transaction_id: str,
        card_event_id: str,
        card: Mapping[str, Any],
    ) -> None:
        """Record the Matrix event one claimed approval card became."""
        ...

    async def resolve_approval_card(
        self,
        *,
        card_event_id: str,
        resolution: Mapping[str, Any],
    ) -> RecordedApprovalDecision:
        """Record the decision one card carries, before it is shown.

        Returns what the durable row ends up carrying, because an update that
        matched nothing is indistinguishable from one that committed unless
        the store says so.
        """
        ...

    async def forget_approval_card(self, *, transaction_id: str) -> None:
        """Drop one approval card whose decision the room now shows, or that was never sent."""
        ...

    async def pending_approval_card(
        self,
        *,
        room_id: str,
        card_event_id: str,
    ) -> StoredApprovalCard | None:
        """Return one card this bot still owes work on under this membership."""
        ...

    async def pending_approval_cards(
        self,
        *,
        room_id: str,
        limit: int = ...,
        after: tuple[int, str] | None = None,
    ) -> tuple[StoredApprovalCard, ...]:
        """Return one room's unfinished cards, oldest first."""
        ...


__all__ = [
    "AdmissionView",
    "ApprovalView",
    "ConversationReadView",
    "DispatchView",
    "HistoryRecoveryRecordView",
    "HydrationView",
    "OutboxView",
    "PendingTurnView",
    "RelationView",
    "ReplayView",
]
