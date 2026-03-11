"""Primary-runtime worker backend selection and caching."""

from __future__ import annotations

import os
import threading

from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.backends.kubernetes import KubernetesWorkerBackend, kubernetes_backend_config_signature
from mindroom.workers.backends.static_runner import StaticSandboxRunnerBackend, normalize_static_runner_api_root
from mindroom.workers.manager import WorkerManager

_PRIMARY_WORKER_BACKEND_ENV = "MINDROOM_WORKER_BACKEND"
_PRIMARY_WORKER_MANAGER: WorkerManager | None = None
_PRIMARY_WORKER_MANAGER_CONFIG: tuple[str, ...] | None = None
_PRIMARY_WORKER_MANAGER_LOCK = threading.Lock()


def _normalize_backend_name(raw_value: str | None) -> str:
    normalized = (raw_value or "").strip().lower()
    if normalized in {"", "static", "static_runner", "shared_runner", "static_sandbox_runner"}:
        return "static_runner"
    if normalized in {"k8s", "kubernetes"}:
        return "kubernetes"
    msg = f"Unsupported worker backend: {raw_value}"
    raise WorkerBackendError(msg)


def primary_worker_backend_name() -> str:
    """Return the configured primary-runtime worker backend name."""
    return _normalize_backend_name(os.getenv(_PRIMARY_WORKER_BACKEND_ENV))


def primary_worker_backend_available(*, proxy_url: str | None, proxy_token: str | None) -> bool:
    """Return whether the configured primary-runtime worker backend can route tool calls."""
    backend_name = primary_worker_backend_name()
    if backend_name == "static_runner":
        return bool(proxy_url)
    if backend_name == "kubernetes":
        if not proxy_token:
            return False
        try:
            kubernetes_backend_config_signature(auth_token=proxy_token)
        except WorkerBackendError:
            return False
        return True
    return False


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
    *,
    proxy_url: str | None,
    proxy_token: str | None,
) -> tuple[str, ...]:
    backend_name = primary_worker_backend_name()
    if backend_name == "static_runner":
        return _static_runner_backend_config_signature(proxy_url=proxy_url, proxy_token=proxy_token)
    if backend_name == "kubernetes":
        return kubernetes_backend_config_signature(auth_token=proxy_token)
    msg = f"Unsupported worker backend: {backend_name}"
    raise WorkerBackendError(msg)


def _build_primary_worker_manager(
    *,
    proxy_url: str | None,
    proxy_token: str | None,
) -> WorkerManager:
    backend_name = primary_worker_backend_name()
    if backend_name == "static_runner":
        return WorkerManager(
            StaticSandboxRunnerBackend(
                api_root=normalize_static_runner_api_root(proxy_url or ""),
                auth_token=proxy_token,
            ),
        )
    if backend_name == "kubernetes":
        return WorkerManager(KubernetesWorkerBackend.from_env(auth_token=proxy_token))
    msg = f"Unsupported worker backend: {backend_name}"
    raise WorkerBackendError(msg)


def get_primary_worker_manager(
    *,
    proxy_url: str | None,
    proxy_token: str | None,
) -> WorkerManager:
    """Return the primary-runtime worker manager for the current backend config."""
    global _PRIMARY_WORKER_MANAGER, _PRIMARY_WORKER_MANAGER_CONFIG

    config_signature = _primary_worker_backend_config_signature(
        proxy_url=proxy_url,
        proxy_token=proxy_token,
    )
    with _PRIMARY_WORKER_MANAGER_LOCK:
        if _PRIMARY_WORKER_MANAGER is None or config_signature != _PRIMARY_WORKER_MANAGER_CONFIG:
            _PRIMARY_WORKER_MANAGER = _build_primary_worker_manager(
                proxy_url=proxy_url,
                proxy_token=proxy_token,
            )
            _PRIMARY_WORKER_MANAGER_CONFIG = config_signature
    return _PRIMARY_WORKER_MANAGER


def _reset_primary_worker_manager() -> None:
    """Reset the cached primary worker manager. Intended for tests."""
    global _PRIMARY_WORKER_MANAGER, _PRIMARY_WORKER_MANAGER_CONFIG
    with _PRIMARY_WORKER_MANAGER_LOCK:
        _PRIMARY_WORKER_MANAGER = None
        _PRIMARY_WORKER_MANAGER_CONFIG = None
