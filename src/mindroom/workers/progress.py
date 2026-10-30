"""Warmup progress of the workers one dedicated backend starts, sent to every caller waiting on each worker.

A start that becomes ready within the grace period reports nothing.
A longer one reports ``cold_start``, then ``waiting`` at each interval, then ``ready``; a failed start reports ``failed``.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.tool_system.worker_routing import worker_log_id
from mindroom.workers.models import WorkerReadyProgress

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from mindroom.workers.models import ProgressSink, WorkerReadyPhase

__all__ = ["WorkerWarmupProgress", "noop_finalize_progress"]

_COLD_START_GRACE_SECONDS = 1.5
_WAITING_PROGRESS_INTERVAL_SECONDS = 5.0
_REPORTER_JOIN_TIMEOUT_SECONDS = 1.0


def noop_finalize_progress(_phase: WorkerReadyPhase, _error: str | None) -> None:
    """Finish a start that no reporter followed."""
    del _phase, _error


@dataclass
class _ReporterState:
    """How far one start's reporting got, guarded by its reporter's condition."""

    worker_key: str
    backend_name: str
    started_at: float
    cold_start_emitted: bool = False
    next_waiting_elapsed: float = _WAITING_PROGRESS_INTERVAL_SECONDS
    done: bool = False

    def elapsed_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.started_at)

    def event(self, phase: WorkerReadyPhase, elapsed_seconds: float, error: str | None = None) -> WorkerReadyProgress:
        return WorkerReadyProgress(
            phase=phase,
            worker_key=self.worker_key,
            backend_name=self.backend_name,
            elapsed_seconds=elapsed_seconds,
            error=error,
        )

    def next_deadline_elapsed(self) -> float:
        return self.next_waiting_elapsed if self.cold_start_emitted else _COLD_START_GRACE_SECONDS

    def due_events(self) -> list[WorkerReadyProgress]:
        """Return ``cold_start`` once the grace period is over, then one ``waiting`` per interval crossed since."""
        elapsed_seconds = self.elapsed_seconds()
        events: list[WorkerReadyProgress] = []
        if not self.cold_start_emitted and elapsed_seconds >= _COLD_START_GRACE_SECONDS:
            events.append(self.event("cold_start", elapsed_seconds))
            self.cold_start_emitted = True
        while self.cold_start_emitted and elapsed_seconds >= self.next_waiting_elapsed:
            events.append(self.event("waiting", elapsed_seconds))
            self.next_waiting_elapsed += _WAITING_PROGRESS_INTERVAL_SECONDS
        return events

    def terminal_event(self, phase: WorkerReadyPhase, error: str | None) -> WorkerReadyProgress | None:
        """Return the closing event: ``ready`` only after a reported ``cold_start``, ``failed`` always."""
        if phase == "ready" and not self.cold_start_emitted:
            return None
        return self.event(phase, self.elapsed_seconds(), error)


def _report_due_events(condition: threading.Condition, state: _ReporterState, progress_sink: ProgressSink) -> None:
    """Report each event when it falls due, until the start is finalized."""
    while True:
        with condition:
            while not state.done:
                wait_seconds = state.started_at + state.next_deadline_elapsed() - time.monotonic()
                if wait_seconds <= 0:
                    break
                condition.wait(timeout=wait_seconds)
            if state.done:
                return
            events = state.due_events()
        for event in events:
            progress_sink(event)


class WorkerWarmupProgress:
    """One backend's worker starts, each reported to every caller waiting on that worker.

    A caller that subscribes while a start is under way is first sent the latest phase reported so far.
    """

    def __init__(self, backend_name: str) -> None:
        self._backend_name = backend_name
        self._sinks: dict[str, list[ProgressSink]] = {}
        self._latest: dict[str, WorkerReadyProgress] = {}
        self._lock = threading.Lock()

    @contextmanager
    def subscribed(self, worker_key: str, progress_sink: ProgressSink | None) -> Iterator[None]:
        """Send the progress of ``worker_key``'s starts to ``progress_sink`` while the block runs."""
        if progress_sink is None:
            yield
            return
        with self._lock:
            self._sinks.setdefault(worker_key, []).append(progress_sink)
            latest = self._latest.get(worker_key)
            if latest is not None:
                progress_sink(latest)
        try:
            yield
        finally:
            with self._lock:
                sinks = self._sinks[worker_key]
                sinks.remove(progress_sink)
                if not sinks:
                    del self._sinks[worker_key]
                    self._latest.pop(worker_key, None)

    def emit(self, progress: WorkerReadyProgress) -> None:
        """Send one event to every caller waiting on its worker, and keep it for later callers until the start ends."""
        with self._lock:
            if progress.phase in {"ready", "failed"}:
                self._latest.pop(progress.worker_key, None)
            else:
                self._latest[progress.worker_key] = progress
            for progress_sink in self._sinks.get(progress.worker_key, ()):
                progress_sink(progress)

    def start_reporter(
        self,
        worker_key: str,
    ) -> tuple[Callable[[], None], Callable[[WorkerReadyPhase, str | None], None]]:
        """Report one start of ``worker_key`` from a daemon thread, as each event falls due.

        Returns ``on_poll_tick``, which a readiness poll calls to wake the reporter, and ``finalize``, which reports
        the events still due and the closing one once the start became ``ready`` or ``failed``.
        """
        condition = threading.Condition()
        state = _ReporterState(worker_key=worker_key, backend_name=self._backend_name, started_at=time.monotonic())
        thread = threading.Thread(
            target=_report_due_events,
            args=(condition, state, self.emit),
            name=f"{self._backend_name}-worker-progress:{worker_log_id(worker_key)}",
            daemon=True,
        )
        thread.start()

        def on_poll_tick() -> None:
            with condition:
                condition.notify_all()

        def finalize(phase: WorkerReadyPhase, error: str | None) -> None:
            with condition:
                events = state.due_events()
                terminal_event = state.terminal_event(phase, error)
                state.done = True
                condition.notify_all()
            # The thread reports its last batch before it exits, so every event goes out in order.
            thread.join(timeout=_REPORTER_JOIN_TIMEOUT_SECONDS)
            for event in events:
                self.emit(event)
            if terminal_event is not None:
                self.emit(terminal_event)

        return on_poll_tick, finalize
