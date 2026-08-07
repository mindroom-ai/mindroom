"""The principal-bound store view runtime code is given.

One database may hold many bots, but no runtime caller ever sees the column
that separates them. Operational methods take no ``principal_id`` at all, so
reading or settling another bot's rows is not something a caller can express,
rather than something it is trusted not to do.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import journal, outbox, reads
from .projection import (
    drop_refetched_message,
    install_refetched_revision,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from .backend import Backend
    from .models import (
        AdmissionResult,
        ConversationCursor,
        ConversationPage,
        DeliveryStage,
        InboundEvent,
        JournalEvent,
        OutboxDelivery,
        RefreshRequest,
        SettlementOutcome,
    )
    from .projection import ProjectedEvent

DEFAULT_PENDING_LIMIT = 256
DEFAULT_REFRESH_LIMIT = 64
DEFAULT_UNACKNOWLEDGED_LIMIT = 256


@dataclass(frozen=True, slots=True)
class PrincipalStore:
    """Everything one bot may durably do, scoped to that bot."""

    _backend: Backend
    _principal_id: str

    @property
    def principal_id(self) -> str:
        """Return the bound principal, for logging and deterministic IDs."""
        return self._principal_id

    async def admit(
        self,
        event: InboundEvent,
        projected: ProjectedEvent | None = None,
    ) -> AdmissionResult:
        """Admit one event and update the projection in a single transaction."""
        return await self._backend.write(
            lambda transaction: journal.admit(transaction, self._principal_id, event, projected),
        )

    async def pending(self, *, limit: int = DEFAULT_PENDING_LIMIT) -> tuple[JournalEvent, ...]:
        """Return actionable events awaiting semantic work, in receipt order."""
        return await self._backend.read(
            lambda transaction: journal.pending(transaction, self._principal_id, limit=limit),
        )

    async def load_event(self, event_id: str) -> JournalEvent | None:
        """Return one admitted event."""
        return await self._backend.read(
            lambda transaction: journal.load(transaction, self._principal_id, event_id),
        )

    async def is_pending(self, event_id: str) -> bool:
        """Return whether one event still owes semantic work."""
        return await self._backend.read(
            lambda transaction: journal.is_pending(transaction, self._principal_id, event_id),
        )

    async def settle(self, event_id: str, outcome: SettlementOutcome) -> None:
        """Mark one event's semantic work terminal."""
        await self._backend.write(
            lambda transaction: journal.settle(transaction, self._principal_id, event_id, outcome),
        )

    async def membership_epoch(self, room_id: str) -> int:
        """Return the current membership epoch for one room."""
        return await self._backend.read(
            lambda transaction: journal.current_membership_epoch(transaction, self._principal_id, room_id),
        )

    async def advance_membership_epoch(self, room_id: str) -> int:
        """Invalidate hydration for a room whose membership restarted."""
        return await self._backend.write(
            lambda transaction: journal.advance_membership_epoch(transaction, self._principal_id, room_id),
        )

    async def read_conversation(
        self,
        *,
        room_id: str,
        thread_id: str | None,
        limit: int,
        before: ConversationCursor | None = None,
    ) -> ConversationPage:
        """Return one bounded page of a conversation."""
        return await self._backend.read(
            lambda transaction: reads.read_conversation(
                transaction,
                self._principal_id,
                room_id=room_id,
                thread_id=thread_id,
                limit=limit,
                before=before,
            ),
        )

    async def pending_refreshes(
        self,
        *,
        room_id: str,
        thread_id: str | None,
        limit: int = DEFAULT_REFRESH_LIMIT,
    ) -> tuple[RefreshRequest, ...]:
        """Return logical messages in one conversation that owe a point refetch."""
        return await self._backend.read(
            lambda transaction: reads.pending_refreshes(
                transaction,
                self._principal_id,
                room_id=room_id,
                thread_id=thread_id,
                limit=limit,
            ),
        )

    async def conversation_is_hydrated(self, *, room_id: str, thread_id: str | None) -> bool:
        """Return whether this conversation was hydrated under current membership."""
        return await self._backend.read(
            lambda transaction: reads.conversation_is_hydrated(
                transaction,
                self._principal_id,
                room_id=room_id,
                thread_id=thread_id,
            ),
        )

    async def install_hydrated_conversation(
        self,
        *,
        room_id: str,
        thread_id: str | None,
        events: tuple[ProjectedEvent, ...],
        expected_membership_epoch: int,
    ) -> bool:
        """Install a completed hydration atomically, or install nothing.

        A partially applied hydration would look complete to the next reader,
        so the events and the completion marker share one transaction.
        """
        return await self._backend.write(
            lambda transaction: _install_hydration(
                transaction,
                self._principal_id,
                room_id=room_id,
                thread_id=thread_id,
                events=events,
                expected_membership_epoch=expected_membership_epoch,
            ),
        )

    async def install_refetched_revision(
        self,
        request: RefreshRequest,
        *,
        revision_event_id: str,
        revision_ts: int,
        content: Mapping[str, object],
    ) -> bool:
        """Install a point-refetched revision if its refresh token still holds."""
        return await self._backend.write(
            lambda transaction: install_refetched_revision(
                transaction,
                self._principal_id,
                room_id=request.room_id,
                logical_event_id=request.logical_event_id,
                revision_event_id=revision_event_id,
                revision_ts=revision_ts,
                content=content,
                expected_refresh_token=request.refresh_token,
                expected_membership_epoch=request.membership_epoch,
            ),
        )

    async def drop_refetched_message(self, request: RefreshRequest) -> bool:
        """Remove a logical message the server has no remaining revision of."""
        return await self._backend.write(
            lambda transaction: drop_refetched_message(
                transaction,
                self._principal_id,
                room_id=request.room_id,
                logical_event_id=request.logical_event_id,
                expected_refresh_token=request.refresh_token,
                expected_membership_epoch=request.membership_epoch,
            ),
        )

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
        return await self._backend.write(
            lambda transaction: outbox.enqueue(
                transaction,
                self._principal_id,
                turn_id=turn_id,
                stage=stage,
                room_id=room_id,
                thread_id=thread_id,
                payload=payload,
                edits_event_id=edits_event_id,
            ),
        )

    async def claim_delivery(self, *, turn_id: str, stage: DeliveryStage) -> OutboxDelivery | None:
        """Freeze one delivery before network I/O and return what to send."""
        return await self._backend.write(
            lambda transaction: outbox.claim(
                transaction,
                self._principal_id,
                turn_id=turn_id,
                stage=stage,
            ),
        )

    async def load_delivery(self, *, turn_id: str, stage: DeliveryStage) -> OutboxDelivery | None:
        """Return one delivery without claiming it."""
        return await self._backend.read(
            lambda transaction: outbox.load(
                transaction,
                self._principal_id,
                turn_id=turn_id,
                stage=stage,
            ),
        )

    async def acknowledge_delivery(
        self,
        *,
        turn_id: str,
        stage: DeliveryStage,
        event_id: str,
    ) -> None:
        """Record the Matrix event one claimed delivery produced."""
        await self._backend.write(
            lambda transaction: outbox.acknowledge(
                transaction,
                self._principal_id,
                turn_id=turn_id,
                stage=stage,
                event_id=event_id,
            ),
        )

    async def unacknowledged_deliveries(
        self,
        *,
        limit: int = DEFAULT_UNACKNOWLEDGED_LIMIT,
    ) -> tuple[OutboxDelivery, ...]:
        """Return deliveries whose Matrix outcome is unknown, oldest first."""
        return await self._backend.read(
            lambda transaction: outbox.unacknowledged(transaction, self._principal_id, limit=limit),
        )


