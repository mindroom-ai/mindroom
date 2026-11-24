"""Tests for worker progress routing from sandbox threads back to streaming."""

from __future__ import annotations

import asyncio
import contextvars
import threading

import pytest

from mindroom.timing import (
    DispatchPipelineTiming,
    ReplyTimingBinding,
    bind_reply_pipeline_timing,
    reply_timing_binding,
)
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


def test_ending_the_binding_waits_for_a_wait_being_noted() -> None:
    """A sink already noting on a worker thread finishes first; nothing is noted once the binding has ended."""
    entered = threading.Event()
    release = threading.Event()

    class _SlowMetadata(dict):
        def __setitem__(self, key: str, value: object) -> None:
            entered.set()
            release.wait(5)
            super().__setitem__(key, value)

    timing = DispatchPipelineTiming(source_event_id="$event", room_id="!room")
    timing.metadata = _SlowMetadata()
    binding = ReplyTimingBinding(timing)
    noter = threading.Thread(target=binding.note_worker_wait, args=(2.0,))
    noter.start()
    assert entered.wait(5)
    ender = threading.Thread(target=binding.end)
    ender.start()
    ender.join(0.2)
    assert ender.is_alive()
    release.set()
    ender.join(5)
    noter.join(5)
    binding.note_worker_wait(3.0)

    assert dict(timing.metadata) == {"worker_wait_ms": 2000.0}
