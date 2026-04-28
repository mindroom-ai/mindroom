"""Shared runtime coordinator for advisory Matrix event-cache writes."""

from __future__ import annotations

import asyncio
import time
import typing
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from mindroom.background_tasks import create_background_task, wait_for_background_tasks
from mindroom.timing import emit_timing_event, timing_enabled

if TYPE_CHECKING:
    import structlog


_UpdateTask = asyncio.Task[Any]
_UpdateCoroFactory = typing.Callable[[], typing.Coroutine[Any, Any, object]]
_CoalesceKey = tuple[str, str]


@dataclass(eq=False)
class _QueuedRoomFence:
    """Preserve one cancelled room barrier until the earlier room segment drains."""

    sequence: int


@dataclass(eq=False)
class _QueuedUpdate:
    sequence: int
    kind: typing.Literal["room", "thread"]
    task: _UpdateTask
    start_signal: asyncio.Future[None]
    update_state: _QueuedUpdateState
    thread_id: str | None = None
    ignore_cancelled_room_fences: bool = False
    coalesce_key: _CoalesceKey | None = None
    started: bool = False


_RoomQueueEntry = _QueuedRoomFence | _QueuedUpdate


@dataclass
class _QueuedUpdateState:
    update_coro_factory: _UpdateCoroFactory
    coalesced_update_count: int = 0
    coalesce_log_context: dict[str, object] = field(default_factory=dict)


@dataclass
class _RoomSchedulerState:
    entries: list[_RoomQueueEntry] = field(default_factory=list)
    active_room: _QueuedUpdate | None = None
    active_threads: dict[str, _QueuedUpdate] = field(default_factory=dict)
    waiters: list[asyncio.Future[None]] = field(default_factory=list)


class EventCacheWriteCoordinator(Protocol):
    """Runtime-facing coordinator contract for ordered advisory cache writes."""

    def queue_room_update(
        self,
        room_id: str,
        update_coro_factory: _UpdateCoroFactory,
        *,
        name: str,
        log_exceptions: bool = True,
        emit_timing: bool = True,
        coalesce_key: _CoalesceKey | None = None,
        coalesce_log_context: dict[str, object] | None = None,
    ) -> asyncio.Task[object]:
        """Queue one room-scoped update behind any active predecessor."""

    async def run_room_update(
        self,
        room_id: str,
        update_coro_factory: _UpdateCoroFactory,
        *,
        name: str,
    ) -> object:
        """Run one room-scoped update through the same ordered barrier."""

    def queue_thread_update(
        self,
        room_id: str,
        thread_id: str,
        update_coro_factory: _UpdateCoroFactory,
        *,
        name: str,
        log_exceptions: bool = True,
        emit_timing: bool = False,
        coalesce_key: _CoalesceKey | None = None,
        coalesce_log_context: dict[str, object] | None = None,
    ) -> asyncio.Task[object]:
        """Queue one thread-scoped update behind same-thread and room-wide predecessors."""

    async def run_thread_update(
        self,
        room_id: str,
        thread_id: str,
        update_coro_factory: _UpdateCoroFactory,
        *,
        name: str,
        ignore_cancelled_room_fences: bool = False,
    ) -> object:
        """Run one thread-scoped update through the ordered thread barrier."""

    async def wait_for_room_idle(self, room_id: str) -> None:
        """Wait for one room's queued updates to drain."""

    async def wait_for_thread_idle(
        self,
        room_id: str,
        thread_id: str,
        *,
        ignore_cancelled_room_fences: bool = False,
    ) -> None:
        """Wait for room-wide and same-thread queued updates to drain.

        Set ``ignore_cancelled_room_fences`` only for read-style callers that can
        safely bypass cancelled room fences preserving write ordering.
        """

    async def close(self) -> None:
        """Drain and tear down the coordinator."""


