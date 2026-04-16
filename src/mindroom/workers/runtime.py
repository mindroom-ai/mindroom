"""Primary-runtime worker backend selection and caching."""

from __future__ import annotations

import json
import threading
from typing import TYPE_CHECKING

from mindroom.constants import DEFAULT_WORKER_GRANTABLE_CREDENTIALS
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.backends.kubernetes import KubernetesWorkerBackend, kubernetes_backend_config_signature
from mindroom.workers.backends.static_runner import StaticSandboxRunnerBackend, normalize_static_runner_api_root
from mindroom.workers.manager import WorkerManager

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

_PRIMARY_WORKER_BACKEND_ENV = "MINDROOM_WORKER_BACKEND"
_PRIMARY_WORKER_MANAGER: WorkerManager | None = None
_PRIMARY_WORKER_MANAGER_CONFIG: tuple[str, ...] | None = None
_PRIMARY_WORKER_MANAGER_LOCK = threading.Lock()


def serialized_kubernetes_worker_validation_snapshot(
    runtime_paths: RuntimePaths,
    *,
    runtime_config: Config | None = None,
) -> dict[str, dict[str, object]]:
    """Build the authoritative worker validation snapshot in the primary runtime."""
    from mindroom.config.main import load_config  # noqa: PLC0415
    from mindroom.tool_system.metadata import (  # noqa: PLC0415
        resolved_tool_validation_snapshot_for_runtime,
        serialize_tool_validation_snapshot,
    )

    snapshot = resolved_tool_validation_snapshot_for_runtime(
        runtime_paths,
        runtime_config or load_config(runtime_paths),
    )
    return serialize_tool_validation_snapshot(snapshot)


def _normalize_backend_name(raw_value: str | None) -> str:
    normalized = (raw_value or "").strip().lower()
    if normalized in {"", "static", "static_runner", "shared_runner", "static_sandbox_runner"}:
        return "static_runner"
    if normalized in {"k8s", "kubernetes"}:
        return "kubernetes"
    msg = f"Unsupported worker backend: {raw_value}"
    raise WorkerBackendError(msg)


def primary_worker_backend_name(runtime_paths: RuntimePaths) -> str:
    """Return the configured primary-runtime worker backend name."""
    return _normalize_backend_name(runtime_paths.env_value(_PRIMARY_WORKER_BACKEND_ENV))


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
    if backend_name == "kubernetes":
        if not proxy_token:
            return False
        try:
            kubernetes_backend_config_signature(runtime_paths, auth_token=proxy_token)
        except WorkerBackendError:
            return False
        return True
    return False


def _require_kubernetes_tool_validation_snapshot(
    kubernetes_tool_validation_snapshot: dict[str, dict[str, object]] | None,
) -> dict[str, dict[str, object]]:
    if kubernetes_tool_validation_snapshot is None:
        msg = "Kubernetes worker backend requires an explicit tool validation snapshot."
        raise WorkerBackendError(msg)
    return kubernetes_tool_validation_snapshot


def _resolve_worker_grantable_credentials(
    worker_grantable_credentials: frozenset[str] | None,
) -> frozenset[str]:
    if worker_grantable_credentials is None:
        return DEFAULT_WORKER_GRANTABLE_CREDENTIALS
    return worker_grantable_credentials


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
    kubernetes_tool_validation_snapshot: dict[str, dict[str, object]] | None = None,
    worker_grantable_credentials: frozenset[str] | None = None,
) -> tuple[str, ...]:
    backend_name = primary_worker_backend_name(runtime_paths)
    if backend_name == "static_runner":
        return _static_runner_backend_config_signature(proxy_url=proxy_url, proxy_token=proxy_token)
    if backend_name == "kubernetes":
        backend_signature = kubernetes_backend_config_signature(
            runtime_paths,
            auth_token=proxy_token,
            storage_root=storage_root,
        )
        kubernetes_tool_validation_snapshot = _require_kubernetes_tool_validation_snapshot(
            kubernetes_tool_validation_snapshot,
        )
        resolved_worker_grantable_credentials = _resolve_worker_grantable_credentials(
            worker_grantable_credentials,
        )
        allowlist_signature = (
            "__worker_grantable_credentials__",
            *sorted(resolved_worker_grantable_credentials),
        )
        return (
            *backend_signature,
            json.dumps(kubernetes_tool_validation_snapshot, separators=(",", ":"), sort_keys=True),
            *allowlist_signature,
        )
    msg = f"Unsupported worker backend: {backend_name}"
    raise WorkerBackendError(msg)


