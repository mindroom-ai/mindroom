"""Tests for worker progress routing from sandbox threads back to streaming."""

from __future__ import annotations

import asyncio
import contextvars
import threading

import pytest

from mindroom.timing import DispatchPipelineTiming, bind_reply_pipeline_timing, reply_timing_binding
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
        reply_timing=None,
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
        reply_timing=None,
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


def _progress(phase: str, elapsed_seconds: float) -> WorkerReadyProgress:
    return WorkerReadyProgress(
        phase=phase,
        worker_key="worker-a",
        backend_name="kubernetes",
        elapsed_seconds=elapsed_seconds,
    )


def test_progress_sink_keeps_the_longest_ready_or_failed_wait() -> None:
    """Only finished starts count, and the reply keeps the longest one, whether it became ready or failed."""
    timing = DispatchPipelineTiming(source_event_id="$event", room_id="!room")
    with bind_reply_pipeline_timing(timing):
        sink = _make_progress_sink(None, reply_timing=reply_timing_binding(), tool_name="shell", function_name="run")
        sink(_progress("cold_start", 9.0))
        sink(_progress("ready", 2.0))
        sink(_progress("failed", 5.0))
        sink(_progress("ready", 1.0))

    assert timing.metadata["worker_wait_ms"] == 5000.0


def test_progress_sink_stops_noting_once_the_reply_is_delivered() -> None:
    """A background job keeps the reply's context after delivery, but its later worker start leaves the reply alone."""
    timing = DispatchPipelineTiming(source_event_id="$event", room_id="!room")
    with bind_reply_pipeline_timing(timing):
        detached_job_context = contextvars.copy_context()
        captured_sink = _make_progress_sink(
            None,
            reply_timing=reply_timing_binding(),
            tool_name="shell",
            function_name="run",
        )

    def start_worker_later() -> None:
        sink = _make_progress_sink(None, reply_timing=reply_timing_binding(), tool_name="shell", function_name="run")
        sink(_progress("ready", 3.0))

    detached_job_context.run(start_worker_later)
    captured_sink(_progress("failed", 4.0))

    assert "worker_wait_ms" not in timing.metadata
