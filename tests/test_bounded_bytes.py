"""Bounded byte collection preserves stream data, failures, and cancellation."""

import asyncio
from collections.abc import AsyncIterator

import pytest

from mindroom.bounded_bytes import ByteLimitExceededError, collect_bounded_bytes


async def _chunks(values: list[bytes]) -> AsyncIterator[bytes]:
    for value in values:
        yield value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chunks", "max_bytes", "expected"),
    [
        ([], 0, b""),
        ([b"", b""], 0, b""),
        ([b"abc"], 4, b"abc"),
        ([b"abc"], 3, b"abc"),
        ([b"a", b"", b"bc", b""], 3, b"abc"),
        ([b"\x00\xff", b"\xc3", b"\xa9"], 4, b"\x00\xff\xc3\xa9"),
    ],
)
async def test_collects_all_bytes_through_eof(chunks: list[bytes], max_bytes: int, expected: bytes) -> None:
    """Empty chunks and binary data survive, including an exact-limit body."""
    assert await collect_bounded_bytes(_chunks(chunks), max_bytes=max_bytes) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chunks", "max_bytes"),
    [([b"x"], 0), ([b"abcd"], 3), ([b"a", b"bc", b"d"], 3)],
)
async def test_overflow_stops_reading(chunks: list[bytes], max_bytes: int) -> None:
    """Reject the first overflowing chunk without requesting the next one."""

    async def stream() -> AsyncIterator[bytes]:
        for chunk in chunks:
            yield chunk
        pytest.fail("Read past the overflowing chunk")

    with pytest.raises(ByteLimitExceededError):
        await collect_bounded_bytes(stream(), max_bytes=max_bytes)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError("stream failed"), ValueError("invalid stream")])
async def test_iterator_failure_propagates_unchanged(error: Exception) -> None:
    """A failed read must not return partial data or replace the original error."""

    async def stream() -> AsyncIterator[bytes]:
        yield b"abc"
        raise error

    with pytest.raises(type(error)) as raised:
        await collect_bounded_bytes(stream(), max_bytes=3)
    assert raised.value is error


@pytest.mark.asyncio
async def test_cancellation_reaches_pending_read() -> None:
    """Cancelling collection interrupts its current read and propagates unchanged."""
    reading = asyncio.Event()
    cancelled: list[asyncio.CancelledError] = []

    async def stream() -> AsyncIterator[bytes]:
        yield b"abc"
        reading.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as error:
            cancelled.append(error)
            raise

    task = asyncio.create_task(collect_bounded_bytes(stream(), max_bytes=3))
    await reading.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as raised:
        await task
    assert cancelled == [raised.value]
