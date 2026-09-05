"""Native-thread event-loop stall detector.

The asyncio watchdogs in this codebase cannot observe a blocked event loop:
they are loop-resident tasks, so they only run after the blockage has already
ended. This detector runs in a daemon ``threading.Thread`` instead. The loop
refreshes a monotonic heartbeat through a trivial repeating callback; when the
heartbeat goes stale the thread captures the loop thread's current stack via
``sys._current_frames()`` and logs it. That identifies the blocking code
without ptrace capabilities, so it works in hardened non-root containers
where external profilers such as py-spy cannot attach.
"""

from __future__ import annotations

import asyncio
import math
import sys
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.logging_config import get_logger
from mindroom.timing import elapsed_ms_between

if TYPE_CHECKING:
    from types import FrameType

    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

_EVENT_LOOP_STALL_THRESHOLD_ENV = "MINDROOM_EVENT_LOOP_STALL_THRESHOLD_SECONDS"
_DEFAULT_EVENT_LOOP_STALL_THRESHOLD_SECONDS = 5.0
_HEARTBEAT_INTERVAL_SECONDS = 0.05
_SCHEDULER_LAG_WINDOW_SECONDS = 60.0
_REPEAT_LOG_INTERVAL_SECONDS = 30.0
_THREAD_JOIN_TIMEOUT_SECONDS = 2.0
_MAX_OTHER_THREAD_STACKS = 8
_MAX_STACK_FRAMES = 32
# Eight one-thousand-character stacks bound stack text to eight thousand characters.
_MAX_OTHER_THREAD_STACK_CHARACTERS = 1_000
_STACK_TRUNCATION_MARKER = "\n...\n"


def _event_loop_stall_threshold_seconds(runtime_paths: RuntimePaths) -> float:
    """Return the stall threshold; zero or negative disables the detector."""
    raw = (runtime_paths.env_value(_EVENT_LOOP_STALL_THRESHOLD_ENV) or "").strip()
    if not raw:
        return _DEFAULT_EVENT_LOOP_STALL_THRESHOLD_SECONDS
    return float(raw)


def _truncate_stack(stack: str) -> str:
    """Return a bounded stack suffix while preserving its active tail."""
    if len(stack) <= _MAX_OTHER_THREAD_STACK_CHARACTERS:
        return stack
    tail_length = _MAX_OTHER_THREAD_STACK_CHARACTERS - len(_STACK_TRUNCATION_MARKER)
    return f"{_STACK_TRUNCATION_MARKER}{stack[-tail_length:]}"


def _frame_chain_exceeds_limit(frame: FrameType, frame_limit: int) -> bool:
    """Return whether walking at most ``frame_limit`` frames omits an ancestor."""
    older_frame: FrameType | None = frame
    for _ in range(frame_limit):
        older_frame = older_frame.f_back
        if older_frame is None:
            return False
    return True


def _format_bounded_stack(frame: FrameType) -> tuple[str, bool]:
    """Format one stack and report whether its frame or character limit applied."""
    frame_limit_truncated = _frame_chain_exceeds_limit(frame, _MAX_STACK_FRAMES)
    full_stack = "".join(traceback.format_stack(frame, limit=_MAX_STACK_FRAMES))
    stack = _truncate_stack(full_stack)
    return stack, frame_limit_truncated or len(stack) < len(full_stack)


@dataclass(frozen=True)
class _Heartbeat:
    """One atomically published heartbeat and its matching process CPU baseline."""

    monotonic_seconds: float
    process_cpu_seconds: float


