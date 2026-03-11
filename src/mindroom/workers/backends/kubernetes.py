"""Kubernetes-backed worker backend for the primary MindRoom runtime."""

from __future__ import annotations

import os
import threading
import time

from mindroom.credentials import sync_shared_credentials_to_worker
from mindroom.tool_system.worker_routing import is_unscoped_worker_key, worker_dir_name
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.models import WorkerHandle, WorkerSpec, WorkerStatus

from .kubernetes_client import (
    _ApiStatusError as ApiStatusError,
)
from .kubernetes_client import (
    _AppsApiProtocol as AppsApiProtocol,
)
from .kubernetes_client import (
    _CoreApiProtocol as CoreApiProtocol,
)
from .kubernetes_client import (
    _KubernetesDeployment as KubernetesDeployment,
)
from .kubernetes_client import (
    apply_deployment,
    apply_service,
    delete_deployment,
    delete_service,
    list_deployments,
    load_clients,
    patch_deployment,
    read_deployment,
    read_pod_node_name,
    wait_for_ready,
)
from .kubernetes_config import KubernetesWorkerBackendConfig, kubernetes_backend_config_signature
from .kubernetes_manifests import (
    ANNOTATION_CREATED_AT,
    ANNOTATION_FAILURE_COUNT,
    ANNOTATION_FAILURE_REASON,
    ANNOTATION_LAST_STARTED_AT,
    ANNOTATION_LAST_USED_AT,
    ANNOTATION_STARTUP_COUNT,
    ANNOTATION_STATE_SUBPATH,
    ANNOTATION_WORKER_KEY,
    ANNOTATION_WORKER_STATUS,
    deployment_manifest,
    list_selector,
    metadata_annotations,
    parse_annotation_float,
    parse_annotation_int,
    service_host,
    service_manifest,
    worker_id_for_key,
)

_HOSTNAME_ENV = "HOSTNAME"

__all__ = [
    "KubernetesWorkerBackend",
    "KubernetesWorkerBackendConfig",
    "kubernetes_backend_config_signature",
]


