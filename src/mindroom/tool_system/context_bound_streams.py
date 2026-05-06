"""Async stream helpers for short-lived context-manager bindings."""

from __future__ import annotations

from collections.abc import AsyncGenerator as AsyncGeneratorABC
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from contextlib import AbstractContextManager


@runtime_checkable
class _AsyncClosableIterator(Protocol):
    """Minimal async-iterator surface that can be closed explicitly."""

    async def aclose(self) -> None:
        """Close the async iterator and release any underlying resources."""


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
            if isinstance(stream, (AsyncGeneratorABC, _AsyncClosableIterator)):
                with context_factory():
                    await stream.aclose()

    return wrapped_stream()
