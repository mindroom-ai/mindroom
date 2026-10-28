"""The principal-bound store view runtime code is given.

One database may hold many bots, but no runtime caller ever sees the column
that separates them. Operational methods take no ``principal_id`` at all, so
reading or settling another bot's rows is not something a caller can express,
rather than something it is trusted not to do.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from itertools import batched
from typing import TYPE_CHECKING
from uuid import UUID

from mindroom import reply_lifecycle as rl
from mindroom.event_journal.history_recovery import (
    HistoryRecoveryOutcome,
    RoomHistoryRecovery,
)

from . import (
    approval_continuations,
    approval_signals,
    interactive_questions,
    journal,
    membership_hooks,
    outbox,
    reads,
    replies,
    reply_messages,
    reply_spans,
    tool_jobs,
    turn_rows,
)
from .approval_signals import ApprovalSignals
from .membership_state import claim_active_membership_epoch
from .models import (
    AdmissionResult,
    DeliveryAcknowledgement,
    DeliveryProjectionPendingError,
    DeliveryStage,
    FinalAnswerEnqueue,
    IngestionConsumer,
    IngestionConsumerBindingError,
    PermanentDeliveryFailure,
    ReplyRowFacts,
    ResponseRecoveryState,
)
from .projection import (
    discard_delivery_event,
    drop_refetched_message,
    install_refetched_revision,
    project,
)
from .replies import (
    AppliedTransition,
    PostCommitEffect,
    PreparedReplyRow,
    ReplyRowEnqueue,
    ReplyRowRequest,
    ReplyStore,
)
from .tool_jobs import SavedToolJob  # noqa: TC001 - part of this module's runtime return types

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from mindroom.interactive_models import InteractivePrompt
    from mindroom.turn_record import TurnRecord

    from .approval_continuations import ApprovalContinuation
    from .approval_store import ApprovalStore
    from .backend import Backend, Transaction
    from .interactive_questions import InteractiveSelection
    from .models import (
        AdmissionFacts,
        ConversationCursor,
        ConversationPage,
        EventKind,
        HydrationCoverage,
        InboundEvent,
        IngestionBatchAdmission,
        JournalEvent,
        MatrixDelivery,
        PendingPage,
        RefreshRequest,
        RoomMembershipPosition,
        SemanticConsumer,
        TerminalTurnWrite,
        UnreadableMatrixDelivery,
    )
    from .projection import ProjectedEvent

_DEFAULT_PENDING_LIMIT = 256
_DEFAULT_UNACKNOWLEDGED_LIMIT = 256
_DEFAULT_APPROVAL_CONTINUATION_OWNER_LIMIT = 100
# A strict export can retain one million messages from as many as two million
# fetched events. Keeping each write to 256 projected events puts a hard ceiling
# on the work done while SQLite holds its global writer, independently of the
# export ceiling or the result's actual size.
_HYDRATION_INSTALL_CHUNK_SIZE = 256


def _consumer(transaction: Transaction, principal_id: str, generation: UUID, stream_id: UUID | None) -> IngestionConsumer:  # fmt: skip
    if stream_id is None:
        transaction.execute("INSERT INTO matrix_sync_consumers (principal_id, consumer_generation, stream_id) VALUES (?, ?, NULL) ON CONFLICT (principal_id) DO NOTHING", (principal_id, str(generation)))  # fmt: skip
    else:
        transaction.execute("UPDATE matrix_sync_consumers SET stream_id = ? WHERE principal_id = ? AND consumer_generation = ? AND (stream_id IS NULL OR stream_id = ?) AND NOT EXISTS (SELECT 1 FROM matrix_sync_consumers WHERE stream_id = ?)", (str(stream_id), principal_id, str(generation), str(stream_id), str(stream_id)))  # fmt: skip
    row = transaction.fetchone("SELECT * FROM matrix_sync_consumers WHERE principal_id = ?", (principal_id,))
    if row is None:
        raise IngestionConsumerBindingError
    try:
        consumer = IngestionConsumer(UUID(str(row["consumer_generation"])), None if row["stream_id"] is None else UUID(str(row["stream_id"])))  # fmt: skip
    except (KeyError, TypeError, ValueError) as error:
        raise IngestionConsumerBindingError from error
    if stream_id is not None and consumer != IngestionConsumer(generation, stream_id):
        raise IngestionConsumerBindingError
    return consumer


def _snapshot_interactive_source(
    transaction: Transaction,
    principal_id: str,
    event: InboundEvent,
) -> None:
    """Freeze an interactive source only after its visible target is durable."""
    if interactive_questions.snapshot_source_candidate(
        transaction,
        principal_id,
        event,
    ) and outbox.has_attempted_unacknowledged_prompt_delivery(
        transaction,
        principal_id,
        room_id=event.room_id,
        membership_epoch=journal.current_membership_epoch(transaction, principal_id, event.room_id),
    ):
        msg = f"Matrix delivery projection is pending in room {event.room_id!r}"
        raise DeliveryProjectionPendingError(msg)


def _admit_ingestion_batch(
    transaction: Transaction,
    principal_id: str,
    admission: IngestionBatchAdmission,
) -> AdmissionFacts:
    """Apply one ingestion receipt with the interactive source snapshot it admits."""
    return journal.admit_ingestion_batch(
        transaction,
        principal_id,
        admission,
        snapshot=_snapshot_interactive_source,
    )


@dataclass(frozen=True, slots=True)
class PrincipalStore:
    """Everything one bot may durably do, scoped to that bot."""

    _backend: Backend
    _principal_id: str
    _approval_signals: ApprovalSignals

    async def load_or_create_ingestion_consumer(self, *, new_generation: UUID) -> IngestionConsumer:  # noqa: D102
        return await self._backend.write(lambda tx: _consumer(tx, self._principal_id, new_generation, None))

    async def bind_ingestion_stream(self, *, generation: UUID, stream_id: UUID) -> IngestionConsumer:  # noqa: D102
        try:
            return await self._backend.write(lambda tx: _consumer(tx, self._principal_id, generation, stream_id))
        except Exception as error:
            if getattr(error, "sqlite_errorname", None) != "SQLITE_CONSTRAINT_UNIQUE" and getattr(error, "sqlstate", None) != "23505":  # fmt: skip
                raise
            owner = await self._backend.read(lambda tx: tx.fetchone("SELECT principal_id FROM matrix_sync_consumers WHERE stream_id = ?", (str(stream_id),)))  # fmt: skip
            if owner is not None and owner["principal_id"] != self._principal_id:
                raise IngestionConsumerBindingError from error
            raise

    async def admit(
        self,
        event: InboundEvent,
        projected: ProjectedEvent | None = None,
    ) -> AdmissionResult:
        """Admit one event and update the projection in a single transaction."""
        return await self._backend.write(
            lambda transaction: _admit(transaction, self._principal_id, event, projected),
        )

    async def admit_ingestion_batch(
        self,
        admission: IngestionBatchAdmission,
    ) -> AdmissionFacts:
        """Atomically persist one trusted nio batch."""
        return await self._backend.write(
            lambda tx: _admit_ingestion_batch(tx, self._principal_id, admission),
        )

    async def pending(
        self,
        *,
        limit: int = _DEFAULT_PENDING_LIMIT,
        room_id: str | None = None,
        after_receipt_order: int | None = None,
        runtime_generation: str = "unmanaged",
    ) -> PendingPage:
        """Return actionable events awaiting semantic work, in receipt order."""
        return await self._backend.read(
            lambda transaction: journal.pending(
                transaction,
                self._principal_id,
                limit=limit,
                room_id=room_id,
                after_receipt_order=after_receipt_order,
                runtime_generation=runtime_generation,
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

    async def response_recovery_state(
        self,
        turn_record: TurnRecord,
        *,
        agent_name: str,
    ) -> ResponseRecoveryState:
        """Read one response's durable handoff through the reserved recovery lane."""

        def load(transaction: Transaction) -> ResponseRecoveryState:
            source_event_ids = turn_record.source_event_ids
            turn_id = turn_record.anchor_event_id
            records = {
                event_id: turn_rows.get_turn_record(transaction, agent_name, event_id)
                for event_id in turn_record.indexed_event_ids
            }
            pending = tuple(
                journal.is_pending(transaction, self._principal_id, event_id) for event_id in source_event_ids
            )
            delivery = (
                None
                if turn_id is None
                else outbox.load(
                    transaction,
                    self._principal_id,
                    delivery_id=turn_id,
                    stage=DeliveryStage.FINAL,
                )
            )
            return ResponseRecoveryState(
                pending_sources=pending,
                redacted_sources=tuple(
                    not is_pending
                    and journal.source_has_redaction_handoff(transaction, self._principal_id, event_id, turn_record)
                    for event_id, is_pending in zip(source_event_ids, pending, strict=True)
                ),
                turn_records=tuple(records.values()),
                final_delivery=delivery,
                sources_settled_by_departure=(
                    not any(pending)
                    and journal.sources_settled_by_departure(transaction, self._principal_id, source_event_ids)
                ),
                reply=reply_messages.for_sources(transaction, self._principal_id, source_event_ids),
            )

        return await self._backend.recovery_read(load)

    async def is_room_member_join_suppressed(self, room_id: str, event_id: str, user_id: str) -> bool:
        """Check one admitted join against earlier baselines and completed hook delivery."""
        return await self._backend.read(
            lambda transaction: membership_hooks.is_suppressed(
                transaction,
                self._principal_id,
                room_id,
                event_id,
                user_id,
            ),
        )

    async def mark_room_member_join_completed(self, room_id: str, user_id: str) -> None:
        """Record successful hook delivery without rewriting any other member's marker."""
        await self._backend.write(
            lambda transaction: membership_hooks.mark_completed(transaction, self._principal_id, room_id, user_id),
        )

    async def settle(self, event_id: str) -> tuple[str, ...]:
        """Mark one event's semantic work terminal; return the replies that ends, as ``settle_many`` does."""
        return await self.settle_many((event_id,))

    async def settle_many(self, event_ids: tuple[str, ...]) -> tuple[str, ...]:
        """Settle every event that one terminal turn accounted for; return the replies that ends.

        A reply an earlier span left waiting to replay these sources ends in
        the same commit, since nothing replays them any more; what it owes
        Matrix is its caller's to deliver.
        """
        if not event_ids:
            return ()
        return await self._backend.write(
            lambda transaction: _settle_turn_sources(transaction, self._principal_id, event_ids),
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
    ) -> SemanticConsumer | None:
        """Record the sole consumer, or retire a stale interactive reaction."""
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

    async def membership_position(self, room_id: str) -> RoomMembershipPosition:
        """Return the journal tenure that owns this room's events and deliveries."""
        return await self._backend.read(
            lambda transaction: journal.membership_position(
                transaction,
                self._principal_id,
                room_id,
            ),
        )

    async def ingestion_membership_position(self, room_id: str) -> RoomMembershipPosition | None:
        """Return the producer position, or None until its first membership admission."""
        return await self._backend.read(
            lambda transaction: journal.ingestion_membership_position(transaction, self._principal_id, room_id),
        )

    async def interactive_prompt_is_current(
        self,
        *,
        room_id: str,
        question_event_id: str,
        expected: InteractivePrompt,
    ) -> bool:
        """Return whether projection still exposes one prompt in its active membership."""
        return await self._backend.write(
            lambda transaction: interactive_questions.prompt_is_current(
                transaction,
                self._principal_id,
                room_id=room_id,
                question_event_id=question_event_id,
                expected=expected,
            ),
        )

    async def claim_interactive_reaction(
        self,
        *,
        source_event_id: str,
    ) -> InteractiveSelection | None:
        """Atomically transfer one question selection to its reaction source."""
        return await self._backend.write(
            lambda transaction: interactive_questions.claim_reaction(
                transaction,
                self._principal_id,
                source_event_id=source_event_id,
            ),
        )

    async def claim_interactive_text(
        self,
        *,
        source_event_id: str,
    ) -> InteractiveSelection | None:
        """Atomically transfer the oldest eligible selection to one text source."""
        return await self._backend.write(
            lambda transaction: interactive_questions.claim_text(
                transaction,
                self._principal_id,
                source_event_id=source_event_id,
            ),
        )

    async def deleted_event_ids(self, room_id: str, event_ids: tuple[str, ...]) -> frozenset[str]:
        """Return which of these events are deleted: redacted, or answering for a relayed message that was."""
        return await self._backend.read(
            lambda transaction: turn_rows.deleted_events(
                transaction,
                turn_rows.agent_of_principal(self._principal_id),
                room_id,
                event_ids,
            ),
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

    async def later_message_exists(
        self,
        *,
        room_id: str,
        thread_id: str | None,
        source_event_ids: tuple[str, ...],
        excluded_senders: frozenset[str],
        whole_room: bool = False,
    ) -> bool:
        """Return whether someone outside ``excluded_senders`` wrote in the conversation after these sources."""
        return await self._backend.read(
            lambda transaction: reads.later_message_exists(
                transaction,
                self._principal_id,
                room_id=room_id,
                thread_id=thread_id,
                source_event_ids=source_event_ids,
                excluded_senders=excluded_senders,
                whole_room=whole_room,
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
        return await self._backend.write(
            lambda transaction: _install_room_history_recovery_chunk(
                transaction,
                self._principal_id,
                recovery,
                events=events,
                expected_membership_epoch=expected_membership_epoch,
            ),
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
        return await self._backend.write(
            lambda transaction: _settle_history_recovery(
                transaction,
                self._principal_id,
                recovery,
                exhausted_server=exhausted_server,
                unreadable=unreadable,
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
        revision_sender: str,
        revision_transaction_id: str | None = None,
        content: Mapping[str, object],
    ) -> int | None:
        """Install a point-refetched revision if its refresh token still holds, returning its stored size."""
        return await self._backend.write(
            lambda transaction: install_refetched_revision(
                transaction,
                self._principal_id,
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
                expected_revision_event_id=request.revision_event_id,
                expected_refresh_token=request.refresh_token,
                expected_membership_epoch=request.membership_epoch,
            ),
        )

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
        return await self._backend.write(
            lambda transaction: _enqueue_matrix_delivery(
                transaction,
                self._principal_id,
                delivery_id=delivery_id,
                stage=stage,
                event_type=event_type,
                room_id=room_id,
                thread_id=thread_id,
                payload=payload,
                result=result,
                edits_event_id=edits_event_id,
                permanent_failure_reason=permanent_failure_reason,
            ),
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
        return await self._backend.write(
            lambda transaction: _enqueue_final_answer(
                transaction,
                self._principal_id,
                delivery_id=delivery_id,
                event_type=event_type,
                room_id=room_id,
                thread_id=thread_id,
                payload=payload,
                result=result,
                edits_event_id=edits_event_id,
                permanent_failure_reason=permanent_failure_reason,
                turn_key=turn_key,
            ),
        )

    async def enqueue_reply_row(self, request: ReplyRowRequest, prepared: PreparedReplyRow) -> ReplyRowEnqueue | None:
        """Decide and record one durable write of an agent or team reply.

        ``None`` means the outbox refused the row (the membership that owns
        the reply has ended), and nothing the rule decided was written.
        """
        try:
            return await self._backend.write(
                lambda transaction: enqueue_reply_row(transaction, self._principal_id, request, prepared),
            )
        except ReplyRowRefusedError:
            return None

    async def unresolved_reply_rows(
        self,
        reply_id: str,
        *,
        before_sequence: int | None = None,
    ) -> tuple[tuple[str, DeliveryStage], ...]:
        """Return a reply's rows whose Matrix outcome is unknown, in write order."""
        return await self._backend.read(
            lambda transaction: outbox.unresolved_reply_rows(
                transaction,
                self._principal_id,
                reply_id,
                before_sequence=before_sequence,
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

    async def claim_matrix_delivery(
        self,
        *,
        delivery_id: str,
        stage: DeliveryStage,
        sending_device_id: str | None = None,
    ) -> MatrixDelivery | None:
        """Freeze one delivery before network I/O and return the row as it stood."""
        return await self._backend.write(
            lambda transaction: outbox.claim(
                transaction,
                self._principal_id,
                delivery_id=delivery_id,
                stage=stage,
                sending_device_id=sending_device_id,
            ),
        )

    async def record_matrix_delivery_device(
        self,
        *,
        delivery_id: str,
        stage: DeliveryStage,
        device_id: str | None,
    ) -> None:
        """Record the device namespace this delivery is about to send under."""
        await self._backend.write(
            lambda transaction: outbox.record_matrix_delivery_device(
                transaction,
                self._principal_id,
                delivery_id=delivery_id,
                stage=stage,
                device_id=device_id,
            ),
        )

    async def load_matrix_delivery(self, *, delivery_id: str, stage: DeliveryStage) -> MatrixDelivery | None:
        """Return one delivery without claiming it."""
        return await self._backend.read(
            lambda transaction: outbox.load(
                transaction,
                self._principal_id,
                delivery_id=delivery_id,
                stage=stage,
            ),
        )

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

        def record(transaction: Transaction) -> PermanentDeliveryFailure:
            failed_rows, acknowledged = outbox.record_permanent_failure(
                transaction,
                self._principal_id,
                delivery_id=delivery_id,
                stage=stage,
                reason=reason,
            )
            effects: list[PostCommitEffect] = []
            for failed_id, failed_stage in failed_rows:
                delivery = outbox.load(transaction, self._principal_id, delivery_id=failed_id, stage=failed_stage)
                applied = None if delivery is None else replies.fail_row(transaction, self._principal_id, delivery)
                if applied is not None:
                    effects.extend(applied.post_commit)
            return PermanentDeliveryFailure(acknowledged_event_id=acknowledged, reply_effects=tuple(effects))

        return await self._backend.write(record)

    async def retire_matrix_delivery(
        self,
        *,
        delivery_id: str,
        stage: DeliveryStage,
        room_id: str,
        membership_epoch: int,
    ) -> str | None:
        """Retain an obsolete send as an identity tombstone, or return its ACK."""

        def retire(transaction: Transaction) -> str | None:
            # Projection and acknowledgement take the membership row before
            # the delivery row. Retirement uses the same order, so an echo
            # either projects first and is removed below, or observes the
            # committed tombstone and is refused.
            reads.claim_membership_epoch(
                transaction,
                self._principal_id,
                room_id=room_id,
                expected_membership_epoch=membership_epoch,
            )
            delivery = outbox.retire(
                transaction,
                self._principal_id,
                delivery_id=delivery_id,
                stage=stage,
                room_id=room_id,
                membership_epoch=membership_epoch,
            )
            if delivery is None:
                return None
            if delivery.retired and delivery.edits_event_id is not None:
                discard_delivery_event(
                    transaction,
                    self._principal_id,
                    event_id=delivery.edits_event_id,
                )
            return delivery.acknowledged_event_id

        return await self._backend.write(retire)

    async def acknowledge_matrix_delivery(
        self,
        *,
        delivery_id: str,
        stage: DeliveryStage,
        event_id: str,
        delivered_projections: tuple[ProjectedEvent, ...],
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

        ``delivered_projections`` is the server-ordered Matrix content this row
        depends on and made visible. The winner projects it in this transaction
        before any reaction or numeric answer can become durable. An empty
        tuple means the server already redacted the event, so frozen plaintext
        must not be resurrected.
        """

        def acknowledge(transaction: Transaction) -> DeliveryAcknowledgement:
            ownership = outbox.claim_active_delivery_ownership(
                transaction,
                self._principal_id,
                delivery_id=delivery_id,
                stage=stage,
            )
            may_project = ownership is not None
            projection_epoch = 0 if ownership is None else ownership[1]
            bound = outbox.acknowledge(
                transaction,
                self._principal_id,
                delivery_id=delivery_id,
                stage=stage,
                event_id=event_id,
            )
            # A caller that lost the acknowledgement must not write the record
            # either. The row already names another event, and a terminal
            # record pointing somewhere else is the disagreement this whole
            # transaction exists to prevent.
            if bound and may_project and terminal_turn is not None:
                turn_rows.complete_bound_delivery(
                    transaction,
                    terminal_turn.agent_name,
                    delivery_id=terminal_turn.delivery_id,
                    response_event_id=terminal_turn.response_event_id,
                )
            if bound and may_project:
                for delivered_projection in delivered_projections:
                    project(
                        transaction,
                        self._principal_id,
                        delivered_projection,
                        receipt_order=0,
                        membership_epoch=projection_epoch,
                    )
            elif bound and not may_project:
                discard_delivery_event(
                    transaction,
                    self._principal_id,
                    event_id=event_id,
                )
            if bound:
                delivery = outbox.load(transaction, self._principal_id, delivery_id=delivery_id, stage=stage)
                reply_applied = (
                    None
                    if delivery is None
                    else replies.acknowledge_row(
                        transaction,
                        self._principal_id,
                        delivery,
                        event_id=event_id,
                        membership_current=may_project,
                    )
                )
                return DeliveryAcknowledgement(
                    settled_event_id=event_id,
                    reply_effects=() if reply_applied is None else reply_applied.post_commit,
                )
            # Lost the row. Whatever is on it now is the answer this delivery
            # resolves to, and the caller has to be told that rather than its
            # own event id -- everything downstream records what `flush`
            # returns, so a loser reporting its own send is how the outbox and
            # the turn record end up naming different events.
            settled = transaction.fetchone(
                """
                SELECT acknowledged_event_id FROM matrix_delivery_outbox
                WHERE principal_id = ? AND delivery_id = ? AND stage = ?
                """,
                (self._principal_id, delivery_id, stage.value),
            )
            return DeliveryAcknowledgement(
                settled_event_id=None if settled is None else str(settled["acknowledged_event_id"]),
            )

        return await self._backend.write(acknowledge)

    async def unacknowledged_matrix_deliveries(
        self,
        *,
        event_type: str = "m.room.message",
        limit: int = _DEFAULT_UNACKNOWLEDGED_LIMIT,
        after: tuple[int, str, str] | None = None,
    ) -> tuple[MatrixDelivery | UnreadableMatrixDelivery, ...]:
        """Return deliveries whose Matrix outcome is unknown, oldest first."""
        return await self._backend.read(
            lambda transaction: outbox.unacknowledged(
                transaction,
                self._principal_id,
                limit=limit,
                event_type=event_type,
                after=after,
            ),
        )

    @property
    def principal_id(self) -> str:
        """Return this view's durable principal identity."""
        return self._principal_id

    @property
    def replies(self) -> ReplyStore:
        """Return this principal's reply records."""
        return ReplyStore(_backend=self._backend, _principal_id=self._principal_id)

    @property
    def approvals(self) -> ApprovalStore:
        """Return this principal's approval records."""
        # The approval store writes its pause rows with this module's reply-row writer.
        from .approval_store import ApprovalStore  # noqa: PLC0415

        return ApprovalStore(
            _backend=self._backend,
            _principal_id=self._principal_id,
            _signals=self._approval_signals,
        )


def _turn_membership_is_current(
    transaction,  # noqa: ANN001 - the backend's Transaction, kept structural
    principal_id: str,
    *,
    turn_id: str,
    room_id: str,
) -> bool:
    """Return whether the membership that admitted a turn is still the room's."""
    owner = journal.admitted_membership_owner(transaction, principal_id, turn_id)
    if owner is None:
        owner = outbox.turn_ownership(transaction, principal_id, delivery_id=turn_id)
    if owner is None:
        # Nothing admitted or enqueued this turn, so no membership owns it yet.
        return True
    owner_room_id, owner_epoch = owner
    return owner_room_id == room_id and owner_epoch == journal.current_membership_epoch(
        transaction,
        principal_id,
        room_id,
    )


def _admit(
    transaction: Transaction,
    principal_id: str,
    event: InboundEvent,
    projected: ProjectedEvent | None,
) -> AdmissionResult:
    """Admit one event after any already-visible outbox delivery is projected."""
    result = journal.admit(transaction, principal_id, event, projected)
    if result is AdmissionResult.ADMITTED:
        _snapshot_interactive_source(transaction, principal_id, event)
    return result


def _enqueue_matrix_delivery(
    transaction,  # noqa: ANN001 - the backend's Transaction, kept structural
    principal_id: str,
    *,
    delivery_id: str,
    stage: DeliveryStage,
    event_type: str,
    room_id: str,
    thread_id: str | None,
    payload: Mapping[str, object],
    result: Mapping[str, object] | None,
    edits_event_id: str | None,
    permanent_failure_reason: str | None,
    edit_target_pending: bool = False,
    reply_id: str | None = None,
    span_id: str | None = None,
    reply_sequence: int | None = None,
    reply_row: ReplyRowFacts | None = None,
) -> str | None:
    """Record delivery intent unless the membership that authorized it has ended.

    The fence retires a room's unattempted deliveries because they answer a
    conversation the bot has left. This closes the other half of the same
    window: a turn that was still running when the fence committed would
    otherwise write its answer back in afterwards, and the fence has already
    been and gone. Because both are writes that claim the membership row, the
    two possible orderings are "enqueued, then retired" and "fenced, then
    refused". Neither leaves a sendable answer behind, while the retired row
    prevents a later stage from adopting the rejoined membership.

    An already-attempted row remains recoverable, and deliberately so. Its
    outcome is unknown -- the homeserver may be holding it -- and refusing the
    retry would strand it unacknowledged forever while leaving whatever it
    sent visible. Same-device recovery can reuse the frozen transaction ID;
    changed-device recovery first reconciles exact room history and then
    follows the delivery type's explicit replay-or-retain policy.

    """
    admitted_owner = journal.admitted_membership_owner(transaction, principal_id, delivery_id)
    attempted = outbox.is_attempted(
        transaction,
        principal_id,
        delivery_id=delivery_id,
        stage=stage,
    )
    if attempted:
        ownership = outbox.delivery_ownership(
            transaction,
            principal_id,
            delivery_id=delivery_id,
            stage=stage,
        )
        if ownership is None:
            msg = f"Attempted delivery {delivery_id!r}/{stage.value!r} has no outbox row"
            raise RuntimeError(msg)
        _stored_room_id, membership_epoch = ownership
    elif admitted_owner is None:
        active_epoch = claim_active_membership_epoch(
            transaction,
            principal_id,
            room_id=room_id,
        )
        if active_epoch is None:
            return None
        membership_epoch = active_epoch
    else:
        admitted_room_id, admitted_epoch = admitted_owner
        if admitted_room_id != room_id or not reads.claim_membership_epoch(
            transaction,
            principal_id,
            room_id=room_id,
            expected_membership_epoch=admitted_epoch,
        ):
            return None
        membership_epoch = admitted_epoch
    return outbox.enqueue(
        transaction,
        principal_id,
        delivery_id=delivery_id,
        stage=stage,
        event_type=event_type,
        room_id=room_id,
        membership_epoch=membership_epoch,
        thread_id=thread_id,
        payload=payload,
        result=result,
        edits_event_id=edits_event_id,
        edit_target_pending=edit_target_pending,
        permanent_failure_reason=permanent_failure_reason,
        reply_id=reply_id,
        span_id=span_id,
        reply_sequence=reply_sequence,
        reply_row=reply_row,
    )


def _enqueue_final_answer(
    transaction,  # noqa: ANN001 - the backend's Transaction, kept structural
    principal_id: str,
    *,
    delivery_id: str,
    event_type: str,
    room_id: str,
    thread_id: str | None,
    payload: Mapping[str, object],
    result: Mapping[str, object] | None,
    edits_event_id: str | None,
    permanent_failure_reason: str | None,
    turn_key: str | None,
) -> FinalAnswerEnqueue | None:
    """Record a FINAL answer and settle what its bound turn answers; a refusal settles nothing.

    Nothing durable would owe the answer after a refusal, so the turn stays the
    journal's.
    """
    transaction_id = _enqueue_matrix_delivery(
        transaction,
        principal_id,
        delivery_id=delivery_id,
        stage=DeliveryStage.FINAL,
        event_type=event_type,
        room_id=room_id,
        thread_id=thread_id,
        payload=payload,
        result=result,
        edits_event_id=edits_event_id,
        permanent_failure_reason=permanent_failure_reason,
    )
    if transaction_id is None:
        return None
    bound = (
        None
        if turn_key is None
        else turn_rows.bind_final_delivery(
            transaction,
            turn_rows.agent_of_principal(principal_id),
            delivery_id=delivery_id,
            room_id=room_id,
            turn_key=turn_key,
        )
    )
    settled = tuple(dict.fromkeys(bound or (delivery_id,)))
    journal.settle_many(transaction, principal_id, settled)
    return FinalAnswerEnqueue(transaction_id=transaction_id, settled_event_ids=settled)


class ReplyRowRefusedError(Exception):
    """The outbox refused a reply row the lifecycle had already decided; roll both back."""


def _decide_reply_row(
    transaction: Transaction,
    principal_id: str,
    request: ReplyRowRequest,
) -> ReplyRowEnqueue | tuple[rl.Reply, rl.Span, rl.Transition]:
    """Return the row a retried write resolves to, or the reply, span, and transition the rule decided."""
    if request.create is not None:
        claim = request.create.claim
        earlier = reply_spans.latest_for_delivery(transaction, principal_id, claim.delivery_id)
        if earlier is not None:
            # A retried acknowledgement finds the reply its first attempt created,
            # and resolves that attempt's row instead of writing another.
            created = reply_messages.lock(transaction, principal_id, earlier.reply_id)
            assert created is not None
            reused = _span_row(transaction, principal_id, created, earlier, rl.WriteStage.INITIAL)
            assert reused is not None, "an acknowledgement's reply is created with its INITIAL row"
            return replace(
                reused,
                applied=AppliedTransition(
                    transition=rl.Transition(outcome=rl.Outcome.DUPLICATE, reply=created, spans=(earlier,)),
                    post_commit=(),
                ),
            )
        transition = rl.interactive_acknowledgement(claim, shown=request.create.shown)
        assert transition.reply is not None
        reply, span = transition.reply, transition.spans[0]
    else:
        assert request.decide is not None
        locked = reply_messages.lock(transaction, principal_id, request.reply_id)
        loaded = reply_spans.load(transaction, principal_id, request.span_id)
        if locked is None or loaded is None:
            msg = f"Reply {request.reply_id} or span {request.span_id} does not exist"
            raise RuntimeError(msg)
        reply, span = locked, loaded
        earlier = _span_row(transaction, principal_id, reply, span, request.stage)
        if earlier is not None:
            return earlier
        transition = (
            rl.Transition(outcome=rl.Outcome.STALE, reply=reply)
            if replies.retired(transaction, principal_id, span, author_generation=request.author_generation)
            else request.decide(reply, span)
        )
    return reply, span, transition


# Every reply row is a room message.
_REPLY_ROW_EVENT_TYPE = "m.room.message"


def enqueue_reply_row(
    transaction: Transaction,
    principal_id: str,
    request: ReplyRowRequest,
    prepared: PreparedReplyRow,
) -> ReplyRowEnqueue:
    """Decide one reply write with its lifecycle rule and record the row it chose, in one transaction.

    The rule runs against the locked reply and its span as they are now, so a
    payload rendered before a Stop or deletion committed is refused rather
    than written (``Outcome.RECOMPUTE``). Sources the rule settles are settled
    here, replacing the turn handoff a plain ``FINAL`` carries.
    """
    decided = _decide_reply_row(transaction, principal_id, request)
    if isinstance(decided, ReplyRowEnqueue):
        return decided
    reply, span, transition = decided
    if not transition.applied or transition.row is None:
        return ReplyRowEnqueue(
            applied=replies.with_ended_span(replies.apply(transaction, principal_id, transition), span),
            settled_event_ids=replies.settled_event_ids(transaction, principal_id, transition),
        )
    row = transition.row
    stage = row.stage
    edits_event_id = None if stage is DeliveryStage.INITIAL else reply.event_id
    # A row waits for the reply's event only while an earlier row may still create it. With none left, as when
    # Matrix refused the only create for good, the row creates the event itself.
    edit_target_pending = (
        edits_event_id is None
        and stage is not DeliveryStage.INITIAL
        and replies.has_unresolved_rows(transaction, principal_id, reply.reply_id)
    )
    row_fields = {
        "reply_id": reply.reply_id,
        "span_id": span.span_id,
        "reply_sequence": row.sequence,
        "reply_row": ReplyRowFacts(placeholder_only=request.placeholder_only, new_text=prepared.new_text),
    }
    if stage is DeliveryStage.EDIT:
        delivery_id = replies.edit_delivery_id(span.delivery_id, row.sequence)
        if not reads.claim_membership_epoch(
            transaction,
            principal_id,
            room_id=reply.room_id,
            expected_membership_epoch=reply.membership_epoch,
        ):
            raise ReplyRowRefusedError
        transaction_id = outbox.enqueue(
            transaction,
            principal_id,
            delivery_id=delivery_id,
            stage=stage,
            event_type=_REPLY_ROW_EVENT_TYPE,
            room_id=reply.room_id,
            membership_epoch=reply.membership_epoch,
            thread_id=reply.thread_id,
            payload=prepared.payload,
            result=prepared.result,
            edits_event_id=edits_event_id,
            edit_target_pending=edit_target_pending,
            permanent_failure_reason=prepared.permanent_failure_reason,
            **row_fields,
        )
    else:
        delivery_id = span.delivery_id
        transaction_id = _enqueue_matrix_delivery(
            transaction,
            principal_id,
            delivery_id=delivery_id,
            stage=stage,
            event_type=_REPLY_ROW_EVENT_TYPE,
            room_id=reply.room_id,
            thread_id=reply.thread_id,
            payload=prepared.payload,
            result=prepared.result,
            edits_event_id=edits_event_id,
            permanent_failure_reason=prepared.permanent_failure_reason,
            edit_target_pending=edit_target_pending,
            **row_fields,
        )
    if transaction_id is None:
        raise ReplyRowRefusedError
    applied = replies.apply(transaction, principal_id, transition)
    if prepared.permanent_failure_reason is not None:
        # A payload refused before any send fails its row now, as a refusal from Matrix would.
        refused = outbox.load(transaction, principal_id, delivery_id=delivery_id, stage=stage)
        assert refused is not None
        failed = replies.fail_row(transaction, principal_id, refused)
        if failed is not None:
            applied = AppliedTransition(
                transition=transition,
                post_commit=(*applied.post_commit, *failed.post_commit),
            )
    return ReplyRowEnqueue(
        applied=applied,
        delivery_id=delivery_id,
        stage=row.stage,
        settled_event_ids=replies.settled_event_ids(transaction, principal_id, transition),
        sequence=row.sequence,
    )


def _span_row(
    transaction: Transaction,
    principal_id: str,
    reply: rl.Reply,
    span: rl.Span,
    stage: rl.WriteStage | None,
) -> ReplyRowEnqueue | None:
    """Return the span's INITIAL or FINAL row already recorded, which a retried write resolves to.

    A send that failed after its row was recorded leaves the row owed; writing
    it again would decide a second time against a span that has moved on. The
    stored row keeps its payload, as a re-enqueued attempted row does.
    """
    if stage not in {rl.WriteStage.INITIAL, rl.WriteStage.FINAL}:
        return None
    stored = outbox.load(transaction, principal_id, delivery_id=span.delivery_id, stage=stage)
    if stored is None or stored.reply_id != reply.reply_id:
        return None
    if stage is rl.WriteStage.FINAL and stored.span_id != span.span_id:
        return None
    return ReplyRowEnqueue(
        applied=AppliedTransition(
            transition=rl.Transition(outcome=rl.Outcome.DUPLICATE, reply=reply),
            post_commit=(),
        ),
        delivery_id=stored.delivery_id,
        stage=stage,
        sequence=stored.reply_sequence,
    )


def _settle_history_recovery(
    transaction,  # noqa: ANN001 - the backend's Transaction, kept structural
    principal_id: str,
    recovery: RoomHistoryRecovery,
    *,
    exhausted_server: bool,
    unreadable: bool,
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
    if exhausted_server and unreadable:
        # Repairing unmasks every marker the gap retracted, and one from before
        # the gap cannot vouch for an event this walk could not read: it may be
        # a reply in that very thread. A truncated obligation already withholds
        # their completeness.
        reads.revoke_room_hydration(transaction, principal_id, room_id=recovery.room_id)
    reads.publish_conversation_hydration(
        transaction,
        principal_id,
        room_id=recovery.room_id,
        thread_id=None,
        complete=exhausted_server and not unreadable,
        attempted_policy_rank=attempted_policy_rank,
        membership_epoch=expected_membership_epoch,
    )
    return journal.settle_room_history_recovery(
        transaction,
        principal_id,
        recovery,
        exhausted_server=exhausted_server,
        attempted_policy_rank=attempted_policy_rank,
    )


def _settle_turn_sources(transaction: Transaction, principal_id: str, event_ids: tuple[str, ...]) -> tuple[str, ...]:
    journal.settle_many(transaction, principal_id, event_ids)
    return replies.drop_replays(transaction, principal_id, event_ids, now_ns=time.time_ns())


def _install_room_history_recovery_chunk(
    transaction,  # noqa: ANN001 - the backend's Transaction, kept structural
    principal_id: str,
    recovery: RoomHistoryRecovery,
    *,
    events: tuple[ProjectedEvent, ...],
    expected_membership_epoch: int,
) -> bool:
    """Project one chunk only while its membership and exact recovery stand."""
    if not reads.claim_membership_epoch(
        transaction,
        principal_id,
        room_id=recovery.room_id,
        expected_membership_epoch=expected_membership_epoch,
    ):
        return False
    if not journal.claim_room_history_recovery(transaction, principal_id, recovery):
        return False
    for event in events:
        journal.project_with_redactions(
            transaction,
            principal_id,
            event,
            receipt_order=0,
            membership_epoch=expected_membership_epoch,
            settle_redacted_arrival=False,
        )
    return True


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
        journal.project_with_redactions(
            transaction,
            principal_id,
            event,
            receipt_order=0,
            membership_epoch=expected_membership_epoch,
            settle_redacted_arrival=False,
        )
    return True


@dataclass(frozen=True, slots=True)
class EventJournalStore:
    """The shared backend, which hands out per-principal views."""

    backend: Backend
    # What this store's committed approval writes tell the process; its backend publishes them here.
    approval_signals: ApprovalSignals

    @classmethod
    def open_sqlite(cls, database_path: Path) -> EventJournalStore:
        """Open a single-writer SQLite store."""
        from .sqlite_backend import SqliteBackend  # noqa: PLC0415 - backend chosen at runtime

        signals = ApprovalSignals()
        return cls(backend=SqliteBackend.open(database_path, publish=signals.publish), approval_signals=signals)

    @classmethod
    def open_postgres(cls, database_url: str) -> EventJournalStore:
        """Open a PostgreSQL store."""
        from .postgres_backend import PostgresBackend  # noqa: PLC0415 - keeps psycopg optional

        signals = ApprovalSignals()
        return cls(backend=PostgresBackend.open(database_url, publish=signals.publish), approval_signals=signals)

    def principal(self, principal_id: str) -> PrincipalStore:
        """Return the bound view for one bot."""
        if not principal_id:
            msg = "An event-journal principal requires an identity"
            raise ValueError(msg)
        return PrincipalStore(
            _backend=self.backend,
            _principal_id=principal_id,
            _approval_signals=self.approval_signals,
        )

    async def load_approval_signals(self) -> None:
        """Load every principal's paused replies into the approval signals, before any bot follows them."""
        await self.backend.write(approval_signals.load)

    async def approval_continuations(
        self,
        *,
        limit: int = _DEFAULT_APPROVAL_CONTINUATION_OWNER_LIMIT,
        after: str | None = None,
    ) -> tuple[tuple[str, ApprovalContinuation], ...]:
        """Return one bounded page of continuations after the approval id ``after``, with their journal principals."""
        return await self.backend.read(
            lambda transaction: approval_continuations.all_owners(
                transaction,
                limit=limit,
                after=after,
            ),
        )

    async def hold_exclusively(self, identity: str) -> bool:
        """Claim this journal for one runtime until it closes; ``False`` when another runtime holds it."""
        return await self.backend.hold_exclusively(identity)

    async def still_held(self) -> bool:
        """Return whether this runtime's claim on the journal still holds."""
        return await self.backend.still_held()

    async def end_entity_replies(self, ends: Callable[[str], bool], *, now_ns: int) -> int:
        """End the open replies of the entities ``ends`` names; return how many ended."""
        return await self.backend.write(
            lambda transaction: replies.end_entity_replies(transaction, ends, now_ns=now_ns),
        )

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

    def turn_records(self, agent_name: str) -> turn_rows.TurnRecordStore:
        """Return the agent-scoped turn-row view.

        Not a ``PrincipalStore`` method, because turn records are not scoped to
        a Matrix identity. They record that a message was answered, which stays
        true across a re-login; scoping them per principal would make a bot
        that logs in as a new device answer everything a second time.
        """
        if not agent_name:
            msg = "Turn records require an agent name"
            raise ValueError(msg)
        return turn_rows.TurnRecordStore(backend=self.backend, agent_name=agent_name)

    def tool_jobs(self, runtime_generation: str) -> ToolJobStore:
        """Return the background tool jobs as one runtime generation saves them.

        Jobs belong to the install, not to a bot, so this is not a principal view
        either. The generation fences writes: whichever runtime last took
        ownership is the only one whose saves land.
        """
        return ToolJobStore(_backend=self.backend, _runtime_generation=runtime_generation)

    async def close(self) -> None:
        """Release every connection the backend owns."""
        await self.backend.close()


@dataclass(frozen=True, slots=True)
class ToolJobStore:
    """The install's background tool jobs, written only by the runtime generation that last took them over.

    A snapshot and its outcome payload commit together, so a reader never sees
    one without the other, and a crash leaves no payload that nothing references.
    """

    _backend: Backend
    _runtime_generation: str

    async def take_ownership(self) -> None:
        """Become the only writer of every job; an older runtime's later saves are refused."""
        await self._backend.write(
            lambda transaction: tool_jobs.take_ownership(transaction, self._runtime_generation),
        )

    async def require_ownership(self) -> None:
        """Raise ``ToolJobOwnershipLostError`` unless this generation still owns every job."""
        await self._backend.write(
            lambda transaction: tool_jobs.require_ownership(transaction, self._runtime_generation),
        )

    async def load_all(self) -> tuple[SavedToolJob, ...]:
        """Return every saved job."""
        return await self._backend.read(tool_jobs.load_all)

    async def accept(self, job_id: str, job_json: str) -> None:
        """Save a newly accepted job, refusing an ID that is still saved."""
        await self._backend.write(
            lambda transaction: tool_jobs.accept(transaction, self._runtime_generation, job_id, job_json),
        )

    async def save(
        self,
        job_id: str,
        job_json: str,
        result_payload_json: str | None = None,
        *,
        ends_approvals: bool,
    ) -> None:
        """Replace a job's snapshot, with its outcome payload when one is given; a save that ends the job's approvals denies its cards."""
        await self._backend.write(
            lambda transaction: tool_jobs.save(
                transaction,
                self._runtime_generation,
                job_id,
                job_json,
                result_payload_json,
                ends_approvals=ends_approvals,
            ),
        )

    async def load_payload(self, job_id: str) -> str | None:
        """Return a job's outcome payload, or ``None`` when it has none or no longer exists."""
        return await self._backend.read(lambda transaction: tool_jobs.load_payload(transaction, job_id))

    async def delete(self, job_id: str) -> None:
        """Forget a job together with its payload."""
        await self._backend.write(
            lambda transaction: tool_jobs.delete(transaction, self._runtime_generation, job_id),
        )
