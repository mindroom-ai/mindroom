"""The narrow slices of a principal's store that collaborators actually use.

`PrincipalStore` is one object whose methods cover admission, replay,
membership, conversation reads, hydration, refetch, and delivery. That
is the shape of the universal cache dependency this design exists to remove:
once every collaborator holds the whole surface, any of them can reach for any
part of it, and the boundaries stop being real.

These views are what each collaborator is actually allowed to do. Each
operation is declared once, on the one view that owns it, and `PrincipalStore`
inherits every view, so the bot hands each collaborator the same object under
a narrower name. The enforcement is the type checker refusing a call the
annotation does not permit. The point is not to hide the store; it is that a
hydrator reaching into the outbox should fail review by failing to type-check.
"""

from __future__ import annotations

from itertools import batched
from typing import TYPE_CHECKING

from . import interactive_questions, journal, matrix_delivery_rows, membership_hooks, outbox, projection, reads
from .approval_store import ApprovalStore
from .matrix_delivery_rows import ReplyRowRefusedError
from .principal_rows import PrincipalRows
from .replies import ReplyStore

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping

    from mindroom.event_journal.history_recovery import (
        HistoryRecoveryOutcome,
        RoomHistoryRecovery,
    )

    from .approval_signals import ApprovalSignals
    from .interactive_questions import InteractiveSelection
    from .models import (
        AdmissionFacts,
        ConversationCursor,
        ConversationPage,
        DeliveryAcknowledgement,
        DeliveryStage,
        FinalAnswerEnqueue,
        HydrationCoverage,
        IngestionBatchAdmission,
        JournalEvent,
        MatrixDelivery,
        PendingPage,
        PermanentDeliveryFailure,
        RefreshRequest,
        SemanticConsumer,
        UnreadableMatrixDelivery,
    )
    from .projection import ProjectedEvent
    from .replies import PreparedReplyRow, ReplyRowEnqueue, ReplyRowRequest

_DEFAULT_PENDING_LIMIT = 256
_DEFAULT_UNACKNOWLEDGED_LIMIT = 256
# A strict export can retain one million messages from as many as two million
# fetched events. Keeping each write to 256 projected events puts a hard ceiling
# on the work done while SQLite holds its global writer, independently of the
# export ceiling or the result's actual size.
_HYDRATION_INSTALL_CHUNK_SIZE = 256


class IngestionBatchAdmissionView(PrincipalRows):
    """Admitting one authenticated nio batch, and nothing else."""

    __slots__ = ()

    async def admit_ingestion_batch(self, admission: IngestionBatchAdmission) -> AdmissionFacts:
        """Atomically persist one trusted nio batch."""
        return await self._write(journal.admit_ingestion_batch, admission)


class ReplayView(PrincipalRows):
    """Draining and settling the work the journal still owes."""

    __slots__ = ()

    async def pending(
        self,
        *,
        limit: int = _DEFAULT_PENDING_LIMIT,
        room_id: str | None = None,
        after_receipt_order: int | None = None,
        runtime_generation: str = "unmanaged",
    ) -> PendingPage:
        """Return actionable events awaiting semantic work, in receipt order."""
        return await self._read(
            journal.pending,
            limit=limit,
            room_id=room_id,
            after_receipt_order=after_receipt_order,
            runtime_generation=runtime_generation,
        )

    async def is_pending(self, event_id: str) -> bool:
        """Return whether one event still owes semantic work."""
        return await self._read(journal.is_pending, event_id)

    async def settle(self, event_id: str) -> tuple[str, ...]:
        """Mark one event's semantic work terminal; return the replies that ends, as ``settle_many`` does."""
        return await self._write(journal.settle_turn_sources, (event_id,))


