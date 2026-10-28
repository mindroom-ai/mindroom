"""Primary-runtime worker backend selection and caching."""

from __future__ import annotations

import functools
import json
import logging
import threading
import time
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Never, cast

from mindroom.config.worker_projection import worker_config_data
from mindroom.runtime_env_policy import KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.backends.docker import DockerWorkerBackend
from mindroom.workers.backends.docker_config import (
    DockerWorkerBackendConfig,
    docker_backend_cleanup_signature,
    docker_backend_config_signature,
)
from mindroom.workers.backends.kubernetes import KubernetesWorkerBackend
from mindroom.workers.backends.kubernetes_config import (
    KubernetesWorkerBackendConfig,
    kubernetes_backend_cleanup_signature,
    kubernetes_backend_config_signature,
)
from mindroom.workers.backends.static_runner import StaticSandboxRunnerBackend, normalize_static_runner_api_root
from mindroom.workers.models import WorkerLaunchConfig

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.workers.backend import WorkerBackend

__all__ = [
    "PrimaryWorkerManagerLease",
    "bind_worker_launch_config_source",
    "build_worker_launch_config",
    "clear_worker_validation_snapshot_cache",
    "configured_primary_worker_manager_identity",
    "current_worker_launch_config",
    "lease_configured_primary_worker_manager",
    "lease_primary_worker_manager",
    "primary_worker_backend_available",
    "primary_worker_backend_is_dedicated",
    "primary_worker_backend_name",
    "reset_primary_worker_manager",
    "serialized_dedicated_worker_validation_snapshot",
    "shutdown_primary_worker_manager",
    "worker_config_snapshot",
    "worker_visible_storage_root",
]

_PRIMARY_WORKER_BACKEND_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["worker_backend"]
_DEDICATED_WORKER_BACKENDS = frozenset({"docker", "kubernetes"})
_PRIMARY_WORKER_MANAGER_LOCK = threading.Lock()
_PRIMARY_WORKER_MANAGER_CONDITION = threading.Condition(_PRIMARY_WORKER_MANAGER_LOCK)
_WORKER_VALIDATION_SNAPSHOT_CACHE: dict[tuple[str, ...], dict[str, dict[str, object]]] = {}
_WORKER_VALIDATION_SNAPSHOT_CACHE_LOCK = threading.Lock()
logger = logging.getLogger(__name__)
_DEFAULT_PRIMARY_WORKER_SHUTDOWN_TIMEOUT_SECONDS = 5.0


@dataclass(slots=True)
class _WorkerManagerEntry:
    manager: WorkerBackend
    config_signature: tuple[str, ...]
    active_leases: int = 0
    shutdown_requested: bool = False


@dataclass(slots=True)
class PrimaryWorkerManagerLease:
    """Request-scoped lease for one active primary worker manager."""

    _entry: _WorkerManagerEntry
    _released: bool = False

    @property
    def manager(self) -> WorkerBackend:
        """Return the leased worker manager."""
        return self._entry.manager

    def __enter__(self) -> WorkerBackend:
        """Enter the lease context and return the borrowed manager."""
        return self.manager

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> bool:
        """Release the borrowed manager when the context exits."""
        self.release()
        return False

    def release(self) -> None:
        """Release the borrowed manager once."""
        if self._released:
            return
        self._released = True
        _release_primary_worker_manager_entry(self._entry)


@dataclass(frozen=True, slots=True)
class _ConfiguredPrimaryWorkerManagerInputs:
    """The backend settings that identify the primary worker manager; config never enters them."""

    proxy_url: str | None
    proxy_token: str | None
    storage_root: Path


@dataclass(slots=True)
class _WorkerLaunchConfigSource:
    """The one source of the applied config that dedicated workers launch from, and its last derived launch."""

    read_config: Callable[[], Config | None] | None = None
    launch: WorkerLaunchConfig | None = None
    launch_runtime_paths: RuntimePaths | None = None
    # Raised by every invalidation, so a launch derived before it is never stored after it.
    epoch: int = 0


