"""Primary-runtime worker backend selection and caching."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.backends.docker import DockerWorkerBackend, docker_backend_config_signature
from mindroom.workers.backends.kubernetes import KubernetesWorkerBackend, kubernetes_backend_config_signature
from mindroom.workers.backends.static_runner import StaticSandboxRunnerBackend, normalize_static_runner_api_root
from mindroom.workers.manager import WorkerManager

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths

_PRIMARY_WORKER_BACKEND_ENV = "MINDROOM_WORKER_BACKEND"
_DEDICATED_WORKER_BACKENDS = frozenset({"docker", "kubernetes"})
_PRIMARY_WORKER_MANAGER: WorkerManager | None = None
_PRIMARY_WORKER_MANAGER_CONFIG: tuple[str, ...] | None = None
_PRIMARY_WORKER_MANAGER_LOCK = threading.Lock()


def _normalize_backend_name(raw_value: str | None) -> str:
    normalized = (raw_value or "").strip().lower()
    if normalized in {"", "static", "static_runner", "shared_runner", "static_sandbox_runner"}:
        return "static_runner"
    if normalized == "docker":
        return "docker"
    if normalized in {"k8s", "kubernetes"}:
        return "kubernetes"
    msg = f"Unsupported worker backend: {raw_value}"
    raise WorkerBackendError(msg)


def primary_worker_backend_name(runtime_paths: RuntimePaths) -> str:
    """Return the configured primary-runtime worker backend name."""
    return _normalize_backend_name(runtime_paths.env_value(_PRIMARY_WORKER_BACKEND_ENV))


def primary_worker_backend_is_dedicated(runtime_paths: RuntimePaths) -> bool:
    """Return whether the configured backend provisions dedicated worker runtimes."""
    return primary_worker_backend_name(runtime_paths) in _DEDICATED_WORKER_BACKENDS


def primary_worker_backend_available(
    runtime_paths: RuntimePaths,
    *,
    proxy_url: str | None,
    proxy_token: str | None,
) -> bool:
    """Return whether the configured primary-runtime worker backend can route tool calls."""
    backend_name = primary_worker_backend_name(runtime_paths)
    if backend_name == "static_runner":
        return bool(proxy_url)
    if not primary_worker_backend_is_dedicated(runtime_paths) or not proxy_token:
        return False
    try:
        if backend_name == "docker":
            docker_backend_config_signature(
                auth_token=proxy_token,
                storage_path=runtime_paths.storage_root,
            )
        elif backend_name == "kubernetes":
            kubernetes_backend_config_signature(
                runtime_paths,
                auth_token=proxy_token,
                storage_root=runtime_paths.storage_root,
            )
        else:
            return False
    except WorkerBackendError:
        return False
    return True


def _static_runner_backend_config_signature(
    *,
    proxy_url: str | None,
    proxy_token: str | None,
) -> tuple[str, ...]:
    return (
        "static_runner",
        normalize_static_runner_api_root(proxy_url or ""),
        proxy_token or "",
    )


def _primary_worker_backend_config_signature(
    runtime_paths: RuntimePaths,
    *,
    proxy_url: str | None,
    proxy_token: str | None,
    storage_root: Path | None,
) -> tuple[str, ...]:
    backend_name = primary_worker_backend_name(runtime_paths)
    resolved_storage_root = (storage_root or runtime_paths.storage_root).expanduser().resolve()
    if backend_name == "static_runner":
        return _static_runner_backend_config_signature(proxy_url=proxy_url, proxy_token=proxy_token)
    if backend_name == "docker":
        return docker_backend_config_signature(
            auth_token=proxy_token,
            storage_path=resolved_storage_root,
        )
    if backend_name == "kubernetes":
        return kubernetes_backend_config_signature(
            runtime_paths,
            auth_token=proxy_token,
            storage_root=resolved_storage_root,
        )
    msg = f"Unsupported worker backend: {backend_name}"
    raise WorkerBackendError(msg)


def _build_primary_worker_manager(
    runtime_paths: RuntimePaths,
    *,
    proxy_url: str | None,
    proxy_token: str | None,
    storage_root: Path | None,
) -> WorkerManager:
    backend_name = primary_worker_backend_name(runtime_paths)
    resolved_storage_root = (storage_root or runtime_paths.storage_root).expanduser().resolve()
    if backend_name == "static_runner":
        return WorkerManager(
            StaticSandboxRunnerBackend(
                api_root=normalize_static_runner_api_root(proxy_url or ""),
                auth_token=proxy_token,
            ),
        )
    if backend_name == "docker":
        return WorkerManager(
            DockerWorkerBackend.from_env(
                auth_token=proxy_token,
                storage_path=resolved_storage_root,
            ),
        )
    if backend_name == "kubernetes":
        return WorkerManager(
            KubernetesWorkerBackend.from_runtime(
                runtime_paths,
                auth_token=proxy_token,
                storage_root=resolved_storage_root,
            ),
        )
    msg = f"Unsupported worker backend: {backend_name}"
    raise WorkerBackendError(msg)


def get_primary_worker_manager(
    runtime_paths: RuntimePaths,
    *,
    proxy_url: str | None,
    proxy_token: str | None,
    storage_root: Path | None = None,
) -> WorkerManager:
    """Return the primary-runtime worker manager for the current backend config."""
    global _PRIMARY_WORKER_MANAGER, _PRIMARY_WORKER_MANAGER_CONFIG

    config_signature = _primary_worker_backend_config_signature(
        runtime_paths,
        proxy_url=proxy_url,
        proxy_token=proxy_token,
        storage_root=storage_root,
    )
    with _PRIMARY_WORKER_MANAGER_LOCK:
        if _PRIMARY_WORKER_MANAGER is None or config_signature != _PRIMARY_WORKER_MANAGER_CONFIG:
            _PRIMARY_WORKER_MANAGER = _build_primary_worker_manager(
                runtime_paths,
                proxy_url=proxy_url,
                proxy_token=proxy_token,
                storage_root=storage_root,
            )
            _PRIMARY_WORKER_MANAGER_CONFIG = config_signature
    return _PRIMARY_WORKER_MANAGER


def _reset_primary_worker_manager() -> None:
    """Reset the cached primary worker manager. Intended for tests."""
    global _PRIMARY_WORKER_MANAGER, _PRIMARY_WORKER_MANAGER_CONFIG
    with _PRIMARY_WORKER_MANAGER_LOCK:
        _PRIMARY_WORKER_MANAGER = None
        _PRIMARY_WORKER_MANAGER_CONFIG = None
