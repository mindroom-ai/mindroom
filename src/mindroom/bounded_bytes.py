"""Bounded collection of asynchronous byte streams."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterable


class ByteLimitExceededError(ValueError):
    """A byte stream exceeded the caller's collection limit."""


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