_PRIMARY_WORKER_MANAGER_ENTRY: _WorkerManagerEntry | None = None
_RETIRED_PRIMARY_WORKER_MANAGER_ENTRIES: list[_WorkerManagerEntry] = []
_PRIMARY_WORKER_MANAGER_BUILDING_SIGNATURES: set[tuple[str, ...]] = set()
_PRIMARY_WORKER_MANAGER_EPOCH = 0
_PRIMARY_WORKER_MANAGER_ACCEPTING_BUILDS = True
_WORKER_LAUNCH_CONFIG_SOURCE = _WorkerLaunchConfigSource()
_WORKER_LAUNCH_CONFIG_LOCK = threading.Lock()


def _stable_json_digest(payload: object) -> str:
    """Return a stable opaque identity for JSON-like config payloads."""
    serialized = json.dumps(payload, default=repr, separators=(",", ":"), sort_keys=True)
    return sha256(serialized.encode("utf-8")).hexdigest()


def _worker_validation_snapshot_cache_key(
    runtime_paths: RuntimePaths,
    runtime_config: Config,
) -> tuple[str, ...]:
    """Return the cheap explicit inputs that affect worker validation metadata."""
    plugins_identity = [plugin_entry.model_dump(mode="json") for plugin_entry in runtime_config.plugins]
    mcp_identity = {
        server_id: server_config.model_dump(mode="json")
        for server_id, server_config in runtime_config.mcp_servers.items()
    }
    return (
        str(runtime_paths.config_path),
        str(runtime_paths.config_dir),
        str(runtime_paths.storage_root),
        _stable_json_digest(plugins_identity),
        _stable_json_digest(mcp_identity),
    )


def clear_worker_validation_snapshot_cache() -> None:
    """Clear cached worker validation snapshots, the launch derived from them, and per-config resolved tool state."""
    from mindroom.tool_system.catalog import clear_resolved_tool_state_cache  # noqa: PLC0415

    with _WORKER_VALIDATION_SNAPSHOT_CACHE_LOCK:
        _WORKER_VALIDATION_SNAPSHOT_CACHE.clear()
    with _WORKER_LAUNCH_CONFIG_LOCK:
        _WORKER_LAUNCH_CONFIG_SOURCE.launch = None
        _WORKER_LAUNCH_CONFIG_SOURCE.epoch += 1
    clear_resolved_tool_state_cache()


def serialized_dedicated_worker_validation_snapshot(
    runtime_paths: RuntimePaths,
    *,
    runtime_config: Config,
) -> dict[str, dict[str, object]]:
    """Build the authoritative worker validation snapshot for one applied config in the primary runtime."""
    with _WORKER_VALIDATION_SNAPSHOT_CACHE_LOCK:
        cache_key = _worker_validation_snapshot_cache_key(runtime_paths, runtime_config)
        cached_snapshot = _WORKER_VALIDATION_SNAPSHOT_CACHE.get(cache_key)
        if cached_snapshot is None:
            from mindroom.tool_system.catalog import (  # noqa: PLC0415
                resolved_tool_validation_snapshot_for_runtime,
                serialize_tool_validation_snapshot,
            )

            snapshot = resolved_tool_validation_snapshot_for_runtime(
                runtime_paths,
                runtime_config,
                tolerate_plugin_load_errors=True,
            )
            cached_snapshot = serialize_tool_validation_snapshot(snapshot)
            _WORKER_VALIDATION_SNAPSHOT_CACHE[cache_key] = cached_snapshot
        return deepcopy(cached_snapshot)


def worker_config_snapshot(runtime_config: Config) -> dict[str, object]:
    """Return the JSON form of the config fields runners and dedicated workers resolve, and nothing else."""
    from mindroom.tool_system.worker_proxy_client import to_json_compatible  # noqa: PLC0415

    return cast("dict[str, object]", to_json_compatible(worker_config_data(runtime_config.runtime_model_dump())))


