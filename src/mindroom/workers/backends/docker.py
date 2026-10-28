"""Docker-backed worker backend for dedicated local worker containers."""

from __future__ import annotations

import hashlib
import hmac
import importlib
import json
import math
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Protocol, cast

import httpx

from mindroom.constants import (
    RuntimePaths,
    resolve_primary_runtime_paths,
    runtime_env_values,
    runtime_paths_with_config_path,
    runtime_paths_with_storage_root,
    sandbox_startup_manifest_path,
    startup_manifest_sha256,
    write_startup_manifest,
)
from mindroom.credentials.manager import (
    CredentialsManager,
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
    mirrored_credential_services,
    required_launch,
    resolve_launch,
)
from mindroom.workers.backends._dedicated_worker_common import (
    plan_scoped_workspace_mounts,
    plan_shared_directory_mounts,
    resolve_state_scope_worker_key,
    validate_dedicated_worker_extra_env,
    validate_unique_worker_visible_paths,
)
from mindroom.workers.backends._lifecycle import (
    initial_worker_lifecycle_state,
    mark_worker_failed,
    mark_worker_idle,
    mark_worker_ready,
    prepare_worker_ensure_lifecycle,
    read_lifecycle_state,
    touch_worker_lifecycle,
    write_lifecycle_state,
)
from mindroom.workers.backends._metadata_store import (
    list_worker_state_paths,
    load_worker_metadata,
    save_worker_metadata,
)
from mindroom.workers.backends.dedicated_launch import (
    DedicatedWorkerLayout,
    dedicated_worker_runner_env,
    dedicated_worker_startup_runtime_paths,
)
from mindroom.workers.backends.docker_config import (
    DEFAULT_WORKER_PORT,
    DOCKER_HOST_ALIAS,
    DOCKER_RESERVED_EXTRA_ENV_NAMES,
    DockerWorkerBackendConfig,
    docker_backend_config_signature,
    docker_workers_root,
    normalize_docker_name_prefix,
    resolve_docker_storage_path,
)
from mindroom.workers.backends.docker_projection import PROJECTED_CONFIGS_DIRNAME, DockerProjectionManager
from mindroom.workers.backends.legacy_docker_worker_metadata import legacy_docker_worker_keys
from mindroom.workers.backends.local import LocalWorkerStatePaths, local_worker_state_paths_for_root
from mindroom.workers.backends.worker_security import (
    docker_worker_security_options,
    docker_worker_security_policy_signature,
)
from mindroom.workers.compatibility import WorkerProtocolMismatch, ready_worker_protocol_mismatch
from mindroom.workers.models import (
    ProgressSink,
    WorkerHandle,
    WorkerReadyPhase,
    WorkerReadyProgress,
    WorkerSpec,
    WorkerStatus,
)
from mindroom.workers.worker_retirement import open_worker_state_root

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from mindroom.workers.models import WorkerLaunchConfig

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
# Kept out of the launch identity: the worker image that supports minimal mode changes it anyway.
_WORKER_EXTRA_HOSTS = {DOCKER_HOST_ALIAS: "host-gateway"}

# Backend-owned control state lives beside the worker roots, never inside one.
# Each worker root is bind-mounted read-write into its own container, so any
# lifecycle state kept there would be rewritable by the untrusted tool code the
# container runs.
_WORKER_CONTROL_DIRNAME = ".mindroom-worker-control"

_LABEL_COMPONENT = "mindroom.ai/component"
_LABEL_COMPONENT_VALUE = "worker"
_LABEL_MANAGED_BY = "app.mindroom.ai/managed-by"
_LABEL_MANAGED_BY_VALUE = "mindroom"
_LABEL_NAME = "app.mindroom.ai/name"
_LABEL_NAME_VALUE = "mindroom-docker-worker"
_LABEL_WORKER_ID = "mindroom.ai/worker-id"
_LABEL_LAUNCH_CONFIG_HASH = "mindroom.ai/launch-config-hash"
_LABEL_RUNTIME_NAMESPACE = "mindroom.ai/runtime-namespace"
# Containers that mount only workspaces; earlier releases mounted whole state roots.
LABEL_STORAGE_LAYOUT = "mindroom.ai/storage-layout"
LABEL_STORAGE_LAYOUT_VALUE = "workspaces"

# A worker container name is the prefix, a dash, and a digest, in at most 63 characters.
_CONTAINER_NAME_DIGEST_LENGTH = 24
_CONTAINER_NAME_PREFIX_MAX_LENGTH = 63 - 1 - _CONTAINER_NAME_DIGEST_LENGTH

_DOCKER_DEPENDENCIES = ["docker"]
_DOCKER_EXTRA = "docker"

__all__ = [
    "LABEL_STORAGE_LAYOUT",
    "LABEL_STORAGE_LAYOUT_VALUE",
    "DockerWorkerBackend",
    "check_docker_workers_absent_for_storage_upgrade",
    "docker_backend_config_signature",
    "ensure_docker_dependencies",
    "list_docker_worker_containers",
]


def _docker_seccomp_profile_matches(options: list[str]) -> bool:
    seccomp_options = [option for option in options if option.lower().startswith("seccomp=")]
    if len(options) != 2 or len(seccomp_options) != 1:
        return False
    actual_profile_json = seccomp_options[0].split("=", 1)[1]
    expected_profile_json = docker_worker_security_options()[1].split("=", 1)[1]
    try:
        return json.loads(actual_profile_json) == json.loads(expected_profile_json)
    except (json.JSONDecodeError, TypeError):
        return False


