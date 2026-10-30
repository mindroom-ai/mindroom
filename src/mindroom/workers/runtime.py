"""Primary-runtime worker backend selection and caching."""

from __future__ import annotations

import functools
import json
import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Never, cast

from mindroom.config.worker_projection import worker_config_data
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.backend_kinds import WorkerBackendKind, worker_backend_kind
from mindroom.workers.backends.docker import configure_docker_worker_backend
from mindroom.workers.backends.docker_config import DockerWorkerBackendConfig
from mindroom.workers.backends.kubernetes import configure_kubernetes_worker_backend
from mindroom.workers.backends.kubernetes_config import KubernetesWorkerBackendConfig
from mindroom.workers.backends.static_runner import StaticSandboxRunnerBackend
from mindroom.workers.models import ConfiguredWorkerBackend, WorkerLaunchConfig, normalize_sandbox_runner_api_root

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.workers.backend import WorkerBackend

__all__ = [
    "PrimaryWorkerManagerLease",
    "bind_worker_launch_config_source",
    "build_worker_launch_config",
    "configured_primary_worker_manager_identity",
    "current_worker_launch_config",
    "lease_configured_primary_worker_manager",
    "lease_primary_worker_manager",
    "reset_primary_worker_manager",
    "reset_worker_launch_config",
    "serialized_dedicated_worker_validation_snapshot",
    "shutdown_primary_worker_manager",
    "worker_config_snapshot",
    "worker_visible_storage_root",
]

_PRIMARY_WORKER_MANAGER_LOCK = threading.Lock()
_PRIMARY_WORKER_MANAGER_CONDITION = threading.Condition(_PRIMARY_WORKER_MANAGER_LOCK)
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


def reset_worker_launch_config() -> None:
    """Forget the launch dedicated workers derived, so the next one reads the published plugins."""
    with _WORKER_LAUNCH_CONFIG_LOCK:
        _WORKER_LAUNCH_CONFIG_SOURCE.launch = None
        _WORKER_LAUNCH_CONFIG_SOURCE.epoch += 1


def serialized_dedicated_worker_validation_snapshot(
    runtime_paths: RuntimePaths,
    *,
    runtime_config: Config,
) -> dict[str, dict[str, object]]:
    """Build the authoritative worker validation snapshot for one applied config in the primary runtime."""
    # Deferred: the plugin loader and catalog pull the runtime tool registry, which this module must not import.
    from mindroom.tool_system.catalog import serialize_tool_validation_snapshot  # noqa: PLC0415
    from mindroom.tool_system.plugins import config_plugins  # noqa: PLC0415

    return serialize_tool_validation_snapshot(config_plugins(runtime_config, runtime_paths).tools.validation_snapshot)


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


def worker_visible_storage_root(runtime_paths: RuntimePaths) -> Path | None:
    """Return where dedicated workers mount the shared storage root, or ``None`` without dedicated workers."""
    match worker_backend_kind(runtime_paths):
        case WorkerBackendKind.STATIC_RUNNER:
            return None
        case WorkerBackendKind.DOCKER:
            return Path(DockerWorkerBackendConfig.from_runtime(runtime_paths).storage_mount_path)
        case WorkerBackendKind.KUBERNETES:
            return Path(KubernetesWorkerBackendConfig.from_runtime(runtime_paths).storage_mount_path)


def lease_configured_primary_worker_manager(
    runtime_paths: RuntimePaths,
    *,
    required_backend_locator: str | None = None,
) -> PrimaryWorkerManagerLease | None:
    """Lease the current worker manager when it owns the requested durable identity."""
    configured = _configured_primary_worker_backend(runtime_paths)
    if configured is None:
        return None
    lease = _lease_primary_worker_manager(runtime_paths, configured)
    if required_backend_locator is None or lease.manager.cleanup_locator == required_backend_locator:
        return lease
    lease.release()
    return None


def configured_primary_worker_manager_identity(runtime_paths: RuntimePaths) -> str | None:
    """Return the opaque identity of the currently configured primary worker manager."""
    configured = _configured_primary_worker_backend(runtime_paths)
    return None if configured is None else _stable_json_digest(configured.signature)


def _configured_primary_worker_backend(runtime_paths: RuntimePaths) -> ConfiguredWorkerBackend | None:
    """Read the backend settings the primary manager is built from, or return None when they cannot route tool calls.

    A config never enters them.
    """
    from mindroom.tool_system.sandbox_proxy import sandbox_proxy_config  # noqa: PLC0415

    proxy_config = sandbox_proxy_config(runtime_paths)
    if not (proxy_config.proxy_token if worker_backend_kind(runtime_paths).dedicated else proxy_config.proxy_url):
        return None
    try:
        return _configure_primary_worker_backend(
            runtime_paths,
            proxy_url=proxy_config.proxy_url,
            proxy_token=proxy_config.proxy_token,
        )
    except WorkerBackendError:
        return None


