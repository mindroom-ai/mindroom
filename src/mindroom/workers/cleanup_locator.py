"""Durable, non-secret locators for exact dedicated-worker cleanup."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from mindroom.constants import RuntimePaths, runtime_env_values
from mindroom.runtime_env_policy import KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.backends.docker_config import DockerWorkerBackendConfig, resolve_docker_storage_path

_DOCKER_TRANSPORT_ENV_NAMES = (
    "DOCKER_HOST",
    "DOCKER_TLS_VERIFY",
    "DOCKER_CERT_PATH",
)


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


def serialize_worker_cleanup_locator(locator: DockerWorkerCleanupLocator) -> str:
    """Serialize one locator canonically for durable storage."""
    return json.dumps(asdict(locator), separators=(",", ":"), sort_keys=True)


def parse_worker_cleanup_locator(serialized_locator: str) -> DockerWorkerCleanupLocator:
    """Parse and strictly validate one durable cleanup locator."""
    try:
        payload = json.loads(serialized_locator)
        if not isinstance(payload, dict) or payload.get("version") != 1:
            msg = "unsupported locator version"
            raise ValueError(msg)  # noqa: TRY301
        backend = payload.get("backend")
        if backend != "docker":
            msg = "unsupported locator backend"
            raise ValueError(msg)  # noqa: TRY301
        locator = DockerWorkerCleanupLocator(**payload)
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
