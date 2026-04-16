"""Shared runtime coordinator for advisory Matrix event-cache writes."""

from __future__ import annotations

import asyncio
import time
import typing
import weakref
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from mindroom.background_tasks import create_background_task, wait_for_background_tasks
from mindroom.timing import emit_timing_event, timing_enabled

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

    def queue_thread_update(
        self,
        room_id: str,
        thread_id: str,
        update_coro_factory: typing.Callable[[], typing.Coroutine[Any, Any, object]],
        *,
        name: str,
        log_exceptions: bool = True,
    ) -> asyncio.Task[object]:
        """Queue one thread-scoped update behind same-thread and room-wide predecessors."""

    async def run_thread_update(
        self,
        room_id: str,
        thread_id: str,
        update_coro_factory: typing.Callable[[], typing.Coroutine[Any, Any, object]],
        *,
        name: str,
    ) -> object:
        """Run one thread-scoped update through the ordered thread barrier."""

    async def wait_for_room_idle(self, room_id: str) -> None:
        """Wait for one room's queued updates to drain."""

    async def wait_for_thread_idle(self, room_id: str, thread_id: str) -> None:
        """Wait for room-wide and same-thread queued updates to drain."""

    async def close(self) -> None:
        """Drain and tear down the coordinator."""


@dataclass
class _EventCacheWriteCoordinator:
    """Serialize same-room advisory cache writes across the whole runtime."""

    logger: structlog.stdlib.BoundLogger
    background_task_owner: object = field(default_factory=object)
    _room_update_tasks: dict[str, asyncio.Task[Any]] = field(default_factory=dict, init=False)
    _thread_update_tasks: dict[tuple[str, str], asyncio.Task[Any]] = field(default_factory=dict, init=False)
    _thread_update_tasks_by_room: dict[str, dict[str, asyncio.Task[Any]]] = field(
        default_factory=dict,
        init=False,
    )
    _room_update_predecessors: weakref.WeakKeyDictionary[
        asyncio.Task[Any],
        asyncio.Task[Any] | None,
    ] = field(default_factory=weakref.WeakKeyDictionary, init=False)
    _thread_update_predecessors: weakref.WeakKeyDictionary[
        asyncio.Task[Any],
        asyncio.Task[Any] | None,
    ] = field(default_factory=weakref.WeakKeyDictionary, init=False)

    def _pending_chain_length(self, tasks: tuple[asyncio.Task[Any], ...]) -> int:
        return len(self._pending_tasks(tasks))

    def _pending_tasks(self, tasks: tuple[asyncio.Task[Any], ...]) -> set[asyncio.Task[Any]]:
        pending_tasks: set[asyncio.Task[Any]] = set()
        for task in tasks:
            pending_tasks.update(self._pending_chain_tasks(task))
        return pending_tasks

    def _pending_chain_tasks(self, task: asyncio.Task[Any] | None) -> set[asyncio.Task[Any]]:
        pending_tasks: set[asyncio.Task[Any]] = set()
        seen: set[asyncio.Task[Any]] = set()
        current = task
        while current is not None and current not in seen:
            seen.add(current)
            if not current.done():
                pending_tasks.add(current)
            current = self._pending_predecessor(current)
        return pending_tasks

    def _emit_idle_wait_timing(
        self,
        *,
        room_id: str,
        wait_started: float | None,
        wait_iterations: int,
        pending_task_count: int,
    ) -> None:
        if wait_started is None:
            return
        emit_timing_event(
            "Event cache idle wait timing",
            barrier_kind="room",
            room_id=room_id,
            wait_ms=round((time.perf_counter() - wait_started) * 1000, 1),
            wait_iterations=wait_iterations,
            pending_task_count=pending_task_count,
        )

    def _pending_room_predecessor(self, task: asyncio.Task[Any]) -> asyncio.Task[Any] | None:
        predecessor = self._room_update_predecessors.get(task)
        while predecessor is not None and predecessor.done():
            if not predecessor.cancelled():
                return None
            predecessor = self._room_update_predecessors.get(predecessor)
        return predecessor

    def _pending_thread_predecessor(self, task: asyncio.Task[Any]) -> asyncio.Task[Any] | None:
        predecessor = self._thread_update_predecessors.get(task)
        while predecessor is not None and predecessor.done():
            if not predecessor.cancelled():
                return None
            predecessor = self._thread_update_predecessors.get(predecessor)
        return predecessor

    def _pending_predecessor(self, task: asyncio.Task[Any]) -> asyncio.Task[Any] | None:
        if task in self._thread_update_predecessors:
            return self._pending_thread_predecessor(task)
        return self._pending_room_predecessor(task)

    async def _await_predecessors(
        self,
        room_id: str,
        operation: str,
        previous_tasks: tuple[asyncio.Task[Any], ...],
    ) -> None:
        pending_predecessors = list(previous_tasks)
        seen_predecessors: set[asyncio.Task[Any]] = set()
        while pending_predecessors:
            predecessor = pending_predecessors.pop(0)
            if predecessor in seen_predecessors:
                continue
            seen_predecessors.add(predecessor)
            try:
                await predecessor
            except asyncio.CancelledError:
                current_task = asyncio.current_task()
                if current_task is not None and current_task.cancelling():
                    raise
                replacement_predecessor = self._pending_predecessor(predecessor)
                if replacement_predecessor is not None:
                    pending_predecessors.append(replacement_predecessor)
            except Exception as exc:
                self.logger.debug(
                    "Previous room cache update failed before follow-up update",
                    room_id=room_id,
                    operation=operation,
                    error=str(exc),
                )

    def _clear_room_tail(self, room_id: str, done_task: asyncio.Task[object]) -> None:
        if self._room_update_tasks.get(room_id) is not done_task:
            return
        predecessor = self._pending_room_predecessor(done_task)
        if done_task.cancelled() and predecessor is not None:
            self._room_update_tasks[room_id] = predecessor
            return
        self._room_update_tasks.pop(room_id, None)

    def _clear_thread_tail(
        self,
        room_id: str,
        thread_id: str,
        done_task: asyncio.Task[object],
    ) -> None:
        key = (room_id, thread_id)
        if self._thread_update_tasks.get(key) is not done_task:
            return
        predecessor = self._pending_thread_predecessor(done_task)
        if done_task.cancelled() and predecessor is not None:
            self._thread_update_tasks[key] = predecessor
            self._thread_update_tasks_by_room.setdefault(room_id, {})[thread_id] = predecessor
            return
        self._thread_update_tasks.pop(key, None)
        room_threads = self._thread_update_tasks_by_room.get(room_id)
        if room_threads is None:
            return
        room_threads.pop(thread_id, None)
        if not room_threads:
            self._thread_update_tasks_by_room.pop(room_id, None)

    def _clear_room_thread_tail_if_current(
        self,
        room_id: str,
        done_task: asyncio.Task[object],
    ) -> None:
        room_threads = self._thread_update_tasks_by_room.get(room_id)
        if room_threads is None:
            return
        for thread_id, current_task in list(room_threads.items()):
            if current_task is done_task and done_task.done():
                self._clear_thread_tail(room_id, thread_id, done_task)

    def _room_predecessors(self, room_id: str) -> tuple[asyncio.Task[Any], ...]:
        predecessors: list[asyncio.Task[Any]] = []
        room_task = self._room_update_tasks.get(room_id)
        if room_task is not None:
            predecessors.append(room_task)
        thread_tasks = self._thread_update_tasks_by_room.get(room_id, {})
        predecessors.extend(thread_tasks.values())
        return tuple(dict.fromkeys(predecessors))

    def _thread_predecessors(self, room_id: str, thread_id: str) -> tuple[asyncio.Task[Any], ...]:
        predecessors: list[asyncio.Task[Any]] = []
        room_task = self._room_update_tasks.get(room_id)
        if room_task is not None:
            predecessors.append(room_task)
        thread_task = self._thread_update_tasks.get((room_id, thread_id))
        if thread_task is not None:
            predecessors.append(thread_task)
        return tuple(dict.fromkeys(predecessors))

    def queue_room_update(
        self,
        room_id: str,
        update_coro_factory: typing.Callable[[], typing.Coroutine[Any, Any, object]],
        *,
        name: str,
        log_exceptions: bool = True,
    ) -> asyncio.Task[object]:
        """Schedule one room-scoped cache update behind any active predecessor."""
        previous_room_task = self._room_update_tasks.get(room_id)
        previous_tasks = self._room_predecessors(room_id)
        instrument_timing = timing_enabled()

        if not instrument_timing:

            async def run_after_previous() -> object:
                await self._await_predecessors(room_id, name, previous_tasks)
                return await update_coro_factory()

        else:
            predecessor_count = self._pending_chain_length(previous_tasks)

            async def run_after_previous() -> object:
                started = time.perf_counter()
                outcome = "ok"
                update_started: float | None = None
                try:
                    await self._await_predecessors(room_id, name, previous_tasks)
                    update_started = time.perf_counter()
                    return await update_coro_factory()
                except asyncio.CancelledError:
                    outcome = "cancelled"
                    raise
                except Exception:
                    outcome = "error"
                    raise
                finally:
                    finished = time.perf_counter()
                    total_ms = round((finished - started) * 1000, 1)
                    if update_started is None:
                        predecessor_wait_ms = total_ms
                        update_run_ms = 0.0
                    else:
                        predecessor_wait_ms = round((update_started - started) * 1000, 1)
                        update_run_ms = round((finished - update_started) * 1000, 1)
                    emit_timing_event(
                        "Event cache update timing",
                        barrier_kind="room",
                        room_id=room_id,
                        operation=name,
                        predecessor_count=predecessor_count,
                        queued_behind_predecessor=predecessor_count > 0,
                        predecessor_wait_ms=predecessor_wait_ms,
                        update_run_ms=update_run_ms,
                        total_ms=total_ms,
                        outcome=outcome,
                    )

        task = create_background_task(
            run_after_previous(),
            name=name,
            owner=self.background_task_owner,
            log_exceptions=log_exceptions,
        )
        self._room_update_predecessors[task] = previous_room_task
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

    def queue_thread_update(
        self,
        room_id: str,
        thread_id: str,
        update_coro_factory: typing.Callable[[], typing.Coroutine[Any, Any, object]],
        *,
        name: str,
        log_exceptions: bool = True,
    ) -> asyncio.Task[object]:
        """Schedule one thread-scoped cache update behind room-wide and same-thread predecessors."""
        key = (room_id, thread_id)
        previous_thread_task = self._thread_update_tasks.get(key)
        previous_tasks = self._thread_predecessors(room_id, thread_id)

        async def run_after_previous() -> object:
            await self._await_predecessors(room_id, name, previous_tasks)
            return await update_coro_factory()

        task = create_background_task(
            run_after_previous(),
            name=name,
            owner=self.background_task_owner,
            log_exceptions=log_exceptions,
        )
        self._thread_update_predecessors[task] = previous_thread_task
        self._thread_update_tasks[key] = task
        self._thread_update_tasks_by_room.setdefault(room_id, {})[thread_id] = task
        task.add_done_callback(lambda done_task: self._clear_thread_tail(room_id, thread_id, done_task))
        return task

    async def run_thread_update(
        self,
        room_id: str,
        thread_id: str,
        update_coro_factory: typing.Callable[[], typing.Coroutine[Any, Any, object]],
        *,
        name: str,
    ) -> object:
        """Run one thread-scoped operation through the ordered thread barrier and await its result."""
        return await self.queue_thread_update(
            room_id,
            thread_id,
            update_coro_factory,
            name=name,
            log_exceptions=False,
        )

    async def wait_for_room_idle(self, room_id: str) -> None:
        """Wait for the currently queued same-room update chain to drain."""
    async def _wait_for_room_idle_without_timing(self, room_id: str) -> None:
        while True:
            pending_tasks = self._room_predecessors(room_id)
            if not pending_tasks:
                return
            for pending_task in pending_tasks:
                try:
                    await pending_task
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
                    self._clear_room_thread_tail_if_current(room_id, pending_task)
                    if self._room_update_tasks.get(room_id) is pending_task and pending_task.done():
                        self._clear_room_tail(room_id, pending_task)

    async def _wait_for_room_idle_with_timing(self, room_id: str) -> None:
        wait_started: float | None = None
        wait_iterations = 0
        pending_tasks_seen: set[asyncio.Task[Any]] = set()
        while True:
            pending_tasks = self._room_predecessors(room_id)
            if not pending_tasks:
                self._emit_idle_wait_timing(
                    room_id=room_id,
                    wait_started=wait_started,
                    wait_iterations=wait_iterations,
                    pending_task_count=len(pending_tasks_seen),
                )
                return
            if wait_started is None:
                wait_started = time.perf_counter()
            pending_tasks_seen.update(self._pending_tasks(pending_tasks))
            for pending_task in pending_tasks:
                try:
                    await pending_task
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
                    self._clear_room_thread_tail_if_current(room_id, pending_task)
                    if self._room_update_tasks.get(room_id) is pending_task and pending_task.done():
                        self._clear_room_tail(room_id, pending_task)
            wait_iterations += 1

    async def wait_for_room_idle(self, room_id: str) -> None:
        """Wait for the currently queued same-room update chain to drain."""
        if timing_enabled():
            await self._wait_for_room_idle_with_timing(room_id)
            return
        await self._wait_for_room_idle_without_timing(room_id)

    async def wait_for_thread_idle(self, room_id: str, thread_id: str) -> None:
        """Wait for room-wide and same-thread queued updates to drain."""
        key = (room_id, thread_id)
        while True:
            pending_tasks = self._thread_predecessors(room_id, thread_id)
            if not pending_tasks:
                return
            for pending_task in pending_tasks:
                try:
                    await pending_task
                except asyncio.CancelledError:
                    current_task = asyncio.current_task()
                    if current_task is not None and current_task.cancelling():
                        raise
                except Exception as exc:
                    self.logger.debug(
                        "Thread cache update failed before thread became idle",
                        room_id=room_id,
                        thread_id=thread_id,
                        error=str(exc),
                    )
                finally:
                    if self._thread_update_tasks.get(key) is pending_task and pending_task.done():
                        self._clear_thread_tail(room_id, thread_id, pending_task)
                    if self._room_update_tasks.get(room_id) is pending_task and pending_task.done():
                        self._clear_room_tail(room_id, pending_task)

    async def close(self) -> None:
        """Drain any queued cache writes for this coordinator."""
        await wait_for_background_tasks(timeout=5.0, owner=self.background_task_owner)
        self._room_update_tasks.clear()
        self._thread_update_tasks.clear()
        self._thread_update_tasks_by_room.clear()
        self._room_update_predecessors.clear()
        self._thread_update_predecessors.clear()
