"""Primary-runtime worker backend selection and caching."""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.constants import DEFAULT_WORKER_GRANTABLE_CREDENTIALS
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.backends.docker import DockerWorkerBackend, docker_backend_config_signature
from mindroom.workers.backends.kubernetes import KubernetesWorkerBackend, kubernetes_backend_config_signature
from mindroom.workers.backends.static_runner import StaticSandboxRunnerBackend, normalize_static_runner_api_root
from mindroom.workers.manager import WorkerManager

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

_PRIMARY_WORKER_BACKEND_ENV = "MINDROOM_WORKER_BACKEND"
_DEDICATED_WORKER_BACKENDS = frozenset({"docker", "kubernetes"})
_PRIMARY_WORKER_MANAGER_LOCK = threading.Lock()
_PRIMARY_WORKER_MANAGER_CONDITION = threading.Condition(_PRIMARY_WORKER_MANAGER_LOCK)
logger = logging.getLogger(__name__)
_DEFAULT_PRIMARY_WORKER_SHUTDOWN_TIMEOUT_SECONDS = 5.0


@dataclass(slots=True)
class _WorkerManagerEntry:
    manager: WorkerManager
    config_signature: tuple[str, ...]
    active_leases: int = 0
    retired: bool = False


@dataclass(slots=True)
class PrimaryWorkerManagerLease:
    """Request-scoped lease for one active primary worker manager."""

    _entry: _WorkerManagerEntry
    _released: bool = False

    @property
    def manager(self) -> WorkerManager:
        """Return the leased worker manager."""
        return self._entry.manager

    def __enter__(self) -> WorkerManager:
        """Enter the lease context and return the borrowed manager."""
        return self.manager

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        """Release the borrowed manager when the context exits."""
        self.release()
        return False

    def release(self) -> None:
        """Release the borrowed manager once."""
        if self._released:
            return
        self._released = True
        _release_primary_worker_manager_entry(self._entry)


_PRIMARY_WORKER_MANAGER_ENTRY: _WorkerManagerEntry | None = None
_RETIRED_PRIMARY_WORKER_MANAGER_ENTRIES: list[_WorkerManagerEntry] = []


