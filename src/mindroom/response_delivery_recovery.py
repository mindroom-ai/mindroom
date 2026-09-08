"""Exact source decisions made while the normal visible-delivery owner is held."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from mindroom.event_journal import DeliveryStage
from mindroom.handled_turns import TurnRecord
from mindroom.matrix.journal_ingress import replayable_redaction_target
from mindroom.message_target import MessageTarget

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.event_journal import MatrixDelivery, PrincipalStore
    from mindroom.event_journal.models import ResponseRecoveryState
    from mindroom.matrix_delivery import MatrixDeliveryWorker
    from mindroom.turn_store import TurnStore


@dataclass(frozen=True)
class ResponseDeliveryRecovery:
    """Join source truth with outbox identity without creating another recovery ledger."""

    principal: PrincipalStore
    turn_store: Callable[[], TurnStore]
    redact: Callable[..., Awaitable[bool]]

    async def state(self, delivery: MatrixDelivery) -> ResponseRecoveryState:
        """Read exact source and FINAL facts, including ACK-before-record-adoption."""
        record = self.turn_store().get_turn_record(delivery.delivery_id)
        if record is None:
            record = TurnRecord.create(
                [delivery.delivery_id],
                completed=False,
                conversation_target=MessageTarget.resolve(delivery.room_id, delivery.thread_id, delivery.delivery_id),
            )
        elif record.conversation_target is None:
            record = replace(
                record,
                conversation_target=MessageTarget.resolve(
                    delivery.room_id,
                    delivery.thread_id,
                    delivery.delivery_id,
                ),
            )
        return await self.principal.response_recovery_state(
            turn_record=record,
            agent_name=self.turn_store().deps.agent_name,
            redaction_target=replayable_redaction_target,
        )

    @staticmethod
    def deleted(state: ResponseRecoveryState) -> bool:
        """Recognize exact admitted tombstones before their semantic callback runs."""
        return bool(state.source_tombstones) and all(state.source_tombstones)

    @staticmethod
    def _final_owned(state: ResponseRecoveryState) -> bool:
        """A frozen owed FINAL and an acknowledged FINAL both retain their target."""
        final = state.final_delivery
        return (
            final is not None
            and (final.acknowledged_event_id is not None or not (final.retired or final.permanently_failed))
        ) or any(
            record is not None
            and record.completed
            and not (record.user_stop_receipt_order is not None and record.response_event_id is None)
            for record in state.turn_records
        )

    async def permits_continuation(self, delivery: MatrixDelivery) -> bool:
        """Only genuinely orphaned work may acquire synthetic startup continuation."""
        state = await self.state(delivery)
        return not (
            any(state.source_tombstones)
            or self._final_owned(state)
            or any(state.pending_sources)
            or state.sources_settled_by_departure
            or any(
                record is not None
                and (
                    record.user_stop_receipt_order is not None
                    or any(self.turn_store().has_live_turn_claim(event_id) for event_id in record.indexed_event_ids)
                )
                for record in state.turn_records
            )
        )

    async def permits_supersession(self, delivery: MatrixDelivery) -> bool:
        """Only terminal or revoked INITIAL work may lose its canonical replay."""
        state = await self.state(delivery)
        return (
            self.deleted(state)
            or self._final_owned(state)
            or state.sources_settled_by_departure
            or any(record is not None and record.user_stop_receipt_order is not None for record in state.turn_records)
        )

    async def cleanup(self, worker: MatrixDeliveryWorker, turn_id: str) -> bool:
        """Remove only obsolete INITIAL, under worker's already-held delivery lock.

        ACK identity remains durable until redaction and detachment both finish.
        Thus a crash after Matrix redaction simply repeats that exact idempotent
        redaction. Retirement is last and fences subsequent INITIAL/FINAL sends.
        """
        initial = await self.principal.load_matrix_delivery(delivery_id=turn_id, stage=DeliveryStage.INITIAL)
        if initial is None or initial.retired:
            return False
        state = await self.state(initial)
        if not self.deleted(state) or self._final_owned(state):
            return False
        for source_id, deleted in zip(
            (self.turn_store().get_turn_record(turn_id) or TurnRecord.create([turn_id])).source_event_ids,
            state.source_tombstones,
            strict=True,
        ):
            if deleted:
                await self.turn_store().mark_source_redacted(source_id)
        event_id = initial.acknowledged_event_id
        if event_id is None and initial.attempted:
            event_id = await worker._resolve_delivered_event(initial)
            if event_id is None:
                if not worker._transaction_id_still_deduplicates(initial):
                    msg = "Deleted INITIAL still owes exact send reconciliation"
                    raise RuntimeError(msg)
                # Same-device resend resolves even a lost ACK to the same event.
                event_id = await worker.send(initial)
            await self.principal.acknowledge_matrix_delivery(
                delivery_id=turn_id,
                stage=DeliveryStage.INITIAL,
                event_id=event_id,
                delivered_projections=(),
            )
        if event_id is not None and not await self.redact(
            room_id=initial.room_id,
            event_id=event_id,
            reason="Source message deleted",
        ):
            msg = "Deleted INITIAL still owes Matrix redaction"
            raise RuntimeError(msg)
        await self.turn_store().detach_deleted_response(turn_id, event_id)
        await self.principal.retire_deleted_initial(delivery_id=turn_id)
        return True
