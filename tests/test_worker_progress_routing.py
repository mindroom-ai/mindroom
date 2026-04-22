"""Tests for worker progress routing from sandbox threads back to streaming."""

from __future__ import annotations

import asyncio
import threading

import pytest

from mindroom.tool_system.runtime_context import WorkerProgressPump
from mindroom.tool_system.sandbox_proxy import _make_progress_sink
from mindroom.workers.models import WorkerReadyProgress


@pytest.mark.asyncio
async def test_progress_sink_drops_events_after_shutdown() -> None:
    """Shutdown pumps should silently drop worker progress instead of queueing late events."""
    pump = WorkerProgressPump(
        loop=asyncio.get_running_loop(),
        queue=asyncio.Queue(),
        shutdown=threading.Event(),
    )
    pump.shutdown.set()
    sink = _make_progress_sink(
        pump,
        tool_name="shell",
        function_name="run",
    )

    sink(
        WorkerReadyProgress(
            phase="cold_start",
            worker_key="worker-a",
            backend_name="kubernetes",
            elapsed_seconds=2.0,
        ),
    )

    assert pump.queue.empty()


@pytest.mark.asyncio
async def test_progress_sink_drops_loop_close_race() -> None:
    """A loop-closing race between the guard and call_soon_threadsafe should be swallowed."""

    class _ClosingLoop:
        def is_closed(self) -> bool:
            return False

        def call_soon_threadsafe(self, _callback: object, _event: object) -> None:
            error_message = "loop closed"
            raise RuntimeError(error_message)

    pump = WorkerProgressPump(
        loop=_ClosingLoop(),
        queue=asyncio.Queue(),
        shutdown=threading.Event(),
    )
    sink = _make_progress_sink(
        pump,
        tool_name="shell",
        function_name="run",
    )

    sink(
        WorkerReadyProgress(
            phase="waiting",
            worker_key="worker-a",
            backend_name="kubernetes",
            elapsed_seconds=7.0,
        ),
    )

    assert pump.queue.empty()
