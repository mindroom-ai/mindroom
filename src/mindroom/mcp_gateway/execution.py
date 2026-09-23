"""Bind gateway admission to the complete lifetime of one operation."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


class ExecutionLease:
    """Hold one server-owned slot until dispatch and all retained work finish."""

    def __init__(self, task: asyncio.Task[Any], release: Callable[[ExecutionLease], None]) -> None:
        self._dispatch: asyncio.Task[Any] | None = task
        self._release = release
        self._pending: set[asyncio.Task[Any]] = set()
        self._finished = asyncio.Event()

    def cancel(self) -> None:
        """Cancel only a live dispatch; retained cleanup keeps its ownership."""
        if self._dispatch is not None:
            self._dispatch.cancel()

    def finish(self) -> None:
        """Detach the response task while retaining any unfinished operation work."""
        self._dispatch = None
        self._release_if_finished()

    def _retain(self, task: asyncio.Task[Any]) -> None:
        """Count an owned task once, including tasks started by retained cleanup."""
        if task not in self._pending:
            self._pending.add(task)
            task.add_done_callback(self._task_finished)

    def _task_finished(self, task: asyncio.Task[Any]) -> None:
        self._pending.discard(task)
        self._release_if_finished()

    def _release_if_finished(self) -> None:
        if self._dispatch is None and not self._pending and not self._finished.is_set():
            self._release(self)
            self._finished.set()

    async def wait(self) -> None:
        """Drain this operation before its owning server finishes shutdown."""
        await self._finished.wait()


_EXECUTION: ContextVar[ExecutionLease | None] = ContextVar("mcp_gateway_execution", default=None)


@contextmanager
def execution_scope(lease: ExecutionLease) -> Iterator[None]:
    """Propagate the admission owner through gateway dispatch and retained tasks."""
    token = _EXECUTION.set(lease)
    try:
        yield
    finally:
        _EXECUTION.reset(token)
        lease.finish()


def retain_execution_task(task: asyncio.Task[Any]) -> None:
    """Attach retained work to the current server's operation, when present."""
    if (lease := _EXECUTION.get()) is not None:
        lease._retain(task)

    def finished(completed: asyncio.Task[Any]) -> None:
        if not completed.cancelled():
            completed.exception()

    task.add_done_callback(finished)


async def run_gateway_sync[**P, T](operation: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Stop waiting promptly on cancellation without losing live thread ownership."""
    task = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
    retain_execution_task(task)
    return await asyncio.shield(task)
