"""Worker manager facade."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mindroom.workers.backend import WorkerBackend
    from mindroom.workers.models import WorkerHandle, WorkerSpec


@dataclass(slots=True)
class WorkerManager:
    """Facade over one concrete worker backend."""

    backend: WorkerBackend

    @property
    def backend_name(self) -> str:
        """Return the configured backend name."""
        return self.backend.backend_name

    @property
    def idle_timeout_seconds(self) -> float:
        """Return the configured backend idle timeout."""
        return self.backend.idle_timeout_seconds

    def shutdown(self) -> None:
        """Release backend-owned resources before discarding this manager."""
        self.backend.shutdown()

    def ensure_worker(self, spec: WorkerSpec, *, now: float | None = None) -> WorkerHandle:
        """Resolve or create a worker."""
        return self.backend.ensure_worker(spec, now=now)

    def get_worker(self, worker_key: str, *, now: float | None = None) -> WorkerHandle | None:
        """Return a known worker handle, if present."""
        return self.backend.get_worker(worker_key, now=now)

    def touch_worker(self, worker_key: str, *, now: float | None = None) -> WorkerHandle | None:
        """Refresh last-used bookkeeping for a worker."""
        return self.backend.touch_worker(worker_key, now=now)

    def list_workers(self, *, include_idle: bool = True, now: float | None = None) -> list[WorkerHandle]:
        """List workers from the configured backend."""
        return self.backend.list_workers(include_idle=include_idle, now=now)

    def evict_worker(
        self,
        worker_key: str,
        *,
        preserve_state: bool = True,
        now: float | None = None,
    ) -> WorkerHandle | None:
        """Evict a worker and optionally retain its state."""
        return self.backend.evict_worker(worker_key, preserve_state=preserve_state, now=now)

    def cleanup_idle_workers(self, *, now: float | None = None) -> list[WorkerHandle]:
        """Apply idle-worker cleanup on the backend."""
        return self.backend.cleanup_idle_workers(now=now)

    def record_failure(self, worker_key: str, failure_reason: str, *, now: float | None = None) -> WorkerHandle:
        """Record a worker failure for observability."""
        return self.backend.record_failure(worker_key, failure_reason, now=now)
