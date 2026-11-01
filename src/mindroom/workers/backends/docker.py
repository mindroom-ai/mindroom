"""Docker-backed worker backend for dedicated local worker containers."""

from __future__ import annotations

import hashlib
import hmac
import importlib
import math
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Protocol, cast

import httpx

from mindroom.constants import (
    RuntimePaths,
    runtime_env_values,
    runtime_paths_with_storage_root,
    sandbox_startup_manifest_path,
    secret_path_env_names,
)
from mindroom.credentials.manager import (
    get_runtime_credentials_manager,
    sync_shared_credentials_to_worker,
)
from mindroom.logging_config import get_logger
from mindroom.path_confinement import open_directory_within_root
from mindroom.redaction import redact_sensitive_text
from mindroom.runtime_env_policy import SANDBOX_RUNTIME_ENV_BY_KEY
from mindroom.tool_system.dependencies import ensure_optional_deps, optional_dependency_environment
from mindroom.tool_system.worker_routing import (
    WORKER_SHARED_CREDENTIALS_DIRNAME,
    resolved_worker_key_scope,
    worker_dir_name,
    worker_key_agent_name,
    worker_log_id,
)
from mindroom.workers.backend import (
    WorkerBackendError,
    filter_and_sort_worker_handles,
    mirrored_credential_services,
)
from mindroom.workers.backend_kinds import WorkerBackendKind
from mindroom.workers.backends._dedicated_worker_common import (
    plan_scoped_workspace_mounts,
    plan_shared_directory_mounts,
    resolve_state_scope_worker_key,
    stable_json_digest,
    validate_dedicated_worker_extra_env,
    validate_unique_worker_visible_paths,
)
from mindroom.workers.backends._lifecycle import (
    WorkerLifecycleState,
    WorkerLocks,
    initial_worker_lifecycle_state,
    mark_worker_failed,
    mark_worker_idle,
    mark_worker_ready,
    prepare_worker_ensure_lifecycle,
    touch_worker_lifecycle,
    worker_handle,
)
from mindroom.workers.backends._metadata_store import (
    list_worker_state_paths,
    load_worker_metadata,
    save_worker_metadata,
)
from mindroom.workers.backends.dedicated_launch import (
    DedicatedWorkerLayout,
    dedicated_worker_runner_env,
    dedicated_worker_startup_manifest_hash,
    dedicated_worker_startup_runtime_paths,
    write_dedicated_worker_startup_manifest,
)
from mindroom.workers.backends.docker_config import (
    DOCKER_HOST_ALIAS,
    DOCKER_RESERVED_EXTRA_ENV_NAMES,
    LABEL_COMPONENT,
    LABEL_IMAGE_REFERENCE,
    LABEL_LAUNCH_CONFIG_HASH,
    LABEL_MANAGED_BY,
    LABEL_NAME,
    LABEL_RUNTIME_NAMESPACE,
    LABEL_STARTUP_MANIFEST_HASH,
    LABEL_STORAGE_LAYOUT,
    LABEL_STORAGE_LAYOUT_VALUE,
    LABEL_WORKER_ID,
    DockerWorkerBackendConfig,
    docker_backend_cleanup_signature,
    docker_backend_config_signature,
    docker_primary_runtime_paths,
    docker_workers_root,
    normalize_docker_name_prefix,
)
from mindroom.workers.backends.docker_projection import PROJECTED_CONFIGS_DIRNAME, DockerProjectionManager
from mindroom.workers.backends.legacy_docker_worker_metadata import (
    drop_retired_docker_worker_record_keys,
    legacy_docker_worker_keys,
)
from mindroom.workers.backends.local import LocalWorkerStatePaths, local_worker_state_paths_for_root
from mindroom.workers.backends.worker_security import docker_worker_security_options
from mindroom.workers.compatibility import WorkerProtocolMismatch, ready_worker_protocol_mismatch
from mindroom.workers.models import SANDBOX_RUNNER_API_ROOT, ConfiguredWorkerBackend
from mindroom.workers.progress import WorkerWarmupProgress, noop_finalize_progress
from mindroom.workers.worker_retirement import open_worker_state_root

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    from mindroom.workers.models import (
        ProgressSink,
        WorkerHandle,
        WorkerLaunchConfig,
        WorkerReadyPhase,
        WorkerSpec,
        WorkerStatus,
    )

    class _DockerContainer(Protocol):
        attrs: dict[str, object]
        status: str
        id: str

        def reload(self) -> None: ...

        def start(self) -> None: ...

        def stop(self, timeout: int = 10) -> None: ...

        def remove(self, force: bool = True) -> None: ...

        def logs(self, *, tail: int = ...) -> bytes: ...

    class _DockerContainersApi(Protocol):
        def get(self, name: str) -> _DockerContainer: ...

        def list(self, **kwargs: object) -> Sequence[_DockerContainer]: ...

        def run(self, image: str, **kwargs: object) -> _DockerContainer: ...

    class _DockerImage(Protocol):
        id: str

    class _DockerImagesApi(Protocol):
        def get(self, name: str) -> _DockerImage: ...

        def pull(self, name: str) -> _DockerImage: ...

    class _DockerApiClient(Protocol):
        timeout: float

    class _DockerClient(Protocol):
        containers: _DockerContainersApi
        images: _DockerImagesApi
        api: _DockerApiClient

    class _DockerErrors(Protocol):
        DockerException: type[Exception]
        NotFound: type[Exception]


logger = get_logger(__name__)

_READY_POLL_INTERVAL_SECONDS = 1.0
_CONTAINER_LOG_EXCERPT_MAX_CHARS = 4096


class _WorkerImageIncompatibleError(WorkerBackendError):
    """A ready worker answered the health check with another protocol."""

    def __init__(self, mismatch: WorkerProtocolMismatch) -> None:
        super().__init__(mismatch.error)
        self.mismatch = mismatch


_TOKEN_ENV_NAME = SANDBOX_RUNTIME_ENV_BY_KEY["proxy_token"]
_DEDICATED_WORKER_KEY_ENV = SANDBOX_RUNTIME_ENV_BY_KEY["dedicated_worker_key"]

# Worker containers get a read-only root filesystem. The image's /app tree stays
# owned by the runtime user so trusted primaries can install tool extras into it,
# but in a worker that would let tool code replace runner code the runner imports
# later, and a stopped container keeps its writable layer across restarts. Only the
# bind mounts and this private /tmp stay writable. The tmpfs is RAM-backed and
# workers have no memory limit, so its size is capped.
_WORKER_TMPFS = {"/tmp": "rw,nosuid,nodev,mode=1777,size=1g"}  # noqa: S108
# Minimal-mode shells call the MindRoom API back, which usually listens on the Docker host.
_WORKER_EXTRA_HOSTS = {DOCKER_HOST_ALIAS: "host-gateway"}
# Only the image command is replaced, so the image's init entrypoint stays PID 1 and reaps orphaned children.
_WORKER_COMMAND = ["/app/run-sandbox-runner.sh"]

