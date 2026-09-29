"""Bounded collection of byte streams."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterable, Iterable


class ByteLimitExceededError(ValueError):
    """A byte stream exceeded the caller's collection limit."""


def _append_within_limit(body: bytearray, chunk: bytes, max_bytes: int) -> None:
    if len(chunk) > max_bytes - len(body):
        message = f"Byte stream exceeds {max_bytes} bytes"
        raise ByteLimitExceededError(message)
    body.extend(chunk)


async def collect_bounded_bytes(chunks: AsyncIterable[bytes], *, max_bytes: int) -> bytes:
    """Collect through EOF without buffering an overflowing chunk.

    Iterator failures and cancellation propagate unchanged.
    The caller owns the stream's lifetime.
    """
    body = bytearray()
    async for chunk in chunks:
        _append_within_limit(body, chunk, max_bytes)
    return bytes(body)


def collect_bounded_bytes_sync(chunks: Iterable[bytes], *, max_bytes: int) -> bytes:
    """Collect a synchronous stream through EOF without buffering an overflowing chunk.

    Iterator failures propagate unchanged.
    The caller owns the stream's lifetime.
    """
    body = bytearray()
    for chunk in chunks:
        _append_within_limit(body, chunk, max_bytes)
    return bytes(body)