def _configure_primary_worker_backend(
    runtime_paths: RuntimePaths,
    *,
    proxy_url: str | None,
    proxy_token: str | None,
) -> ConfiguredWorkerBackend:
    """Read the selected worker backend's settings once, raising when they are invalid."""
    storage_root = runtime_paths.storage_root
    match worker_backend_kind(runtime_paths):
        case WorkerBackendKind.STATIC_RUNNER:
            return _configure_static_runner(proxy_url=proxy_url, proxy_token=proxy_token)
        case WorkerBackendKind.DOCKER:
            return configure_docker_worker_backend(runtime_paths, auth_token=proxy_token, storage_root=storage_root)
        case WorkerBackendKind.KUBERNETES:
            return configure_kubernetes_worker_backend(runtime_paths, auth_token=proxy_token, storage_root=storage_root)


def _configure_static_runner(*, proxy_url: str | None, proxy_token: str | None) -> ConfiguredWorkerBackend:
    """Read the shared sandbox runner's settings; it owns no workers of its own, so it is cleaned up as itself."""
    api_root = normalize_sandbox_runner_api_root(proxy_url or "")
    signature = (WorkerBackendKind.STATIC_RUNNER.value, api_root, proxy_token or "")
    return ConfiguredWorkerBackend(
        signature=signature,
        cleanup_signature=signature,
        build=lambda _current_launch: StaticSandboxRunnerBackend(api_root=api_root, auth_token=proxy_token),
    )


def _shutdown_worker_manager_now(manager: WorkerBackend) -> str | None:
    """Shut down one manager, returning its failure message."""
    try:
        manager.shutdown()
    except WorkerBackendError as exc:
        return str(exc)
    return None


def _shutdown_retired_managers(managers: list[WorkerBackend]) -> None:
    """Shut down managers no lease holds anymore, logging each failure instead of raising it."""
    for manager in managers:
        try:
            manager.shutdown()
        except WorkerBackendError:
            logger.exception("Failed to shut down retired primary worker manager")


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


def _primary_worker_manager_epoch_current_locked(acquisition_epoch: int) -> bool:
    return acquisition_epoch == _PRIMARY_WORKER_MANAGER_EPOCH and _PRIMARY_WORKER_MANAGER_ACCEPTING_BUILDS


def _require_primary_worker_manager_epoch_locked(acquisition_epoch: int) -> None:
    if not _primary_worker_manager_epoch_current_locked(acquisition_epoch):
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
        if not _primary_worker_manager_epoch_current_locked(acquisition_epoch):
            return None
        _PRIMARY_WORKER_MANAGER_BUILDING_SIGNATURES.discard(config_signature)
        active_entry = _PRIMARY_WORKER_MANAGER_ENTRY
        if active_entry is not None and active_entry.config_signature == config_signature:
            # Another build of the same settings won; lease it and retire this duplicate.
            active_entry.active_leases += 1
            leased_entry = active_entry
            _RETIRED_PRIMARY_WORKER_MANAGER_ENTRIES.append(
                _WorkerManagerEntry(manager=built_manager, config_signature=config_signature, shutdown_requested=True),
            )
        else:
            leased_entry = _WorkerManagerEntry(
                manager=built_manager,
                config_signature=config_signature,
                active_leases=1,
            )
            _PRIMARY_WORKER_MANAGER_ENTRY = leased_entry
            if active_entry is not None:
                active_entry.shutdown_requested = True
                _RETIRED_PRIMARY_WORKER_MANAGER_ENTRIES.append(active_entry)
        managers_to_shutdown = _drain_retired_entries_locked()
        _PRIMARY_WORKER_MANAGER_CONDITION.notify_all()
        return leased_entry, managers_to_shutdown


def _dispose_fenced_primary_worker_manager(
    built_manager: WorkerBackend,
    config_signature: tuple[str, ...],
) -> Never:
    try:
        shutdown_failure = _shutdown_worker_manager_now(built_manager)
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
    configured: ConfiguredWorkerBackend,
) -> tuple[_WorkerManagerEntry, list[WorkerBackend]]:
    acquisition_epoch = _capture_primary_worker_manager_epoch()
    config_signature = configured.signature
    active_entry = _reserve_primary_worker_manager_build(config_signature, acquisition_epoch=acquisition_epoch)
    if active_entry is not None:
        return active_entry, []

    try:
        built_manager = configured.build(functools.partial(current_worker_launch_config, runtime_paths))
        built_manager.cleanup_locator = _stable_json_digest(configured.cleanup_signature)
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

    _shutdown_retired_managers(managers_to_shutdown)


def lease_primary_worker_manager(
    runtime_paths: RuntimePaths,
    *,
    proxy_url: str | None,
    proxy_token: str | None,
) -> PrimaryWorkerManagerLease:
    """Borrow the active primary worker manager for one request-scoped operation."""
    return _lease_primary_worker_manager(
        runtime_paths,
        _configure_primary_worker_backend(runtime_paths, proxy_url=proxy_url, proxy_token=proxy_token),
    )


def _lease_primary_worker_manager(
    runtime_paths: RuntimePaths,
    configured: ConfiguredWorkerBackend,
) -> PrimaryWorkerManagerLease:
    """Borrow the manager ``configured`` identifies, building it from that same reading when none is active."""
    entry, managers_to_shutdown = _resolve_primary_worker_manager_entry(runtime_paths, configured)
    _shutdown_retired_managers(managers_to_shutdown)
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
            if (failure := _shutdown_worker_manager_now(manager)) is not None
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
