"""Tests for backend-neutral worker lifecycle helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.workers.backend import effective_idle_status, filter_and_sort_worker_handles
from mindroom.workers.backends import local as local_module
from mindroom.workers.backends._lifecycle import (
    _WorkerLifecycleState,
    mark_worker_failed,
    mark_worker_idle,
    touch_worker_lifecycle,
)
from mindroom.workers.backends._metadata_store import save_worker_metadata
from mindroom.workers.backends.static_runner import StaticSandboxRunnerBackend
from mindroom.workers.models import WorkerHandle, WorkerSpec, WorkerStatus

if TYPE_CHECKING:
    from pathlib import Path


def _handle(worker_key: str, *, status: WorkerStatus, last_used_at: float) -> WorkerHandle:
    return WorkerHandle(
        worker_id=f"worker-{worker_key}",
        worker_key=worker_key,
        endpoint="http://worker/api/sandbox-runner/execute",
        auth_token=None,
        status=status,
        backend_name="test",
        last_used_at=last_used_at,
        created_at=0.0,
    )


def test_effective_idle_status_only_marks_ready_workers_idle_at_timeout_boundary() -> None:
    """Idle timeout presentation should only affect ready workers at or beyond the timeout."""
    assert effective_idle_status("ready", 10.0, 5.0, 14.99) == "ready"
    assert effective_idle_status("ready", 10.0, 5.0, 15.0) == "idle"
    assert effective_idle_status("starting", 10.0, 5.0, 20.0) == "starting"
    assert effective_idle_status("failed", 10.0, 5.0, 20.0) == "failed"


def test_filter_and_sort_worker_handles_hides_idle_workers_and_orders_by_recent_use() -> None:
    """Worker lists should preserve existing idle filtering and newest-first ordering."""
    handles = [
        _handle("old-ready", status="ready", last_used_at=10.0),
        _handle("idle", status="idle", last_used_at=30.0),
        _handle("new-ready", status="ready", last_used_at=20.0),
    ]

    assert [handle.worker_key for handle in filter_and_sort_worker_handles(handles, True)] == [
        "idle",
        "new-ready",
        "old-ready",
    ]
    assert [handle.worker_key for handle in filter_and_sort_worker_handles(handles, False)] == [
        "new-ready",
        "old-ready",
    ]


def test_touch_revives_idle_worker_to_ready() -> None:
    """Touching an idle worker brings it back to ready and refreshes last-used."""
    state = _WorkerLifecycleState(created_at=1.0, last_used_at=1.0, status="idle")
    revived = touch_worker_lifecycle(state, now=50.0)
    assert revived.status == "ready"
    assert revived.last_used_at == 50.0


def test_touch_clears_stale_failure_reason_when_not_failed() -> None:
    """Reviving an idle worker clears a stale failure reason but keeps the count."""
    state = _WorkerLifecycleState(
        created_at=1.0,
        last_used_at=1.0,
        status="idle",
        failure_count=2,
        failure_reason="boom",
    )
    revived = touch_worker_lifecycle(state, now=50.0)
    assert revived.status == "ready"
    assert revived.failure_reason is None
    assert revived.failure_count == 2


def test_touch_keeps_failed_status_and_reason() -> None:
    """A failed worker is not revived by a touch."""
    state = _WorkerLifecycleState(created_at=1.0, last_used_at=1.0, status="failed", failure_reason="boom")
    touched = touch_worker_lifecycle(state, now=50.0)
    assert touched.status == "failed"
    assert touched.failure_reason == "boom"


def test_mark_idle_clears_failure_reason() -> None:
    """Idling a worker clears any leftover failure reason."""
    state = _WorkerLifecycleState(created_at=1.0, last_used_at=1.0, status="ready", failure_reason="boom")
    idled = mark_worker_idle(state)
    assert idled.status == "idle"
    assert idled.failure_reason is None


def test_mark_failed_increments_count_and_records_reason() -> None:
    """Failing a worker records the reason and increments the failure count."""
    state = _WorkerLifecycleState(created_at=1.0, last_used_at=1.0, status="ready", failure_count=1)
    failed = mark_worker_failed(state, now=9.0, failure_reason="kaboom")
    assert failed.status == "failed"
    assert failed.failure_count == 2
    assert failed.failure_reason == "kaboom"
    assert failed.last_used_at == 9.0


def test_static_backend_touch_revives_idle_worker() -> None:
    """The static backend adopts the helper: a touch revives an idled worker."""
    backend = StaticSandboxRunnerBackend(
        api_root="http://runner",
        auth_token="tok",  # noqa: S106
        idle_timeout_seconds=10.0,
    )
    backend.ensure_worker(WorkerSpec(worker_key="v1:t:shared:a"), now=0.0)
    backend.evict_worker("v1:t:shared:a", preserve_state=True, now=0.0)

    idled = backend.get_worker("v1:t:shared:a", now=0.0)
    assert idled is not None
    assert idled.status == "idle"

    touched = backend.touch_worker("v1:t:shared:a", now=1.0)
    assert touched is not None
    assert touched.status == "ready"


def test_static_backend_touch_does_not_revive_failed_worker() -> None:
    """Touching a failed static worker keeps it failed (only idle revives)."""
    backend = StaticSandboxRunnerBackend(
        api_root="http://runner",
        auth_token="tok",  # noqa: S106
        idle_timeout_seconds=10.0,
    )
    backend.ensure_worker(WorkerSpec(worker_key="v1:t:shared:a"), now=0.0)
    backend.record_failure("v1:t:shared:a", "boom", now=0.0)

    touched = backend.touch_worker("v1:t:shared:a", now=1.0)
    assert touched is not None
    assert touched.status == "failed"
    assert touched.failure_reason == "boom"


def test_local_backend_touch_revives_idle_worker_and_clears_failure(tmp_path: Path) -> None:
    """The local backend adopts the helper: a touch revives idle and clears stale failure."""
    backend = local_module._LocalWorkerBackend(
        worker_root=tmp_path / "workers",
        api_root="/api/sandbox-runner",
        idle_timeout_seconds=1800.0,
    )
    worker_key = "v1:t:shared:a"
    paths = local_module._local_worker_state_paths(worker_key, worker_root=backend.worker_root)
    paths.metadata_dir.mkdir(parents=True, exist_ok=True)
    save_worker_metadata(
        paths,
        local_module._LocalWorkerMetadata(
            worker_id="w",
            worker_key=worker_key,
            endpoint="/api/sandbox-runner/execute",
            backend_name=backend.backend_name,
            created_at=0.0,
            last_used_at=0.0,
            status="idle",
            failure_reason="boom",
        ),
    )

    touched = backend.touch_worker(worker_key, now=5.0)
    assert touched is not None
    assert touched.status == "ready"
    assert touched.failure_reason is None
