"""Explicit response and accepted-operation references for resource cleanup."""

from __future__ import annotations

import threading
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

from mindroom.background_tasks import run_coroutine_until_complete

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterator

_RESOURCES: ContextVar[ExecutionResources | None] = ContextVar("tool_job_resources", default=None)


class ExecutionResourceReference:
    """One idempotent ownership reference acquired before durable admission."""

    def __init__(self, owner: ExecutionResources) -> None:
        self._owner = owner
        self._released = False

    async def release(self) -> None:
        """Release once, draining cleanup when this was the final owner."""
        if not self._released:
            self._released = True
            await self._owner.release()


class ExecutionResources:
    """Retain concrete cleanup callbacks until the parent and all jobs settle."""

    def __init__(self) -> None:
        self._references = 1
        self._lock = threading.Lock()
        self._callbacks: list[Callable[[], Awaitable[None]]] = []
        self._keys: set[int] = set()
        self._parent_live = True

    def acquire(self) -> ExecutionResourceReference:
        """Acquire an accepted operation's independent reference."""
        with self._lock:
            if self._references == 0:
                msg = "Execution resources have already closed"
                raise RuntimeError(msg)
            self._references += 1
        return ExecutionResourceReference(self)

    def defer(self, callback: Callable[[], Awaitable[None]], resource: object | None = None) -> bool:
        """Transfer exact cleanup when accepted execution still owns this lease."""
        with self._lock:
            if self._references <= int(self._parent_live):
                return False
            if resource is None or id(resource) not in self._keys:
                self._callbacks.append(callback)
                if resource is not None:
                    self._keys.add(id(resource))
            return True

    async def release_parent(self) -> None:
        """Release the response without blocking on its detached operations."""
        with self._lock:
            self._parent_live = False
        await self.release()

    async def release(self) -> None:
        """Drain captured resource cleanup after the final reference settles."""
        with self._lock:
            self._references -= 1
            callbacks = self._callbacks if self._references == 0 else []
            if callbacks:
                self._callbacks = []
        if callbacks:
            await run_coroutine_until_complete(_drain_cleanup(callbacks))


def current_execution_resources() -> ExecutionResources | None:
    """Return the resources belonging to this exact response or accepted job."""
    return _RESOURCES.get()


@contextmanager
def bind_execution_resources(resources: ExecutionResources) -> Iterator[None]:
    """Bind only one operation or stream pull, never across a stream yield."""
    token = _RESOURCES.set(resources)
    try:
        yield
    finally:
        _RESOURCES.reset(token)


@asynccontextmanager
async def execution_resources() -> AsyncIterator[ExecutionResources]:
    """Own response resources while accepted children retain independent refs."""
    resources = ExecutionResources()
    with bind_execution_resources(resources):
        try:
            yield resources
        finally:
            await run_coroutine_until_complete(resources.release_parent())


def defer_execution_cleanup(callback: Callable[[], Awaitable[None]], *, resource: object | None = None) -> bool:
    """Return whether live accepted work has taken ownership of this close."""
    owner = current_execution_resources()
    return owner is not None and owner.defer(callback, resource)


async def _drain_cleanup(callbacks: list[Callable[[], Awaitable[None]]]) -> None:
    """Finish the entire captured release batch before cancellation escapes."""
    failures = []
    for callback in callbacks:
        try:
            await callback()
        except Exception as error:
            failures.append(error)
    if failures:
        msg = "Tool execution resource cleanup failed"
        raise ExceptionGroup(msg, failures)
