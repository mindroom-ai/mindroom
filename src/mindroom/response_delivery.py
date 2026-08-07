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
    from mindroom.event_journal.models import TerminalTurnWrite

logger = get_logger(__name__)

type SendDelivery = Callable[[OutboxDelivery], Awaitable[str]]

# Finding the Matrix event a previous attempt already produced, when the frozen
# transaction ID can no longer prove there wasn't one. Returns the event ID if
# the answer is already in the room, or ``None`` if it never arrived.
type ResolveDelivered = Callable[[OutboxDelivery], Awaitable[str | None]]

# The terminal turn record one delivered answer completes, given ``(turn_id,
# response_event_id)``. Returns ``None`` when there is nothing to write --
# no record for the turn, or one that already knows its response event.
type _TerminalTurnFor = Callable[[str, str], "TerminalTurnWrite | None"]

# Told after an acknowledgement this caller actually bound, so an in-memory
# view of the same record can catch up without a second write. A caller that
# lost the row is never told, because it committed nothing to catch up with.
type _TerminalTurnCommitted = Callable[[str, str], None]


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
    # The device this process is logged in as, recorded on every claim. A
    # Matrix transaction ID is idempotent within one device and meaningless
    # across a change of one, so the row has to remember which device's
    # namespace its frozen ID belongs to.
    sending_device_id: str | None = None
    # How to find out whether an answer this process cannot vouch for is
    # already in the room. Only consulted when the transaction ID has stopped
    # being proof, which is the one case where resending blind duplicates.
    resolve_delivered: ResolveDelivered | None = None
    # Where a turn stops being the journal's work and becomes the outbox's.
    # Deliberately unused for `INITIAL`: a placeholder is not an answer, and
    # handing the turn over on one would leave a crash before the model
    # finished with nothing pending to replay and "Thinking..." in the room
    # forever.
    handoff: TurnHandoff | None = None
    # The turn record this delivery completes, asked for only once the event ID
    # exists and written in the acknowledgement's own transaction. The
    # acknowledgement is the proof that an answer is visible and what its event
    # ID is; the record is the thing that needs to know it. Committing them
    # apart leaves a delivered answer whose record cannot be edited.
    terminal_turn_for: _TerminalTurnFor | None = None
    terminal_turn_committed: _TerminalTurnCommitted | None = None

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

    def _transaction_id_still_deduplicates(self, claimed: OutboxDelivery) -> bool:
        """Return whether resending this row can only collapse onto its own event.

        True in the ordinary case, and the reason recovery can resend without
        thinking: the homeserver remembers the transaction ID and returns the
        event the first attempt produced.

        It stops being true when the sending device changes, because a Matrix
        transaction ID is scoped to the device that used it. A row attempted by
        a device this process is no longer logged in as carries an ID the
        homeserver has never seen from *this* device, so the resend is accepted
        as a new message and the room gets the answer twice.

        What separates the safe rows from the rest is whether anyone has
        *attempted* this one, not whether a device is recorded. An unattempted
        row has no device for the uninteresting reason that nothing has sent
        it, and there is no earlier event for a resend to collide with, so it
        is exempt and no first delivery pays for this guard.

        Once a row is attempted, an unrecorded device is not "unchanged" -- it
        is a device nobody can name, which is the case the resend cannot be
        proven safe in. That covers a row written before the column existed,
        and a process that cannot name the device it is about to send from.
        An earlier version of this returned True for both, reasoning that
        reconciling would put a backward scan in front of every ordinary
        recovery. That reasoning was wrong twice over: ordinary recovery
        records its device before sending, so those rows compare equal and
        scan nothing, and the rows that do reach here are the rare ones --
        attempted, unacknowledged, and older than the column or sent by a
        process mid-login. Reconciling them costs one scan and cannot lose an
        answer, because a lookup that finds nothing still sends and a lookup
        that cannot run at all sends too.

        An edit is exempt. A second ``m.replace`` carrying identical content
        resolves to the same visible message as the first, so the duplicate a
        stale transaction ID admits is not one anybody can see.
        """
        if claimed.edits_event_id is not None:
            return True
        if not claimed.attempted:
            return True
        if claimed.sending_device_id is None or self.sending_device_id is None:
            return False
        return claimed.sending_device_id == self.sending_device_id

    async def flush(self, *, turn_id: str, stage: DeliveryStage) -> str | None:
        """Send one enqueued delivery, or resend the identical one.

        Nothing means the row is gone, which only a membership fence does, and
        only to a delivery nothing outside this process has seen.

        Between the claim and the send sits the one question the outbox cannot
        answer from its own state: is the frozen transaction ID still proof
        that a resend cannot duplicate? When it is not -- when the device that
        attempted this row is not the device about to retry it -- the room is
        asked directly, and an answer already there is adopted instead of sent
        again.
        """
        claimed = await self.store.claim_delivery(turn_id=turn_id, stage=stage)
        if claimed is None:
            logger.info("response_delivery_row_withdrawn", turn_id=turn_id, stage=stage.value)
            return None
        if claimed.acknowledged_event_id is not None:
            return claimed.acknowledged_event_id
        if not self._transaction_id_still_deduplicates(claimed):
            already_delivered = await self._delivered_before_device_changed(claimed)
            if already_delivered is not None:
                return await self._acknowledge(turn_id, stage, already_delivered)
        # Only now, with a send actually about to happen. Writing this at claim
        # time instead loses the fact that a lookup is still owed: a room scan
        # that raises would leave the row unacknowledged but stamped with this
        # device, and the next pass would see its own marker, skip the lookup
        # and post the answer twice.
        await self.store.record_sending_device(turn_id=turn_id, stage=stage, device_id=self.sending_device_id)
        event_id = await self.send(claimed)
        return await self._acknowledge(turn_id, stage, event_id)

    async def _acknowledge(self, turn_id: str, stage: DeliveryStage, event_id: str) -> str:
        """Bind the row and let an in-memory view of the record catch up.

        The record commits inside the acknowledgement, so nothing else writes
        it -- which means nothing else tells the synchronous ledger either.
        Recovery is where that matters: it acknowledges and returns without any
        ordinary terminal write following, so without this the database knows
        the answer's event and memory does not, and an edit arriving before the
        next restart is dropped for having no response to edit.

        Only on a bound acknowledgement. A loser committed nothing, so it has
        nothing to publish and must not overwrite the winner's record.

        Returns the event the row actually names, which is not always the one
        just sent. A caller that lost the race must report the winner's event
        upward, because everything downstream records what delivery returns --
        and a loser reporting its own send is how the outbox and the terminal
        record end up naming different events even though the acknowledgement
        itself was guarded.
        """
        settled = await self.store.acknowledge_delivery(
            turn_id=turn_id,
            stage=stage,
            event_id=event_id,
            terminal_turn=self._terminal_turn(turn_id, stage, event_id),
        )
        if settled is None:
            return event_id
        if settled == event_id and stage is DeliveryStage.FINAL and self.terminal_turn_committed is not None:
            self.terminal_turn_committed(turn_id, event_id)
        return settled

    def _terminal_turn(self, turn_id: str, stage: DeliveryStage, event_id: str) -> TerminalTurnWrite | None:
        """Return the turn record this acknowledgement should also commit.

        Only for ``FINAL``. An ``INITIAL`` row is a placeholder, and binding a
        turn's terminal record to one would call a turn finished while the
        model is still running.
        """
        if stage is not DeliveryStage.FINAL or self.terminal_turn_for is None:
            return None
        return self.terminal_turn_for(turn_id, event_id)

    async def _delivered_before_device_changed(self, claimed: OutboxDelivery) -> str | None:
        """Return the event a previous device's attempt left in the room, if any.

        Failing to find one is not the same as there not being one, and the
        difference decides between a duplicate and a lost answer. Both are bad;
        a duplicate is the one the user can act on, so a lookup that cannot run
        at all sends anyway.

        A lookup that runs and *raises* is different: it propagates, the row
        stays unacknowledged, and -- because the device marker has not moved --
        the next pass asks the room again. That costs a repeated scan while the
        homeserver is unreachable, which is the right price for not guessing.
        """
        if self.resolve_delivered is None:
            logger.warning(
                "response_delivery_resend_unverified",
                turn_id=claimed.turn_id,
                stage=claimed.stage.value,
                room_id=claimed.room_id,
                claimed_by_device=claimed.sending_device_id,
                sending_device=self.sending_device_id,
            )
            return None
        already_delivered = await self.resolve_delivered(claimed)
        logger.info(
            "response_delivery_device_changed",
            turn_id=claimed.turn_id,
            stage=claimed.stage.value,
            room_id=claimed.room_id,
            claimed_by_device=claimed.sending_device_id,
            sending_device=self.sending_device_id,
            adopted_event_id=already_delivered,
        )
        return already_delivered

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
        cannot duplicate a visible message -- as long as the device that made
        the first attempt is the one retrying. When it is not, ``flush`` asks
        the room before it sends; see ``_transaction_id_still_deduplicates``.

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


__all__ = [
    "DeliveryStage",
    "RecoveryOutcome",
    "ResolveDelivered",
    "ResponseDelivery",
    "SendDelivery",
    "TurnHandoff",
]
