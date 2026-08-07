"""Sending what the outbox says to send.

Ordering here is the whole design. The model result becomes durable, then the
delivery is enqueued, then it is claimed, and only then does the network call
happen. Every crash boundary between those steps resolves to one terminal turn
and at most one visible message.

The first of those steps is also where the turn changes hands, and that is one
commit rather than two. Recording the answer and settling the journal sources
it answers happen in a single write, so no crash can find the journal and the
outbox both owning the same turn.
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
class TurnHandoff:
    """Which journal sources one FINAL answer discharges, and who to tell.

    The two halves are split by the commit, not by preference. Resolving the
    sources has to happen before the write, because the settlement travels
    inside it; telling the in-process worker has to happen after it, because a
    transaction that rolled back handed nothing over and the worker would
    otherwise re-dispatch a turn it still owns.
    """

    # The turn is keyed on its anchor event, but a coalesced batch answers
    # several sources at once, and every one of them is discharged together.
    sources_for_turn: Callable[[str], tuple[str, ...]]
    # In-memory only: the pending-event worker no longer holds these.
    released: Callable[[tuple[str, ...]], None]


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
    # Where a turn stops being the journal's work and becomes the outbox's.
    # Deliberately unused for `INITIAL`: a placeholder is not an answer, and
    # handing the turn over on one would leave a crash before the model
    # finished with nothing pending to replay and "Thinking..." in the room
    # forever.
    handoff: TurnHandoff | None = None

    async def deliver(
        self,
        *,
        turn_id: str,
        stage: DeliveryStage,
        room_id: str,
        thread_id: str | None,
        payload: Mapping[str, object],
        edits_event_id: str | None = None,
    ) -> str | None:
        """Enqueue, claim, send, and acknowledge one delivery.

        Enqueueing an already-attempted delivery leaves the stored payload
        alone, so a turn that ran twice still sends what was sent the first
        time. Content that could never become visible is worse than content
        that is slightly stale: the homeserver would silently drop it as a
        duplicate transaction and the durable result and the room would
        disagree forever.

        Nothing means the answer is not this membership's to give: either the
        store refused the intent, or the fence deleted the row between
        recording it and claiming it. Both are the same fact arriving through
        different orderings, and neither is a failure to report.

        A refusal is the one outcome that must leave the turn where it was.
        Nothing durable owes the answer afterwards -- there is no row -- so
        handing the turn over would leave no owner at all, which is the silent
        loss this ordering exists to prevent. A row withdrawn after it was
        recorded is different: the intent existed, and the fence decided
        against it.

        The handoff rides inside the enqueue rather than following it. Both
        halves of "the outbox owes this answer, the journal no longer does"
        commit together, so the send below is the only step a crash can leave
        half done -- and resending a frozen row is what recovery is for.
        """
        handoff = self.handoff if stage is DeliveryStage.FINAL else None
        handed_over = handoff.sources_for_turn(turn_id) if handoff is not None else ()
        transaction_id = await self.store.enqueue_delivery(
            turn_id=turn_id,
            stage=stage,
            room_id=room_id,
            thread_id=thread_id,
            payload=payload,
            edits_event_id=edits_event_id,
            settle_source_event_ids=handed_over,
        )
        if transaction_id is None:
            logger.info("response_delivery_refused_for_ended_membership", turn_id=turn_id, stage=stage.value)
            return None
        if handoff is not None:
            handoff.released(handed_over)
        return await self.flush(turn_id=turn_id, stage=stage)

    async def flush(self, *, turn_id: str, stage: DeliveryStage) -> str | None:
        """Send one enqueued delivery, or resend the identical one.

        Nothing means the row is gone, which only a membership fence does, and
        only to a delivery nothing outside this process has seen.
        """
        claimed = await self.store.claim_delivery(turn_id=turn_id, stage=stage)
        if claimed is None:
            logger.info("response_delivery_row_withdrawn", turn_id=turn_id, stage=stage.value)
            return None
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
                    sent = await self.flush(turn_id=delivery.turn_id, stage=delivery.stage)
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
                if sent is None:
                    # The row went away between being listed and being
                    # claimed, which only a membership fence does. Nothing is
                    # owed and nothing failed.
                    continue
                recovered += 1


__all__ = ["DeliveryStage", "RecoveryOutcome", "ResponseDelivery", "SendDelivery", "TurnHandoff"]
