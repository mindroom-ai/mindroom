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

    from .models import (
        AdmissionResult,
        ConversationCursor,
        ConversationPage,
        DeliveryStage,
        EventKind,
        InboundEvent,
        JournalEvent,
        OutboxDelivery,
        RefreshRequest,
        SemanticConsumer,
        SettlementOutcome,
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
    ) -> tuple[JournalEvent, ...]:
        """Return actionable events awaiting semantic work, in receipt order."""
        ...

    async def is_pending(self, event_id: str) -> bool:
        """Return whether one event still owes semantic work."""
        ...

    async def settle(self, event_id: str, outcome: SettlementOutcome) -> None:
        """Mark one event's semantic work terminal."""
        ...


class DispatchView(ReplayView, AdmissionView, Protocol):
    """Everything the dispatcher coordinates: admission, replay, and claims."""

    async def settle_many(self, event_ids: tuple[str, ...], outcome: SettlementOutcome) -> None:
        """Settle every event that one terminal turn accounted for."""
        ...

    async def unsettled_event_ids(self) -> frozenset[str]:
        """Return every event that still owes semantic work."""
        ...

    async def load_event(self, event_id: str) -> JournalEvent | None:
        """Return one admitted event."""
        ...

    async def pending_of_kind(self, kind: EventKind, *, limit: int = ...) -> tuple[JournalEvent, ...]:
        """Return pending events of one kind, in receipt order."""
        ...

    async def claim_semantic_consumer(
        self,
        event_id: str,
        consumer: SemanticConsumer,
    ) -> SemanticConsumer:
        """Record the sole consumer of one event, returning whoever holds it."""
        ...


class ProjectionView(Protocol):
    """Reading a conversation, without any way to change one."""

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


class ConversationReadView(ProjectionView, Protocol):
    """Reading a conversation plus the evidence needed to judge completeness."""

    async def latest_visible_event_id(self, *, room_id: str, thread_id: str) -> str | None:
        """Return the newest visible event in one thread, or nothing."""
        ...

    async def conversation_is_hydrated(self, *, room_id: str, thread_id: str | None) -> bool:
        """Return whether this conversation was hydrated under current membership."""
        ...

    async def has_other_admitted_room_event(self, *, room_id: str, event_id: str) -> bool:
        """Return whether another event from this room has reached the journal."""
        ...


class HydrationView(Protocol):
    """Building a conversation from the server and repairing one message of it."""

    async def membership_epoch(self, room_id: str) -> int:
        """Return the current membership epoch for one room."""
        ...

    async def conversation_is_hydrated(self, *, room_id: str, thread_id: str | None) -> bool:
        """Return whether this conversation was hydrated under current membership."""
        ...

    async def install_hydrated_conversation(
        self,
        *,
        room_id: str,
        thread_id: str | None,
        events: tuple[ProjectedEvent, ...],
        expected_membership_epoch: int,
    ) -> bool:
        """Install a completed hydration atomically, or install nothing."""
        ...

    async def pending_refreshes(
        self,
        *,
        room_id: str,
        thread_id: str | None,
        limit: int = ...,
    ) -> tuple[RefreshRequest, ...]:
        """Return logical messages in one conversation that owe a point refetch."""
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
    """Delivering what was generated, with no access to the journal behind it."""

    async def enqueue_delivery(
        self,
        *,
        turn_id: str,
        stage: DeliveryStage,
        room_id: str,
        thread_id: str | None,
        payload: Mapping[str, object],
        edits_event_id: str | None = None,
    ) -> str:
        """Record delivery intent and return its deterministic transaction ID."""
        ...

    async def claim_delivery(self, *, turn_id: str, stage: DeliveryStage) -> OutboxDelivery | None:
        """Freeze one delivery before network I/O and return what to send."""
        ...

    async def load_delivery(self, *, turn_id: str, stage: DeliveryStage) -> OutboxDelivery | None:
        """Return one delivery without claiming it."""
        ...

    async def acknowledge_delivery(self, *, turn_id: str, stage: DeliveryStage, event_id: str) -> None:
        """Record the Matrix event one claimed delivery produced."""
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

    async def remember_approval_card(
        self,
        *,
        room_id: str,
        card_event_id: str,
        card: Mapping[str, Any],
    ) -> None:
        """Record one sent approval card as awaiting a decision."""
        ...

    async def forget_approval_card(self, *, card_event_id: str) -> None:
        """Drop one approval card that has reached a terminal state."""
        ...

    async def pending_approval_card(
        self,
        *,
        room_id: str,
        card_event_id: str,
    ) -> dict[str, Any] | None:
        """Return one card still awaiting a decision under this membership."""
        ...

    async def pending_approval_cards(
        self,
        *,
        room_id: str,
        limit: int = ...,
    ) -> tuple[dict[str, Any], ...]:
        """Return one room's cards still awaiting a decision, oldest first."""
        ...


__all__ = [
    "AdmissionView",
    "ApprovalView",
    "ConversationReadView",
    "DispatchView",
    "HydrationView",
    "OutboxView",
    "ProjectionView",
    "ReplayView",
]