# Backend-owned control state lives beside the worker roots, never inside one.
# Each worker root is bind-mounted read-write into its own container, so any
# lifecycle state kept there would be rewritable by the untrusted tool code the
# container runs.
_WORKER_CONTROL_DIRNAME = ".mindroom-worker-control"

# A worker container name is the prefix, a dash, and a digest, in at most 63 characters.
_CONTAINER_NAME_DIGEST_LENGTH = 24
_CONTAINER_NAME_PREFIX_MAX_LENGTH = 63 - 1 - _CONTAINER_NAME_DIGEST_LENGTH

_DOCKER_DEPENDENCIES = ["docker"]
_DOCKER_EXTRA = "docker"

__all__ = [
    "DockerWorkerBackend",
    "check_docker_workers_absent_for_storage_upgrade",
    "configure_docker_worker_backend",
    "container_config_labels",
    "ensure_docker_dependencies",
    "list_docker_worker_containers",
]


def _runtime_namespace_for_workers_root(workers_root: Path) -> str:
    resolved_workers_root = workers_root.expanduser().resolve()
    return hashlib.sha256(str(resolved_workers_root).encode("utf-8")).hexdigest()[:12]


def _runtime_namespace_filters(workers_root: Path) -> dict[str, list[str]]:
    """Return the ``containers.list`` filters that select every container in one runtime's namespace."""
    return {"label": [f"{LABEL_RUNTIME_NAMESPACE}={_runtime_namespace_for_workers_root(workers_root)}"]}


def container_config_labels(container: _DockerContainer) -> dict[str, str]:
    """Return the string labels recorded on one container."""
    config = container.attrs.get("Config")
    if not isinstance(config, dict):
        return {}
    labels = cast("dict[str, object]", config).get("Labels")
    if not isinstance(labels, dict):
        return {}
    return {
        name: value
        for name, value in cast("dict[str, object]", labels).items()
        if isinstance(name, str) and isinstance(value, str)
    }


def _container_name_prefix(prefix: str) -> str:
    """Return the prefix every worker container name starts with: the normalized prefix, cut to the name cap."""
    return normalize_docker_name_prefix(prefix)[:_CONTAINER_NAME_PREFIX_MAX_LENGTH].rstrip("-")


def _container_name_for_worker(worker_key: str, *, prefix: str, runtime_namespace: str) -> str:
    digest = hashlib.sha256(f"{runtime_namespace}:{worker_key}".encode()).hexdigest()[:_CONTAINER_NAME_DIGEST_LENGTH]
    return f"{_container_name_prefix(prefix)}-{digest}"


def _launch_hash(image: str, arguments: Mapping[str, object]) -> str:
    """Return the digest of one ``containers.run`` call, given without its launch-hash label."""
    return stable_json_digest({"image": image, **arguments})


def ensure_docker_dependencies(runtime_paths: RuntimePaths) -> None:
    """Install the optional Docker SDK runtime when needed."""
    try:
        ensure_optional_deps(_DOCKER_DEPENDENCIES, _DOCKER_EXTRA, runtime_paths)
    except ImportError as exc:
        raise WorkerBackendError(str(exc)) from exc


def _load_docker_client_and_errors(
    *,
    runtime_paths: RuntimePaths,
    timeout_seconds: float | None = None,
    ensure_dependencies: bool = True,
) -> tuple[_DockerClient, _DockerErrors]:
    if ensure_dependencies:
        ensure_docker_dependencies(runtime_paths)
    try:
        with optional_dependency_environment():
            docker_module = importlib.import_module("docker")
            docker_errors = cast("_DockerErrors", importlib.import_module("docker.errors"))
    except ModuleNotFoundError as exc:
        msg = (
            "The Docker worker backend could not import the Docker SDK. "
            "Install the optional 'docker' extra before starting the primary."
        )
        raise WorkerBackendError(msg) from exc

    docker_from_env = cast("Callable[..., _DockerClient]", docker_module.from_env)
    try:
        kwargs: dict[str, object] = {"environment": runtime_env_values(runtime_paths)}
        if timeout_seconds is not None:
            kwargs["timeout"] = timeout_seconds
        client = docker_from_env(**kwargs)
    except docker_errors.DockerException as exc:
        msg = f"Failed to initialize Docker client: {exc}"
        raise WorkerBackendError(msg) from exc
    return client, docker_errors


@dataclass(frozen=True, slots=True)
class _DockerWorkerPaths:
    """Filesystem layout for one Docker worker.

    ``state`` is the worker root bind-mounted read-write into the container, so
    untrusted tool code owns every byte below it. The control metadata this
    backend reads back therefore lives under ``root``, a directory beside the
    worker roots that is never mounted into any container.
    """

    state: LocalWorkerStatePaths
    root: Path
    metadata_dir: Path
    metadata_file: Path


def _docker_worker_paths(workers_root: Path, worker_dir: str) -> _DockerWorkerPaths:
    """Return the state and control paths owned by one Docker worker directory."""
    control_root = workers_root / _WORKER_CONTROL_DIRNAME / worker_dir
    metadata_dir = control_root / "metadata"
    return _DockerWorkerPaths(
        state=local_worker_state_paths_for_root(workers_root / worker_dir),
        root=control_root,
        metadata_dir=metadata_dir,
        metadata_file=metadata_dir / "worker.json",
    )


@dataclass
class _DockerWorkerMetadata:
    """One worker's control record; its endpoint, port, and container ID are read from the live container."""

    worker_id: str
    worker_key: str
    container_name: str
    lifecycle: WorkerLifecycleState


def _set_docker_request_timeout(client: _DockerClient, timeout_seconds: float) -> None:
    try:
        client.api.timeout = timeout_seconds
    except (AttributeError, TypeError) as exc:
        msg = "Docker client does not expose a bounded request timeout."
        raise WorkerBackendError(msg) from exc


def check_docker_workers_absent_for_storage_upgrade(
    runtime_paths: RuntimePaths,
    *,
    timeout_seconds: float,
) -> None:
    """Verify no containers remain in this runtime namespace, without changing state."""
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        msg = "Docker worker preflight timeout must be a positive finite number."
        raise WorkerBackendError(msg)
    deadline = time.monotonic() + timeout_seconds

    def remaining() -> float:
        value = deadline - time.monotonic()
        if value <= 0:
            msg = "Docker worker preflight timed out before absence was verified."
            raise WorkerBackendError(msg)
        return value

    workers_root = docker_workers_root(runtime_paths.storage_root.expanduser().resolve())
    client, _docker_errors = _load_docker_client_and_errors(
        runtime_paths=runtime_paths,
        timeout_seconds=remaining(),
        ensure_dependencies=False,
    )
    _set_docker_request_timeout(client, remaining())
    try:
        containers = client.containers.list(
            all=True,
            sparse=True,
            filters=_runtime_namespace_filters(workers_root),
        )
    except Exception as exc:
        msg = f"Failed to verify Docker worker absence: {exc}"
        raise WorkerBackendError(msg) from exc
    remaining()
    if not isinstance(containers, list):
        msg = "Docker worker preflight returned an invalid container inventory."
        raise WorkerBackendError(msg)
    if containers:
        msg = "Remove all Docker worker containers in this runtime namespace before the private-storage upgrade."
        raise WorkerBackendError(msg)


