"""Async stream helpers for short-lived context-manager bindings."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator as AsyncGeneratorABC
from contextlib import asynccontextmanager, nullcontext
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from mindroom.background_tasks import wait_for_future_until_complete

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from contextlib import AbstractContextManager


@runtime_checkable
class _AsyncClosableIterator(Protocol):
    """Minimal async-iterator surface that can be closed explicitly."""

    async def aclose(self) -> None:
        """Close the async iterator and release any underlying resources."""


async def close_async_stream(
    stream: AsyncIterator[object] | None,
    *,
    context_factory: Callable[[], AbstractContextManager[object]] = nullcontext,
) -> None:
    """Close a supported iterator, binding its owner's context only for the close."""
    if isinstance(stream, (AsyncGeneratorABC, _AsyncClosableIterator)):
        with context_factory():
            await stream.aclose()


@asynccontextmanager
async def closing_async_stream(stream: AsyncIterator[object]) -> AsyncIterator[None]:
    """Close an owned stream before releasing its caller's resources."""
    try:
        yield
    finally:
        await close_async_stream(stream)


def context_bound_async_stream[ChunkT](
    *,
    context_factory: Callable[[], AbstractContextManager[object]],
    stream_factory: Callable[[], AsyncIterator[ChunkT]],
) -> AsyncIterator[ChunkT]:
    """Wrap an async iterator with context bound for factory, pulls, and close only."""

    async def wrapped_stream() -> AsyncIterator[ChunkT]:
        stream: AsyncIterator[ChunkT] | None = None
        try:
            with context_factory():
                stream = stream_factory()
            while True:
                try:
                    with context_factory():
                        chunk = await anext(stream)
                except StopAsyncIteration:
                    return
                yield chunk
        finally:
            await close_async_stream(stream, context_factory=context_factory)

    return wrapped_stream()


async def callback_event_stream[ResultT](
    produce: Callable[[Callable[[object], None]], Awaitable[ResultT]],
) -> AsyncIterator[object]:
    """Stream a native producer's callbacks, then its result, owning cancellation."""
    queue: asyncio.Queue[object] = asyncio.Queue()
    finished = object()

    async def drive() -> ResultT:
        try:
            return await produce(queue.put_nowait)
        finally:
            queue.put_nowait(finished)

    task = asyncio.create_task(drive())
    try:
        while (event := await queue.get()) is not finished:
            yield event
        yield await task
    finally:
        if not task.done():
            task.cancel()
            await wait_for_future_until_complete(asyncio.gather(task, return_exceptions=True))
        elif not task.cancelled():
            # An early-closing consumer may never await the producer above.
            # Observe its failure without replacing the consumer's exception.
            task.exception()