class KubernetesWorkerBackend:
    """Kubernetes-backed worker provider for dedicated worker pods."""

    backend_name = "kubernetes"

    def __init__(self, *, config: KubernetesWorkerBackendConfig, auth_token: str | None) -> None:
        self.config = config
        self.auth_token = auth_token
        self.idle_timeout_seconds = config.idle_timeout_seconds
        self._apps_api: AppsApiProtocol | None = None
        self._core_api: CoreApiProtocol | None = None
        self._api_exception_cls: type[ApiStatusError] | None = None
        self._worker_locks: dict[str, threading.Lock] = {}
        self._worker_locks_lock = threading.Lock()
        self._control_plane_node_name: str | None = None
        self._control_plane_node_name_loaded = False
        self._owner_reference: dict[str, object] | None = None
        self._owner_reference_loaded = False

    @classmethod
    def from_env(cls, *, auth_token: str | None) -> KubernetesWorkerBackend:
        """Construct a backend instance from environment-backed configuration."""
        return cls(config=KubernetesWorkerBackendConfig.from_env(), auth_token=auth_token)

    def ensure_worker(self, spec: WorkerSpec, *, now: float | None = None) -> WorkerHandle:
        """Resolve or start the worker backing the given worker key."""
        with self._worker_lock(spec.worker_key):
            timestamp = time.time() if now is None else now
            deployment_name = self._worker_id(spec.worker_key)
            state_subpath = self._state_subpath(spec.worker_key)
            worker_key = spec.worker_key
            existing = self._read_deployment(deployment_name)
            current_handle = self._handle_from_deployment(existing, now=timestamp) if existing is not None else None
            should_restart = current_handle is None or current_handle.status in {"idle", "failed"}
            startup_count = (current_handle.startup_count if current_handle is not None else 0) + (
                1 if should_restart else 0
            )
            created_at = current_handle.created_at if current_handle is not None else timestamp
            if should_restart:
                last_started_at = timestamp
            else:
                assert current_handle is not None
                last_started_at = current_handle.last_started_at
            annotations = metadata_annotations(
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
            )
            self._apply_service(deployment_name)
            self._apply_deployment(
                worker_key=worker_key,
                deployment_name=deployment_name,
                state_subpath=state_subpath,
                annotations=annotations,
                replicas=1,
            )
            try:
                deployment = self._wait_for_ready(deployment_name, timeout_seconds=self.config.ready_timeout_seconds)
            except Exception as exc:
                failure_reason = str(exc)
                self.record_failure(worker_key, failure_reason, now=timestamp)
                if isinstance(exc, WorkerBackendError):
                    raise
                raise WorkerBackendError(failure_reason) from exc
            final_annotations = dict(annotations)
            final_annotations[ANNOTATION_WORKER_STATUS] = "ready"
            self._patch_deployment_metadata(deployment_name, annotations=final_annotations)
            deployment.metadata.annotations = final_annotations
            return self._handle_from_deployment(deployment, now=timestamp)

    def get_worker(self, worker_key: str, *, now: float | None = None) -> WorkerHandle | None:
        """Return the current worker handle for one worker key, if present."""
        deployment = self._read_deployment(self._worker_id(worker_key))
        if deployment is None:
            return None
        timestamp = time.time() if now is None else now
        return self._handle_from_deployment(deployment, now=timestamp)

    def touch_worker(self, worker_key: str, *, now: float | None = None) -> WorkerHandle | None:
        """Refresh last-used metadata for one existing worker."""
        timestamp = time.time() if now is None else now
        deployment_name = self._worker_id(worker_key)
        deployment = self._read_deployment(deployment_name)
        if deployment is None:
            return None
        annotations = dict(deployment.metadata.annotations or {})
        annotations[ANNOTATION_LAST_USED_AT] = str(timestamp)
        if annotations.get(ANNOTATION_WORKER_STATUS) == "idle":
            annotations[ANNOTATION_WORKER_STATUS] = "ready"
        self._patch_deployment_metadata(deployment_name, annotations=annotations)
        deployment.metadata.annotations = annotations
        return self._handle_from_deployment(deployment, now=timestamp)

    def list_workers(self, *, include_idle: bool = True, now: float | None = None) -> list[WorkerHandle]:
        """List workers known to this backend."""
        timestamp = time.time() if now is None else now
        deployments = self._list_deployments()
        handles = [self._handle_from_deployment(deployment, now=timestamp) for deployment in deployments]
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
        deployment_name = self._worker_id(worker_key)
        deployment = self._read_deployment(deployment_name)
        if deployment is None:
            return None
        if not preserve_state:
            self._delete_deployment(deployment_name)
            self._delete_service(deployment_name)
            return None

        annotations = dict(deployment.metadata.annotations or {})
        annotations[ANNOTATION_LAST_USED_AT] = str(timestamp)
        annotations[ANNOTATION_WORKER_STATUS] = "idle"
        self._patch_deployment(deployment_name, replicas=0, annotations=annotations)
        self._delete_service(deployment_name)
        deployment.spec.replicas = 0
        deployment.metadata.annotations = annotations
        return self._handle_from_deployment(deployment, now=timestamp)

    def cleanup_idle_workers(self, *, now: float | None = None) -> list[WorkerHandle]:
        """Scale idle workers to zero while retaining their state."""
        timestamp = time.time() if now is None else now
        cleaned: list[WorkerHandle] = []
        for deployment in self._list_deployments():
            handle = self._handle_from_deployment(deployment, now=timestamp)
            if handle.status != "idle" or int(deployment.spec.replicas or 0) == 0:
                continue
            annotations = dict(deployment.metadata.annotations or {})
            annotations[ANNOTATION_WORKER_STATUS] = "idle"
            self._patch_deployment(handle.worker_id, replicas=0, annotations=annotations)
            self._delete_service(handle.worker_id)
            deployment.spec.replicas = 0
            deployment.metadata.annotations = annotations
            cleaned.append(self._handle_from_deployment(deployment, now=timestamp))
        return cleaned

    def record_failure(self, worker_key: str, failure_reason: str, *, now: float | None = None) -> WorkerHandle:
        """Persist a failed worker startup or execution state."""
        timestamp = time.time() if now is None else now
        deployment_name = self._worker_id(worker_key)
        deployment = self._read_deployment(deployment_name)
        if deployment is None:
            msg = f"Unknown worker '{worker_key}' for Kubernetes failure recording."
            raise WorkerBackendError(msg)
        annotations = dict(deployment.metadata.annotations or {})
        annotations[ANNOTATION_LAST_USED_AT] = str(timestamp)
        annotations[ANNOTATION_WORKER_STATUS] = "failed"
        annotations[ANNOTATION_FAILURE_REASON] = failure_reason
        annotations[ANNOTATION_FAILURE_COUNT] = str(
            parse_annotation_int(annotations, ANNOTATION_FAILURE_COUNT) + 1,
        )
        self._patch_deployment(deployment_name, replicas=0, annotations=annotations)
        self._delete_service(deployment_name)
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
        return worker_id_for_key(worker_key, prefix=self.config.name_prefix)

    def _state_subpath(self, worker_key: str) -> str:
        prefix = self.config.storage_subpath_prefix.strip().strip("/")
        worker_dir = worker_dir_name(worker_key)
        return f"{prefix}/{worker_dir}" if prefix else worker_dir

    def _load_clients(self) -> None:
        self._apps_api, self._core_api, self._api_exception_cls = load_clients(
            apps_api=self._apps_api,
            core_api=self._core_api,
            api_exception_cls=self._api_exception_cls,
        )

    @property
    def _apps(self) -> AppsApiProtocol:
        self._load_clients()
        assert self._apps_api is not None
        return self._apps_api

    @property
    def _core(self) -> CoreApiProtocol:
        self._load_clients()
        assert self._core_api is not None
        return self._core_api

    @property
    def _api_exception(self) -> type[ApiStatusError]:
        self._load_clients()
        assert self._api_exception_cls is not None
        return self._api_exception_cls

    def _list_selector(self) -> str:
        return list_selector(extra_labels=self.config.extra_labels)

    def _apply_service(self, worker_id: str) -> None:
        apply_service(
            core_api=self._core,
            api_exception_cls=self._api_exception,
            namespace=self.config.namespace,
            service_name=worker_id,
            manifest=service_manifest(
                config=self.config,
                worker_id=worker_id,
                owner_reference=self._owner_reference_or_none(),
            ),
        )

    def _apply_deployment(
        self,
        *,
        worker_key: str,
        deployment_name: str,
        state_subpath: str,
        annotations: dict[str, str],
        replicas: int,
    ) -> None:
        apply_deployment(
            apps_api=self._apps,
            api_exception_cls=self._api_exception,
            namespace=self.config.namespace,
            deployment_name=deployment_name,
            manifest=deployment_manifest(
                config=self.config,
                auth_token=self.auth_token,
                worker_key=worker_key,
                worker_id=deployment_name,
                state_subpath=state_subpath,
                annotations=annotations,
                replicas=replicas,
                owner_reference=self._owner_reference_or_none(),
                node_name=self._worker_node_name_or_none(),
            ),
        )

    def _patch_deployment(
        self,
        deployment_name: str,
        *,
        replicas: int | None = None,
        annotations: dict[str, str] | None = None,
    ) -> None:
        patch_deployment(
            apps_api=self._apps,
            namespace=self.config.namespace,
            deployment_name=deployment_name,
            replicas=replicas,
            annotations=annotations,
        )

    def _patch_deployment_metadata(self, deployment_name: str, *, annotations: dict[str, str]) -> None:
        self._patch_deployment(deployment_name, annotations=annotations)

    def _read_deployment(self, deployment_name: str) -> KubernetesDeployment | None:
        return read_deployment(
            apps_api=self._apps,
            api_exception_cls=self._api_exception,
            namespace=self.config.namespace,
            deployment_name=deployment_name,
        )

    def _list_deployments(self) -> list[KubernetesDeployment]:
        return list_deployments(
            apps_api=self._apps,
            namespace=self.config.namespace,
            label_selector=self._list_selector(),
        )

    def _worker_node_name_or_none(self) -> str | None:
        if self.config.node_name is not None:
            return self.config.node_name
        if not self.config.colocate_with_control_plane_node:
            return None
        if self._control_plane_node_name_loaded:
            return self._control_plane_node_name

        pod_name = os.getenv(_HOSTNAME_ENV, "").strip()
        if not pod_name:
            self._control_plane_node_name_loaded = True
            return None

        self._control_plane_node_name = read_pod_node_name(
            core_api=self._core,
            api_exception_cls=self._api_exception,
            namespace=self.config.namespace,
            pod_name=pod_name,
        )
        self._control_plane_node_name_loaded = True
        return self._control_plane_node_name

    def _owner_reference_or_none(self) -> dict[str, object] | None:
        if self._owner_reference_loaded:
            return self._owner_reference
        if self.config.owner_deployment_name is None:
            self._owner_reference_loaded = True
            return None

        owner_deployment = self._read_deployment(self.config.owner_deployment_name)
        if owner_deployment is None:
            msg = f"Configured Kubernetes worker owner deployment '{self.config.owner_deployment_name}' was not found."
            raise WorkerBackendError(msg)

        owner_uid = owner_deployment.metadata.uid
        if owner_uid is None or not owner_uid.strip():
            msg = (
                f"Configured Kubernetes worker owner deployment '{self.config.owner_deployment_name}' is missing a UID."
            )
            raise WorkerBackendError(msg)

        self._owner_reference = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "name": self.config.owner_deployment_name,
            "uid": owner_uid,
            "controller": False,
            "blockOwnerDeletion": False,
        }
        self._owner_reference_loaded = True
        return self._owner_reference

    def _delete_deployment(self, deployment_name: str) -> None:
        delete_deployment(
            apps_api=self._apps,
            api_exception_cls=self._api_exception,
            namespace=self.config.namespace,
            deployment_name=deployment_name,
        )

    def _delete_service(self, service_name: str) -> None:
        delete_service(
            core_api=self._core,
            api_exception_cls=self._api_exception,
            namespace=self.config.namespace,
            service_name=service_name,
        )

    def _wait_for_ready(self, deployment_name: str, *, timeout_seconds: float) -> KubernetesDeployment:
        return wait_for_ready(
            deployment_name=deployment_name,
            timeout_seconds=timeout_seconds,
            read_deployment_fn=self._read_deployment,
            deployment_ready_fn=self._deployment_ready,
        )

    def _deployment_ready(self, deployment: KubernetesDeployment) -> bool:
        desired = int(deployment.spec.replicas or 0)
        if desired == 0:
            return True
        ready = int(deployment.status.ready_replicas or 0)
        observed_generation = deployment.status.observed_generation
        generation = deployment.metadata.generation
        generation_ready = observed_generation is None or generation is None or observed_generation >= generation
        return generation_ready and ready >= desired

    def _handle_from_deployment(self, deployment: KubernetesDeployment, *, now: float) -> WorkerHandle:
        metadata = deployment.metadata
        annotations = dict(metadata.annotations or {})
        worker_key = annotations.get(ANNOTATION_WORKER_KEY)
        if not worker_key:
            msg = f"Deployment '{metadata.name}' is missing worker metadata."
            raise WorkerBackendError(msg)

        worker_id = str(metadata.name)
        last_used_at = parse_annotation_float(annotations, ANNOTATION_LAST_USED_AT, now)
        created_at = parse_annotation_float(annotations, ANNOTATION_CREATED_AT, last_used_at)
        last_started_at = annotations.get(ANNOTATION_LAST_STARTED_AT)
        status = self._effective_status(deployment, now=now)
        endpoint_root = service_host(worker_id, self.config.namespace, self.config.worker_port)
        debug_metadata = {
            "namespace": self.config.namespace,
            "deployment_name": worker_id,
            "service_name": worker_id,
            "state_subpath": annotations.get(ANNOTATION_STATE_SUBPATH, ""),
            "api_root": f"{endpoint_root}/api/sandbox-runner",
        }
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
            startup_count=parse_annotation_int(annotations, ANNOTATION_STARTUP_COUNT),
            failure_count=parse_annotation_int(annotations, ANNOTATION_FAILURE_COUNT),
            failure_reason=annotations.get(ANNOTATION_FAILURE_REASON),
            debug_metadata=debug_metadata,
        )

    def _effective_status(self, deployment: KubernetesDeployment, *, now: float) -> WorkerStatus:
        annotations = dict(deployment.metadata.annotations or {})
        stored_status = annotations.get(ANNOTATION_WORKER_STATUS, "starting")
        if stored_status == "failed":
            return "failed"
        replicas = int(deployment.spec.replicas or 0)
        if replicas == 0:
            return "idle"
        if not self._deployment_ready(deployment):
            return "starting"
        last_used_at = parse_annotation_float(annotations, ANNOTATION_LAST_USED_AT, now)
        if now - last_used_at >= self.idle_timeout_seconds:
            return "idle"
        return "ready"
