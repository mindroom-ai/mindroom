"""Kubernetes-backed worker backend for the primary MindRoom runtime."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Protocol, cast

from mindroom.credentials import sync_env_credentials_to_worker
from mindroom.tool_system.worker_routing import worker_dir_name
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.models import WorkerHandle, WorkerSpec, WorkerStatus

_DEFAULT_IDLE_TIMEOUT_SECONDS = 1800.0
_DEFAULT_READY_TIMEOUT_SECONDS = 60.0
_DEFAULT_WORKER_PORT = 8766
_DEFAULT_IMAGE_PULL_POLICY = "IfNotPresent"
_DEFAULT_STORAGE_SUBPATH_PREFIX = "workers"
_DEFAULT_CONFIG_KEY = "config.yaml"
_DEFAULT_CONFIG_PATH = "/app/config.yaml"
_DEFAULT_STORAGE_MOUNT_PATH = "/app/worker"
_DEFAULT_SERVICE_ACCOUNT_NAME = "default"
_DEFAULT_NAME_PREFIX = "mindroom-worker"
_DEFAULT_RESOURCE_REQUESTS = {"memory": "256Mi", "cpu": "100m"}
_DEFAULT_RESOURCE_LIMITS = {"memory": "1Gi", "cpu": "500m"}
_READY_POLL_INTERVAL_SECONDS = 1.0

_WORKER_BACKEND_ENV = "MINDROOM_WORKER_BACKEND"
_NAMESPACE_ENV = "MINDROOM_KUBERNETES_WORKER_NAMESPACE"
_IMAGE_ENV = "MINDROOM_KUBERNETES_WORKER_IMAGE"
_IMAGE_PULL_POLICY_ENV = "MINDROOM_KUBERNETES_WORKER_IMAGE_PULL_POLICY"
_PORT_ENV = "MINDROOM_KUBERNETES_WORKER_PORT"
_SERVICE_ACCOUNT_ENV = "MINDROOM_KUBERNETES_WORKER_SERVICE_ACCOUNT_NAME"
_STORAGE_PVC_ENV = "MINDROOM_KUBERNETES_WORKER_STORAGE_PVC_NAME"
_STORAGE_MOUNT_PATH_ENV = "MINDROOM_KUBERNETES_WORKER_STORAGE_MOUNT_PATH"
_STORAGE_SUBPATH_PREFIX_ENV = "MINDROOM_KUBERNETES_WORKER_STORAGE_SUBPATH_PREFIX"
_CONFIG_MAP_NAME_ENV = "MINDROOM_KUBERNETES_WORKER_CONFIG_MAP_NAME"
_CONFIG_KEY_ENV = "MINDROOM_KUBERNETES_WORKER_CONFIG_KEY"
_CONFIG_PATH_ENV = "MINDROOM_KUBERNETES_WORKER_CONFIG_PATH"
_TOKEN_SECRET_NAME_ENV = "MINDROOM_KUBERNETES_WORKER_TOKEN_SECRET_NAME"  # noqa: S105
_TOKEN_SECRET_KEY_ENV = "MINDROOM_KUBERNETES_WORKER_TOKEN_SECRET_KEY"  # noqa: S105
_IDLE_TIMEOUT_ENV = "MINDROOM_KUBERNETES_WORKER_IDLE_TIMEOUT_SECONDS"
_READY_TIMEOUT_ENV = "MINDROOM_KUBERNETES_WORKER_READY_TIMEOUT_SECONDS"
_NAME_PREFIX_ENV = "MINDROOM_KUBERNETES_WORKER_NAME_PREFIX"
_NODE_NAME_ENV = "MINDROOM_KUBERNETES_WORKER_NODE_NAME"
_COLOCATE_WITH_CONTROL_PLANE_NODE_ENV = "MINDROOM_KUBERNETES_WORKER_COLOCATE_WITH_CONTROL_PLANE_NODE"
_EXTRA_ENV_JSON_ENV = "MINDROOM_KUBERNETES_WORKER_ENV_JSON"
_EXTRA_LABELS_JSON_ENV = "MINDROOM_KUBERNETES_WORKER_LABELS_JSON"
_POD_NAMESPACE_ENV = "POD_NAMESPACE"
_HOSTNAME_ENV = "HOSTNAME"

_ANNOTATION_CREATED_AT = "mindroom.ai/created-at"
_ANNOTATION_LAST_USED_AT = "mindroom.ai/last-used-at"
_ANNOTATION_LAST_STARTED_AT = "mindroom.ai/last-started-at"
_ANNOTATION_STARTUP_COUNT = "mindroom.ai/startup-count"
_ANNOTATION_FAILURE_COUNT = "mindroom.ai/failure-count"
_ANNOTATION_FAILURE_REASON = "mindroom.ai/failure-reason"
_ANNOTATION_WORKER_KEY = "mindroom.ai/worker-key"
_ANNOTATION_WORKER_STATUS = "mindroom.ai/worker-status"
_ANNOTATION_STATE_SUBPATH = "mindroom.ai/state-subpath"

_LABEL_COMPONENT = "mindroom.ai/component"
_LABEL_COMPONENT_VALUE = "worker"
_LABEL_MANAGED_BY = "app.kubernetes.io/managed-by"
_LABEL_MANAGED_BY_VALUE = "mindroom"
_LABEL_NAME = "app.kubernetes.io/name"
_LABEL_NAME_VALUE = "mindroom-worker"
_LABEL_WORKER_ID = "mindroom.ai/worker-id"

_CONTAINER_NAME = "sandbox-runner"
_TOKEN_ENV_NAME = "MINDROOM_SANDBOX_PROXY_TOKEN"  # noqa: S105
_RUNNER_PORT_ENV_NAME = "MINDROOM_SANDBOX_RUNNER_PORT"
_DEDICATED_WORKER_KEY_ENV = "MINDROOM_SANDBOX_DEDICATED_WORKER_KEY"
_DEDICATED_WORKER_ROOT_ENV = "MINDROOM_SANDBOX_DEDICATED_WORKER_ROOT"


class _ApiStatusError(Exception):
    status: int


class _KubernetesMetadata(Protocol):
    name: str
    annotations: dict[str, str] | None
    labels: dict[str, str]
    generation: int | None


class _KubernetesDeploymentSpec(Protocol):
    replicas: int | None


class _KubernetesDeploymentStatus(Protocol):
    ready_replicas: int | None
    observed_generation: int | None


class _KubernetesDeployment(Protocol):
    metadata: _KubernetesMetadata
    spec: _KubernetesDeploymentSpec
    status: _KubernetesDeploymentStatus


class _KubernetesPodSpec(Protocol):
    node_name: str | None


class _KubernetesPod(Protocol):
    spec: _KubernetesPodSpec


class _KubernetesDeploymentList(Protocol):
    items: list[_KubernetesDeployment] | None


class _AppsApiProtocol(Protocol):
    def read_namespaced_deployment(self, name: str, namespace: str) -> _KubernetesDeployment: ...

    def create_namespaced_deployment(self, namespace: str, body: dict[str, object]) -> _KubernetesDeployment: ...

    def patch_namespaced_deployment(
        self,
        name: str,
        namespace: str,
        body: dict[str, object],
    ) -> _KubernetesDeployment: ...

    def delete_namespaced_deployment(self, name: str, namespace: str) -> None: ...

    def list_namespaced_deployment(self, namespace: str, label_selector: str) -> _KubernetesDeploymentList: ...


class _CoreApiProtocol(Protocol):
    def read_namespaced_service(self, name: str, namespace: str) -> object: ...

    def create_namespaced_service(self, namespace: str, body: dict[str, object]) -> object: ...

    def patch_namespaced_service(self, name: str, namespace: str, body: dict[str, object]) -> object: ...

    def delete_namespaced_service(self, name: str, namespace: str) -> None: ...

    def read_namespaced_pod(self, name: str, namespace: str) -> _KubernetesPod: ...


def _read_float_env(name: str, default: float) -> float:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError:
        value = default
    return max(1.0, value)


def _read_int_env(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError:
        value = default
    return max(1, value)


def _read_bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _read_json_mapping_env(name: str) -> dict[str, str]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    cleaned: dict[str, str] = {}
    for key, value in parsed.items():
        if not isinstance(key, str):
            continue
        if isinstance(value, str):
            cleaned[key] = value
        elif value is not None:
            cleaned[key] = str(value)
    return cleaned


def _worker_id_for_key(worker_key: str, *, prefix: str) -> str:
    digest = hashlib.sha256(worker_key.encode("utf-8")).hexdigest()[:24]
    normalized_prefix = prefix.strip().lower().strip("-") or _DEFAULT_NAME_PREFIX
    max_prefix_length = 63 - len(digest) - 1
    safe_prefix = normalized_prefix[:max_prefix_length].rstrip("-")
    if not safe_prefix:
        safe_prefix = _DEFAULT_NAME_PREFIX[:max_prefix_length].rstrip("-") or "worker"
    return f"{safe_prefix}-{digest}"


def _service_host(service_name: str, namespace: str, port: int) -> str:
    return f"http://{service_name}.{namespace}.svc.cluster.local:{port}"


def _parse_annotation_float(annotations: dict[str, str], key: str, default: float) -> float:
    raw = annotations.get(key)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _parse_annotation_int(annotations: dict[str, str], key: str, default: int = 0) -> int:
    raw = annotations.get(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True, slots=True)
class KubernetesWorkerBackendConfig:
    """Resolved environment-backed configuration for the Kubernetes provider."""

    namespace: str
    image: str
    image_pull_policy: str
    worker_port: int
    service_account_name: str
    storage_pvc_name: str
    storage_mount_path: str
    storage_subpath_prefix: str
    config_map_name: str | None
    config_key: str
    config_path: str
    token_secret_name: str | None
    token_secret_key: str
    idle_timeout_seconds: float
    ready_timeout_seconds: float
    name_prefix: str
    node_name: str | None
    colocate_with_control_plane_node: bool
    extra_env: dict[str, str]
    extra_labels: dict[str, str]

    @classmethod
    def from_env(cls) -> KubernetesWorkerBackendConfig:
        """Build Kubernetes worker configuration from the current environment."""
        namespace = os.getenv(_NAMESPACE_ENV, "").strip() or os.getenv(_POD_NAMESPACE_ENV, "").strip() or "default"
        image = os.getenv(_IMAGE_ENV, "").strip()
        if not image:
            msg = f"{_IMAGE_ENV} must be set when {_WORKER_BACKEND_ENV}=kubernetes."
            raise WorkerBackendError(msg)

        storage_pvc_name = os.getenv(_STORAGE_PVC_ENV, "").strip()
        if not storage_pvc_name:
            msg = f"{_STORAGE_PVC_ENV} must be set when {_WORKER_BACKEND_ENV}=kubernetes."
            raise WorkerBackendError(msg)
        config_map_name = os.getenv(_CONFIG_MAP_NAME_ENV, "").strip() or None
        token_secret_name = os.getenv(_TOKEN_SECRET_NAME_ENV, "").strip() or None
        return cls(
            namespace=namespace,
            image=image,
            image_pull_policy=os.getenv(_IMAGE_PULL_POLICY_ENV, _DEFAULT_IMAGE_PULL_POLICY).strip()
            or _DEFAULT_IMAGE_PULL_POLICY,
            worker_port=_read_int_env(_PORT_ENV, _DEFAULT_WORKER_PORT),
            service_account_name=os.getenv(_SERVICE_ACCOUNT_ENV, _DEFAULT_SERVICE_ACCOUNT_NAME).strip()
            or _DEFAULT_SERVICE_ACCOUNT_NAME,
            storage_pvc_name=storage_pvc_name,
            storage_mount_path=os.getenv(_STORAGE_MOUNT_PATH_ENV, _DEFAULT_STORAGE_MOUNT_PATH).strip()
            or _DEFAULT_STORAGE_MOUNT_PATH,
            storage_subpath_prefix=os.getenv(_STORAGE_SUBPATH_PREFIX_ENV, _DEFAULT_STORAGE_SUBPATH_PREFIX).strip()
            or _DEFAULT_STORAGE_SUBPATH_PREFIX,
            config_map_name=config_map_name,
            config_key=os.getenv(_CONFIG_KEY_ENV, _DEFAULT_CONFIG_KEY).strip() or _DEFAULT_CONFIG_KEY,
            config_path=os.getenv(_CONFIG_PATH_ENV, _DEFAULT_CONFIG_PATH).strip() or _DEFAULT_CONFIG_PATH,
            token_secret_name=token_secret_name,
            token_secret_key=os.getenv(_TOKEN_SECRET_KEY_ENV, "sandbox_proxy_token").strip() or "sandbox_proxy_token",
            idle_timeout_seconds=_read_float_env(_IDLE_TIMEOUT_ENV, _DEFAULT_IDLE_TIMEOUT_SECONDS),
            ready_timeout_seconds=_read_float_env(_READY_TIMEOUT_ENV, _DEFAULT_READY_TIMEOUT_SECONDS),
            name_prefix=os.getenv(_NAME_PREFIX_ENV, _DEFAULT_NAME_PREFIX).strip() or _DEFAULT_NAME_PREFIX,
            node_name=os.getenv(_NODE_NAME_ENV, "").strip() or None,
            colocate_with_control_plane_node=_read_bool_env(_COLOCATE_WITH_CONTROL_PLANE_NODE_ENV, default=False),
            extra_env=_read_json_mapping_env(_EXTRA_ENV_JSON_ENV),
            extra_labels=_read_json_mapping_env(_EXTRA_LABELS_JSON_ENV),
        )


def kubernetes_backend_config_signature(*, auth_token: str | None) -> tuple[str, ...]:
    """Return a cache signature for one concrete Kubernetes backend config."""
    config = KubernetesWorkerBackendConfig.from_env()
    extra_env_json = json.dumps(config.extra_env, sort_keys=True, separators=(",", ":"))
    extra_labels_json = json.dumps(config.extra_labels, sort_keys=True, separators=(",", ":"))
    return (
        "kubernetes",
        config.namespace,
        config.image,
        config.image_pull_policy,
        str(config.worker_port),
        config.service_account_name,
        config.storage_pvc_name,
        config.storage_mount_path,
        config.storage_subpath_prefix,
        config.config_map_name or "",
        config.config_key,
        config.config_path,
        config.token_secret_name or "",
        config.token_secret_key,
        str(config.idle_timeout_seconds),
        str(config.ready_timeout_seconds),
        config.name_prefix,
        config.node_name or "",
        str(config.colocate_with_control_plane_node),
        extra_env_json,
        extra_labels_json,
        auth_token or "",
    )


class KubernetesWorkerBackend:
    """Kubernetes-backed worker provider for dedicated worker pods."""

    backend_name = "kubernetes"

    def __init__(self, *, config: KubernetesWorkerBackendConfig, auth_token: str | None) -> None:
        self.config = config
        self.auth_token = auth_token
        self.idle_timeout_seconds = config.idle_timeout_seconds
        self._apps_api: _AppsApiProtocol | None = None
        self._core_api: _CoreApiProtocol | None = None
        self._api_exception_cls: type[_ApiStatusError] | None = None
        self._worker_locks: dict[str, threading.Lock] = {}
        self._worker_locks_lock = threading.Lock()
        self._control_plane_node_name: str | None = None
        self._control_plane_node_name_loaded = False

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
            annotations = self._metadata_annotations(
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

            sync_env_credentials_to_worker(worker_key)
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
            final_annotations[_ANNOTATION_WORKER_STATUS] = "ready"
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
        annotations[_ANNOTATION_LAST_USED_AT] = str(timestamp)
        if annotations.get(_ANNOTATION_WORKER_STATUS) == "idle":
            annotations[_ANNOTATION_WORKER_STATUS] = "ready"
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
        annotations[_ANNOTATION_LAST_USED_AT] = str(timestamp)
        annotations[_ANNOTATION_WORKER_STATUS] = "idle"
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
            annotations[_ANNOTATION_WORKER_STATUS] = "idle"
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
        annotations[_ANNOTATION_LAST_USED_AT] = str(timestamp)
        annotations[_ANNOTATION_WORKER_STATUS] = "failed"
        annotations[_ANNOTATION_FAILURE_REASON] = failure_reason
        annotations[_ANNOTATION_FAILURE_COUNT] = str(_parse_annotation_int(annotations, _ANNOTATION_FAILURE_COUNT) + 1)
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
        return _worker_id_for_key(worker_key, prefix=self.config.name_prefix)

    def _state_subpath(self, worker_key: str) -> str:
        prefix = self.config.storage_subpath_prefix.strip().strip("/")
        worker_dir = worker_dir_name(worker_key)
        return f"{prefix}/{worker_dir}" if prefix else worker_dir

    def _load_clients(self) -> None:
        if self._apps_api is not None and self._core_api is not None and self._api_exception_cls is not None:
            return

        try:
            kubernetes_config = importlib.import_module("kubernetes.config")
            kubernetes_client = importlib.import_module("kubernetes.client")
            kubernetes_exceptions = importlib.import_module("kubernetes.client.exceptions")
        except ModuleNotFoundError as exc:
            msg = "The 'kubernetes' package is required for the Kubernetes worker backend."
            raise WorkerBackendError(msg) from exc

        try:
            kubernetes_config.load_incluster_config()
        except Exception:
            kubernetes_config.load_kube_config()

        self._apps_api = cast("_AppsApiProtocol", kubernetes_client.AppsV1Api())
        self._core_api = cast("_CoreApiProtocol", kubernetes_client.CoreV1Api())
        self._api_exception_cls = cast("type[_ApiStatusError]", kubernetes_exceptions.ApiException)

    @property
    def _apps(self) -> _AppsApiProtocol:
        self._load_clients()
        assert self._apps_api is not None
        return self._apps_api

    @property
    def _core(self) -> _CoreApiProtocol:
        self._load_clients()
        assert self._core_api is not None
        return self._core_api

    @property
    def _api_exception(self) -> type[_ApiStatusError]:
        self._load_clients()
        assert self._api_exception_cls is not None
        return self._api_exception_cls

    def _labels(self, worker_id: str) -> dict[str, str]:
        labels = {
            _LABEL_COMPONENT: _LABEL_COMPONENT_VALUE,
            _LABEL_MANAGED_BY: _LABEL_MANAGED_BY_VALUE,
            _LABEL_NAME: _LABEL_NAME_VALUE,
        }
        labels.update(self.config.extra_labels)
        labels[_LABEL_WORKER_ID] = worker_id
        return labels

    def _list_selector(self) -> str:
        selector_labels = {
            _LABEL_COMPONENT: _LABEL_COMPONENT_VALUE,
            _LABEL_MANAGED_BY: _LABEL_MANAGED_BY_VALUE,
            _LABEL_NAME: _LABEL_NAME_VALUE,
        }
        selector_labels.update(self.config.extra_labels)
        return ",".join(f"{key}={value}" for key, value in sorted(selector_labels.items()))

    def _metadata_annotations(
        self,
        *,
        worker_key: str,
        state_subpath: str,
        created_at: float,
        last_used_at: float,
        last_started_at: float | None,
        startup_count: int,
        failure_count: int,
        failure_reason: str | None,
        status: WorkerStatus,
    ) -> dict[str, str]:
        annotations = {
            _ANNOTATION_WORKER_KEY: worker_key,
            _ANNOTATION_STATE_SUBPATH: state_subpath,
            _ANNOTATION_CREATED_AT: str(created_at),
            _ANNOTATION_LAST_USED_AT: str(last_used_at),
            _ANNOTATION_STARTUP_COUNT: str(startup_count),
            _ANNOTATION_FAILURE_COUNT: str(failure_count),
            _ANNOTATION_WORKER_STATUS: status,
        }
        if last_started_at is not None:
            annotations[_ANNOTATION_LAST_STARTED_AT] = str(last_started_at)
        if failure_reason:
            annotations[_ANNOTATION_FAILURE_REASON] = failure_reason
        return annotations

    def _worker_env(self, *, worker_key: str) -> list[dict[str, object]]:
        env: list[dict[str, object]] = [
            {"name": "MINDROOM_SANDBOX_RUNNER_MODE", "value": "true"},
            {"name": "MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE", "value": "subprocess"},
            {"name": _RUNNER_PORT_ENV_NAME, "value": str(self.config.worker_port)},
            {"name": "MINDROOM_STORAGE_PATH", "value": self.config.storage_mount_path},
            {"name": _DEDICATED_WORKER_KEY_ENV, "value": worker_key},
            {"name": _DEDICATED_WORKER_ROOT_ENV, "value": self.config.storage_mount_path},
            {"name": "HOME", "value": self.config.storage_mount_path},
        ]
        if self.config.config_map_name is not None:
            env.append({"name": "MINDROOM_CONFIG_PATH", "value": self.config.config_path})
        if self.config.token_secret_name is not None:
            env.append(
                {
                    "name": _TOKEN_ENV_NAME,
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": self.config.token_secret_name,
                            "key": self.config.token_secret_key,
                        },
                    },
                },
            )
        elif self.auth_token is not None:
            env.append({"name": _TOKEN_ENV_NAME, "value": self.auth_token})
        else:
            msg = "A worker auth token is required for Kubernetes workers."
            raise WorkerBackendError(msg)

        for name, value in sorted(self.config.extra_env.items()):
            env.append({"name": name, "value": value})
        return env

    def _volume_mounts(self, *, state_subpath: str) -> list[dict[str, object]]:
        mounts: list[dict[str, object]] = [
            {
                "name": "worker-storage",
                "mountPath": self.config.storage_mount_path,
                "subPath": state_subpath,
            },
        ]
        if self.config.config_map_name is not None:
            mounts.append(
                {
                    "name": "worker-config",
                    "mountPath": self.config.config_path,
                    "subPath": self.config.config_key,
                    "readOnly": True,
                },
            )
        return mounts

    def _volumes(self) -> list[dict[str, object]]:
        volumes: list[dict[str, object]] = [
            {
                "name": "worker-storage",
                "persistentVolumeClaim": {
                    "claimName": self.config.storage_pvc_name,
                },
            },
        ]
        if self.config.config_map_name is not None:
            volumes.append(
                {
                    "name": "worker-config",
                    "configMap": {
                        "name": self.config.config_map_name,
                    },
                },
            )
        return volumes

    def _service_manifest(self, *, worker_id: str) -> dict[str, object]:
        labels = self._labels(worker_id)
        return {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": worker_id,
                "namespace": self.config.namespace,
                "labels": labels,
            },
            "spec": {
                "selector": labels,
                "ports": [
                    {
                        "name": "api",
                        "port": self.config.worker_port,
                        "targetPort": self.config.worker_port,
                    },
                ],
            },
        }

    def _deployment_manifest(
        self,
        *,
        worker_key: str,
        worker_id: str,
        state_subpath: str,
        annotations: dict[str, str],
        replicas: int,
    ) -> dict[str, object]:
        labels = self._labels(worker_id)
        pod_spec: dict[str, object] = {
            "serviceAccountName": self.config.service_account_name,
            "securityContext": {
                "runAsUser": 1000,
                "runAsGroup": 1000,
                "fsGroup": 1000,
                "runAsNonRoot": True,
                "fsGroupChangePolicy": "OnRootMismatch",
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [
                {
                    "name": _CONTAINER_NAME,
                    "image": self.config.image,
                    "imagePullPolicy": self.config.image_pull_policy,
                    "command": ["/app/run-sandbox-runner.sh"],
                    "ports": [{"containerPort": self.config.worker_port, "name": "api"}],
                    "env": self._worker_env(worker_key=worker_key),
                    "volumeMounts": self._volume_mounts(state_subpath=state_subpath),
                    "readinessProbe": {
                        "httpGet": {"path": "/healthz", "port": "api"},
                        "periodSeconds": 5,
                        "failureThreshold": 6,
                    },
                    "livenessProbe": {
                        "httpGet": {"path": "/healthz", "port": "api"},
                        "periodSeconds": 10,
                        "failureThreshold": 6,
                    },
                    "resources": {
                        "requests": dict(_DEFAULT_RESOURCE_REQUESTS),
                        "limits": dict(_DEFAULT_RESOURCE_LIMITS),
                    },
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "capabilities": {
                            "drop": ["ALL"],
                        },
                    },
                },
            ],
            "volumes": self._volumes(),
        }
        node_name = self._worker_node_name_or_none()
        if node_name is not None:
            pod_spec["nodeName"] = node_name
        return {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": worker_id,
                "namespace": self.config.namespace,
                "labels": labels,
                "annotations": annotations,
            },
            "spec": {
                "replicas": replicas,
                "selector": {"matchLabels": labels},
                "template": {
                    "metadata": {
                        "labels": labels,
                    },
                    "spec": pod_spec,
                },
            },
        }

    def _apply_service(self, worker_id: str) -> None:
        manifest = self._service_manifest(worker_id=worker_id)
        try:
            self._core.read_namespaced_service(worker_id, self.config.namespace)
        except self._api_exception as exc:
            if exc.status != 404:
                raise
            try:
                self._core.create_namespaced_service(self.config.namespace, manifest)
            except self._api_exception as create_exc:
                if create_exc.status != 409:
                    raise
                self._core.patch_namespaced_service(worker_id, self.config.namespace, manifest)
            return
        self._core.patch_namespaced_service(worker_id, self.config.namespace, manifest)

    def _apply_deployment(
        self,
        *,
        worker_key: str,
        deployment_name: str,
        state_subpath: str,
        annotations: dict[str, str],
        replicas: int,
    ) -> None:
        manifest = self._deployment_manifest(
            worker_key=worker_key,
            worker_id=deployment_name,
            state_subpath=state_subpath,
            annotations=annotations,
            replicas=replicas,
        )
        try:
            self._apps.read_namespaced_deployment(deployment_name, self.config.namespace)
        except self._api_exception as exc:
            if exc.status != 404:
                raise
            try:
                self._apps.create_namespaced_deployment(self.config.namespace, manifest)
            except self._api_exception as create_exc:
                if create_exc.status != 409:
                    raise
                self._apps.patch_namespaced_deployment(deployment_name, self.config.namespace, manifest)
            return
        self._apps.patch_namespaced_deployment(deployment_name, self.config.namespace, manifest)

    def _patch_deployment(
        self,
        deployment_name: str,
        *,
        replicas: int | None = None,
        annotations: dict[str, str] | None = None,
    ) -> None:
        body: dict[str, object] = {}
        if annotations is not None:
            body["metadata"] = {"annotations": annotations}
        if replicas is not None:
            body["spec"] = {"replicas": replicas}
        self._apps.patch_namespaced_deployment(deployment_name, self.config.namespace, body)

    def _patch_deployment_metadata(self, deployment_name: str, *, annotations: dict[str, str]) -> None:
        self._patch_deployment(deployment_name, annotations=annotations)

    def _read_deployment(self, deployment_name: str) -> _KubernetesDeployment | None:
        try:
            return self._apps.read_namespaced_deployment(deployment_name, self.config.namespace)
        except self._api_exception as exc:
            if exc.status == 404:
                return None
            raise

    def _list_deployments(self) -> list[_KubernetesDeployment]:
        selector = self._list_selector()
        response = self._apps.list_namespaced_deployment(self.config.namespace, label_selector=selector)
        return list(response.items or [])

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

        try:
            pod = self._core.read_namespaced_pod(pod_name, self.config.namespace)
        except self._api_exception as exc:
            if exc.status == 404:
                self._control_plane_node_name_loaded = True
                return None
            raise

        self._control_plane_node_name = pod.spec.node_name
        self._control_plane_node_name_loaded = True
        return self._control_plane_node_name

    def _delete_deployment(self, deployment_name: str) -> None:
        try:
            self._apps.delete_namespaced_deployment(deployment_name, self.config.namespace)
        except self._api_exception as exc:
            if exc.status != 404:
                raise

    def _delete_service(self, service_name: str) -> None:
        try:
            self._core.delete_namespaced_service(service_name, self.config.namespace)
        except self._api_exception as exc:
            if exc.status != 404:
                raise

    def _wait_for_ready(self, deployment_name: str, *, timeout_seconds: float) -> _KubernetesDeployment:
        deadline = time.time() + timeout_seconds
        while True:
            deployment = self._read_deployment(deployment_name)
            if deployment is None:
                msg = f"Kubernetes worker deployment '{deployment_name}' disappeared during startup."
                raise WorkerBackendError(msg)
            if self._deployment_ready(deployment):
                return deployment
            if time.time() >= deadline:
                msg = f"Kubernetes worker '{deployment_name}' did not become ready within {timeout_seconds:.0f}s."
                raise WorkerBackendError(msg)
            time.sleep(_READY_POLL_INTERVAL_SECONDS)

    def _deployment_ready(self, deployment: _KubernetesDeployment) -> bool:
        desired = int(deployment.spec.replicas or 0)
        if desired == 0:
            return True
        ready = int(deployment.status.ready_replicas or 0)
        observed_generation = deployment.status.observed_generation
        generation = deployment.metadata.generation
        generation_ready = observed_generation is None or generation is None or observed_generation >= generation
        return generation_ready and ready >= desired

    def _handle_from_deployment(self, deployment: _KubernetesDeployment, *, now: float) -> WorkerHandle:
        metadata = deployment.metadata
        annotations = dict(metadata.annotations or {})
        worker_key = annotations.get(_ANNOTATION_WORKER_KEY)
        if not worker_key:
            msg = f"Deployment '{metadata.name}' is missing worker metadata."
            raise WorkerBackendError(msg)

        worker_id = str(metadata.name)
        last_used_at = _parse_annotation_float(annotations, _ANNOTATION_LAST_USED_AT, now)
        created_at = _parse_annotation_float(annotations, _ANNOTATION_CREATED_AT, last_used_at)
        last_started_at = annotations.get(_ANNOTATION_LAST_STARTED_AT)
        status = self._effective_status(deployment, now=now)
        endpoint_root = _service_host(worker_id, self.config.namespace, self.config.worker_port)
        debug_metadata = {
            "namespace": self.config.namespace,
            "deployment_name": worker_id,
            "service_name": worker_id,
            "state_subpath": annotations.get(_ANNOTATION_STATE_SUBPATH, ""),
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
            startup_count=_parse_annotation_int(annotations, _ANNOTATION_STARTUP_COUNT),
            failure_count=_parse_annotation_int(annotations, _ANNOTATION_FAILURE_COUNT),
            failure_reason=annotations.get(_ANNOTATION_FAILURE_REASON),
            debug_metadata=debug_metadata,
        )

    def _effective_status(self, deployment: _KubernetesDeployment, *, now: float) -> WorkerStatus:
        annotations = dict(deployment.metadata.annotations or {})
        stored_status = annotations.get(_ANNOTATION_WORKER_STATUS, "starting")
        if stored_status == "failed":
            return "failed"
        replicas = int(deployment.spec.replicas or 0)
        if replicas == 0:
            return "idle"
        if not self._deployment_ready(deployment):
            return "starting"
        last_used_at = _parse_annotation_float(annotations, _ANNOTATION_LAST_USED_AT, now)
        if now - last_used_at >= self.idle_timeout_seconds:
            return "idle"
        return "ready"
