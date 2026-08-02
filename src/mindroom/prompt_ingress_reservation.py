"""Prompt-ingress lane slot ownership."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.coalescing import (
    CoalescingGate,
    LaneSlot,
    ReadyPendingEvent,
    close_ready_task_result_metadata,
)

if TYPE_CHECKING:
    from mindroom.coalescing_batch import CoalescingKey
    from mindroom.pending_turn_claim import PendingTurnClaim


@dataclass
class PromptIngressReservationOwner:
    """Own one prompt ingress lane slot until it is admitted or released."""

    gate: CoalescingGate
    slot: LaneSlot
    admitted: bool = False
    ready_task: asyncio.Task[ReadyPendingEvent | None] | None = None
    pending_turn_claim: PendingTurnClaim | None = None

    def own_pending_turn_claim(self, turn_claim: PendingTurnClaim) -> None:
        """Own one live-turn claim until dispatch handoff or release."""
        if self.pending_turn_claim is not None:
            msg = "Prompt ingress reservation already owns a turn claim"
            raise RuntimeError(msg)
        self.pending_turn_claim = turn_claim

    def take_pending_turn_claim(self) -> PendingTurnClaim | None:
        """Transfer the owned live-turn claim without releasing it."""
        turn_claim = self.pending_turn_claim
        self.pending_turn_claim = None
        return turn_claim

    def release_pending_turn_claim(self) -> None:
        """Release and clear the owned live-turn claim once."""
        turn_claim = self.pending_turn_claim
        self.pending_turn_claim = None
        if turn_claim is not None:
            turn_claim.close()

    @staticmethod
    def _close_late_ready_task_result(task: asyncio.Task[ReadyPendingEvent | None]) -> None:
        try:
            result = task.result()
        except BaseException:
            return
        close_ready_task_result_metadata(result)

    async def admit(
        self,
        key: CoalescingKey,
        *,
        source_event_id: str | None,
        source_kind: str,
        callback_source_kind: str | None = None,
        ready_result: ReadyPendingEvent | None = None,
        ready_task: asyncio.Task[ReadyPendingEvent | None] | None = None,
    ) -> None:
        """Transfer this lane slot and any ready metadata to the coalescing gate."""
        if ready_task is not None:
            self.ready_task = ready_task
        metadata_transferred = False
        try:
            self.gate.submit_lane_slot(
                self.slot,
                key=key,
                ready_result=ready_result,
                ready_task=ready_task,
                source_event_id=source_event_id,
                source_kind=source_kind,
                callback_source_kind=callback_source_kind,
            )
            metadata_transferred = True
        except BaseException:
            await self._cancel_ready_task()
            if ready_result is not None and not metadata_transferred:
                close_ready_task_result_metadata(ready_result)
            raise
        self.admitted = True
        self.ready_task = None

    async def _cancel_ready_task(self) -> None:
        """Cancel or collect the owned ready task once."""
        if self.ready_task is None:
            return
        ready_task = self.ready_task
        self.ready_task = None
        if not ready_task.done():
            ready_task.cancel()
        try:
            result = await asyncio.gather(ready_task, return_exceptions=True)
        except asyncio.CancelledError:
            if ready_task.done():
                self._close_late_ready_task_result(ready_task)
            else:
                ready_task.add_done_callback(self._close_late_ready_task_result)
            raise
        close_ready_task_result_metadata(result[0])

    async def release(self) -> None:
        """Release this lane slot and any claim not transferred to dispatch."""
        if self.admitted:
            return
        try:
            await self._cancel_ready_task()
        finally:
            try:
                self.release_pending_turn_claim()
            finally:
                self.gate.release_lane_slot(self.slot)