def list_docker_worker_containers(runtime_paths: RuntimePaths) -> Sequence[_DockerContainer]:
    """List every container, running or stopped, in this runtime's namespace."""
    workers_root = docker_workers_root(runtime_paths.storage_root.expanduser().resolve())
    client, _docker_errors = _load_docker_client_and_errors(runtime_paths=runtime_paths)
    return client.containers.list(
        all=True,
        filters=_runtime_namespace_filters(workers_root),
    )


def configure_docker_worker_backend(
    runtime_paths: RuntimePaths,
    *,
    auth_token: str | None,
    storage_root: Path,
) -> ConfiguredWorkerBackend:
    """Read the Docker backend's settings once: the manager they identify, the workers it owns, and its build."""
    config = DockerWorkerBackendConfig.from_runtime(runtime_paths)
    storage_root = storage_root.expanduser().resolve()
    primary_runtime_paths = docker_primary_runtime_paths(runtime_paths, config, storage_root=storage_root)
    return ConfiguredWorkerBackend(
        signature=docker_backend_config_signature(config, primary_runtime_paths, auth_token=auth_token),
        cleanup_signature=docker_backend_cleanup_signature(config, primary_runtime_paths),
        build=lambda current_launch: DockerWorkerBackend(
            config=config,
            auth_token=auth_token,
            current_launch=current_launch,
            storage_root=storage_root,
            runtime_paths=runtime_paths,
        ),
    )


@dataclass(frozen=True, slots=True)
class _DockerLaunch:
    """One worker container's ``containers.run`` call: the image ID it passes and exactly its keyword arguments.

    ``launch_hash`` digests the image and every argument but the launch-hash label that carries it, so a
    container whose label differs from a launch rendered now was run with some other argument.
    """

    image: str
    arguments: dict[str, object]
    launch_hash: str


@dataclass(frozen=True, slots=True)
class _WorkerContainerState:
    """What one ensure finds for its worker, evaluated once before it acts.

    ``current`` holds when the owned container was run with the launch rendered now and its config projection
    is still intact on disk; a rebuilt projection keeps its pathname, which a running bind mount does not follow.
    ``image_id`` is the configured image's local ID, or None when it is not local.
    """

    container: _DockerContainer | None
    current: bool
    image_id: str | None
    mounts: list[tuple[Path, str, bool]]


