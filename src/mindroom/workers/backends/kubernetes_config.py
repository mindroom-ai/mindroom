"""Environment-backed configuration for the Kubernetes worker backend."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.constants import runtime_env_values
from mindroom.runtime_env_policy import (
    CREDENTIALS_ENCRYPTION_KEY_ENV,
    KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY,
    credentials_encryption_key_value,
    is_worker_backend_config_env_name,
)
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.backends._config_helpers import (
    read_bool_env,
    read_env,
    read_float_env,
    read_int_env,
    read_json_mapping_env,
)
from mindroom.workers.backends._dedicated_worker_common import stable_signature_json

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

_DEFAULT_AGENT_VAULT_VAULT_NAME_PREFIX = "agent-vault"
_DEFAULT_AGENT_VAULT_API_URL = "http://agent-vault:14321"
_DEFAULT_AGENT_VAULT_PROXY_URL = "http://agent-vault:14322"
_DEFAULT_AGENT_VAULT_BOOTSTRAP_SECRET_NAME = "agent-vault-bootstrap"  # noqa: S105

_WORKER_BACKEND_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["worker_backend"]
_NAMESPACE_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["namespace"]
_IMAGE_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["image"]
_IMAGE_PULL_POLICY_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["image_pull_policy"]
_PORT_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["port"]
_SERVICE_ACCOUNT_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["service_account"]
_STORAGE_PVC_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["storage_pvc"]
_STORAGE_MOUNT_PATH_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["storage_mount_path"]
_STORAGE_SUBPATH_PREFIX_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["storage_subpath_prefix"]
_CONFIG_MAP_NAME_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["config_map_name"]
_CONFIG_KEY_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["config_key"]
_CONFIG_PATH_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["config_path"]
_IDLE_TIMEOUT_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["idle_timeout"]
_READY_TIMEOUT_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["ready_timeout"]
_NAME_PREFIX_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["name_prefix"]
_NODE_NAME_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["node_name"]
_COLOCATE_WITH_CONTROL_PLANE_NODE_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["colocate_with_control_plane_node"]
_RECONCILE_POD_TEMPLATES_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["reconcile_pod_templates"]
_EXTRA_ENV_JSON_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["extra_env_json"]
_EXTRA_LABELS_JSON_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["extra_labels_json"]
_EXTRA_ANNOTATIONS_JSON_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["extra_annotations_json"]
_OWNER_DEPLOYMENT_NAME_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["owner_deployment_name"]
_MEMORY_REQUEST_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["memory_request"]
_MEMORY_LIMIT_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["memory_limit"]
_CPU_REQUEST_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["cpu_request"]
_CPU_LIMIT_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["cpu_limit"]
_ENABLE_SERVICE_LINKS_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["enable_service_links"]
_AUTH_SECRET_NAME_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["auth_secret_name"]
_AGENT_VAULT_ENABLED_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["agent_vault_enabled"]
_AGENT_VAULT_VAULT_NAME_PREFIX_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["agent_vault_vault_name_prefix"]
_AGENT_VAULT_CLI_IMAGE_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["agent_vault_cli_image"]
_AGENT_VAULT_API_URL_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["agent_vault_api_url"]
_AGENT_VAULT_PROXY_URL_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["agent_vault_proxy_url"]
_AGENT_VAULT_OWNER_EMAIL_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["agent_vault_owner_email"]
_AGENT_VAULT_BOOTSTRAP_SECRET_NAME_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY[
    "agent_vault_bootstrap_secret_name"
]
_AGENT_VAULT_WORKER_CA_CONFIGMAP_NAME_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY[
    "agent_vault_worker_ca_configmap_name"
]
_POD_NAMESPACE_ENV = "POD_NAMESPACE"


def is_kubernetes_worker_backend_config_env_name(name: str) -> bool:
    """Return whether an env var configures the primary-side Kubernetes worker backend."""
    return is_worker_backend_config_env_name(name) and name != _WORKER_BACKEND_ENV


@dataclass(frozen=True, slots=True)
class KubernetesAgentVaultConfig:
    """Per-worker Agent Vault egress settings (no separate bridge pod).

    When present, each dedicated worker pod gets an init container that mints
    (or rotates) a proxy-role Agent Vault agent token for that worker's vault
    and writes it to a shared in-pod volume. The sandbox runner composes
    ``http://<token>:@<proxy host>`` for python/shell egress so Agent Vault
    injects credentials in transit. The owner password is mounted only on the
    init container, never on the agent-executing container.
    """

    vault_name_prefix: str
    cli_image: str
    api_url: str
    proxy_url: str
    owner_email: str
    bootstrap_secret_name: str
    worker_ca_configmap_name: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> KubernetesAgentVaultConfig | None:
        """Parse Agent Vault egress settings, returning ``None`` when disabled."""
        if not read_bool_env(env, _AGENT_VAULT_ENABLED_ENV, default=False):
            return None
        cli_image = read_env(env, _AGENT_VAULT_CLI_IMAGE_ENV)
        if not cli_image:
            msg = f"{_AGENT_VAULT_CLI_IMAGE_ENV} must be set when {_AGENT_VAULT_ENABLED_ENV} is enabled."
            raise WorkerBackendError(msg)
        owner_email = read_env(env, _AGENT_VAULT_OWNER_EMAIL_ENV)
        if not owner_email:
            msg = f"{_AGENT_VAULT_OWNER_EMAIL_ENV} must be set when {_AGENT_VAULT_ENABLED_ENV} is enabled."
            raise WorkerBackendError(msg)
        return cls(
            vault_name_prefix=read_env(env, _AGENT_VAULT_VAULT_NAME_PREFIX_ENV, _DEFAULT_AGENT_VAULT_VAULT_NAME_PREFIX)
            or _DEFAULT_AGENT_VAULT_VAULT_NAME_PREFIX,
            cli_image=cli_image,
            api_url=read_env(env, _AGENT_VAULT_API_URL_ENV, _DEFAULT_AGENT_VAULT_API_URL)
            or _DEFAULT_AGENT_VAULT_API_URL,
            proxy_url=read_env(env, _AGENT_VAULT_PROXY_URL_ENV, _DEFAULT_AGENT_VAULT_PROXY_URL)
            or _DEFAULT_AGENT_VAULT_PROXY_URL,
            owner_email=owner_email,
            bootstrap_secret_name=read_env(
                env,
                _AGENT_VAULT_BOOTSTRAP_SECRET_NAME_ENV,
                _DEFAULT_AGENT_VAULT_BOOTSTRAP_SECRET_NAME,
            )
            or _DEFAULT_AGENT_VAULT_BOOTSTRAP_SECRET_NAME,
            worker_ca_configmap_name=read_env(env, _AGENT_VAULT_WORKER_CA_CONFIGMAP_NAME_ENV) or None,
        )

    def signature(self) -> str:
        """Return a stable signature fragment for backend cache keys."""
        return stable_signature_json(
            {
                "vault_name_prefix": self.vault_name_prefix,
                "cli_image": self.cli_image,
                "api_url": self.api_url,
                "proxy_url": self.proxy_url,
                "owner_email": self.owner_email,
                "bootstrap_secret_name": self.bootstrap_secret_name,
                "worker_ca_configmap_name": self.worker_ca_configmap_name,
            },
        )


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
    reconcile_pod_templates: bool = True
    agent_vault: KubernetesAgentVaultConfig | None = None

    @classmethod
    def from_runtime(cls, runtime_paths: RuntimePaths) -> KubernetesWorkerBackendConfig:
        """Build Kubernetes worker configuration from one explicit runtime context."""
        env = runtime_env_values(runtime_paths)
        namespace = read_env(env, _NAMESPACE_ENV) or read_env(env, _POD_NAMESPACE_ENV) or "default"
        image = read_env(env, _IMAGE_ENV)
        if not image:
            msg = f"{_IMAGE_ENV} must be set when {_WORKER_BACKEND_ENV}=kubernetes."
            raise WorkerBackendError(msg)

        storage_pvc_name = read_env(env, _STORAGE_PVC_ENV)
        if not storage_pvc_name:
            msg = f"{_STORAGE_PVC_ENV} must be set when {_WORKER_BACKEND_ENV}=kubernetes."
            raise WorkerBackendError(msg)

        config_map_name = read_env(env, _CONFIG_MAP_NAME_ENV) or None
        resource_requests = {
            "memory": read_env(env, _MEMORY_REQUEST_ENV, _DEFAULT_MEMORY_REQUEST) or _DEFAULT_MEMORY_REQUEST,
            "cpu": read_env(env, _CPU_REQUEST_ENV, _DEFAULT_CPU_REQUEST) or _DEFAULT_CPU_REQUEST,
        }
        resource_limits = {
            "memory": read_env(env, _MEMORY_LIMIT_ENV, _DEFAULT_MEMORY_LIMIT) or _DEFAULT_MEMORY_LIMIT,
            "cpu": read_env(env, _CPU_LIMIT_ENV, _DEFAULT_CPU_LIMIT) or _DEFAULT_CPU_LIMIT,
        }
        return cls(
            namespace=namespace,
            image=image,
            image_pull_policy=read_env(env, _IMAGE_PULL_POLICY_ENV, _DEFAULT_IMAGE_PULL_POLICY)
            or _DEFAULT_IMAGE_PULL_POLICY,
            worker_port=read_int_env(env, _PORT_ENV, _DEFAULT_WORKER_PORT),
            service_account_name=read_env(env, _SERVICE_ACCOUNT_ENV, _DEFAULT_SERVICE_ACCOUNT_NAME)
            or _DEFAULT_SERVICE_ACCOUNT_NAME,
            storage_pvc_name=storage_pvc_name,
            storage_mount_path=read_env(env, _STORAGE_MOUNT_PATH_ENV, _DEFAULT_STORAGE_MOUNT_PATH)
            or _DEFAULT_STORAGE_MOUNT_PATH,
            storage_subpath_prefix=read_env(env, _STORAGE_SUBPATH_PREFIX_ENV, _DEFAULT_STORAGE_SUBPATH_PREFIX)
            or _DEFAULT_STORAGE_SUBPATH_PREFIX,
            config_map_name=config_map_name,
            config_key=read_env(env, _CONFIG_KEY_ENV, _DEFAULT_CONFIG_KEY) or _DEFAULT_CONFIG_KEY,
            config_path=read_env(env, _CONFIG_PATH_ENV, _DEFAULT_CONFIG_PATH) or _DEFAULT_CONFIG_PATH,
            idle_timeout_seconds=read_float_env(env, _IDLE_TIMEOUT_ENV, _DEFAULT_IDLE_TIMEOUT_SECONDS),
            ready_timeout_seconds=read_float_env(env, _READY_TIMEOUT_ENV, _DEFAULT_READY_TIMEOUT_SECONDS),
            name_prefix=read_env(env, _NAME_PREFIX_ENV, _DEFAULT_NAME_PREFIX) or _DEFAULT_NAME_PREFIX,
            node_name=read_env(env, _NODE_NAME_ENV) or None,
            colocate_with_control_plane_node=read_bool_env(env, _COLOCATE_WITH_CONTROL_PLANE_NODE_ENV, default=False),
            extra_env=read_json_mapping_env(env, _EXTRA_ENV_JSON_ENV),
            extra_labels=read_json_mapping_env(env, _EXTRA_LABELS_JSON_ENV),
            extra_annotations=read_json_mapping_env(env, _EXTRA_ANNOTATIONS_JSON_ENV),
            owner_deployment_name=read_env(env, _OWNER_DEPLOYMENT_NAME_ENV) or None,
            resource_requests=resource_requests,
            resource_limits=resource_limits,
            enable_service_links=read_bool_env(env, _ENABLE_SERVICE_LINKS_ENV, default=False),
            auth_secret_name=read_env(env, _AUTH_SECRET_NAME_ENV) or None,
            reconcile_pod_templates=read_bool_env(env, _RECONCILE_POD_TEMPLATES_ENV, default=True),
            agent_vault=KubernetesAgentVaultConfig.from_env(env),
        )


def kubernetes_backend_config_signature(
    runtime_paths: RuntimePaths,
    *,
    auth_token: str | None,
    storage_root: Path | None = None,
) -> tuple[str, ...]:
    """Return a cache signature for one concrete Kubernetes backend config."""
    config = KubernetesWorkerBackendConfig.from_runtime(runtime_paths)
    credentials_encryption_key = runtime_paths.env_value(CREDENTIALS_ENCRYPTION_KEY_ENV)
    credentials_encryption_key_marker = credentials_encryption_key_hash(credentials_encryption_key) or ""
    extra_env_json = stable_signature_json(config.extra_env)
    extra_labels_json = stable_signature_json(config.extra_labels)
    extra_annotations_json = stable_signature_json(config.extra_annotations)
    resource_requests_json = stable_signature_json(config.resource_requests)
    resource_limits_json = stable_signature_json(config.resource_limits)
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
        str(config.reconcile_pod_templates),
        config.agent_vault.signature() if config.agent_vault is not None else "",
        credentials_encryption_key_marker,
        auth_token or "",
        str(storage_root.expanduser().resolve()) if storage_root is not None else "",
    )


def credentials_encryption_key_hash(encryption_key: str | None) -> str | None:
    """Return a stable non-secret marker for the credential encryption key."""
    normalized_key = credentials_encryption_key_value(encryption_key)
    if normalized_key is None:
        return None
    return hashlib.sha256(normalized_key.encode("utf-8")).hexdigest()
