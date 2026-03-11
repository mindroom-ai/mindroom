"""Environment-backed configuration for the Kubernetes worker backend."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from mindroom.workers.backend import WorkerBackendError

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
_OWNER_DEPLOYMENT_NAME_ENV = "MINDROOM_KUBERNETES_WORKER_OWNER_DEPLOYMENT_NAME"
_POD_NAMESPACE_ENV = "POD_NAMESPACE"


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


@dataclass(frozen=True, slots=True)
class _KubernetesWorkerBackendConfig:
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
    owner_deployment_name: str | None

    @classmethod
    def from_env(cls) -> _KubernetesWorkerBackendConfig:
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
            owner_deployment_name=os.getenv(_OWNER_DEPLOYMENT_NAME_ENV, "").strip() or None,
        )


def kubernetes_backend_config_signature(*, auth_token: str | None) -> tuple[str, ...]:
    """Return a cache signature for one concrete Kubernetes backend config."""
    config = _KubernetesWorkerBackendConfig.from_env()
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
        config.owner_deployment_name or "",
        auth_token or "",
    )
