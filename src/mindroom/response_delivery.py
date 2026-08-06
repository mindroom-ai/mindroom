"""Sending what the outbox says to send.

Ordering here is the whole design. The model result becomes durable, then the
delivery is enqueued, then it is claimed, and only then does the network call
happen. Every crash boundary between those steps resolves to one terminal turn
and at most one visible message.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.event_journal import DeliveryStage
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from mindroom.event_journal import OutboxDelivery, OutboxView

logger = get_logger(__name__)

type SendDelivery = Callable[[OutboxDelivery], Awaitable[str]]


@dataclass(frozen=True, slots=True)
class RecoveryOutcome:
    """What one recovery pass sent, and what it still owes."""

    recovered: int
    failed: int

    @property
    def complete(self) -> bool:
        """Return whether nothing is left for a later pass to retry."""
        return self.failed == 0


@dataclass(frozen=True, slots=True)
class ResponseDelivery:
    """Claim-before-send delivery against one principal's outbox."""

    store: OutboxView
    send: SendDelivery

    async def deliver(
        self,
        *,
        turn_id: str,
        stage: DeliveryStage,
        room_id: str,
        thread_id: str | None,
        payload: Mapping[str, object],
        edits_event_id: str | None = None,
    ) -> str:
        """Enqueue, claim, send, and acknowledge one delivery.

        Enqueueing an already-attempted delivery leaves the stored payload
        alone, so a turn that ran twice still sends what was sent the first
        time. Content that could never become visible is worse than content
        that is slightly stale: the homeserver would silently drop it as a
        duplicate transaction and the durable result and the room would
        disagree forever.
        """
        await self.store.enqueue_delivery(
            turn_id=turn_id,
            stage=stage,
            room_id=room_id,
            thread_id=thread_id,
            payload=payload,
            edits_event_id=edits_event_id,
        )
        return await self.flush(turn_id=turn_id, stage=stage)

    async def flush(self, *, turn_id: str, stage: DeliveryStage) -> str:
        """Send one enqueued delivery, or resend the identical one."""
        claimed = await self.store.claim_delivery(turn_id=turn_id, stage=stage)
        if claimed is None:
            msg = f"No delivery enqueued for turn {turn_id!r} stage {stage.value!r}"
            raise RuntimeError(msg)
        if claimed.acknowledged_event_id is not None:
            return claimed.acknowledged_event_id
        event_id = await self.send(claimed)
        await self.store.acknowledge_delivery(
            turn_id=turn_id,
            stage=stage,
            event_id=event_id,
        )
        return event_id

    async def _superseded_placeholder(self, delivery: OutboxDelivery) -> bool:
        """Return whether this delivery is a placeholder the answer overtook.

        A placeholder send whose outcome was never confirmed leaves a row
        behind. If the turn went on to write an answer at all, resending that
        placeholder would put "Thinking..." into the room next to the answer
        it was supposed to precede -- and in this same pass, before it, since
        the older row is recovered first.

        The FINAL row existing is the proof, not the FINAL row being
        acknowledged. An unacknowledged FINAL is still a turn that got past
        the placeholder, and recovery sends it in the same pass. An
        edit-shaped FINAL cannot be stranded by this: its target event ID only
        exists because the placeholder send returned one, so the placeholder
        really is in the room whether or not the row records it.

        The row is left unacknowledged rather than deleted, because an
        attempted row is the only record that something may already be in the
        room under its transaction ID.
        """
        if delivery.stage is not DeliveryStage.INITIAL:
            return False
        return await self.store.load_delivery(turn_id=delivery.turn_id, stage=DeliveryStage.FINAL) is not None

    async def recover(self) -> RecoveryOutcome:
        """Resend every delivery whose Matrix outcome is unknown.

        A delivery the homeserver already accepted is resent under the same
        transaction ID and collapses back to the same event, so recovery
        cannot duplicate a visible message.

        Every unacknowledged delivery is walked, not one page of them. The
        store reads in bounded batches, but stopping after the first would
        report success while leaving answers the user is waiting for unsent.

        The failure count is what the caller schedules on. A pass that could
        not send is not a pass that finished, and the rows it left behind are
        answers a user is waiting for.
        """
        recovered = 0
        failed = 0
        # A failure leaves the row unacknowledged, so it stays in the query's
        # window. Filtering it in memory is not enough: a whole page of
        # failures would be re-read forever and everything behind it starved.
        # The scan therefore advances past every row it has visited.
        cursor: tuple[int, str, str] | None = None
        while True:
            batch = await self.store.unacknowledged_deliveries(after=cursor)
            if not batch:
                return RecoveryOutcome(recovered=recovered, failed=failed)
            cursor = (batch[-1].created_at_ns, batch[-1].turn_id, batch[-1].stage.value)
            for delivery in batch:
                if await self._superseded_placeholder(delivery):
                    continue
                try:
                    await self.flush(turn_id=delivery.turn_id, stage=delivery.stage)
                except Exception:
                    logger.exception(
                        "response_delivery_recovery_failed",
                        turn_id=delivery.turn_id,
                        stage=delivery.stage.value,
                        room_id=delivery.room_id,
                    )
                    # Left unacknowledged deliberately: a later recovery pass
                    # picks it up again, while this pass moves on to the rest.
                    failed += 1
                    continue
                recovered += 1


__all__ = ["DeliveryStage", "RecoveryOutcome", "ResponseDelivery", "SendDelivery"]
