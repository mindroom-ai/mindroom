"""Bounded, payload-free diagnostics for suspended Matrix tasks."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from types import coroutine
from typing import TYPE_CHECKING

import pytest

from mindroom.matrix.sync_diagnostics import _capture_sync_task_snapshots

if TYPE_CHECKING:
    from collections.abc import Generator


@coroutine
def _generator_wait(event: asyncio.Event) -> Generator[object, None, None]:
    yield from _generator_leaf(event)


def _generator_leaf(event: asyncio.Event) -> Generator[object, None, None]:
    yield from event.wait().__await__()


async def _nested_wait(event: asyncio.Event, depth: int = 0, *, _payload: str = "example-message-payload") -> None:
    if depth:
        await _nested_wait(event, depth - 1)
    else:
        await event.wait()


async def _legacy_wait(event: asyncio.Event) -> None:
    await _generator_wait(event)


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_snapshots_follow_nested_coroutines_and_generator_waits(legacy: bool) -> None:
    """Show the inner wait without exposing its local values or source text."""
    event = asyncio.Event()
    task = asyncio.create_task(_legacy_wait(event) if legacy else _nested_wait(event), name="matrix_sync_test_agent")
    try:
        await asyncio.sleep(0)
        snapshots = _capture_sync_task_snapshots("test_agent")
        assert len(snapshots) == 1
        snapshot = snapshots[0]
        assert snapshot.task_name == "matrix_sync_test_agent"
        assert snapshot.truncated is False
        if legacy:
            assert any(frame.endswith(":_legacy_wait") for frame in snapshot.await_chain)
            assert any(frame.endswith(":_generator_wait") for frame in snapshot.await_chain)
            assert any(frame.endswith(":_generator_leaf") for frame in snapshot.await_chain)
        else:
            assert any(frame.endswith(":_nested_wait") for frame in snapshot.await_chain)
            assert any(frame.endswith(":wait") for frame in snapshot.await_chain)
        assert snapshot.await_boundary is not None
        assert all("/" not in frame for frame in snapshot.await_chain)
        assert "yield from" not in json.dumps(asdict(snapshot))
        assert "Event object" not in json.dumps(asdict(snapshot))
        assert "example-message-payload" not in json.dumps(asdict(snapshot))
        assert not task.done()
        assert not task.cancelling()
    finally:
        event.set()
        await task


@pytest.mark.asyncio
async def test_snapshots_include_only_exact_owned_task_names() -> None:
    """Exclude other agents, watchdogs, unrelated tasks, and finished tasks."""
    event = asyncio.Event()
    names = [
        "matrix_sync_test_agent",
        "matrix_ingestion_runner_test_agent",
        "matrix_ingestion_pump_test_agent",
        "delivery_recovery_test_agent",
        "matrix_sync_test_agent_other",
        "matrix_sync_other_agent",
        "matrix_sync_watchdog_test_agent",
        "response_test_agent",
    ]
    tasks = [asyncio.create_task(_nested_wait(event), name=name) for name in names]
    finished = asyncio.create_task(asyncio.sleep(0), name="matrix_sync_test_agent")
    try:
        await finished
        snapshots = _capture_sync_task_snapshots("test_agent")
        assert {snapshot.task_name for snapshot in snapshots} == set(names[:4])
        assert len(snapshots) == 4
        assert all(not task.cancelling() for task in tasks)
    finally:
        event.set()
        await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_snapshots_bound_duplicate_tasks_deep_chains_and_strings() -> None:
    """Large agent names and deep waits cannot produce unbounded log records."""
    event = asyncio.Event()
    agent_name = "agent_" + "x" * 500
    tasks = [asyncio.create_task(_nested_wait(event, 100), name=f"matrix_sync_{agent_name}") for _ in range(12)]
    try:
        await asyncio.sleep(0)
        snapshots = _capture_sync_task_snapshots(agent_name)
        assert 0 < len(snapshots) <= 4
        for snapshot in snapshots:
            assert len(snapshot.task_name) <= 240
            assert 0 < len(snapshot.await_chain) <= 32
            assert all(len(frame) <= 240 for frame in snapshot.await_chain)
            assert snapshot.truncated is True
        assert len(json.dumps([asdict(snapshot) for snapshot in snapshots])) < 40_000
    finally:
        event.set()
        await asyncio.gather(*tasks)