class DockerWorkerBackend:
    """Docker-backed worker provider for dedicated local sandbox-runner containers."""

    backend_name = WorkerBackendKind.DOCKER.value
    cleanup_locator: str | None = None

    def __init__(
        self,
        *,
        config: DockerWorkerBackendConfig,
        auth_token: str | None,
        current_launch: Callable[[], WorkerLaunchConfig],
        storage_root: Path,
        runtime_paths: RuntimePaths,
    ) -> None:
        if auth_token is None:
            msg = "A worker auth token is required for Docker workers."
            raise WorkerBackendError(msg)
        validate_dedicated_worker_extra_env(
            config.extra_env,
            backend_name="Docker",
            extra_reserved_names=DOCKER_RESERVED_EXTRA_ENV_NAMES,
        )

        if len(normalize_docker_name_prefix(config.name_prefix)) > _CONTAINER_NAME_PREFIX_MAX_LENGTH:
            logger.warning(
                "MINDROOM_DOCKER_WORKER_NAME_PREFIX is too long, so worker container names start with only part of it",
                max_length=_CONTAINER_NAME_PREFIX_MAX_LENGTH,
                container_name_prefix=_container_name_prefix(config.name_prefix),
            )

        self.config = config
        self._worker_layout = DedicatedWorkerLayout(
            dedicated_root=config.storage_mount_path,
            config_path=config.config_path,
            worker_port=config.worker_port,
        )
        self._current_launch = current_launch
        self.auth_token = auth_token
        self.idle_timeout_seconds = config.idle_timeout_seconds
        self._storage_root = storage_root.expanduser().resolve()
        self._workers_root = docker_workers_root(self._storage_root)
        self._control_root = self._workers_root / _WORKER_CONTROL_DIRNAME
        self._runtime_paths = docker_primary_runtime_paths(runtime_paths, config, storage_root=self._storage_root)
        config.validate_runtime_security(self._runtime_paths)
        self._client, self._docker_errors = _load_docker_client_and_errors(runtime_paths=self._runtime_paths)
        self._worker_locks = WorkerLocks()
        self._metadata_lock = threading.Lock()
        self._progress = WorkerWarmupProgress(self.backend_name)
        self._projection_manager = DockerProjectionManager(
            config=config,
            projected_configs_root=self._workers_root / PROJECTED_CONFIGS_DIRNAME,
            runtime_paths=self._runtime_paths,
        )
        # The primary's own environment declares the credentials it grants; the worker's config file may live
        # beside another `.env`, so only the storage root is rebased.
        self._credentials_manager = get_runtime_credentials_manager(
            runtime_paths_with_storage_root(runtime_paths, self._storage_root),
        )
        self._runtime_namespace = _runtime_namespace_for_workers_root(self._workers_root)
        self._workers_root.mkdir(parents=True, exist_ok=True)
        drop_retired_docker_worker_record_keys(self._control_root)
        self._adopt_legacy_worker_records()

    def shutdown(self) -> None:
        """Remove backend-owned containers before discarding this Docker manager."""
        failures: list[str] = []
        for paths in self._metadata_paths():
            metadata = self._load_metadata(paths)
            if metadata is None:
                continue
            with self._worker_locks.for_key(metadata.worker_key):
                metadata = self._load_metadata(paths)
                if metadata is None:
                    continue
                try:
                    self._remove_container(self._read_owned_container(metadata.worker_key))
                except WorkerBackendError as exc:
                    failures.append(str(exc))
                    continue
                metadata.lifecycle = mark_worker_idle(metadata.lifecycle)
                self._save_metadata(paths, metadata)
        if failures:
            failure_text = "; ".join(failures)
            msg = f"Failed to shut down Docker workers: {failure_text}"
            raise WorkerBackendError(msg)

    def ensure_worker(
        self,
        spec: WorkerSpec,
        *,
        now: float | None = None,
        progress_sink: ProgressSink | None = None,
    ) -> WorkerHandle:
        """Resolve or start the worker container for one worker key, from the launch applied once its lock is held."""
        timestamp = time.time() if now is None else now
        # A caller waiting behind another's start of this worker is sent that start's progress.
        with self._progress.subscribed(spec.worker_key, progress_sink), self._worker_locks.for_key(spec.worker_key):
            launch = self._current_launch()
            # Docker creates missing bind sources as root, so shared workspaces exist before any plan.
            plan_scoped_workspace_mounts(
                worker_key=resolve_state_scope_worker_key(spec.worker_key, spec.state_scope_worker_key),
                local_shared_storage_root=self._storage_root,
                worker_visible_shared_storage_root=Path(self.config.storage_mount_path),
                private_agent_names=spec.private_agent_names,
                resolved_agent_policies=launch.agent_policies,
                create_shared=True,
            )
            paths = self._worker_paths(spec.worker_key)
            metadata = self._load_metadata(paths, expected_worker_key=spec.worker_key) or self._default_metadata(
                spec.worker_key,
                timestamp,
            )
            self._sync_metadata_identity(metadata)
            state = self._worker_container_state(paths, spec=spec, launch=launch)

            cold_start = not state.current or not self._container_is_running(state.container)
            metadata.lifecycle = prepare_worker_ensure_lifecycle(
                metadata.lifecycle,
                now=timestamp,
                should_restart=cold_start or metadata.lifecycle.status == "failed",
            )
            self._save_metadata(paths, metadata)
            poll_reporter: Callable[[], None] | None = None
            finalize_progress: Callable[[WorkerReadyPhase, str | None], None] = noop_finalize_progress
            if cold_start:
                poll_reporter, finalize_progress = self._progress.start_reporter(spec.worker_key)

            try:
                sync_shared_credentials_to_worker(
                    spec.worker_key,
                    allowed_services=mirrored_credential_services(spec, launch),
                    credentials_manager=self._credentials_manager,
                )
                container = self._ensure_container(paths, spec=spec, launch=launch, state=state)
                try:
                    self._wait_for_ready(container, on_poll_tick=poll_reporter)
                except _WorkerImageIncompatibleError as stale_error:
                    metadata.lifecycle = prepare_worker_ensure_lifecycle(
                        metadata.lifecycle,
                        now=timestamp,
                        should_restart=True,
                    )
                    self._save_metadata(paths, metadata)
                    if poll_reporter is None:
                        poll_reporter, finalize_progress = self._progress.start_reporter(spec.worker_key)
                    container = self._relaunch_after_stale_image(
                        paths,
                        spec=spec,
                        launch=launch,
                        image_id=state.image_id,
                        mismatch=stale_error.mismatch,
                    )
                    # A worker is replaced once; a replacement that still mismatches fails the request.
                    self._wait_for_ready(container, on_poll_tick=poll_reporter)
            except Exception as exc:
                failure_reason = str(exc)
                finalize_progress("failed", failure_reason)
                self._record_failure_locked(paths, metadata, failure_reason, now=timestamp)
                if isinstance(exc, WorkerBackendError):
                    raise
                raise WorkerBackendError(failure_reason) from exc

            finalize_progress("ready", None)
            metadata.lifecycle = mark_worker_ready(metadata.lifecycle, now=timestamp)
            self._save_metadata(paths, metadata)
            return self._to_handle(metadata, container, now=timestamp, paths=paths)

    def touch_worker(self, worker_key: str, *, now: float | None = None) -> WorkerHandle | None:
        """Refresh last-used metadata for one existing worker."""
        timestamp = time.time() if now is None else now
        with self._worker_locks.for_key(worker_key):
            paths = self._worker_paths(worker_key)
            metadata = self._load_metadata(paths, expected_worker_key=worker_key)
            if metadata is None:
                return None
            metadata.lifecycle = touch_worker_lifecycle(metadata.lifecycle, now=timestamp)
            container = self._read_owned_container(metadata.worker_key)
            self._save_metadata(paths, metadata)
            return self._to_handle(metadata, container, now=timestamp, paths=paths)

    def list_workers(self, *, include_idle: bool = True, now: float | None = None) -> list[WorkerHandle]:
        """List workers known to this backend."""
        timestamp = time.time() if now is None else now
        handles: list[WorkerHandle] = []
        for paths in self._metadata_paths():
            metadata = self._load_metadata(paths)
            if metadata is None:
                continue
            with self._worker_locks.for_key(metadata.worker_key):
                metadata = self._load_metadata(paths)
                if metadata is None:
                    continue
                container = self._read_owned_container(metadata.worker_key)
                handles.append(self._to_handle(metadata, container, now=timestamp, paths=paths))
        return filter_and_sort_worker_handles(handles, include_idle)

    def cleanup_idle_workers(self, *, now: float | None = None) -> list[WorkerHandle]:
        """Stop idle containers while retaining worker-owned state."""
        timestamp = time.time() if now is None else now
        cleaned: list[WorkerHandle] = []
        for paths in self._metadata_paths():
            metadata = self._load_metadata(paths)
            if metadata is None:
                continue
            with self._worker_locks.for_key(metadata.worker_key):
                metadata = self._load_metadata(paths)
                if metadata is None:
                    continue
                container = self._read_owned_container(metadata.worker_key)
                handle = self._to_handle(metadata, container, now=timestamp, paths=paths)
                idle_timed_out = timestamp - metadata.lifecycle.last_used_at >= self.idle_timeout_seconds
                if handle.status == "idle" and self._container_is_running(container):
                    self._stop_container(container)
                    metadata.lifecycle = mark_worker_idle(metadata.lifecycle)
                    self._save_metadata(paths, metadata)
                    cleaned.append(self._to_handle(metadata, container, now=timestamp, paths=paths))
                elif handle.status == "failed" and container is not None and idle_timed_out:
                    # A worker that failed and was never revived keeps its exited
                    # container so a quick retry can restart it. Once it is past the
                    # idle timeout it is abandoned, so reap the container to stop
                    # stale failures from accumulating; the failed metadata is kept
                    # and a later ensure recreates the container.
                    self._stop_container(container)
                    self._remove_container(container)
        return filter_and_sort_worker_handles(cleaned, True)

    def retire_worker(self, worker_key: str) -> None:
        """Remove one exact Docker worker container and all backend-owned state."""
        with self._worker_locks.for_key(worker_key):
            worker_name = worker_dir_name(worker_key)
            try:
                with (
                    open_worker_state_root(
                        self._workers_root,
                        workers_subpath=(),
                        worker_name=worker_name,
                    ) as state,
                    open_worker_state_root(
                        self._control_root,
                        workers_subpath=(),
                        worker_name=worker_name,
                        expected_worker_key=worker_key,
                        identity_path=("metadata", "worker.json"),
                        identity_field_path=("worker_key",),
                    ) as control,
                ):
                    if state.exists and not control.exists:
                        msg = (
                            f"Docker worker '{worker_log_id(worker_key)}' is missing the control identity metadata "
                            "required to retire its state root."
                        )
                        raise WorkerBackendError(msg)
                    container_name = self._container_name_for_worker(worker_key)
                    container = self._read_container(container_name)
                    if container is not None and not self._container_ownership_matches(
                        container,
                        container_name=container_name,
                        worker_key=worker_key,
                    ):
                        msg = f"Docker worker container does not match retirement key '{worker_log_id(worker_key)}'."
                        raise WorkerBackendError(msg)
                    self._remove_container(container)
                    if self._read_container(container_name) is not None:
                        msg = f"Docker worker '{worker_log_id(worker_key)}' still exists after retirement."
                        raise WorkerBackendError(msg)
                    self._projection_manager.retire_worker_projection(worker_name)
                    state.remove()
                    control.remove()
            except (OSError, RecursionError, TypeError, ValueError) as exc:
                msg = f"Failed to retire Docker worker '{worker_log_id(worker_key)}': {exc}"
                raise WorkerBackendError(msg) from exc

    def record_failure(
        self,
        worker_key: str,
        failure_reason: str,
        *,
        now: float | None = None,
        startup_count: int | None = None,
    ) -> WorkerHandle:
        """Persist a failed worker startup or execution state."""
        timestamp = time.time() if now is None else now
        with self._worker_locks.for_key(worker_key):
            paths = self._worker_paths(worker_key)
            metadata = self._load_metadata(
                paths,
                expected_worker_key=worker_key,
            ) or self._default_metadata(worker_key, timestamp)
            if startup_count is not None and metadata.lifecycle.startup_count != startup_count:
                # The request failed on a container this worker has since replaced.
                return self._to_handle(metadata, None, now=timestamp, paths=paths)
            return self._record_failure_locked(paths, metadata, failure_reason, now=timestamp)

    def _adopt_legacy_worker_records(self) -> None:
        """Give workers from before the control directory a fresh backend-owned record.

        Their containers carry a launch hash of an older form, so the next ensure replaces each one.
        """
        worker_keys = legacy_docker_worker_keys(self._workers_root, control_root=self._control_root)
        if not worker_keys:
            return
        now = time.time()
        for worker_key in worker_keys:
            metadata = self._default_metadata(worker_key, now)
            metadata.lifecycle = mark_worker_idle(metadata.lifecycle)
            self._save_metadata(self._worker_paths(worker_key), metadata)

    def _worker_paths(self, worker_key: str) -> _DockerWorkerPaths:
        return _docker_worker_paths(self._workers_root, worker_dir_name(worker_key))

    def _default_metadata(self, worker_key: str, now: float) -> _DockerWorkerMetadata:
        worker_id = self._container_name_for_worker(worker_key)
        return _DockerWorkerMetadata(
            worker_id=worker_id,
            worker_key=worker_key,
            container_name=worker_id,
            lifecycle=initial_worker_lifecycle_state(now=now),
        )

    def _container_name_for_worker(self, worker_key: str) -> str:
        return _container_name_for_worker(
            worker_key,
            prefix=self.config.name_prefix,
            runtime_namespace=self._runtime_namespace,
        )

    def _sync_metadata_identity(self, metadata: _DockerWorkerMetadata) -> None:
        expected_container_name = self._container_name_for_worker(metadata.worker_key)
        if metadata.container_name != expected_container_name:
            # The recorded name is only a hint for reaping the container this
            # backend started for this worker key under an earlier naming config;
            # anything else answering to that name is left untouched.
            self._remove_container(
                self._read_owned_container_named(metadata.container_name, worker_key=metadata.worker_key),
            )
        metadata.worker_id = expected_container_name
        metadata.container_name = expected_container_name

    def _metadata_paths(self) -> list[_DockerWorkerPaths]:
        return list_worker_state_paths(
            self._control_root,
            state_paths_from_root=lambda control_root: _docker_worker_paths(self._workers_root, control_root.name),
        )

    def _load_metadata(
        self,
        paths: _DockerWorkerPaths,
        *,
        expected_worker_key: str | None = None,
    ) -> _DockerWorkerMetadata | None:
        """Load one control record, rejecting any record that is not this worker's.

        A record is only authoritative for the worker key it is filed under, so
        callers that already know their key bind the record to it and the
        discovery paths fall back to the control directory name.
        """
        metadata = load_worker_metadata(paths, metadata_type=_DockerWorkerMetadata)
        if metadata is None:
            return None
        if not isinstance(metadata.worker_key, str):
            return None
        if expected_worker_key is not None:
            return metadata if metadata.worker_key == expected_worker_key else None
        return metadata if worker_dir_name(metadata.worker_key) == paths.root.name else None

    def _save_metadata(self, paths: _DockerWorkerPaths, metadata: _DockerWorkerMetadata) -> None:
        save_worker_metadata(
            paths,
            metadata,
            ensure_root=True,
            lock=self._metadata_lock,
        )

    def _read_container(self, container_name: str) -> _DockerContainer | None:
        try:
            return self._client.containers.get(container_name)
        except self._docker_errors.NotFound:
            return None
        except self._docker_errors.DockerException as exc:
            msg = f"Failed to inspect Docker worker '{container_name}': {exc}"
            raise WorkerBackendError(msg) from exc

    def _container_ownership_matches(
        self,
        container: _DockerContainer,
        *,
        container_name: str,
        worker_key: str,
    ) -> bool:
        """Return whether this backend runtime owns one container for one worker key.

        ``containers.get`` resolves any name or id on the daemon, so ownership is
        proven from the labels this backend stamps on the containers it creates
        plus the worker key the container was started with. Anything else that
        answers to the same name is foreign and must never be started, stopped,
        removed or reused as this worker.
        """
        labels = container_config_labels(container)
        return (
            labels.get(LABEL_RUNTIME_NAMESPACE) == self._runtime_namespace
            and labels.get(LABEL_WORKER_ID) == container_name
            and self._container_worker_key(container) == worker_key
        )

    def _read_owned_container_named(self, container_name: str, *, worker_key: str) -> _DockerContainer | None:
        """Return one named container only when this backend owns it for ``worker_key``."""
        container = self._read_container(container_name)
        if container is None or not self._container_ownership_matches(
            container,
            container_name=container_name,
            worker_key=worker_key,
        ):
            return None
        return container

    def _read_owned_container(self, worker_key: str) -> _DockerContainer | None:
        """Return this backend's container for one worker key, treating foreign ones as absent."""
        return self._read_owned_container_named(
            self._container_name_for_worker(worker_key),
            worker_key=worker_key,
        )

    def _require_owned_container(self, worker_key: str) -> _DockerContainer | None:
        """Return this backend's container for one worker key, or fail on a foreign one.

        Lifecycle transitions recreate and remove containers, so a foreign
        container holding the derived name fails the request instead of being
        destroyed.
        """
        container_name = self._container_name_for_worker(worker_key)
        container = self._read_container(container_name)
        if container is not None and not self._container_ownership_matches(
            container,
            container_name=container_name,
            worker_key=worker_key,
        ):
            msg = (
                f"Docker container '{container_name}' is not owned by this MindRoom worker runtime. "
                f"Remove it manually before running worker '{worker_log_id(worker_key)}'."
            )
            raise WorkerBackendError(msg)
        return container

    def _worker_container_state(
        self,
        paths: _DockerWorkerPaths,
        *,
        spec: WorkerSpec,
        launch: WorkerLaunchConfig,
    ) -> _WorkerContainerState:
        """Evaluate once whether the worker's container was run with the launch rendered now.

        When the configured image is not local, the container's own image may only prove reuse, so an idle-stopped
        worker restarts without the registry: the launch is rendered from it, so its configured-image label still has
        to match. Any replacement starts from the configured image, pulled when it is not local.
        """
        container = self._require_owned_container(spec.worker_key)
        image_id = self._configured_image_id()
        if container is None:
            return _WorkerContainerState(container=None, current=False, image_id=image_id, mounts=[])
        mounts = self._worker_mounts(paths, spec=spec, launch=launch, materialize_projection=False)
        render_image_id = image_id or self._container_image_id(container)
        current = (
            mounts is not None
            and render_image_id is not None
            and container_config_labels(container).get(LABEL_LAUNCH_CONFIG_HASH)
            == self._render_launch(
                spec.worker_key,
                image_id=render_image_id,
                mounts=mounts,
                startup_manifest_hash=self._startup_manifest_hash(spec, launch),
            ).launch_hash
        )
        return _WorkerContainerState(container=container, current=current, image_id=image_id, mounts=mounts or [])

    def _render_launch(
        self,
        worker_key: str,
        *,
        image_id: str,
        mounts: Sequence[tuple[Path, str, bool]],
        startup_manifest_hash: str,
    ) -> _DockerLaunch:
        """Return the ``containers.run`` call that starts the worker's container from ``image_id`` now."""
        container_name = self._container_name_for_worker(worker_key)
        labels = {
            LABEL_COMPONENT: "worker",
            LABEL_MANAGED_BY: "mindroom",
            LABEL_NAME: "mindroom-docker-worker",
            LABEL_WORKER_ID: container_name,
            LABEL_RUNTIME_NAMESPACE: self._runtime_namespace,
            LABEL_STORAGE_LAYOUT: LABEL_STORAGE_LAYOUT_VALUE,
            LABEL_STARTUP_MANIFEST_HASH: startup_manifest_hash,
            LABEL_IMAGE_REFERENCE: self.config.image,
            **self.config.extra_labels,
        }
        arguments: dict[str, object] = {
            "command": _WORKER_COMMAND,
            "name": container_name,
            "detach": True,
            "environment": self._container_env(worker_key),
            "volumes": [
                f"{host_path}:{container_path}:{'ro' if read_only else 'rw'}"
                for host_path, container_path, read_only in mounts
            ],
            "ports": {f"{self.config.worker_port}/tcp": (self.config.publish_host, None)},
            "labels": labels,
            "user": self.config.user,
            "read_only": True,
            "tmpfs": _WORKER_TMPFS,
            "extra_hosts": _WORKER_EXTRA_HOSTS,
            "runtime": self.config.runtime,
        }
        if self.config.security_policy == "computer":
            arguments["cap_drop"] = ["ALL"]
            arguments["security_opt"] = docker_worker_security_options()
        launch_hash = _launch_hash(image_id, arguments)
        labels[LABEL_LAUNCH_CONFIG_HASH] = launch_hash
        return _DockerLaunch(image=image_id, arguments=arguments, launch_hash=launch_hash)

    def _ensure_container(
        self,
        paths: _DockerWorkerPaths,
        *,
        spec: WorkerSpec,
        launch: WorkerLaunchConfig,
        state: _WorkerContainerState,
    ) -> _DockerContainer:
        paths.state.root.mkdir(parents=True, exist_ok=True)
        container = state.container
        if container is None or not state.current:
            self._remove_container(container)
            return self._create_container(paths, spec=spec, launch=launch, image_id=state.image_id)
        if not self._container_is_running(container):
            # Docker resolves every bind destination again on start, through whatever worker code left in its root.
            self._prepare_nested_storage_mount_targets(paths, (mount[1] for mount in state.mounts))
            try:
                container.start()
            except self._docker_errors.DockerException as exc:
                msg = f"Failed to start Docker worker '{worker_log_id(spec.worker_key)}': {exc}"
                raise WorkerBackendError(msg) from exc
        return self._published_container(container, worker_key=spec.worker_key)

    def _create_container(
        self,
        paths: _DockerWorkerPaths,
        *,
        spec: WorkerSpec,
        launch: WorkerLaunchConfig,
        image_id: str | None,
    ) -> _DockerContainer:
        """Write what a new container boots from, then run it from ``image_id``, or the configured image pulled now."""
        if image_id is None:
            image_id = self._pull_configured_image()
        startup_manifest_hash = self._write_startup_manifest(paths, spec=spec, launch=launch)
        mounts = self._worker_mounts(paths, spec=spec, launch=launch, materialize_projection=True)
        assert mounts is not None, "a materialized config projection is ready"
        self._prepare_nested_storage_mount_targets(paths, (mount[1] for mount in mounts))
        rendered = self._render_launch(
            spec.worker_key,
            image_id=image_id,
            mounts=mounts,
            startup_manifest_hash=startup_manifest_hash,
        )
        container = self._client.containers.run(rendered.image, **rendered.arguments)
        return self._published_container(container, worker_key=spec.worker_key)

    def _published_container(self, container: _DockerContainer, *, worker_key: str) -> _DockerContainer:
        self._reload_container(container)
        if self._container_host_port(container) is None:
            msg = f"Docker worker '{worker_log_id(worker_key)}' is missing a published port."
            raise WorkerBackendError(msg)
        return container

    def _configured_image_id(self) -> str | None:
        """Return the local ID of the configured image, or None when the daemon has no such image."""
        try:
            image_id = self._client.images.get(self.config.image).id
        except self._docker_errors.DockerException:
            return None
        return image_id if isinstance(image_id, str) and image_id.strip() else None

    def _pull_configured_image(self) -> str:
        try:
            return self._client.images.pull(self.config.image).id
        except self._docker_errors.DockerException as exc:
            msg = f"Pulling '{self.config.image}' failed: {exc}"
            raise WorkerBackendError(msg) from exc

    def _relaunch_after_stale_image(
        self,
        paths: _DockerWorkerPaths,
        *,
        spec: WorkerSpec,
        launch: WorkerLaunchConfig,
        image_id: str | None,
        mismatch: WorkerProtocolMismatch,
    ) -> _DockerContainer:
        """Replace the worker once after a protocol mismatch, first pulling the configured image when it is older.

        A stale local image otherwise fails every worker start until someone
        pulls by hand, since tag references like ``:latest`` keep resolving to
        the local image. A newer worker needs a newer runtime, which no pull provides.
        """
        if not mismatch.worker_is_newer:
            try:
                image_id = self._pull_configured_image()
            except WorkerBackendError as exc:
                msg = f"{mismatch.error} {exc}"
                raise WorkerBackendError(msg) from exc
        container = self._require_owned_container(spec.worker_key)
        self._stop_container(container)
        self._remove_container(container)
        return self._create_container(paths, spec=spec, launch=launch, image_id=image_id)

    def _reload_container(self, container: _DockerContainer) -> None:
        try:
            container.reload()
        except self._docker_errors.DockerException as exc:
            msg = f"Failed to refresh Docker worker state: {exc}"
            raise WorkerBackendError(msg) from exc

    def _wait_for_ready(
        self,
        container: _DockerContainer,
        *,
        on_poll_tick: Callable[[], None] | None = None,
    ) -> None:
        """Poll the worker's health until it answers with this runtime's protocol, calling ``on_poll_tick`` per retry."""
        host_port = self._container_host_port(container)
        assert host_port is not None, "callers pass a container whose published port was checked"

        endpoint_root = self._endpoint_root(host_port)
        healthz_url = f"{endpoint_root}/healthz"
        deadline = time.time() + self.config.ready_timeout_seconds
        with httpx.Client(timeout=min(5.0, self.config.ready_timeout_seconds)) as client:
            while True:
                self._reload_container(container)
                if not self._container_is_running(container):
                    msg = "Docker worker stopped before it became ready." + self._container_logs_excerpt(container)
                    raise WorkerBackendError(msg)

                try:
                    response = client.get(healthz_url)
                except httpx.HTTPError:
                    response = None

                if response is not None and 200 <= response.status_code < 300:
                    mismatch = ready_worker_protocol_mismatch(
                        response,
                        worker=f"Docker worker image '{self.config.image}'",
                    )
                    if mismatch is not None:
                        raise _WorkerImageIncompatibleError(mismatch)
                    return

                if time.time() >= deadline:
                    msg = (
                        f"Docker worker did not become ready within {self.config.ready_timeout_seconds:.0f}s."
                        + self._container_logs_excerpt(container)
                    )
                    raise WorkerBackendError(msg)
                if on_poll_tick is not None:
                    on_poll_tick()
                time.sleep(_READY_POLL_INTERVAL_SECONDS)

    def _record_failure_locked(
        self,
        paths: _DockerWorkerPaths,
        metadata: _DockerWorkerMetadata,
        failure_reason: str,
        *,
        now: float,
    ) -> WorkerHandle:
        container = self._read_owned_container(metadata.worker_key)
        self._stop_container(container)
        metadata.lifecycle = mark_worker_failed(metadata.lifecycle, now=now, failure_reason=failure_reason)
        self._save_metadata(paths, metadata)
        return self._to_handle(metadata, container, now=now, paths=paths)

    def _control_token(self, worker_key: str) -> str:
        return hmac.new(
            self.auth_token.encode(),
            f"docker-worker:{self._runtime_namespace}:{worker_key}".encode(),
            hashlib.sha256,
        ).hexdigest()

    def _container_env(self, worker_key: str) -> dict[str, str]:
        env = dedicated_worker_runner_env(self._worker_layout, worker_key=worker_key)
        if self.config.host_config_path is None:
            # Nothing is mounted at the config path without a host config, so only the startup manifest names it.
            del env["MINDROOM_CONFIG_PATH"]
        env["HOME"] = self._container_home_path(worker_key)
        env[_TOKEN_ENV_NAME] = self._control_token(worker_key)
        # Operator env never replaces a backend-owned variable; the config rejects the names it could collide on.
        return {**self.config.extra_env, **env}

    def _write_startup_manifest(
        self,
        paths: _DockerWorkerPaths,
        *,
        spec: WorkerSpec,
        launch: WorkerLaunchConfig,
    ) -> str:
        """Persist primary validation state before creating one Docker worker, and return the manifest's hash."""
        return write_dedicated_worker_startup_manifest(
            paths.state.root,
            self._worker_runtime_paths(spec.worker_key),
            tool_validation_snapshot=launch.tool_validation_snapshot,
        )

    def _startup_manifest_hash(self, spec: WorkerSpec, launch: WorkerLaunchConfig) -> str:
        """Return the hash of the startup manifest a worker created now for ``spec`` would boot from."""
        return dedicated_worker_startup_manifest_hash(
            self._worker_runtime_paths(spec.worker_key),
            tool_validation_snapshot=launch.tool_validation_snapshot,
        )

    def _container_worker_key(self, container: _DockerContainer) -> str | None:
        """Return the worker key one container was started with, taking the last entry when it repeats."""
        config = container.attrs.get("Config")
        if not isinstance(config, dict):
            return None
        raw_env = cast("dict[str, object]", config).get("Env")
        if not isinstance(raw_env, list):
            return None
        prefix = f"{_DEDICATED_WORKER_KEY_ENV}="
        worker_key = None
        for item in raw_env:
            if isinstance(item, str) and item.startswith(prefix):
                worker_key = item.removeprefix(prefix)
        return worker_key

    def _worker_runtime_paths(self, worker_key: str) -> RuntimePaths:
        validate_dedicated_worker_extra_env(
            self.config.extra_env,
            backend_name="Docker",
            # The primary's secret-path variables, whose worker-side values operator env must not replace.
            extra_reserved_names=secret_path_env_names(self._runtime_paths),
        )
        return dedicated_worker_startup_runtime_paths(
            self._worker_layout,
            worker_key=worker_key,
            process_env={**self._runtime_paths.process_env, **self.config.extra_env},
            env_file_values=self._runtime_paths.env_file_values,
        )

    def _container_home_path(self, worker_key: str) -> str:
        agent_name = worker_key_agent_name(worker_key)
        if agent_name is None:
            return self.config.storage_mount_path
        if resolved_worker_key_scope(worker_key) == "user_agent":
            # Private workspaces can be renamed in config, so the request
            # preparation layer remains the source of truth for the command cwd.
            return self.config.storage_mount_path
        return str(Path(self.config.storage_mount_path) / "agents" / agent_name / "workspace")

    def _worker_root_mount_specs(self, paths: LocalWorkerStatePaths) -> list[tuple[Path, str, bool]]:
        """Return the worker-root binds, keeping primary-written directories read-only.

        The worker root is writable so tools can persist state, but the primary keeps
        mirroring credentials into ``.shared_credentials`` on every ensure and writes the
        startup manifest the runner boots from into ``.runtime``. Mounting those
        directories read-only stops worker code from rewriting them or replacing them
        with links elsewhere.
        """
        storage_mount_path = Path(self.config.storage_mount_path)
        return [
            (paths.root, self.config.storage_mount_path, False),
            (
                paths.root / WORKER_SHARED_CREDENTIALS_DIRNAME,
                f"{self.config.storage_mount_path}/{WORKER_SHARED_CREDENTIALS_DIRNAME}",
                True,
            ),
            (
                sandbox_startup_manifest_path(paths.root).parent,
                str(sandbox_startup_manifest_path(storage_mount_path).parent),
                True,
            ),
        ]

    def _worker_mounts(
        self,
        paths: _DockerWorkerPaths,
        *,
        spec: WorkerSpec,
        launch: WorkerLaunchConfig,
        materialize_projection: bool,
    ) -> list[tuple[Path, str, bool]] | None:
        """Return a worker's bind mounts in container order, or None while its config projection is not written."""
        storage_mounts = self._scoped_storage_mount_specs(spec, launch)
        config_mounts, projection = self._projection_manager.config_mount_specs(
            paths.state,
            launch=launch,
            worker_key=spec.worker_key,
            materialize_projection=materialize_projection,
            storage_mounts=storage_mounts,
        )
        if projection is not None and not projection.ready:
            return None
        return [*self._worker_root_mount_specs(paths.state), *storage_mounts, *config_mounts]

    def _prepare_nested_storage_mount_targets(
        self,
        paths: _DockerWorkerPaths,
        container_paths: Iterable[str],
    ) -> None:
        """Create nested bind targets as real directories, so the daemon neither creates them as root nor follows a link."""
        storage_root = PurePosixPath(self.config.storage_mount_path)
        for raw_container_path in container_paths:
            container_path = PurePosixPath(raw_container_path)
            if container_path == storage_root or storage_root not in container_path.parents:
                continue
            relative_path = container_path.relative_to(storage_root)
            if ".." in relative_path.parts:
                msg = f"Docker worker mount target escapes the worker storage root: {container_path}"
                raise WorkerBackendError(msg)
            # The worker root is worker-writable, so the walk never follows a planted link.
            try:
                with open_directory_within_root(paths.state.root, Path(*relative_path.parts), create=True):
                    pass
            except OSError as exc:
                msg = f"Docker worker mount target must be a real directory: {paths.state.root / relative_path}"
                raise WorkerBackendError(msg) from exc

    def _scoped_storage_mount_specs(self, spec: WorkerSpec, launch: WorkerLaunchConfig) -> list[tuple[Path, str, bool]]:
        resolved_agent_policies = launch.agent_policies
        state_scope_key = resolve_state_scope_worker_key(spec.worker_key, spec.state_scope_worker_key)
        mount_specs = [
            (workspace_mount.local_path, str(workspace_mount.worker_visible_path), False)
            for workspace_mount in plan_scoped_workspace_mounts(
                worker_key=state_scope_key,
                local_shared_storage_root=self._storage_root,
                worker_visible_shared_storage_root=Path(self.config.storage_mount_path),
                private_agent_names=spec.private_agent_names,
                resolved_agent_policies=resolved_agent_policies,
            )
        ]
        mount_specs.extend(
            (shared_mount.local_path, str(shared_mount.worker_visible_path), shared_mount.read_only)
            for shared_mount in plan_shared_directory_mounts(
                worker_key=state_scope_key,
                runtime_paths=self._runtime_paths,
                worker_visible_shared_storage_root=Path(self.config.storage_mount_path),
                resolved_agent_policies=resolved_agent_policies,
            )
        )
        validate_unique_worker_visible_paths(
            (container_path for _host_path, container_path, _read_only in mount_specs),
            worker_key=spec.worker_key,
            duplicate_label="Docker mount",
        )
        return mount_specs

    def _container_image_id(self, container: _DockerContainer) -> str | None:
        image_id = container.attrs.get("Image")
        return image_id if isinstance(image_id, str) and image_id.strip() else None

    def _container_logs_excerpt(self, container: _DockerContainer, *, tail: int = 50) -> str:
        """Return a short tail of a worker container's logs for failure diagnostics.

        A worker that exits during startup (for example because its projected
        config fails validation) only records the cause in its own logs, so the
        backend surfaces that tail in the raised error instead of forcing
        operators to inspect the container by hand.
        """
        try:
            raw_logs = container.logs(tail=tail)
        except self._docker_errors.DockerException:
            return ""
        raw_text = raw_logs.decode("utf-8", errors="replace") if isinstance(raw_logs, bytes) else str(raw_logs)
        text = redact_sensitive_text(raw_text.strip(), max_length=_CONTAINER_LOG_EXCERPT_MAX_CHARS)
        if not text:
            return ""
        return f"\nRecent worker container logs:\n{text}"

    def _container_is_running(self, container: _DockerContainer | None) -> bool:
        return container is not None and container.status == "running"

    def _container_host_port(self, container: _DockerContainer | None) -> int | None:
        if container is None:
            return None
        network_settings = container.attrs.get("NetworkSettings")
        ports = cast("dict[str, object]", network_settings).get("Ports") if isinstance(network_settings, dict) else None
        bindings = (
            cast("dict[str, object]", ports).get(f"{self.config.worker_port}/tcp") if isinstance(ports, dict) else None
        )
        first_binding = bindings[0] if isinstance(bindings, list) and bindings else None
        raw_host_port = (
            cast("dict[str, object]", first_binding).get("HostPort") if isinstance(first_binding, dict) else None
        )
        if not isinstance(raw_host_port, str):
            return None
        try:
            return int(raw_host_port)
        except ValueError:
            return None

    def _stop_container(self, container: _DockerContainer | None) -> None:
        if container is None or not self._container_is_running(container):
            return
        try:
            container.stop(timeout=10)
            container.reload()
        except self._docker_errors.DockerException as exc:
            msg = f"Failed to stop Docker worker: {exc}"
            raise WorkerBackendError(msg) from exc

    def _remove_container(self, container: _DockerContainer | None) -> None:
        if container is None:
            return
        try:
            container.remove(force=True)
        except self._docker_errors.NotFound:
            return
        except self._docker_errors.DockerException as exc:
            msg = f"Failed to remove Docker worker: {exc}"
            raise WorkerBackendError(msg) from exc

    def _endpoint_root(self, host_port: int) -> str:
        return f"http://{self.config.endpoint_host}:{host_port}"

    def _effective_status(
        self,
        metadata: _DockerWorkerMetadata,
        container: _DockerContainer | None,
        *,
        now: float,
    ) -> WorkerStatus:
        lifecycle = metadata.lifecycle
        if lifecycle.status in ("failed", "starting"):
            return lifecycle.status
        if not self._container_is_running(container) or now - lifecycle.last_used_at >= self.idle_timeout_seconds:
            return "idle"
        return "ready"

    def _to_handle(
        self,
        metadata: _DockerWorkerMetadata,
        container: _DockerContainer | None,
        *,
        now: float,
        paths: _DockerWorkerPaths,
    ) -> WorkerHandle:
        # Only a running container publishes a port, so any other worker has no endpoint.
        host_port = self._container_host_port(container) if self._container_is_running(container) else None
        endpoint = "" if host_port is None else f"{self._endpoint_root(host_port)}{SANDBOX_RUNNER_API_ROOT}/execute"
        return worker_handle(
            worker_id=metadata.worker_id,
            worker_key=metadata.worker_key,
            lifecycle=metadata.lifecycle,
            endpoint=endpoint,
            auth_token=self._control_token(metadata.worker_key),
            status=self._effective_status(metadata, container, now=now),
            backend_name=self.backend_name,
            debug_metadata={
                "container_name": metadata.container_name,
                "container_id": "" if container is None else container.id,
                "host_port": str(host_port or ""),
                "state_root": str(paths.state.root),
                "api_root": endpoint.removesuffix("/execute").rstrip("/"),
            },
        )