@contextmanager
def bind_worker_launch_config_source(read_config: Callable[[], Config | None]) -> Iterator[None]:
    """Make ``read_config`` this process's one source of the applied config dedicated workers launch from.

    The process that owns the applied config binds it for its whole lifetime; a second source is refused,
    so no other config can reach a worker launch.
    """
    with _WORKER_LAUNCH_CONFIG_LOCK:
        if _WORKER_LAUNCH_CONFIG_SOURCE.read_config is not None:
            msg = "A worker launch config source is already bound in this process."
            raise RuntimeError(msg)
        _WORKER_LAUNCH_CONFIG_SOURCE.read_config = read_config
    try:
        yield
    finally:
        with _WORKER_LAUNCH_CONFIG_LOCK:
            _WORKER_LAUNCH_CONFIG_SOURCE.read_config = None
            _WORKER_LAUNCH_CONFIG_SOURCE.launch = None
            _WORKER_LAUNCH_CONFIG_SOURCE.epoch += 1


def current_worker_launch_config(runtime_paths: RuntimePaths) -> WorkerLaunchConfig:
    """Return what dedicated workers launch with now, derived once per applied config."""
    with _WORKER_LAUNCH_CONFIG_LOCK:
        read_config = _WORKER_LAUNCH_CONFIG_SOURCE.read_config
    config = read_config() if read_config is not None else None
    if config is None:
        msg = "Dedicated workers need the applied config, and none is available yet."
        raise WorkerBackendError(msg)
    with _WORKER_LAUNCH_CONFIG_LOCK:
        cached = _WORKER_LAUNCH_CONFIG_SOURCE.launch
        if (
            cached is not None
            and cached.config is config
            and _WORKER_LAUNCH_CONFIG_SOURCE.launch_runtime_paths == runtime_paths
        ):
            return cached
        epoch = _WORKER_LAUNCH_CONFIG_SOURCE.epoch
    launch = build_worker_launch_config(
        config,
        tool_validation_snapshot=serialized_dedicated_worker_validation_snapshot(runtime_paths, runtime_config=config),
    )
    with _WORKER_LAUNCH_CONFIG_LOCK:
        if _WORKER_LAUNCH_CONFIG_SOURCE.epoch == epoch:
            _WORKER_LAUNCH_CONFIG_SOURCE.launch = launch
            _WORKER_LAUNCH_CONFIG_SOURCE.launch_runtime_paths = runtime_paths
    return launch


def build_worker_launch_config(
    config: Config,
    *,
    tool_validation_snapshot: dict[str, dict[str, object]],
) -> WorkerLaunchConfig:
    """Derive one launch config from a validated config and the tool validation snapshot built for it."""
    config_data = worker_config_snapshot(config)
    tool_snapshot_digest = _stable_json_digest(tool_validation_snapshot)
    return WorkerLaunchConfig(
        config=config,
        config_data=config_data,
        agent_policies=config.get_agent_policies(),
        tool_validation_snapshot=tool_validation_snapshot,
        grantable_credentials=config.get_worker_grantable_credentials(),
        digest=_stable_json_digest({"config": config_data, "tools": tool_snapshot_digest}),
    )


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


def worker_visible_storage_root(runtime_paths: RuntimePaths) -> Path | None:
    """Return where dedicated workers mount the shared storage root, or ``None`` without dedicated workers."""
    backend_name = primary_worker_backend_name(runtime_paths)
    if backend_name == "kubernetes":
        return Path(KubernetesWorkerBackendConfig.from_runtime(runtime_paths).storage_mount_path)
    if backend_name == "docker":
        return Path(DockerWorkerBackendConfig.from_runtime(runtime_paths).storage_mount_path)
    return None


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


def lease_configured_primary_worker_manager(
    runtime_paths: RuntimePaths,
    *,
    required_backend_locator: str | None = None,
) -> PrimaryWorkerManagerLease | None:
    """Lease the current worker manager when it owns the requested durable identity."""
    inputs = _configured_primary_worker_manager_inputs(runtime_paths)
    if inputs is None:
        return None
    lease = lease_primary_worker_manager(
        runtime_paths,
        proxy_url=inputs.proxy_url,
        proxy_token=inputs.proxy_token,
        storage_root=inputs.storage_root,
    )
    if required_backend_locator is None or lease.manager.cleanup_locator == required_backend_locator:
        return lease
    lease.release()
    return None


def configured_primary_worker_manager_identity(runtime_paths: RuntimePaths) -> str | None:
    """Return the opaque identity of the currently configured primary worker manager."""
    inputs = _configured_primary_worker_manager_inputs(runtime_paths)
    if inputs is None:
        return None
    signature = _primary_worker_backend_config_signature(
        runtime_paths,
        proxy_url=inputs.proxy_url,
        proxy_token=inputs.proxy_token,
        storage_root=inputs.storage_root,
    )
    return _stable_json_digest(signature)


