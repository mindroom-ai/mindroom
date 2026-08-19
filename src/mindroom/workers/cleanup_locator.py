"""Durable, non-secret locators for exact dedicated-worker cleanup."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml

from mindroom.constants import RuntimePaths, runtime_env_values
from mindroom.runtime_env_policy import KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.backends.docker_config import DockerWorkerBackendConfig, resolve_docker_storage_path
from mindroom.workers.backends.kubernetes_config import KubernetesWorkerBackendConfig, resolve_kubeconfig_paths

_DOCKER_TRANSPORT_ENV_NAMES = (
    "DOCKER_HOST",
    "DOCKER_TLS_VERIFY",
    "DOCKER_CERT_PATH",
)
_IN_CLUSTER_SERVICE_ACCOUNT_TOKEN = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")


@dataclass(frozen=True, slots=True)
class DockerWorkerCleanupLocator:
    """Inputs needed to find and retire one worker on its original Docker daemon."""

    version: Literal[1]
    backend: Literal["docker"]
    storage_root: str
    name_prefix: str
    docker_host: str | None
    docker_tls_verify: str | None
    docker_cert_path: str | None


@dataclass(frozen=True, slots=True)
class KubernetesWorkerCleanupLocator:
    """Inputs needed to find and retire one worker on its original Kubernetes cluster."""

    version: Literal[1]
    backend: Literal["kubernetes"]
    storage_root: str
    namespace: str
    name_prefix: str
    storage_subpath_prefix: str
    ready_timeout_seconds: float
    client_mode: Literal["in_cluster", "kubeconfig"]
    service_host: str | None
    service_port: str | None
    kubeconfig_paths: tuple[str, ...] | None
    kube_context: str | None


_WorkerCleanupLocator = DockerWorkerCleanupLocator | KubernetesWorkerCleanupLocator


def serialize_worker_cleanup_locator(locator: _WorkerCleanupLocator) -> str:
    """Serialize one locator canonically for durable storage."""
    return json.dumps(asdict(locator), separators=(",", ":"), sort_keys=True)


def parse_worker_cleanup_locator(serialized_locator: str) -> _WorkerCleanupLocator:
    """Parse and strictly validate one durable cleanup locator."""
    try:
        payload = json.loads(serialized_locator)
        if not isinstance(payload, dict) or payload.get("version") != 1:
            msg = "unsupported locator version"
            raise ValueError(msg)  # noqa: TRY301
        backend = payload.get("backend")
        if backend == "docker":
            locator: _WorkerCleanupLocator = DockerWorkerCleanupLocator(**payload)
        elif backend == "kubernetes":
            raw_paths = payload.get("kubeconfig_paths")
            if raw_paths is not None:
                if (
                    not isinstance(raw_paths, list)
                    or not raw_paths
                    or not all(isinstance(path, str) for path in raw_paths)
                ):
                    msg = "invalid Kubernetes kubeconfig paths"
                    raise ValueError(msg)  # noqa: TRY301
                payload["kubeconfig_paths"] = tuple(raw_paths)
            locator = KubernetesWorkerCleanupLocator(**payload)
        else:
            msg = "unsupported locator backend"
            raise ValueError(msg)  # noqa: TRY301
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        msg = f"Invalid durable worker cleanup locator: {exc}"
        raise WorkerBackendError(msg) from exc
    if serialize_worker_cleanup_locator(locator) != serialized_locator:
        msg = "Durable worker cleanup locator is not canonical."
        raise WorkerBackendError(msg)
    if not Path(locator.storage_root).is_absolute():
        msg = "Durable worker cleanup locator storage root must be absolute."
        raise WorkerBackendError(msg)
    return locator


def docker_worker_cleanup_locator(
    runtime_paths: RuntimePaths,
    *,
    storage_root: Path | None,
) -> DockerWorkerCleanupLocator:
    """Capture the non-secret inputs that select one Docker cleanup owner."""
    config = DockerWorkerBackendConfig.from_runtime(runtime_paths)
    env = runtime_env_values(runtime_paths)
    docker_host = env.get(_DOCKER_TRANSPORT_ENV_NAMES[0])
    if docker_host is not None and urlsplit(docker_host).password is not None:
        msg = "Credential-bearing DOCKER_HOST values cannot be persisted for background-script cleanup."
        raise WorkerBackendError(msg)
    docker_cert_path = env.get(_DOCKER_TRANSPORT_ENV_NAMES[2])
    if docker_cert_path:
        docker_cert_path = str(Path(docker_cert_path).expanduser().resolve())
    return DockerWorkerCleanupLocator(
        version=1,
        backend="docker",
        storage_root=str(resolve_docker_storage_path(storage_root, runtime_paths=runtime_paths)),
        name_prefix=config.name_prefix,
        docker_host=docker_host,
        docker_tls_verify=env.get(_DOCKER_TRANSPORT_ENV_NAMES[1]),
        docker_cert_path=docker_cert_path,
    )


def kubernetes_worker_cleanup_locator(
    runtime_paths: RuntimePaths,
    *,
    storage_root: Path,
) -> KubernetesWorkerCleanupLocator | None:
    """Capture the non-secret selectors and reachable client context for Kubernetes cleanup."""
    config = KubernetesWorkerBackendConfig.from_runtime(runtime_paths)
    env = runtime_env_values(runtime_paths)
    service_host = env.get("KUBERNETES_SERVICE_HOST")
    service_port = env.get("KUBERNETES_SERVICE_PORT")
    if service_host and service_port and _IN_CLUSTER_SERVICE_ACCOUNT_TOKEN.is_file():
        client_mode: Literal["in_cluster", "kubeconfig"] = "in_cluster"
        kubeconfig_paths = None
        kube_context = None
    else:
        client_mode = "kubeconfig"
        service_host = None
        service_port = None
        resolved_paths = tuple(path for path in resolve_kubeconfig_paths(runtime_paths) if path.is_file())
        kubeconfig_paths = tuple(str(path) for path in resolved_paths)
        kube_context = _kubeconfig_current_context(resolved_paths)
        if kube_context is None:
            return None
    return KubernetesWorkerCleanupLocator(
        version=1,
        backend="kubernetes",
        storage_root=str(storage_root.expanduser().resolve()),
        namespace=config.namespace,
        name_prefix=config.name_prefix,
        storage_subpath_prefix=config.storage_subpath_prefix,
        ready_timeout_seconds=config.ready_timeout_seconds,
        client_mode=client_mode,
        service_host=service_host,
        service_port=service_port,
        kubeconfig_paths=kubeconfig_paths,
        kube_context=kube_context,
    )


def _kubeconfig_current_context(kubeconfig_paths: tuple[Path, ...]) -> str | None:
    current_context: str | None = None
    for kubeconfig_path in kubeconfig_paths:
        try:
            payload = yaml.safe_load(kubeconfig_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(payload, dict):
            continue
        configured_context = payload.get("current-context")
        if isinstance(configured_context, str) and configured_context.strip():
            current_context = configured_context.strip()
    return current_context


def docker_cleanup_runtime_paths(
    runtime_paths: RuntimePaths,
    locator: DockerWorkerCleanupLocator,
) -> RuntimePaths:
    """Rebase a runtime context onto the exact Docker cleanup selector and transport."""
    process_env = {
        KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["worker_backend"]: "docker",
        "MINDROOM_DOCKER_WORKER_IMAGE": "cleanup-only",
        "MINDROOM_DOCKER_WORKER_NAME_PREFIX": locator.name_prefix,
    }
    process_env.update(
        {
            name: value
            for name, value in zip(
                _DOCKER_TRANSPORT_ENV_NAMES,
                (locator.docker_host, locator.docker_tls_verify, locator.docker_cert_path),
                strict=True,
            )
            if value is not None
        },
    )
    return RuntimePaths(
        config_path=runtime_paths.config_path,
        config_dir=runtime_paths.config_dir,
        env_path=runtime_paths.env_path,
        storage_root=Path(locator.storage_root),
        control_state_root=runtime_paths.control_state_root,
        process_env=process_env,
        env_file_values={},
    )


def kubernetes_cleanup_runtime_paths(
    runtime_paths: RuntimePaths,
    locator: KubernetesWorkerCleanupLocator,
) -> RuntimePaths:
    """Rebase a runtime context onto the exact Kubernetes cleanup selector."""
    process_env = {
        KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["worker_backend"]: "kubernetes",
        KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["image"]: "cleanup-only",
        KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["storage_pvc"]: "cleanup-only",
        KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["namespace"]: locator.namespace,
        KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["name_prefix"]: locator.name_prefix,
        KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["storage_subpath_prefix"]: locator.storage_subpath_prefix,
        KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["ready_timeout"]: str(locator.ready_timeout_seconds),
    }
    if locator.client_mode == "in_cluster":
        assert locator.service_host is not None
        assert locator.service_port is not None
        process_env["KUBERNETES_SERVICE_HOST"] = locator.service_host
        process_env["KUBERNETES_SERVICE_PORT"] = locator.service_port
    else:
        assert locator.kubeconfig_paths is not None
        process_env["KUBECONFIG"] = os.pathsep.join(locator.kubeconfig_paths)
    return RuntimePaths(
        config_path=runtime_paths.config_path,
        config_dir=runtime_paths.config_dir,
        env_path=runtime_paths.env_path,
        storage_root=Path(locator.storage_root),
        control_state_root=runtime_paths.control_state_root,
        process_env=process_env,
        env_file_values={},
    )
