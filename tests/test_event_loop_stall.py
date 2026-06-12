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
