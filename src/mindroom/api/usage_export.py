"""Bounded background preparation for authenticated usage exports."""

from __future__ import annotations

import threading
import time
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from mindroom.api import config_lifecycle
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi import FastAPI

    from mindroom.constants import RuntimePaths

_SUCCESS_TTL_SECONDS = 60
_FAILURE_TTL_SECONDS = 5
_DIAGNOSTIC_FRAME_LIMIT = 8
RETRY_AFTER_SECONDS = 5
logger = get_logger(__name__)


@dataclass(frozen=True)
class UsageExportContext:
    """Runtime/config identity and report variant for one export scan."""

    runtime_paths: RuntimePaths
    generation: int
    include_daily: bool
    include_requests: bool = False

    def _same_runtime_generation(self, other: UsageExportContext) -> bool:
        """Return whether two requests belong to one committed runtime/config scope."""
        return self.runtime_paths == other.runtime_paths and self.generation == other.generation


class UsageExportStatus(Enum):
    """Public polling states for one prepared export."""

    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"


@dataclass(frozen=True)
class _UsageExportPoll:
    """Result of checking or starting one export variant."""

    status: UsageExportStatus
    report: dict[str, object] | None = None


@dataclass
class _ActiveExport:
    context: UsageExportContext
    future: Future[dict[str, object]]
    context_is_current: Callable[[], bool]
    failure_event: str = "usage_export_preparation_failed"


@dataclass(frozen=True)
class _CompletedExport:
    context: UsageExportContext
    completed_at: float
    report: dict[str, object] | None


def _start_daemon_worker(target: Callable[[], None]) -> None:
    """Start report preparation without adding a process-shutdown wait."""
    threading.Thread(target=target, name="mindroom-usage-export", daemon=True).start()


class UsageExportRunner:
    """Own one in-flight scan and at most one cached result per report variant."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        start_worker: Callable[[Callable[[], None]], None] = _start_daemon_worker,
    ) -> None:
        self._clock = clock
        self._start_worker = start_worker
        self._lock = threading.Lock()
        self._active: _ActiveExport | None = None
        self._completed: dict[tuple[bool, bool], _CompletedExport] = {}
        self._closed = False

    @property
    def closed(self) -> bool:
        """Return whether lifecycle cleanup retired this runner."""
        with self._lock:
            return self._closed

    def poll(
        self,
        context: UsageExportContext,
        build_report: Callable[[], dict[str, object]],
        *,
        context_is_current: Callable[[], bool],
    ) -> _UsageExportPoll:
        """Return a cached outcome or start one bounded background scan."""
        with self._lock:
            if self._closed:
                return _UsageExportPoll(UsageExportStatus.FAILED)
            self._discard_other_runtime_generations(context)
            variant = (context.include_daily, context.include_requests)
            completed = self._completed.get(variant)
            if completed is not None and completed.context == context:
                ttl = _SUCCESS_TTL_SECONDS if completed.report is not None else _FAILURE_TTL_SECONDS
                if self._clock() - completed.completed_at < ttl:
                    status = UsageExportStatus.READY if completed.report is not None else UsageExportStatus.FAILED
                    return _UsageExportPoll(status, completed.report)
                del self._completed[variant]
            if self._active is not None:
                return _UsageExportPoll(UsageExportStatus.PENDING)

            future: Future[dict[str, object]] = Future()
            active = _ActiveExport(
                context=context,
                future=future,
                context_is_current=context_is_current,
            )
            self._active = active
            future.add_done_callback(lambda completed_future: self._finish(active, completed_future))

        try:
            self._start_worker(lambda: self._run(active, build_report))
        except Exception as exc:
            active.failure_event = "usage_export_worker_start_failed"
            future.set_exception(exc)
            return _UsageExportPoll(UsageExportStatus.FAILED)
        return _UsageExportPoll(UsageExportStatus.PENDING)

    def close(self) -> None:
        """Discard cached work and cancel queued work without joining a running scan."""
        with self._lock:
            self._closed = True
            self._completed.clear()
            active = self._active
            self._active = None
        if active is not None:
            active.future.cancel()

    def _discard_other_runtime_generations(self, context: UsageExportContext) -> None:
        stale_variants = [
            variant
            for variant, completed in self._completed.items()
            if not completed.context._same_runtime_generation(context)
        ]
        for variant in stale_variants:
            del self._completed[variant]

    @staticmethod
    def _run(active: _ActiveExport, build_report: Callable[[], dict[str, object]]) -> None:
        if not active.future.set_running_or_notify_cancel():
            return
        try:
            active.future.set_result(build_report())
        except BaseException as exc:
            # A detached worker must always settle its Future; otherwise a
            # process-control exception would leave the single scan slot stuck.
            active.future.set_exception(exc)

    def _finish(self, active: _ActiveExport, future: Future[dict[str, object]]) -> None:
        try:
            report = future.result()
        except BaseException as exc:
            if future.cancelled():
                return
            # The worker boundary deliberately permits any BaseException to
            # reach this Future so the in-flight slot can always be released.
            _log_sanitized_failure(active.failure_event, exc)
            report = None
        with self._lock:
            if self._active is not active or self._closed:
                return
        try:
            context_is_current = active.context_is_current()
        except BaseException as exc:
            # Validation runs in the detached completion callback, which must
            # still retire the active slot after a process-control exception.
            _log_sanitized_failure("usage_export_context_validation_failed", exc)
            context_is_current = False
        with self._lock:
            if self._active is not active:
                return
            self._active = None
            if self._closed or not context_is_current:
                return
            self._completed[(active.context.include_daily, active.context.include_requests)] = _CompletedExport(
                context=active.context,
                completed_at=self._clock(),
                report=report,
            )


def _log_sanitized_failure(event: str, error: BaseException) -> None:
    """Log bounded traceback metadata without exception text, locals, or source."""
    frames: deque[dict[str, object]] = deque(maxlen=_DIAGNOSTIC_FRAME_LIMIT)
    traceback = error.__traceback__
    while traceback is not None:
        frame = traceback.tb_frame
        module = frame.f_globals.get("__name__")
        frames.append(
            {
                "module": module if isinstance(module, str) else "<unknown>",
                "function": frame.f_code.co_name,
                "line": traceback.tb_lineno,
            },
        )
        traceback = traceback.tb_next
    error_class = type(error)
    logger.error(
        event,
        error_type=f"{error_class.__module__}.{error_class.__qualname__}",
        stack=tuple(frames),
    )


def usage_export_runner(api_app: FastAPI) -> UsageExportRunner:
    """Return the single usage-export runner scoped to one FastAPI application."""
    api_state = config_lifecycle.require_api_state(api_app)
    with api_state.config_lock:
        app_state = config_lifecycle.app_state(api_app)
        runner = app_state.usage_export_runner
        if runner is None or runner.closed:
            runner = UsageExportRunner()
            app_state.usage_export_runner = runner
        return runner


def context_is_current(api_app: FastAPI, context: UsageExportContext) -> bool:
    """Return whether a completed scan still belongs to the app's published snapshot."""
    api_state = config_lifecycle.require_api_state(api_app)
    with api_state.config_lock:
        snapshot = api_state.snapshot
        return snapshot.runtime_paths == context.runtime_paths and snapshot.generation == context.generation


def close_usage_export_runner(api_app: FastAPI) -> None:
    """Retire an app's runner without waiting for a running daemon scan."""
    api_state = config_lifecycle.require_api_state(api_app)
    with api_state.config_lock:
        state = config_lifecycle.app_state(api_app)
        runner = state.usage_export_runner
        state.usage_export_runner = None
    if runner is not None:
        runner.close()
