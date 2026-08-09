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

import asyncio
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING
from weakref import WeakValueDictionary

from mindroom.background_tasks import run_coroutine_until_complete
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

# Told after an acknowledgement this caller actually bound, so the record the
# transaction committed can be re-asserted through whatever ordering the ledger
# uses for every other write. A caller that lost the row is never told, because
# it committed nothing to settle.
type _TerminalTurnCommitted = Callable[[str, str], Awaitable[None]]


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
class _FlushOutcome:
    """One locked send result and the callback it leaves for after unlock."""

    event_id: str | None
    publish_committed_terminal: bool = False
    retry_required: bool = False
    propagate_cancellation: asyncio.CancelledError | None = field(default=None, repr=False, compare=False)


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
    turn_locks: WeakValueDictionary[str, asyncio.Lock] = field(
        default_factory=WeakValueDictionary,
        repr=False,
        compare=False,
    )

    def _turn_lock(self, turn_id: str) -> asyncio.Lock:
        """Return the lock ordering visible delivery for one turn."""
        lock = self.turn_locks.get(turn_id)
        if lock is None:
            lock = asyncio.Lock()
            self.turn_locks[turn_id] = lock
        return lock

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

        Nothing means the delivery must not become visible: either the store
        refused the intent, the fence deleted the row between recording it and
        claiming it, or a FINAL already superseded an INITIAL. None is a
        failure to report.

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
        async with self._turn_lock(turn_id):
            outcome = await self._complete_delivery_across_cancellation(
                turn_id=turn_id,
                stage=stage,
                room_id=room_id,
                thread_id=thread_id,
                payload=payload,
                edits_event_id=edits_event_id,
            )
        return await self._finish_flush(turn_id, outcome)

    async def _complete_delivery_across_cancellation(
        self,
        *,
        turn_id: str,
        stage: DeliveryStage,
        room_id: str,
        thread_id: str | None,
        payload: Mapping[str, object],
        edits_event_id: str | None,
    ) -> _FlushOutcome:
        """Finish a delivery whose durable handoff may already have committed."""
        completed: _FlushOutcome | None = None

        async def finish() -> _FlushOutcome:
            nonlocal completed
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
                completed = _FlushOutcome(event_id=None)
                return completed
            if handoff is not None:
                handoff.released(handed_over)
            outcome = await self._flush(turn_id=turn_id, stage=stage)
            if stage is DeliveryStage.FINAL and outcome.retry_required:
                # A prior process may have attempted the placeholder without
                # recording whether Matrix accepted it. FINAL is durably
                # queued, but reporting that temporary ordering block as a
                # delivery failure would run terminal cancellation hooks even
                # though recovery later shows the answer. Resolve INITIAL,
                # then retry FINAL before returning a lifecycle-visible result.
                await self._flush(turn_id=turn_id, stage=DeliveryStage.INITIAL)
                outcome = await self._flush(turn_id=turn_id, stage=DeliveryStage.FINAL)
            completed = outcome
            return completed

        try:
            return await run_coroutine_until_complete(finish())
        except asyncio.CancelledError as cancellation:
            if completed is None:
                raise
            return replace(completed, propagate_cancellation=cancellation)

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

        Nothing means the row is gone behind a membership fence, or this is an
        INITIAL a FINAL already superseded.

        Between the claim and the send sits the one question the outbox cannot
        answer from its own state: is the frozen transaction ID still proof
        that a resend cannot duplicate? When it is not -- when the device that
        attempted this row is not the device about to retry it -- the room is
        asked directly, and an answer already there is adopted instead of sent
        again.
        """
        async with self._turn_lock(turn_id):
            outcome = await self._flush(turn_id=turn_id, stage=stage)
        return await self._finish_flush(turn_id, outcome)

    async def _flush(self, *, turn_id: str, stage: DeliveryStage) -> _FlushOutcome:
        """Send one delivery while holding its turn's visible-delivery lock."""
        claimed = await self.store.claim_delivery(turn_id=turn_id, stage=stage)
        if claimed is None:
            blocked_final = (
                stage is DeliveryStage.FINAL
                and await self.store.load_delivery(
                    turn_id=turn_id,
                    stage=stage,
                )
                is not None
            )
            logger.info(
                "response_delivery_stage_blocked" if blocked_final else "response_delivery_row_withdrawn",
                turn_id=turn_id,
                stage=stage.value,
            )
            return _FlushOutcome(event_id=None, retry_required=blocked_final)
        if claimed.acknowledged_event_id is not None:
            return _FlushOutcome(event_id=claimed.acknowledged_event_id)
        if not self._transaction_id_still_deduplicates(claimed):
            already_delivered = await self._delivered_before_device_changed(claimed)
            if already_delivered is not None:
                return await self._acknowledge(turn_id, stage, already_delivered)
        # Only now, with a send actually about to happen. Writing this at claim
        # time instead loses the fact that a lookup is still owed: a room scan
        # that raises would leave the row unacknowledged but stamped with this
        # device, and the next pass would see its own marker, skip the lookup
        # and post the answer twice.
        return await self._complete_send_across_cancellation(claimed)

    async def _complete_send_across_cancellation(self, claimed: OutboxDelivery) -> _FlushOutcome:
        """Retain a completed outcome while delaying cancellation until post-lock work."""
        completed: _FlushOutcome | None = None

        async def finish() -> _FlushOutcome:
            nonlocal completed
            completed = await self._send_and_acknowledge(claimed)
            return completed

        try:
            return await run_coroutine_until_complete(finish())
        except asyncio.CancelledError as cancellation:
            if completed is None:
                raise
            return _FlushOutcome(
                event_id=completed.event_id,
                publish_committed_terminal=completed.publish_committed_terminal,
                retry_required=completed.retry_required,
                propagate_cancellation=cancellation,
            )

    async def _send_and_acknowledge(self, claimed: OutboxDelivery) -> _FlushOutcome:
        """Finish an accepted Matrix attempt before propagating local cancellation."""
        await self.store.record_sending_device(
            turn_id=claimed.turn_id,
            stage=claimed.stage,
            device_id=self.sending_device_id,
        )
        event_id = await self.send(claimed)
        return await self._acknowledge(claimed.turn_id, claimed.stage, event_id)

    async def _acknowledge(self, turn_id: str, stage: DeliveryStage, event_id: str) -> _FlushOutcome:
        """Bind the row and report whether its record needs publishing.

        The record commits inside the acknowledgement, so nothing else writes
        it -- which means the ledger never sees that write in the order it
        orders its own. Recovery is where that matters: it acknowledges and
        returns with no ordinary terminal write following, so a mutation racing
        this one overwrites the row and the answer's event ID is gone for good.
        Publishing the committed record is what puts it back under the
        ledger's ordering. That callback runs after the visible-delivery lock
        is released: it is downstream bookkeeping, and keeping the lock while
        calling out would let a callback that re-enters delivery deadlock the
        turn on itself.

        Only on a bound acknowledgement. A loser committed nothing, so it has
        nothing to settle and must not overwrite the winner's record.

        Ownership is the store's answer, never this call's own inference. The
        settled event matching the one just sent looks like proof of winning
        and is not: two processes resending one frozen transaction ID from the
        same device are deduplicated by Matrix into the *same* event, so a
        loser sees its own event on the row while the write that put it there
        was somebody else's -- and it would publish a record the database does
        not hold.

        Returns the event the row actually names, which is not always the one
        just sent. A caller that lost the race must report the winner's event
        upward, because everything downstream records what delivery returns --
        and a loser reporting its own send is how the outbox and the terminal
        record end up naming different events even though the acknowledgement
        itself was guarded.
        """
        acknowledged = await self.store.acknowledge_delivery(
            turn_id=turn_id,
            stage=stage,
            event_id=event_id,
            terminal_turn=self._terminal_turn(turn_id, stage, event_id),
        )
        if acknowledged.settled_event_id is None:
            return _FlushOutcome(event_id=event_id)
        return _FlushOutcome(
            event_id=acknowledged.settled_event_id,
            publish_committed_terminal=acknowledged.bound and stage is DeliveryStage.FINAL,
        )

    async def _finish_flush(self, turn_id: str, outcome: _FlushOutcome) -> str | None:
        """Run post-lock bookkeeping and return the visible event."""
        if (
            outcome.publish_committed_terminal
            and outcome.event_id is not None
            and self.terminal_turn_committed is not None
        ):
            await self.terminal_turn_committed(turn_id, outcome.event_id)
        if outcome.propagate_cancellation is not None:
            raise outcome.propagate_cancellation
        return outcome.event_id

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
                try:
                    async with self._turn_lock(delivery.turn_id):
                        outcome = await self._flush(turn_id=delivery.turn_id, stage=delivery.stage)
                    sent = await self._finish_flush(delivery.turn_id, outcome)
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
                    if outcome.retry_required:
                        failed += 1
                        continue
                    # The row went away behind a membership fence, or a
                    # FINAL superseded this INITIAL. Nothing is owed and
                    # nothing failed.
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
