"""Shared runtime coordinator for advisory Matrix event-cache writes."""

from __future__ import annotations

import asyncio
import time
import typing
import weakref
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from mindroom.background_tasks import create_background_task, wait_for_background_tasks
from mindroom.timing import timing_enabled, timing_scope

if TYPE_CHECKING:
    import structlog


class EventCacheWriteCoordinator(Protocol):
    """Runtime-facing coordinator contract for ordered advisory cache writes."""

    def queue_room_update(
        self,
        room_id: str,
        update_coro_factory: typing.Callable[[], typing.Coroutine[Any, Any, object]],
        *,
        name: str,
        log_context: dict[str, object] | None = None,
        log_exceptions: bool = True,
    ) -> asyncio.Task[object]:
        """Queue one room-scoped update behind any active predecessor."""

    async def run_room_update(
        self,
        room_id: str,
        update_coro_factory: typing.Callable[[], typing.Coroutine[Any, Any, object]],
        *,
        name: str,
        log_context: dict[str, object] | None = None,
    ) -> object:
        """Run one room-scoped update through the same ordered barrier."""

    async def wait_for_room_idle(self, room_id: str) -> None:
        """Wait for one room's queued updates to drain."""

    async def close(self) -> None:
        """Drain and tear down the coordinator."""


@dataclass
class _EventCacheWriteCoordinator:
    """Serialize same-room advisory cache writes across the whole runtime."""

    logger: structlog.stdlib.BoundLogger
    background_task_owner: object = field(default_factory=object)
    _room_update_tasks: dict[str, asyncio.Task[Any]] = field(default_factory=dict, init=False)
    _room_update_predecessors: weakref.WeakKeyDictionary[
        asyncio.Task[Any],
        asyncio.Task[Any] | None,
    ] = field(default_factory=weakref.WeakKeyDictionary, init=False)

    def _pending_predecessor(self, task: asyncio.Task[Any]) -> asyncio.Task[Any] | None:
        predecessor = self._room_update_predecessors.get(task)
        while predecessor is not None and predecessor.done():
            if not predecessor.cancelled():
                return None
            predecessor = self._room_update_predecessors.get(predecessor)
        return predecessor

    async def _await_predecessor(
        self,
        room_id: str,
        operation: str,
        previous_task: asyncio.Task[Any] | None,
    ) -> None:
        predecessor = previous_task
        while predecessor is not None:
            try:
                await predecessor
            except asyncio.CancelledError:
                current_task = asyncio.current_task()
                if current_task is not None and current_task.cancelling():
                    raise
                predecessor = self._pending_predecessor(predecessor)
            except Exception as exc:
                self.logger.debug(
                    "Previous room cache update failed before follow-up update",
                    room_id=room_id,
                    operation=operation,
                    error=str(exc),
                )
                return
            else:
                return

    def _clear_room_tail(self, room_id: str, done_task: asyncio.Task[object]) -> None:
        if self._room_update_tasks.get(room_id) is not done_task:
            return
        predecessor = self._pending_predecessor(done_task)
        if done_task.cancelled() and predecessor is not None:
            self._room_update_tasks[room_id] = predecessor
            return
        self._room_update_tasks.pop(room_id, None)

    def _emit_timing_log(self, event: str, **event_data: object) -> None:
        if not timing_enabled():
            return
        scope = timing_scope.get()
        if scope:
            event_data["timing_scope"] = scope
        self.logger.info(event, **event_data)

    def queue_room_update(
        self,
        room_id: str,
        update_coro_factory: typing.Callable[[], typing.Coroutine[Any, Any, object]],
        *,
        name: str,
        log_context: dict[str, object] | None = None,
        log_exceptions: bool = True,
    ) -> asyncio.Task[object]:
        """Schedule one room-scoped cache update behind any active predecessor."""
        previous_task = self._room_update_tasks.get(room_id)

        async def run_after_previous() -> object:
            queued_at = time.perf_counter()
            update_started_at = queued_at
            outcome = "ok"
            try:
                await self._await_predecessor(room_id, name, previous_task)
                update_started_at = time.perf_counter()
                return await update_coro_factory()
            except asyncio.CancelledError:
                outcome = "cancelled"
                raise
            except Exception:
                outcome = "error"
                raise
            finally:
                finished_at = time.perf_counter()
                self._emit_timing_log(
                    "Room cache update timing",
                    room_id=room_id,
                    operation=name,
                    predecessor_wait_ms=round((update_started_at - queued_at) * 1000, 1),
                    update_run_ms=round((finished_at - update_started_at) * 1000, 1),
                    total_ms=round((finished_at - queued_at) * 1000, 1),
                    queued_behind_predecessor=previous_task is not None,
                    outcome=outcome,
                    **(log_context or {}),
                )

        task = create_background_task(
            run_after_previous(),
            name=name,
            owner=self.background_task_owner,
            log_exceptions=log_exceptions,
        )
        self._room_update_predecessors[task] = previous_task
        self._room_update_tasks[room_id] = task
        task.add_done_callback(lambda done_task: self._clear_room_tail(room_id, done_task))
        return task

    async def run_room_update(
        self,
        room_id: str,
        update_coro_factory: typing.Callable[[], typing.Coroutine[Any, Any, object]],
        *,
        name: str,
        log_context: dict[str, object] | None = None,
    ) -> object:
        """Run one room-scoped operation through the same ordered barrier and await its result."""
        return await self.queue_room_update(
            room_id,
            update_coro_factory,
            name=name,
            log_context=log_context,
            log_exceptions=False,
        )

    async def wait_for_room_idle(self, room_id: str) -> None:
        """Wait for the currently queued same-room update chain to drain."""
        wait_started_at: float | None = None
        initial_tail_name: str | None = None
        while True:
            tail_task = self._room_update_tasks.get(room_id)
            if tail_task is None:
                if wait_started_at is not None:
                    self._emit_timing_log(
                        "Room cache idle wait timing",
                        room_id=room_id,
                        wait_ms=round((time.perf_counter() - wait_started_at) * 1000, 1),
                        waiting_for_operation=initial_tail_name,
                    )
                return
            if wait_started_at is None:
                wait_started_at = time.perf_counter()
                initial_tail_name = tail_task.get_name()
            try:
                await tail_task
            except asyncio.CancelledError:
                current_task = asyncio.current_task()
                if current_task is not None and current_task.cancelling():
                    raise
            except Exception as exc:
                self.logger.debug(
                    "Room cache update failed before room became idle",
                    room_id=room_id,
                    error=str(exc),
                )
            finally:
                if self._room_update_tasks.get(room_id) is tail_task and tail_task.done():
                    self._clear_room_tail(room_id, tail_task)

    async def close(self) -> None:
        """Drain any queued cache writes for this coordinator."""
        await wait_for_background_tasks(timeout=5.0, owner=self.background_task_owner)
        self._room_update_tasks.clear()
        self._room_update_predecessors.clear()