def _build_primary_worker_manager(
    runtime_paths: RuntimePaths,
    *,
    proxy_url: str | None,
    proxy_token: str | None,
    storage_root: Path | None,
    kubernetes_tool_validation_snapshot: dict[str, dict[str, object]] | None = None,
    worker_grantable_credentials: frozenset[str] | None = None,
) -> WorkerManager:
    backend_name = primary_worker_backend_name(runtime_paths)
    if backend_name == "static_runner":
        return WorkerManager(
            StaticSandboxRunnerBackend(
                api_root=normalize_static_runner_api_root(proxy_url or ""),
                auth_token=proxy_token,
            ),
        )
    if backend_name == "kubernetes":
        if storage_root is None:
            msg = "Kubernetes worker backend requires an explicit runtime storage root."
            raise WorkerBackendError(msg)
        kubernetes_tool_validation_snapshot = _require_kubernetes_tool_validation_snapshot(
            kubernetes_tool_validation_snapshot,
        )
        resolved_worker_grantable_credentials = _resolve_worker_grantable_credentials(
            worker_grantable_credentials,
        )
        return WorkerManager(
            KubernetesWorkerBackend.from_runtime(
                runtime_paths,
                auth_token=proxy_token,
                storage_root=storage_root,
                tool_validation_snapshot=kubernetes_tool_validation_snapshot,
                worker_grantable_credentials=resolved_worker_grantable_credentials,
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
    kubernetes_tool_validation_snapshot: dict[str, dict[str, object]] | None = None,
    worker_grantable_credentials: frozenset[str] | None = None,
) -> WorkerManager:
    """Return the primary-runtime worker manager for the current backend config."""
    global _PRIMARY_WORKER_MANAGER, _PRIMARY_WORKER_MANAGER_CONFIG

    config_signature = _primary_worker_backend_config_signature(
        runtime_paths,
        proxy_url=proxy_url,
        proxy_token=proxy_token,
        storage_root=storage_root,
        kubernetes_tool_validation_snapshot=kubernetes_tool_validation_snapshot,
        worker_grantable_credentials=worker_grantable_credentials,
    )
    with _PRIMARY_WORKER_MANAGER_LOCK:
        if _PRIMARY_WORKER_MANAGER is None or config_signature != _PRIMARY_WORKER_MANAGER_CONFIG:
            _PRIMARY_WORKER_MANAGER = _build_primary_worker_manager(
                runtime_paths,
                proxy_url=proxy_url,
                proxy_token=proxy_token,
                storage_root=storage_root,
                kubernetes_tool_validation_snapshot=kubernetes_tool_validation_snapshot,
                worker_grantable_credentials=worker_grantable_credentials,
            )
            _PRIMARY_WORKER_MANAGER_CONFIG = config_signature
    return _PRIMARY_WORKER_MANAGER


def _reset_primary_worker_manager() -> None:
    """Reset the cached primary worker manager. Intended for tests."""
    global _PRIMARY_WORKER_MANAGER, _PRIMARY_WORKER_MANAGER_CONFIG
    with _PRIMARY_WORKER_MANAGER_LOCK:
        _PRIMARY_WORKER_MANAGER = None
        _PRIMARY_WORKER_MANAGER_CONFIG = None
