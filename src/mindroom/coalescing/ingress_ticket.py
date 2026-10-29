"""One ingress item's ownership from its Matrix callback to gate admission."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.coalescing.gate import CoalescingGate, LaneSlot, ReadyPendingEvent
from mindroom.coalescing.ingress_lanes import drop_readiness
from mindroom.dispatch_callback_outcome import TurnDispatchOutcome
from mindroom.turn_store import ClaimContended, ClaimRefused, TurnClaim

if TYPE_CHECKING:
    import asyncio

    from mindroom.coalescing.batch import CoalescingKey, PendingEvent
    from mindroom.response_lifecycle import QueuedHumanNoticeReservation
    from mindroom.turn_record import TurnRecord
    from mindroom.turn_store import TurnStore


@dataclass(eq=False)
class IngressTicket:
    """Own one ingress item's lane slot, turn claim, and queued notice until admission moves them on.

    ``close`` gives back whatever admission did not move, so every exit of a
    callback ends with the same idempotent call.
    """

    gate: CoalescingGate
    slot: LaneSlot
    turn_store: TurnStore
    claim: TurnClaim | None = None
    notice: QueuedHumanNoticeReservation | None = None
    _closed: bool = False

    async def claim_turn(self, record: TurnRecord) -> TurnDispatchOutcome | None:
        """Claim this item's turn, or return the outcome that ends its callback.

        A claim another live owner holds is not waited for: the wait would hold
        the room's journal lane, and that owner can be a turn held for an
        approval whose decision arrives in the same room. The source goes back
        to the journal once the owner releases, and the caller's ``close``
        gives the lane slot up, because the owner's batch cannot flush while it
        is held.
        """
        match await self.turn_store.claim(record):
            case TurnClaim() as claim:
                self.claim = claim
                return None
            case ClaimContended(blocking=blocking):
                # A contended claim has sources: one without any is refused before it can meet another.
                self.turn_store.retry_when_released(blocking, self.slot.room_id, record.source_event_ids[0])
                return TurnDispatchOutcome.DEFERRED
            case ClaimRefused():
                # It has no source, or a completed turn of another identity owns one of its aliases.
                if await self.refusal_defers(record):
                    return TurnDispatchOutcome.DEFERRED
                return TurnDispatchOutcome.INTENTIONALLY_IGNORED

    async def refusal_defers(self, record: TurnRecord) -> bool:
        """Return whether the callback of an item whose claim on this turn was refused defers instead of settling.

        It defers when one of its sources is already handled: answered, or redacted.
        """
        return bool(await self.turn_store.handled_among(self.slot.room_id, record.source_event_ids))

    def take_claim(self) -> TurnClaim:
        """Hand this item's claim to a dispatch that bypasses the gate."""
        claim, self.claim = self.claim, None
        assert claim is not None
        return claim

    def admit(self, key: CoalescingKey, pending_event: PendingEvent) -> None:
        """Move the claim and queued notice into the event and hand it to the gate through this lane slot."""
        pending_event.claim, self.claim = self.claim, None
        pending_event.notice, self.notice = self.notice, None
        try:
            self.gate.submit_lane_slot(
                self.slot,
                key=key,
                ready_result=ReadyPendingEvent(pending_event=pending_event),
                source_event_id=pending_event.event.event_id,
            )
        except BaseException:
            pending_event.close()
            raise
        self._closed = True

    def admit_ready(
        self,
        key: CoalescingKey,
        ready_task: asyncio.Task[ReadyPendingEvent | None],
        *,
        source_event_id: str,
    ) -> None:
        """Hand a readiness task to the gate through this lane slot, its lane holding the claim until readiness ends."""
        claim, self.claim = self.claim, None
        try:
            self.gate.submit_lane_slot(
                self.slot,
                key=key,
                ready_task=ready_task,
                claim=claim,
                source_event_id=source_event_id,
            )
        except BaseException:
            ready_task.cancel()
            drop_readiness(ready_task, claim)
            raise
        self._closed = True

    def close(self) -> None:
        """Give back what admission did not move: the claim, the queued notice, and the lane slot."""
        if self._closed:
            return
        self._closed = True
        claim, self.claim = self.claim, None
        if claim is not None:
            claim.release()
        notice, self.notice = self.notice, None
        if notice is not None:
            notice.release()
        self.gate.release_lane_slot(self.slot)
