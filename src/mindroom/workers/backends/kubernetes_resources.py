"""Resource and manifest helpers for the Kubernetes worker backend."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import shutil
import time
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol, cast

from mindroom import constants
from mindroom.constants import RuntimePaths, serialize_public_runtime_paths
from mindroom.credentials import SHARED_CREDENTIALS_PATH_ENV
from mindroom.tool_system.worker_routing import resolved_worker_key_scope, visible_state_roots_for_worker_key
from mindroom.workers.backend import WorkerBackendError
from mindroom.workspaces import validate_local_copy_source_path

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.workers.models import WorkerStatus

    from .kubernetes_config import _KubernetesWorkerBackendConfig

_DEFAULT_NAME_PREFIX = "mindroom-worker"
_DEFAULT_RESOURCE_REQUESTS = {"memory": "256Mi", "cpu": "100m"}
_DEFAULT_RESOURCE_LIMITS = {"memory": "1Gi", "cpu": "500m"}
_READY_POLL_INTERVAL_SECONDS = 1.0
_DELETE_POLL_INTERVAL_SECONDS = 0.2
_HOSTNAME_ENV = "HOSTNAME"

ANNOTATION_CREATED_AT = "mindroom.ai/created-at"
ANNOTATION_LAST_USED_AT = "mindroom.ai/last-used-at"
ANNOTATION_LAST_STARTED_AT = "mindroom.ai/last-started-at"
ANNOTATION_STARTUP_COUNT = "mindroom.ai/startup-count"
ANNOTATION_FAILURE_COUNT = "mindroom.ai/failure-count"
ANNOTATION_FAILURE_REASON = "mindroom.ai/failure-reason"
ANNOTATION_WORKER_KEY = "mindroom.ai/worker-key"
ANNOTATION_WORKER_STATUS = "mindroom.ai/worker-status"
ANNOTATION_STATE_SUBPATH = "mindroom.ai/state-subpath"
ANNOTATION_TEMPLATE_HASH = "mindroom.ai/template-hash"

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
_SHARED_STORAGE_ROOT_ENV = "MINDROOM_SANDBOX_SHARED_STORAGE_ROOT"
_STARTUP_RUNTIME_PATHS_ENV = "MINDROOM_RUNTIME_PATHS_JSON"
_DEFAULT_CONTAINER_PATH = "/app/.venv/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin"


class _ApiStatusError(Exception):
    status: int


class _KubernetesMetadata(Protocol):
    name: str
    annotations: dict[str, str] | None
    labels: dict[str, str]
    generation: int | None
    uid: str | None


class _KubernetesDeploymentSpec(Protocol):
    replicas: int | None


class _KubernetesDeploymentStatus(Protocol):
    ready_replicas: int | None
    observed_generation: int | None


class KubernetesDeployment(Protocol):
    """Minimal Deployment surface used by the backend."""

    metadata: _KubernetesMetadata
    spec: _KubernetesDeploymentSpec
    status: _KubernetesDeploymentStatus


class _KubernetesPodSpec(Protocol):
    node_name: str | None


class _KubernetesPod(Protocol):
    spec: _KubernetesPodSpec


class _KubernetesDeploymentList(Protocol):
    items: list[KubernetesDeployment] | None


class _AppsApiProtocol(Protocol):
    def read_namespaced_deployment(self, name: str, namespace: str) -> KubernetesDeployment: ...

    def create_namespaced_deployment(self, namespace: str, body: dict[str, object]) -> KubernetesDeployment: ...

    def patch_namespaced_deployment(
        self,
        name: str,
        namespace: str,
        body: dict[str, object],
    ) -> KubernetesDeployment: ...

    def delete_namespaced_deployment(self, name: str, namespace: str) -> None: ...

    def list_namespaced_deployment(self, namespace: str, label_selector: str) -> _KubernetesDeploymentList: ...


class _CoreApiProtocol(Protocol):
    def read_namespaced_service(self, name: str, namespace: str) -> object: ...

    def create_namespaced_service(self, namespace: str, body: dict[str, object]) -> object: ...

    def patch_namespaced_service(self, name: str, namespace: str, body: dict[str, object]) -> object: ...

    def delete_namespaced_service(self, name: str, namespace: str) -> None: ...

    def read_namespaced_pod(self, name: str, namespace: str) -> _KubernetesPod: ...


def worker_id_for_key(worker_key: str, *, prefix: str) -> str:
    """Return a DNS-safe Kubernetes resource name for one worker key."""
    digest = hashlib.sha256(worker_key.encode("utf-8")).hexdigest()[:24]
    normalized_prefix = prefix.strip().lower().strip("-") or _DEFAULT_NAME_PREFIX
    max_prefix_length = 63 - len(digest) - 1
    safe_prefix = normalized_prefix[:max_prefix_length].rstrip("-")
    if not safe_prefix:
        safe_prefix = _DEFAULT_NAME_PREFIX[:max_prefix_length].rstrip("-") or "worker"
    return f"{safe_prefix}-{digest}"


def service_host(service_name: str, namespace: str, port: int) -> str:
    """Return the cluster-local HTTP root for one worker Service."""
    return f"http://{service_name}.{namespace}.svc.cluster.local:{port}"


def parse_annotation_float(annotations: dict[str, str], key: str, default: float) -> float:
    """Parse one float annotation, falling back to a caller-provided default."""
    raw = annotations.get(key)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def parse_annotation_int(annotations: dict[str, str], key: str, default: int = 0) -> int:
    """Parse one integer annotation, falling back to a caller-provided default."""
    raw = annotations.get(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def metadata_annotations(
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
    """Build persisted worker lifecycle metadata stored on Deployments."""
    annotations = {
        ANNOTATION_WORKER_KEY: worker_key,
        ANNOTATION_STATE_SUBPATH: state_subpath,
        ANNOTATION_CREATED_AT: str(created_at),
        ANNOTATION_LAST_USED_AT: str(last_used_at),
        ANNOTATION_STARTUP_COUNT: str(startup_count),
        ANNOTATION_FAILURE_COUNT: str(failure_count),
        ANNOTATION_WORKER_STATUS: status,
    }
    if last_started_at is not None:
        annotations[ANNOTATION_LAST_STARTED_AT] = str(last_started_at)
    if failure_reason:
        annotations[ANNOTATION_FAILURE_REASON] = failure_reason
    return annotations


def _template_hash(template: dict[str, object]) -> str:
    """Return a stable hash for one Deployment pod template."""
    payload = json.dumps(template, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _labels(*, extra_labels: dict[str, str], worker_id: str) -> dict[str, str]:
    labels = {
        _LABEL_COMPONENT: _LABEL_COMPONENT_VALUE,
        _LABEL_MANAGED_BY: _LABEL_MANAGED_BY_VALUE,
        _LABEL_NAME: _LABEL_NAME_VALUE,
    }
    labels.update(extra_labels)
    labels[_LABEL_WORKER_ID] = worker_id
    return labels


def _list_selector(*, extra_labels: dict[str, str]) -> str:
    selector = {
        _LABEL_COMPONENT: _LABEL_COMPONENT_VALUE,
        _LABEL_MANAGED_BY: _LABEL_MANAGED_BY_VALUE,
        _LABEL_NAME: _LABEL_NAME_VALUE,
    }
    selector.update(extra_labels)
    return ",".join(f"{key}={value}" for key, value in sorted(selector.items()))


class KubernetesResourceManager:
    """Own Kubernetes API access, manifest construction, and cached cluster metadata."""

    def __init__(
        self,
        *,
        runtime_paths: RuntimePaths,
        config: _KubernetesWorkerBackendConfig,
        auth_token: str | None,
        storage_root: Path,
    ) -> None:
        """Initialize one resource manager for a concrete backend configuration."""
        self.runtime_paths = runtime_paths
        self.config = config
        self.auth_token = auth_token
        self.storage_root = storage_root.expanduser().resolve()
        self.apps_api: _AppsApiProtocol | None = None
        self.core_api: _CoreApiProtocol | None = None
        self.api_exception_cls: type[_ApiStatusError] | None = None
        self._control_plane_node_name: str | None = None
        self._control_plane_node_name_loaded = False
        self._owner_reference: dict[str, object] | None = None
        self._owner_reference_loaded = False

    @property
    def _apps(self) -> _AppsApiProtocol:
        self._load_clients()
        assert self.apps_api is not None
        return self.apps_api

    @property
    def _core(self) -> _CoreApiProtocol:
        self._load_clients()
        assert self.core_api is not None
        return self.core_api

    @property
    def _api_exception(self) -> type[_ApiStatusError]:
        self._load_clients()
        assert self.api_exception_cls is not None
        return self.api_exception_cls

    def list_deployments(self) -> list[KubernetesDeployment]:
        """List managed worker Deployments in this namespace."""
        response = self._apps.list_namespaced_deployment(
            self.config.namespace,
            label_selector=_list_selector(extra_labels=self.config.extra_labels),
        )
        return list(response.items or [])

    def read_deployment(self, deployment_name: str) -> KubernetesDeployment | None:
        """Read one Deployment, returning ``None`` for 404s."""
        try:
            return self._apps.read_namespaced_deployment(deployment_name, self.config.namespace)
        except self._api_exception as exc:
            if exc.status == 404:
                return None
            raise

    def apply_service(self, worker_id: str) -> None:
        """Create-or-patch one worker Service."""
        self._apply_object(
            read_fn=self._core.read_namespaced_service,
            create_fn=self._core.create_namespaced_service,
            patch_fn=self._core.patch_namespaced_service,
            resource_name=worker_id,
            manifest=self._service_manifest(worker_id),
        )

    def apply_deployment(
        self,
        *,
        worker_key: str,
        worker_id: str,
        state_subpath: str,
        annotations: dict[str, str],
        replicas: int,
        private_agent_names: frozenset[str] | None = None,
    ) -> None:
        """Create-or-patch one worker Deployment."""
        manifest = self._deployment_manifest(
            worker_key=worker_key,
            worker_id=worker_id,
            state_subpath=state_subpath,
            annotations=annotations,
            replicas=replicas,
            private_agent_names=private_agent_names,
        )
        existing = self.read_deployment(worker_id)
        if existing is not None:
            existing_annotations = existing.metadata.annotations or {}
            desired_metadata = cast("dict[str, object]", manifest.get("metadata", {}))
            desired_annotations = cast("dict[str, str]", desired_metadata.get("annotations", {}))
            if existing_annotations.get(ANNOTATION_TEMPLATE_HASH) != desired_annotations[ANNOTATION_TEMPLATE_HASH]:
                self._recreate_deployment(worker_id, manifest, timeout_seconds=self.config.ready_timeout_seconds)
                return
        self._apply_object(
            read_fn=self._apps.read_namespaced_deployment,
            create_fn=self._apps.create_namespaced_deployment,
            patch_fn=self._apps.patch_namespaced_deployment,
            resource_name=worker_id,
            manifest=manifest,
        )

    def patch_deployment(
        self,
        deployment_name: str,
        *,
        replicas: int | None = None,
        annotations: dict[str, str] | None = None,
    ) -> None:
        """Patch Deployment metadata and/or scale."""
        body: dict[str, object] = {}
        if annotations is not None:
            existing = self.read_deployment(deployment_name)
            merged_annotations = dict(existing.metadata.annotations or {}) if existing is not None else {}
            merged_annotations.update(annotations)
            body["metadata"] = {"annotations": merged_annotations}
        if replicas is not None:
            body["spec"] = {"replicas": replicas}
        self._apps.patch_namespaced_deployment(deployment_name, self.config.namespace, body)

    def delete_deployment(self, deployment_name: str) -> None:
        """Delete one worker Deployment, ignoring 404s."""
        self._delete_object(self._apps.delete_namespaced_deployment, deployment_name)

    def delete_service(self, service_name: str) -> None:
        """Delete one worker Service, ignoring 404s."""
        self._delete_object(self._core.delete_namespaced_service, service_name)

    def _recreate_deployment(
        self,
        deployment_name: str,
        manifest: dict[str, object],
        *,
        timeout_seconds: float,
    ) -> None:
        """Replace one Deployment when pod-template drift requires a full recreate."""
        self.delete_deployment(deployment_name)
        self._wait_for_deployment_absent(deployment_name, timeout_seconds=timeout_seconds)
        deadline = time.time() + timeout_seconds
        while True:
            try:
                self._apps.create_namespaced_deployment(self.config.namespace, manifest)
            except self._api_exception as exc:
                if exc.status != 409:
                    raise
                if time.time() >= deadline:
                    msg = (
                        f"Kubernetes worker deployment '{deployment_name}' did not finish deleting "
                        f"within {timeout_seconds:.0f}s before recreate."
                    )
                    raise WorkerBackendError(msg) from exc
                time.sleep(_DELETE_POLL_INTERVAL_SECONDS)
            else:
                return

    def _wait_for_deployment_absent(self, deployment_name: str, *, timeout_seconds: float) -> None:
        """Poll until one Deployment is fully gone after delete has been requested."""
        deadline = time.time() + timeout_seconds
        while True:
            if self.read_deployment(deployment_name) is None:
                return
            if time.time() >= deadline:
                msg = (
                    f"Kubernetes worker deployment '{deployment_name}' did not finish deleting "
                    f"within {timeout_seconds:.0f}s."
                )
                raise WorkerBackendError(msg)
            time.sleep(_DELETE_POLL_INTERVAL_SECONDS)

    def wait_for_ready(
        self,
        deployment_name: str,
        *,
        timeout_seconds: float,
        deployment_ready_fn: Callable[[KubernetesDeployment], bool],
    ) -> KubernetesDeployment:
        """Poll a worker Deployment until it becomes ready or times out."""
        deadline = time.time() + timeout_seconds
        while True:
            deployment = self.read_deployment(deployment_name)
            if deployment is None:
                msg = f"Kubernetes worker deployment '{deployment_name}' disappeared during startup."
                raise WorkerBackendError(msg)
            if deployment_ready_fn(deployment):
                return deployment
            if time.time() >= deadline:
                msg = f"Kubernetes worker '{deployment_name}' did not become ready within {timeout_seconds:.0f}s."
                raise WorkerBackendError(msg)
            time.sleep(_READY_POLL_INTERVAL_SECONDS)

    def _apply_object(
        self,
        *,
        read_fn: Callable[[str, str], object],
        create_fn: Callable[[str, dict[str, object]], object],
        patch_fn: Callable[[str, str, dict[str, object]], object],
        resource_name: str,
        manifest: dict[str, object],
    ) -> None:
        try:
            read_fn(resource_name, self.config.namespace)
        except self._api_exception as exc:
            if exc.status != 404:
                raise
            try:
                create_fn(self.config.namespace, manifest)
            except self._api_exception as create_exc:
                if create_exc.status != 409:
                    raise
                patch_fn(resource_name, self.config.namespace, manifest)
            return
        patch_fn(resource_name, self.config.namespace, manifest)

    def _delete_object(self, delete_fn: Callable[[str, str], None], resource_name: str) -> None:
        try:
            delete_fn(resource_name, self.config.namespace)
        except self._api_exception as exc:
            if exc.status != 404:
                raise

    def _load_clients(self) -> None:
        if self.apps_api is not None and self.core_api is not None and self.api_exception_cls is not None:
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

        self.apps_api = cast("_AppsApiProtocol", kubernetes_client.AppsV1Api())
        self.core_api = cast("_CoreApiProtocol", kubernetes_client.CoreV1Api())
        self.api_exception_cls = cast("type[_ApiStatusError]", kubernetes_exceptions.ApiException)

    def _service_manifest(self, worker_id: str) -> dict[str, object]:
        worker_labels = _labels(extra_labels=self.config.extra_labels, worker_id=worker_id)
        metadata: dict[str, object] = {
            "name": worker_id,
            "namespace": self.config.namespace,
            "labels": worker_labels,
        }
        owner_reference = self._owner_reference_or_none()
        if owner_reference is not None:
            metadata["ownerReferences"] = [owner_reference]
        return {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": metadata,
            "spec": {
                "selector": worker_labels,
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
        private_agent_names: frozenset[str] | None = None,
    ) -> dict[str, object]:
        worker_labels = _labels(extra_labels=self.config.extra_labels, worker_id=worker_id)
        template_metadata = {"labels": worker_labels}
        template_spec: dict[str, object] = {
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
                    "env": self._worker_env(worker_key, state_subpath),
                    "volumeMounts": self._volume_mounts(worker_key, state_subpath, private_agent_names),
                    "readinessProbe": {
                        "httpGet": {"path": "/healthz", "port": "api"},
                        "periodSeconds": 5,
                        "failureThreshold": 6,
                    },
                    "startupProbe": {
                        "httpGet": {"path": "/healthz", "port": "api"},
                        "periodSeconds": 5,
                        "failureThreshold": 60,
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
                        "capabilities": {"drop": ["ALL"]},
                    },
                },
            ],
            "volumes": self._volumes(),
        }
        template: dict[str, object] = {
            "metadata": template_metadata,
            "spec": template_spec,
        }
        metadata: dict[str, object] = {
            "name": worker_id,
            "namespace": self.config.namespace,
            "labels": worker_labels,
        }
        owner_reference = self._owner_reference_or_none()
        if owner_reference is not None:
            metadata["ownerReferences"] = [owner_reference]
        node_name = self._worker_node_name_or_none()
        if node_name is not None:
            template_spec["nodeName"] = node_name
        desired_annotations = dict(annotations)
        desired_annotations[ANNOTATION_TEMPLATE_HASH] = _template_hash(template)
        metadata["annotations"] = desired_annotations

        return {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": metadata,
            "spec": {
                "replicas": replicas,
                "selector": {"matchLabels": worker_labels},
                "template": template,
            },
        }

    def _worker_env(self, worker_key: str, state_subpath: str) -> list[dict[str, object]]:
        dedicated_root = f"{self.config.storage_mount_path}/{state_subpath}".rstrip("/")
        local_dedicated_root = (self.storage_root / state_subpath).resolve()
        venv_path = f"{dedicated_root}/venv"
        startup_runtime_paths = self._worker_runtime_paths(
            worker_key=worker_key,
            dedicated_root=Path(dedicated_root),
            local_dedicated_root=local_dedicated_root,
        )
        env: list[dict[str, object]] = [
            {"name": "MINDROOM_SANDBOX_RUNNER_MODE", "value": "true"},
            {"name": "MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE", "value": "subprocess"},
            {"name": _RUNNER_PORT_ENV_NAME, "value": str(self.config.worker_port)},
            {
                "name": _STARTUP_RUNTIME_PATHS_ENV,
                "value": json.dumps(
                    serialize_public_runtime_paths(startup_runtime_paths),
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            },
            {"name": "MINDROOM_CONFIG_PATH", "value": self.config.config_path},
            {"name": "MINDROOM_STORAGE_PATH", "value": dedicated_root},
            {"name": _SHARED_STORAGE_ROOT_ENV, "value": self.config.storage_mount_path},
            {"name": "VIRTUAL_ENV", "value": venv_path},
            {"name": "PATH", "value": f"{venv_path}/bin:{_DEFAULT_CONTAINER_PATH}"},
            {
                "name": SHARED_CREDENTIALS_PATH_ENV,
                "value": f"{dedicated_root}/.shared_credentials",
            },
            {"name": _DEDICATED_WORKER_KEY_ENV, "value": worker_key},
            {"name": _DEDICATED_WORKER_ROOT_ENV, "value": dedicated_root},
            {"name": "HOME", "value": dedicated_root},
        ]
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

    def _worker_google_application_credentials_path(
        self,
        dedicated_root: Path,
        *,
        local_dedicated_root: Path,
    ) -> str | None:
        """Return a worker-visible ADC file path, copying the source into shared storage when needed."""
        raw_value = self.runtime_paths.env_value("GOOGLE_APPLICATION_CREDENTIALS")
        if raw_value is None or not raw_value.strip():
            return None
        if not self.storage_root.exists():
            return None

        source_path = constants.runtime_env_source_path(self.runtime_paths, "GOOGLE_APPLICATION_CREDENTIALS")
        if source_path is None or (not source_path.exists() and not source_path.is_symlink()):
            return None
        try:
            resolved_source_path = validate_local_copy_source_path(
                source_path,
                field_name="Kubernetes worker GOOGLE_APPLICATION_CREDENTIALS",
            )
        except ValueError as exc:
            raise WorkerBackendError(str(exc)) from exc
        if not resolved_source_path.is_file():
            return None

        runtime_dir = local_dedicated_root / ".runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        target_path = runtime_dir / resolved_source_path.name
        if resolved_source_path.resolve() != target_path.resolve():
            shutil.copyfile(resolved_source_path, target_path)
            target_path.chmod(0o600)
        return str(dedicated_root / ".runtime" / resolved_source_path.name)

    def _worker_runtime_paths(
        self,
        *,
        worker_key: str,
        dedicated_root: Path,
        local_dedicated_root: Path,
    ) -> RuntimePaths:
        config_path = (
            Path(self.config.config_path)
            if self.config.config_map_name is not None
            else self.runtime_paths.config_path.expanduser().resolve()
        )
        process_env = dict(self.runtime_paths.process_env)
        process_env.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
        env_file_values = dict(self.runtime_paths.env_file_values)
        env_file_values.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
        if google_application_credentials := self._worker_google_application_credentials_path(
            dedicated_root,
            local_dedicated_root=local_dedicated_root,
        ):
            process_env["GOOGLE_APPLICATION_CREDENTIALS"] = google_application_credentials
        process_env.update(
            {
                "MINDROOM_SANDBOX_RUNNER_MODE": "true",
                "MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE": "subprocess",
                _RUNNER_PORT_ENV_NAME: str(self.config.worker_port),
                "MINDROOM_CONFIG_PATH": str(config_path),
                "MINDROOM_STORAGE_PATH": str(dedicated_root),
                _SHARED_STORAGE_ROOT_ENV: self.config.storage_mount_path,
                SHARED_CREDENTIALS_PATH_ENV: f"{dedicated_root}/.shared_credentials",
                _DEDICATED_WORKER_KEY_ENV: worker_key,
                _DEDICATED_WORKER_ROOT_ENV: str(dedicated_root),
            },
        )
        process_env.update(self.config.extra_env)
        return RuntimePaths(
            config_path=config_path,
            config_dir=config_path.parent,
            env_path=config_path.parent / ".env",
            storage_root=dedicated_root.resolve(),
            process_env=MappingProxyType(process_env),
            env_file_values=MappingProxyType(env_file_values),
        )

    def _volume_mounts(
        self,
        worker_key: str,
        state_subpath: str,
        private_agent_names: frozenset[str] | None,
    ) -> list[dict[str, object]]:
        mounts = self._scoped_storage_mounts(
            worker_key,
            state_subpath,
            private_agent_names=private_agent_names,
        )
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
                "persistentVolumeClaim": {"claimName": self.config.storage_pvc_name},
            },
        ]
        if self.config.config_map_name is not None:
            volumes.append(
                {
                    "name": "worker-config",
                    "configMap": {"name": self.config.config_map_name},
                },
            )
        return volumes

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
            if exc.status != 404:
                raise
            pod = None
        self._control_plane_node_name = None if pod is None else pod.spec.node_name
        self._control_plane_node_name_loaded = True
        return self._control_plane_node_name

    def _owner_reference_or_none(self) -> dict[str, object] | None:
        if self._owner_reference_loaded:
            return self._owner_reference
        if self.config.owner_deployment_name is None:
            self._owner_reference_loaded = True
            return None

        owner_deployment = self.read_deployment(self.config.owner_deployment_name)
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

    def _scoped_storage_mounts(
        self,
        worker_key: str,
        state_subpath: str,
        *,
        private_agent_names: frozenset[str] | None,
    ) -> list[dict[str, object]]:
        mounted_storage_root = Path(self.config.storage_mount_path)
        if resolved_worker_key_scope(worker_key) == "user_agent" and private_agent_names is None:
            msg = f"user_agent workers require explicit private-agent visibility: {worker_key}"
            raise WorkerBackendError(msg)
        effective_private_agent_names = private_agent_names or frozenset()
        visible_state_roots = visible_state_roots_for_worker_key(
            mounted_storage_root,
            worker_key,
            private_agent_names=effective_private_agent_names,
        )
        local_visible_state_roots = visible_state_roots_for_worker_key(
            self.storage_root,
            worker_key,
            private_agent_names=effective_private_agent_names,
        )
        if not visible_state_roots or len(visible_state_roots) != len(local_visible_state_roots):
            msg = f"Unsupported worker key for scoped storage mounts: {worker_key}"
            raise WorkerBackendError(msg)
        for local_state_root in local_visible_state_roots:
            local_state_root.mkdir(parents=True, exist_ok=True)

        mounts: list[dict[str, object]] = [
            {
                "name": "worker-storage",
                "mountPath": str(state_root),
                "subPath": str(state_root.relative_to(mounted_storage_root)),
            }
            for state_root in visible_state_roots
        ]
        mounts.append(
            {
                "name": "worker-storage",
                "mountPath": f"{self.config.storage_mount_path}/{state_subpath}",
                "subPath": state_subpath,
            },
        )
        mount_paths = [str(mount["mountPath"]) for mount in mounts]
        if len(mount_paths) != len(set(mount_paths)):
            msg = f"Duplicate Kubernetes mountPath generated for worker key: {worker_key}"
            raise WorkerBackendError(msg)
        return mounts
