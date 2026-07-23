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
import sys
import threading
import time
import traceback
from typing import TYPE_CHECKING

from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

_EVENT_LOOP_STALL_THRESHOLD_ENV = "MINDROOM_EVENT_LOOP_STALL_THRESHOLD_SECONDS"
_DEFAULT_EVENT_LOOP_STALL_THRESHOLD_SECONDS = 5.0
_HEARTBEAT_INTERVAL_SECONDS = 1.0
_REPEAT_LOG_INTERVAL_SECONDS = 30.0
_THREAD_JOIN_TIMEOUT_SECONDS = 2.0


def _event_loop_stall_threshold_seconds(runtime_paths: RuntimePaths) -> float:
    """Return the stall threshold; zero or negative disables the detector."""
    raw = (runtime_paths.env_value(_EVENT_LOOP_STALL_THRESHOLD_ENV) or "").strip()
    if not raw:
        return _DEFAULT_EVENT_LOOP_STALL_THRESHOLD_SECONDS
    return float(raw)


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
        self.threshold_seconds = threshold_seconds
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.repeat_log_interval_seconds = repeat_log_interval_seconds
        self.poll_interval_seconds = poll_interval_seconds or max(min(1.0, threshold_seconds / 2), 0.01)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread_ident: int | None = None
        self._heartbeat_handle: asyncio.TimerHandle | None = None
        self._last_beat: float = 0.0
        self._stalled_beat: float | None = None
        self._next_repeat_log: float = 0.0
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Arm the heartbeat on the running loop and start the watcher thread."""
        self._loop = asyncio.get_running_loop()
        self._loop_thread_ident = threading.get_ident()
        self._last_beat = time.monotonic()
        self._heartbeat_handle = self._loop.call_later(self.heartbeat_interval_seconds, self._beat)
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

    def _beat(self) -> None:
        """Refresh the heartbeat on the loop and re-arm the next beat."""
        self._last_beat = time.monotonic()
        if not self._stop_event.is_set() and self._loop is not None:
            self._heartbeat_handle = self._loop.call_later(self.heartbeat_interval_seconds, self._beat)

    def _loop_thread_stack(self) -> str | None:
        """Return the loop thread's current stack, formatted for logging."""
        if self._loop_thread_ident is None:
            return None
        frame = sys._current_frames().get(self._loop_thread_ident)
        if frame is None:
            return None
        return "".join(traceback.format_stack(frame))

    def _note_stall_ended(self, fresh_beat: float) -> None:
        """Log the end of one stall using the heartbeat gap as its duration."""
        assert self._stalled_beat is not None
        logger.warning(
            "event_loop_stall_ended",
            stall_duration_seconds=round(fresh_beat - self._stalled_beat, 3),
        )
        self._stalled_beat = None

    def _note_stalled(self, now: float, last_beat: float) -> None:
        """Log one stalled heartbeat, rate-limited to once per repeat interval."""
        stalled_for_seconds = round(now - last_beat, 3)
        if self._stalled_beat is None:
            self._stalled_beat = last_beat
            self._next_repeat_log = now + self.repeat_log_interval_seconds
            logger.error(
                "event_loop_stall_detected",
                stalled_for_seconds=stalled_for_seconds,
                threshold_seconds=self.threshold_seconds,
                stack=self._loop_thread_stack(),
            )
        elif now >= self._next_repeat_log:
            self._next_repeat_log = now + self.repeat_log_interval_seconds
            logger.error(
                "event_loop_stall_ongoing",
                stalled_for_seconds=stalled_for_seconds,
                threshold_seconds=self.threshold_seconds,
                stack=self._loop_thread_stack(),
            )

    def _watch(self) -> None:
        """Poll the heartbeat off-loop and log stalls with the blocking stack."""
        while not self._stop_event.wait(self.poll_interval_seconds):
            now = time.monotonic()
            last_beat = self._last_beat
            if self._stalled_beat is not None and last_beat != self._stalled_beat:
                self._note_stall_ended(last_beat)
            if now - last_beat > self.threshold_seconds:
                self._note_stalled(now, last_beat)


def start_event_loop_stall_detector(runtime_paths: RuntimePaths) -> EventLoopStallDetector | None:
    """Start a detector for the running loop unless disabled via the env knob."""
    threshold_seconds = _event_loop_stall_threshold_seconds(runtime_paths)
    if threshold_seconds <= 0:
        logger.info("event_loop_stall_detector_disabled", env_var=_EVENT_LOOP_STALL_THRESHOLD_ENV)
        return None
    detector = EventLoopStallDetector(threshold_seconds=threshold_seconds)
    detector.start()
    return detector