def _configured_primary_worker_manager_inputs(
    runtime_paths: RuntimePaths,
) -> _ConfiguredPrimaryWorkerManagerInputs | None:
    """Resolve the backend settings the primary manager is built from."""
    from mindroom.tool_system.sandbox_proxy import sandbox_proxy_config  # noqa: PLC0415

    proxy_config = sandbox_proxy_config(runtime_paths)
    if not primary_worker_backend_available(
        runtime_paths,
        proxy_url=proxy_config.proxy_url,
        proxy_token=proxy_config.proxy_token,
    ):
        return None
    return _ConfiguredPrimaryWorkerManagerInputs(
        proxy_url=proxy_config.proxy_url,
        proxy_token=proxy_config.proxy_token,
        storage_root=runtime_paths.storage_root,
    )


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
            runtime_paths,
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


def _primary_worker_backend_cleanup_signature(
    runtime_paths: RuntimePaths,
    *,
    storage_root: Path | None,
    config_signature: tuple[str, ...],
) -> tuple[str, ...]:
    """Return the stable resource-owner identity used for durable script cleanup."""
    if primary_worker_backend_name(runtime_paths) == "docker":
        return docker_backend_cleanup_signature(
            runtime_paths,
            storage_path=(storage_root or runtime_paths.storage_root).expanduser().resolve(),
        )
    if primary_worker_backend_name(runtime_paths) == "kubernetes":
        return kubernetes_backend_cleanup_signature(
            runtime_paths,
            storage_root=(storage_root or runtime_paths.storage_root).expanduser().resolve(),
        )
    return config_signature


def _build_primary_worker_manager(
    runtime_paths: RuntimePaths,
    *,
    proxy_url: str | None,
    proxy_token: str | None,
    storage_root: Path | None,
) -> WorkerBackend:
    backend_name = primary_worker_backend_name(runtime_paths)
    resolved_storage_root = (storage_root or runtime_paths.storage_root).expanduser().resolve()
    if backend_name == "static_runner":
        return StaticSandboxRunnerBackend(
            api_root=normalize_static_runner_api_root(proxy_url or ""),
            auth_token=proxy_token,
        )
    if backend_name == "docker":
        return cast(
            "WorkerBackend",
            DockerWorkerBackend.from_runtime(
                runtime_paths,
                auth_token=proxy_token,
                current_launch=functools.partial(current_worker_launch_config, runtime_paths),
                storage_path=resolved_storage_root,
            ),
        )
    if backend_name == "kubernetes":
        return KubernetesWorkerBackend.from_runtime(
            runtime_paths,
            auth_token=proxy_token,
            storage_root=resolved_storage_root,
            current_launch=functools.partial(current_worker_launch_config, runtime_paths),
        )
    msg = f"Unsupported worker backend: {backend_name}"
    raise WorkerBackendError(msg)


