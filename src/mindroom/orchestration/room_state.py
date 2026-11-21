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

    from mindroom.runtime_protocols import OrchestratorRuntime

_MAX_RETRY_SECONDS = 60


class RoomStateWorker:
    """Own one background task per dirty or retrying room in this runtime."""

    def __init__(self, runtime: OrchestratorRuntime) -> None:
        self._runtime = runtime
        self._rooms: dict[str, tuple[asyncio.Task[None], asyncio.Event]] = {}
        self._slots = asyncio.Semaphore(4)

    def _accepting(self) -> bool:
        return self._runtime.running and not self._runtime.response_admission_gate.closed

    def mark(self, room_id: str) -> None:
        """Schedule a fresh comparison, without waiting for Matrix or a room lock."""
        if not self._accepting():
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
            task.cancel()

    async def _attempt(
        self,
        room_id: str,
        retry: tuple[str, set[int]] | None,
        dirty: asyncio.Event,
        failure_level: Callable[[bool], int],
    ) -> tuple[str, set[int]] | None:
        """Return the writer and failed steps, or None when there is no eligible client."""
        if not self._accepting():
            return None
        runtime = self._runtime
        steward = runtime.room_steward(room_id)
        if steward is None or (client := steward.client) is None:
            return None
        config = runtime.config

        def current() -> bool:
            return (
                self._accepting()
                and runtime.config is config
                and runtime.room_steward(room_id) is steward
                and steward.client is client
            )

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
            and current()
            and await step(may_write=current, failure_level=failure_level) is False
        }
        if self._accepting() and not current():
            # Keep the work in this room when its credential or config changes during a comparison.
            dirty.set()
        return client.user_id, failed

    async def _run(self, room_id: str, dirty: asyncio.Event) -> None:
        retry: tuple[str, set[int]] | None = None
        delay, warned = 0, False

        def failure_level(_timed_out: bool) -> int:
            nonlocal warned
            if warned:
                return logging.DEBUG
            if delay < _MAX_RETRY_SECONDS:
                return logging.INFO
            warned = True
            return logging.WARNING

        while self._accepting():
            async with self._slots:
                if dirty.is_set():
                    retry = None
                    dirty.clear()
                retry = await self._attempt(room_id, retry, dirty, failure_level)
            if retry is None:
                return
            if not retry[1]:
                delay, warned = 0, False
                if not dirty.is_set():
                    return
            elif not dirty.is_set() and self._accepting():
                delay = min(max(1, delay * 2), _MAX_RETRY_SECONDS)
                jitter = uniform(delay * 0.5, min(delay * 1.5, _MAX_RETRY_SECONDS))  # noqa: S311 - retry timing
                with suppress(TimeoutError):
                    async with asyncio.timeout(jitter):
                        await dirty.wait()
