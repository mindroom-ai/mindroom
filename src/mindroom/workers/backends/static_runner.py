"""Static shared sandbox-runner backend for the primary MindRoom runtime."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from mindroom.tool_system.worker_routing import worker_dir_name
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.models import WorkerHandle, WorkerSpec, WorkerStatus

_DEFAULT_IDLE_TIMEOUT_SECONDS = 1800.0
_SANDBOX_RUNNER_API_ROOT = "/api/sandbox-runner"


def normalize_static_runner_api_root(base_url: str) -> str:
    """Normalize a configured sandbox-runner URL into its API root."""
    normalized = base_url.strip().rstrip("/")
    if not normalized:
        return normalized
    if normalized.endswith("/execute"):
        return normalized.removesuffix("/execute")
    if normalized.endswith(_SANDBOX_RUNNER_API_ROOT):
        return normalized
    return f"{normalized}{_SANDBOX_RUNNER_API_ROOT}"


@dataclass
class _StaticWorkerMetadata:
    worker_id: str
    worker_key: str
    created_at: float
    last_used_at: float
    status: WorkerStatus
    last_started_at: float | None = None
    startup_count: int = 0
    failure_count: int = 0
    failure_reason: str | None = None


class StaticSandboxRunnerBackend:
    """Worker backend representing the current shared sandbox-runner deployment."""

    backend_name = "static_sandbox_runner"

    def __init__(
        self,
        *,
        api_root: str,
        auth_token: str | None,
        idle_timeout_seconds: float = _DEFAULT_IDLE_TIMEOUT_SECONDS,
    ) -> None:
        self.api_root = normalize_static_runner_api_root(api_root)
        self.auth_token = auth_token
        self.idle_timeout_seconds = max(1.0, idle_timeout_seconds)
        self._lock = threading.Lock()
        self._workers: dict[str, _StaticWorkerMetadata] = {}

    def shutdown(self) -> None:
        """Drop cached shared-runner worker handles before manager replacement."""
        with self._lock:
            self._workers.clear()

    def ensure_worker(self, spec: WorkerSpec, *, now: float | None = None) -> WorkerHandle:
        """Resolve or create one worker handle for the shared sandbox runner."""
        if not self.api_root:
            msg = "MINDROOM_SANDBOX_PROXY_URL must be set when sandbox proxying is enabled."
            raise WorkerBackendError(msg)
        if self.auth_token is None:
            msg = "MINDROOM_SANDBOX_PROXY_TOKEN must be set when sandbox proxying is enabled."
            raise WorkerBackendError(msg)

        timestamp = time.time() if now is None else now
        with self._lock:
            metadata = self._workers.get(spec.worker_key)
            if metadata is None:
                metadata = _StaticWorkerMetadata(
                    worker_id=worker_dir_name(spec.worker_key),
                    worker_key=spec.worker_key,
                    created_at=timestamp,
                    last_used_at=timestamp,
                    status="ready",
                    last_started_at=timestamp,
                    startup_count=1,
                )
            else:
                if self._effective_status(metadata, timestamp) == "idle":
                    metadata.last_started_at = timestamp
                    metadata.startup_count += 1
                metadata.status = "ready"
                metadata.last_used_at = timestamp
                metadata.failure_reason = None
            self._workers[spec.worker_key] = metadata
            return self._to_handle(metadata, now=timestamp)

    def get_worker(self, worker_key: str, *, now: float | None = None) -> WorkerHandle | None:
        """Return one known shared-runner worker handle."""
        timestamp = time.time() if now is None else now
        with self._lock:
            metadata = self._workers.get(worker_key)
            if metadata is None:
                return None
            return self._to_handle(metadata, now=timestamp)

    def touch_worker(self, worker_key: str, *, now: float | None = None) -> WorkerHandle | None:
        """Refresh last-used bookkeeping for one shared-runner worker."""
        timestamp = time.time() if now is None else now
        with self._lock:
            metadata = self._workers.get(worker_key)
            if metadata is None:
                return None
            metadata.last_used_at = timestamp
            return self._to_handle(metadata, now=timestamp)

    def list_workers(self, *, include_idle: bool = True, now: float | None = None) -> list[WorkerHandle]:
        """List workers seen through the shared-runner provider."""
        timestamp = time.time() if now is None else now
        with self._lock:
            handles = [self._to_handle(metadata, now=timestamp) for metadata in self._workers.values()]
        if not include_idle:
            handles = [handle for handle in handles if handle.status != "idle"]
        return sorted(handles, key=lambda handle: handle.last_used_at, reverse=True)

    def evict_worker(
        self,
        worker_key: str,
        *,
        preserve_state: bool = True,
        now: float | None = None,
    ) -> WorkerHandle | None:
        """Evict one shared-runner worker handle."""
        timestamp = time.time() if now is None else now
        with self._lock:
            metadata = self._workers.get(worker_key)
            if metadata is None:
                return None
            if preserve_state:
                metadata.status = "idle"
                metadata.last_used_at = timestamp
                return self._to_handle(metadata, now=timestamp)
            self._workers.pop(worker_key, None)
            return None

    def cleanup_idle_workers(self, *, now: float | None = None) -> list[WorkerHandle]:
        """Mark idle shared-runner workers inactive."""
        timestamp = time.time() if now is None else now
        cleaned_workers: list[WorkerHandle] = []
        with self._lock:
            for metadata in self._workers.values():
                if metadata.status == "ready" and self._effective_status(metadata, timestamp) == "idle":
                    metadata.status = "idle"
                    cleaned_workers.append(self._to_handle(metadata, now=timestamp))
        return sorted(cleaned_workers, key=lambda handle: handle.last_used_at, reverse=True)

    def record_failure(self, worker_key: str, failure_reason: str, *, now: float | None = None) -> WorkerHandle:
        """Persist one shared-runner worker failure."""
        timestamp = time.time() if now is None else now
        with self._lock:
            metadata = self._workers.get(worker_key)
            if metadata is None:
                metadata = _StaticWorkerMetadata(
                    worker_id=worker_dir_name(worker_key),
                    worker_key=worker_key,
                    created_at=timestamp,
                    last_used_at=timestamp,
                    status="failed",
                )
            metadata.status = "failed"
            metadata.last_used_at = timestamp
            metadata.failure_count += 1
            metadata.failure_reason = failure_reason
            self._workers[worker_key] = metadata
            return self._to_handle(metadata, now=timestamp)

    def _effective_status(self, metadata: _StaticWorkerMetadata, now: float) -> WorkerStatus:
        if metadata.status == "ready" and now - metadata.last_used_at >= self.idle_timeout_seconds:
            return "idle"
        return metadata.status

    def _to_handle(self, metadata: _StaticWorkerMetadata, *, now: float) -> WorkerHandle:
        return WorkerHandle(
            worker_id=metadata.worker_id,
            worker_key=metadata.worker_key,
            endpoint=f"{self.api_root}/execute",
            auth_token=self.auth_token,
            status=self._effective_status(metadata, now),
            backend_name=self.backend_name,
            last_used_at=metadata.last_used_at,
            created_at=metadata.created_at,
            last_started_at=metadata.last_started_at,
            expires_at=None,
            startup_count=metadata.startup_count,
            failure_count=metadata.failure_count,
            failure_reason=metadata.failure_reason,
            debug_metadata={"api_root": self.api_root},
        )
