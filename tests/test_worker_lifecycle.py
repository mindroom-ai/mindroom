"""Tests for backend-neutral worker lifecycle helpers."""

from __future__ import annotations

from mindroom.workers.backend import effective_idle_status, filter_and_sort_worker_handles
from mindroom.workers.models import WorkerHandle, WorkerStatus


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
