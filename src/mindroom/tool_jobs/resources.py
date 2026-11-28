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
_RESPONSE_RESOURCES: ContextVar[ExecutionResources | None] = ContextVar("response_resources", default=None)


class ExecutionResourceReference:
    """One idempotent ownership reference acquired before durable admission."""

    def __init__(self, owner: ExecutionResources) -> None:
        self._owner = owner
        self._released = False

    @property
    def released(self) -> bool:
        """Return whether this reference has already been released."""
        return self._released

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

    def retain(self, resource: object, callback: Callable[[], Awaitable[None]]) -> bool | None:
        """Register lifetime cleanup once; None means the response and its jobs already settled."""
        with self._lock:
            if self._references == 0:
                return None
            key = id(resource)
            if key in self._keys:
                return False
            self._keys.add(key)
            self._callbacks.append(callback)
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


def current_response_resources() -> ExecutionResources | None:
    """Return the reply's resources, even inside a job with its own execution resources."""
    return _RESPONSE_RESOURCES.get() or _RESOURCES.get()


@contextmanager
def bind_execution_resources(
    resources: ExecutionResources,
    *,
    response: bool = False,
    jobs_enabled: bool = True,
) -> Iterator[None]:
    """Bind one operation or stream pull, keeping the response owner reachable inside its jobs."""
    response_token = _RESPONSE_RESOURCES.set(resources) if response else None
    token = _RESOURCES.set(resources) if jobs_enabled else None
    try:
        yield
    finally:
        if token is not None:
            _RESOURCES.reset(token)
        if response_token is not None:
            _RESPONSE_RESOURCES.reset(response_token)


@asynccontextmanager
async def execution_resources(
    *,
    response: bool = False,
    jobs_enabled: bool = True,
) -> AsyncIterator[ExecutionResources]:
    """Own response resources while accepted children retain independent refs."""
    resources = ExecutionResources()
    with bind_execution_resources(resources, response=response, jobs_enabled=jobs_enabled):
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
