"""Local persistent worker backend for the sandbox runner runtime."""

from __future__ import annotations

import json
import shutil
import threading
import time
import venv
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from mindroom.tool_system.worker_routing import worker_dir_name
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.manager import WorkerManager
from mindroom.workers.models import WorkerHandle, WorkerSpec, WorkerStatus

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths

_DEFAULT_IDLE_TIMEOUT_SECONDS = 1800.0
_DEFAULT_WORKER_API_ROOT = "/api/sandbox-runner"
_WORKER_ENDPOINT_ENV = "MINDROOM_SANDBOX_WORKER_ENDPOINT"
_WORKER_IDLE_TIMEOUT_ENV = "MINDROOM_SANDBOX_WORKER_IDLE_TIMEOUT_SECONDS"
_SHARED_INITIALIZATION_LOCK = threading.Lock()
_SHARED_INITIALIZATION_LOCKS: dict[str, threading.Lock] = {}


@dataclass(frozen=True)
class LocalWorkerStatePaths:
    """Filesystem layout for one local worker."""

    root: Path
    workspace: Path
    venv_dir: Path
    cache_dir: Path
    storage_dir: Path
    metadata_dir: Path
    metadata_file: Path


@dataclass
class _LocalWorkerMetadata:
    worker_id: str
    worker_key: str
    endpoint: str
    backend_name: str
    created_at: float
    last_used_at: float
    status: WorkerStatus
    last_started_at: float | None = None
    startup_count: int = 0
    failure_count: int = 0
    failure_reason: str | None = None


def _default_worker_root(runtime_paths: RuntimePaths) -> Path:
    return runtime_paths.storage_root.resolve() / "workers"


def _read_idle_timeout_seconds(runtime_paths: RuntimePaths) -> float:
    raw_timeout = runtime_paths.env_value(
        _WORKER_IDLE_TIMEOUT_ENV,
        default=str(_DEFAULT_IDLE_TIMEOUT_SECONDS),
    ) or str(_DEFAULT_IDLE_TIMEOUT_SECONDS)
    try:
        timeout = float(raw_timeout)
    except ValueError:
        timeout = _DEFAULT_IDLE_TIMEOUT_SECONDS
    return max(1.0, timeout)


def _normalize_worker_api_root(raw_endpoint: str) -> str:
    normalized = raw_endpoint.strip() or _DEFAULT_WORKER_API_ROOT
    normalized = normalized.rstrip("/")
    if normalized.endswith("/execute"):
        normalized = normalized.removesuffix("/execute")
    return normalized or _DEFAULT_WORKER_API_ROOT


def _read_worker_api_root(runtime_paths: RuntimePaths) -> str:
    raw_api_root = runtime_paths.env_value(_WORKER_ENDPOINT_ENV, default=_DEFAULT_WORKER_API_ROOT)
    return _normalize_worker_api_root(raw_api_root or _DEFAULT_WORKER_API_ROOT)


def _local_worker_state_paths_for_root(state_root: Path) -> LocalWorkerStatePaths:
    """Return the filesystem paths for one concrete worker runtime root."""
    resolved_root = state_root.expanduser().resolve()
    metadata_dir = resolved_root / "metadata"
    return LocalWorkerStatePaths(
        root=resolved_root,
        workspace=resolved_root / "workspace",
        venv_dir=resolved_root / "venv",
        cache_dir=resolved_root / "cache",
        storage_dir=resolved_root,
        metadata_dir=metadata_dir,
        metadata_file=metadata_dir / "worker.json",
    )


def local_worker_state_paths_for_root(state_root: Path) -> LocalWorkerStatePaths:
    """Return the filesystem paths owned by one concrete worker runtime root."""
    return _local_worker_state_paths_for_root(state_root)


def _local_worker_state_paths(worker_key: str, *, worker_root: Path) -> LocalWorkerStatePaths:
    """Return the runtime-local filesystem paths owned by one worker key."""
    resolved_root = worker_root.expanduser().resolve()
    return _local_worker_state_paths_for_root(resolved_root / worker_dir_name(worker_key))


def local_worker_state_paths_from_handle(handle: WorkerHandle) -> LocalWorkerStatePaths:
    """Resolve local runtime paths from a local worker handle."""
    state_root = handle.debug_metadata.get("state_root")
    if state_root is None:
        msg = f"Worker '{handle.worker_key}' does not expose local state metadata."
        raise WorkerBackendError(msg)
    return _local_worker_state_paths_for_root(Path(state_root))


def _ensure_local_worker_state(paths: LocalWorkerStatePaths) -> None:
    """Create the persistent directories and venv for one worker runtime root."""
    paths.workspace.mkdir(parents=True, exist_ok=True)
    paths.cache_dir.mkdir(parents=True, exist_ok=True)
    paths.metadata_dir.mkdir(parents=True, exist_ok=True)
    if (paths.venv_dir / "bin" / "python").exists():
        return

    builder = venv.EnvBuilder(with_pip=True, system_site_packages=True)
    builder.create(paths.venv_dir)


