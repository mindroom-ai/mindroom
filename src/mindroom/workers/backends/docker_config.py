"""Configuration helpers for the Docker worker backend."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.constants import (
    RuntimePaths,
    resolve_config_relative_path,
    resolve_primary_runtime_paths,
    runtime_env_values,
    runtime_paths_with_storage_root,
)
from mindroom.credentials import runtime_credentials_manager_key
from mindroom.tool_system.worker_routing import worker_root_path
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.backends._dedicated_worker_common import (
    build_backend_config_signature,
    validate_dedicated_worker_extra_env,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

_DEFAULT_IDLE_TIMEOUT_SECONDS = 1800.0
_DEFAULT_READY_TIMEOUT_SECONDS = 60.0
_DEFAULT_WORKER_PORT = 8766
_DEFAULT_STORAGE_MOUNT_PATH = "/app/worker"
_DEFAULT_CONFIG_PATH = "/app/config-host/config.yaml"
_DEFAULT_NAME_PREFIX = "mindroom-worker"
_DEFAULT_PUBLISH_HOST = "127.0.0.1"

_WORKER_BACKEND_ENV = "MINDROOM_WORKER_BACKEND"
_IMAGE_ENV = "MINDROOM_DOCKER_WORKER_IMAGE"
_PORT_ENV = "MINDROOM_DOCKER_WORKER_PORT"
_STORAGE_MOUNT_PATH_ENV = "MINDROOM_DOCKER_WORKER_STORAGE_MOUNT_PATH"
_CONFIG_PATH_ENV = "MINDROOM_DOCKER_WORKER_CONFIG_PATH"
_HOST_CONFIG_PATH_ENV = "MINDROOM_DOCKER_WORKER_HOST_CONFIG_PATH"
_IDLE_TIMEOUT_ENV = "MINDROOM_DOCKER_WORKER_IDLE_TIMEOUT_SECONDS"
_READY_TIMEOUT_ENV = "MINDROOM_DOCKER_WORKER_READY_TIMEOUT_SECONDS"
_NAME_PREFIX_ENV = "MINDROOM_DOCKER_WORKER_NAME_PREFIX"
_PUBLISH_HOST_ENV = "MINDROOM_DOCKER_WORKER_PUBLISH_HOST"
_ENDPOINT_HOST_ENV = "MINDROOM_DOCKER_WORKER_ENDPOINT_HOST"
_USER_ENV = "MINDROOM_DOCKER_WORKER_USER"
_EXTRA_ENV_JSON_ENV = "MINDROOM_DOCKER_WORKER_ENV_JSON"
_EXTRA_LABELS_JSON_ENV = "MINDROOM_DOCKER_WORKER_LABELS_JSON"
_DOCKER_RESERVED_EXTRA_ENV_NAMES = frozenset(
    {
        "MINDROOM_RUNTIME_PATHS_JSON",
        "MINDROOM_SANDBOX_PROXY_TOKEN",
    },
)


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


def _read_host_config_path(runtime_paths: RuntimePaths, env: Mapping[str, str]) -> Path | None:
    configured = _read_env(env, _HOST_CONFIG_PATH_ENV)
    if configured:
        resolved = resolve_config_relative_path(configured, runtime_paths)
        if not resolved.exists():
            msg = f"{_HOST_CONFIG_PATH_ENV} points to a missing file: {resolved}"
            raise WorkerBackendError(msg)
        return resolved
    runtime_config_path = runtime_paths.config_path.expanduser().resolve()
    if runtime_config_path.exists():
        return runtime_config_path
    return None


def _default_docker_user_for_os(os_name: str) -> str | None:
    if os_name == "posix":
        return f"{os.getuid()}:{os.getgid()}"
    if os_name == "nt":
        return None
    return None


def _default_docker_user() -> str | None:
    return _default_docker_user_for_os(os.name)


def _read_docker_user(env: Mapping[str, str] | None = None) -> str | None:
    raw_value = os.getenv(_USER_ENV) if env is None else env.get(_USER_ENV)
    if raw_value is None:
        return _default_docker_user()
    normalized = raw_value.strip()
    return normalized or None


def normalize_docker_name_prefix(raw_value: str) -> str:
    """Normalize a configured Docker name prefix to container-safe characters."""
    normalized = re.sub(r"[^a-z0-9-]+", "-", raw_value.strip().lower()).strip("-")
    return normalized or _DEFAULT_NAME_PREFIX


def docker_workers_root(base_storage_path: Path) -> Path:
    """Return the top-level workers directory used by the Docker backend."""
    return worker_root_path(base_storage_path, "__mindroom_root__").parent


def resolve_docker_storage_path(storage_path: Path | None = None, *, runtime_paths: RuntimePaths | None = None) -> Path:
    """Resolve the storage root used by the Docker backend."""
    if storage_path is not None:
        base_storage_path = storage_path
    elif runtime_paths is not None:
        base_storage_path = runtime_paths.storage_root
    else:
        base_storage_path = resolve_primary_runtime_paths(process_env=dict(os.environ)).storage_root
    return base_storage_path.expanduser().resolve()


@dataclass(frozen=True, slots=True)
class _DockerWorkerBackendConfig:
    image: str
    worker_port: int
    storage_mount_path: str
    config_path: str
    host_config_path: Path | None
    idle_timeout_seconds: float
    ready_timeout_seconds: float
    name_prefix: str
    publish_host: str
    endpoint_host: str
    user: str | None
    extra_env: dict[str, str]
    extra_labels: dict[str, str]

    @classmethod
    def from_runtime(cls, runtime_paths: RuntimePaths) -> _DockerWorkerBackendConfig:
        env = runtime_env_values(runtime_paths)
        image = _read_env(env, _IMAGE_ENV)
        if not image:
            msg = f"{_IMAGE_ENV} must be set when {_WORKER_BACKEND_ENV}=docker."
            raise WorkerBackendError(msg)

        publish_host = _read_env(env, _PUBLISH_HOST_ENV, _DEFAULT_PUBLISH_HOST) or _DEFAULT_PUBLISH_HOST
        endpoint_host = _read_env(env, _ENDPOINT_HOST_ENV, publish_host) or publish_host
        extra_env = _read_json_mapping_env(env, _EXTRA_ENV_JSON_ENV)
        validate_dedicated_worker_extra_env(
            extra_env,
            backend_name="Docker",
            extra_reserved_names=_DOCKER_RESERVED_EXTRA_ENV_NAMES,
        )
        return cls(
            image=image,
            worker_port=_read_int_env(env, _PORT_ENV, _DEFAULT_WORKER_PORT),
            storage_mount_path=_read_env(env, _STORAGE_MOUNT_PATH_ENV, _DEFAULT_STORAGE_MOUNT_PATH)
            or _DEFAULT_STORAGE_MOUNT_PATH,
            config_path=_read_env(env, _CONFIG_PATH_ENV, _DEFAULT_CONFIG_PATH) or _DEFAULT_CONFIG_PATH,
            host_config_path=_read_host_config_path(runtime_paths, env),
            idle_timeout_seconds=_read_float_env(env, _IDLE_TIMEOUT_ENV, _DEFAULT_IDLE_TIMEOUT_SECONDS),
            ready_timeout_seconds=_read_float_env(env, _READY_TIMEOUT_ENV, _DEFAULT_READY_TIMEOUT_SECONDS),
            name_prefix=_read_env(env, _NAME_PREFIX_ENV, _DEFAULT_NAME_PREFIX) or _DEFAULT_NAME_PREFIX,
            publish_host=publish_host,
            endpoint_host=endpoint_host,
            user=_read_docker_user(env),
            extra_env=extra_env,
            extra_labels=_read_json_mapping_env(env, _EXTRA_LABELS_JSON_ENV),
        )

    @classmethod
    def from_env(cls) -> _DockerWorkerBackendConfig:
        return cls.from_runtime(resolve_primary_runtime_paths(process_env=dict(os.environ)))


def docker_backend_config_signature(
    runtime_paths: RuntimePaths,
    *,
    auth_token: str | None,
    storage_path: Path | None = None,
) -> tuple[str, ...]:
    """Return a cache signature for one concrete Docker backend config."""
    effective_runtime_paths = runtime_paths_with_storage_root(
        runtime_paths,
        resolve_docker_storage_path(storage_path, runtime_paths=runtime_paths),
    )
    config = _DockerWorkerBackendConfig.from_runtime(effective_runtime_paths)
    workers_root = docker_workers_root(effective_runtime_paths.storage_root)
    credentials_key = runtime_credentials_manager_key(effective_runtime_paths)
    return build_backend_config_signature(
        prefix_parts=(
            "docker",
            config.image,
            str(config.worker_port),
            config.storage_mount_path,
            config.config_path,
            str(config.host_config_path or ""),
            str(config.idle_timeout_seconds),
            str(config.ready_timeout_seconds),
            config.name_prefix,
            config.publish_host,
            config.endpoint_host,
            config.user or "",
            str(workers_root),
            str(credentials_key.shared_base_path),
            credentials_key.current_worker_key or "",
            str(credentials_key.current_worker_root or ""),
        ),
        runtime_paths=effective_runtime_paths,
        json_values=(
            config.extra_env,
            config.extra_labels,
        ),
        suffix_parts=(auth_token or "",),
    )