class DispatchView(ReplayView):
    """Everything the dispatcher coordinates: replay and semantic claims."""

    __slots__ = ()

    async def is_room_member_join_suppressed(self, room_id: str, event_id: str, user_id: str) -> bool:
        """Check one admitted join against earlier baselines and completed hook delivery."""
        return await self._read(membership_hooks.is_suppressed, room_id, event_id, user_id)

    async def mark_room_member_join_completed(self, room_id: str, user_id: str) -> None:
        """Record successful hook delivery without rewriting any other member's marker."""
        await self._write(membership_hooks.mark_completed, room_id, user_id)

    async def settle_many(self, event_ids: tuple[str, ...]) -> tuple[str, ...]:
        """Settle every event that one terminal turn accounted for; return the replies that ends.

        A reply an earlier span left waiting to replay these sources ends in
        the same commit, since nothing replays them any more; what it owes
        Matrix is its caller's to deliver.
        """
        if not event_ids:
            return ()
        return await self._write(journal.settle_turn_sources, event_ids)

    async def unsettled_event_ids(self) -> frozenset[str]:
        """Return every event that still owes semantic work."""
        return await self._read(journal.unsettled_event_ids)

    async def load_event(self, event_id: str) -> JournalEvent | None:
        """Return one admitted event."""
        return await self._read(journal.load, event_id)

    async def claim_semantic_consumer(
        self,
        event_id: str,
        consumer: SemanticConsumer,
    ) -> SemanticConsumer | None:
        """Record the sole consumer, or retire a stale interactive reaction."""
        return await self._write(journal.claim_semantic_consumer, event_id, consumer)

    async def claim_interactive_reaction(
        self,
        *,
        source_event_id: str,
    ) -> InteractiveSelection | None:
        """Atomically transfer one question selection to its reaction source."""
        return await self._write(interactive_questions.claim_reaction, source_event_id=source_event_id)


class PendingTurnView(PrincipalRows):
    """Asking what unfinished work one conversation is already holding.

    Deliberately not part of ``ReplayView``. The caller is a guard deciding
    whether an older turn is still worth running, and it must be able to see
    the pending set without being able to settle any of it.
    """

    __slots__ = ()

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
        return await self._read(
            journal.pending_thread_events_after,
            room_id=room_id,
            thread_id=thread_id,
            after_origin_server_ts=after_origin_server_ts,
            excluding_event_id=excluding_event_id,
            limit=limit,
        )

    async def pending_among(self, event_ids: Collection[str]) -> frozenset[str]:
        """Return which of these events still owe semantic work."""
        if not event_ids:
            return frozenset()
        return await self._read(journal.pending_among, tuple(event_ids))


class RelationView(PrincipalRows):
    """Asking what the journal already knows about one event's place in a thread."""

    __slots__ = ()

    async def admitted_thread_id(self, *, room_id: str, event_id: str) -> tuple[bool, str | None]:
        """Return whether one event was admitted, and which thread it belongs to."""
        return await self._read(journal.admitted_thread_id, room_id=room_id, event_id=event_id)


class _MembershipEpochView(PrincipalRows):
    """The room membership epoch both hydration and delivery fence their writes on."""

    __slots__ = ()

    async def membership_epoch(self, room_id: str) -> int:
        """Return the current membership epoch for one room."""
        return await self._read(journal.current_membership_epoch, room_id)


class _HydrationMarkerView(PrincipalRows):
    """The hydration marker both reading and hydrating a conversation consult."""

    __slots__ = ()

    async def conversation_is_hydrated(self, *, room_id: str, thread_id: str | None) -> bool:
        """Return whether this conversation was hydrated under current membership."""
        return await self._read(reads.conversation_is_hydrated, room_id=room_id, thread_id=thread_id)


class ConversationReadView(_HydrationMarkerView):
    """Reading a conversation, plus the evidence needed to judge completeness.

    Nothing here can change a conversation, which is the point: a reader that
    could write one is a reader that can be made to.
    """

    __slots__ = ()

    async def read_conversation(
        self,
        *,
        room_id: str,
        thread_id: str | None,
        limit: int,
        before: ConversationCursor | None = None,
    ) -> ConversationPage:
        """Return one bounded page of a conversation."""
        return await self._read(
            reads.read_conversation,
            room_id=room_id,
            thread_id=thread_id,
            limit=limit,
            before=before,
        )

    async def latest_visible_event_id(self, *, room_id: str, thread_id: str) -> str | None:
        """Return the newest visible event in one thread, or nothing."""
        return await self._read(reads.latest_visible_event_id, room_id=room_id, thread_id=thread_id)

    async def conversation_hydration_was_truncated(self, *, room_id: str, thread_id: str | None) -> bool:
        """Return whether a walk ran for this conversation and stopped at a ceiling."""
        return await self._read(reads.conversation_hydration_was_truncated, room_id=room_id, thread_id=thread_id)