def ensure_local_worker_state_locked(worker_key: str, paths: LocalWorkerStatePaths) -> None:
    """Create one worker runtime root under a shared per-worker initialization lock."""
    with _shared_worker_initialization_lock(worker_key):
        _ensure_local_worker_state(paths)


def _shared_worker_initialization_lock(worker_key: str) -> threading.Lock:
    with _SHARED_INITIALIZATION_LOCK:
        worker_lock = _SHARED_INITIALIZATION_LOCKS.get(worker_key)
        if worker_lock is None:
            worker_lock = threading.Lock()
            _SHARED_INITIALIZATION_LOCKS[worker_key] = worker_lock
    return worker_lock


class _LocalWorkerBackend:
    """Persistent local worker backend used by the sandbox runner."""

    backend_name = "local_sandbox_runner"

    def __init__(
        self,
        *,
        worker_root: Path,
        api_root: str,
        idle_timeout_seconds: float,
    ) -> None:
        self.worker_root = worker_root.expanduser().resolve()
        self.api_root = _normalize_worker_api_root(api_root)
        self.idle_timeout_seconds = max(1.0, idle_timeout_seconds)
        self.worker_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._initialization_locks: dict[str, threading.Lock] = {}

    def ensure_worker(self, spec: WorkerSpec, *, now: float | None = None) -> WorkerHandle:
        """Resolve or create one local worker."""
        timestamp = time.time() if now is None else now
        worker_lock = self._worker_lock(spec.worker_key)
        paths = _local_worker_state_paths(spec.worker_key, worker_root=self.worker_root)

        with worker_lock:
            with self._lock:
                metadata = self._load_metadata(paths) or self._default_metadata(spec.worker_key, timestamp)
                if self._effective_status(metadata, timestamp) != "ready":
                    metadata.status = "starting"
                    metadata.last_started_at = timestamp
                    metadata.startup_count += 1
                    metadata.failure_reason = None
                metadata.last_used_at = timestamp
                self._save_metadata(paths, metadata)

            try:
                self._ensure_worker_state(paths)
            except Exception as exc:
                failure_reason = f"Failed to initialize worker '{spec.worker_key}': {exc}"
                with self._lock:
                    self._record_failure_locked(paths, spec.worker_key, failure_reason, now=timestamp)
                raise WorkerBackendError(failure_reason) from exc

            with self._lock:
                metadata.status = "ready"
                metadata.last_used_at = timestamp
                self._save_metadata(paths, metadata)
                return self._to_handle(metadata, paths, now=timestamp)

    def get_worker(self, worker_key: str, *, now: float | None = None) -> WorkerHandle | None:
        """Return one known local worker handle."""
        timestamp = time.time() if now is None else now
        paths = _local_worker_state_paths(worker_key, worker_root=self.worker_root)
        with self._lock:
            metadata = self._load_metadata(paths)
            if metadata is None:
                return None
            return self._to_handle(metadata, paths, now=timestamp)

    def touch_worker(self, worker_key: str, *, now: float | None = None) -> WorkerHandle | None:
        """Refresh last-used bookkeeping for one local worker."""
        timestamp = time.time() if now is None else now
        worker_lock = self._worker_lock(worker_key)
        paths = _local_worker_state_paths(worker_key, worker_root=self.worker_root)
        with worker_lock, self._lock:
            metadata = self._load_metadata(paths)
            if metadata is None:
                return None
            metadata.last_used_at = timestamp
            self._save_metadata(paths, metadata)
            return self._to_handle(metadata, paths, now=timestamp)

    def list_workers(self, *, include_idle: bool = True, now: float | None = None) -> list[WorkerHandle]:
        """List known local workers."""
        timestamp = time.time() if now is None else now
        with self._lock:
            handles = [
                self._to_handle(metadata, paths, now=timestamp)
                for paths in self._metadata_paths()
                if (metadata := self._load_metadata(paths)) is not None
            ]

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
        """Evict one local worker and optionally preserve its state."""
        timestamp = time.time() if now is None else now
        worker_lock = self._worker_lock(worker_key)
        paths = _local_worker_state_paths(worker_key, worker_root=self.worker_root)

        with worker_lock:
            with self._lock:
                metadata = self._load_metadata(paths)
                if metadata is None:
                    return None
                if preserve_state:
                    metadata.status = "idle"
                    metadata.last_used_at = timestamp
                    self._save_metadata(paths, metadata)
                    return self._to_handle(metadata, paths, now=timestamp)

            if paths.root.exists():
                shutil.rmtree(paths.root)
            return None

    def cleanup_idle_workers(self, *, now: float | None = None) -> list[WorkerHandle]:
        """Mark timed-out local workers idle."""
        timestamp = time.time() if now is None else now
        cleaned_workers: list[WorkerHandle] = []

        with self._lock:
            for paths in self._metadata_paths():
                metadata = self._load_metadata(paths)
                if metadata is None:
                    continue
                if metadata.status == "ready" and self._effective_status(metadata, timestamp) == "idle":
                    metadata.status = "idle"
                    self._save_metadata(paths, metadata)
                    cleaned_workers.append(self._to_handle(metadata, paths, now=timestamp))

        return sorted(cleaned_workers, key=lambda handle: handle.last_used_at, reverse=True)

    def record_failure(self, worker_key: str, failure_reason: str, *, now: float | None = None) -> WorkerHandle:
        """Persist one local worker failure."""
        timestamp = time.time() if now is None else now
        worker_lock = self._worker_lock(worker_key)
        paths = _local_worker_state_paths(worker_key, worker_root=self.worker_root)

        with worker_lock, self._lock:
            return self._record_failure_locked(paths, worker_key, failure_reason, now=timestamp)

    def _worker_lock(self, worker_key: str) -> threading.Lock:
        with self._lock:
            worker_lock = self._initialization_locks.get(worker_key)
            if worker_lock is None:
                worker_lock = threading.Lock()
                self._initialization_locks[worker_key] = worker_lock
            return worker_lock

    def _default_metadata(self, worker_key: str, now: float) -> _LocalWorkerMetadata:
        return _LocalWorkerMetadata(
            worker_id=worker_dir_name(worker_key),
            worker_key=worker_key,
            endpoint=f"{self.api_root}/execute",
            backend_name=self.backend_name,
            created_at=now,
            last_used_at=now,
            status="starting",
        )

    def _ensure_worker_state(self, paths: LocalWorkerStatePaths) -> None:
        _ensure_local_worker_state(paths)

    def _metadata_paths(self) -> list[LocalWorkerStatePaths]:
        if not self.worker_root.exists():
            return []

        return [
            _local_worker_state_paths_for_root(metadata_file.parents[1])
            for metadata_file in sorted(self.worker_root.glob("*/metadata/worker.json"))
        ]

    def _load_metadata(self, paths: LocalWorkerStatePaths) -> _LocalWorkerMetadata | None:
        if not paths.metadata_file.exists():
            return None
        try:
            with paths.metadata_file.open(encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return None

        try:
            return _LocalWorkerMetadata(**data)
        except TypeError:
            return None

    def _save_metadata(self, paths: LocalWorkerStatePaths, metadata: _LocalWorkerMetadata) -> None:
        paths.metadata_dir.mkdir(parents=True, exist_ok=True)
        with paths.metadata_file.open("w", encoding="utf-8") as f:
            json.dump(asdict(metadata), f, sort_keys=True)

    def _effective_status(self, metadata: _LocalWorkerMetadata, now: float) -> WorkerStatus:
        if metadata.status == "ready" and now - metadata.last_used_at >= self.idle_timeout_seconds:
            return "idle"
        return metadata.status

    def _record_failure_locked(
        self,
        paths: LocalWorkerStatePaths,
        worker_key: str,
        failure_reason: str,
        *,
        now: float,
    ) -> WorkerHandle:
        metadata = self._load_metadata(paths) or self._default_metadata(worker_key, now)
        metadata.status = "failed"
        metadata.last_used_at = now
        metadata.failure_count += 1
        metadata.failure_reason = failure_reason
        self._save_metadata(paths, metadata)
        return self._to_handle(metadata, paths, now=now)

    def _to_handle(self, metadata: _LocalWorkerMetadata, paths: LocalWorkerStatePaths, *, now: float) -> WorkerHandle:
        return WorkerHandle(
            worker_id=metadata.worker_id,
            worker_key=metadata.worker_key,
            endpoint=metadata.endpoint,
            auth_token=None,
            status=self._effective_status(metadata, now),
            backend_name=metadata.backend_name,
            last_used_at=metadata.last_used_at,
            created_at=metadata.created_at,
            last_started_at=metadata.last_started_at,
            expires_at=None,
            startup_count=metadata.startup_count,
            failure_count=metadata.failure_count,
            failure_reason=metadata.failure_reason,
            debug_metadata={
                "api_root": self.api_root,
                "state_root": str(paths.root),
            },
        )


_local_worker_manager: WorkerManager | None = None
_local_worker_manager_config: tuple[str, str, float] | None = None
_local_worker_manager_lock = threading.Lock()


def get_local_worker_manager(runtime_paths: RuntimePaths) -> WorkerManager:
    """Return the local sandbox worker manager for the current config."""
    global _local_worker_manager, _local_worker_manager_config

    worker_root = _default_worker_root(runtime_paths)
    api_root = _read_worker_api_root(runtime_paths)
    idle_timeout_seconds = _read_idle_timeout_seconds(runtime_paths)
    config = (str(worker_root), api_root, idle_timeout_seconds)

    with _local_worker_manager_lock:
        if _local_worker_manager is None or _local_worker_manager_config != config:
            _local_worker_manager = WorkerManager(
                _LocalWorkerBackend(
                    worker_root=worker_root,
                    api_root=api_root,
                    idle_timeout_seconds=idle_timeout_seconds,
                ),
            )
            _local_worker_manager_config = config

    return _local_worker_manager