@dataclass
class _EventCacheWriteCoordinator:
    """Serialize same-room advisory cache writes across the whole runtime."""

    logger: structlog.stdlib.BoundLogger
    background_task_owner: object = field(default_factory=object)
    _room_states: dict[str, _RoomSchedulerState] = field(default_factory=dict, init=False)
    _room_update_tasks: dict[str, _UpdateTask] = field(default_factory=dict, init=False)
    _thread_update_tasks: dict[tuple[str, str], _UpdateTask] = field(default_factory=dict, init=False)
    _thread_update_tasks_by_room: dict[str, dict[str, _UpdateTask]] = field(
        default_factory=dict,
        init=False,
    )
    _next_sequence: int = field(default=0, init=False)

    def _next_entry_sequence(self) -> int:
        sequence = self._next_sequence
        self._next_sequence += 1
        return sequence

    def _pending_tasks(self, tasks: tuple[_UpdateTask, ...]) -> set[_UpdateTask]:
        return {task for task in tasks if not task.done()}

    def _pending_chain_length(self, tasks: tuple[_UpdateTask, ...]) -> int:
        return len(self._pending_tasks(tasks))

    def _pending_entry_tasks(self, entries: list[_RoomQueueEntry]) -> tuple[_UpdateTask, ...]:
        tasks = [entry.task for entry in entries if isinstance(entry, _QueuedUpdate) and not entry.task.done()]
        return tuple(dict.fromkeys(tasks))

    def _room_pending_tasks(self, room_id: str) -> tuple[_UpdateTask, ...]:
        tasks = list(self._fallback_room_tasks(room_id))
        state = self._room_states.get(room_id)
        if state is not None:
            tasks.extend(self._pending_entry_tasks(state.entries))
        return tuple(dict.fromkeys(tasks))

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

    def _room_state(self, room_id: str) -> _RoomSchedulerState:
        return self._room_states.setdefault(room_id, _RoomSchedulerState())

    def _find_entry_index(
        self,
        entries: list[_RoomQueueEntry],
        target: _RoomQueueEntry,
    ) -> int | None:
        for index, entry in enumerate(entries):
            if entry is target:
                return index
        return None

    def _prune_done_task_maps(self, room_id: str) -> None:
        room_task = self._room_update_tasks.get(room_id)
        if room_task is not None and room_task.done():
            self._room_update_tasks.pop(room_id, None)

        room_threads = self._thread_update_tasks_by_room.get(room_id)
        if room_threads is None:
            return

        for thread_id, task in list(room_threads.items()):
            if not task.done():
                continue
            room_threads.pop(thread_id, None)
            self._thread_update_tasks.pop((room_id, thread_id), None)

        if not room_threads:
            self._thread_update_tasks_by_room.pop(room_id, None)

    def _wake_waiters(self, room_id: str) -> None:
        state = self._room_states.get(room_id)
        if state is None:
            return
        waiters, state.waiters = state.waiters, []
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(None)

    def _discard_waiter(self, room_id: str, waiter: asyncio.Future[None]) -> None:
        state = self._room_states.get(room_id)
        if state is None:
            return
        state.waiters = [existing for existing in state.waiters if existing is not waiter]

    def _cleanup_room_state(self, room_id: str) -> None:
        self._prune_done_task_maps(room_id)
        state = self._room_states.get(room_id)
        if state is None:
            return
        if state.entries or state.active_room is not None or state.active_threads or state.waiters:
            return
        self._room_states.pop(room_id, None)

    def _start_entry(
        self,
        room_id: str,
        state: _RoomSchedulerState,
        entry: _QueuedUpdate,
    ) -> None:
        if entry.started or entry.task.done():
            return

        entry.started = True
        if entry.kind == "room":
            state.active_room = entry
            self._room_update_tasks[room_id] = entry.task
        else:
            assert entry.thread_id is not None
            state.active_threads[entry.thread_id] = entry
            self._thread_update_tasks[(room_id, entry.thread_id)] = entry.task
            self._thread_update_tasks_by_room.setdefault(room_id, {})[entry.thread_id] = entry.task

        if not entry.start_signal.done():
            entry.start_signal.set_result(None)

    def _drop_leading_room_fences(self, state: _RoomSchedulerState) -> None:
        while state.entries and isinstance(state.entries[0], _QueuedRoomFence):
            state.entries.pop(0)

    def _reevaluate_entry(
        self,
        room_id: str,
        state: _RoomSchedulerState,
        entry: _RoomQueueEntry,
        *,
        room_barrier_pending: bool,
        cancelled_room_fence_pending: bool,
        same_thread_predecessor_pending: bool,
    ) -> tuple[bool, bool]:
        if isinstance(entry, _QueuedRoomFence):
            return room_barrier_pending, True

        if entry.started:
            return room_barrier_pending or entry.kind == "room", cancelled_room_fence_pending

        if entry.kind == "room":
            if state.active_room is None and not state.active_threads:
                self._start_entry(room_id, state, entry)
            return True, cancelled_room_fence_pending

        assert entry.thread_id is not None
        cancelled_room_fence_blocks_entry = cancelled_room_fence_pending and not entry.ignore_cancelled_room_fences
        if (
            room_barrier_pending
            or cancelled_room_fence_blocks_entry
            or same_thread_predecessor_pending
            or state.active_room is not None
        ):
            return room_barrier_pending, cancelled_room_fence_pending
        if entry.thread_id in state.active_threads:
            return room_barrier_pending, cancelled_room_fence_pending
        self._start_entry(room_id, state, entry)
        return room_barrier_pending, cancelled_room_fence_pending

    def _reevaluate_room(self, room_id: str) -> None:
        state = self._room_states.get(room_id)
        if state is None:
            self._prune_done_task_maps(room_id)
            return

        self._drop_leading_room_fences(state)

        room_barrier_pending = False
        cancelled_room_fence_pending = False
        queued_thread_predecessors: set[str] = set()
        for entry in state.entries:
            same_thread_predecessor_pending = (
                isinstance(entry, _QueuedUpdate)
                and entry.kind == "thread"
                and entry.thread_id is not None
                and entry.thread_id in queued_thread_predecessors
            )
            room_barrier_pending, cancelled_room_fence_pending = self._reevaluate_entry(
                room_id,
                state,
                entry,
                room_barrier_pending=room_barrier_pending,
                cancelled_room_fence_pending=cancelled_room_fence_pending,
                same_thread_predecessor_pending=same_thread_predecessor_pending,
            )
            if (
                isinstance(entry, _QueuedUpdate)
                and entry.kind == "thread"
                and entry.thread_id is not None
                and not entry.task.done()
            ):
                queued_thread_predecessors.add(entry.thread_id)

        self._cleanup_room_state(room_id)

    def _coalescible_pending_entry(
        self,
        state: _RoomSchedulerState,
        *,
        kind: typing.Literal["room", "thread"],
        thread_id: str | None,
        coalesce_key: _CoalesceKey,
    ) -> _QueuedUpdate | None:
        for entry in reversed(state.entries):
            if not isinstance(entry, _QueuedUpdate):
                continue
            if entry.started or entry.task.done():
                continue
            same_order_lane = kind == "room" or entry.kind == "room" or entry.thread_id == thread_id
            if not same_order_lane:
                continue
            if entry.kind == kind and entry.thread_id == thread_id and entry.coalesce_key == coalesce_key:
                return entry
            return None
        return None

    def _coalesce_pending_update(
        self,
        state: _RoomSchedulerState,
        *,
        kind: typing.Literal["room", "thread"],
        thread_id: str | None,
        update_coro_factory: _UpdateCoroFactory,
        coalesce_key: _CoalesceKey | None,
        coalesce_log_context: dict[str, object] | None,
    ) -> asyncio.Task[object] | None:
        if coalesce_key is None:
            return None
        entry = self._coalescible_pending_entry(
            state,
            kind=kind,
            thread_id=thread_id,
            coalesce_key=coalesce_key,
        )
        if entry is None:
            return None
        entry.update_state.update_coro_factory = update_coro_factory
        entry.update_state.coalesced_update_count += 1
        entry.update_state.coalesce_log_context = dict(coalesce_log_context or {})
        return entry.task

    def _log_coalesced_update_if_needed(
        self,
        *,
        room_id: str,
        thread_id: str | None,
        kind: typing.Literal["room", "thread"],
        name: str,
        update_state: _QueuedUpdateState,
    ) -> None:
        dropped_update_count = update_state.coalesced_update_count
        if dropped_update_count <= 0:
            return
        log_context = {
            "room_id": room_id,
            "barrier_kind": kind,
            "operation": name,
            "coalesced_update_count": dropped_update_count,
            "dropped_update_count": dropped_update_count,
            **update_state.coalesce_log_context,
        }
        if thread_id is not None:
            log_context["thread_id"] = thread_id
        self.logger.info("Coalesced outbound streaming edit cache updates", **log_context)

    def _release_active_entry(
        self,
        room_id: str,
        state: _RoomSchedulerState | None,
        entry: _QueuedUpdate,
    ) -> None:
        if entry.kind == "room":
            if state is not None and state.active_room is entry:
                state.active_room = None
            if self._room_update_tasks.get(room_id) is entry.task:
                self._room_update_tasks.pop(room_id, None)
            return

        assert entry.thread_id is not None
        if state is not None and state.active_threads.get(entry.thread_id) is entry:
            state.active_threads.pop(entry.thread_id, None)
        key = (room_id, entry.thread_id)
        if self._thread_update_tasks.get(key) is entry.task:
            self._thread_update_tasks.pop(key, None)
        room_threads = self._thread_update_tasks_by_room.get(room_id)
        if room_threads is not None and room_threads.get(entry.thread_id) is entry.task:
            room_threads.pop(entry.thread_id, None)
            if not room_threads:
                self._thread_update_tasks_by_room.pop(room_id, None)

    def _remove_finished_entry(
        self,
        state: _RoomSchedulerState,
        entry: _QueuedUpdate,
    ) -> None:
        index = self._find_entry_index(state.entries, entry)
        if index is None:
            return
        if entry.kind == "room" and not entry.started and entry.task.cancelled():
            state.entries[index] = _QueuedRoomFence(sequence=entry.sequence)
            return
        state.entries.pop(index)

    def _finish_entry(
        self,
        room_id: str,
        entry: _QueuedUpdate,
    ) -> None:
        state = self._room_states.get(room_id)
        self._release_active_entry(room_id, state, entry)

        if state is None:
            self._cleanup_room_state(room_id)
            return

        self._remove_finished_entry(state, entry)
        self._reevaluate_room(room_id)
        self._wake_waiters(room_id)
        self._cleanup_room_state(room_id)

    def _queue_update(
        self,
        *,
        room_id: str,
        thread_id: str | None,
        kind: typing.Literal["room", "thread"],
        update_coro_factory: _UpdateCoroFactory,
        name: str,
        log_exceptions: bool,
        emit_timing: bool = False,
        ignore_cancelled_room_fences: bool = False,
        coalesce_key: _CoalesceKey | None = None,
        coalesce_log_context: dict[str, object] | None = None,
    ) -> asyncio.Task[object]:
        room_state = self._room_state(room_id)
        coalesced_task = self._coalesce_pending_update(
            room_state,
            kind=kind,
            thread_id=thread_id,
            update_coro_factory=update_coro_factory,
            coalesce_key=coalesce_key,
            coalesce_log_context=coalesce_log_context,
        )
        if coalesced_task is not None:
            return coalesced_task

        update_state = _QueuedUpdateState(
            update_coro_factory=update_coro_factory,
            coalesce_log_context=dict(coalesce_log_context or {}),
        )
        instrument_timing = emit_timing and timing_enabled()
        predecessor_count = self._pending_chain_length(self._pending_entry_tasks(room_state.entries))
        loop = asyncio.get_running_loop()
        start_signal: asyncio.Future[None] = loop.create_future()

        if not instrument_timing:

            async def run_when_scheduled() -> object:
                await start_signal
                self._log_coalesced_update_if_needed(
                    room_id=room_id,
                    thread_id=thread_id,
                    kind=kind,
                    name=name,
                    update_state=update_state,
                )
                return await update_state.update_coro_factory()

        else:
            queued_at = time.perf_counter()

            async def run_when_scheduled() -> object:
                outcome = "ok"
                update_started: float | None = None
                try:
                    await start_signal
                    update_started = time.perf_counter()
                    self._log_coalesced_update_if_needed(
                        room_id=room_id,
                        thread_id=thread_id,
                        kind=kind,
                        name=name,
                        update_state=update_state,
                    )
                    return await update_state.update_coro_factory()
                except asyncio.CancelledError:
                    outcome = "cancelled"
                    raise
                except Exception:
                    outcome = "error"
                    raise
                finally:
                    finished = time.perf_counter()
                    total_ms = round((finished - queued_at) * 1000, 1)
                    if update_started is None:
                        predecessor_wait_ms = total_ms
                        update_run_ms = 0.0
                    else:
                        predecessor_wait_ms = round((update_started - queued_at) * 1000, 1)
                        update_run_ms = round((finished - update_started) * 1000, 1)
                    coalescing_context: dict[str, object] = {}
                    if update_state.coalesced_update_count:
                        coalescing_context = {
                            "coalesced_update_count": update_state.coalesced_update_count,
                            "dropped_update_count": update_state.coalesced_update_count,
                            **update_state.coalesce_log_context,
                        }
                    emit_timing_event(
                        "Event cache update timing",
                        barrier_kind=kind,
                        room_id=room_id,
                        thread_id=thread_id,
                        operation=name,
                        predecessor_count=predecessor_count,
                        queued_behind_predecessor=predecessor_count > 0,
                        predecessor_wait_ms=predecessor_wait_ms,
                        update_run_ms=update_run_ms,
                        total_ms=total_ms,
                        outcome=outcome,
                        **coalescing_context,
                    )

        task = create_background_task(
            run_when_scheduled(),
            name=name,
            owner=self.background_task_owner,
            log_exceptions=log_exceptions,
        )
        entry = _QueuedUpdate(
            sequence=self._next_entry_sequence(),
            kind=kind,
            task=task,
            start_signal=start_signal,
            update_state=update_state,
            thread_id=thread_id,
            ignore_cancelled_room_fences=ignore_cancelled_room_fences,
            coalesce_key=coalesce_key,
        )

        room_state.entries.append(entry)
        task.add_done_callback(lambda _done_task, queued_entry=entry: self._finish_entry(room_id, queued_entry))
        self._reevaluate_room(room_id)
        return task

    async def _await_idle_task(
        self,
        pending_task: _UpdateTask,
        *,
        room_id: str,
        log_message: str,
        thread_id: str | None = None,
    ) -> None:
        try:
            await pending_task
        except asyncio.CancelledError:
            current_task = asyncio.current_task()
            if current_task is not None and current_task.cancelling():
                raise
        except Exception as exc:
            log_context: dict[str, object] = {"room_id": room_id, "error": str(exc)}
            if thread_id is not None:
                log_context["thread_id"] = thread_id
            self.logger.debug(log_message, **log_context)

    def _room_is_idle(self, room_id: str) -> bool:
        self._prune_done_task_maps(room_id)
        state = self._room_states.get(room_id)
        if state is not None and state.entries:
            return False
        if self._room_update_tasks.get(room_id) is not None:
            return False
        room_threads = self._thread_update_tasks_by_room.get(room_id)
        return not room_threads

    def _thread_is_idle(
        self,
        room_id: str,
        thread_id: str,
        *,
        ignore_cancelled_room_fences: bool = False,
    ) -> bool:
        self._prune_done_task_maps(room_id)
        state = self._room_states.get(room_id)
        if state is not None:
            for entry in state.entries:
                if isinstance(entry, _QueuedRoomFence):
                    if ignore_cancelled_room_fences:
                        continue
                    return False
                if entry.kind == "room":
                    return False
                if entry.thread_id == thread_id:
                    return False
        if self._room_update_tasks.get(room_id) is not None:
            return False
        return self._thread_update_tasks.get((room_id, thread_id)) is None

    def _fallback_room_tasks(self, room_id: str) -> tuple[_UpdateTask, ...]:
        pending_tasks: list[_UpdateTask] = []
        room_task = self._room_update_tasks.get(room_id)
        if room_task is not None and not room_task.done():
            pending_tasks.append(room_task)
        room_threads = self._thread_update_tasks_by_room.get(room_id, {})
        pending_tasks.extend(task for task in room_threads.values() if not task.done())
        return tuple(dict.fromkeys(pending_tasks))

    def _fallback_thread_tasks(self, room_id: str, thread_id: str) -> tuple[_UpdateTask, ...]:
        pending_tasks: list[_UpdateTask] = []
        room_task = self._room_update_tasks.get(room_id)
        if room_task is not None and not room_task.done():
            pending_tasks.append(room_task)
        thread_task = self._thread_update_tasks.get((room_id, thread_id))
        if thread_task is not None and not thread_task.done():
            pending_tasks.append(thread_task)
        return tuple(dict.fromkeys(pending_tasks))

    def queue_room_update(
        self,
        room_id: str,
        update_coro_factory: _UpdateCoroFactory,
        *,
        name: str,
        log_exceptions: bool = True,
        emit_timing: bool = True,
        coalesce_key: _CoalesceKey | None = None,
        coalesce_log_context: dict[str, object] | None = None,
    ) -> asyncio.Task[object]:
        """Schedule one room-scoped cache update behind any active predecessor."""
        return self._queue_update(
            room_id=room_id,
            thread_id=None,
            kind="room",
            update_coro_factory=update_coro_factory,
            name=name,
            log_exceptions=log_exceptions,
            emit_timing=emit_timing,
            coalesce_key=coalesce_key,
            coalesce_log_context=coalesce_log_context,
        )

    async def run_room_update(
        self,
        room_id: str,
        update_coro_factory: _UpdateCoroFactory,
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
        update_coro_factory: _UpdateCoroFactory,
        *,
        name: str,
        log_exceptions: bool = True,
        emit_timing: bool = False,
        coalesce_key: _CoalesceKey | None = None,
        coalesce_log_context: dict[str, object] | None = None,
    ) -> asyncio.Task[object]:
        """Schedule one thread-scoped cache update behind room-wide and same-thread predecessors."""
        return self._queue_update(
            room_id=room_id,
            thread_id=thread_id,
            kind="thread",
            update_coro_factory=update_coro_factory,
            name=name,
            log_exceptions=log_exceptions,
            emit_timing=emit_timing,
            coalesce_key=coalesce_key,
            coalesce_log_context=coalesce_log_context,
        )

    async def run_thread_update(
        self,
        room_id: str,
        thread_id: str,
        update_coro_factory: _UpdateCoroFactory,
        *,
        name: str,
        ignore_cancelled_room_fences: bool = False,
    ) -> object:
        """Run one thread-scoped operation through the ordered thread barrier and await its result."""
        return await self._queue_update(
            room_id=room_id,
            thread_id=thread_id,
            kind="thread",
            update_coro_factory=update_coro_factory,
            name=name,
            log_exceptions=False,
            ignore_cancelled_room_fences=ignore_cancelled_room_fences,
        )

    async def _wait_for_room_idle_without_timing(self, room_id: str) -> None:
        while True:
            self._reevaluate_room(room_id)
            if self._room_is_idle(room_id):
                return

            state = self._room_states.get(room_id)
            if state is None or not state.entries:
                for pending_task in self._fallback_room_tasks(room_id):
                    await self._await_idle_task(
                        pending_task,
                        room_id=room_id,
                        log_message="Room cache update failed before room became idle",
                    )
                continue

            waiter = asyncio.get_running_loop().create_future()
            state.waiters.append(waiter)
            self._reevaluate_room(room_id)
            if self._room_is_idle(room_id):
                self._discard_waiter(room_id, waiter)
                return
            try:
                await waiter
            except asyncio.CancelledError:
                self._discard_waiter(room_id, waiter)
                raise

    async def _wait_for_room_idle_with_timing(self, room_id: str) -> None:
        wait_started: float | None = None
        wait_iterations = 0
        pending_tasks_seen: set[_UpdateTask] = set()
        while True:
            self._reevaluate_room(room_id)
            if self._room_is_idle(room_id):
                self._emit_idle_wait_timing(
                    room_id=room_id,
                    wait_started=wait_started,
                    wait_iterations=wait_iterations,
                    pending_task_count=len(pending_tasks_seen),
                )
                return

            if wait_started is None:
                wait_started = time.perf_counter()
            pending_tasks_seen.update(self._room_pending_tasks(room_id))

            state = self._room_states.get(room_id)
            if state is None or not state.entries:
                for pending_task in self._fallback_room_tasks(room_id):
                    await self._await_idle_task(
                        pending_task,
                        room_id=room_id,
                        log_message="Room cache update failed before room became idle",
                    )
                wait_iterations += 1
                continue

            waiter = asyncio.get_running_loop().create_future()
            state.waiters.append(waiter)
            self._reevaluate_room(room_id)
            if self._room_is_idle(room_id):
                self._discard_waiter(room_id, waiter)
                self._emit_idle_wait_timing(
                    room_id=room_id,
                    wait_started=wait_started,
                    wait_iterations=wait_iterations,
                    pending_task_count=len(pending_tasks_seen),
                )
                return
            try:
                await waiter
            except asyncio.CancelledError:
                self._discard_waiter(room_id, waiter)
                raise
            wait_iterations += 1

    async def wait_for_room_idle(self, room_id: str) -> None:
        """Wait for the currently queued same-room update chain to drain."""
        if timing_enabled():
            await self._wait_for_room_idle_with_timing(room_id)
            return
        await self._wait_for_room_idle_without_timing(room_id)

    async def wait_for_thread_idle(
        self,
        room_id: str,
        thread_id: str,
        *,
        ignore_cancelled_room_fences: bool = False,
    ) -> None:
        """Wait for room-wide and same-thread queued updates to drain.

        Set ``ignore_cancelled_room_fences`` only for read-style callers that can
        safely bypass cancelled room fences preserving write ordering.
        """
        while True:
            self._reevaluate_room(room_id)
            if self._thread_is_idle(
                room_id,
                thread_id,
                ignore_cancelled_room_fences=ignore_cancelled_room_fences,
            ):
                return

            state = self._room_states.get(room_id)
            if state is None or not state.entries:
                for pending_task in self._fallback_thread_tasks(room_id, thread_id):
                    await self._await_idle_task(
                        pending_task,
                        room_id=room_id,
                        thread_id=thread_id,
                        log_message="Thread cache update failed before thread became idle",
                    )
                continue

            waiter = asyncio.get_running_loop().create_future()
            state.waiters.append(waiter)
            self._reevaluate_room(room_id)
            if self._thread_is_idle(
                room_id,
                thread_id,
                ignore_cancelled_room_fences=ignore_cancelled_room_fences,
            ):
                self._discard_waiter(room_id, waiter)
                return
            try:
                await waiter
            except asyncio.CancelledError:
                self._discard_waiter(room_id, waiter)
                raise

    async def close(self) -> None:
        """Drain any queued cache writes for this coordinator."""
        await wait_for_background_tasks(timeout=5.0, owner=self.background_task_owner)
        self._room_states.clear()
        self._room_update_tasks.clear()
        self._thread_update_tasks.clear()
        self._thread_update_tasks_by_room.clear()