def _container_root_filesystem_read_only(container: _DockerContainer) -> bool:
    host_config = container.attrs.get("HostConfig")
    return isinstance(host_config, dict) and cast("dict[str, object]", host_config).get("ReadonlyRootfs") is True


def _docker_security_options_match(value: object) -> bool:
    if not isinstance(value, list) or not all(isinstance(option, str) for option in value):
        return False
    options = cast("list[str]", value)
    no_new_privileges_options = [
        option for option in options if option.lower() in {"no-new-privileges", "no-new-privileges:true"}
    ]
    if len(no_new_privileges_options) != 1:
        return False
    return _docker_seccomp_profile_matches(options)


def _protected_runtime_env_names(runtime_paths: RuntimePaths) -> frozenset[str]:
    """Return the primary's secret-path variables, whose worker-side values operator env must not replace."""
    protected_names = {
        env_name
        for env_name in {*runtime_paths.process_env, *runtime_paths.env_file_values}
        if env_name.endswith("_FILE")
    }
    if runtime_paths.env_value("GOOGLE_APPLICATION_CREDENTIALS"):
        protected_names.add("GOOGLE_APPLICATION_CREDENTIALS")
    return frozenset(protected_names)


def _runtime_namespace_for_workers_root(workers_root: Path) -> str:
    resolved_workers_root = workers_root.expanduser().resolve()
    return hashlib.sha256(str(resolved_workers_root).encode("utf-8")).hexdigest()[:12]


def _container_name_prefix(prefix: str) -> str:
    """Return the prefix every worker container name starts with: the normalized prefix, cut to the name cap."""
    return normalize_docker_name_prefix(prefix)[:_CONTAINER_NAME_PREFIX_MAX_LENGTH].rstrip("-")


def _container_name_for_worker(worker_key: str, *, prefix: str, runtime_namespace: str) -> str:
    digest = hashlib.sha256(f"{runtime_namespace}:{worker_key}".encode()).hexdigest()[:_CONTAINER_NAME_DIGEST_LENGTH]
    return f"{_container_name_prefix(prefix)}-{digest}"


def _docker_image_identity_state(
    image: str,
    *,
    client: _DockerClient,
    docker_errors: _DockerErrors,
) -> tuple[str, bool]:
    try:
        docker_image = client.images.get(image)
    except docker_errors.NotFound:
        return image, False
    except docker_errors.DockerException:
        return image, False

    image_id = docker_image.id
    if isinstance(image_id, str) and image_id.strip():
        return image_id, True
    return image, False


def ensure_docker_dependencies(runtime_paths: RuntimePaths | None = None) -> None:
    """Install the optional Docker SDK runtime when needed."""
    effective_runtime_paths = runtime_paths or resolve_primary_runtime_paths(process_env=dict(os.environ))
    try:
        ensure_optional_deps(
            _DOCKER_DEPENDENCIES,
            _DOCKER_EXTRA,
            effective_runtime_paths,
        )
    except ImportError as exc:
        raise WorkerBackendError(str(exc)) from exc


