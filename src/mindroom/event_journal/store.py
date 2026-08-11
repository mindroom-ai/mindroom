"""The principal-bound store view runtime code is given.

One database may hold many bots, but no runtime caller ever sees the column
that separates them. Operational methods take no ``principal_id`` at all, so
reading or settling another bot's rows is not something a caller can express,
rather than something it is trusted not to do.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import batched
from typing import TYPE_CHECKING, Any

from mindroom.history_recovery import (
    HistoryRecoveryOutcome,
    RoomHistoryRecovery,
)

from . import approvals, journal, outbox, reads, turn_records
from .approvals import (  # noqa: TC001 - part of this module's runtime return types
    RecordedApprovalDecision,
    StoredApprovalCard,
)
from .models import DeliveryAcknowledgement
from .projection import drop_refetched_message, install_refetched_revision, project

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from .backend import Backend, Transaction
    from .models import (
        AdmissionResult,
        ConversationCursor,
        ConversationPage,
        DeliveryStage,
        DepartureOutcome,
        DepartureSource,
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

_DEFAULT_PENDING_LIMIT = 256
_DEFAULT_UNACKNOWLEDGED_LIMIT = 256
_DEFAULT_ROOM_CARD_LIMIT = 256
# A strict export can retain one million messages from as many as two million
# fetched events. Keeping each write to 256 projected events puts a hard ceiling
# on the work done while SQLite holds its global writer, independently of the
# export ceiling or the result's actual size.
_HYDRATION_INSTALL_CHUNK_SIZE = 256


@dataclass(frozen=True, slots=True)
class PrincipalStore:
    """Everything one bot may durably do, scoped to that bot."""

    _backend: Backend
    _principal_id: str

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
    ) -> PendingPage:
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

    async def is_pending(self, event_id: str) -> bool:
        """Return whether one event still owes semantic work."""
        return await self._backend.read(
            lambda transaction: journal.is_pending(transaction, self._principal_id, event_id),
        )

    async def settle(self, event_id: str) -> None:
        """Mark one event's semantic work terminal."""
        await self._backend.write(
            lambda transaction: journal.settle(transaction, self._principal_id, event_id),
        )

    async def settle_many(self, event_ids: tuple[str, ...]) -> None:
        """Settle every event that one terminal turn accounted for."""
        if not event_ids:
            return
        await self._backend.write(
            lambda transaction: journal.settle_many(transaction, self._principal_id, event_ids),
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
        after_receipt_order: int | None = None,
    ) -> PendingPage:
        """Return pending events of one kind, in receipt order."""
        return await self._backend.read(
            lambda transaction: journal.pending_of_kind(
                transaction,
                self._principal_id,
                kind,
                limit=limit,
                after_receipt_order=after_receipt_order,
            ),
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

    async def conversation_hydration_coverage(
        self,
        *,
        room_id: str,
        thread_id: str | None,
    ) -> HydrationCoverage | None:
        """Return what walks under this membership proved here, or nothing if none did."""
        return await self._backend.read(
            lambda transaction: reads.conversation_hydration_coverage(
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
        attempted_policy_rank: int = 0,
        expected_membership_epoch: int,
    ) -> bool:
        """Install a hydration in bounded writes, then publish it once complete.

        Projection chunks are idempotent and may remain after interruption, but
        no strict reader trusts them until the final marker commits. Every
        chunk and the marker claim the expected membership epoch separately,
        so a fence between commits deletes earlier rows and refuses later ones.

        ``complete`` is the walk's own account of why it stopped, and it is
        recorded rather than inferred because nothing downstream could recover
        it: a conversation bounded by the prompt window and one that is simply
        that short leave identical rows behind.

        ``attempted_policy_rank`` names the `HydrationPolicy` that walk ran
        under, and it is what lets a later caller tell a conversation nobody
        walked deeply from one where its own policy has already been spent and
        failed. It defaults to zero -- no walk of any policy on record -- which
        is the literal truth for a caller installing events it did not walk
        for, and the direction that costs a redundant walk rather than a short
        answer.
        """
        if not await _install_hydration_chunks(
            self._backend,
            self._principal_id,
            room_id=room_id,
            events=events,
            expected_membership_epoch=expected_membership_epoch,
        ):
            return False
        return await self._backend.write(
            lambda transaction: reads.mark_conversation_hydrated(
                transaction,
                self._principal_id,
                room_id=room_id,
                thread_id=thread_id,
                complete=complete,
                attempted_policy_rank=attempted_policy_rank,
                expected_membership_epoch=expected_membership_epoch,
            ),
        )

    async def record_room_history_recovery(
        self,
        room_id: str,
    ) -> RoomHistoryRecovery | None:
        """Write down that a room's history could not be proven continuous."""
        return await self._backend.write(
            lambda transaction: journal.record_room_history_recovery(
                transaction,
                self._principal_id,
                room_id,
            ),
        )

    async def room_history_recovery(self, room_id: str) -> RoomHistoryRecovery | None:
        """Return one room's current history-recovery obligation, if any."""
        return await self._backend.read(
            lambda transaction: journal.room_history_recovery(transaction, self._principal_id, room_id),
        )

    async def settle_room_history_recovery(
        self,
        recovery: RoomHistoryRecovery,
        *,
        events: tuple[ProjectedEvent, ...],
        exhausted_server: bool,
        attempted_policy_rank: int,
        expected_membership_epoch: int,
    ) -> HistoryRecoveryOutcome:
        """Install a recovery in bounded writes, then publish and settle once."""
        if not await _install_hydration_chunks(
            self._backend,
            self._principal_id,
            room_id=recovery.room_id,
            events=events,
            expected_membership_epoch=expected_membership_epoch,
        ):
            return HistoryRecoveryOutcome.SUPERSEDED
        return await self._backend.write(
            lambda transaction: _settle_history_recovery(
                transaction,
                self._principal_id,
                recovery,
                exhausted_server=exhausted_server,
                attempted_policy_rank=attempted_policy_rank,
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
        """Freeze one delivery before network I/O and return the row as it stood."""
        return await self._backend.write(
            lambda transaction: outbox.claim(
                transaction,
                self._principal_id,
                turn_id=turn_id,
                stage=stage,
            ),
        )

    async def record_sending_device(
        self,
        *,
        turn_id: str,
        stage: DeliveryStage,
        device_id: str | None,
    ) -> None:
        """Record the device namespace this delivery is about to send under."""
        await self._backend.write(
            lambda transaction: outbox.record_sending_device(
                transaction,
                self._principal_id,
                turn_id=turn_id,
                stage=stage,
                device_id=device_id,
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
        terminal_turn: TerminalTurnWrite | None = None,
    ) -> DeliveryAcknowledgement:
        """Record the Matrix event one claimed delivery produced, if nothing else has.

        Returns the event the row now names -- this call's if it bound the row,
        and the winner's if it did not. A loser must not go on reporting its own
        send: everything downstream records what delivery returns, so that is
        exactly how the outbox and the terminal record come to name different
        events.

        Ownership rides back beside it rather than being inferred from it.
        Matrix deduplicates a resent transaction ID by handing both callers the
        same event, so a loser's settled event can equal the one it just sent
        while it bound nothing at all -- and only the conditional update below
        knows the difference.

        ``terminal_turn`` is the turn record this acknowledgement completes,
        written in the same transaction. The acknowledgement is the durable
        proof that a visible answer exists and what its event ID is, and that
        is precisely the fact the record is missing until it is told. Two
        commits leave a window in which the answer is delivered and the record
        does not know it, and an edit of that message arriving in that state is
        dropped with nothing to edit -- permanently, because nothing
        re-delivers a consumed edit.

        The two rows are scoped differently, one to this principal and one to
        an agent, and they still share a transaction because they share a
        database. That is the whole reason turn records were moved here.
        """

        def acknowledge(transaction: Transaction) -> DeliveryAcknowledgement:
            bound = outbox.acknowledge(
                transaction,
                self._principal_id,
                turn_id=turn_id,
                stage=stage,
                event_id=event_id,
            )
            # A caller that lost the acknowledgement must not write the record
            # either. The row already names another event, and a terminal
            # record pointing somewhere else is the disagreement this whole
            # transaction exists to prevent.
            if bound and terminal_turn is not None:
                turn_records.upsert(
                    transaction,
                    terminal_turn.agent_name,
                    index_event_ids=terminal_turn.index_event_ids,
                    anchor_event_id=terminal_turn.anchor_event_id,
                    record_json=terminal_turn.record_json,
                )
            if bound:
                return DeliveryAcknowledgement(settled_event_id=event_id, bound=True)
            # Lost the row. Whatever is on it now is the answer this delivery
            # resolves to, and the caller has to be told that rather than its
            # own event id -- everything downstream records what `flush`
            # returns, so a loser reporting its own send is how the outbox and
            # the turn record end up naming different events.
            settled = transaction.fetchone(
                """
                SELECT acknowledged_event_id FROM response_outbox
                WHERE principal_id = ? AND turn_id = ? AND stage = ?
                """,
                (self._principal_id, turn_id, stage.value),
            )
            return DeliveryAcknowledgement(
                settled_event_id=None if settled is None else str(settled["acknowledged_event_id"]),
                bound=False,
            )

        return await self._backend.write(acknowledge)

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

    async def claim_approval_card(
        self,
        *,
        room_id: str,
        transaction_id: str,
        card: Mapping[str, Any],
    ) -> None:
        """Record one approval card as awaiting a decision, before it is sent."""
        await self._backend.write(
            lambda transaction: approvals.claim(
                transaction,
                self._principal_id,
                room_id=room_id,
                transaction_id=transaction_id,
                card=card,
            ),
        )

    async def mark_approval_card_attempted(
        self,
        *,
        transaction_id: str,
        sending_device_id: str | None,
    ) -> bool:
        """Record that one claimed approval card is about to be offered to Matrix."""
        return await self._backend.write(
            lambda transaction: approvals.mark_attempted(
                transaction,
                self._principal_id,
                transaction_id=transaction_id,
                sending_device_id=sending_device_id,
            ),
        )

    async def acknowledge_approval_card(
        self,
        *,
        transaction_id: str,
        card_event_id: str,
        card: Mapping[str, Any],
    ) -> None:
        """Record the Matrix event one claimed approval card became."""
        await self._backend.write(
            lambda transaction: approvals.acknowledge(
                transaction,
                self._principal_id,
                transaction_id=transaction_id,
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

    async def forget_approval_card(self, *, transaction_id: str) -> None:
        """Drop one approval card that has reached a terminal state."""
        await self._backend.write(
            lambda transaction: approvals.forget(
                transaction,
                self._principal_id,
                transaction_id=transaction_id,
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
        after: tuple[int, str] | None = None,
    ) -> tuple[StoredApprovalCard, ...]:
        """Return one room's unfinished cards, oldest first."""
        return await self._backend.read(
            lambda transaction: approvals.pending_cards(
                transaction,
                self._principal_id,
                room_id=room_id,
                limit=limit,
                after=after,
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
    journal.settle_many(transaction, principal_id, settle_source_event_ids)
    return transaction_id


def _settle_history_recovery(
    transaction,  # noqa: ANN001 - the backend's Transaction, kept structural
    principal_id: str,
    recovery: RoomHistoryRecovery,
    *,
    exhausted_server: bool,
    attempted_policy_rank: int,
    expected_membership_epoch: int,
) -> HistoryRecoveryOutcome:
    """Publish a recovery and settle its exact obligation in one transaction."""
    if not reads.claim_membership_epoch(
        transaction,
        principal_id,
        room_id=recovery.room_id,
        expected_membership_epoch=expected_membership_epoch,
    ):
        return HistoryRecoveryOutcome.SUPERSEDED
    if not journal.claim_room_history_recovery(transaction, principal_id, recovery):
        return HistoryRecoveryOutcome.SUPERSEDED
    reads.publish_conversation_hydration(
        transaction,
        principal_id,
        room_id=recovery.room_id,
        thread_id=None,
        complete=exhausted_server,
        attempted_policy_rank=attempted_policy_rank,
        membership_epoch=expected_membership_epoch,
    )
    return journal.settle_room_history_recovery(
        transaction,
        principal_id,
        recovery,
        exhausted_server=exhausted_server,
    )


async def _install_hydration_chunks(
    backend: Backend,
    principal_id: str,
    *,
    room_id: str,
    events: tuple[ProjectedEvent, ...],
    expected_membership_epoch: int,
) -> bool:
    """Project a walk in hard-bounded, independently fenced transactions."""
    for chunk in batched(events, _HYDRATION_INSTALL_CHUNK_SIZE):
        installed = await backend.write(
            lambda transaction, chunk=chunk: _install_hydration_chunk(
                transaction,
                principal_id,
                room_id=room_id,
                events=chunk,
                expected_membership_epoch=expected_membership_epoch,
            ),
        )
        if not installed:
            return False
    return True


def _install_hydration_chunk(
    transaction,  # noqa: ANN001 - the backend's Transaction, kept structural
    principal_id: str,
    *,
    room_id: str,
    events: tuple[ProjectedEvent, ...],
    expected_membership_epoch: int,
) -> bool:
    """Project one chunk only while its membership epoch is current."""
    if not reads.claim_membership_epoch(
        transaction,
        principal_id,
        room_id=room_id,
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

    async def existing_generation(self) -> str | None:
        """Return this database's identity without minting one, or ``None`` if unused.

        The distinction matters to the caller that decides whether this install
        may use the database at all: a database with no generation has never
        been opened by anything, which is a different operator problem from one
        carrying another install's generation. Minting on the way past would
        erase that difference.
        """
        return await self.backend.read(journal.read_generation)

    def turn_records(self, agent_name: str) -> TurnRecordStore:
        """Return the agent-scoped turn-record view.

        Not a ``PrincipalStore`` method, because turn records are not scoped to
        a Matrix identity. They record that a message was answered, which stays
        true across a re-login; scoping them per principal would make a bot
        that logs in as a new device answer everything a second time.
        """
        if not agent_name:
            msg = "Turn records require an agent name"
            raise ValueError(msg)
        return TurnRecordStore(_backend=self.backend, _agent_name=agent_name)

    async def close(self) -> None:
        """Release every connection the backend owns."""
        await self.backend.close()


@dataclass(frozen=True, slots=True)
class TurnRecordStore:
    """One agent's durable turn records, in the journal's own database.

    Deliberately separate from ``PrincipalStore``. The point of moving these
    rows here is that the acknowledgement binding a delivery's Matrix event and
    the terminal record naming that event commit together, and that needs one
    database -- not one scope key.

    Not one transaction spanning the whole answer, which is the easy thing to
    read into this and is wrong. Sources settle when the ``FINAL`` row is
    enqueued; the record lands when that row is acknowledged, a send later. One
    database buys an unbroken chain of ownership across those two writes, not a
    single write -- and a crash between them is a state the outbox is designed
    to own, not one a reconciler has to repair.

    Keeping the two views apart keeps a reader from assuming a turn record
    belongs to the principal it happened to be fetched beside.
    """

    _backend: Backend
    _agent_name: str

    @property
    def state_key(self) -> str:
        """Identify the physical database these records live in.

        Callers that cache records in memory need to know when two views are
        the same store and when they are different ones. Two stores over
        different databases must not share a cache, or the second answers from
        rows it does not have.

        The backend object is the identity. One backend owns one database and
        every view of that database is handed the same instance, so this is
        stable where it must be and distinct where it must be, without a
        connection string that would put a password in a cache key.
        """
        return f"{id(self._backend):x}"

    async def upsert(
        self,
        *,
        index_event_ids: Sequence[str],
        anchor_event_id: str,
        record_json: str,
    ) -> None:
        """Store one record under every event that indexes it."""
        await self._backend.write(
            lambda transaction: turn_records.upsert(
                transaction,
                self._agent_name,
                index_event_ids=index_event_ids,
                anchor_event_id=anchor_event_id,
                record_json=record_json,
            ),
        )

    async def adopt_missing(
        self,
        *,
        index_event_ids: Sequence[str],
        anchor_event_id: str,
        record_json: str,
    ) -> int:
        """Fill only the indexes with no record yet, for migration. Returns how many."""
        return await self._backend.write(
            lambda transaction: turn_records.adopt_missing(
                transaction,
                self._agent_name,
                index_event_ids=index_event_ids,
                anchor_event_id=anchor_event_id,
                record_json=record_json,
            ),
        )

    async def load_all(self) -> tuple[tuple[str, str, str], ...]:
        """Return every record this agent holds, for a warm-up."""
        return await self._backend.read(
            lambda transaction: turn_records.load_all(transaction, self._agent_name),
        )

    async def forget(self, *, index_event_ids: Sequence[str]) -> None:
        """Drop records indexed by these events, as compaction does."""
        await self._backend.write(
            lambda transaction: turn_records.forget(
                transaction,
                self._agent_name,
                index_event_ids=index_event_ids,
            ),
        )
