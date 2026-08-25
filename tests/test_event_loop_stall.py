"""Event-loop stall detector behavior."""

from __future__ import annotations

import _thread
import asyncio
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from structlog.testing import capture_logs

from mindroom import event_loop_stall
from mindroom.constants import RuntimePaths
from mindroom.event_loop_stall import (
    _DEFAULT_EVENT_LOOP_STALL_THRESHOLD_SECONDS,
    _EVENT_LOOP_STALL_THRESHOLD_ENV,
    EventLoopStallDetector,
    _event_loop_stall_threshold_seconds,
    start_event_loop_stall_detector,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_STALL_EVENTS = {"event_loop_stall_detected", "event_loop_stall_ongoing", "event_loop_stall_ended"}


class _LoopClock:
    """Tiny loop clock for deterministic scheduled-callback lag tests."""

    def __init__(self) -> None:
        self.now = 0.0
        self.scheduled: list[tuple[float, Callable[[float], None], float]] = []

    def time(self) -> float:
        return self.now

    def call_at(self, when: float, callback: Callable[[float], None], scheduled_loop_time: float) -> object:
        self.scheduled.append((when, callback, scheduled_loop_time))
        return object()

    def next_scheduled_time(self) -> float:
        return self.scheduled[0][0]

    def run_next(self) -> None:
        _, callback, scheduled_loop_time = self.scheduled.pop(0)
        callback(scheduled_loop_time)


class _FakeFrame:
    """Minimal frame chain for deterministic stack-depth tests."""

    def __init__(self, f_back: _FakeFrame | None = None) -> None:
        self.f_back = f_back


def _fake_frame_chain(depth: int) -> _FakeFrame:
    """Return an active fake frame with ``depth`` linked frames."""
    frame: _FakeFrame | None = None
    for _ in range(depth):
        frame = _FakeFrame(frame)
    assert frame is not None
    return frame


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


@pytest.mark.parametrize("heartbeat_interval_seconds", [0.0, -0.05, float("inf"), float("-inf"), float("nan")])
def test_detector_rejects_non_positive_or_nonfinite_heartbeat_intervals(
    heartbeat_interval_seconds: float,
) -> None:
    """Heartbeat scheduling requires a finite interval greater than zero."""
    with pytest.raises(ValueError, match="finite and > 0"):
        EventLoopStallDetector(heartbeat_interval_seconds=heartbeat_interval_seconds)


def test_scheduler_lag_summary_aggregates_delayed_heartbeats() -> None:
    """Delayed callbacks emit one nearest-rank aggregate, never sample logs."""
    detector = _detector()
    loop = _LoopClock()
    detector._loop = loop
    detector._scheduler_lag_window_started_at = 0.0
    detector._schedule_heartbeat(1.0)

    with capture_logs() as logs:
        for lag_seconds in (0.001, 0.002, 0.003, 0.004, 0.005):
            loop.now = loop.next_scheduled_time() + lag_seconds
            loop.run_next()
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
    detector._schedule_heartbeat(1.0)

    with capture_logs() as logs:
        loop.now = loop.next_scheduled_time() + 0.001
        loop.run_next()
        detector._report_scheduler_lag(60.0)
        detector._report_scheduler_lag(60.1)
        loop.now = loop.next_scheduled_time() + 0.004
        loop.run_next()
        detector._report_scheduler_lag(120.0)

    summaries = [entry for entry in logs if entry["event"] == "event_loop_scheduler_lag_summary"]
    assert [
        (entry["sample_count"], entry["p50_ms"], entry["p95_ms"], entry["p99_ms"], entry["max_ms"])
        for entry in summaries
    ] == [(1, 1.0, 1.0, 1.0, 1.0), (1, 4.0, 4.0, 4.0, 4.0)]


def test_scheduler_lag_heartbeat_rearms_from_actual_time_after_stall() -> None:
    """A recovered heartbeat must schedule future 50 ms samples, not replay missed ones."""
    detector = EventLoopStallDetector(heartbeat_interval_seconds=0.05)
    loop = _LoopClock()
    detector._loop = loop
    detector._schedule_heartbeat(1.0)

    loop.now = 1.31
    loop.run_next()
    assert loop.next_scheduled_time() == pytest.approx(1.36)

    loop.now = 1.36
    loop.run_next()
    assert loop.next_scheduled_time() == pytest.approx(1.41)


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
async def test_detector_logs_process_cpu_and_other_python_thread_stacks() -> None:
    """A stall report must distinguish process activity and expose competing Python work."""
    worker_started = threading.Event()
    release_worker = threading.Event()
    detector = _detector()

    def parked_worker() -> None:
        worker_started.set()
        release_worker.wait()

    worker = threading.Thread(target=parked_worker, name="stall-context-worker")
    try:
        worker.start()
        assert worker_started.wait(timeout=1.0)
        with capture_logs() as logs:
            detector.start()
            await asyncio.sleep(0.1)
            time.sleep(0.6)  # noqa: ASYNC251 - deliberately block the event loop.
            await asyncio.sleep(0.2)
    finally:
        detector.stop()
        release_worker.set()
        worker.join(timeout=1.0)
    assert not worker.is_alive()

    detected = [entry for entry in logs if entry["event"] == "event_loop_stall_detected"]
    assert len(detected) == 1
    assert isinstance(detected[0]["process_cpu_seconds_since_heartbeat"], float)
    assert detected[0]["process_cpu_seconds_since_heartbeat"] >= 0
    other_thread_stacks = detected[0]["other_thread_stacks"]
    assert isinstance(other_thread_stacks, list)
    worker_stacks = [entry for entry in other_thread_stacks if entry["thread_name"] == "stall-context-worker"]
    assert len(worker_stacks) == 1
    assert "parked_worker" in worker_stacks[0]["stack"]
    assert detected[0]["omitted_thread_stack_count"] >= 0


def test_other_thread_stacks_uses_frame_snapshot_for_low_level_thread() -> None:
    """A live native thread is reported even when threading does not know about it."""
    worker_started = threading.Event()
    release_worker = threading.Event()
    worker_stopped = threading.Event()
    worker_ident: list[int] = []

    def low_level_worker() -> None:
        try:
            worker_ident.append(threading.get_ident())
            worker_started.set()
            release_worker.wait()
        finally:
            worker_stopped.set()

    _thread.start_new_thread(low_level_worker, ())
    try:
        assert worker_started.wait(timeout=1.0)
        detector = _detector()
        stacks, omitted, truncated = detector._other_thread_stacks()
    finally:
        release_worker.set()
    assert worker_stopped.wait(timeout=1.0)

    assert worker_ident[0] not in {thread.ident for thread in threading.enumerate()}
    assert omitted == 0
    assert truncated == 0
    stack = next(entry for entry in stacks if entry["thread_ident"] == worker_ident[0])
    assert stack["thread_name"] == f"thread-{worker_ident[0]}"
    assert stack["daemon"] is None
    assert "low_level_worker" in stack["stack"]


def test_other_thread_stacks_keeps_snapshot_frame_when_thread_metadata_has_gone_away(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A frame present in the snapshot remains reportable through metadata lookup races."""
    detector = _detector()
    frame_ident = 123
    frame = _FakeFrame()
    monkeypatch.setattr(event_loop_stall.sys, "_current_frames", lambda: {frame_ident: frame})
    monkeypatch.setattr(event_loop_stall.threading, "enumerate", list)
    monkeypatch.setattr(event_loop_stall.traceback, "format_stack", lambda *_args, **_kwargs: ["snapshot-frame"])

    stacks, omitted, truncated = detector._other_thread_stacks()

    assert stacks == [
        {
            "thread_name": "thread-123",
            "thread_ident": 123,
            "daemon": None,
            "stack": "snapshot-frame",
        },
    ]
    assert omitted == 0
    assert truncated == 0


def test_other_thread_stacks_excludes_loop_and_watcher_and_bounds_characters(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only eligible snapshot frames consume the exact per-stack and total budgets."""
    detector = _detector()
    watcher_ident = threading.get_ident()
    loop_ident = 10
    first_ident = 20
    second_ident = 30
    third_ident = 40
    frames = {ident: _FakeFrame() for ident in (loop_ident, watcher_ident, first_ident, second_ident, third_ident)}
    names = {
        first_ident: SimpleNamespace(ident=first_ident, name="first", daemon=False),
        second_ident: SimpleNamespace(ident=second_ident, name="second", daemon=True),
        third_ident: SimpleNamespace(ident=third_ident, name="third", daemon=True),
    }
    detector._loop_thread_ident = loop_ident
    monkeypatch.setattr(event_loop_stall.sys, "_current_frames", lambda: frames)
    monkeypatch.setattr(event_loop_stall.threading, "enumerate", lambda: list(names.values()))
    monkeypatch.setattr(
        event_loop_stall.traceback,
        "format_stack",
        lambda frame, **_kwargs: {  # type: ignore[call-arg]
            frames[first_ident]: ["abcdefgh"],
            frames[second_ident]: ["ijklmnop"],
            frames[third_ident]: ["qrstuvwx"],
        }[frame],
    )
    monkeypatch.setattr(event_loop_stall, "_MAX_OTHER_THREAD_STACKS", 3)
    monkeypatch.setattr(event_loop_stall, "_MAX_OTHER_THREAD_STACK_CHARACTERS", 5)
    monkeypatch.setattr(event_loop_stall, "_MAX_OTHER_THREAD_STACK_TOTAL_CHARACTERS", 7)

    stacks, omitted, truncated = detector._other_thread_stacks()

    assert [(entry["thread_name"], entry["stack"]) for entry in stacks] == [
        ("first", "defgh"),
        ("second", "op"),
    ]
    assert omitted == 1
    assert truncated == 2
    assert sum(len(entry["stack"]) for entry in stacks) == 7


def test_other_thread_stack_truncation_keeps_the_active_frame_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bounded stack retains its active frame at the formatted-stack tail."""
    detector = _detector()
    frame_ident = 123
    frame = _FakeFrame()
    monkeypatch.setattr(event_loop_stall.sys, "_current_frames", lambda: {frame_ident: frame})
    monkeypatch.setattr(event_loop_stall.threading, "enumerate", list)
    monkeypatch.setattr(
        event_loop_stall.traceback,
        "format_stack",
        lambda *_args, **_kwargs: ["unimportant\n", "active-frame\n"],
    )
    monkeypatch.setattr(event_loop_stall, "_MAX_OTHER_THREAD_STACK_CHARACTERS", 20)
    monkeypatch.setattr(event_loop_stall, "_MAX_OTHER_THREAD_STACK_TOTAL_CHARACTERS", 20)

    stacks, omitted, truncated = detector._other_thread_stacks()

    assert stacks[0]["stack"] == "\n...\nt\nactive-frame\n"
    assert len(stacks[0]["stack"]) == 20
    assert stacks[0]["stack"].endswith("active-frame\n")
    assert omitted == 0
    assert truncated == 1


def test_other_thread_stacks_counts_frame_limit_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
    """The truncation count includes stacks whose older frames exceed the frame cap."""
    detector = _detector()
    frame_ident = 123
    frame = _fake_frame_chain(event_loop_stall._MAX_STACK_FRAMES + 1)
    observed_limits: list[int | None] = []
    monkeypatch.setattr(event_loop_stall.sys, "_current_frames", lambda: {frame_ident: frame})
    monkeypatch.setattr(event_loop_stall.threading, "enumerate", list)
    monkeypatch.setattr(
        event_loop_stall.traceback,
        "format_stack",
        lambda *_args, limit=None: observed_limits.append(limit) or ["active-frame\n"],
    )

    stacks, omitted, truncated = detector._other_thread_stacks()

    assert stacks[0]["stack"] == "active-frame\n"
    assert observed_limits == [event_loop_stall._MAX_STACK_FRAMES]
    assert omitted == 0
    assert truncated == 1


def test_stall_diagnostics_uses_one_heartbeat_snapshot_and_samples_cpu_before_formatting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CPU work is measured from the observed heartbeat before stack formatting begins."""
    detector = _detector()
    heartbeat = event_loop_stall._Heartbeat(monotonic_seconds=1.0, process_cpu_seconds=10.0)
    detector._heartbeat = event_loop_stall._Heartbeat(monotonic_seconds=2.0, process_cpu_seconds=20.0)
    events: list[str] = []
    monkeypatch.setattr(event_loop_stall.time, "process_time", lambda: events.append("cpu") or 12.345)
    monkeypatch.setattr(event_loop_stall.sys, "_current_frames", dict)
    monkeypatch.setattr(
        detector,
        "_other_thread_stacks",
        lambda _frames: events.append("format") or ([], 0, 0),
    )

    diagnostics = detector._stall_diagnostics(heartbeat)

    assert diagnostics["process_cpu_seconds_since_heartbeat"] == 2.345
    assert events == ["cpu", "format"]


def test_watch_passes_the_observed_heartbeat_snapshot_to_stall_reporting(monkeypatch: pytest.MonkeyPatch) -> None:
    """The watcher never pairs a stale monotonic time with a newer CPU baseline."""
    detector = _detector(threshold_seconds=0.1)
    heartbeat = event_loop_stall._Heartbeat(monotonic_seconds=1.0, process_cpu_seconds=10.0)
    detector._heartbeat = heartbeat
    observed: list[object] = []

    class _OnePollStopEvent:
        def __init__(self) -> None:
            self.polls = 0

        def wait(self, _timeout: float) -> bool:
            self.polls += 1
            return self.polls > 1

    detector._stop_event = _OnePollStopEvent()  # type: ignore[assignment]
    monkeypatch.setattr(detector, "_report_scheduler_lag", lambda _now: None)
    monkeypatch.setattr(event_loop_stall.time, "monotonic", lambda: 2.0)
    monkeypatch.setattr(detector, "_note_stalled", lambda _now, seen: observed.append(seen))

    detector._watch()

    assert observed == [heartbeat]


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