def _shutdown_worker_manager_now(
    manager: WorkerBackend,
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


def _drain_retired_entries_locked() -> list[WorkerBackend]:
    global _RETIRED_PRIMARY_WORKER_MANAGER_ENTRIES

    ready_managers: list[WorkerBackend] = []
    pending_entries: list[_WorkerManagerEntry] = []
    for entry in _RETIRED_PRIMARY_WORKER_MANAGER_ENTRIES:
        if entry.shutdown_requested and entry.active_leases == 0:
            ready_managers.append(entry.manager)
        else:
            pending_entries.append(entry)
    _RETIRED_PRIMARY_WORKER_MANAGER_ENTRIES = pending_entries
    return ready_managers


def _require_primary_worker_manager_epoch_locked(acquisition_epoch: int) -> None:
    if acquisition_epoch != _PRIMARY_WORKER_MANAGER_EPOCH or not _PRIMARY_WORKER_MANAGER_ACCEPTING_BUILDS:
        msg = "Primary worker manager runtime shut down during acquisition."
        raise WorkerBackendError(msg)


def _capture_primary_worker_manager_epoch() -> int:
    with _PRIMARY_WORKER_MANAGER_CONDITION:
        if not _PRIMARY_WORKER_MANAGER_ACCEPTING_BUILDS:
            msg = "Primary worker manager runtime has shut down."
            raise WorkerBackendError(msg)
        return _PRIMARY_WORKER_MANAGER_EPOCH


def _reserve_primary_worker_manager_build(
    config_signature: tuple[str, ...],
    *,
    acquisition_epoch: int,
) -> _WorkerManagerEntry | None:
    with _PRIMARY_WORKER_MANAGER_CONDITION:
        while True:
            _require_primary_worker_manager_epoch_locked(acquisition_epoch)
            active_entry = _PRIMARY_WORKER_MANAGER_ENTRY
            if active_entry is not None and active_entry.config_signature == config_signature:
                active_entry.active_leases += 1
                return active_entry
            if config_signature not in _PRIMARY_WORKER_MANAGER_BUILDING_SIGNATURES:
                _PRIMARY_WORKER_MANAGER_BUILDING_SIGNATURES.add(config_signature)
                return None
            _PRIMARY_WORKER_MANAGER_CONDITION.wait()


def _publish_primary_worker_manager_build(
    built_manager: WorkerBackend,
    config_signature: tuple[str, ...],
    *,
    acquisition_epoch: int,
) -> tuple[_WorkerManagerEntry, list[WorkerBackend]] | None:
    global _PRIMARY_WORKER_MANAGER_ENTRY

    with _PRIMARY_WORKER_MANAGER_CONDITION:
        if acquisition_epoch != _PRIMARY_WORKER_MANAGER_EPOCH or not _PRIMARY_WORKER_MANAGER_ACCEPTING_BUILDS:
            return None
        _PRIMARY_WORKER_MANAGER_BUILDING_SIGNATURES.discard(config_signature)
        active_entry = _PRIMARY_WORKER_MANAGER_ENTRY
        if active_entry is not None and active_entry.config_signature == config_signature:
            active_entry.active_leases += 1
            duplicate_entry = _WorkerManagerEntry(
                manager=built_manager,
                config_signature=config_signature,
                shutdown_requested=True,
            )
            _RETIRED_PRIMARY_WORKER_MANAGER_ENTRIES.append(duplicate_entry)
            managers_to_shutdown = _drain_retired_entries_locked()
            _PRIMARY_WORKER_MANAGER_CONDITION.notify_all()
            return active_entry, managers_to_shutdown
        previous_entry = _PRIMARY_WORKER_MANAGER_ENTRY
        new_entry = _WorkerManagerEntry(
            manager=built_manager,
            config_signature=config_signature,
            active_leases=1,
        )
        _PRIMARY_WORKER_MANAGER_ENTRY = new_entry
        if previous_entry is not None:
            previous_entry.shutdown_requested = True
            _RETIRED_PRIMARY_WORKER_MANAGER_ENTRIES.append(previous_entry)
        managers_to_shutdown = _drain_retired_entries_locked()
        _PRIMARY_WORKER_MANAGER_CONDITION.notify_all()
        return new_entry, managers_to_shutdown


def _dispose_fenced_primary_worker_manager(
    built_manager: WorkerBackend,
    config_signature: tuple[str, ...],
) -> Never:
    try:
        shutdown_failure = _shutdown_worker_manager_now(
            built_manager,
            suppress_errors=False,
            log_message="Failed to shut down fenced primary worker manager",
        )
    finally:
        with _PRIMARY_WORKER_MANAGER_CONDITION:
            _PRIMARY_WORKER_MANAGER_BUILDING_SIGNATURES.discard(config_signature)
            _PRIMARY_WORKER_MANAGER_CONDITION.notify_all()
    message = "Primary worker manager runtime shut down during construction."
    if shutdown_failure is not None:
        message = f"{message} Manager disposal failed: {shutdown_failure}"
    raise WorkerBackendError(message)


def _resolve_primary_worker_manager_entry(
    runtime_paths: RuntimePaths,
    *,
    proxy_url: str | None,
    proxy_token: str | None,
    storage_root: Path | None,
) -> tuple[_WorkerManagerEntry, list[WorkerBackend]]:
    acquisition_epoch = _capture_primary_worker_manager_epoch()
    config_signature = _primary_worker_backend_config_signature(
        runtime_paths,
        proxy_url=proxy_url,
        proxy_token=proxy_token,
        storage_root=storage_root,
    )
    cleanup_signature = _primary_worker_backend_cleanup_signature(
        runtime_paths,
        storage_root=storage_root,
        config_signature=config_signature,
    )
    active_entry = _reserve_primary_worker_manager_build(config_signature, acquisition_epoch=acquisition_epoch)
    if active_entry is not None:
        return active_entry, []

    try:
        built_manager = _build_primary_worker_manager(
            runtime_paths,
            proxy_url=proxy_url,
            proxy_token=proxy_token,
            storage_root=storage_root,
        )
        built_manager.cleanup_locator = _stable_json_digest(cleanup_signature)
    except Exception:
        with _PRIMARY_WORKER_MANAGER_CONDITION:
            _PRIMARY_WORKER_MANAGER_BUILDING_SIGNATURES.discard(config_signature)
            _PRIMARY_WORKER_MANAGER_CONDITION.notify_all()
        raise

    published = _publish_primary_worker_manager_build(
        built_manager,
        config_signature,
        acquisition_epoch=acquisition_epoch,
    )
    if published is not None:
        return published
    _dispose_fenced_primary_worker_manager(built_manager, config_signature)


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


def lease_primary_worker_manager(
    runtime_paths: RuntimePaths,
    *,
    proxy_url: str | None,
    proxy_token: str | None,
    storage_root: Path | None = None,
) -> PrimaryWorkerManagerLease:
    """Borrow the active primary worker manager for one request-scoped operation."""
    entry, managers_to_shutdown = _resolve_primary_worker_manager_entry(
        runtime_paths,
        proxy_url=proxy_url,
        proxy_token=proxy_token,
        storage_root=storage_root,
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
    global _PRIMARY_WORKER_MANAGER_ACCEPTING_BUILDS
    global _PRIMARY_WORKER_MANAGER_ENTRY
    global _PRIMARY_WORKER_MANAGER_EPOCH

    deadline = time.monotonic() + max(timeout_seconds, 0.0)
    failures: list[str] = []

    with _PRIMARY_WORKER_MANAGER_CONDITION:
        _PRIMARY_WORKER_MANAGER_EPOCH += 1
        _PRIMARY_WORKER_MANAGER_ACCEPTING_BUILDS = False
        _PRIMARY_WORKER_MANAGER_CONDITION.notify_all()

    while True:
        with _PRIMARY_WORKER_MANAGER_CONDITION:
            active_entry = _PRIMARY_WORKER_MANAGER_ENTRY
            if active_entry is not None:
                _RETIRED_PRIMARY_WORKER_MANAGER_ENTRIES.append(active_entry)
                _PRIMARY_WORKER_MANAGER_ENTRY = None

            for entry in _RETIRED_PRIMARY_WORKER_MANAGER_ENTRIES:
                entry.shutdown_requested = True

            managers_to_shutdown = _drain_retired_entries_locked()
            if not managers_to_shutdown:
                if not _RETIRED_PRIMARY_WORKER_MANAGER_ENTRIES:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning(
                        "Skipping shutdown of %s leased primary worker managers",
                        len(_RETIRED_PRIMARY_WORKER_MANAGER_ENTRIES),
                    )
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


def reset_primary_worker_manager() -> None:
    """Drain stale managers and establish a fresh in-process startup epoch."""
    global _PRIMARY_WORKER_MANAGER_ACCEPTING_BUILDS
    global _PRIMARY_WORKER_MANAGER_EPOCH

    shutdown_primary_worker_manager(timeout_seconds=0.0)
    with _PRIMARY_WORKER_MANAGER_CONDITION:
        _PRIMARY_WORKER_MANAGER_EPOCH += 1
        _PRIMARY_WORKER_MANAGER_ACCEPTING_BUILDS = True
        _PRIMARY_WORKER_MANAGER_CONDITION.notify_all()


def _reset_primary_worker_manager() -> None:
    """Reset the cached primary worker manager. Intended for tests."""
    reset_primary_worker_manager()
