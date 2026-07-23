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


@dataclass
class PromptIngressReservationOwner:
    """Own one prompt ingress lane slot until it is admitted or released."""

    gate: CoalescingGate
    slot: LaneSlot
    admitted: bool = False
    ready_task: asyncio.Task[ReadyPendingEvent | None] | None = None

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
            )
            metadata_transferred = True
        except BaseException:
            await self.cancel_ready_task()
            if ready_result is not None and not metadata_transferred:
                close_ready_task_result_metadata(ready_result)
            raise
        self.admitted = True
        self.ready_task = None

    async def cancel_ready_task(self) -> None:
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
        """Release this lane slot if admission did not transfer ownership."""
        if self.admitted:
            return
        try:
            await self.cancel_ready_task()
        finally:
            self.gate.release_lane_slot(self.slot)
