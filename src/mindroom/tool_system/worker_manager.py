"""Worker lifecycle management for persistent sandbox workers."""

from __future__ import annotations

import json
import os
import threading
import time
import venv
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from mindroom.tool_system.worker_routing import worker_dir_name

WorkerBackend = Literal["local_sandbox_runner"]
WorkerStatus = Literal["starting", "ready", "idle", "failed"]

_DEFAULT_IDLE_TIMEOUT_SECONDS = 1800.0
_DEFAULT_WORKER_ENDPOINT = "/api/sandbox-runner/execute"
_WORKER_ROOT_ENV = "MINDROOM_SANDBOX_WORKER_ROOT"
_WORKER_ENDPOINT_ENV = "MINDROOM_SANDBOX_WORKER_ENDPOINT"
_WORKER_IDLE_TIMEOUT_ENV = "MINDROOM_SANDBOX_WORKER_IDLE_TIMEOUT_SECONDS"


@dataclass(frozen=True)
class WorkerStatePaths:
    """Filesystem layout for one persistent worker scope."""

    root: Path
    workspace: Path
    venv_dir: Path
    cache_dir: Path
    storage_dir: Path
    metadata_dir: Path
    metadata_file: Path


@dataclass(frozen=True)
class WorkerHandle:
    """Resolved worker metadata returned to callers."""

    worker_key: str
    endpoint: str
    state_root: Path
    status: WorkerStatus
    backend: WorkerBackend
    last_seen_at: float
    created_at: float
    last_started_at: float | None = None
    startup_count: int = 0
    failure_count: int = 0
    failure_reason: str | None = None


@dataclass(frozen=True)
class PreparedWorker:
    """Worker handle plus resolved filesystem paths."""

    handle: WorkerHandle
    paths: WorkerStatePaths


@dataclass
class _WorkerMetadata:
    """Persisted worker metadata stored alongside worker state."""

    worker_key: str
    endpoint: str
    backend: WorkerBackend
    created_at: float
    last_seen_at: float
    status: WorkerStatus
    last_started_at: float | None = None
    startup_count: int = 0
    failure_count: int = 0
    failure_reason: str | None = None


class WorkerManagerError(RuntimeError):
    """Raised when a worker cannot be prepared for execution."""


def _default_worker_root() -> Path:
    configured_root = os.getenv(_WORKER_ROOT_ENV)
    if configured_root:
        return Path(configured_root).expanduser().resolve()

    storage_path = os.getenv("MINDROOM_STORAGE_PATH", "/app/workspace/.mindroom")
    return Path(storage_path).expanduser().resolve() / "workers"


def _read_idle_timeout_seconds() -> float:
    raw_timeout = os.getenv(_WORKER_IDLE_TIMEOUT_ENV, str(_DEFAULT_IDLE_TIMEOUT_SECONDS))
    try:
        timeout = float(raw_timeout)
    except ValueError:
        timeout = _DEFAULT_IDLE_TIMEOUT_SECONDS
    return max(1.0, timeout)


def _read_worker_endpoint() -> str:
    value = os.getenv(_WORKER_ENDPOINT_ENV, _DEFAULT_WORKER_ENDPOINT).strip()
    return value or _DEFAULT_WORKER_ENDPOINT


