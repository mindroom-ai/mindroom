"""Event-loop stall detector behavior."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from mindroom.constants import RuntimePaths
from mindroom.event_loop_stall import (
    _DEFAULT_EVENT_LOOP_STALL_THRESHOLD_SECONDS,
    _EVENT_LOOP_STALL_THRESHOLD_ENV,
    EventLoopStallDetector,
    _event_loop_stall_threshold_seconds,
    start_event_loop_stall_detector,
)

_STALL_EVENTS = {"event_loop_stall_detected", "event_loop_stall_ongoing", "event_loop_stall_ended"}


class _LoopClock:
    """Tiny loop clock for deterministic scheduled-callback lag tests."""

    def __init__(self) -> None:
        self.now = 0.0
        self.scheduled: list[float] = []

    def time(self) -> float:
        return self.now

    def call_at(self, when: float, _callback: object, *_args: object) -> object:
        self.scheduled.append(when)
        return object()


def _fake_runtime_paths(**env_overrides: str) -> RuntimePaths:
    fake = Path("/var/empty/mindroom-test")
    return RuntimePaths(
        config_path=fake / "config.yaml",
        config_dir=fake,
        env_path=fake / ".env",
        storage_root=fake / "data",
        process_env={**env_overrides},
    )


def _detector(
    *,
    threshold_seconds: float = 0.15,
    repeat_log_interval_seconds: float = 10.0,
) -> EventLoopStallDetector:
    return EventLoopStallDetector(
        threshold_seconds=threshold_seconds,
        heartbeat_interval_seconds=0.02,
        poll_interval_seconds=0.02,
        repeat_log_interval_seconds=repeat_log_interval_seconds,
    )


def _stall_logs(logs: list[dict[str, object]]) -> list[dict[str, object]]:
    return [entry for entry in logs if entry["event"] in _STALL_EVENTS]


def test_scheduler_lag_summary_aggregates_delayed_heartbeats() -> None:
    """Delayed callbacks emit one nearest-rank aggregate, never sample logs."""
    detector = _detector()
    loop = _LoopClock()
    detector._loop = loop
    detector._scheduler_lag_window_started_at = 0.0

    with capture_logs() as logs:
        for scheduled, lag_seconds in ((1.0, 0.001), (2.0, 0.002), (3.0, 0.003), (4.0, 0.004), (5.0, 0.005)):
            loop.now = scheduled + lag_seconds
            detector._beat(scheduled)
        detector._report_scheduler_lag(60.0)

    summaries = [entry for entry in logs if entry["event"] == "event_loop_scheduler_lag_summary"]
    assert len(summaries) == 1
    assert {field: summaries[0][field] for field in ("sample_count", "p50_ms", "p95_ms", "p99_ms", "max_ms")} == {
        "sample_count": 5,
        "p50_ms": 3.0,
        "p95_ms": 5.0,
        "p99_ms": 5.0,
        "max_ms": 5.0,
    }
    assert [entry["event"] for entry in logs] == ["event_loop_scheduler_lag_summary"]


def test_scheduler_lag_summary_resets_completed_window_samples() -> None:
    """One completed window reports once; next window contains only new samples."""
    detector = _detector()
    loop = _LoopClock()
    detector._loop = loop
    detector._scheduler_lag_window_started_at = 0.0

    with capture_logs() as logs:
        loop.now = 1.001
        detector._beat(1.0)
        detector._report_scheduler_lag(60.0)
        detector._report_scheduler_lag(60.1)
        loop.now = 61.004
        detector._beat(61.0)
        detector._report_scheduler_lag(120.0)

    summaries = [entry for entry in logs if entry["event"] == "event_loop_scheduler_lag_summary"]
    assert [
        (entry["sample_count"], entry["p50_ms"], entry["p95_ms"], entry["p99_ms"], entry["max_ms"])
        for entry in summaries
    ] == [(1, 1.0, 1.0, 1.0, 1.0), (1, 4.0, 4.0, 4.0, 4.0)]


@pytest.mark.asyncio
async def test_detector_logs_blocking_stack_and_stall_duration() -> None:
    """Blocking the loop must produce one stall log naming the blocking frame."""
    detector = _detector()
    with capture_logs() as logs:
        detector.start()
        await asyncio.sleep(0.1)  # Let the heartbeat establish a fresh beat.
        time.sleep(0.6)  # noqa: ASYNC251 - deliberately block the event loop.
        await asyncio.sleep(0.2)  # Let the heartbeat recover and the watcher observe it.
        detector.stop()

    detected = [entry for entry in logs if entry["event"] == "event_loop_stall_detected"]
    assert len(detected) == 1
    assert detected[0]["stalled_for_seconds"] >= 0.15
    stack = detected[0]["stack"]
    assert isinstance(stack, str)
    assert "time.sleep(0.6)" in stack
    assert "test_event_loop_stall.py" in stack

    ended = [entry for entry in logs if entry["event"] == "event_loop_stall_ended"]
    assert len(ended) == 1
    assert ended[0]["stall_duration_seconds"] >= 0.5


@pytest.mark.asyncio
async def test_detector_repeats_rate_limited_logs_during_long_stall() -> None:
    """A long stall logs once at detection plus rate-limited ongoing events."""
    detector = _detector(repeat_log_interval_seconds=0.15)
    with capture_logs() as logs:
        detector.start()
        await asyncio.sleep(0.1)
        time.sleep(0.7)  # noqa: ASYNC251 - deliberately block the event loop.
        await asyncio.sleep(0.2)
        detector.stop()

    detected = [entry for entry in logs if entry["event"] == "event_loop_stall_detected"]
    ongoing = [entry for entry in logs if entry["event"] == "event_loop_stall_ongoing"]
    assert len(detected) == 1
    assert ongoing, "expected at least one rate-limited ongoing stall log"
    assert all(isinstance(entry["stack"], str) for entry in ongoing)


@pytest.mark.asyncio
async def test_detector_is_quiet_during_normal_operation() -> None:
    """A healthy loop must not produce any stall logs."""
    detector = _detector()
    with capture_logs() as logs:
        detector.start()
        for _ in range(10):
            await asyncio.sleep(0.03)
        detector.stop()

    assert _stall_logs(logs) == []


def test_threshold_defaults_and_env_override() -> None:
    """The env knob tunes the threshold and zero disables the detector."""
    assert _event_loop_stall_threshold_seconds(_fake_runtime_paths()) == _DEFAULT_EVENT_LOOP_STALL_THRESHOLD_SECONDS
    assert _event_loop_stall_threshold_seconds(_fake_runtime_paths(**{_EVENT_LOOP_STALL_THRESHOLD_ENV: "2.5"})) == 2.5


@pytest.mark.asyncio
async def test_start_helper_honors_disable_knob() -> None:
    """A non-positive threshold disables the detector entirely."""
    assert start_event_loop_stall_detector(_fake_runtime_paths(**{_EVENT_LOOP_STALL_THRESHOLD_ENV: "0"})) is None

    detector = start_event_loop_stall_detector(_fake_runtime_paths())
    assert detector is not None
    assert detector.threshold_seconds == _DEFAULT_EVENT_LOOP_STALL_THRESHOLD_SECONDS
    detector.stop()
