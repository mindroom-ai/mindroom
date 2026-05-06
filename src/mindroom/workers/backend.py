"""Worker backend protocol."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from mindroom.workers.models import ProgressSink, WorkerHandle, WorkerSpec, WorkerStatus


class WorkerBackendError(RuntimeError):
    """Raised when a worker backend cannot satisfy a request."""


def effective_idle_status(
    status: WorkerStatus,
    last_used_at: float,
    idle_timeout_seconds: float,
    now: float,
) -> WorkerStatus:
    """Return the effective status after applying ready-worker idle timeout."""
    if status == "ready" and now - last_used_at >= idle_timeout_seconds:
        return "idle"
    return status


def filter_and_sort_worker_handles(handles: list[WorkerHandle], include_idle: bool) -> list[WorkerHandle]:
    """Apply idle filtering and newest-first worker list ordering."""
    filtered_handles = list(handles)
    if not include_idle:
        filtered_handles = [handle for handle in filtered_handles if handle.status != "idle"]
    return sorted(filtered_handles, key=lambda handle: handle.last_used_at, reverse=True)


class WorkerBackend(Protocol):
    """Backend contract for realizing persistent workers."""

    backend_name: str
    idle_timeout_seconds: float

    def ensure_worker(
        self,
        spec: WorkerSpec,
        *,
        now: float | None = None,
        progress_sink: ProgressSink | None = None,
    ) -> WorkerHandle:
        """Resolve or create the worker described by *spec*."""

    def get_worker(self, worker_key: str, *, now: float | None = None) -> WorkerHandle | None:
        """Return the current handle for *worker_key*, if known."""

    def touch_worker(self, worker_key: str, *, now: float | None = None) -> WorkerHandle | None:
        """Update last-used bookkeeping for *worker_key*."""

    def list_workers(self, *, include_idle: bool = True, now: float | None = None) -> list[WorkerHandle]:
        """List known workers."""

    def evict_worker(
        self,
        worker_key: str,
        *,
        preserve_state: bool = True,
        now: float | None = None,
    ) -> WorkerHandle | None:
        """Evict a worker and optionally retain its state."""

    def cleanup_idle_workers(self, *, now: float | None = None) -> list[WorkerHandle]:
        """Apply idle cleanup to known workers."""

    def record_failure(self, worker_key: str, failure_reason: str, *, now: float | None = None) -> WorkerHandle:
        """Persist a worker failure for observability."""
