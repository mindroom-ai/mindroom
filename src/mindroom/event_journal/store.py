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
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from mindroom import reply_lifecycle as rl
from mindroom.history_recovery import (
    HistoryRecoveryOutcome,
    RoomHistoryRecovery,
)
from mindroom.tool_approval_grants import ApprovalGrant, ApprovalGrantRevocation  # noqa: TC001

from . import (
    approval_continuations,
    approval_grants,
    approvals,
    background_approvals,
    interactive_questions,
    journal,
    legacy_reply_messages,
    legacy_turn_records,
    membership_hooks,
    outbox,
    reads,
    replies,
    reply_messages,
    reply_spans,
    response_attempts,
    scheduled_approvals,
    turn_records,
)
from .approval_card_state import (  # noqa: TC001 - part of this module's runtime return types
    ApprovalCardReservation,
    ApprovalDecisionMetadata,
    RecordedApprovalDecision,
)
from .approval_continuations import (
    ApprovalAdvance,
    ApprovalCall,
    ApprovalContinuation,
    ApprovalContinuationState,
)
from .approvals import (  # noqa: TC001 - part of this module's runtime return types
    StoredApprovalCard,
    UnreadableApprovalCard,
)
from .background_approvals import BackgroundApprovalDecision  # noqa: TC001
from .membership_state import claim_active_membership_epoch
from .models import (
    AdmissionResult,
    DeliveryAcknowledgement,
    DeliveryProjectionPendingError,
    DeliveryStage,
    IngestionConsumer,
    IngestionConsumerBindingError,
    PermanentDeliveryFailure,
    ResponseRecoveryState,
)
from .projection import (
    discard_delivery_event,
    drop_refetched_message,
    install_refetched_revision,
    is_tombstoned,
    project,
    tombstoned_event_ids,
)
from .replies import ReplyRowEnqueue, ReplyRowRequest, ReplyStore
from .scheduled_approvals import (  # noqa: TC001
    ScheduledApprovalArmState,
    ScheduledCall,
    ScheduledCallBinding,
    ScheduledCallClaim,
    ScheduledCallOutcome,
    ScheduledCallRefusal,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    from mindroom.interactive_models import InteractivePrompt
    from mindroom.response_sources import ResponseAttempt
    from mindroom.turn_record import TurnRecord

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
_DEFAULT_ROOM_CARD_LIMIT = 256
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
        *,
        turn_record: TurnRecord,
        agent_name: str,
        redaction_target: Callable[[JournalEvent], str | None],
    ) -> ResponseRecoveryState:
        """Read one response's durable handoff through the reserved recovery lane."""

        def load(transaction: Transaction) -> ResponseRecoveryState:
            source_event_ids = turn_record.source_event_ids
            turn_id = turn_record.anchor_event_id
            records = {
                event_id: turn_records.load_record(transaction, agent_name, event_id)
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
                approval_owned=any(
                    outbox.approval_owns_delivery(transaction, self._principal_id, event_id)
                    for event_id in source_event_ids
                ),
                pending_sources=pending,
                redacted_sources=tuple(
                    not is_pending
                    and journal.source_has_redaction_handoff(
                        transaction,
                        self._principal_id,
                        event_id,
                        turn_record,
                        records.get(event_id),
                        redaction_target,
                    )
                    for event_id, is_pending in zip(source_event_ids, pending, strict=True)
                ),
                turn_records=tuple(records.values()),
                final_delivery=delivery,
                sources_settled_by_departure=(
                    not any(pending)
                    and journal.sources_settled_by_departure(transaction, self._principal_id, source_event_ids)
                ),
                source_tombstones=tuple(
                    (
                        turn_record.conversation_target is not None
                        and is_tombstoned(
                            transaction,
                            self._principal_id,
                            room_id=turn_record.conversation_target.room_id,
                            event_id=event_id,
                        )
                    )
                    or (
                        (current := records.get(event_id)) is not None and event_id in current.redacted_source_event_ids
                    )
                    for event_id in source_event_ids
                ),
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

    async def is_event_redacted(self, *, room_id: str, event_id: str) -> bool:
        """Read exact projection tombstone authority for this principal."""
        return await self._backend.read(
            lambda transaction: is_tombstoned(transaction, self._principal_id, room_id, event_id),
        )

    async def redacted_event_ids(self, room_id: str, event_ids: tuple[str, ...]) -> frozenset[str]:
        """Read recorded context tombstones in one transaction and one offload."""
        return await self._backend.read(
            lambda transaction: frozenset(
                event_id
                for batch in batched(event_ids, 256)
                for event_id in tombstoned_event_ids(transaction, self._principal_id, room_id, batch)
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
        attempted_policy_rank: int,
        expected_membership_epoch: int,
    ) -> HistoryRecoveryOutcome:
        """Publish an installed recovery and settle its exact obligation once."""
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
        response_attempt: ResponseAttempt | None = None,
        event_type: str = "m.room.message",
        edits_event_id: str | None = None,
        settle_source_event_ids: tuple[str, ...] = (),
        permanent_failure_reason: str | None = None,
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
                response_attempt=response_attempt,
                edits_event_id=edits_event_id,
                settle_source_event_ids=settle_source_event_ids,
                permanent_failure_reason=permanent_failure_reason,
            ),
        )

    async def enqueue_reply_row(
        self,
        *,
        request: ReplyRowRequest,
        room_id: str,
        thread_id: str | None,
        payload: Mapping[str, object],
        result: Mapping[str, object] | None = None,
        response_attempt: ResponseAttempt | None = None,
        event_type: str = "m.room.message",
        permanent_failure_reason: str | None = None,
        new_text: str | None = None,
    ) -> ReplyRowEnqueue | None:
        """Decide and record one durable write of an agent or team reply.

        ``None`` means the outbox refused the row (the membership that owns
        the reply has ended), and nothing the rule decided was written.
        """
        try:
            return await self._backend.write(
                lambda transaction: _enqueue_reply_row(
                    transaction,
                    self._principal_id,
                    request=request,
                    event_type=event_type,
                    room_id=room_id,
                    thread_id=thread_id,
                    payload=payload,
                    result=result,
                    response_attempt=response_attempt,
                    permanent_failure_reason=permanent_failure_reason,
                    new_text=new_text,
                ),
            )
        except _ReplyRowRefusedError:
            return None

    async def unresolved_reply_rows(
        self,
        reply_id: str,
        *,
        before_sequence: int | None = None,
    ) -> tuple[tuple[str, DeliveryStage, int], ...]:
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

        def claim(transaction: Transaction) -> MatrixDelivery | None:
            if stage is DeliveryStage.FINAL and approval_continuations.retire_superseded_failure_for_source(
                transaction,
                self._principal_id,
                event_id=delivery_id,
            ):
                return None
            return outbox.claim(
                transaction,
                self._principal_id,
                delivery_id=delivery_id,
                stage=stage,
                sending_device_id=sending_device_id,
            )

        return await self._backend.write(claim)

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
            effects: list[replies.PostCommitEffect] = []
            for failed_id, failed_stage in failed_rows:
                delivery = outbox.load(transaction, self._principal_id, delivery_id=failed_id, stage=failed_stage)
                applied = (
                    None
                    if delivery is None
                    else replies.fail_row(
                        transaction,
                        self._principal_id,
                        delivery,
                        reason=delivery.permanent_failure_reason or reason,
                    )
                )
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
            if bound:
                delivery = transaction.fetchone(
                    "SELECT edits_event_id FROM matrix_delivery_outbox WHERE principal_id = ? AND delivery_id = ? AND stage = ?",
                    (self._principal_id, delivery_id, stage.value),
                )
                if delivery is not None:
                    response_attempts.bind_response_target(
                        transaction,
                        self._principal_id,
                        delivery_id,
                        str(delivery["edits_event_id"] or event_id),
                    )
            # A caller that lost the acknowledgement must not write the record
            # either. The row already names another event, and a terminal
            # record pointing somewhere else is the disagreement this whole
            # transaction exists to prevent.
            committed_terminal = (
                turn_records.commit_terminal(transaction, terminal_turn)
                if bound and may_project and terminal_turn is not None
                else None
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
                    bound=True,
                    terminal_turn=committed_terminal,
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
                bound=False,
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

    async def initial_response_delivery_id(self, event_id: str) -> str | None:
        """Resolve this principal's exact INITIAL ACK, including retired cleanup proof."""
        return await self._backend.read(
            lambda transaction: outbox.initial_response_delivery_id(transaction, self._principal_id, event_id),
        )

    async def deleted_initial_deliveries(
        self,
        *,
        agent_name: str,
        after: tuple[int, str] | None = None,
    ) -> tuple[MatrixDelivery | UnreadableMatrixDelivery, ...]:
        """Discover exact deleted-source INITIAL debt, including acknowledged sends."""
        return await self._backend.read(
            lambda transaction: outbox.deleted_initials(
                transaction,
                self._principal_id,
                agent_name=agent_name,
                after=after,
            ),
        )

    async def retire_deleted_initial(self, *, delivery_id: str) -> None:
        """Fence an INITIAL whose visible cleanup and record detachment finished."""
        await self._backend.write(
            lambda transaction: outbox.retire_deleted_initial(transaction, self._principal_id, delivery_id),
        )

    async def reserve_approval_card_deliveries(
        self,
        *,
        continuation_principal_id: str,
        continuation_id: str,
        expected_generation: int,
        cards: tuple[ApprovalCardReservation, ...],
    ) -> bool:
        """Atomically reserve every exact-call card and release its publication lease."""
        return await self._backend.write(
            lambda transaction: approvals.reserve_deliveries(
                transaction,
                self._principal_id,
                continuation_principal_id=continuation_principal_id,
                continuation_id=continuation_id,
                expected_generation=expected_generation,
                cards=cards,
            ),
        )

    async def reserve_background_approval_card(
        self,
        *,
        room_id: str,
        thread_id: str | None,
        run_id: str,
        call_id: str,
        expires_at_ns: int,
        card: ApprovalCardReservation,
    ) -> bool:
        """Atomically reserve one exact background-call approval card."""
        return await self._backend.write(
            lambda transaction: background_approvals.reserve_script_delivery(
                transaction,
                self._principal_id,
                room_id=room_id,
                thread_id=thread_id,
                run_id=run_id,
                call_id=call_id,
                expires_at_ns=expires_at_ns,
                card=card,
            ),
        )

    async def background_approval_decision(
        self,
        *,
        run_id: str,
        call_id: str,
    ) -> BackgroundApprovalDecision | None:
        """Return one exact background call's terminal decision."""
        return await self._backend.read(
            lambda transaction: background_approvals.decision(
                transaction,
                self._principal_id,
                run_id=run_id,
                call_id=call_id,
            ),
        )

    async def resolve_background_approval_call(
        self,
        *,
        run_id: str,
        call_id: str,
        requested_status: Literal["denied", "expired"],
        reason: str,
    ) -> RecordedApprovalDecision:
        """Resolve one exact background target through the shared card transaction."""
        return await self._backend.write(
            lambda transaction: background_approvals.resolve_call(
                transaction,
                self._principal_id,
                run_id=run_id,
                call_id=call_id,
                requested_status=requested_status,
                reason=reason,
            ),
        )

    async def resolve_pending_background_approval_calls(
        self,
        *,
        run_id: str,
        reason: str,
    ) -> int:
        """Resolve every pending background target for one run atomically."""
        return await self._backend.write(
            lambda transaction: background_approvals.resolve_pending_calls(
                transaction,
                self._principal_id,
                run_id=run_id,
                reason=reason,
            ),
        )

    async def prune_background_approvals(self, *, run_id: str) -> bool:
        """Prune settled background targets after their cards have retired."""
        return await self._backend.write(
            lambda transaction: background_approvals.prune_calls(
                transaction,
                self._principal_id,
                run_id=run_id,
            ),
        )

    async def reserve_scheduled_call_approval(
        self,
        *,
        binding: ScheduledCallBinding,
        card: ApprovalCardReservation,
    ) -> bool:
        """Atomically reserve one scheduling-time card and its exact-call binding."""
        return await self._backend.write(
            lambda transaction: scheduled_approvals.reserve(
                transaction,
                self._principal_id,
                binding=binding,
                card=card,
            ),
        )

    async def arm_scheduled_call_approval(
        self,
        *,
        task_id: str,
        workflow_digest: str,
        any_arguments_allowed: bool,
    ) -> ScheduledApprovalArmState:
        """Arm one approved scheduled call for its unchanged task firing on time."""
        return await self._backend.write(
            lambda transaction: scheduled_approvals.arm(
                transaction,
                self._principal_id,
                task_id=task_id,
                workflow_digest=workflow_digest,
                any_arguments_allowed=any_arguments_allowed,
                now_ns=time.time_ns(),
            ),
        )

    async def withdraw_scheduled_call_approval(self, *, task_id: str, reason: str) -> RecordedApprovalDecision:
        """Withdraw one cancelled task's approval and deny its card if still pending."""
        return await self._backend.write(
            lambda transaction: scheduled_approvals.withdraw(
                transaction,
                self._principal_id,
                task_id=task_id,
                reason=reason,
            ),
        )

    async def scheduled_call(self, *, task_id: str) -> ScheduledCall | None:
        """Read the call one scheduled task stored."""
        return await self._backend.read(
            lambda transaction: scheduled_approvals.stored_call(transaction, self._principal_id, task_id=task_id),
        )

    async def claim_scheduled_call(
        self,
        *,
        call: ScheduledCall,
        arguments_json: str,
        receipt: ApprovalCardReservation,
    ) -> ScheduledCallClaim | ScheduledCallRefusal:
        """Spend one armed scheduled approval and reserve its receipt in one commit."""
        return await self._backend.write(
            lambda transaction: scheduled_approvals.claim(
                transaction,
                self._principal_id,
                call=call,
                arguments_json=arguments_json,
                receipt=receipt,
                now_ns=time.time_ns(),
            ),
        )

    async def record_scheduled_call_outcome(self, *, task_id: str, outcome: ScheduledCallOutcome) -> None:
        """Record how one claimed scheduled call ended."""
        await self._backend.write(
            lambda transaction: scheduled_approvals.record_outcome(
                transaction,
                self._principal_id,
                task_id=task_id,
                outcome=outcome,
            ),
        )

    async def resolve_continuation_approval_card(
        self,
        *,
        card_event_id: str,
        requested_status: Literal["approved", "denied", "expired"],
        reason: str | None,
        metadata: ApprovalDecisionMetadata,
    ) -> RecordedApprovalDecision:
        """Atomically record one native card and its exact-call decision."""
        return await self._backend.write(
            lambda transaction: approvals.resolve_card(
                transaction,
                self._principal_id,
                card_event_id=card_event_id,
                requested_status=requested_status,
                reason=reason,
                metadata=metadata,
            ),
        )

    async def create_approval_grant(
        self,
        *,
        room_id: str,
        card_event_id: str,
        sender_id: str,
        seconds: int,
        metadata: ApprovalDecisionMetadata,
        reason: str | None = None,
        current_binding: str | None = None,
    ) -> tuple[RecordedApprovalDecision, ...]:
        """Commit the originating decision, fixed grant, and matching pending decisions."""
        return await self._backend.write(
            lambda transaction: approval_grants.create(
                transaction,
                self._principal_id,
                room_id=room_id,
                card_event_id=card_event_id,
                sender_id=sender_id,
                seconds=seconds,
                metadata=metadata,
                reason=reason,
                current_binding=current_binding,
            ),
        )

    async def approval_grant_for_card(self, *, room_id: str, card_event_id: str) -> ApprovalGrant | None:
        """Load a grant independently of its retired card."""
        return await self._backend.read(
            lambda transaction: approval_grants.for_card(
                transaction,
                self._principal_id,
                room_id=room_id,
                card_event_id=card_event_id,
            ),
        )

    async def maintain_automatic_approvals(self, *, grant_id: str | None = None) -> tuple[str, ...]:
        """Retire settled automatic receipts, enqueue grant revocations after their approval edits, and prune old scheduled calls."""

        def maintain(transaction: Transaction) -> tuple[str, ...]:
            if grant_id is None:
                approvals.retire_automatic_receipts(transaction, self._principal_id)
            deliveries = approval_grants.maintain(transaction, self._principal_id, grant_id=grant_id)
            if grant_id is None:
                scheduled_approvals.prune(transaction, self._principal_id, time.time_ns())
            return deliveries

        return await self._backend.write(maintain)

    async def revoke_approval_grant(
        self,
        *,
        room_id: str,
        card_event_id: str,
        sender_id: str,
        grant_id: str,
    ) -> ApprovalGrantRevocation | None:
        """Record durable revocation debt for ordered acknowledgement delivery."""
        return await self._backend.write(
            lambda transaction: approval_grants.revoke(
                transaction,
                self._principal_id,
                room_id=room_id,
                card_event_id=card_event_id,
                sender_id=sender_id,
                grant_id=grant_id,
            ),
        )

    async def expire_unacknowledged_approval_card(
        self,
        *,
        delivery_id: str,
    ) -> RecordedApprovalDecision:
        """Atomically expire a due call whose attempted card still lacks an event ID."""
        return await self._backend.write(
            lambda transaction: approvals.resolve_card(
                transaction,
                self._principal_id,
                card_event_id=None,
                requested_status="expired",
                reason=None,
                metadata=None,
                delivery_id=delivery_id,
            ),
        )

    async def retire_approval_card(self, *, delivery_id: str, card_event_id: str) -> bool:
        """Retire delivered payload while preserving durable approval-only classification."""
        return await self._backend.write(
            lambda transaction: approvals.retire(
                transaction,
                self._principal_id,
                delivery_id=delivery_id,
                card_event_id=card_event_id,
            ),
        )

    async def remember_terminal_approval_alias(self, *, room_id: str, card_event_id: str, delivery_id: str) -> None:
        """Retain a transport-verified receipt alias without changing its decision."""
        await self._backend.write(
            lambda transaction: approvals.remember_terminal_alias(
                transaction,
                self._principal_id,
                room_id=room_id,
                card_event_id=card_event_id,
                delivery_id=delivery_id,
            ),
        )

    async def is_terminal_approval_card(self, *, room_id: str, card_event_id: str) -> bool:
        """Return whether one delivered approval action is durably terminal."""
        return await self._backend.read(
            lambda transaction: approvals.is_terminal_card(
                transaction,
                self._principal_id,
                room_id=room_id,
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

    async def pending_approval_room_ids(self) -> tuple[str, ...]:
        """Return every current room where this principal still owns a card."""
        return await self._backend.read(
            lambda transaction: approvals.pending_room_ids(transaction, self._principal_id),
        )

    async def pending_approval_cards(
        self,
        *,
        room_id: str,
        limit: int = _DEFAULT_ROOM_CARD_LIMIT,
        after: tuple[int, str] | None = None,
    ) -> tuple[StoredApprovalCard | UnreadableApprovalCard, ...]:
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

    async def create_approval_continuation(
        self,
        continuation: ApprovalContinuation,
    ) -> ApprovalContinuation | None:
        """Create one paused-run owner while all original sources remain pending."""
        return await self._backend.write(
            lambda transaction: approval_continuations.create(
                transaction,
                self._principal_id,
                continuation,
            ),
        )

    async def pause_for_approval(
        self,
        hold: ApprovalContinuation | ApprovalAdvance,
        *,
        request: ReplyRowRequest,
        room_id: str,
        thread_id: str | None,
        payload: Mapping[str, object],
        result: Mapping[str, object] | None = None,
        response_attempt: ResponseAttempt | None = None,
        event_type: str = "m.room.message",
        permanent_failure_reason: str | None = None,
        new_text: str | None = None,
    ) -> ReplyRowEnqueue | None:
        """Create or advance a paused run's owner and pause its reply with the pause row, in one transaction.

        ``None`` means the continuation could not take its sources or advance,
        or the outbox refused the row; a pause the reply's rule refuses (a Stop
        committed meanwhile) comes back unapplied. Either way nothing is written.
        """
        try:
            return await self._backend.write(
                lambda transaction: _pause_for_approval(
                    transaction,
                    self._principal_id,
                    hold,
                    request=request,
                    event_type=event_type,
                    room_id=room_id,
                    thread_id=thread_id,
                    payload=payload,
                    result=result,
                    response_attempt=response_attempt,
                    permanent_failure_reason=permanent_failure_reason,
                    new_text=new_text,
                ),
            )
        except _ReplyRowRefusedError:
            return None
        except _PauseRefusedError as refused:
            return refused.enqueue

    async def approval_continuation_for_source(
        self,
        event_id: str,
    ) -> ApprovalContinuation | None:
        """Return the paused run that owns one original source event."""
        return await self._backend.read(
            lambda transaction: approval_continuations.for_source(
                transaction,
                self._principal_id,
                event_id=event_id,
            ),
        )

    async def approval_continuation(self, approval_id: str) -> ApprovalContinuation | None:
        """Return one principal-owned paused run by its stable identity."""
        return await self._backend.read(
            lambda transaction: approval_continuations.get(
                transaction,
                self._principal_id,
                approval_id=approval_id,
            ),
        )

    async def edited_approval_sources_for_user_stop(
        self,
        *,
        room_id: str,
        response_event_id: str,
        source_event_id: str,
        stop_receipt_order: int,
    ) -> tuple[str, ...]:
        """Resolve edit-owned approvals and finished FINALs within one STOP cutoff."""
        return await self._backend.read(
            lambda transaction: response_attempts.edited_attempt_sources_before_stop(
                transaction,
                self._principal_id,
                room_id=room_id,
                response_event_id=response_event_id,
                source_event_id=source_event_id,
                stop_receipt_order=stop_receipt_order,
            ),
        )

    async def claim_approval_continuation(
        self,
        approval_id: str,
        *,
        runtime_generation: str,
        legacy_show_tool_calls: bool | None = None,
    ) -> ApprovalContinuation | None:
        """Claim one ready paused run for exactly one response lifecycle."""
        return await self._backend.write(
            lambda transaction: approval_continuations.claim(
                transaction,
                self._principal_id,
                approval_id=approval_id,
                runtime_generation=runtime_generation,
                legacy_show_tool_calls=legacy_show_tool_calls,
            ),
        )

    async def claim_approval_resume(
        self,
        approval_id: str,
        *,
        runtime_generation: str,
        claim: rl.ClaimRequest,
        legacy_show_tool_calls: bool | None = None,
    ) -> tuple[ApprovalContinuation | None, replies.AppliedTransition | None]:
        """Claim one ready paused run and its reply's resume span together (PR-1.md §4.3).

        A reply with unresolved durable writes refuses the resume, and the
        continuation stays ready; a continuation whose response no reply
        records own is claimed alone, as on main.
        """
        return await self._backend.write(
            lambda transaction: _claim_approval_resume(
                transaction,
                self._principal_id,
                approval_id=approval_id,
                runtime_generation=runtime_generation,
                claim=claim,
                legacy_show_tool_calls=legacy_show_tool_calls,
            ),
        )

    async def claim_approval_in_place(
        self,
        approval_id: str,
        *,
        runtime_generation: str,
        legacy_show_tool_calls: bool | None,
        reply_id: str,
        span_id: str,
    ) -> tuple[ApprovalContinuation | None, replies.AppliedTransition | None]:
        """Claim a ready continuation for the response waiting on it, and resume its reply in place."""

        def claim(transaction: Transaction) -> tuple[ApprovalContinuation | None, replies.AppliedTransition | None]:
            claimed = approval_continuations.claim(
                transaction,
                self._principal_id,
                approval_id=approval_id,
                runtime_generation=runtime_generation,
                legacy_show_tool_calls=legacy_show_tool_calls,
            )
            if claimed is None:
                return None, None
            return claimed, replies.decide_on_span(
                transaction,
                self._principal_id,
                reply_id=reply_id,
                span_id=span_id,
                decide=lambda reply, span: rl.resumed_in_place(
                    reply,
                    span,
                    approval_id=approval_id,
                    now_ns=time.time_ns(),
                ),
            )

        return await self._backend.write(claim)

    async def advance_approval_continuation(
        self,
        approval_id: str,
        *,
        claimant_generation: int,
        run_id: str,
        session_id: str,
        calls: tuple[ApprovalCall, ...],
        runtime_model_name: str | None = None,
        response_text: str | None = None,
        response_tool_trace: tuple[dict[str, object], ...] | None = None,
        response_presentation_state: dict[str, object] | None = None,
        delegation_storage_bindings: dict[str, dict[str, object]] | None = None,
        cli_call: dict[str, object] | None = None,
        continuation_count: int | None = None,
    ) -> ApprovalContinuation | None:
        """Replace one claimed generation with the next exact Agno pause."""
        advance = ApprovalAdvance(
            approval_id=approval_id,
            claimant_generation=claimant_generation,
            run_id=run_id,
            session_id=session_id,
            calls=calls,
            runtime_model_name=runtime_model_name,
            response_text=response_text,
            response_tool_trace=response_tool_trace,
            response_presentation_state=response_presentation_state,
            delegation_storage_bindings=delegation_storage_bindings,
            cli_call=cli_call,
            continuation_count=continuation_count,
        )
        return await self._backend.write(lambda transaction: advance.apply(transaction, self._principal_id))

    async def activate_approval_continuation(
        self,
        approval_id: str,
        *,
        expected_generation: int,
    ) -> ApprovalContinuation | None:
        """Expose one generation only after every approval card is durable."""
        return await self._backend.write(
            lambda transaction: approval_continuations.activate(
                transaction,
                self._principal_id,
                approval_id=approval_id,
                expected_generation=expected_generation,
            ),
        )

    async def request_approval_failure(
        self,
        approval_id: str,
        reason: str,
        *,
        expected_state: ApprovalContinuationState,
        expected_generation: int = 0,
        expected_runtime_generation: str | None = None,
    ) -> ApprovalContinuation | None:
        """Fence one observed continuation state against any later execution."""
        return await self._backend.write(
            lambda transaction: approval_continuations.request_failure(
                transaction,
                self._principal_id,
                approval_id=approval_id,
                reason=reason,
                expected_state=expected_state,
                expected_generation=expected_generation,
                expected_runtime_generation=expected_runtime_generation,
            ),
        )

    async def release_approval_continuation(self, approval_id: str, *, expected_generation: int) -> bool:
        """Hand an interrupted continuation's still-pending sources back to replay, with its reply."""

        def release(transaction: Transaction) -> bool:
            continuation = approval_continuations.get(transaction, self._principal_id, approval_id=approval_id)
            if not approval_continuations.release(
                transaction,
                self._principal_id,
                approval_id=approval_id,
                expected_generation=expected_generation,
            ):
                return False
            if continuation is not None:
                replies.approval_released(transaction, self._principal_id, continuation)
            return True

        return await self._backend.write(release)

    async def finish_approval_continuation(self, approval_id: str) -> bool:
        """Settle one paused run after its FINAL delivery reaches a terminal outcome, and end its reply."""
        return await self._backend.write(
            lambda transaction: _finish_approval_continuation(transaction, self._principal_id, approval_id),
        )

    async def enqueue_unavailable_approval_notice(
        self,
        *,
        approval_id: str,
        room_id: str,
        thread_id: str | None,
        payload: Mapping[str, object],
    ) -> str | None:
        """Enqueue this membership's physical attempt for one logical notice."""
        return await self._backend.write(
            lambda transaction: approval_continuations.enqueue_unavailable_notice(
                transaction,
                self._principal_id,
                approval_id=approval_id,
                room_id=room_id,
                thread_id=thread_id,
                payload=payload,
            ),
        )

    async def discard_unavailable_approval_continuation(
        self,
        approval_id: str,
        *,
        notice_principal_id: str,
    ) -> bool:
        """Release sources after permanent owner loss and visible card cleanup."""
        return await self._backend.write(
            lambda transaction: approval_continuations.discard_unavailable(
                transaction,
                self._principal_id,
                approval_id=approval_id,
                notice_principal_id=notice_principal_id,
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

    async def adopt_legacy_replies(
        self,
        *,
        entity_name: str,
        presentations: legacy_reply_messages.LegacyPresentations,
        now_ns: int,
    ) -> tuple[replies.AppliedTransition, ...]:
        """Give the replies main left in flight records, once per principal (DESIGN.md §14.5)."""
        return await self._backend.write(
            lambda transaction: legacy_reply_messages.classify(
                transaction,
                self._principal_id,
                entity_name=entity_name,
                presentations=presentations,
                now_ns=now_ns,
            ),
        )

    async def legacy_reply_reads(self) -> tuple[tuple[rl.Reply, rl.Span], ...]:
        """Return main-era replies waiting for what only their Matrix event shows."""
        return await self._backend.read(
            lambda transaction: legacy_reply_messages.pending_reads(transaction, self._principal_id),
        )

    async def finish_legacy_reply_read(
        self,
        reply_id: str,
        read: rl.LegacyRead,
        *,
        now_ns: int,
    ) -> replies.AppliedTransition | None:
        """Record what a main-era reply's event showed, releasing whatever waited for it."""
        return await self._backend.write(
            lambda transaction: legacy_reply_messages.read_done(
                transaction,
                self._principal_id,
                reply_id=reply_id,
                read=read,
                now_ns=now_ns,
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
    response_attempt: ResponseAttempt | None,
    edits_event_id: str | None,
    settle_source_event_ids: tuple[str, ...],
    permanent_failure_reason: str | None,
    edit_target_pending: bool = False,
    reply_id: str | None = None,
    span_id: str | None = None,
    reply_sequence: int | None = None,
    reply_row: Mapping[str, object] | None = None,
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

    Settling the sources here rather than after the commit is what makes the
    handoff one event. A refusal settles nothing, because nothing durable
    would owe the answer afterwards; anything else settles every source the
    delivery accounts for, atomically with the row that now answers them.
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
    transaction_id = outbox.enqueue(
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
    if transaction_id is None:
        return None
    if response_attempt is not None:
        if response_attempt.sources.pending_event_ids[0] != delivery_id:
            message = "Conflicting response attempt identity: driving event"
            raise ValueError(message)
        frozen = (
            transaction.fetchone(
                """SELECT delivery.edits_event_id FROM matrix_delivery_outbox AS delivery
                JOIN response_attempts AS attempt
                  ON attempt.principal_id = delivery.principal_id AND attempt.driving_event_id = delivery.delivery_id
                WHERE delivery.principal_id = ? AND delivery.delivery_id = ? AND delivery.stage = ?""",
                (principal_id, delivery_id, stage.value),
            )
            if attempted
            else None
        )
        if attempted and (frozen is None or frozen["edits_event_id"] != edits_event_id):
            message = "Cannot replace an attempted delivery identity"
            raise ValueError(message)
        response_attempts.register_response_attempt(
            transaction,
            principal_id,
            attempt=response_attempt,
            room_id=room_id,
            membership_epoch=membership_epoch,
            response_event_id=edits_event_id,
        )
    journal.settle_many(transaction, principal_id, settle_source_event_ids)
    return transaction_id


def _claim_approval_resume(
    transaction: Transaction,
    principal_id: str,
    *,
    approval_id: str,
    runtime_generation: str,
    claim: rl.ClaimRequest,
    legacy_show_tool_calls: bool | None,
) -> tuple[ApprovalContinuation | None, replies.AppliedTransition | None]:
    """Claim the continuation and the resume span of its paused reply in one transaction."""
    current = approval_continuations.get(transaction, principal_id, approval_id=approval_id)
    if current is None or current.state != "ready":
        return None, None
    applied = None
    if reply_messages.for_event(transaction, principal_id, current.response_event_id) is not None:
        applied = replies.claim(
            transaction,
            principal_id,
            replace(claim, approval_id=current.approval_id, approval_generation=current.generation),
            replies.ClaimLookup(existing_event_id=current.response_event_id),
        )
        if applied.transition.claimed is None:
            # The reply's earlier writes are unresolved; their resolution wakes the sources.
            return None, applied
    claimed = approval_continuations.claim(
        transaction,
        principal_id,
        approval_id=approval_id,
        runtime_generation=runtime_generation,
        legacy_show_tool_calls=legacy_show_tool_calls,
    )
    assert claimed is not None, "a ready continuation is claimed in the transaction that read it"
    return claimed, applied


def _finish_approval_continuation(transaction: Transaction, principal_id: str, approval_id: str) -> bool:
    """Finish a continuation and apply the outcome to the reply it paused, in one transaction."""
    continuation = approval_continuations.get(transaction, principal_id, approval_id=approval_id)
    if not approval_continuations.finish(transaction, principal_id, approval_id=approval_id):
        return False
    if continuation is not None:
        replies.approval_finished(transaction, principal_id, continuation)
    return True


class _ReplyRowRefusedError(Exception):
    """The outbox refused a reply row the lifecycle had already decided; roll both back."""


class _PauseRefusedError(Exception):
    """The reply's rule refused a pause; the continuation created with it rolls back."""

    def __init__(self, enqueue: ReplyRowEnqueue) -> None:
        super().__init__("The reply's rule refused the pause")
        self.enqueue = enqueue


def _pause_for_approval(
    transaction: Transaction,
    principal_id: str,
    hold: ApprovalContinuation | ApprovalAdvance,
    *,
    request: ReplyRowRequest,
    event_type: str,
    room_id: str,
    thread_id: str | None,
    payload: Mapping[str, object],
    result: Mapping[str, object] | None,
    response_attempt: ResponseAttempt | None,
    permanent_failure_reason: str | None,
    new_text: str | None,
) -> ReplyRowEnqueue:
    """Create or advance the continuation, then pause its reply, so neither exists without the other (DESIGN.md §6.4)."""
    held = (
        hold.apply(transaction, principal_id)
        if isinstance(hold, ApprovalAdvance)
        else approval_continuations.create(transaction, principal_id, hold)
    )
    if held is None:
        raise _ReplyRowRefusedError
    enqueued = _enqueue_reply_row(
        transaction,
        principal_id,
        request=request,
        event_type=event_type,
        room_id=room_id,
        thread_id=thread_id,
        payload=payload,
        result=result,
        response_attempt=response_attempt,
        permanent_failure_reason=permanent_failure_reason,
        new_text=new_text,
    )
    if not enqueued.transition.applied:
        raise _PauseRefusedError(enqueued)
    return enqueued


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
                applied=replies.AppliedTransition(
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
        transition = request.decide(reply, span)
    return reply, span, transition


def _enqueue_reply_row(
    transaction: Transaction,
    principal_id: str,
    *,
    request: ReplyRowRequest,
    event_type: str,
    room_id: str,
    thread_id: str | None,
    payload: Mapping[str, object],
    result: Mapping[str, object] | None,
    response_attempt: ResponseAttempt | None,
    permanent_failure_reason: str | None,
    new_text: str | None = None,
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
            applied=replies.apply(transaction, principal_id, transition),
            settled_event_ids=replies.settled_event_ids(transaction, principal_id, transition),
        )
    row = transition.row
    stage = DeliveryStage(row.stage.value)
    edits_event_id = None if stage is DeliveryStage.INITIAL else reply.event_id
    edit_target_pending = (
        edits_event_id is None
        and stage is not DeliveryStage.INITIAL
        and (stage is DeliveryStage.EDIT or replies.has_unresolved_rows(transaction, principal_id, reply.reply_id))
    )
    row_fields = {
        "reply_id": reply.reply_id,
        "span_id": span.span_id,
        "reply_sequence": row.sequence,
        "reply_row": replies.row_facts(placeholder_only=request.placeholder_only, new_text=new_text),
    }
    if stage is DeliveryStage.EDIT:
        delivery_id = replies.edit_delivery_id(span.delivery_id, row.sequence)
        if not reads.claim_membership_epoch(
            transaction,
            principal_id,
            room_id=reply.room_id,
            expected_membership_epoch=reply.membership_epoch,
        ):
            raise _ReplyRowRefusedError
        transaction_id = outbox.enqueue(
            transaction,
            principal_id,
            delivery_id=delivery_id,
            stage=stage,
            event_type=event_type,
            room_id=reply.room_id,
            membership_epoch=reply.membership_epoch,
            thread_id=thread_id,
            payload=payload,
            result=result,
            edits_event_id=edits_event_id,
            edit_target_pending=edit_target_pending,
            permanent_failure_reason=permanent_failure_reason,
            **row_fields,
        )
    else:
        delivery_id = span.delivery_id
        transaction_id = _enqueue_matrix_delivery(
            transaction,
            principal_id,
            delivery_id=delivery_id,
            stage=stage,
            event_type=event_type,
            room_id=room_id,
            thread_id=thread_id,
            payload=payload,
            result=result,
            response_attempt=response_attempt if stage is DeliveryStage.FINAL else None,
            edits_event_id=edits_event_id,
            settle_source_event_ids=(),
            permanent_failure_reason=permanent_failure_reason,
            edit_target_pending=edit_target_pending,
            **row_fields,
        )
    if transaction_id is None:
        raise _ReplyRowRefusedError
    applied = replies.apply(transaction, principal_id, transition)
    if permanent_failure_reason is not None:
        # A payload refused before any send fails its row now, as a refusal from Matrix would.
        refused = outbox.load(transaction, principal_id, delivery_id=delivery_id, stage=stage)
        assert refused is not None
        failed = replies.fail_row(transaction, principal_id, refused, reason=permanent_failure_reason)
        if failed is not None:
            applied = replies.AppliedTransition(
                transition=transition,
                post_commit=(*applied.post_commit, *failed.post_commit),
            )
    return ReplyRowEnqueue(
        applied=applied,
        delivery_id=delivery_id,
        stage=row.stage,
        transaction_id=transaction_id,
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
    stored row keeps its payload, as main's re-enqueue of an attempted row does.
    """
    if stage not in {rl.WriteStage.INITIAL, rl.WriteStage.FINAL}:
        return None
    stored = outbox.load(transaction, principal_id, delivery_id=span.delivery_id, stage=DeliveryStage(stage.value))
    if stored is None or stored.reply_id != reply.reply_id:
        return None
    if stage is rl.WriteStage.FINAL and stored.span_id != span.span_id:
        return None
    return ReplyRowEnqueue(
        applied=replies.AppliedTransition(
            transition=rl.Transition(outcome=rl.Outcome.DUPLICATE, reply=reply),
            post_commit=(),
        ),
        delivery_id=stored.delivery_id,
        stage=stage,
        transaction_id=stored.transaction_id,
        sequence=stored.reply_sequence,
    )


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
        attempted_policy_rank=attempted_policy_rank,
    )


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
        project(
            transaction,
            principal_id,
            event,
            receipt_order=0,
            membership_epoch=expected_membership_epoch,
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

    async def approval_continuations_for_entities(
        self,
        entity_names: set[str],
        *,
        limit: int = _DEFAULT_APPROVAL_CONTINUATION_OWNER_LIMIT,
        after: tuple[str, str] | None = None,
    ) -> tuple[tuple[str, ApprovalContinuation], ...]:
        """Return one bounded page of owners for unavailable entities."""
        return await self.backend.read(
            lambda transaction: approval_continuations.for_entities(
                transaction,
                entity_names,
                limit=limit,
                after=after,
            ),
        )

    async def approval_continuations(
        self,
        *,
        limit: int = _DEFAULT_APPROVAL_CONTINUATION_OWNER_LIMIT,
        after: tuple[str, str] | None = None,
    ) -> tuple[tuple[str, ApprovalContinuation], ...]:
        """Return one bounded page with its journal principals."""
        return await self.backend.read(
            lambda transaction: approval_continuations.all_owners(
                transaction,
                limit=limit,
                after=after,
            ),
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
        also: Callable[[Transaction], object] | None = None,
    ) -> str | None:
        """Store a record and return its committed state, or reject a changed owner.

        ``also`` runs in the same transaction when the write takes effect, for
        state that must commit with the record or not at all.
        """

        def write(transaction: Transaction) -> str | None:
            committed = turn_records.write_record(
                transaction,
                self._agent_name,
                index_event_ids=index_event_ids,
                anchor_event_id=anchor_event_id,
                record_json=record_json,
            )
            if committed is not None and also is not None:
                also(transaction)
            return committed

        return await self._backend.write(write)

    async def adopt_missing(
        self,
        *,
        index_event_ids: Sequence[str],
        anchor_event_id: str,
        record_json: str,
    ) -> int:
        """Fill only the indexes with no record yet, for migration. Returns how many."""
        return await self._backend.write(
            lambda transaction: legacy_turn_records.adopt_missing(
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

    async def load(self, event_id: str) -> TurnRecord | None:
        """Read one exact record without depending on a runtime ledger cache."""
        return await self._backend.read(
            lambda transaction: turn_records.load_record(transaction, self._agent_name, event_id),
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
