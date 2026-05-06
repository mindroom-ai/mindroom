"""Environment-backed configuration for the Kubernetes worker backend."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.constants import runtime_env_values
from mindroom.workers.backend import WorkerBackendError

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from mindroom.constants import RuntimePaths

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
_DEFAULT_MEMORY_REQUEST = "256Mi"
_DEFAULT_MEMORY_LIMIT = "1Gi"
_DEFAULT_CPU_REQUEST = "100m"
_DEFAULT_CPU_LIMIT = "500m"

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
_IDLE_TIMEOUT_ENV = "MINDROOM_KUBERNETES_WORKER_IDLE_TIMEOUT_SECONDS"
_READY_TIMEOUT_ENV = "MINDROOM_KUBERNETES_WORKER_READY_TIMEOUT_SECONDS"
_NAME_PREFIX_ENV = "MINDROOM_KUBERNETES_WORKER_NAME_PREFIX"
_NODE_NAME_ENV = "MINDROOM_KUBERNETES_WORKER_NODE_NAME"
_COLOCATE_WITH_CONTROL_PLANE_NODE_ENV = "MINDROOM_KUBERNETES_WORKER_COLOCATE_WITH_CONTROL_PLANE_NODE"
_EXTRA_ENV_JSON_ENV = "MINDROOM_KUBERNETES_WORKER_ENV_JSON"
_EXTRA_LABELS_JSON_ENV = "MINDROOM_KUBERNETES_WORKER_LABELS_JSON"
_EXTRA_ANNOTATIONS_JSON_ENV = "MINDROOM_KUBERNETES_WORKER_ANNOTATIONS_JSON"
_OWNER_DEPLOYMENT_NAME_ENV = "MINDROOM_KUBERNETES_WORKER_OWNER_DEPLOYMENT_NAME"
_MEMORY_REQUEST_ENV = "MINDROOM_KUBERNETES_WORKER_MEMORY_REQUEST"
_MEMORY_LIMIT_ENV = "MINDROOM_KUBERNETES_WORKER_MEMORY_LIMIT"
_CPU_REQUEST_ENV = "MINDROOM_KUBERNETES_WORKER_CPU_REQUEST"
_CPU_LIMIT_ENV = "MINDROOM_KUBERNETES_WORKER_CPU_LIMIT"
_ENABLE_SERVICE_LINKS_ENV = "MINDROOM_KUBERNETES_WORKER_ENABLE_SERVICE_LINKS"
_AUTH_SECRET_NAME_ENV = "MINDROOM_KUBERNETES_WORKER_AUTH_SECRET_NAME"  # noqa: S105
_POD_NAMESPACE_ENV = "POD_NAMESPACE"


def _read_env(env: Mapping[str, str], name: str, default: str = "") -> str:
    return env.get(name, default).strip()


def _read_float_env(env: Mapping[str, str], name: str, default: float) -> float:
    raw = _read_env(env, name, str(default))
    try:
        value = float(raw)
    except ValueError:
        value = default
    return max(1.0, value)


def _read_int_env(env: Mapping[str, str], name: str, default: int) -> int:
    raw = _read_env(env, name, str(default))
    try:
        value = int(raw)
    except ValueError:
        value = default
    return max(1, value)


def _read_bool_env(env: Mapping[str, str], name: str, default: bool = False) -> bool:
    raw = env.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _read_json_mapping_env(env: Mapping[str, str], name: str) -> dict[str, str]:
    raw = _read_env(env, name)
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
    idle_timeout_seconds: float
    ready_timeout_seconds: float
    name_prefix: str
    node_name: str | None
    colocate_with_control_plane_node: bool
    extra_env: dict[str, str]
    extra_labels: dict[str, str]
    extra_annotations: dict[str, str]
    owner_deployment_name: str | None
    resource_requests: dict[str, str]
    resource_limits: dict[str, str]
    enable_service_links: bool
    auth_secret_name: str | None

    @classmethod
    def from_runtime(cls, runtime_paths: RuntimePaths) -> KubernetesWorkerBackendConfig:
        """Build Kubernetes worker configuration from one explicit runtime context."""
        env = runtime_env_values(runtime_paths)
        namespace = _read_env(env, _NAMESPACE_ENV) or _read_env(env, _POD_NAMESPACE_ENV) or "default"
        image = _read_env(env, _IMAGE_ENV)
        if not image:
            msg = f"{_IMAGE_ENV} must be set when {_WORKER_BACKEND_ENV}=kubernetes."
            raise WorkerBackendError(msg)

        storage_pvc_name = _read_env(env, _STORAGE_PVC_ENV)
        if not storage_pvc_name:
            msg = f"{_STORAGE_PVC_ENV} must be set when {_WORKER_BACKEND_ENV}=kubernetes."
            raise WorkerBackendError(msg)

        config_map_name = _read_env(env, _CONFIG_MAP_NAME_ENV) or None
        resource_requests = {
            "memory": _read_env(env, _MEMORY_REQUEST_ENV, _DEFAULT_MEMORY_REQUEST) or _DEFAULT_MEMORY_REQUEST,
            "cpu": _read_env(env, _CPU_REQUEST_ENV, _DEFAULT_CPU_REQUEST) or _DEFAULT_CPU_REQUEST,
        }
        resource_limits = {
            "memory": _read_env(env, _MEMORY_LIMIT_ENV, _DEFAULT_MEMORY_LIMIT) or _DEFAULT_MEMORY_LIMIT,
            "cpu": _read_env(env, _CPU_LIMIT_ENV, _DEFAULT_CPU_LIMIT) or _DEFAULT_CPU_LIMIT,
        }
        return cls(
            namespace=namespace,
            image=image,
            image_pull_policy=_read_env(env, _IMAGE_PULL_POLICY_ENV, _DEFAULT_IMAGE_PULL_POLICY)
            or _DEFAULT_IMAGE_PULL_POLICY,
            worker_port=_read_int_env(env, _PORT_ENV, _DEFAULT_WORKER_PORT),
            service_account_name=_read_env(env, _SERVICE_ACCOUNT_ENV, _DEFAULT_SERVICE_ACCOUNT_NAME)
            or _DEFAULT_SERVICE_ACCOUNT_NAME,
            storage_pvc_name=storage_pvc_name,
            storage_mount_path=_read_env(env, _STORAGE_MOUNT_PATH_ENV, _DEFAULT_STORAGE_MOUNT_PATH)
            or _DEFAULT_STORAGE_MOUNT_PATH,
            storage_subpath_prefix=_read_env(env, _STORAGE_SUBPATH_PREFIX_ENV, _DEFAULT_STORAGE_SUBPATH_PREFIX)
            or _DEFAULT_STORAGE_SUBPATH_PREFIX,
            config_map_name=config_map_name,
            config_key=_read_env(env, _CONFIG_KEY_ENV, _DEFAULT_CONFIG_KEY) or _DEFAULT_CONFIG_KEY,
            config_path=_read_env(env, _CONFIG_PATH_ENV, _DEFAULT_CONFIG_PATH) or _DEFAULT_CONFIG_PATH,
            idle_timeout_seconds=_read_float_env(env, _IDLE_TIMEOUT_ENV, _DEFAULT_IDLE_TIMEOUT_SECONDS),
            ready_timeout_seconds=_read_float_env(env, _READY_TIMEOUT_ENV, _DEFAULT_READY_TIMEOUT_SECONDS),
            name_prefix=_read_env(env, _NAME_PREFIX_ENV, _DEFAULT_NAME_PREFIX) or _DEFAULT_NAME_PREFIX,
            node_name=_read_env(env, _NODE_NAME_ENV) or None,
            colocate_with_control_plane_node=_read_bool_env(env, _COLOCATE_WITH_CONTROL_PLANE_NODE_ENV, default=False),
            extra_env=_read_json_mapping_env(env, _EXTRA_ENV_JSON_ENV),
            extra_labels=_read_json_mapping_env(env, _EXTRA_LABELS_JSON_ENV),
            extra_annotations=_read_json_mapping_env(env, _EXTRA_ANNOTATIONS_JSON_ENV),
            owner_deployment_name=_read_env(env, _OWNER_DEPLOYMENT_NAME_ENV) or None,
            resource_requests=resource_requests,
            resource_limits=resource_limits,
            enable_service_links=_read_bool_env(env, _ENABLE_SERVICE_LINKS_ENV, default=False),
            auth_secret_name=_read_env(env, _AUTH_SECRET_NAME_ENV) or None,
        )


def kubernetes_backend_config_signature(
    runtime_paths: RuntimePaths,
    *,
    auth_token: str | None,
    storage_root: Path | None = None,
) -> tuple[str, ...]:
    """Return a cache signature for one concrete Kubernetes backend config."""
    config = KubernetesWorkerBackendConfig.from_runtime(runtime_paths)
    extra_env_json = json.dumps(config.extra_env, sort_keys=True, separators=(",", ":"))
    extra_labels_json = json.dumps(config.extra_labels, sort_keys=True, separators=(",", ":"))
    extra_annotations_json = json.dumps(config.extra_annotations, sort_keys=True, separators=(",", ":"))
    resource_requests_json = json.dumps(config.resource_requests, sort_keys=True, separators=(",", ":"))
    resource_limits_json = json.dumps(config.resource_limits, sort_keys=True, separators=(",", ":"))
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
        str(config.idle_timeout_seconds),
        str(config.ready_timeout_seconds),
        config.name_prefix,
        config.node_name or "",
        str(config.colocate_with_control_plane_node),
        extra_env_json,
        extra_labels_json,
        extra_annotations_json,
        config.owner_deployment_name or "",
        resource_requests_json,
        resource_limits_json,
        str(config.enable_service_links),
        config.auth_secret_name or "",
        auth_token or "",
        str(storage_root.expanduser().resolve()) if storage_root is not None else "",
    )
