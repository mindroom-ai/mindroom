"""Worker backend protocol."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from mindroom.workers.models import WorkerHandle, WorkerSpec


class WorkerBackendError(RuntimeError):
    """Raised when a worker backend cannot satisfy a request."""


class WorkerBackend(Protocol):
    """Backend contract for realizing persistent workers."""

    backend_name: str
    idle_timeout_seconds: float

    def ensure_worker(self, spec: WorkerSpec, *, now: float | None = None) -> WorkerHandle:
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