class EventLoopStallDetector:
    """Watch one event loop's heartbeat from a native daemon thread."""

    def __init__(
        self,
        *,
        threshold_seconds: float = _DEFAULT_EVENT_LOOP_STALL_THRESHOLD_SECONDS,
        heartbeat_interval_seconds: float = _HEARTBEAT_INTERVAL_SECONDS,
        repeat_log_interval_seconds: float = _REPEAT_LOG_INTERVAL_SECONDS,
        poll_interval_seconds: float | None = None,
    ) -> None:
        """Configure thresholds; ``start()`` arms the heartbeat and thread."""
        if not math.isfinite(heartbeat_interval_seconds) or heartbeat_interval_seconds <= 0:
            msg = "heartbeat_interval_seconds must be finite and > 0"
            raise ValueError(msg)
        self.threshold_seconds = threshold_seconds
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.repeat_log_interval_seconds = repeat_log_interval_seconds
        self.poll_interval_seconds = poll_interval_seconds or max(min(1.0, threshold_seconds / 2), 0.01)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread_ident: int | None = None
        self._heartbeat_handle: asyncio.TimerHandle | None = None
        self._heartbeat = _Heartbeat(monotonic_seconds=0.0, process_cpu_seconds=0.0)
        self._stalled_beat: float | None = None
        self._next_repeat_log: float = 0.0
        self._scheduler_lag_samples: deque[float] = deque(
            maxlen=max(1, math.ceil(_SCHEDULER_LAG_WINDOW_SECONDS / heartbeat_interval_seconds)),
        )
        self._scheduler_lag_window_started_at: float = 0.0
        self._scheduler_lag_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Arm the heartbeat on the running loop and start the watcher thread."""
        self._loop = asyncio.get_running_loop()
        self._loop_thread_ident = threading.get_ident()
        self._heartbeat = _Heartbeat(
            monotonic_seconds=time.monotonic(),
            process_cpu_seconds=time.process_time(),
        )
        self._scheduler_lag_window_started_at = time.monotonic()
        self._schedule_heartbeat(self._loop.time() + self.heartbeat_interval_seconds)
        self._thread = threading.Thread(
            target=self._watch,
            name="event-loop-stall-detector",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "event_loop_stall_detector_started",
            threshold_seconds=self.threshold_seconds,
            heartbeat_interval_seconds=self.heartbeat_interval_seconds,
        )

    def stop(self) -> None:
        """Stop the watcher thread and disarm the heartbeat."""
        self._stop_event.set()
        if self._heartbeat_handle is not None:
            self._heartbeat_handle.cancel()
            self._heartbeat_handle = None
        if self._thread is not None:
            self._thread.join(timeout=_THREAD_JOIN_TIMEOUT_SECONDS)
            self._thread = None

    def _schedule_heartbeat(self, scheduled_loop_time: float) -> None:
        """Schedule one heartbeat while retaining its requested loop time."""
        assert self._loop is not None
        self._heartbeat_handle = self._loop.call_at(scheduled_loop_time, self._beat, scheduled_loop_time)

    def _beat(self, scheduled_loop_time: float) -> None:
        """Refresh heartbeat, sample callback lag, and re-arm from actual loop time."""
        assert self._loop is not None
        actual_loop_time = self._loop.time()
        self._heartbeat = _Heartbeat(
            monotonic_seconds=time.monotonic(),
            process_cpu_seconds=time.process_time(),
        )
        with self._scheduler_lag_lock:
            self._scheduler_lag_samples.append(max(0.0, actual_loop_time - scheduled_loop_time))
        if not self._stop_event.is_set():
            self._schedule_heartbeat(actual_loop_time + self.heartbeat_interval_seconds)

    def _report_scheduler_lag(self, now: float) -> None:
        """Emit one completed scheduler-lag window from the native watcher."""
        with self._scheduler_lag_lock:
            if now - self._scheduler_lag_window_started_at < _SCHEDULER_LAG_WINDOW_SECONDS:
                return
            samples = list(self._scheduler_lag_samples)
            self._scheduler_lag_samples.clear()
            self._scheduler_lag_window_started_at = now
        if not samples:
            return
        milliseconds = sorted(elapsed_ms_between(0.0, sample, ndigits=3) for sample in samples)
        logger.info(
            "event_loop_scheduler_lag_summary",
            sample_count=len(milliseconds),
            p50_ms=_nearest_rank_percentile(milliseconds, 50),
            p95_ms=_nearest_rank_percentile(milliseconds, 95),
            p99_ms=_nearest_rank_percentile(milliseconds, 99),
            max_ms=milliseconds[-1],
        )

    def _loop_thread_stack(self, frames: dict[int, FrameType]) -> str | None:
        """Return the loop thread's current stack, formatted for logging."""
        if self._loop_thread_ident is None:
            return None
        frame = frames.get(self._loop_thread_ident)
        if frame is None:
            return None
        return "".join(traceback.format_stack(frame))

    def _other_thread_stacks(
        self,
        frames: dict[int, FrameType],
    ) -> tuple[list[dict[str, object]], int]:
        """Return bounded stacks from a frame snapshot, excluding loop and watcher."""
        current_ident = threading.get_ident()
        known_threads = {thread.ident: thread for thread in threading.enumerate() if thread.ident is not None}
        candidates = sorted(
            (thread_ident for thread_ident in frames if thread_ident not in {self._loop_thread_ident, current_ident}),
            key=lambda thread_ident: (
                known_threads[thread_ident].daemon if thread_ident in known_threads else True,
                known_threads[thread_ident].name if thread_ident in known_threads else f"thread-{thread_ident}",
                thread_ident,
            ),
        )
        stacks: list[dict[str, object]] = []
        for thread_ident in candidates[:_MAX_OTHER_THREAD_STACKS]:
            thread = known_threads.get(thread_ident)
            thread_name = thread.name if thread is not None else f"thread-{thread_ident}"
            stack, truncated = _format_bounded_stack(frames[thread_ident])
            stacks.append(
                {
                    "thread_name": thread_name,
                    "thread_ident": thread_ident,
                    "daemon": thread.daemon if thread is not None else None,
                    "stack": stack,
                    "truncated": truncated,
                },
            )
        return stacks, len(candidates) - len(stacks)

    def _stall_diagnostics(
        self,
        heartbeat: _Heartbeat,
        *,
        frames: dict[int, FrameType],
        current_process_cpu: float,
    ) -> dict[str, object]:
        """Capture process activity and competing Python work for one stall log."""
        other_thread_stacks, omitted_thread_stack_count = self._other_thread_stacks(frames)
        return {
            "process_cpu_seconds_since_heartbeat": round(
                max(0.0, current_process_cpu - heartbeat.process_cpu_seconds),
                3,
            ),
            "other_thread_stacks": other_thread_stacks,
            "omitted_thread_stack_count": omitted_thread_stack_count,
        }

    def _note_stall_ended(self, fresh_beat: float) -> None:
        """Log the end of one stall using the heartbeat gap as its duration."""
        assert self._stalled_beat is not None
        logger.warning(
            "event_loop_stall_ended",
            stall_duration_seconds=round(fresh_beat - self._stalled_beat, 3),
        )
        self._stalled_beat = None

    def _note_stalled(self, now: float, heartbeat: _Heartbeat) -> None:
        """Log one stalled heartbeat, rate-limited to once per repeat interval."""
        last_beat = heartbeat.monotonic_seconds
        stalled_for_seconds = round(now - last_beat, 3)
        if self._stalled_beat is None:
            self._stalled_beat = last_beat
            self._next_repeat_log = now + self.repeat_log_interval_seconds
            current_process_cpu = time.process_time()
            frames = sys._current_frames()
            logger.error(
                "event_loop_stall_detected",
                stalled_for_seconds=stalled_for_seconds,
                threshold_seconds=self.threshold_seconds,
                stack=self._loop_thread_stack(frames),
                **self._stall_diagnostics(heartbeat, frames=frames, current_process_cpu=current_process_cpu),
            )
        elif now >= self._next_repeat_log:
            self._next_repeat_log = now + self.repeat_log_interval_seconds
            current_process_cpu = time.process_time()
            frames = sys._current_frames()
            logger.error(
                "event_loop_stall_ongoing",
                stalled_for_seconds=stalled_for_seconds,
                threshold_seconds=self.threshold_seconds,
                stack=self._loop_thread_stack(frames),
                **self._stall_diagnostics(heartbeat, frames=frames, current_process_cpu=current_process_cpu),
            )

    def _watch(self) -> None:
        """Poll the heartbeat off-loop and log stalls with the blocking stack."""
        while not self._stop_event.wait(self.poll_interval_seconds):
            now = time.monotonic()
            self._report_scheduler_lag(now)
            heartbeat = self._heartbeat
            last_beat = heartbeat.monotonic_seconds
            if self._stalled_beat is not None and last_beat != self._stalled_beat:
                self._note_stall_ended(last_beat)
            if now - last_beat > self.threshold_seconds:
                self._note_stalled(now, heartbeat)


def _nearest_rank_percentile(samples: list[float], percentile: int) -> float:
    """Return one nearest-rank percentile from sorted non-empty samples."""
    return samples[math.ceil(len(samples) * percentile / 100) - 1]


def start_event_loop_stall_detector(runtime_paths: RuntimePaths) -> EventLoopStallDetector | None:
    """Start a detector for the running loop unless disabled via the env knob."""
    threshold_seconds = _event_loop_stall_threshold_seconds(runtime_paths)
    if threshold_seconds <= 0:
        logger.info("event_loop_stall_detector_disabled", env_var=_EVENT_LOOP_STALL_THRESHOLD_ENV)
        return None
    detector = EventLoopStallDetector(threshold_seconds=threshold_seconds)
    detector.start()
    return detector
