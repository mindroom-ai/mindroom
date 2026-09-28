"""Bounded collection of asynchronous and synchronous byte streams."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterable, Iterable


class ByteLimitExceededError(ValueError):
    """A byte stream exceeded the caller's collection limit."""


@dataclass(frozen=True, slots=True)
class BytePrefix:
    """At most a limit's worth of one stream, and whether the stream went on past it."""

    data: bytes
    truncated: bool


async def collect_bounded_bytes(chunks: AsyncIterable[bytes], *, max_bytes: int) -> bytes:
    """Collect through EOF without buffering an overflowing chunk.

    Iterator failures and cancellation propagate unchanged.
    The caller owns the stream's lifetime.
    """
    body = bytearray()
    async for chunk in chunks:
        if len(chunk) > max_bytes - len(body):
            message = f"Byte stream exceeds {max_bytes} bytes"
            raise ByteLimitExceededError(message)
        body.extend(chunk)
    return bytes(body)


def collect_sync_byte_prefix(chunks: Iterable[bytes], *, max_bytes: int) -> BytePrefix:
    """Collect a synchronous stream up to ``max_bytes`` and stop reading at the first chunk that crosses it.

    Iterator failures propagate unchanged.
    The caller owns the stream's lifetime.
    """
    body = bytearray()
    for chunk in chunks:
        if len(chunk) > max_bytes - len(body):
            body.extend(chunk[: max_bytes - len(body)])
            return BytePrefix(bytes(body), truncated=True)
        body.extend(chunk)
    return BytePrefix(bytes(body), truncated=False)
