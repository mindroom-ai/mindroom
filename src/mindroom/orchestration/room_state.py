"""Coalesce background Matrix state reconciliation independently of the elected account."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from contextvars import Context
from functools import partial
from random import uniform
from typing import TYPE_CHECKING

from mindroom.background_tasks import create_background_task
from mindroom.matrix.room_accounts import reconcile_room_accounts, reconcile_room_router_binding
from mindroom.matrix.room_thread_mode import reconcile_room_thread_mode

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.bot import AgentBot
    from mindroom.runtime_protocols import OrchestratorRuntime

_MAX_RETRY_SECONDS = 60


class RoomStateWorker:
    """Own one background task per dirty or retrying room in this runtime."""

    def __init__(self, runtime: OrchestratorRuntime) -> None:
        self._runtime = runtime
        self._rooms: dict[str, tuple[asyncio.Task[None], asyncio.Event]] = {}
        self._slots = asyncio.Semaphore(4)
        self._borrowed: dict[str, AgentBot] = {}

    def mark(self, room_id: str) -> None:
        """Schedule a fresh comparison, without waiting for Matrix or a room lock."""
        if not self._runtime.running:
            return
        if (entry := self._rooms.get(room_id)) is not None and not entry[0].done():
            entry[1].set()
            return
        dirty = asyncio.Event()
        dirty.set()
        context = Context()  # Room work outlives the sync callback that marked it.
        task = create_background_task(self._run(room_id, dirty), "room_state_worker", owner=self, context=context)
        self._rooms[room_id] = (task, dirty)
        task.add_done_callback(partial(self._retire, room_id), context=context)

    def _retire(self, room_id: str, task: asyncio.Task[None]) -> None:
        if (entry := self._rooms.get(room_id)) is not None and entry[0] is task:
            del self._rooms[room_id]

    async def mark_held(self) -> None:
        """Mark the union of held rooms after startup or a reload, including invited rooms."""
        for room_id in {
            r for bot in self._runtime.agent_bots.values() if bot.client is not None for r in bot.client.rooms
        }:
            self.mark(room_id)

    def stop(self) -> None:
        """Cancel in-flight and pending work once runtime admission has closed."""
        for task, _ in self._rooms.values():
            if not task.cancelling():
                task.cancel()

    async def release(self, bot: AgentBot) -> None:
        """Cancel and drain attempts borrowing a draining bot before its client closes."""
        tasks = {room_id: self._rooms[room_id][0] for room_id, borrower in self._borrowed.items() if borrower is bot}
        for task in tasks.values():
            if not task.cancelling():
                task.cancel()
        try:
            await asyncio.gather(*tasks.values(), return_exceptions=True)
        finally:
            for room_id in tasks:
                self.mark(room_id)

    async def _attempt(
        self,
        room_id: str,
        retry: tuple[str, set[int]] | None,
        dirty: asyncio.Event,
        failure_level: Callable[[bool], int],
    ) -> tuple[str, set[int]] | None:
        """Return the writer and failed steps, or None when there is no eligible client."""
        runtime = self._runtime
        if not runtime.running:
            return None
        steward = runtime.room_steward(room_id, accepting_only=True)
        if steward is None or (client := steward.client) is None:
            return None
        self._borrowed[room_id] = steward
        config = runtime.config

        def current() -> bool:
            return (
                runtime.running
                and runtime.config is config
                and runtime.room_steward(room_id, accepting_only=True) is steward
                and steward.client is client
            )

        def may_write() -> bool:
            return not runtime.response_admission_gate.closed and current()

        args = (client, config, runtime.runtime_paths, room_id)
        steps = (
            partial(reconcile_room_thread_mode, *args),
            partial(reconcile_room_accounts, *args),
            partial(reconcile_room_router_binding, client, room_id),
        )
        # A different writer has its own roster key and must compare all three steps.
        needed = retry[1] if retry is not None and retry[0] == client.user_id else None
        failed = {
            i
            for i, step in enumerate(steps)
            if (needed is None or i in needed)
            and (not may_write() or await step(may_write=may_write, failure_level=failure_level) is False)
        }
        if not current():
            # Keep the work in this room when its credential or config changes during a comparison.
            dirty.set()
        return client.user_id, failed

    async def _run(self, room_id: str, dirty: asyncio.Event) -> None:
        retry: tuple[str, set[int]] | None = None
        delay, warned = 0, False

        def failure_level(timed_out: bool) -> int:
            nonlocal warned
            if warned:
                return logging.DEBUG
            if timed_out and delay < _MAX_RETRY_SECONDS:
                return logging.INFO
            warned = True
            return logging.WARNING

        gate = self._runtime.response_admission_gate
        while self._runtime.running:
            await gate.wait_until_open()
            async with self._slots:
                if gate.closed:
                    continue
                if dirty.is_set():
                    retry = None
                    dirty.clear()
                try:
                    retry = await self._attempt(room_id, retry, dirty, failure_level)
                finally:
                    self._borrowed.pop(room_id, None)
            if retry is None or not (retry[1] or dirty.is_set()):
                return
            if not retry[1]:
                delay, warned = 0, False
            elif not (gate.closed or dirty.is_set()):
                delay = min(max(1, delay * 2), _MAX_RETRY_SECONDS)
                jitter = uniform(delay * 0.5, min(delay * 1.5, _MAX_RETRY_SECONDS))  # noqa: S311 - retry timing
                with suppress(TimeoutError):
                    async with asyncio.timeout(jitter):
                        await dirty.wait()