def _install_hydration(
    transaction,  # noqa: ANN001 - the backend's Transaction, kept structural
    principal_id: str,
    *,
    room_id: str,
    thread_id: str | None,
    events: tuple[ProjectedEvent, ...],
    expected_membership_epoch: int,
) -> bool:
    from .projection import project  # noqa: PLC0415 - keeps the module import-light

    if not reads.mark_conversation_hydrated(
        transaction,
        principal_id,
        room_id=room_id,
        thread_id=thread_id,
        expected_membership_epoch=expected_membership_epoch,
    ):
        return False
    for event in events:
        project(
            transaction,
            principal_id,
            event,
            receipt_order=0,
            membership_epoch=expected_membership_epoch,
        )
    return True


@dataclass(frozen=True, slots=True)
class EventJournalStore:
    """The shared backend, which hands out per-principal views."""

    backend: Backend

    @classmethod
    async def open_sqlite(cls, database_path: Path) -> EventJournalStore:
        """Open a single-writer SQLite store."""
        from .sqlite_backend import SqliteBackend  # noqa: PLC0415 - backend chosen at runtime

        return cls(backend=await SqliteBackend.open(database_path))

    @classmethod
    async def open_postgres(cls, database_url: str) -> EventJournalStore:
        """Open a PostgreSQL store."""
        from .postgres_backend import PostgresBackend  # noqa: PLC0415 - keeps psycopg optional

        return cls(backend=await PostgresBackend.open(database_url))

    def principal(self, principal_id: str) -> PrincipalStore:
        """Return the bound view for one bot."""
        if not principal_id:
            msg = "An event-journal principal requires an identity"
            raise ValueError(msg)
        return PrincipalStore(_backend=self.backend, _principal_id=principal_id)

    async def close(self) -> None:
        """Release every connection the backend owns."""
        await self.backend.close()
