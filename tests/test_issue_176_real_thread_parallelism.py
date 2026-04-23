"""Production-shaped validation for ISSUE-176 thread-barrier parallelism."""

from __future__ import annotations

import asyncio
import statistics
import time
from unittest.mock import MagicMock

import pytest

from mindroom.matrix.cache.write_coordinator import _EventCacheWriteCoordinator

ROOM_ID = "!issue-176:localhost"
THREAD_A_ID = "$thread-a:localhost"
THREAD_B_ID = "$thread-b:localhost"
SLOW_FETCH_SECONDS = 0.2
MEASUREMENT_RUNS = 3


async def _slow_network_bound_update(label: str) -> str:
    await asyncio.sleep(SLOW_FETCH_SECONDS)
    return label


async def _measure_room_scoped_total_ms(coord: _EventCacheWriteCoordinator) -> float:
    started = time.perf_counter()
    first = asyncio.create_task(
        coord.run_room_update(
            ROOM_ID,
            lambda: _slow_network_bound_update("room-a"),
            name="measure_room_update_a",
        ),
    )
    second = asyncio.create_task(
        coord.run_room_update(
            ROOM_ID,
            lambda: _slow_network_bound_update("room-b"),
            name="measure_room_update_b",
        ),
    )
    assert await asyncio.gather(first, second) == ["room-a", "room-b"]
    return (time.perf_counter() - started) * 1000.0


async def _measure_thread_scoped_total_ms(coord: _EventCacheWriteCoordinator) -> float:
    started = time.perf_counter()
    first = asyncio.create_task(
        coord.run_thread_update(
            ROOM_ID,
            THREAD_A_ID,
            lambda: _slow_network_bound_update("thread-a"),
            name="measure_thread_update_a",
        ),
    )
    second = asyncio.create_task(
        coord.run_thread_update(
            ROOM_ID,
            THREAD_B_ID,
            lambda: _slow_network_bound_update("thread-b"),
            name="measure_thread_update_b",
        ),
    )
    assert await asyncio.gather(first, second) == ["thread-a", "thread-b"]
    return (time.perf_counter() - started) * 1000.0


@pytest.mark.asyncio
async def test_issue_176_network_bound_sibling_thread_updates_run_in_parallel() -> None:
    """Different-thread updates should overlap when the slow work is outside SQLite."""
    coord = _EventCacheWriteCoordinator(
        logger=MagicMock(),
        background_task_owner=object(),
    )
    try:
        room_scoped_samples_ms = [await _measure_room_scoped_total_ms(coord) for _ in range(MEASUREMENT_RUNS)]
        thread_scoped_samples_ms = [await _measure_thread_scoped_total_ms(coord) for _ in range(MEASUREMENT_RUNS)]
    finally:
        await coord.close()

    per_coro_sleep_ms = SLOW_FETCH_SECONDS * 1000.0
    room_scoped_total_ms = statistics.median(room_scoped_samples_ms)
    thread_scoped_total_ms = statistics.median(thread_scoped_samples_ms)

    assert thread_scoped_total_ms < per_coro_sleep_ms * 1.5
    assert room_scoped_total_ms > per_coro_sleep_ms * 1.8
