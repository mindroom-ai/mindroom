"""Docker-backed worker backend for dedicated local worker containers."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import shutil
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Protocol, cast

import httpx

from mindroom.constants import CONFIG_PATH, STORAGE_PATH_OBJ
from mindroom.credentials import SHARED_CREDENTIALS_PATH_ENV, CredentialsManager, sync_shared_credentials_to_worker
from mindroom.tool_system.dependencies import ensure_optional_deps
from mindroom.tool_system.worker_routing import is_unscoped_worker_key, worker_dir_name, worker_root_path
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.backends.local import LocalWorkerStatePaths, local_worker_state_paths_for_root
from mindroom.workers.models import WorkerHandle, WorkerSpec, WorkerStatus

if TYPE_CHECKING:
    from collections.abc import Callable

    class _DockerContainer(Protocol):
        attrs: dict[str, object]
        status: str
        id: str

        def reload(self) -> None: ...

        def start(self) -> None: ...

        def stop(self, timeout: int = 10) -> None: ...

        def remove(self, force: bool = True) -> None: ...

    class _DockerContainersApi(Protocol):
        def get(self, name: str) -> _DockerContainer: ...

        def run(self, image: str, **kwargs: object) -> _DockerContainer: ...

    class _DockerClient(Protocol):
        containers: _DockerContainersApi

    class _DockerErrors(Protocol):
        DockerException: type[Exception]
        NotFound: type[Exception]


_DEFAULT_IDLE_TIMEOUT_SECONDS = 1800.0
_DEFAULT_READY_TIMEOUT_SECONDS = 60.0
_DEFAULT_WORKER_PORT = 8766
_DEFAULT_STORAGE_MOUNT_PATH = "/app/worker"
_DEFAULT_CONFIG_PATH = "/app/config-host/config.yaml"
_DEFAULT_NAME_PREFIX = "mindroom-worker"
_DEFAULT_PUBLISH_HOST = "127.0.0.1"
_READY_POLL_INTERVAL_SECONDS = 1.0

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

_TOKEN_ENV_NAME = "MINDROOM_SANDBOX_PROXY_TOKEN"  # noqa: S105
_RUNNER_PORT_ENV_NAME = "MINDROOM_SANDBOX_RUNNER_PORT"
_DEDICATED_WORKER_KEY_ENV = "MINDROOM_SANDBOX_DEDICATED_WORKER_KEY"
_DEDICATED_WORKER_ROOT_ENV = "MINDROOM_SANDBOX_DEDICATED_WORKER_ROOT"

_LABEL_COMPONENT = "mindroom.ai/component"
_LABEL_COMPONENT_VALUE = "worker"
_LABEL_MANAGED_BY = "app.mindroom.ai/managed-by"
_LABEL_MANAGED_BY_VALUE = "mindroom"
_LABEL_NAME = "app.mindroom.ai/name"
_LABEL_NAME_VALUE = "mindroom-docker-worker"
_LABEL_WORKER_ID = "mindroom.ai/worker-id"
_LABEL_WORKER_KEY = "mindroom.ai/worker-key"
_LABEL_LAUNCH_CONFIG_HASH = "mindroom.ai/launch-config-hash"
_LABEL_RUNTIME_NAMESPACE = "mindroom.ai/runtime-namespace"

_DOCKER_DEPENDENCIES = ["docker"]
_DOCKER_EXTRA = "docker"


def _read_float_env(name: str, default: float) -> float:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError:
        value = default
    return max(1.0, value)


def _read_int_env(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError:
        value = default
    return max(1, value)


def _read_json_mapping_env(name: str) -> dict[str, str]:
    raw = os.getenv(name, "").strip()
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


def _read_host_config_path() -> Path | None:
    configured = os.getenv(_HOST_CONFIG_PATH_ENV, "").strip()
    if configured:
        resolved = Path(configured).expanduser().resolve()
        if not resolved.exists():
            msg = f"{_HOST_CONFIG_PATH_ENV} points to a missing file: {resolved}"
            raise WorkerBackendError(msg)
        return resolved
    if CONFIG_PATH.exists():
        return CONFIG_PATH.expanduser().resolve()
    return None


def _default_docker_user_for_os(os_name: str) -> str | None:
    if os_name == "posix":
        return f"{os.getuid()}:{os.getgid()}"
    if os_name == "nt":
        return None
    return None


def _default_docker_user() -> str | None:
    return _default_docker_user_for_os(os.name)


def _read_docker_user() -> str | None:
    raw_value = os.getenv(_USER_ENV)
    if raw_value is None:
        return _default_docker_user()
    normalized = raw_value.strip()
    return normalized or None


def _normalize_name_prefix(raw_value: str) -> str:
    normalized = re.sub(r"[^a-z0-9-]+", "-", raw_value.strip().lower()).strip("-")
    return normalized or _DEFAULT_NAME_PREFIX


def _runtime_namespace_for_workers_root(workers_root: Path) -> str:
    resolved_workers_root = workers_root.expanduser().resolve()
    return hashlib.sha256(str(resolved_workers_root).encode("utf-8")).hexdigest()[:12]


def _container_name_for_worker(worker_key: str, *, prefix: str, runtime_namespace: str) -> str:
    digest = hashlib.sha256(f"{runtime_namespace}:{worker_key}".encode()).hexdigest()[:24]
    normalized_prefix = _normalize_name_prefix(prefix)
    max_prefix_length = 63 - len(digest) - 1
    safe_prefix = normalized_prefix[:max_prefix_length].rstrip("-")
    if not safe_prefix:
        safe_prefix = _DEFAULT_NAME_PREFIX[:max_prefix_length].rstrip("-") or "worker"
    return f"{safe_prefix}-{digest}"


def _workers_root(base_storage_path: Path) -> Path:
    return worker_root_path(base_storage_path, "__mindroom_root__").parent


def _resolved_storage_path(storage_path: Path | None = None) -> Path:
    base_storage_path = STORAGE_PATH_OBJ if storage_path is None else storage_path
    return base_storage_path.expanduser().resolve()


def _container_config_dir(config_path: str) -> str:
    return str(PurePosixPath(config_path).parent)


def _container_config_dotenv_path(config_path: str) -> str:
    return str(PurePosixPath(_container_config_dir(config_path)) / ".env")


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
    def from_env(cls) -> _DockerWorkerBackendConfig:
        image = os.getenv(_IMAGE_ENV, "").strip()
        if not image:
            msg = f"{_IMAGE_ENV} must be set when {_WORKER_BACKEND_ENV}=docker."
            raise WorkerBackendError(msg)

        publish_host = os.getenv(_PUBLISH_HOST_ENV, _DEFAULT_PUBLISH_HOST).strip() or _DEFAULT_PUBLISH_HOST
        endpoint_host = os.getenv(_ENDPOINT_HOST_ENV, publish_host).strip() or publish_host
        return cls(
            image=image,
            worker_port=_read_int_env(_PORT_ENV, _DEFAULT_WORKER_PORT),
            storage_mount_path=os.getenv(_STORAGE_MOUNT_PATH_ENV, _DEFAULT_STORAGE_MOUNT_PATH).strip()
            or _DEFAULT_STORAGE_MOUNT_PATH,
            config_path=os.getenv(_CONFIG_PATH_ENV, _DEFAULT_CONFIG_PATH).strip() or _DEFAULT_CONFIG_PATH,
            host_config_path=_read_host_config_path(),
            idle_timeout_seconds=_read_float_env(_IDLE_TIMEOUT_ENV, _DEFAULT_IDLE_TIMEOUT_SECONDS),
            ready_timeout_seconds=_read_float_env(_READY_TIMEOUT_ENV, _DEFAULT_READY_TIMEOUT_SECONDS),
            name_prefix=os.getenv(_NAME_PREFIX_ENV, _DEFAULT_NAME_PREFIX).strip() or _DEFAULT_NAME_PREFIX,
            publish_host=publish_host,
            endpoint_host=endpoint_host,
            user=_read_docker_user(),
            extra_env=_read_json_mapping_env(_EXTRA_ENV_JSON_ENV),
            extra_labels=_read_json_mapping_env(_EXTRA_LABELS_JSON_ENV),
        )


def docker_backend_config_signature(
    *,
    auth_token: str | None,
    storage_path: Path | None = None,
) -> tuple[str, ...]:
    """Return a cache signature for one concrete Docker backend config."""
    config = _DockerWorkerBackendConfig.from_env()
    workers_root = _workers_root(_resolved_storage_path(storage_path))
    extra_env_json = json.dumps(config.extra_env, sort_keys=True, separators=(",", ":"))
    extra_labels_json = json.dumps(config.extra_labels, sort_keys=True, separators=(",", ":"))
    return (
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
        extra_env_json,
        extra_labels_json,
        auth_token or "",
    )


def _host_config_contents_hash(host_config_path: Path | None) -> str:
    if host_config_path is None:
        return ""
    try:
        return hashlib.sha256(host_config_path.read_bytes()).hexdigest()
    except OSError as exc:
        msg = f"Failed to read Docker worker config file '{host_config_path}': {exc}"
        raise WorkerBackendError(msg) from exc


def ensure_docker_dependencies() -> None:
    """Install the optional Docker SDK runtime when needed."""
    try:
        ensure_optional_deps(_DOCKER_DEPENDENCIES, _DOCKER_EXTRA)
    except ImportError as exc:
        raise WorkerBackendError(str(exc)) from exc


def _load_docker_client_and_errors() -> tuple[_DockerClient, _DockerErrors]:
    ensure_docker_dependencies()
    try:
        docker_module = importlib.import_module("docker")
        docker_errors = cast("_DockerErrors", importlib.import_module("docker.errors"))
    except ModuleNotFoundError as exc:
        msg = "The Docker worker backend could not import the Docker SDK after ensuring the optional 'docker' extra."
        raise WorkerBackendError(msg) from exc

    docker_from_env = cast("Callable[[], _DockerClient]", docker_module.from_env)
    try:
        client = docker_from_env()
    except docker_errors.DockerException as exc:
        msg = f"Failed to initialize Docker client: {exc}"
        raise WorkerBackendError(msg) from exc
    return client, docker_errors


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
    worker_port: int = _DEFAULT_WORKER_PORT
    last_started_at: float | None = None
    startup_count: int = 0
    failure_count: int = 0
    failure_reason: str | None = None
    launch_config_hash: str | None = None


class DockerWorkerBackend:
    """Docker-backed worker provider for dedicated local sandbox-runner containers."""

    backend_name = "docker"

    def __init__(
        self,
        *,
        config: _DockerWorkerBackendConfig,
        auth_token: str | None,
        storage_path: Path | None = None,
    ) -> None:
        if auth_token is None:
            msg = "A worker auth token is required for Docker workers."
            raise WorkerBackendError(msg)

        self.config = config
        self.auth_token = auth_token
        self.idle_timeout_seconds = config.idle_timeout_seconds
        self._client, self._docker_errors = _load_docker_client_and_errors()
        self._worker_locks: dict[str, threading.Lock] = {}
        self._worker_locks_lock = threading.Lock()
        self._metadata_lock = threading.Lock()
        self._storage_path = _resolved_storage_path(storage_path)
        self._workers_root = _workers_root(self._storage_path)
        self._credentials_manager = CredentialsManager(base_path=self._storage_path / "credentials")
        self._runtime_namespace = _runtime_namespace_for_workers_root(self._workers_root)
        self._launch_config_hash = self._compute_launch_config_hash()
        self._workers_root.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(
        cls,
        *,
        auth_token: str | None,
        storage_path: Path | None = None,
    ) -> DockerWorkerBackend:
        """Construct a backend instance from environment-backed configuration."""
        return cls(config=_DockerWorkerBackendConfig.from_env(), auth_token=auth_token, storage_path=storage_path)

    def ensure_worker(self, spec: WorkerSpec, *, now: float | None = None) -> WorkerHandle:
        """Resolve or start the dedicated worker container for the given worker key."""
        timestamp = time.time() if now is None else now
        with self._worker_lock(spec.worker_key):
            self._launch_config_hash = self._compute_launch_config_hash()
            paths = self._state_paths(spec.worker_key)
            metadata = self._load_metadata(paths) or self._default_metadata(spec.worker_key, timestamp)
            identity_changed = self._sync_metadata_identity(metadata)
            metadata.last_used_at = timestamp
            metadata.failure_reason = None

            should_restart = identity_changed or self._should_restart(metadata, paths)
            if should_restart:
                metadata.status = "starting"
                metadata.last_started_at = timestamp
                metadata.startup_count += 1
            self._save_metadata(paths, metadata)

            sync_shared_credentials_to_worker(
                spec.worker_key,
                include_ui_credentials=is_unscoped_worker_key(spec.worker_key),
                credentials_manager=self._credentials_manager,
            )

            try:
                container = self._ensure_container(metadata, paths)
                endpoint = self._wait_for_ready(container)
            except Exception as exc:
                failure_reason = str(exc)
                self._record_failure_locked(paths, metadata, failure_reason, now=timestamp, stop_container=True)
                if isinstance(exc, WorkerBackendError):
                    raise
                raise WorkerBackendError(failure_reason) from exc

            metadata.status = "ready"
            metadata.last_used_at = timestamp
            metadata.failure_reason = None
            metadata.endpoint = endpoint
            metadata.host_port = self._container_host_port(container)
            metadata.container_id = self._container_id(container)
            metadata.image = self.config.image
            metadata.publish_host = self.config.publish_host
            metadata.worker_port = self.config.worker_port
            metadata.launch_config_hash = self._launch_config_hash
            self._save_metadata(paths, metadata)
            return self._to_handle(metadata, container, now=timestamp, paths=paths)

    def get_worker(self, worker_key: str, *, now: float | None = None) -> WorkerHandle | None:
        """Return the current worker handle for one worker key, if present."""
        timestamp = time.time() if now is None else now
        paths = self._state_paths(worker_key)
        metadata = self._load_metadata(paths)
        if metadata is None:
            return None
        container = self._read_container(metadata.container_name)
        return self._to_handle(metadata, container, now=timestamp, paths=paths)

    def touch_worker(self, worker_key: str, *, now: float | None = None) -> WorkerHandle | None:
        """Refresh last-used metadata for one existing worker."""
        timestamp = time.time() if now is None else now
        with self._worker_lock(worker_key):
            paths = self._state_paths(worker_key)
            metadata = self._load_metadata(paths)
            if metadata is None:
                return None
            metadata.last_used_at = timestamp
            if metadata.status == "idle":
                metadata.status = "ready"
            self._save_metadata(paths, metadata)
            return self._to_handle(metadata, self._read_container(metadata.container_name), now=timestamp, paths=paths)

    def list_workers(self, *, include_idle: bool = True, now: float | None = None) -> list[WorkerHandle]:
        """List workers known to this backend."""
        timestamp = time.time() if now is None else now
        handles: list[WorkerHandle] = []
        for paths in self._metadata_paths():
            metadata = self._load_metadata(paths)
            if metadata is None:
                continue
            handle = self._to_handle(
                metadata,
                self._read_container(metadata.container_name),
                now=timestamp,
                paths=paths,
            )
            if include_idle or handle.status != "idle":
                handles.append(handle)
        return sorted(handles, key=lambda handle: handle.last_used_at, reverse=True)

    def evict_worker(
        self,
        worker_key: str,
        *,
        preserve_state: bool = True,
        now: float | None = None,
    ) -> WorkerHandle | None:
        """Evict one worker and optionally preserve its persisted state."""
        timestamp = time.time() if now is None else now
        with self._worker_lock(worker_key):
            paths = self._state_paths(worker_key)
            metadata = self._load_metadata(paths)
            if metadata is None:
                return None

            container = self._read_container(metadata.container_name)
            if preserve_state:
                self._stop_container(container)
                metadata.status = "idle"
                metadata.last_used_at = timestamp
                self._save_metadata(paths, metadata)
                return self._to_handle(metadata, container, now=timestamp, paths=paths)

            self._remove_container(container)
            if paths.root.exists():
                shutil.rmtree(paths.root)
            return None

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
                container = self._read_container(metadata.container_name)
                handle = self._to_handle(metadata, container, now=timestamp, paths=paths)
                if handle.status != "idle" or not self._container_is_running(container):
                    continue
                self._stop_container(container)
                metadata.status = "idle"
                self._save_metadata(paths, metadata)
                cleaned.append(self._to_handle(metadata, container, now=timestamp, paths=paths))
        return sorted(cleaned, key=lambda handle: handle.last_used_at, reverse=True)

    def record_failure(self, worker_key: str, failure_reason: str, *, now: float | None = None) -> WorkerHandle:
        """Persist a failed worker startup or execution state."""
        timestamp = time.time() if now is None else now
        with self._worker_lock(worker_key):
            paths = self._state_paths(worker_key)
            metadata = self._load_metadata(paths) or self._default_metadata(worker_key, timestamp)
            return self._record_failure_locked(paths, metadata, failure_reason, now=timestamp, stop_container=True)

    def _worker_lock(self, worker_key: str) -> threading.Lock:
        with self._worker_locks_lock:
            worker_lock = self._worker_locks.get(worker_key)
            if worker_lock is None:
                worker_lock = threading.Lock()
                self._worker_locks[worker_key] = worker_lock
        return worker_lock

    def _state_paths(self, worker_key: str) -> LocalWorkerStatePaths:
        return local_worker_state_paths_for_root(self._workers_root / worker_dir_name(worker_key))

    def _default_metadata(self, worker_key: str, now: float) -> _DockerWorkerMetadata:
        worker_id = self._container_name_for_worker(worker_key)
        return _DockerWorkerMetadata(
            worker_id=worker_id,
            worker_key=worker_key,
            endpoint=self._endpoint_for_host_port(None),
            backend_name=self.backend_name,
            container_name=worker_id,
            created_at=now,
            last_used_at=now,
            status="starting",
            image=self.config.image,
            publish_host=self.config.publish_host,
            worker_port=self.config.worker_port,
            launch_config_hash=self._launch_config_hash,
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
            self._remove_container(self._read_container(metadata.container_name))
        metadata.worker_id = expected_container_name
        metadata.container_name = expected_container_name
        metadata.endpoint = self._endpoint_for_host_port(None)
        metadata.host_port = None
        metadata.container_id = None
        metadata.launch_config_hash = None
        return True

    def _metadata_paths(self) -> list[LocalWorkerStatePaths]:
        if not self._workers_root.exists():
            return []
        return [
            local_worker_state_paths_for_root(metadata_file.parents[1])
            for metadata_file in sorted(self._workers_root.glob("*/metadata/worker.json"))
        ]

    def _load_metadata(self, paths: LocalWorkerStatePaths) -> _DockerWorkerMetadata | None:
        if not paths.metadata_file.exists():
            return None
        try:
            with paths.metadata_file.open(encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return None
        try:
            return _DockerWorkerMetadata(**data)
        except TypeError:
            return None

    def _save_metadata(self, paths: LocalWorkerStatePaths, metadata: _DockerWorkerMetadata) -> None:
        paths.root.mkdir(parents=True, exist_ok=True)
        paths.metadata_dir.mkdir(parents=True, exist_ok=True)
        with self._metadata_lock, paths.metadata_file.open("w", encoding="utf-8") as f:
            json.dump(asdict(metadata), f, sort_keys=True)

    def _read_container(self, container_name: str) -> _DockerContainer | None:
        try:
            return self._client.containers.get(container_name)
        except self._docker_errors.NotFound:
            return None
        except self._docker_errors.DockerException as exc:
            msg = f"Failed to inspect Docker worker '{container_name}': {exc}"
            raise WorkerBackendError(msg) from exc

    def _should_restart(self, metadata: _DockerWorkerMetadata, paths: LocalWorkerStatePaths) -> bool:
        container = self._read_container(metadata.container_name)
        if metadata.status == "failed":
            return True
        if container is None:
            return True
        if not self._container_matches_config(metadata, container, paths):
            return True
        return not self._container_is_running(container)

    def _container_matches_config(
        self,
        metadata: _DockerWorkerMetadata,
        container: _DockerContainer | None,
        paths: LocalWorkerStatePaths,
    ) -> bool:
        if metadata.launch_config_hash != self._launch_config_hash:
            return False
        if self._container_launch_config_hash(container) != self._launch_config_hash:
            return False

        mount_checks = [
            (paths.root, self.config.storage_mount_path, False),
        ]
        if self.config.host_config_path is not None:
            mount_checks.extend(
                [
                    (
                        self.config.host_config_path.parent,
                        _container_config_dir(self.config.config_path),
                        True,
                    ),
                    (
                        self.config.host_config_path,
                        self.config.config_path,
                        True,
                    ),
                    (
                        self._masked_config_dotenv_host_path(paths),
                        _container_config_dotenv_path(self.config.config_path),
                        True,
                    ),
                ],
            )
        return all(
            self._container_mount_matches(
                container,
                host_path=host_path,
                container_path=container_path,
                read_only=read_only,
            )
            for host_path, container_path, read_only in mount_checks
        )

    def _ensure_container(self, metadata: _DockerWorkerMetadata, paths: LocalWorkerStatePaths) -> _DockerContainer:
        paths.root.mkdir(parents=True, exist_ok=True)
        container = self._read_container(metadata.container_name)
        if container is not None and not self._container_matches_config(metadata, container, paths):
            self._remove_container(container)
            container = None

        if container is None:
            container = self._client.containers.run(
                self.config.image,
                command=["/app/run-sandbox-runner.sh"],
                name=metadata.container_name,
                detach=True,
                environment=self._container_env(metadata.worker_key),
                volumes=self._container_volumes(paths),
                ports={f"{self.config.worker_port}/tcp": (self.config.publish_host, None)},
                labels=self._container_labels(metadata),
                user=self.config.user,
            )
        elif not self._container_is_running(container):
            try:
                container.start()
            except self._docker_errors.DockerException as exc:
                msg = f"Failed to start Docker worker '{metadata.container_name}': {exc}"
                raise WorkerBackendError(msg) from exc

        self._reload_container(container)
        if self._container_host_port(container) is None:
            msg = f"Docker worker '{metadata.container_name}' is missing a published port."
            raise WorkerBackendError(msg)
        return container

    def _reload_container(self, container: _DockerContainer) -> None:
        try:
            container.reload()
        except self._docker_errors.DockerException as exc:
            msg = f"Failed to refresh Docker worker state: {exc}"
            raise WorkerBackendError(msg) from exc

    def _wait_for_ready(self, container: _DockerContainer) -> str:
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
                    msg = "Docker worker stopped before it became ready."
                    raise WorkerBackendError(msg)

                try:
                    response = client.get(healthz_url)
                except httpx.HTTPError:
                    response = None

                if response is not None and 200 <= response.status_code < 300:
                    return f"{endpoint_root}/api/sandbox-runner/execute"

                if time.time() >= deadline:
                    msg = f"Docker worker did not become ready within {self.config.ready_timeout_seconds:.0f}s."
                    raise WorkerBackendError(msg)
                time.sleep(_READY_POLL_INTERVAL_SECONDS)

    def _record_failure_locked(
        self,
        paths: LocalWorkerStatePaths,
        metadata: _DockerWorkerMetadata,
        failure_reason: str,
        *,
        now: float,
        stop_container: bool,
    ) -> WorkerHandle:
        container = self._read_container(metadata.container_name)
        if stop_container:
            self._stop_container(container)
        metadata.status = "failed"
        metadata.last_used_at = now
        metadata.failure_count += 1
        metadata.failure_reason = failure_reason
        self._save_metadata(paths, metadata)
        return self._to_handle(metadata, container, now=now, paths=paths)

    def _container_env(self, worker_key: str) -> dict[str, str]:
        env = {
            "MINDROOM_SANDBOX_RUNNER_MODE": "true",
            "MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE": "subprocess",
            _RUNNER_PORT_ENV_NAME: str(self.config.worker_port),
            "MINDROOM_STORAGE_PATH": self.config.storage_mount_path,
            SHARED_CREDENTIALS_PATH_ENV: f"{self.config.storage_mount_path}/.shared_credentials",
            _DEDICATED_WORKER_KEY_ENV: worker_key,
            _DEDICATED_WORKER_ROOT_ENV: self.config.storage_mount_path,
            "HOME": self.config.storage_mount_path,
            _TOKEN_ENV_NAME: self.auth_token,
        }
        if self.config.host_config_path is not None:
            env["MINDROOM_CONFIG_PATH"] = self.config.config_path
        env.update(self.config.extra_env)
        return env

    def _container_volumes(self, paths: LocalWorkerStatePaths) -> dict[str, dict[str, str]]:
        volumes = {
            str(paths.root): {"bind": self.config.storage_mount_path, "mode": "rw"},
        }
        if self.config.host_config_path is not None:
            volumes[str(self.config.host_config_path.parent)] = {
                "bind": _container_config_dir(self.config.config_path),
                "mode": "ro",
            }
            volumes[str(self.config.host_config_path)] = {"bind": self.config.config_path, "mode": "ro"}
            volumes[str(self._masked_config_dotenv_host_path(paths))] = {
                "bind": _container_config_dotenv_path(self.config.config_path),
                "mode": "ro",
            }
        return volumes

    def _masked_config_dotenv_host_path(self, paths: LocalWorkerStatePaths) -> Path:
        masked_dotenv = paths.metadata_dir / "config-dir.env"
        paths.metadata_dir.mkdir(parents=True, exist_ok=True)
        masked_dotenv.write_text("", encoding="utf-8")
        return masked_dotenv

    def _container_labels(self, metadata: _DockerWorkerMetadata) -> dict[str, str]:
        labels = {
            _LABEL_COMPONENT: _LABEL_COMPONENT_VALUE,
            _LABEL_MANAGED_BY: _LABEL_MANAGED_BY_VALUE,
            _LABEL_NAME: _LABEL_NAME_VALUE,
            _LABEL_WORKER_ID: metadata.worker_id,
            _LABEL_WORKER_KEY: metadata.worker_key,
            _LABEL_LAUNCH_CONFIG_HASH: self._launch_config_hash,
            _LABEL_RUNTIME_NAMESPACE: self._runtime_namespace,
        }
        labels.update(self.config.extra_labels)
        return labels

    def _compute_launch_config_hash(self) -> str:
        config_payload = {
            "auth_token": self.auth_token or "",
            "config_path": self.config.config_path,
            "config_contents_hash": _host_config_contents_hash(self.config.host_config_path),
            "extra_env": self.config.extra_env,
            "extra_labels": self.config.extra_labels,
            "host_config_path": str(self.config.host_config_path or ""),
            "image": self.config.image,
            "name_prefix": self.config.name_prefix,
            "publish_host": self.config.publish_host,
            "storage_mount_path": self.config.storage_mount_path,
            "workers_root": str(self._workers_root),
            "user": self.config.user or "",
            "worker_port": self.config.worker_port,
        }
        normalized = json.dumps(config_payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def _container_launch_config_hash(self, container: _DockerContainer | None) -> str | None:
        if container is None:
            return None
        attrs = getattr(container, "attrs", {})
        config = attrs.get("Config", {}) if isinstance(attrs, dict) else {}
        labels = config.get("Labels", {}) if isinstance(config, dict) else {}
        launch_config_hash = labels.get(_LABEL_LAUNCH_CONFIG_HASH) if isinstance(labels, dict) else None
        if isinstance(launch_config_hash, str) and launch_config_hash:
            return launch_config_hash
        return None

    def _container_mount_matches(
        self,
        container: _DockerContainer | None,
        *,
        host_path: Path,
        container_path: str,
        read_only: bool,
    ) -> bool:
        if container is None:
            return False

        expected_host_path = str(host_path.expanduser().resolve())
        attrs = getattr(container, "attrs", {})
        mounts = attrs.get("Mounts", []) if isinstance(attrs, dict) else []
        if not isinstance(mounts, list):
            return False

        for mount in mounts:
            if not isinstance(mount, dict):
                continue
            source = mount.get("Source")
            destination = mount.get("Destination")
            if not isinstance(source, str) or not isinstance(destination, str):
                continue
            if destination != container_path:
                continue
            if str(Path(source).expanduser().resolve()) != expected_host_path:
                continue
            writable = mount.get("RW")
            if isinstance(writable, bool):
                return writable is (not read_only)
            mode = mount.get("Mode")
            if isinstance(mode, str):
                return "ro" in mode if read_only else "ro" not in mode
            return True
        return False

    def _container_status(self, container: _DockerContainer) -> str | None:
        status = getattr(container, "status", None)
        if isinstance(status, str):
            return status
        attrs = getattr(container, "attrs", {})
        if isinstance(attrs, dict):
            state = attrs.get("State", {})
            if isinstance(state, dict):
                state_status = state.get("Status")
                if isinstance(state_status, str):
                    return state_status
        return None

    def _container_is_running(self, container: _DockerContainer | None) -> bool:
        if container is None:
            return False
        return self._container_status(container) == "running"

    def _container_host_port(self, container: _DockerContainer | None) -> int | None:
        host_port: int | None = None
        if container is None:
            return host_port

        attrs = getattr(container, "attrs", {})
        network_settings = attrs.get("NetworkSettings", {}) if isinstance(attrs, dict) else {}
        ports = network_settings.get("Ports", {}) if isinstance(network_settings, dict) else {}
        bindings = ports.get(f"{self.config.worker_port}/tcp") if isinstance(ports, dict) else None
        first_binding = bindings[0] if isinstance(bindings, list) and bindings else None
        raw_host_port = first_binding.get("HostPort") if isinstance(first_binding, dict) else None
        if raw_host_port is None:
            return host_port
        try:
            host_port = int(raw_host_port)
        except (TypeError, ValueError):
            host_port = None
        return host_port

    def _container_id(self, container: _DockerContainer | None) -> str | None:
        if container is None:
            return None
        container_id = getattr(container, "id", None)
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
        paths: LocalWorkerStatePaths,
    ) -> WorkerHandle:
        host_port = self._container_host_port(container) or metadata.host_port
        endpoint = self._endpoint_for_host_port(host_port)
        return WorkerHandle(
            worker_id=metadata.worker_id,
            worker_key=metadata.worker_key,
            endpoint=endpoint,
            auth_token=self.auth_token,
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
                "state_root": str(paths.root),
                "api_root": endpoint.removesuffix("/execute").rstrip("/"),
            },
        )