def serialized_kubernetes_worker_validation_snapshot(
    runtime_paths: RuntimePaths,
    *,
    runtime_config: Config | None = None,
) -> dict[str, dict[str, object]]:
    """Build the authoritative worker validation snapshot in the primary runtime."""
    from mindroom.config.main import load_config  # noqa: PLC0415
    from mindroom.tool_system.catalog import (  # noqa: PLC0415
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
    if normalized == "docker":
        return "docker"
    if normalized in {"k8s", "kubernetes"}:
        return "kubernetes"
    msg = f"Unsupported worker backend: {raw_value}"
    raise WorkerBackendError(msg)


def set_primary_worker_storage_path(storage_path: Path | None) -> None:
    """Clear cached dedicated-worker managers when the primary storage root changes."""
    del storage_path
    shutdown_primary_worker_manager(timeout_seconds=0.0)


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
                runtime_paths,
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
    resolved_storage_root = (storage_root or runtime_paths.storage_root).expanduser().resolve()
    if backend_name == "static_runner":
        return _static_runner_backend_config_signature(proxy_url=proxy_url, proxy_token=proxy_token)
    if backend_name == "docker":
        return docker_backend_config_signature(
            runtime_paths,
            auth_token=proxy_token,
            storage_path=resolved_storage_root,
            worker_grantable_credentials=_resolve_worker_grantable_credentials(
                worker_grantable_credentials,
            ),
        )
    if backend_name == "kubernetes":
        backend_signature = kubernetes_backend_config_signature(
            runtime_paths,
            auth_token=proxy_token,
            storage_root=resolved_storage_root,
        )
        resolved_worker_grantable_credentials = _resolve_worker_grantable_credentials(
            worker_grantable_credentials,
        )
        return (
            *backend_signature,
            json.dumps(
                _require_kubernetes_tool_validation_snapshot(kubernetes_tool_validation_snapshot),
                separators=(",", ":"),
                sort_keys=True,
            ),
            "__worker_grantable_credentials__",
            *sorted(resolved_worker_grantable_credentials),
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
            DockerWorkerBackend.from_runtime(
                runtime_paths,
                auth_token=proxy_token,
                storage_path=resolved_storage_root,
                worker_grantable_credentials=_resolve_worker_grantable_credentials(
                    worker_grantable_credentials,
                ),
            ),
        )
    if backend_name == "kubernetes":
        if storage_root is None:
            msg = "Kubernetes worker backend requires an explicit runtime storage root."
            raise WorkerBackendError(msg)
        return WorkerManager(
            KubernetesWorkerBackend.from_runtime(
                runtime_paths,
                auth_token=proxy_token,
                storage_root=resolved_storage_root,
                tool_validation_snapshot=_require_kubernetes_tool_validation_snapshot(
                    kubernetes_tool_validation_snapshot,
                ),
                worker_grantable_credentials=_resolve_worker_grantable_credentials(
                    worker_grantable_credentials,
                ),
            ),
        )
    msg = f"Unsupported worker backend: {backend_name}"
    raise WorkerBackendError(msg)


def _shutdown_worker_manager_now(
    manager: WorkerManager,
    *,
    suppress_errors: bool,
    log_message: str,
) -> str | None:
    try:
        manager.shutdown()
    except WorkerBackendError as exc:
        if suppress_errors:
            logger.exception(log_message)
            return None
        return str(exc)
    return None


def _drain_retired_entries_locked() -> list[WorkerManager]:
    global _RETIRED_PRIMARY_WORKER_MANAGER_ENTRIES

    ready_managers: list[WorkerManager] = []
    pending_entries: list[_WorkerManagerEntry] = []
    for entry in _RETIRED_PRIMARY_WORKER_MANAGER_ENTRIES:
        if entry.active_leases == 0:
            ready_managers.append(entry.manager)
        else:
            pending_entries.append(entry)
    _RETIRED_PRIMARY_WORKER_MANAGER_ENTRIES = pending_entries
    return ready_managers


def _resolve_primary_worker_manager_entry(
    runtime_paths: RuntimePaths,
    *,
    proxy_url: str | None,
    proxy_token: str | None,
    storage_root: Path | None,
    acquire_lease: bool,
    kubernetes_tool_validation_snapshot: dict[str, dict[str, object]] | None = None,
    worker_grantable_credentials: frozenset[str] | None = None,
) -> tuple[_WorkerManagerEntry, list[WorkerManager], WorkerManager | None]:
    global _PRIMARY_WORKER_MANAGER_ENTRY

    config_signature = _primary_worker_backend_config_signature(
        runtime_paths,
        proxy_url=proxy_url,
        proxy_token=proxy_token,
        storage_root=storage_root,
        kubernetes_tool_validation_snapshot=kubernetes_tool_validation_snapshot,
        worker_grantable_credentials=worker_grantable_credentials,
    )
    built_manager: WorkerManager | None = None

    while True:
        with _PRIMARY_WORKER_MANAGER_CONDITION:
            active_entry = _PRIMARY_WORKER_MANAGER_ENTRY
            if active_entry is not None and active_entry.config_signature == config_signature:
                if acquire_lease:
                    active_entry.active_leases += 1
                return active_entry, [], None

        if built_manager is None:
            built_manager = _build_primary_worker_manager(
                runtime_paths,
                proxy_url=proxy_url,
                proxy_token=proxy_token,
                storage_root=storage_root,
                kubernetes_tool_validation_snapshot=kubernetes_tool_validation_snapshot,
                worker_grantable_credentials=worker_grantable_credentials,
            )

        with _PRIMARY_WORKER_MANAGER_CONDITION:
            active_entry = _PRIMARY_WORKER_MANAGER_ENTRY
            if active_entry is not None and active_entry.config_signature == config_signature:
                if acquire_lease:
                    active_entry.active_leases += 1
                discarded_manager = built_manager
                built_manager = None
                return active_entry, [], discarded_manager

            previous_entry = _PRIMARY_WORKER_MANAGER_ENTRY
            new_entry = _WorkerManagerEntry(
                manager=built_manager,
                config_signature=config_signature,
                active_leases=1 if acquire_lease else 0,
            )
            built_manager = None
            _PRIMARY_WORKER_MANAGER_ENTRY = new_entry
            if previous_entry is not None:
                previous_entry.retired = True
                _RETIRED_PRIMARY_WORKER_MANAGER_ENTRIES.append(previous_entry)
            managers_to_shutdown = _drain_retired_entries_locked()
            _PRIMARY_WORKER_MANAGER_CONDITION.notify_all()
            return new_entry, managers_to_shutdown, None


def _release_primary_worker_manager_entry(entry: _WorkerManagerEntry) -> None:
    with _PRIMARY_WORKER_MANAGER_CONDITION:
        if entry.active_leases <= 0:
            return
        entry.active_leases -= 1
        managers_to_shutdown = _drain_retired_entries_locked()
        _PRIMARY_WORKER_MANAGER_CONDITION.notify_all()

    for manager in managers_to_shutdown:
        _shutdown_worker_manager_now(
            manager,
            suppress_errors=True,
            log_message="Failed to shut down retired primary worker manager",
        )


def get_primary_worker_manager(
    runtime_paths: RuntimePaths,
    *,
    proxy_url: str | None,
    proxy_token: str | None,
    storage_root: Path | None = None,
    kubernetes_tool_validation_snapshot: dict[str, dict[str, object]] | None = None,
    worker_grantable_credentials: frozenset[str] | None = None,
) -> WorkerManager:
    """Return the current primary worker manager snapshot for the current backend config."""
    entry, managers_to_shutdown, discarded_manager = _resolve_primary_worker_manager_entry(
        runtime_paths,
        proxy_url=proxy_url,
        proxy_token=proxy_token,
        storage_root=storage_root,
        acquire_lease=False,
        kubernetes_tool_validation_snapshot=kubernetes_tool_validation_snapshot,
        worker_grantable_credentials=worker_grantable_credentials,
    )
    if discarded_manager is not None:
        _shutdown_worker_manager_now(
            discarded_manager,
            suppress_errors=True,
            log_message="Failed to shut down discarded duplicate primary worker manager",
        )
    for manager in managers_to_shutdown:
        _shutdown_worker_manager_now(
            manager,
            suppress_errors=True,
            log_message="Failed to shut down retired primary worker manager",
        )
    return entry.manager


def lease_primary_worker_manager(
    runtime_paths: RuntimePaths,
    *,
    proxy_url: str | None,
    proxy_token: str | None,
    storage_root: Path | None = None,
    kubernetes_tool_validation_snapshot: dict[str, dict[str, object]] | None = None,
    worker_grantable_credentials: frozenset[str] | None = None,
) -> PrimaryWorkerManagerLease:
    """Borrow the active primary worker manager for one request-scoped operation."""
    entry, managers_to_shutdown, discarded_manager = _resolve_primary_worker_manager_entry(
        runtime_paths,
        proxy_url=proxy_url,
        proxy_token=proxy_token,
        storage_root=storage_root,
        acquire_lease=True,
        kubernetes_tool_validation_snapshot=kubernetes_tool_validation_snapshot,
        worker_grantable_credentials=worker_grantable_credentials,
    )
    if discarded_manager is not None:
        _shutdown_worker_manager_now(
            discarded_manager,
            suppress_errors=True,
            log_message="Failed to shut down discarded duplicate primary worker manager",
        )
    for manager in managers_to_shutdown:
        _shutdown_worker_manager_now(
            manager,
            suppress_errors=True,
            log_message="Failed to shut down retired primary worker manager",
        )
    return PrimaryWorkerManagerLease(entry)


def shutdown_primary_worker_manager(
    *,
    timeout_seconds: float = _DEFAULT_PRIMARY_WORKER_SHUTDOWN_TIMEOUT_SECONDS,
) -> None:
    """Drain and shut down the cached primary worker manager from a real shutdown path."""
    global _PRIMARY_WORKER_MANAGER_ENTRY, _RETIRED_PRIMARY_WORKER_MANAGER_ENTRIES

    deadline = time.monotonic() + max(timeout_seconds, 0.0)
    failures: list[str] = []

    while True:
        with _PRIMARY_WORKER_MANAGER_CONDITION:
            active_entry = _PRIMARY_WORKER_MANAGER_ENTRY
            if active_entry is not None:
                active_entry.retired = True
                _RETIRED_PRIMARY_WORKER_MANAGER_ENTRIES.append(active_entry)
                _PRIMARY_WORKER_MANAGER_ENTRY = None

            managers_to_shutdown = _drain_retired_entries_locked()
            if managers_to_shutdown:
                pass
            elif not _RETIRED_PRIMARY_WORKER_MANAGER_ENTRIES:
                break
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning(
                        "Skipping shutdown of %s leased primary worker managers",
                        len(_RETIRED_PRIMARY_WORKER_MANAGER_ENTRIES),
                    )
                    _RETIRED_PRIMARY_WORKER_MANAGER_ENTRIES = []
                    break
                _PRIMARY_WORKER_MANAGER_CONDITION.wait(timeout=remaining)
                continue

        failures.extend(
            failure
            for manager in managers_to_shutdown
            if (
                failure := _shutdown_worker_manager_now(
                    manager,
                    suppress_errors=False,
                    log_message="Failed to shut down primary worker manager",
                )
            )
            is not None
        )

    if failures:
        failure_text = "; ".join(failures)
        msg = f"Failed to shut down primary worker managers: {failure_text}"
        raise WorkerBackendError(msg)


def _reset_primary_worker_manager() -> None:
    """Reset the cached primary worker manager. Intended for tests."""
    shutdown_primary_worker_manager(timeout_seconds=0.0)
