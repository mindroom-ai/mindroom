"""Kubernetes-backed worker backend for the primary MindRoom runtime."""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

from mindroom.credentials import get_credentials_manager, sync_shared_credentials_to_worker
from mindroom.tool_system.worker_routing import is_unscoped_worker_key, worker_dir_name
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.models import WorkerHandle, WorkerSpec, WorkerStatus

from . import kubernetes_resources as resources
from .kubernetes_config import _KubernetesWorkerBackendConfig, kubernetes_backend_config_signature

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths

__all__ = [
    "KubernetesWorkerBackend",
    "_KubernetesWorkerBackendConfig",
    "kubernetes_backend_config_signature",
]


class KubernetesWorkerBackend:
    """Kubernetes-backed worker provider for dedicated worker pods."""

    backend_name = "kubernetes"

    def __init__(
        self,
        *,
        config: _KubernetesWorkerBackendConfig,
        auth_token: str | None,
        storage_root: Path,
    ) -> None:
        self.config = config
        self.auth_token = auth_token
        self.storage_root = storage_root.expanduser().resolve()
        self.idle_timeout_seconds = config.idle_timeout_seconds
        self._resources = resources.KubernetesResourceManager(config=config, auth_token=auth_token)
        self._worker_locks: dict[str, threading.Lock] = {}
        self._worker_locks_lock = threading.Lock()

    @classmethod
    def from_runtime(
        cls,
        runtime_paths: RuntimePaths,
        *,
        auth_token: str | None,
        storage_root: Path,
    ) -> KubernetesWorkerBackend:
        """Construct a backend instance from one explicit runtime context."""
        return cls(
            config=_KubernetesWorkerBackendConfig.from_runtime(runtime_paths),
            auth_token=auth_token,
            storage_root=storage_root,
        )

    def ensure_worker(self, spec: WorkerSpec, *, now: float | None = None) -> WorkerHandle:
        """Resolve or start the worker backing the given worker key."""
        with self._worker_lock(spec.worker_key):
            timestamp = time.time() if now is None else now
            worker_key = spec.worker_key
            worker_id = self._worker_id(worker_key)
            state_subpath = self._state_subpath(worker_key)
            existing = self._resources.read_deployment(worker_id)
            current_handle = self._handle_from_deployment(existing, now=timestamp) if existing is not None else None
            should_restart = current_handle is None or current_handle.status in {"idle", "failed"}
            startup_count = (current_handle.startup_count if current_handle is not None else 0) + int(should_restart)
            created_at = current_handle.created_at if current_handle is not None else timestamp
            if should_restart:
                last_started_at = timestamp
            else:
                assert current_handle is not None
                last_started_at = current_handle.last_started_at
            annotations = resources.metadata_annotations(
                worker_key=worker_key,
                state_subpath=state_subpath,
                created_at=created_at,
                last_used_at=timestamp,
                last_started_at=last_started_at,
                startup_count=startup_count,
                failure_count=current_handle.failure_count if current_handle is not None else 0,
                failure_reason=None,
                status="starting",
            )

            sync_shared_credentials_to_worker(
                worker_key,
                include_ui_credentials=is_unscoped_worker_key(worker_key),
                credentials_manager=get_credentials_manager(storage_root=self.storage_root),
            )
            self._resources.apply_service(worker_id)
            self._resources.apply_deployment(
                worker_key=worker_key,
                worker_id=worker_id,
                state_subpath=state_subpath,
                annotations=annotations,
                replicas=1,
            )
            try:
                deployment = self._resources.wait_for_ready(
                    worker_id,
                    timeout_seconds=self.config.ready_timeout_seconds,
                    deployment_ready_fn=self._deployment_ready,
                )
            except Exception as exc:
                failure_reason = str(exc)
                self.record_failure(worker_key, failure_reason, now=timestamp)
                if isinstance(exc, WorkerBackendError):
                    raise
                raise WorkerBackendError(failure_reason) from exc

            final_annotations = dict(annotations)
            final_annotations[resources.ANNOTATION_WORKER_STATUS] = "ready"
            self._resources.patch_deployment(worker_id, annotations=final_annotations)
            deployment.metadata.annotations = final_annotations
            return self._handle_from_deployment(deployment, now=timestamp)

    def get_worker(self, worker_key: str, *, now: float | None = None) -> WorkerHandle | None:
        """Return the current worker handle for one worker key, if present."""
        deployment = self._resources.read_deployment(self._worker_id(worker_key))
        if deployment is None:
            return None
        timestamp = time.time() if now is None else now
        return self._handle_from_deployment(deployment, now=timestamp)

    def touch_worker(self, worker_key: str, *, now: float | None = None) -> WorkerHandle | None:
        """Refresh last-used metadata for one existing worker."""
        timestamp = time.time() if now is None else now
        worker_id = self._worker_id(worker_key)
        deployment = self._resources.read_deployment(worker_id)
        if deployment is None:
            return None

        annotations = dict(deployment.metadata.annotations or {})
        annotations[resources.ANNOTATION_LAST_USED_AT] = str(timestamp)
        if annotations.get(resources.ANNOTATION_WORKER_STATUS) == "idle":
            annotations[resources.ANNOTATION_WORKER_STATUS] = "ready"
        self._resources.patch_deployment(worker_id, annotations=annotations)
        deployment.metadata.annotations = annotations
        return self._handle_from_deployment(deployment, now=timestamp)

    def list_workers(self, *, include_idle: bool = True, now: float | None = None) -> list[WorkerHandle]:
        """List workers known to this backend."""
        timestamp = time.time() if now is None else now
        handles = [
            self._handle_from_deployment(deployment, now=timestamp) for deployment in self._resources.list_deployments()
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
        """Evict a worker and optionally retain its persisted state."""
        timestamp = time.time() if now is None else now
        worker_id = self._worker_id(worker_key)
        deployment = self._resources.read_deployment(worker_id)
        if deployment is None:
            return None
        if not preserve_state:
            self._resources.delete_deployment(worker_id)
            self._resources.delete_service(worker_id)
            return None

        annotations = dict(deployment.metadata.annotations or {})
        annotations[resources.ANNOTATION_LAST_USED_AT] = str(timestamp)
        annotations[resources.ANNOTATION_WORKER_STATUS] = "idle"
        self._resources.patch_deployment(worker_id, replicas=0, annotations=annotations)
        self._resources.delete_service(worker_id)
        deployment.spec.replicas = 0
        deployment.metadata.annotations = annotations
        return self._handle_from_deployment(deployment, now=timestamp)

    def cleanup_idle_workers(self, *, now: float | None = None) -> list[WorkerHandle]:
        """Scale idle workers to zero while retaining their state."""
        timestamp = time.time() if now is None else now
        cleaned: list[WorkerHandle] = []
        for deployment in self._resources.list_deployments():
            handle = self._handle_from_deployment(deployment, now=timestamp)
            if handle.status != "idle" or int(deployment.spec.replicas or 0) == 0:
                continue
            annotations = dict(deployment.metadata.annotations or {})
            annotations[resources.ANNOTATION_WORKER_STATUS] = "idle"
            self._resources.patch_deployment(handle.worker_id, replicas=0, annotations=annotations)
            self._resources.delete_service(handle.worker_id)
            deployment.spec.replicas = 0
            deployment.metadata.annotations = annotations
            cleaned.append(self._handle_from_deployment(deployment, now=timestamp))
        return cleaned

    def record_failure(self, worker_key: str, failure_reason: str, *, now: float | None = None) -> WorkerHandle:
        """Persist a failed worker startup or execution state."""
        timestamp = time.time() if now is None else now
        worker_id = self._worker_id(worker_key)
        deployment = self._resources.read_deployment(worker_id)
        if deployment is None:
            msg = f"Unknown worker '{worker_key}' for Kubernetes failure recording."
            raise WorkerBackendError(msg)

        annotations = dict(deployment.metadata.annotations or {})
        annotations[resources.ANNOTATION_LAST_USED_AT] = str(timestamp)
        annotations[resources.ANNOTATION_WORKER_STATUS] = "failed"
        annotations[resources.ANNOTATION_FAILURE_REASON] = failure_reason
        annotations[resources.ANNOTATION_FAILURE_COUNT] = str(
            resources.parse_annotation_int(annotations, resources.ANNOTATION_FAILURE_COUNT) + 1,
        )
        self._resources.patch_deployment(worker_id, replicas=0, annotations=annotations)
        self._resources.delete_service(worker_id)
        deployment.spec.replicas = 0
        deployment.metadata.annotations = annotations
        return self._handle_from_deployment(deployment, now=timestamp)

    def _worker_lock(self, worker_key: str) -> threading.Lock:
        with self._worker_locks_lock:
            worker_lock = self._worker_locks.get(worker_key)
            if worker_lock is None:
                worker_lock = threading.Lock()
                self._worker_locks[worker_key] = worker_lock
        return worker_lock

    def _worker_id(self, worker_key: str) -> str:
        return resources.worker_id_for_key(worker_key, prefix=self.config.name_prefix)

    def _state_subpath(self, worker_key: str) -> str:
        prefix = self.config.storage_subpath_prefix.strip().strip("/")
        worker_dir = worker_dir_name(worker_key)
        return f"{prefix}/{worker_dir}" if prefix else worker_dir

    def _deployment_ready(self, deployment: resources.KubernetesDeployment) -> bool:
        desired = int(deployment.spec.replicas or 0)
        if desired == 0:
            return True
        ready = int(deployment.status.ready_replicas or 0)
        observed_generation = deployment.status.observed_generation
        generation = deployment.metadata.generation
        generation_ready = observed_generation is None or generation is None or observed_generation >= generation
        return generation_ready and ready >= desired

    def _handle_from_deployment(self, deployment: resources.KubernetesDeployment, *, now: float) -> WorkerHandle:
        metadata = deployment.metadata
        annotations = dict(metadata.annotations or {})
        worker_key = annotations.get(resources.ANNOTATION_WORKER_KEY)
        if not worker_key:
            msg = f"Deployment '{metadata.name}' is missing worker metadata."
            raise WorkerBackendError(msg)

        worker_id = str(metadata.name)
        last_used_at = resources.parse_annotation_float(annotations, resources.ANNOTATION_LAST_USED_AT, now)
        created_at = resources.parse_annotation_float(annotations, resources.ANNOTATION_CREATED_AT, last_used_at)
        last_started_at = annotations.get(resources.ANNOTATION_LAST_STARTED_AT)
        status = self._effective_status(deployment, now=now)
        endpoint_root = resources.service_host(worker_id, self.config.namespace, self.config.worker_port)
        return WorkerHandle(
            worker_id=worker_id,
            worker_key=worker_key,
            endpoint=f"{endpoint_root}/api/sandbox-runner/execute",
            auth_token=self.auth_token,
            status=status,
            backend_name=self.backend_name,
            last_used_at=last_used_at,
            created_at=created_at,
            last_started_at=float(last_started_at) if last_started_at is not None else None,
            expires_at=None,
            startup_count=resources.parse_annotation_int(annotations, resources.ANNOTATION_STARTUP_COUNT),
            failure_count=resources.parse_annotation_int(annotations, resources.ANNOTATION_FAILURE_COUNT),
            failure_reason=annotations.get(resources.ANNOTATION_FAILURE_REASON),
            debug_metadata={
                "namespace": self.config.namespace,
                "deployment_name": worker_id,
                "service_name": worker_id,
                "state_subpath": annotations.get(resources.ANNOTATION_STATE_SUBPATH, ""),
                "api_root": f"{endpoint_root}/api/sandbox-runner",
            },
        )

    def _effective_status(self, deployment: resources.KubernetesDeployment, *, now: float) -> WorkerStatus:
        annotations = dict(deployment.metadata.annotations or {})
        stored_status = annotations.get(resources.ANNOTATION_WORKER_STATUS, "starting")
        if stored_status == "failed":
            return "failed"
        replicas = int(deployment.spec.replicas or 0)
        if replicas == 0:
            return "idle"
        if not self._deployment_ready(deployment):
            return "starting"
        last_used_at = resources.parse_annotation_float(annotations, resources.ANNOTATION_LAST_USED_AT, now)
        if now - last_used_at >= self.idle_timeout_seconds:
            return "idle"
        return "ready"
