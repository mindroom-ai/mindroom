"""Production-shaped validation for ISSUE-176 thread-barrier parallelism."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from mindroom.matrix.cache.write_coordinator import _EventCacheWriteCoordinator

ROOM_ID = "!issue-176:localhost"
THREAD_A_ID = "$thread-a:localhost"
THREAD_B_ID = "$thread-b:localhost"


async def _assert_sibling_threads_start_concurrently(coord: _EventCacheWriteCoordinator) -> None:
    started_a = asyncio.Event()
    started_b = asyncio.Event()
    release_a = asyncio.Event()
    release_b = asyncio.Event()

    async def thread_a_update() -> str:
        started_a.set()
        await release_a.wait()
        return "thread-a"

    async def thread_b_update() -> str:
        started_b.set()
        await release_b.wait()
        return "thread-b"

    first = asyncio.create_task(
        coord.run_thread_update(
            ROOM_ID,
            THREAD_A_ID,
            thread_a_update,
            name="measure_thread_update_a",
        ),
    )
    await asyncio.wait_for(started_a.wait(), timeout=1.0)

    second = asyncio.create_task(
        coord.run_thread_update(
            ROOM_ID,
            THREAD_B_ID,
            thread_b_update,
            name="measure_thread_update_b",
        ),
    )

    try:
        await asyncio.wait_for(started_b.wait(), timeout=1.0)
        assert release_a.is_set() is False

        release_a.set()
        release_b.set()
        assert await asyncio.wait_for(asyncio.gather(first, second), timeout=1.0) == ["thread-a", "thread-b"]
    finally:
        release_a.set()
        release_b.set()
        await asyncio.gather(first, second, return_exceptions=True)


async def _assert_room_update_blocks_later_thread(coord: _EventCacheWriteCoordinator) -> None:
    started_a = asyncio.Event()
    started_b = asyncio.Event()
    release_a = asyncio.Event()
    release_b = asyncio.Event()

    async def room_update() -> str:
        started_a.set()
        await release_a.wait()
        return "room-a"

    async def thread_b_update() -> str:
        started_b.set()
        await release_b.wait()
        return "thread-b"

    first = asyncio.create_task(
        coord.run_room_update(
            ROOM_ID,
            room_update,
            name="measure_room_update_a",
        ),
    )
    await asyncio.wait_for(started_a.wait(), timeout=1.0)

    second = asyncio.create_task(
        coord.run_thread_update(
            ROOM_ID,
            THREAD_B_ID,
            thread_b_update,
            name="measure_thread_update_b",
        ),
    )

    try:
        await asyncio.sleep(0)
        assert started_b.is_set() is False

        release_a.set()
        await asyncio.wait_for(started_b.wait(), timeout=1.0)
        release_b.set()
        assert await asyncio.wait_for(asyncio.gather(first, second), timeout=1.0) == ["room-a", "thread-b"]
    finally:
        release_a.set()
        release_b.set()
        await asyncio.gather(first, second, return_exceptions=True)


@pytest.mark.asyncio
async def test_issue_176_network_bound_sibling_thread_updates_run_in_parallel() -> None:
    """Different-thread updates should overlap when the slow work is outside SQLite."""
    coord = _EventCacheWriteCoordinator(
        logger=MagicMock(),
        background_task_owner=object(),
    )
    try:
        await _assert_sibling_threads_start_concurrently(coord)
        await _assert_room_update_blocks_later_thread(coord)
    finally:
        await coord.close()
