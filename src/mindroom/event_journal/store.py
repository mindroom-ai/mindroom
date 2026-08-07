"""The principal-bound store view runtime code is given.

One database may hold many bots, but no runtime caller ever sees the column
that separates them. Operational methods take no ``principal_id`` at all, so
reading or settling another bot's rows is not something a caller can express,
rather than something it is trusted not to do.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import approvals, journal, outbox, reads
from .approvals import (  # noqa: TC001 - part of this module's runtime return types
    RecordedApprovalDecision,
    StoredApprovalCard,
)
from .models import SettlementOutcome
from .projection import drop_refetched_message, install_refetched_revision

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from .backend import Backend
    from .models import (
        AdmissionResult,
        ConversationCursor,
        ConversationPage,
        DeliveryStage,
        DepartureOutcome,
        DepartureSource,
        EventKind,
        InboundEvent,
        JournalEvent,
        OutboxDelivery,
        RefreshRequest,
        SemanticConsumer,
        VisibleMessage,
    )
    from .projection import ProjectedEvent

_DEFAULT_PENDING_LIMIT = 256
_DEFAULT_REFRESH_LIMIT = 64
_DEFAULT_UNACKNOWLEDGED_LIMIT = 256
_DEFAULT_ROOM_CARD_LIMIT = 256


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

    async def pending(
        self,
        *,
        limit: int = _DEFAULT_PENDING_LIMIT,
        after_receipt_order: int | None = None,
    ) -> tuple[JournalEvent, ...]:
        """Return actionable events awaiting semantic work, in receipt order."""
        return await self._backend.read(
            lambda transaction: journal.pending(
                transaction,
                self._principal_id,
                limit=limit,
                after_receipt_order=after_receipt_order,
            ),
        )

    async def load_event(self, event_id: str) -> JournalEvent | None:
        """Return one admitted event."""
        return await self._backend.read(
            lambda transaction: journal.load(transaction, self._principal_id, event_id),
        )

    async def has_other_admitted_room_event(self, *, room_id: str, event_id: str) -> bool:
        """Return whether another event from this room has reached the journal."""
        return await self._backend.read(
            lambda transaction: journal.has_other_admitted_room_event(
                transaction,
                self._principal_id,
                room_id=room_id,
                event_id=event_id,
            ),
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

    async def settle_many(self, event_ids: tuple[str, ...], outcome: SettlementOutcome) -> None:
        """Settle every event that one terminal turn accounted for."""
        if not event_ids:
            return
        await self._backend.write(
            lambda transaction: journal.settle_many(transaction, self._principal_id, event_ids, outcome),
        )

    async def unsettled_event_ids(self) -> frozenset[str]:
        """Return every event that still owes semantic work."""
        return await self._backend.read(
            lambda transaction: journal.unsettled_event_ids(transaction, self._principal_id),
        )

    async def pending_of_kind(
        self,
        kind: EventKind,
        *,
        limit: int = _DEFAULT_PENDING_LIMIT,
    ) -> tuple[JournalEvent, ...]:
        """Return pending events of one kind, in receipt order."""
        return await self._backend.read(
            lambda transaction: journal.pending_of_kind(transaction, self._principal_id, kind, limit=limit),
        )

    async def pending_thread_events_after(
        self,
        *,
        room_id: str,
        thread_id: str,
        after_origin_server_ts: int,
        excluding_event_id: str,
        limit: int = _DEFAULT_PENDING_LIMIT,
    ) -> tuple[JournalEvent, ...]:
        """Return unsettled events in one thread newer than a timestamp."""
        return await self._backend.read(
            lambda transaction: journal.pending_thread_events_after(
                transaction,
                self._principal_id,
                room_id=room_id,
                thread_id=thread_id,
                after_origin_server_ts=after_origin_server_ts,
                excluding_event_id=excluding_event_id,
                limit=limit,
            ),
        )

    async def claim_semantic_consumer(
        self,
        event_id: str,
        consumer: SemanticConsumer,
    ) -> SemanticConsumer:
        """Record the sole consumer of one event, returning whoever holds it."""
        return await self._backend.write(
            lambda transaction: journal.claim_semantic_consumer(
                transaction,
                self._principal_id,
                event_id,
                consumer,
            ),
        )

    async def admitted_thread_id(self, *, room_id: str, event_id: str) -> tuple[bool, str | None]:
        """Return whether one event was admitted, and which thread it belongs to."""
        return await self._backend.read(
            lambda transaction: journal.admitted_thread_id(
                transaction,
                self._principal_id,
                room_id=room_id,
                event_id=event_id,
            ),
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

    async def fence_departure(self, room_id: str, *, source: DepartureSource) -> DepartureOutcome:
        """Apply one observation of a departure, invalidating at most once per departure."""
        return await self._backend.write(
            lambda transaction: journal.fence_departure(
                transaction,
                self._principal_id,
                room_id,
                source=source,
            ),
        )

    async def note_membership_restarted(self, room_id: str) -> None:
        """Record a confirmed join, so the room's next departure fences again."""
        await self._backend.write(
            lambda transaction: journal.note_membership_restarted(transaction, self._principal_id, room_id),
        )

    async def retire_owed_departure_reports(self, room_id: str) -> None:
        """Forget sync reports that can no longer arrive for one room."""
        await self._backend.write(
            lambda transaction: journal.retire_owed_departure_reports(transaction, self._principal_id, room_id),
        )

    async def rooms_owing_departure_reports(self) -> frozenset[str]:
        """Return every room whose local departure is still owed a sync report."""
        return await self._backend.read(
            lambda transaction: journal.rooms_owing_departure_reports(transaction, self._principal_id),
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

    async def visible_message(self, *, room_id: str, logical_event_id: str) -> VisibleMessage | None:
        """Return the current visible revision of one logical message."""
        return await self._backend.read(
            lambda transaction: reads.visible_message(
                transaction,
                self._principal_id,
                room_id=room_id,
                logical_event_id=logical_event_id,
            ),
        )

    async def latest_visible_event_id(self, *, room_id: str, thread_id: str) -> str | None:
        """Return the newest visible event in one thread, or nothing."""
        return await self._backend.read(
            lambda transaction: reads.latest_visible_event_id(
                transaction,
                self._principal_id,
                room_id=room_id,
                thread_id=thread_id,
            ),
        )

    async def pending_refreshes(
        self,
        *,
        room_id: str,
        thread_id: str | None,
        limit: int = _DEFAULT_REFRESH_LIMIT,
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

    async def conversation_is_complete(self, *, room_id: str, thread_id: str | None) -> bool:
        """Return whether this conversation's hydration walk reached its end."""
        return await self._backend.read(
            lambda transaction: reads.conversation_is_complete(
                transaction,
                self._principal_id,
                room_id=room_id,
                thread_id=thread_id,
            ),
        )

    async def conversation_hydration_was_truncated(self, *, room_id: str, thread_id: str | None) -> bool:
        """Return whether a walk ran for this conversation and stopped at a ceiling."""
        return await self._backend.read(
            lambda transaction: reads.conversation_hydration_was_truncated(
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
        complete: bool,
        expected_membership_epoch: int,
    ) -> bool:
        """Install a completed hydration atomically, or install nothing.

        A partially applied hydration would look complete to the next reader,
        so the events and the completion marker share one transaction.

        ``complete`` is the walk's own account of why it stopped, and it is
        recorded rather than inferred because nothing downstream could recover
        it: a conversation bounded by the prompt window and one that is simply
        that short leave identical rows behind.
        """
        return await self._backend.write(
            lambda transaction: _install_hydration(
                transaction,
                self._principal_id,
                room_id=room_id,
                thread_id=thread_id,
                events=events,
                complete=complete,
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
        settle_source_event_ids: tuple[str, ...] = (),
    ) -> str | None:
        """Record delivery intent, or refuse it as an answer to a membership that ended.

        Nothing means refused. The turn is named by the Matrix event that
        caused it, and that event's journal row records which membership
        admitted it, so this needs no epoch from its caller and cannot be
        given a stale one.

        ``settle_source_event_ids`` are the journal sources this delivery
        discharges, and they are settled in the same transaction that records
        it. That is the whole handoff: ownership of the turn moves from the
        journal to the outbox at one commit, so there is no instant at which a
        crash leaves both of them owning it. Two transactions would leave one,
        and a process that died there would send the frozen answer *and*
        replay the turn -- a second model run for a question already answered.
        """
        return await self._backend.write(
            lambda transaction: _enqueue_delivery(
                transaction,
                self._principal_id,
                turn_id=turn_id,
                stage=stage,
                room_id=room_id,
                thread_id=thread_id,
                payload=payload,
                edits_event_id=edits_event_id,
                settle_source_event_ids=settle_source_event_ids,
            ),
        )

    async def turn_membership_is_current(self, *, turn_id: str, room_id: str) -> bool:
        """Return whether a turn still speaks for the room's current membership."""
        return await self._backend.read(
            lambda transaction: _turn_membership_is_current(
                transaction,
                self._principal_id,
                turn_id=turn_id,
                room_id=room_id,
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
        limit: int = _DEFAULT_UNACKNOWLEDGED_LIMIT,
        after: tuple[int, str, str] | None = None,
    ) -> tuple[OutboxDelivery, ...]:
        """Return deliveries whose Matrix outcome is unknown, oldest first."""
        return await self._backend.read(
            lambda transaction: outbox.unacknowledged(
                transaction,
                self._principal_id,
                limit=limit,
                after=after,
            ),
        )

    async def remember_approval_card(
        self,
        *,
        room_id: str,
        card_event_id: str,
        card: Mapping[str, Any],
    ) -> None:
        """Record one sent approval card as awaiting a decision."""
        await self._backend.write(
            lambda transaction: approvals.remember(
                transaction,
                self._principal_id,
                room_id=room_id,
                card_event_id=card_event_id,
                card=card,
            ),
        )

    async def resolve_approval_card(
        self,
        *,
        card_event_id: str,
        resolution: Mapping[str, Any],
    ) -> RecordedApprovalDecision:
        """Record the decision one card carries, before it is shown."""
        return await self._backend.write(
            lambda transaction: approvals.resolve(
                transaction,
                self._principal_id,
                card_event_id=card_event_id,
                resolution=resolution,
            ),
        )

    async def forget_approval_card(self, *, card_event_id: str) -> None:
        """Drop one approval card that has reached a terminal state."""
        await self._backend.write(
            lambda transaction: approvals.forget(
                transaction,
                self._principal_id,
                card_event_id=card_event_id,
            ),
        )

    async def pending_approval_card(
        self,
        *,
        room_id: str,
        card_event_id: str,
    ) -> StoredApprovalCard | None:
        """Return one card this bot still owes work on under this membership."""
        return await self._backend.read(
            lambda transaction: approvals.pending_card(
                transaction,
                self._principal_id,
                room_id=room_id,
                card_event_id=card_event_id,
            ),
        )

    async def pending_approval_cards(
        self,
        *,
        room_id: str,
        limit: int = _DEFAULT_ROOM_CARD_LIMIT,
    ) -> tuple[StoredApprovalCard, ...]:
        """Return one room's unfinished cards, oldest first."""
        return await self._backend.read(
            lambda transaction: approvals.pending_cards(
                transaction,
                self._principal_id,
                room_id=room_id,
                limit=limit,
            ),
        )


def _turn_membership_is_current(
    transaction,  # noqa: ANN001 - the backend's Transaction, kept structural
    principal_id: str,
    *,
    turn_id: str,
    room_id: str,
) -> bool:
    """Return whether the membership that admitted a turn is still the room's."""
    admitted = journal.admitted_membership_epoch(transaction, principal_id, turn_id)
    if admitted is None:
        # Nothing the journal admitted, so nothing a rejoin invalidated.
        return True
    return admitted == journal.current_membership_epoch(transaction, principal_id, room_id)


def _enqueue_delivery(
    transaction,  # noqa: ANN001 - the backend's Transaction, kept structural
    principal_id: str,
    *,
    turn_id: str,
    stage: DeliveryStage,
    room_id: str,
    thread_id: str | None,
    payload: Mapping[str, object],
    edits_event_id: str | None,
    settle_source_event_ids: tuple[str, ...],
) -> str | None:
    """Record delivery intent unless the membership that authorized it has ended.

    The fence deletes a room's unattempted deliveries because they answer a
    conversation the bot has left. This closes the other half of the same
    window: a turn that was still running when the fence committed would
    otherwise write its answer back in afterwards, and the fence has already
    been and gone. Because both are single write transactions against a
    serialized writer, the two possible orderings are "enqueued, then deleted"
    and "fenced, then refused". Neither leaves an answer behind.

    An already-attempted row is exempt, and deliberately so. Its outcome is
    unknown -- the homeserver may be holding it -- and refusing the retry
    would strand it unacknowledged forever while leaving whatever it sent
    visible. Only the frozen transaction ID can resolve that, by collapsing
    the retry onto the same event.

    Settling the sources here rather than after the commit is what makes the
    handoff one event. A refusal settles nothing, because nothing durable
    would owe the answer afterwards; anything else settles every source the
    delivery accounts for, atomically with the row that now answers them.
    """
    if not outbox.is_attempted(transaction, principal_id, turn_id=turn_id, stage=stage) and not (
        _turn_membership_is_current(transaction, principal_id, turn_id=turn_id, room_id=room_id)
    ):
        return None
    transaction_id = outbox.enqueue(
        transaction,
        principal_id,
        turn_id=turn_id,
        stage=stage,
        room_id=room_id,
        thread_id=thread_id,
        payload=payload,
        edits_event_id=edits_event_id,
    )
    journal.settle_many(transaction, principal_id, settle_source_event_ids, SettlementOutcome.SUCCEEDED)
    return transaction_id


def _install_hydration(
    transaction,  # noqa: ANN001 - the backend's Transaction, kept structural
    principal_id: str,
    *,
    room_id: str,
    thread_id: str | None,
    events: tuple[ProjectedEvent, ...],
    complete: bool,
    expected_membership_epoch: int,
) -> bool:
    from .projection import project  # noqa: PLC0415 - keeps the module import-light

    if not reads.mark_conversation_hydrated(
        transaction,
        principal_id,
        room_id=room_id,
        thread_id=thread_id,
        complete=complete,
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
    def open_sqlite(cls, database_path: Path) -> EventJournalStore:
        """Open a single-writer SQLite store."""
        from .sqlite_backend import SqliteBackend  # noqa: PLC0415 - backend chosen at runtime

        return cls(backend=SqliteBackend.open(database_path))

    @classmethod
    def open_postgres(cls, database_url: str) -> EventJournalStore:
        """Open a PostgreSQL store."""
        from .postgres_backend import PostgresBackend  # noqa: PLC0415 - keeps psycopg optional

        return cls(backend=PostgresBackend.open(database_url))

    def principal(self, principal_id: str) -> PrincipalStore:
        """Return the bound view for one bot."""
        if not principal_id:
            msg = "An event-journal principal requires an identity"
            raise ValueError(msg)
        return PrincipalStore(_backend=self.backend, _principal_id=principal_id)

    async def generation(self, *, new_generation: str) -> str:
        """Return this database's identity, minting it the first time it is opened.

        Shared across principals rather than per-principal: the thing being
        identified is the database, and every principal in it lost the same
        history if it were replaced.
        """
        return await self.backend.write(
            lambda transaction: journal.store_generation(transaction, new_generation=new_generation),
        )

    async def close(self) -> None:
        """Release every connection the backend owns."""
        await self.backend.close()