class LocalWorkerManager:
    """Own logical worker lifecycle for the local sandbox runner backend."""

    def __init__(
        self,
        *,
        worker_root: Path,
        endpoint: str,
        idle_timeout_seconds: float,
    ) -> None:
        self.worker_root = worker_root.expanduser().resolve()
        self.endpoint = endpoint
        self.idle_timeout_seconds = max(1.0, idle_timeout_seconds)
        self.worker_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def state_paths(self, worker_key: str) -> WorkerStatePaths:
        """Return the filesystem paths owned by a worker."""
        worker_root = self.worker_root / worker_dir_name(worker_key)
        metadata_dir = worker_root / "metadata"
        return WorkerStatePaths(
            root=worker_root,
            workspace=worker_root / "workspace",
            venv_dir=worker_root / "venv",
            cache_dir=worker_root / "cache",
            storage_dir=worker_root,
            metadata_dir=metadata_dir,
            metadata_file=metadata_dir / "worker.json",
        )

    def get_or_create_worker(self, worker_key: str, *, now: float | None = None) -> PreparedWorker:
        """Resolve a worker and ensure its state root exists."""
        timestamp = time.time() if now is None else now
        paths = self.state_paths(worker_key)

        with self._lock:
            metadata = self._load_metadata(paths) or self._default_metadata(worker_key, timestamp)
            if self._effective_status(metadata, timestamp) != "ready":
                metadata.status = "starting"
                metadata.last_started_at = timestamp
                metadata.startup_count += 1
                metadata.failure_reason = None
            metadata.last_seen_at = timestamp
            self._save_metadata(paths, metadata)

        try:
            self._ensure_worker_state(paths)
        except Exception as exc:
            failure_reason = f"Failed to initialize worker '{worker_key}': {exc}"
            self.record_failure(worker_key, failure_reason, now=timestamp)
            raise WorkerManagerError(failure_reason) from exc

        with self._lock:
            metadata = self._load_metadata(paths) or self._default_metadata(worker_key, timestamp)
            metadata.status = "ready"
            metadata.last_seen_at = timestamp
            self._save_metadata(paths, metadata)
            return PreparedWorker(
                handle=self._to_handle(metadata, paths, now=timestamp),
                paths=paths,
            )

    def list_workers(self, *, include_idle: bool = True, now: float | None = None) -> list[WorkerHandle]:
        """Return known workers from on-disk metadata."""
        timestamp = time.time() if now is None else now
        with self._lock:
            handles = [
                self._to_handle(metadata, paths, now=timestamp)
                for paths in self._metadata_paths()
                if (metadata := self._load_metadata(paths)) is not None
            ]

        if not include_idle:
            handles = [handle for handle in handles if handle.status != "idle"]
        return sorted(handles, key=lambda handle: handle.last_seen_at, reverse=True)

    def cleanup_idle_workers(self, *, now: float | None = None) -> list[WorkerHandle]:
        """Mark timed-out workers idle while retaining their state on disk."""
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

        return sorted(cleaned_workers, key=lambda handle: handle.last_seen_at, reverse=True)

    def record_failure(self, worker_key: str, failure_reason: str, *, now: float | None = None) -> WorkerHandle:
        """Persist a worker failure for later observability."""
        timestamp = time.time() if now is None else now
        paths = self.state_paths(worker_key)

        with self._lock:
            metadata = self._load_metadata(paths) or self._default_metadata(worker_key, timestamp)
            metadata.status = "failed"
            metadata.last_seen_at = timestamp
            metadata.failure_count += 1
            metadata.failure_reason = failure_reason
            self._save_metadata(paths, metadata)
            return self._to_handle(metadata, paths, now=timestamp)

    def _default_metadata(self, worker_key: str, now: float) -> _WorkerMetadata:
        return _WorkerMetadata(
            worker_key=worker_key,
            endpoint=self.endpoint,
            backend="local_sandbox_runner",
            created_at=now,
            last_seen_at=now,
            status="starting",
        )

    def _ensure_worker_state(self, paths: WorkerStatePaths) -> None:
        paths.workspace.mkdir(parents=True, exist_ok=True)
        paths.cache_dir.mkdir(parents=True, exist_ok=True)
        paths.metadata_dir.mkdir(parents=True, exist_ok=True)
        if (paths.venv_dir / "bin" / "python").exists():
            return

        builder = venv.EnvBuilder(with_pip=True, system_site_packages=True)
        builder.create(paths.venv_dir)

    def _metadata_paths(self) -> list[WorkerStatePaths]:
        if not self.worker_root.exists():
            return []

        paths: list[WorkerStatePaths] = []
        for metadata_file in sorted(self.worker_root.glob("*/metadata/worker.json")):
            worker_root = metadata_file.parents[1]
            metadata_dir = metadata_file.parent
            paths.append(
                WorkerStatePaths(
                    root=worker_root,
                    workspace=worker_root / "workspace",
                    venv_dir=worker_root / "venv",
                    cache_dir=worker_root / "cache",
                    storage_dir=worker_root,
                    metadata_dir=metadata_dir,
                    metadata_file=metadata_file,
                ),
            )
        return paths

    def _load_metadata(self, paths: WorkerStatePaths) -> _WorkerMetadata | None:
        if not paths.metadata_file.exists():
            return None
        try:
            with paths.metadata_file.open(encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return None

        try:
            return _WorkerMetadata(**data)
        except TypeError:
            return None

    def _save_metadata(self, paths: WorkerStatePaths, metadata: _WorkerMetadata) -> None:
        paths.metadata_dir.mkdir(parents=True, exist_ok=True)
        with paths.metadata_file.open("w", encoding="utf-8") as f:
            json.dump(asdict(metadata), f, sort_keys=True)

    def _effective_status(self, metadata: _WorkerMetadata, now: float) -> WorkerStatus:
        if metadata.status == "ready" and now - metadata.last_seen_at >= self.idle_timeout_seconds:
            return "idle"
        return metadata.status

    def _to_handle(self, metadata: _WorkerMetadata, paths: WorkerStatePaths, *, now: float) -> WorkerHandle:
        return WorkerHandle(
            worker_key=metadata.worker_key,
            endpoint=self.endpoint,
            state_root=paths.root,
            status=self._effective_status(metadata, now),
            backend=metadata.backend,
            last_seen_at=metadata.last_seen_at,
            created_at=metadata.created_at,
            last_started_at=metadata.last_started_at,
            startup_count=metadata.startup_count,
            failure_count=metadata.failure_count,
            failure_reason=metadata.failure_reason,
        )


_worker_manager: LocalWorkerManager | None = None
_worker_manager_config: tuple[str, str, float] | None = None


def get_worker_manager() -> LocalWorkerManager:
    """Return the local worker manager for the current sandbox-runner config."""
    global _worker_manager, _worker_manager_config

    worker_root = _default_worker_root()
    endpoint = _read_worker_endpoint()
    idle_timeout_seconds = _read_idle_timeout_seconds()
    config = (str(worker_root), endpoint, idle_timeout_seconds)

    if _worker_manager is None or _worker_manager_config != config:
        _worker_manager = LocalWorkerManager(
            worker_root=worker_root,
            endpoint=endpoint,
            idle_timeout_seconds=idle_timeout_seconds,
        )
        _worker_manager_config = config

    return _worker_manager