def _load_docker_client_and_errors(
    *,
    runtime_paths: RuntimePaths | None = None,
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
        kwargs: dict[str, object] = {}
        if runtime_paths is not None:
            kwargs["environment"] = runtime_env_values(runtime_paths)
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
    worker_id: str
    worker_key: str
    endpoint: str
    backend_name: str
    container_name: str
    created_at: float
    last_used_at: float
    status: WorkerStatus
    host_port: int | None = None
    container_id: str | None = None
    image: str | None = None
    publish_host: str | None = None
    worker_port: int = DEFAULT_WORKER_PORT
    last_started_at: float | None = None
    startup_count: int = 0
    failure_count: int = 0
    failure_reason: str | None = None
    launch_config_hash: str | None = None
    # The container's startup manifest is written once, when it is created, so its hash tells whether it is current.
    startup_manifest_hash: str | None = None


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

    workers_root = docker_workers_root(resolve_docker_storage_path(runtime_paths=runtime_paths))
    runtime_namespace = _runtime_namespace_for_workers_root(workers_root)
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
            filters={"label": [f"{_LABEL_RUNTIME_NAMESPACE}={runtime_namespace}"]},
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
    workers_root = docker_workers_root(resolve_docker_storage_path(runtime_paths=runtime_paths))
    client, _docker_errors = _load_docker_client_and_errors(runtime_paths=runtime_paths)
    return client.containers.list(
        all=True,
        filters={"label": [f"{_LABEL_RUNTIME_NAMESPACE}={_runtime_namespace_for_workers_root(workers_root)}"]},
    )


@dataclass(frozen=True, slots=True)
class _DockerLaunchConfig:
    image_reference: str
    launch_config_hash: str
    image_resolved: bool
    tool_snapshot_digest: str


class DockerWorkerBackend:
    """Docker-backed worker provider for dedicated local sandbox-runner containers."""

    backend_name = "docker"
    cleanup_locator: str | None = None

    def __init__(
        self,
        *,
        config: DockerWorkerBackendConfig,
        auth_token: str | None,
        current_launch: Callable[[], WorkerLaunchConfig],
        storage_path: Path | None = None,
        runtime_paths: RuntimePaths | None = None,
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
        self._storage_path = resolve_docker_storage_path(storage_path, runtime_paths=runtime_paths)
        self._workers_root = docker_workers_root(self._storage_path)
        self._control_root = self._workers_root / _WORKER_CONTROL_DIRNAME
        base_runtime_paths = (
            resolve_primary_runtime_paths(
                config_path=config.host_config_path,
                storage_path=self._storage_path,
                process_env=dict(os.environ),
            )
            if runtime_paths is None
            else runtime_paths
        )
        if config.host_config_path is not None:
            base_runtime_paths = runtime_paths_with_config_path(base_runtime_paths, config.host_config_path)
        self._runtime_paths = runtime_paths_with_storage_root(base_runtime_paths, self._storage_path)
        config.validate_runtime_security(self._runtime_paths)
        self._client, self._docker_errors = _load_docker_client_and_errors(runtime_paths=self._runtime_paths)
        self._worker_locks: dict[str, threading.Lock] = {}
        self._worker_locks_lock = threading.Lock()
        self._metadata_lock = threading.Lock()
        self._projection_manager = DockerProjectionManager(
            config=config,
            projected_configs_root=self._workers_root / PROJECTED_CONFIGS_DIRNAME,
            runtime_paths=self._runtime_paths,
        )
        if runtime_paths is None:
            self._credentials_manager = CredentialsManager(base_path=self._storage_path / "credentials")
        else:
            self._credentials_manager = get_runtime_credentials_manager(self._runtime_paths)
        self._runtime_namespace = _runtime_namespace_for_workers_root(self._workers_root)
        self._workers_root.mkdir(parents=True, exist_ok=True)
        self._adopt_legacy_worker_records()

    @classmethod
    def from_runtime(
        cls,
        runtime_paths: RuntimePaths,
        *,
        auth_token: str | None,
        current_launch: Callable[[], WorkerLaunchConfig],
        storage_path: Path | None = None,
    ) -> DockerWorkerBackend:
        """Construct a backend instance from one explicit runtime context."""
        return cls(
            config=DockerWorkerBackendConfig.from_runtime(runtime_paths),
            auth_token=auth_token,
            current_launch=current_launch,
            storage_path=storage_path,
            runtime_paths=runtime_paths,
        )

    def shutdown(self) -> None:
        """Remove backend-owned containers before discarding this Docker manager."""
        failures: list[str] = []
        for paths in self._metadata_paths():
            metadata = self._load_metadata(paths)
            if metadata is None:
                continue
            with self._worker_lock(metadata.worker_key):
                metadata = self._load_metadata(paths)
                if metadata is None:
                    continue
                try:
                    self._remove_container(self._read_owned_container(metadata.worker_key))
                except WorkerBackendError as exc:
                    failures.append(str(exc))
                    continue
                write_lifecycle_state(
                    metadata,
                    mark_worker_idle(read_lifecycle_state(metadata)),
                )
                metadata.endpoint = self._endpoint_for_host_port(None)
                metadata.host_port = None
                metadata.container_id = None
                metadata.launch_config_hash = None
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
        start_time = time.monotonic()

        def emit_progress(phase: WorkerReadyPhase, *, error: str | None = None) -> None:
            if progress_sink is None:
                return
            progress_sink(
                WorkerReadyProgress(
                    phase=phase,
                    worker_key=spec.worker_key,
                    backend_name=self.backend_name,
                    elapsed_seconds=max(0.0, time.monotonic() - start_time),
                    error=error,
                ),
            )

        with self._worker_lock(spec.worker_key):
            spec, launch = resolve_launch(spec, self._current_launch)
            # Docker creates missing bind sources as root, so shared workspaces exist before any plan.
            plan_scoped_workspace_mounts(
                worker_key=resolve_state_scope_worker_key(spec.worker_key, spec.state_scope_worker_key),
                local_shared_storage_root=self._storage_path,
                worker_visible_shared_storage_root=Path(self.config.storage_mount_path),
                private_agent_names=spec.private_agent_names,
                resolved_agent_policies=launch.agent_policies,
                create_shared=True,
            )
            launch_config = self._resolve_launch_config(launch)
            paths = self._worker_paths(spec.worker_key)
            metadata = self._load_metadata(paths, expected_worker_key=spec.worker_key) or self._default_metadata(
                spec.worker_key,
                timestamp,
                launch_config_hash=launch_config.launch_config_hash,
            )
            identity_changed = self._sync_metadata_identity(metadata)

            should_restart = identity_changed or self._should_restart(
                metadata,
                paths,
                spec=spec,
                launch_config=launch_config,
            )
            lifecycle_state = prepare_worker_ensure_lifecycle(
                read_lifecycle_state(metadata),
                now=timestamp,
                should_restart=should_restart,
            )
            write_lifecycle_state(metadata, lifecycle_state)
            self._save_metadata(paths, metadata)
            cold_start_emitted = lifecycle_state.status == "starting"
            if cold_start_emitted:
                emit_progress("cold_start")

            sync_shared_credentials_to_worker(
                spec.worker_key,
                allowed_services=mirrored_credential_services(spec),
                credentials_manager=self._credentials_manager,
            )

            try:
                container = self._ensure_container(
                    metadata,
                    paths,
                    spec=spec,
                    launch_config=launch_config,
                )
                try:
                    endpoint = self._wait_for_ready(container)
                except _WorkerImageIncompatibleError as stale_error:
                    write_lifecycle_state(
                        metadata,
                        prepare_worker_ensure_lifecycle(
                            read_lifecycle_state(metadata),
                            now=timestamp,
                            should_restart=True,
                        ),
                    )
                    self._save_metadata(paths, metadata)
                    if not cold_start_emitted:
                        emit_progress("cold_start")
                    container, launch_config = self._relaunch_after_stale_image(
                        metadata,
                        paths,
                        spec=spec,
                        launch_config=launch_config,
                        mismatch=stale_error.mismatch,
                    )
                    endpoint = self._wait_for_ready(container, replacing=True)
            except Exception as exc:
                failure_reason = str(exc)
                self._record_failure_locked(paths, metadata, failure_reason, now=timestamp, stop_container=True)
                emit_progress("failed", error=failure_reason)
                if isinstance(exc, WorkerBackendError):
                    raise
                raise WorkerBackendError(failure_reason) from exc

            write_lifecycle_state(
                metadata,
                mark_worker_ready(read_lifecycle_state(metadata), now=timestamp),
            )
            metadata.endpoint = endpoint
            metadata.host_port = self._container_host_port(container)
            metadata.container_id = self._container_id(container)
            metadata.image = self.config.image
            metadata.publish_host = self.config.publish_host
            metadata.worker_port = self.config.worker_port
            metadata.launch_config_hash = launch_config.launch_config_hash
            self._save_metadata(paths, metadata)
            handle = self._to_handle(metadata, container, now=timestamp, paths=paths)
            emit_progress("ready")
            return handle

    def touch_worker(self, worker_key: str, *, now: float | None = None) -> WorkerHandle | None:
        """Refresh last-used metadata for one existing worker."""
        timestamp = time.time() if now is None else now
        with self._worker_lock(worker_key):
            paths = self._worker_paths(worker_key)
            metadata = self._load_metadata(paths, expected_worker_key=worker_key)
            if metadata is None:
                return None
            write_lifecycle_state(
                metadata,
                touch_worker_lifecycle(read_lifecycle_state(metadata), now=timestamp),
            )
            container = self._read_owned_container(metadata.worker_key)
            metadata = self._reconcile_missing_container_metadata(paths, metadata, container)
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
            with self._worker_lock(metadata.worker_key):
                metadata = self._load_metadata(paths)
                if metadata is None:
                    continue
                container = self._read_owned_container(metadata.worker_key)
                metadata = self._reconcile_missing_container_metadata(paths, metadata, container)
                handle = self._to_handle(
                    metadata,
                    container,
                    now=timestamp,
                    paths=paths,
                )
            if include_idle or handle.status != "idle":
                handles.append(handle)
        return sorted(handles, key=lambda handle: handle.last_used_at, reverse=True)

    def cleanup_idle_workers(self, *, now: float | None = None) -> list[WorkerHandle]:
        """Stop idle containers while retaining worker-owned state."""
        timestamp = time.time() if now is None else now
        cleaned: list[WorkerHandle] = []
        for paths in self._metadata_paths():
            metadata = self._load_metadata(paths)
            if metadata is None:
                continue
            with self._worker_lock(metadata.worker_key):
                metadata = self._load_metadata(paths)
                if metadata is None:
                    continue
                container = self._read_owned_container(metadata.worker_key)
                metadata = self._reconcile_missing_container_metadata(paths, metadata, container)
                handle = self._to_handle(metadata, container, now=timestamp, paths=paths)
                idle_timed_out = timestamp - metadata.last_used_at >= self.idle_timeout_seconds
                if handle.status == "idle" and self._container_is_running(container):
                    self._stop_container(container)
                    write_lifecycle_state(
                        metadata,
                        mark_worker_idle(read_lifecycle_state(metadata)),
                    )
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
                    self._reconcile_missing_container_metadata(paths, metadata, None)
        return sorted(cleaned, key=lambda handle: handle.last_used_at, reverse=True)

    def retire_worker(self, worker_key: str) -> None:
        """Remove one exact Docker worker container and all backend-owned state."""
        with self._worker_lock(worker_key):
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
        with self._worker_lock(worker_key):
            paths = self._worker_paths(worker_key)
            metadata = self._load_metadata(
                paths,
                expected_worker_key=worker_key,
            ) or self._default_metadata(worker_key, timestamp)
            if startup_count is not None and metadata.startup_count != startup_count:
                # The request failed on a container this worker has since replaced.
                return self._to_handle(metadata, None, now=timestamp, paths=paths)
            return self._record_failure_locked(paths, metadata, failure_reason, now=timestamp, stop_container=True)

    def _adopt_legacy_worker_records(self) -> None:
        """Give workers from before the control directory a fresh backend-owned record.

        The record has no launch identity, so the next ensure replaces the worker's container.
        """
        worker_keys = legacy_docker_worker_keys(self._workers_root, control_root=self._control_root)
        if not worker_keys:
            return
        now = time.time()
        for worker_key in worker_keys:
            metadata = self._default_metadata(worker_key, now)
            write_lifecycle_state(metadata, mark_worker_idle(read_lifecycle_state(metadata)))
            self._save_metadata(self._worker_paths(worker_key), metadata)

    def _worker_lock(self, worker_key: str) -> threading.Lock:
        with self._worker_locks_lock:
            worker_lock = self._worker_locks.get(worker_key)
            if worker_lock is None:
                worker_lock = threading.Lock()
                self._worker_locks[worker_key] = worker_lock
        return worker_lock

    def _worker_paths(self, worker_key: str) -> _DockerWorkerPaths:
        return _docker_worker_paths(self._workers_root, worker_dir_name(worker_key))

    def _default_metadata(
        self,
        worker_key: str,
        now: float,
        *,
        launch_config_hash: str | None = None,
    ) -> _DockerWorkerMetadata:
        worker_id = self._container_name_for_worker(worker_key)
        lifecycle = initial_worker_lifecycle_state(now=now)
        return _DockerWorkerMetadata(
            worker_id=worker_id,
            worker_key=worker_key,
            endpoint=self._endpoint_for_host_port(None),
            backend_name=self.backend_name,
            container_name=worker_id,
            created_at=lifecycle.created_at,
            last_used_at=lifecycle.last_used_at,
            status=lifecycle.status,
            image=self.config.image,
            publish_host=self.config.publish_host,
            worker_port=self.config.worker_port,
            launch_config_hash=launch_config_hash,
        )

    def _container_name_for_worker(self, worker_key: str) -> str:
        return _container_name_for_worker(
            worker_key,
            prefix=self.config.name_prefix,
            runtime_namespace=self._runtime_namespace,
        )

    def _sync_metadata_identity(self, metadata: _DockerWorkerMetadata) -> bool:
        expected_container_name = self._container_name_for_worker(metadata.worker_key)
        if metadata.container_name == expected_container_name and metadata.worker_id == expected_container_name:
            return False

        if metadata.container_name != expected_container_name:
            # The recorded name is only a hint for reaping the container this
            # backend started for this worker key under an earlier naming config;
            # anything else answering to that name is left untouched.
            self._remove_container(
                self._read_owned_container_named(metadata.container_name, worker_key=metadata.worker_key),
            )
        metadata.worker_id = expected_container_name
        metadata.container_name = expected_container_name
        metadata.endpoint = self._endpoint_for_host_port(None)
        metadata.host_port = None
        metadata.container_id = None
        metadata.launch_config_hash = None
        return True

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

    def _reconcile_missing_container_metadata(
        self,
        paths: _DockerWorkerPaths,
        metadata: _DockerWorkerMetadata,
        container: _DockerContainer | None,
    ) -> _DockerWorkerMetadata:
        if container is not None:
            return metadata
        if metadata.host_port is None and metadata.container_id is None:
            return metadata
        metadata.endpoint = self._endpoint_for_host_port(None)
        metadata.host_port = None
        metadata.container_id = None
        self._save_metadata(paths, metadata)
        return metadata

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
        labels = self._container_config_labels(container)
        return (
            labels.get(_LABEL_RUNTIME_NAMESPACE) == self._runtime_namespace
            and labels.get(_LABEL_WORKER_ID) == container_name
            and self._container_env_matches(container, expected_env={_DEDICATED_WORKER_KEY_ENV: worker_key})
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

    def _should_restart(
        self,
        metadata: _DockerWorkerMetadata,
        paths: _DockerWorkerPaths,
        *,
        spec: WorkerSpec,
        launch_config: _DockerLaunchConfig,
    ) -> bool:
        container = self._require_owned_container(metadata.worker_key)
        if metadata.status == "failed":
            return True
        if container is None:
            return True
        if (
            self._current_container_mounts(
                metadata,
                container,
                paths,
                spec=spec,
                launch_config=launch_config,
            )
            is None
        ):
            return True
        return not self._container_is_running(container)

    def _current_container_mounts(
        self,
        metadata: _DockerWorkerMetadata,
        container: _DockerContainer,
        paths: _DockerWorkerPaths,
        *,
        spec: WorkerSpec,
        launch_config: _DockerLaunchConfig,
    ) -> list[tuple[Path, str, bool]] | None:
        """Return the bind mounts of a container that matches the current config, or None when it must be replaced."""
        compatible_launch_config_hashes = self._compatible_launch_config_hashes(container, launch_config)
        if metadata.launch_config_hash not in compatible_launch_config_hashes:
            return None
        if self._container_launch_config_hash(container) not in compatible_launch_config_hashes:
            return None
        if not _container_root_filesystem_read_only(container) or (
            self.config.security_policy == "computer" and not self._container_runtime_security_matches(container)
        ):
            return None

        storage_mounts = self._scoped_storage_mount_specs(spec)
        config_mount_specs, projection = self._projection_manager.config_mount_specs(
            paths.state,
            launch=required_launch(spec),
            worker_key=metadata.worker_key,
            materialize_projection=False,
            storage_mounts=storage_mounts,
        )
        if projection is not None and not projection.ready:
            return None

        if not self._container_env_matches(
            container,
            expected_env=self._container_env(metadata.worker_key),
        ) or metadata.startup_manifest_hash != self._startup_manifest_hash(spec):
            return None

        mount_checks = list(self._worker_root_mount_specs(paths.state))
        mount_checks.extend(storage_mounts)
        mount_checks.extend(config_mount_specs)
        return mount_checks if self._container_mount_layout_matches(container, expected_mounts=mount_checks) else None

    def _container_runtime_security_matches(self, container: _DockerContainer) -> bool:
        host_config = container.attrs.get("HostConfig")
        if not isinstance(host_config, dict):
            return False
        host_config = cast("dict[str, object]", host_config)
        cap_add = host_config.get("CapAdd")
        cap_drop = host_config.get("CapDrop")
        return (
            host_config.get("Privileged") is False
            and (cap_add is None or (isinstance(cap_add, list) and not cap_add))
            and isinstance(cap_drop, list)
            and [str(cap).upper() for cap in cap_drop] == ["ALL"]
            and _docker_security_options_match(host_config.get("SecurityOpt"))
        )

    def _ensure_container(
        self,
        metadata: _DockerWorkerMetadata,
        paths: _DockerWorkerPaths,
        *,
        spec: WorkerSpec,
        launch_config: _DockerLaunchConfig,
    ) -> _DockerContainer:
        paths.state.root.mkdir(parents=True, exist_ok=True)
        container_name = self._container_name_for_worker(metadata.worker_key)
        container = self._require_owned_container(metadata.worker_key)
        if container is not None:
            mounts = self._current_container_mounts(
                metadata,
                container,
                paths,
                spec=spec,
                launch_config=launch_config,
            )
            if mounts is None:
                self._remove_container(container)
                container = None
            elif not self._container_is_running(container):
                # Docker resolves every bind destination again on start, through whatever worker code left in its root.
                self._prepare_nested_storage_mount_targets(paths, (mount[1] for mount in mounts))
                try:
                    container.start()
                except self._docker_errors.DockerException as exc:
                    msg = f"Failed to start Docker worker '{container_name}': {exc}"
                    raise WorkerBackendError(msg) from exc

        if container is None:
            metadata.startup_manifest_hash = self._write_startup_manifest(paths, spec=spec)
            volumes = self._container_volumes(paths, spec=spec)
            self._prepare_nested_storage_mount_targets(paths, (volume.rsplit(":", 2)[1] for volume in volumes))
            security_kwargs = (
                {"cap_drop": ["ALL"], "security_opt": docker_worker_security_options()}
                if self.config.security_policy == "computer"
                else {}
            )
            container = self._client.containers.run(
                launch_config.image_reference,
                command=["/app/run-sandbox-runner.sh"],
                name=container_name,
                detach=True,
                environment=self._container_env(metadata.worker_key),
                volumes=volumes,
                ports={f"{self.config.worker_port}/tcp": (self.config.publish_host, None)},
                labels=self._container_labels(
                    container_name,
                    launch_config_hash=launch_config.launch_config_hash,
                ),
                user=self.config.user,
                read_only=True,
                tmpfs=_WORKER_TMPFS,
                extra_hosts=_WORKER_EXTRA_HOSTS,
                runtime=self.config.runtime,
                **security_kwargs,
            )

        self._reload_container(container)
        if self._container_host_port(container) is None:
            msg = f"Docker worker '{container_name}' is missing a published port."
            raise WorkerBackendError(msg)
        return container

    def _relaunch_after_stale_image(
        self,
        metadata: _DockerWorkerMetadata,
        paths: _DockerWorkerPaths,
        *,
        spec: WorkerSpec,
        launch_config: _DockerLaunchConfig,
        mismatch: WorkerProtocolMismatch,
    ) -> tuple[_DockerContainer, _DockerLaunchConfig]:
        """Relaunch once after a protocol mismatch, first pulling the configured image when the worker is older.

        A stale local image otherwise fails every worker start until someone
        pulls by hand, since tag references like ``:latest`` keep resolving to
        the local image. A newer worker needs a newer runtime, which no pull provides.
        """
        if not mismatch.worker_is_newer:
            try:
                pulled_image = self._client.images.pull(self.config.image)
            except self._docker_errors.DockerException as exc:
                msg = f"{mismatch.error} Pulling '{self.config.image}' failed: {exc}"
                raise WorkerBackendError(msg) from exc
            launch_config = self._launch_config_from_image_identity(
                pulled_image.id,
                image_resolved=True,
                tool_snapshot_digest=launch_config.tool_snapshot_digest,
            )
            metadata.launch_config_hash = launch_config.launch_config_hash
            self._save_metadata(paths, metadata)
        container = self._require_owned_container(metadata.worker_key)
        self._stop_container(container)
        self._remove_container(container)
        return (
            self._ensure_container(
                metadata,
                paths,
                spec=spec,
                launch_config=launch_config,
            ),
            launch_config,
        )

    def _reload_container(self, container: _DockerContainer) -> None:
        try:
            container.reload()
        except self._docker_errors.DockerException as exc:
            msg = f"Failed to refresh Docker worker state: {exc}"
            raise WorkerBackendError(msg) from exc

    def _wait_for_ready(self, container: _DockerContainer, *, replacing: bool = False) -> str:
        host_port = self._container_host_port(container)
        if host_port is None:
            msg = "Docker worker is missing a published port."
            raise WorkerBackendError(msg)

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
                        backend_label="Docker",
                        image=self.config.image,
                        replacing=replacing,
                    )
                    if mismatch is not None:
                        raise _WorkerImageIncompatibleError(mismatch)
                    return f"{endpoint_root}/api/sandbox-runner/execute"

                if time.time() >= deadline:
                    msg = (
                        f"Docker worker did not become ready within {self.config.ready_timeout_seconds:.0f}s."
                        + self._container_logs_excerpt(container)
                    )
                    raise WorkerBackendError(msg)
                time.sleep(_READY_POLL_INTERVAL_SECONDS)

    def _record_failure_locked(
        self,
        paths: _DockerWorkerPaths,
        metadata: _DockerWorkerMetadata,
        failure_reason: str,
        *,
        now: float,
        stop_container: bool,
    ) -> WorkerHandle:
        container = self._read_owned_container(metadata.worker_key)
        if stop_container:
            self._stop_container(container)
        write_lifecycle_state(
            metadata,
            mark_worker_failed(
                read_lifecycle_state(metadata),
                now=now,
                failure_reason=failure_reason,
            ),
        )
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

    def _write_startup_manifest(self, paths: _DockerWorkerPaths, *, spec: WorkerSpec) -> str:
        """Persist primary validation state before creating one Docker worker, and return the manifest's hash."""
        runtime_paths = self._worker_runtime_paths(spec.worker_key)
        tool_validation_snapshot = required_launch(spec).tool_validation_snapshot
        write_startup_manifest(
            paths.state.root,
            runtime_paths,
            tool_validation_snapshot=tool_validation_snapshot,
            public_runtime=True,
        )
        return startup_manifest_sha256(
            runtime_paths,
            tool_validation_snapshot=tool_validation_snapshot,
            public_runtime=True,
        )

    def _startup_manifest_hash(self, spec: WorkerSpec) -> str:
        """Return the hash of the startup manifest a worker created now for ``spec`` would boot from."""
        return startup_manifest_sha256(
            self._worker_runtime_paths(spec.worker_key),
            tool_validation_snapshot=required_launch(spec).tool_validation_snapshot,
            public_runtime=True,
        )

    def _container_env_matches(
        self,
        container: _DockerContainer | None,
        *,
        expected_env: dict[str, str],
    ) -> bool:
        if container is None:
            return False
        attrs = container.attrs
        config = attrs.get("Config")
        if not isinstance(config, dict):
            return False
        raw_env = cast("dict[str, object]", config).get("Env")
        if not isinstance(raw_env, list):
            return False

        actual_env: dict[str, str] = {}
        for item in raw_env:
            if not isinstance(item, str) or "=" not in item:
                continue
            name, value = item.split("=", 1)
            actual_env[name] = value
        return all(actual_env.get(name) == value for name, value in expected_env.items())

    def _worker_runtime_paths(self, worker_key: str) -> RuntimePaths:
        validate_dedicated_worker_extra_env(
            self.config.extra_env,
            backend_name="Docker",
            extra_reserved_names=_protected_runtime_env_names(self._runtime_paths),
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

    def _container_volumes(self, paths: _DockerWorkerPaths, *, spec: WorkerSpec) -> list[str]:
        storage_mounts = self._scoped_storage_mount_specs(spec)
        config_mounts, _projection = self._projection_manager.config_mount_specs(
            paths.state,
            launch=required_launch(spec),
            worker_key=spec.worker_key,
            storage_mounts=storage_mounts,
        )
        return [
            f"{host_path}:{container_path}:{'ro' if read_only else 'rw'}"
            for host_path, container_path, read_only in (
                *self._worker_root_mount_specs(paths.state),
                *storage_mounts,
                *config_mounts,
            )
        ]

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

    def _scoped_storage_mount_specs(self, spec: WorkerSpec) -> list[tuple[Path, str, bool]]:
        resolved_agent_policies = required_launch(spec).agent_policies
        state_scope_key = resolve_state_scope_worker_key(spec.worker_key, spec.state_scope_worker_key)
        mount_specs = [
            (workspace_mount.local_path, str(workspace_mount.worker_visible_path), False)
            for workspace_mount in plan_scoped_workspace_mounts(
                worker_key=state_scope_key,
                local_shared_storage_root=self._storage_path,
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

    def _container_labels(
        self,
        container_name: str,
        *,
        launch_config_hash: str,
    ) -> dict[str, str]:
        labels = {
            _LABEL_COMPONENT: _LABEL_COMPONENT_VALUE,
            _LABEL_MANAGED_BY: _LABEL_MANAGED_BY_VALUE,
            _LABEL_NAME: _LABEL_NAME_VALUE,
            _LABEL_WORKER_ID: container_name,
            _LABEL_LAUNCH_CONFIG_HASH: launch_config_hash,
            _LABEL_RUNTIME_NAMESPACE: self._runtime_namespace,
            LABEL_STORAGE_LAYOUT: LABEL_STORAGE_LAYOUT_VALUE,
        }
        labels.update(self.config.extra_labels)
        return labels

    def _compute_launch_config_hash(self, *, image_identity: str, tool_snapshot_digest: str) -> str:
        # The worker's own projected config and assets restart it through the projection mount it compares,
        # so config edits that other workers or only the primary see never restart this one.
        config_payload: dict[str, object] = {
            "auth_token": self.auth_token or "",
            "config_path": self.config.config_path,
            "extra_env": self.config.extra_env,
            "extra_labels": self.config.extra_labels,
            "host_config_path": str(self.config.host_config_path or ""),
            "image": self.config.image,
            "resolved_image": image_identity,
            "name_prefix": self.config.name_prefix,
            "publish_host": self.config.publish_host,
            "storage_mount_path": self.config.storage_mount_path,
            "tool_snapshot_digest": tool_snapshot_digest,
            "workers_root": str(self._workers_root),
            "user": self.config.user or "",
            "worker_port": self.config.worker_port,
        }
        if self.config.security_policy == "computer":
            config_payload["runtime_security"] = docker_worker_security_policy_signature()
        if self.config.runtime is not None:
            config_payload["runtime"] = self.config.runtime
        normalized = json.dumps(config_payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def _resolve_launch_config(self, launch: WorkerLaunchConfig) -> _DockerLaunchConfig:
        image_identity, image_resolved = _docker_image_identity_state(
            self.config.image,
            client=self._client,
            docker_errors=self._docker_errors,
        )
        return self._launch_config_from_image_identity(
            image_identity,
            image_resolved=image_resolved,
            tool_snapshot_digest=launch.tool_snapshot_digest,
        )

    def _launch_config_from_image_identity(
        self,
        image_identity: str,
        *,
        image_resolved: bool,
        tool_snapshot_digest: str,
    ) -> _DockerLaunchConfig:
        return _DockerLaunchConfig(
            image_reference=image_identity if image_resolved else self.config.image,
            launch_config_hash=self._compute_launch_config_hash(
                image_identity=image_identity,
                tool_snapshot_digest=tool_snapshot_digest,
            ),
            image_resolved=image_resolved,
            tool_snapshot_digest=tool_snapshot_digest,
        )

    def _container_config_labels(self, container: _DockerContainer) -> dict[str, str]:
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

    def _container_launch_config_hash(self, container: _DockerContainer | None) -> str | None:
        if container is None:
            return None
        return self._container_config_labels(container).get(_LABEL_LAUNCH_CONFIG_HASH) or None

    def _container_image_identity(self, container: _DockerContainer | None) -> str | None:
        if container is None:
            return None

        attrs = container.attrs
        raw_image = attrs.get("Image")
        if isinstance(raw_image, str) and raw_image.strip():
            return raw_image

        config = attrs.get("Config")
        if not isinstance(config, dict):
            return None
        config_image = cast("dict[str, object]", config).get("Image")
        if isinstance(config_image, str) and config_image.strip():
            return config_image
        return None

    def _compatible_launch_config_hashes(
        self,
        container: _DockerContainer | None,
        launch_config: _DockerLaunchConfig,
    ) -> set[str]:
        # One ensure uses one image snapshot; the next call resolves the tag again.
        current_image_identity = launch_config.image_reference
        compatible_hashes = {launch_config.launch_config_hash}
        container_image_identity = self._container_image_identity(container)
        if container_image_identity is None:
            return compatible_hashes

        if not launch_config.image_resolved:
            compatible_hashes.add(
                self._compute_launch_config_hash(
                    image_identity=container_image_identity,
                    tool_snapshot_digest=launch_config.tool_snapshot_digest,
                ),
            )
            return compatible_hashes

        if container_image_identity == current_image_identity:
            compatible_hashes.add(
                self._compute_launch_config_hash(
                    image_identity=self.config.image,
                    tool_snapshot_digest=launch_config.tool_snapshot_digest,
                ),
            )
        return compatible_hashes

    def _container_mount_layout_matches(
        self,
        container: _DockerContainer,
        *,
        expected_mounts: list[tuple[Path, str, bool]],
    ) -> bool:
        attrs = container.attrs
        mounts = attrs.get("Mounts", [])
        if not isinstance(mounts, list):
            return False

        actual_layout: set[tuple[str, str, bool]] = set()
        for mount in mounts:
            if not isinstance(mount, dict):
                return False
            mount_data = cast("dict[str, object]", mount)
            mount_type = mount_data.get("Type")
            source = mount_data.get("Source")
            destination = mount_data.get("Destination")
            if mount_type != "bind" or not isinstance(source, str) or not isinstance(destination, str):
                continue
            writable = mount_data.get("RW")
            if isinstance(writable, bool):
                read_only = not writable
            else:
                mode = mount_data.get("Mode")
                read_only = "ro" in mode if isinstance(mode, str) else False
            actual_layout.add((str(Path(source).expanduser().resolve()), destination, read_only))

        expected_layout = {
            (str(host_path.expanduser().resolve()), container_path, read_only)
            for host_path, container_path, read_only in expected_mounts
        }
        return actual_layout == expected_layout

    def _container_status(self, container: _DockerContainer) -> str | None:
        status = container.status
        if isinstance(status, str):
            return status
        attrs = container.attrs
        state = attrs.get("State", {})
        if isinstance(state, dict):
            state_status = state.get("Status")
            if isinstance(state_status, str):
                return state_status
        return None

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
        if container is None:
            return False
        return self._container_status(container) == "running"

    def _container_host_port(self, container: _DockerContainer | None) -> int | None:
        host_port: int | None = None
        if container is None:
            return host_port

        attrs = container.attrs
        network_settings = attrs.get("NetworkSettings")
        if not isinstance(network_settings, dict):
            return host_port
        ports = cast("dict[str, object]", network_settings).get("Ports")
        bindings = (
            cast("dict[str, object]", ports).get(f"{self.config.worker_port}/tcp") if isinstance(ports, dict) else None
        )
        first_binding = bindings[0] if isinstance(bindings, list) and bindings else None
        raw_host_port = (
            cast("dict[str, object]", first_binding).get("HostPort") if isinstance(first_binding, dict) else None
        )
        if not isinstance(raw_host_port, str):
            return host_port
        try:
            host_port = int(raw_host_port)
        except (TypeError, ValueError):
            host_port = None
        return host_port

    def _container_id(self, container: _DockerContainer | None) -> str | None:
        if container is None:
            return None
        container_id = container.id
        return container_id if isinstance(container_id, str) and container_id else None

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

    def _endpoint_for_host_port(self, host_port: int | None) -> str:
        if host_port is None:
            return "/api/sandbox-runner/execute"
        return f"{self._endpoint_root(host_port)}/api/sandbox-runner/execute"

    def _effective_status(
        self,
        metadata: _DockerWorkerMetadata,
        container: _DockerContainer | None,
        *,
        now: float,
    ) -> WorkerStatus:
        if metadata.status == "failed":
            return "failed"

        if container is None or not self._container_is_running(container):
            return "idle" if metadata.status != "starting" else "starting"

        if metadata.status == "starting":
            return "starting"
        if now - metadata.last_used_at >= self.idle_timeout_seconds:
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
        host_port = self._container_host_port(container) or metadata.host_port
        endpoint = self._endpoint_for_host_port(host_port)
        return WorkerHandle(
            worker_id=metadata.worker_id,
            worker_key=metadata.worker_key,
            endpoint=endpoint,
            auth_token=self._control_token(metadata.worker_key),
            status=self._effective_status(metadata, container, now=now),
            backend_name=self.backend_name,
            last_used_at=metadata.last_used_at,
            created_at=metadata.created_at,
            last_started_at=metadata.last_started_at,
            expires_at=None,
            startup_count=metadata.startup_count,
            failure_count=metadata.failure_count,
            failure_reason=metadata.failure_reason,
            debug_metadata={
                "container_name": metadata.container_name,
                "container_id": self._container_id(container) or metadata.container_id or "",
                "host_port": str(host_port or ""),
                "state_root": str(paths.state.root),
                "api_root": endpoint.removesuffix("/execute").rstrip("/"),
            },
        )
