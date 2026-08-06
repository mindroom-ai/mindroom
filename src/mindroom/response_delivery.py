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

    async def recover(self) -> int:
        """Resend every delivery whose Matrix outcome is unknown.

        Run at startup. A delivery the homeserver already accepted is resent
        under the same transaction ID and collapses back to the same event, so
        recovery cannot duplicate a visible message.

        Every unacknowledged delivery is walked, not one page of them. The
        store reads in bounded batches, but stopping after the first would
        report success while leaving answers the user is waiting for unsent.
        """
        recovered = 0
        failed: set[tuple[str, str]] = set()
        while True:
            batch = await self.store.unacknowledged_deliveries()
            remaining = [delivery for delivery in batch if (delivery.turn_id, delivery.stage.value) not in failed]
            if not remaining:
                return recovered
            for delivery in remaining:
                try:
                    await self.flush(turn_id=delivery.turn_id, stage=delivery.stage)
                except Exception:
                    logger.exception(
                        "response_delivery_recovery_failed",
                        turn_id=delivery.turn_id,
                        stage=delivery.stage.value,
                        room_id=delivery.room_id,
                    )
                    # Acknowledgement is what removes a delivery from this
                    # query, so a failure that is not remembered would be
                    # retried forever instead of letting the rest through.
                    failed.add((delivery.turn_id, delivery.stage.value))
                    continue
                recovered += 1


__all__ = ["DeliveryStage", "ResponseDelivery", "SendDelivery"]