class HydrationView(_MembershipEpochView, _HydrationMarkerView):
    """Building a conversation from the server and repairing one message of it."""

    __slots__ = ()

    async def room_history_recovery(self, room_id: str) -> RoomHistoryRecovery | None:
        """Return one room's current history-recovery obligation, if any."""
        return await self._read(journal.room_history_recovery, room_id)

    async def install_room_history_recovery_chunk(
        self,
        recovery: RoomHistoryRecovery,
        *,
        events: tuple[ProjectedEvent, ...],
        expected_membership_epoch: int,
    ) -> bool:
        """Project one bounded recovery chunk only while both fences match."""
        if len(events) > _HYDRATION_INSTALL_CHUNK_SIZE:
            msg = f"Room history recovery chunks may contain at most {_HYDRATION_INSTALL_CHUNK_SIZE} projected events"
            raise ValueError(msg)
        return await self._write(
            journal.install_room_history_recovery_chunk,
            recovery,
            events=events,
            expected_membership_epoch=expected_membership_epoch,
        )

    async def settle_room_history_recovery(
        self,
        recovery: RoomHistoryRecovery,
        *,
        exhausted_server: bool,
        unreadable: bool,
        attempted_policy_rank: int,
        expected_membership_epoch: int,
    ) -> HistoryRecoveryOutcome:
        """Publish an installed recovery and settle its exact obligation once.

        ``exhausted_server`` alone decides the obligation, because a walk that
        reached the start of the room fetched everything the gap skipped.
        ``unreadable`` says some of it could not be read, which no marker in
        the room can vouch for, so a repaired settlement records the room
        conversation as incomplete and revokes every thread's marker for that
        thread's own walk to settle again.
        """
        return await self._write(
            journal.settle_room_history_recovery,
            recovery,
            exhausted_server=exhausted_server,
            unreadable=unreadable,
            attempted_policy_rank=attempted_policy_rank,
            expected_membership_epoch=expected_membership_epoch,
        )

    async def conversation_hydration_coverage(
        self,
        *,
        room_id: str,
        thread_id: str | None,
    ) -> HydrationCoverage | None:
        """Return what walks under this membership proved here, or nothing if none did."""
        return await self._read(reads.conversation_hydration_coverage, room_id=room_id, thread_id=thread_id)

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
        for chunk in batched(events, _HYDRATION_INSTALL_CHUNK_SIZE, strict=False):
            if not await self._write(
                journal.install_hydration_chunk,
                room_id=room_id,
                events=chunk,
                expected_membership_epoch=expected_membership_epoch,
            ):
                return False
        return await self._write(
            reads.mark_conversation_hydrated,
            room_id=room_id,
            thread_id=thread_id,
            complete=complete,
            attempted_policy_rank=attempted_policy_rank,
            expected_membership_epoch=expected_membership_epoch,
        )

    async def install_refetched_revision(
        self,
        request: RefreshRequest,
        *,
        revision_event_id: str,
        revision_ts: int,
        revision_sender: str,
        revision_transaction_id: str | None = None,
        content: Mapping[str, object],
    ) -> int | None:
        """Install a point-refetched revision if its refresh token still holds, returning its stored size."""
        return await self._write(
            projection.install_refetched_revision,
            room_id=request.room_id,
            logical_event_id=request.logical_event_id,
            revision_event_id=revision_event_id,
            revision_ts=revision_ts,
            revision_sender=revision_sender,
            revision_transaction_id=revision_transaction_id,
            content=content,
            expected_revision_event_id=request.revision_event_id,
            expected_refresh_token=request.refresh_token,
            expected_membership_epoch=request.membership_epoch,
        )

    async def drop_refetched_message(self, request: RefreshRequest) -> bool:
        """Remove a logical message the server has no remaining revision of."""
        return await self._write(
            projection.drop_refetched_message,
            room_id=request.room_id,
            logical_event_id=request.logical_event_id,
            expected_revision_event_id=request.revision_event_id,
            expected_refresh_token=request.refresh_token,
            expected_membership_epoch=request.membership_epoch,
        )


class ExportProjectionView(ConversationReadView, HydrationView):
    """Everything export reads from one principal's projection, and nothing else.

    Export is the one caller that needs all three slices at once -- it reads
    pages, it triggers the hydration behind the first of them, and it asks
    whether that hydration finished -- so this is where the union is written
    down. Naming it keeps the factory from taking the whole ``PrincipalStore``
    for want of a type: an export that reached for ``enqueue_matrix_delivery`` or
    ``admit`` would fail the type checker rather than typecheck and be caught,
    if at all, in review.
    """

    __slots__ = ()

    async def conversation_is_complete(self, *, room_id: str, thread_id: str | None) -> bool:
        """Return whether this conversation's hydration walk reached its end."""
        return await self._read(reads.conversation_is_complete, room_id=room_id, thread_id=thread_id)


