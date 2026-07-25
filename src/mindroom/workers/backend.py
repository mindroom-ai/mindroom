"""Worker backend protocol."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from mindroom.workers.models import WorkerMaintenanceResult

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

    def shutdown(self) -> None:
        """Release backend-owned runtime resources before discarding this manager."""

    def ensure_worker(
        self,
        spec: WorkerSpec,
        *,
        now: float | None = None,
        progress_sink: ProgressSink | None = None,
    ) -> WorkerHandle:
        """Resolve or create the worker described by *spec*."""

    def touch_worker(self, worker_key: str, *, now: float | None = None) -> WorkerHandle | None:
        """Update last-used bookkeeping for *worker_key*."""

    def list_workers(self, *, include_idle: bool = True, now: float | None = None) -> list[WorkerHandle]:
        """List known workers."""

    def cleanup_idle_workers(self, *, now: float | None = None) -> list[WorkerHandle]:
        """Apply idle cleanup to known workers."""

    def record_failure(self, worker_key: str, failure_reason: str, *, now: float | None = None) -> WorkerHandle:
        """Persist a worker failure for observability."""


@runtime_checkable
class _MaintainingWorkerBackend(Protocol):
    """Backends that own a maintenance pass richer than idle cleanup."""

    def maintain_workers(self, *, now: float | None = None) -> WorkerMaintenanceResult:
        """Run one backend-specific maintenance pass."""


def maintain_workers(backend: WorkerBackend, *, now: float | None = None) -> WorkerMaintenanceResult:
    """Run one maintenance pass, using the backend's own pass when it has one.

    Kept a function rather than a ``WorkerBackend`` default so the protocol stays
    structural: inheriting it would turn every doc-only method in this file into a
    ``None``-returning default that silently satisfies the interface.
    """
    if isinstance(backend, _MaintainingWorkerBackend):
        return backend.maintain_workers(now=now)
    return WorkerMaintenanceResult(cleaned=tuple(backend.cleanup_idle_workers(now=now)), reconciled=())
