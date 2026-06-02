"""Tests for the advisory file-lock primitives."""
# ruff: noqa: D103

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING

import pytest

from mindroom.file_locks import advisory_file_lock, async_exclusive_file_lock

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_async_exclusive_file_lock_serializes_in_process(tmp_path: Path) -> None:
    # Separate async_exclusive_file_lock calls open distinct descriptions; flock
    # contends across them, so only one critical section runs at a time.
    lock_path = tmp_path / "index.lock"
    active = 0
    max_active = 0

    async def worker() -> None:
        nonlocal active, max_active
        async with async_exclusive_file_lock(lock_path, poll_seconds=0.01):
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.05)
            active -= 1

    await asyncio.gather(*(worker() for _ in range(3)))

    assert max_active == 1


@pytest.mark.asyncio
async def test_async_exclusive_file_lock_released_on_cancellation(tmp_path: Path) -> None:
    lock_path = tmp_path / "index.lock"
    holding = asyncio.Event()

    async def holder() -> None:
        async with async_exclusive_file_lock(lock_path, poll_seconds=0.01):
            holding.set()
            await asyncio.sleep(3600)

    task = asyncio.create_task(holder())
    await holding.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async def acquire_once() -> bool:
        async with async_exclusive_file_lock(lock_path, poll_seconds=0.01):
            return True

    # A cancelled waiter/holder must release the lock, so this acquires without hanging.
    assert await asyncio.wait_for(acquire_once(), timeout=2)


def test_advisory_file_lock_exclusive_blocks_second_holder(tmp_path: Path) -> None:
    lock_path = tmp_path / "state.lock"
    order: list[str] = []
    second_attempting = threading.Event()

    def second() -> None:
        second_attempting.set()
        with advisory_file_lock(lock_path):
            order.append("second-acquire")

    thread = threading.Thread(target=second)
    with advisory_file_lock(lock_path):
        order.append("first-acquire")
        thread.start()
        assert second_attempting.wait(timeout=2)
        assert order == ["first-acquire"]
        order.append("first-release")

    thread.join(timeout=2)
    assert not thread.is_alive()
    assert order == ["first-acquire", "first-release", "second-acquire"]


def test_advisory_file_lock_shared_allows_concurrent_readers(tmp_path: Path) -> None:
    lock_path = tmp_path / "state.lock"
    barrier = threading.Barrier(2)

    def reader() -> None:
        with advisory_file_lock(lock_path, exclusive=False):
            # Both readers must hold the shared lock simultaneously; if shared locks
            # excluded each other this barrier would time out and break.
            barrier.wait(timeout=2)

    threads = [threading.Thread(target=reader), threading.Thread(target=reader)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert barrier.broken is False