class MatrixDeliveryView(_MembershipEpochView):
    """Delivering what was generated, plus the one journal fact delivery owns.

    That exception is the handoff, and it is narrower than it looks: the only
    thing this view may say about the journal is "these sources are answered
    now", and it may only say it in the same breath as recording the answer.
    Nothing here can read the journal, replay it, or settle a source that no
    delivery accounts for.
    """

    __slots__ = ()

    @property
    def replies(self) -> ReplyStore:
        """Return this principal's reply records."""
        return ReplyStore(_backend=self._backend, _principal_id=self._principal_id)

    async def enqueue_matrix_delivery(
        self,
        *,
        delivery_id: str,
        stage: DeliveryStage,
        room_id: str,
        thread_id: str | None,
        payload: Mapping[str, object],
        result: Mapping[str, object] | None = None,
        event_type: str = "m.room.message",
        edits_event_id: str | None = None,
        permanent_failure_reason: str | None = None,
    ) -> str | None:
        """Record delivery intent, or refuse it as an answer to a membership that ended.

        Nothing means refused. The turn is named by the Matrix event that
        caused it, and that event's journal row records which membership
        admitted it, so this needs no epoch from its caller and cannot be
        given a stale one.
        """
        return await self._write(
            matrix_delivery_rows.enqueue_delivery,
            delivery_id=delivery_id,
            stage=stage,
            event_type=event_type,
            room_id=room_id,
            thread_id=thread_id,
            payload=payload,
            result=result,
            edits_event_id=edits_event_id,
            permanent_failure_reason=permanent_failure_reason,
        )

    async def enqueue_final_answer(
        self,
        *,
        delivery_id: str,
        room_id: str,
        thread_id: str | None,
        payload: Mapping[str, object],
        turn_key: str | None,
        result: Mapping[str, object] | None = None,
        event_type: str = "m.room.message",
        edits_event_id: str | None = None,
        permanent_failure_reason: str | None = None,
    ) -> FinalAnswerEnqueue | None:
        """Record a non-AI FINAL answer and hand its turn from the journal to the outbox, in one transaction.

        The turn the producer recorded under ``turn_key`` is bound to this
        delivery, and its sources and aliases settle with the row, so there is
        no instant at which a crash leaves both the journal and the outbox
        owning it: one would send the frozen answer *and* replay the turn. A
        turn that is not the one the producer recorded binds nothing, and only
        the delivery's own source settles. Nothing means refused.
        """
        return await self._write(
            matrix_delivery_rows.enqueue_final_answer,
            delivery_id=delivery_id,
            event_type=event_type,
            room_id=room_id,
            thread_id=thread_id,
            payload=payload,
            result=result,
            edits_event_id=edits_event_id,
            permanent_failure_reason=permanent_failure_reason,
            turn_key=turn_key,
        )

    async def enqueue_reply_row(self, request: ReplyRowRequest, prepared: PreparedReplyRow) -> ReplyRowEnqueue | None:
        """Decide and record one durable write of an agent or team reply.

        ``None`` means the outbox refused the row (the membership that owns
        the reply has ended), and nothing the rule decided was written.
        """
        try:
            return await self._write(matrix_delivery_rows.enqueue_reply_row, request, prepared)
        except ReplyRowRefusedError:
            return None

    async def unresolved_reply_rows(
        self,
        reply_id: str,
        *,
        before_sequence: int | None = None,
    ) -> tuple[tuple[str, DeliveryStage], ...]:
        """Return a reply's rows whose Matrix outcome is unknown, in write order."""
        return await self._read(outbox.unresolved_reply_rows, reply_id, before_sequence=before_sequence)

    async def turn_membership_is_current(self, *, turn_id: str, room_id: str) -> bool:
        """Return whether a turn still speaks for the room's current membership."""
        return await self._read(matrix_delivery_rows.turn_membership_is_current, turn_id=turn_id, room_id=room_id)

    async def claim_matrix_delivery(
        self,
        *,
        delivery_id: str,
        stage: DeliveryStage,
        sending_device_id: str | None = None,
    ) -> MatrixDelivery | None:
        """Freeze one delivery before network I/O and return the row as it stood."""
        return await self._write(
            outbox.claim,
            delivery_id=delivery_id,
            stage=stage,
            sending_device_id=sending_device_id,
        )

    async def record_matrix_delivery_device(
        self,
        *,
        delivery_id: str,
        stage: DeliveryStage,
        device_id: str | None,
    ) -> None:
        """Record the device namespace this delivery is about to send under."""
        await self._write(
            outbox.record_matrix_delivery_device,
            delivery_id=delivery_id,
            stage=stage,
            device_id=device_id,
        )

    async def load_matrix_delivery(self, *, delivery_id: str, stage: DeliveryStage) -> MatrixDelivery | None:
        """Return one delivery without claiming it."""
        return await self._read(outbox.load, delivery_id=delivery_id, stage=stage)

    async def record_permanent_matrix_delivery_failure(
        self,
        *,
        delivery_id: str,
        stage: DeliveryStage,
        reason: str,
    ) -> PermanentDeliveryFailure:
        """Stop retrying one definitively refused immutable payload, or report its ACK.

        Reply rows this refusal fails, including rows waiting on a refused
        create, apply their reply's rule in the same transaction.
        """
        return await self._write(
            matrix_delivery_rows.record_permanent_failure,
            delivery_id=delivery_id,
            stage=stage,
            reason=reason,
        )

    async def retire_matrix_delivery(
        self,
        *,
        delivery_id: str,
        stage: DeliveryStage,
        room_id: str,
        membership_epoch: int,
    ) -> str | None:
        """Retain an obsolete send as an identity tombstone, or return its ACK."""
        return await self._write(
            matrix_delivery_rows.retire_delivery,
            delivery_id=delivery_id,
            stage=stage,
            room_id=room_id,
            membership_epoch=membership_epoch,
        )

    async def acknowledge_matrix_delivery(
        self,
        *,
        delivery_id: str,
        stage: DeliveryStage,
        event_id: str,
        delivered_projections: tuple[ProjectedEvent, ...],
        terminal_response_event_id: str | None = None,
    ) -> DeliveryAcknowledgement:
        """Record the Matrix event one claimed delivery produced, if nothing else has.

        Returns the event the row now names -- this call's if it bound the row,
        and the winner's if it did not. A loser must not go on reporting its own
        send: everything downstream records what delivery returns, so that is
        exactly how the outbox and the terminal record come to name different
        events.

        Only the call that bound the row writes the terminal record and
        projections, and only it returns reply effects to run after commit.
        Matrix deduplicates a resent transaction ID by handing both callers the
        same event, so a loser's settled event can equal the one it just sent
        while it bound nothing at all.

        ``terminal_response_event_id``, when given, completes the turn this
        delivery was bound to with that response event, written in the same
        transaction. The acknowledgement is the durable
        proof that a visible answer exists and what its event ID is, and that
        is precisely the fact the record is missing until it is told. Two
        commits leave a window in which the answer is delivered and the record
        does not know it, and an edit of that message arriving in that state is
        dropped with nothing to edit -- permanently, because nothing
        re-delivers a consumed edit.

        The two rows are scoped differently, one to this principal and one to
        an agent, and they still share a transaction because they share a
        database. That is the whole reason turn records were moved here.

        ``delivered_projections`` is the server-ordered Matrix content this row
        depends on and made visible. The winner projects it in this transaction
        before any reaction or numeric answer can become durable. An empty
        tuple means the server already redacted the event, so frozen plaintext
        must not be resurrected.
        """
        return await self._write(
            matrix_delivery_rows.acknowledge_delivery,
            delivery_id=delivery_id,
            stage=stage,
            event_id=event_id,
            delivered_projections=delivered_projections,
            terminal_response_event_id=terminal_response_event_id,
        )

    async def unacknowledged_matrix_deliveries(
        self,
        *,
        event_type: str = "m.room.message",
        limit: int = _DEFAULT_UNACKNOWLEDGED_LIMIT,
        after: tuple[int, str, str] | None = None,
    ) -> tuple[MatrixDelivery | UnreadableMatrixDelivery, ...]:
        """Return deliveries whose Matrix outcome is unknown, oldest first."""
        return await self._read(outbox.unacknowledged, limit=limit, event_type=event_type, after=after)


class ApprovalDeliveryView(MatrixDeliveryView):
    """Approval-domain state plus the generic delivery operations it uses."""

    __slots__ = ()

    # Held by `PrincipalStore`, the only class constructed with this view.
    _approval_signals: ApprovalSignals

    @property
    def approvals(self) -> ApprovalStore:
        """Return this principal's approval records."""
        return ApprovalStore(
            _backend=self._backend,
            _principal_id=self._principal_id,
            _signals=self._approval_signals,
        )


__all__ = [
    "ApprovalDeliveryView",
    "ConversationReadView",
    "DispatchView",
    "ExportProjectionView",
    "HydrationView",
    "IngestionBatchAdmissionView",
    "MatrixDeliveryView",
    "PendingTurnView",
    "RelationView",
    "ReplayView",
]
