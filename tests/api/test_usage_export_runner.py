"""Deterministic concurrency tests for usage export preparation."""

from __future__ import annotations

import threading
from concurrent.futures import Future
from typing import TYPE_CHECKING

from fastapi import FastAPI

from mindroom import constants
from mindroom.api import config_lifecycle, main, usage_export
from mindroom.api.usage_export import (
    UsageExportContext,
    UsageExportRunner,
    UsageExportStatus,
    usage_export_runner,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


class ManualWorkers:
    """Capture background targets so tests decide exactly when they run."""

    def __init__(self) -> None:
        self.targets: list[Callable[[], None]] = []

    def start(self, target) -> None:  # noqa: ANN001
        """Capture one worker target without starting it."""
        self.targets.append(target)

    def run_next(self) -> None:
        """Run the oldest captured target synchronously."""
        self.targets.pop(0)()


class ManualClock:
    """Expose explicit monotonic time without duration assertions."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        """Return the explicitly controlled time."""
        return self.now


def _runtime_paths(tmp_path: Path, name: str = "runtime") -> constants.RuntimePaths:
    return constants.resolve_primary_runtime_paths(
        config_path=tmp_path / name / "config.yaml",
        storage_path=tmp_path / name / "storage",
        process_env={},
    )


def _context(
    tmp_path: Path,
    *,
    generation: int = 1,
    include_daily: bool = False,
    runtime_name: str = "runtime",
) -> UsageExportContext:
    return UsageExportContext(
        runtime_paths=_runtime_paths(tmp_path, runtime_name),
        generation=generation,
        include_daily=include_daily,
    )


def test_repeated_polls_share_one_in_flight_scan(tmp_path: Path) -> None:
    """Repeated polls must not enqueue another scan while the first is blocked."""
    workers = ManualWorkers()
    report: Future[dict[str, object]] = Future()
    runner = UsageExportRunner(start_worker=workers.start)
    context = _context(tmp_path)

    first = runner.poll(context, report.result, context_is_current=lambda: True)
    second = runner.poll(context, report.result, context_is_current=lambda: True)

    assert first.status is UsageExportStatus.PENDING
    assert second.status is UsageExportStatus.PENDING
    assert len(workers.targets) == 1

    report.set_result({"scope": "admin", "variant": "summary"})
    workers.run_next()
    ready = runner.poll(context, report.result, context_is_current=lambda: True)
    assert ready == runner.poll(context, report.result, context_is_current=lambda: True)
    assert ready.status is UsageExportStatus.READY
    assert ready.report == {"scope": "admin", "variant": "summary"}
    assert workers.targets == []


def test_include_daily_variants_keep_separate_bounded_results(tmp_path: Path) -> None:
    """The two report variants must never reuse each other's cached schema."""
    workers = ManualWorkers()
    runner = UsageExportRunner(start_worker=workers.start)
    summary = _context(tmp_path, include_daily=False)
    daily = _context(tmp_path, include_daily=True)

    assert (
        runner.poll(summary, lambda: {"daily": False}, context_is_current=lambda: True).status
        is UsageExportStatus.PENDING
    )
    workers.run_next()
    assert runner.poll(summary, dict, context_is_current=lambda: True).report == {"daily": False}

    assert (
        runner.poll(daily, lambda: {"daily": True}, context_is_current=lambda: True).status is UsageExportStatus.PENDING
    )
    workers.run_next()
    assert runner.poll(daily, dict, context_is_current=lambda: True).report == {"daily": True}
    assert runner.poll(summary, dict, context_is_current=lambda: True).report == {"daily": False}


def test_failure_is_sanitized_and_suppresses_retry_storm(tmp_path: Path) -> None:
    """A failed scan must expose no exception and remain cached for five seconds."""
    workers = ManualWorkers()
    clock = ManualClock()
    failure: Future[dict[str, object]] = Future()
    failure.set_exception(RuntimeError("private retained row"))
    runner = UsageExportRunner(start_worker=workers.start, clock=clock)
    context = _context(tmp_path)

    assert runner.poll(context, failure.result, context_is_current=lambda: True).status is UsageExportStatus.PENDING
    workers.run_next()
    failed = runner.poll(context, failure.result, context_is_current=lambda: True)
    assert failed.status is UsageExportStatus.FAILED
    assert failed.report is None
    assert workers.targets == []

    clock.now = 5
    assert (
        runner.poll(context, lambda: {"refreshed": True}, context_is_current=lambda: True).status
        is UsageExportStatus.PENDING
    )
    assert len(workers.targets) == 1


def test_success_expiry_starts_exactly_one_refresh(tmp_path: Path) -> None:
    """A successful result must refresh once after its 60-second TTL."""
    workers = ManualWorkers()
    clock = ManualClock()
    runner = UsageExportRunner(start_worker=workers.start, clock=clock)
    context = _context(tmp_path)

    assert (
        runner.poll(context, lambda: {"revision": 1}, context_is_current=lambda: True).status
        is UsageExportStatus.PENDING
    )
    workers.run_next()
    clock.now = 59
    assert runner.poll(context, dict, context_is_current=lambda: True).report == {"revision": 1}

    clock.now = 60
    first = runner.poll(context, lambda: {"revision": 2}, context_is_current=lambda: True)
    second = runner.poll(context, lambda: {"revision": 3}, context_is_current=lambda: True)
    assert first.status is UsageExportStatus.PENDING
    assert second.status is UsageExportStatus.PENDING
    assert len(workers.targets) == 1
    workers.run_next()
    assert runner.poll(context, dict, context_is_current=lambda: True).report == {"revision": 2}


def test_changed_generation_discards_old_in_flight_result(tmp_path: Path) -> None:
    """A generation change must serialize behind and then discard an old scan."""
    workers = ManualWorkers()
    runner = UsageExportRunner(start_worker=workers.start)
    old = _context(tmp_path, generation=1)
    current = _context(tmp_path, generation=2)

    assert (
        runner.poll(old, lambda: {"generation": 1}, context_is_current=lambda: False).status
        is UsageExportStatus.PENDING
    )
    assert (
        runner.poll(current, lambda: {"generation": 2}, context_is_current=lambda: True).status
        is UsageExportStatus.PENDING
    )
    assert len(workers.targets) == 1
    workers.run_next()

    assert (
        runner.poll(current, lambda: {"generation": 2}, context_is_current=lambda: True).status
        is UsageExportStatus.PENDING
    )
    assert len(workers.targets) == 1
    workers.run_next()
    assert runner.poll(current, dict, context_is_current=lambda: True).report == {"generation": 2}


def test_changed_runtime_discards_completed_result(tmp_path: Path) -> None:
    """A result from another runtime path must never satisfy the current request."""
    workers = ManualWorkers()
    runner = UsageExportRunner(start_worker=workers.start)
    old = _context(tmp_path, runtime_name="old")
    current = _context(tmp_path, runtime_name="current")

    assert (
        runner.poll(old, lambda: {"runtime": "old"}, context_is_current=lambda: True).status
        is UsageExportStatus.PENDING
    )
    workers.run_next()
    assert (
        runner.poll(current, lambda: {"runtime": "current"}, context_is_current=lambda: True).status
        is UsageExportStatus.PENDING
    )
    workers.run_next()
    assert runner.poll(current, dict, context_is_current=lambda: True).report == {"runtime": "current"}


def test_runner_is_scoped_to_fastapi_app(tmp_path: Path) -> None:
    """Two applications must never share a worker or cached report."""
    first_app = FastAPI()
    second_app = FastAPI()
    main.initialize_api_app(first_app, _runtime_paths(tmp_path, "first"))
    main.initialize_api_app(second_app, _runtime_paths(tmp_path, "second"))

    first = usage_export_runner(first_app)
    second = usage_export_runner(second_app)

    assert first is not second
    first.close()
    second.close()


def test_close_cancels_queued_work_without_running_or_waiting(tmp_path: Path) -> None:
    """Lifecycle cleanup must not run queued work or join a worker."""
    workers = ManualWorkers()
    runner = UsageExportRunner(start_worker=workers.start)
    context = _context(tmp_path)
    build_calls = 0
    context_checks = 0

    def build() -> dict[str, object]:
        nonlocal build_calls
        build_calls += 1
        return {"unexpected": True}

    def context_is_current() -> bool:
        nonlocal context_checks
        context_checks += 1
        return True

    assert runner.poll(context, build, context_is_current=context_is_current).status is UsageExportStatus.PENDING
    runner.close()
    workers.run_next()

    assert build_calls == 0
    assert context_checks == 0
    assert runner.poll(context, build, context_is_current=lambda: True).status is UsageExportStatus.FAILED


def test_app_cleanup_retires_runner_without_waiting(tmp_path: Path) -> None:
    """Application cleanup must detach and close its runner immediately."""
    api_app = FastAPI()
    main.initialize_api_app(api_app, _runtime_paths(tmp_path))
    workers = ManualWorkers()
    runner = UsageExportRunner(start_worker=workers.start)
    config_state = config_lifecycle.app_state(api_app)
    config_state.usage_export_runner = runner

    usage_export.close_usage_export_runner(api_app)

    assert runner.closed
    assert config_state.usage_export_runner is None


def test_default_worker_is_detached_from_request_and_shutdown_wait(tmp_path: Path) -> None:
    """Production workers must be daemon threads that cleanup never joins."""
    entered: Future[bool] = Future()
    release = threading.Event()
    runner = UsageExportRunner()

    def build() -> dict[str, object]:
        entered.set_result(threading.current_thread().daemon)
        release.wait()
        return {"finished": True}

    try:
        pending = runner.poll(_context(tmp_path), build, context_is_current=lambda: True)
        assert pending.status is UsageExportStatus.PENDING
        assert entered.result(timeout=1) is True
        runner.close()
        assert runner.closed
    finally:
        release.set()
