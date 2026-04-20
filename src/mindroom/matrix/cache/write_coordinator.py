"""Shared runtime coordinator for advisory Matrix event-cache writes."""

from __future__ import annotations

import asyncio
import typing
import weakref
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from mindroom.background_tasks import create_background_task, wait_for_background_tasks
from mindroom.logging_config import bound_log_context

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
        log_exceptions: bool = True,
    ) -> asyncio.Task[object]:
        """Queue one room-scoped update behind any active predecessor."""

    async def run_room_update(
        self,
        room_id: str,
        update_coro_factory: typing.Callable[[], typing.Coroutine[Any, Any, object]],
        *,
        name: str,
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
        *,
        logger: structlog.stdlib.BoundLogger,
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
                logger.debug(
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

    def queue_room_update(
        self,
        room_id: str,
        update_coro_factory: typing.Callable[[], typing.Coroutine[Any, Any, object]],
        *,
        name: str,
        log_exceptions: bool = True,
    ) -> asyncio.Task[object]:
        """Schedule one room-scoped cache update behind any active predecessor."""
        previous_task = self._room_update_tasks.get(room_id)

        async def run_after_previous() -> object:
            current_task = asyncio.current_task()
            task_name = current_task.get_name() if current_task is not None else name
            with bound_log_context(task_name=task_name):
                await self._await_predecessor(
                    room_id,
                    name,
                    previous_task,
                    logger=self.logger,
                )
                return await update_coro_factory()

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
    ) -> object:
        """Run one room-scoped operation through the same ordered barrier and await its result."""
        return await self.queue_room_update(
            room_id,
            update_coro_factory,
            name=name,
            log_exceptions=False,
        )

    async def wait_for_room_idle(self, room_id: str) -> None:
        """Wait for the currently queued same-room update chain to drain."""
        while True:
            tail_task = self._room_update_tasks.get(room_id)
            if tail_task is None:
                return
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
