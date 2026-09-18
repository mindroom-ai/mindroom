"""Explicit response and accepted-operation references for resource cleanup."""

from __future__ import annotations

import asyncio
import threading
from collections import Counter
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
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


@dataclass
class _SyncConnection:
    resource: object
    close: Callable[[], None]
    thread_id: int
    owners: Counter[ExecutionResources] = field(default_factory=Counter)


@dataclass
class _AsyncConnection:
    resource: object
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    closing: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None
    owners: Counter[ExecutionResources] = field(default_factory=Counter)


_SYNC_CONNECTIONS: dict[int, _SyncConnection] = {}
_ASYNC_CONNECTIONS: dict[int, _AsyncConnection] = {}


def connect_execution_resource(resource: object, connect: Callable[[], None], close: Callable[[], None]) -> None:
    """Reuse a concrete synchronous connection while any response still owns it."""
    owner = current_execution_resources()
    if owner is None:
        msg = "Managed connection requires a resource owner"
        raise RuntimeError(msg)
    connection = _SYNC_CONNECTIONS.get(id(resource))
    if connection is None:
        connect()
        connection = _SyncConnection(resource, close, threading.get_ident())
        _SYNC_CONNECTIONS[id(resource)] = connection
    connection.owners[owner] += 1


def disconnect_execution_resource(resource: object) -> None:
    """Transfer this run's exact connection reference to its accepted children."""
    owner = current_execution_resources()
    assert owner is not None
    connection = _SYNC_CONNECTIONS[id(resource)]

    def close() -> None:
        connection.owners[owner] -= 1
        if connection.owners[owner] == 0:
            del connection.owners[owner]
        if not connection.owners:
            if connection.thread_id != threading.get_ident():
                msg = "Synchronous toolkit cleanup requires its original connection thread"
                raise RuntimeError(msg)
            try:
                connection.close()
            finally:
                _SYNC_CONNECTIONS.pop(id(resource), None)

    async def deferred() -> None:
        close()

    # Each actor acquisition needs a release, even when one parent owns them all.
    if not defer_execution_cleanup(deferred, resource=deferred):
        close()


async def connect_async_execution_resource(
    resource: object,
    connect: Callable[[], Awaitable[None]],
    close: Callable[[], Awaitable[None]],
) -> None:
    """Connect and close task-affine transports in the same persistent owner task."""
    owner = current_execution_resources()
    if owner is None:
        msg = "Managed connection requires a resource owner"
        raise RuntimeError(msg)
    connection = _ASYNC_CONNECTIONS.get(id(resource))
    while connection is not None and connection.closing.is_set():
        assert connection.task is not None
        await run_coroutine_until_complete(_join_connection(connection.task))
        connection = _ASYNC_CONNECTIONS.get(id(resource))
    if connection is None:
        connection = _AsyncConnection(resource)
        _ASYNC_CONNECTIONS[id(resource)] = connection

        async def manage() -> None:
            try:
                await connect()
                connection.ready.set()
                await connection.closing.wait()
            finally:
                connection.ready.set()
                try:
                    await close()
                finally:
                    if _ASYNC_CONNECTIONS.get(id(resource)) is connection:
                        _ASYNC_CONNECTIONS.pop(id(resource))

        connection.task = asyncio.create_task(manage(), name="tool-connection-owner")
    connection.owners[owner] += 1
    await run_coroutine_until_complete(connection.ready.wait())
    if connection.task is not None and connection.task.done():
        connection.task.result()


async def disconnect_async_execution_resource(resource: object) -> None:
    """Release one run while retaining shared and detached connection users."""
    owner = current_execution_resources()
    assert owner is not None
    connection = _ASYNC_CONNECTIONS[id(resource)]

    async def close() -> None:
        connection.owners[owner] -= 1
        if connection.owners[owner] == 0:
            del connection.owners[owner]
        if not connection.owners:
            connection.closing.set()
            if connection.task is not None:
                await run_coroutine_until_complete(_join_connection(connection.task))

    if not defer_execution_cleanup(close, resource=close):
        await close()


async def _join_connection(task: asyncio.Task[None]) -> None:
    await asyncio.shield(task)


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
