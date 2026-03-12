"""Tests for the Docker worker backend."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING

from mindroom.tool_system.worker_routing import worker_root_path
from mindroom.workers.backends.docker import DockerWorkerBackend, _DockerWorkerBackendConfig
from mindroom.workers.models import WorkerSpec

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_TEST_AUTH_TOKEN = "test-token"  # noqa: S105
_ROTATED_AUTH_TOKEN = "rotated-token"  # noqa: S105


class _FakeDockerError(Exception):
    pass


class _FakeNotFoundError(_FakeDockerError):
    pass


class _FakeContainer:
    def __init__(
        self,
        *,
        name: str,
        image: str,
        host_port: int,
        environment: dict[str, str],
        labels: dict[str, str],
        user: str | None,
        status: str = "running",
    ) -> None:
        self.name = name
        self.image = image
        self.status = status
        self.id = f"{name}-id"
        self.attrs = {
            "State": {"Status": status},
            "Config": {
                "Image": image,
                "Env": [f"{key}={value}" for key, value in sorted(environment.items())],
                "Labels": dict(labels),
                "User": user or "",
            },
            "Mounts": [],
            "NetworkSettings": {
                "Ports": {
                    "8766/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(host_port)}],
                },
            },
        }
        self.started = 0
        self.stopped = 0
        self.removed = 0

    def reload(self) -> None:
        self.attrs["State"]["Status"] = self.status

    def start(self) -> None:
        self.started += 1
        self.status = "running"
        self.reload()

    def stop(self, timeout: int = 10) -> None:
        assert timeout == 10
        self.stopped += 1
        self.status = "exited"
        self.reload()

    def remove(self, force: bool = True) -> None:
        assert force is True
        self.removed += 1
        self.status = "removed"


class _FakeContainersApi:
    def __init__(self) -> None:
        self.by_name: dict[str, _FakeContainer] = {}
        self.run_calls: list[dict[str, object]] = []
        self.next_host_port = 43001

    def get(self, name: str) -> _FakeContainer:
        container = self.by_name.get(name)
        if container is None or container.status == "removed":
            raise _FakeNotFoundError(name)
        return container

    def run(self, image: str, **kwargs: object) -> _FakeContainer:
        host_port = self.next_host_port
        self.next_host_port += 1
        environment = kwargs.get("environment")
        labels = kwargs.get("labels")
        volumes = kwargs.get("volumes")
        container = _FakeContainer(
            name=str(kwargs["name"]),
            image=image,
            host_port=host_port,
            environment=dict(environment) if isinstance(environment, dict) else {},
            labels=dict(labels) if isinstance(labels, dict) else {},
            user=str(kwargs["user"]) if kwargs.get("user") is not None else None,
        )
        if isinstance(volumes, dict):
            container.attrs["Mounts"] = [
                {
                    "Type": "bind",
                    "Source": source,
                    "Destination": str(spec.get("bind", "")),
                    "Mode": str(spec.get("mode", "")),
                    "RW": str(spec.get("mode", "rw")) != "ro",
                }
                for source, spec in volumes.items()
                if isinstance(source, str) and isinstance(spec, dict)
            ]
        self.by_name[container.name] = container
        self.run_calls.append({"image": image, **kwargs})
        return container


class _FakeDockerClient:
    def __init__(self) -> None:
        self.containers = _FakeContainersApi()


def _noop_sync_shared_credentials(*_args: object, **_kwargs: object) -> None:
    return None


def _backend(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    idle_timeout_seconds: float = 60.0,
) -> tuple[DockerWorkerBackend, _FakeDockerClient, list[tuple[str, bool]]]:
    config = _DockerWorkerBackendConfig(
        image="ghcr.io/mindroom-ai/mindroom:latest",
        worker_port=8766,
        storage_mount_path="/app/worker",
        config_path="/app/config.yaml",
        host_config_path=tmp_path / "config.yaml",
        idle_timeout_seconds=idle_timeout_seconds,
        ready_timeout_seconds=5.0,
        name_prefix="mindroom-worker",
        publish_host="127.0.0.1",
        endpoint_host="127.0.0.1",
        user="1000:1000",
        extra_env={"EXTRA_ENV": "present"},
        extra_labels={"mindroom.ai/tenant": "test"},
    )
    config.host_config_path.write_text("agents: {}\n", encoding="utf-8")
    fake_client = _FakeDockerClient()
    sync_calls: list[tuple[str, bool]] = []

    monkeypatch.setattr(
        "mindroom.workers.backends.docker._load_docker_client_and_errors",
        lambda: (
            fake_client,
            SimpleNamespace(
                DockerException=_FakeDockerError,
                NotFound=_FakeNotFoundError,
            ),
        ),
    )
    monkeypatch.setattr(
        "mindroom.workers.backends.docker.sync_shared_credentials_to_worker",
        lambda worker_key, include_ui_credentials=False: sync_calls.append((worker_key, include_ui_credentials)),
    )
    backend = DockerWorkerBackend(config=config, auth_token=_TEST_AUTH_TOKEN, storage_path=tmp_path)
    monkeypatch.setattr(
        backend,
        "_wait_for_ready",
        lambda container: (f"http://127.0.0.1:{backend._container_host_port(container)}/api/sandbox-runner/execute"),
    )
    return backend, fake_client, sync_calls


def test_docker_backend_ensures_worker_container_and_bind_mount(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Ensuring one worker should create a dedicated sandbox-runner container."""
    backend, fake_client, sync_calls = _backend(monkeypatch, tmp_path)

    handle = backend.ensure_worker(WorkerSpec("worker-a"), now=10.0)

    assert handle.worker_key == "worker-a"
    assert handle.backend_name == "docker"
    assert handle.status == "ready"
    assert handle.endpoint.endswith("/api/sandbox-runner/execute")
    assert handle.debug_metadata["state_root"] == str(worker_root_path(tmp_path, "worker-a"))
    assert sync_calls == [("worker-a", False)]

    run_call = fake_client.containers.run_calls[0]
    env = run_call["environment"]
    assert isinstance(env, dict)
    assert env["MINDROOM_SANDBOX_RUNNER_MODE"] == "true"
    assert env["MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE"] == "subprocess"
    assert env["MINDROOM_SANDBOX_PROXY_TOKEN"] == _TEST_AUTH_TOKEN
    assert env["MINDROOM_SANDBOX_DEDICATED_WORKER_KEY"] == "worker-a"
    assert env["MINDROOM_SANDBOX_DEDICATED_WORKER_ROOT"] == "/app/worker"
    assert env["MINDROOM_SHARED_CREDENTIALS_PATH"] == "/app/worker/.shared_credentials"
    assert env["EXTRA_ENV"] == "present"

    volumes = run_call["volumes"]
    assert isinstance(volumes, dict)
    state_root = str(worker_root_path(tmp_path, "worker-a"))
    assert volumes[state_root]["bind"] == "/app/worker"
    assert volumes[str(tmp_path / "config.yaml")]["bind"] == "/app/config.yaml"
    assert run_call["user"] == "1000:1000"
    assert run_call["ports"] == {"8766/tcp": ("127.0.0.1", None)}

    labels = run_call["labels"]
    assert isinstance(labels, dict)
    assert labels["mindroom.ai/component"] == "worker"
    assert labels["mindroom.ai/worker-key"] == "worker-a"
    assert labels["mindroom.ai/runtime-namespace"]
    assert labels["mindroom.ai/tenant"] == "test"

    metadata_path = worker_root_path(tmp_path, "worker-a") / "metadata" / "worker.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["status"] == "ready"
    assert metadata["startup_count"] == 1


def test_docker_backend_cleanup_stops_idle_workers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Idle cleanup should stop running containers while retaining worker state."""
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, idle_timeout_seconds=60.0)

    backend.ensure_worker(WorkerSpec("worker-a"), now=10.0)

    metadata_path = worker_root_path(tmp_path, "worker-a") / "metadata" / "worker.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["last_used_at"] = 0.0
    metadata["status"] = "ready"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    cleaned = backend.cleanup_idle_workers(now=100.0)

    assert [worker.worker_key for worker in cleaned] == ["worker-a"]
    assert cleaned[0].status == "idle"

    container = next(iter(fake_client.containers.by_name.values()))
    assert container.stopped == 1
    assert container.status == "exited"

    worker_file = worker_root_path(tmp_path, "worker-a") / "workspace" / "note.txt"
    worker_file.parent.mkdir(parents=True, exist_ok=True)
    worker_file.write_text("still here", encoding="utf-8")
    assert worker_file.read_text(encoding="utf-8") == "still here"


def test_docker_backend_evict_without_preserving_state_removes_container_and_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Evicting without preserving state should remove both the container and state root."""
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path)

    backend.ensure_worker(WorkerSpec("worker-a"), now=10.0)
    worker_root = worker_root_path(tmp_path, "worker-a")

    result = backend.evict_worker("worker-a", preserve_state=False, now=20.0)

    assert result is None
    assert not worker_root.exists()
    container = next(iter(fake_client.containers.by_name.values()))
    assert container.removed == 1


def test_docker_backend_records_failure_and_stops_container(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Recording a failure should stop the worker and persist failure metadata."""
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path)

    backend.ensure_worker(WorkerSpec("worker-a"), now=10.0)

    handle = backend.record_failure("worker-a", "boom", now=11.0)

    assert handle.status == "failed"
    assert handle.failure_count == 1
    assert handle.failure_reason == "boom"

    container = next(iter(fake_client.containers.by_name.values()))
    assert container.stopped == 1
    assert container.status == "exited"


def test_docker_backend_recreates_container_when_launch_config_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Token and launch-config changes should force worker recreation."""
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path)

    first_handle = backend.ensure_worker(WorkerSpec("worker-a"), now=10.0)
    first_container = fake_client.containers.by_name[first_handle.worker_id]

    updated_config = replace(
        backend.config,
        user="2000:2000",
        extra_env={"EXTRA_ENV": "updated"},
    )
    updated_backend = DockerWorkerBackend(config=updated_config, auth_token=_ROTATED_AUTH_TOKEN, storage_path=tmp_path)
    monkeypatch.setattr(
        updated_backend,
        "_wait_for_ready",
        lambda container: (
            f"http://127.0.0.1:{updated_backend._container_host_port(container)}/api/sandbox-runner/execute"
        ),
    )

    updated_backend.ensure_worker(WorkerSpec("worker-a"), now=20.0)

    second_container = fake_client.containers.by_name[first_handle.worker_id]
    assert second_container is not first_container
    assert first_container.removed == 1
    assert len(fake_client.containers.run_calls) == 2

    second_run_call = fake_client.containers.run_calls[-1]
    second_env = second_run_call["environment"]
    assert isinstance(second_env, dict)
    assert second_env["MINDROOM_SANDBOX_PROXY_TOKEN"] == _ROTATED_AUTH_TOKEN
    assert second_env["EXTRA_ENV"] == "updated"
    assert second_run_call["user"] == "2000:2000"


def test_docker_backend_recreates_container_when_name_prefix_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Changing the configured name prefix should recreate the worker with a new identity."""
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path)

    first_handle = backend.ensure_worker(WorkerSpec("worker-a"), now=10.0)
    first_container = fake_client.containers.by_name[first_handle.worker_id]

    updated_config = replace(backend.config, name_prefix="other-prefix")
    updated_backend = DockerWorkerBackend(config=updated_config, auth_token=_TEST_AUTH_TOKEN, storage_path=tmp_path)
    monkeypatch.setattr(
        updated_backend,
        "_wait_for_ready",
        lambda container: (
            f"http://127.0.0.1:{updated_backend._container_host_port(container)}/api/sandbox-runner/execute"
        ),
    )

    second_handle = updated_backend.ensure_worker(WorkerSpec("worker-a"), now=20.0)

    assert second_handle.worker_id != first_handle.worker_id
    assert first_container.removed == 1
    assert len(fake_client.containers.run_calls) == 2
    assert fake_client.containers.run_calls[0]["name"] != fake_client.containers.run_calls[1]["name"]


def test_docker_backend_uses_distinct_container_names_for_different_storage_roots(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Separate runtimes should not share Docker worker identities."""
    config = _DockerWorkerBackendConfig(
        image="ghcr.io/mindroom-ai/mindroom:latest",
        worker_port=8766,
        storage_mount_path="/app/worker",
        config_path="/app/config.yaml",
        host_config_path=None,
        idle_timeout_seconds=60.0,
        ready_timeout_seconds=5.0,
        name_prefix="mindroom-worker",
        publish_host="127.0.0.1",
        endpoint_host="127.0.0.1",
        user="1000:1000",
        extra_env={},
        extra_labels={},
    )
    fake_client = _FakeDockerClient()

    monkeypatch.setattr(
        "mindroom.workers.backends.docker._load_docker_client_and_errors",
        lambda: (
            fake_client,
            SimpleNamespace(
                DockerException=_FakeDockerError,
                NotFound=_FakeNotFoundError,
            ),
        ),
    )
    monkeypatch.setattr(
        "mindroom.workers.backends.docker.sync_shared_credentials_to_worker",
        _noop_sync_shared_credentials,
    )

    first_storage_root = tmp_path / "runtime-a"
    second_storage_root = tmp_path / "runtime-b"
    first_backend = DockerWorkerBackend(config=config, auth_token=_TEST_AUTH_TOKEN, storage_path=first_storage_root)
    monkeypatch.setattr(
        first_backend,
        "_wait_for_ready",
        lambda container: (
            f"http://127.0.0.1:{first_backend._container_host_port(container)}/api/sandbox-runner/execute"
        ),
    )

    second_backend = DockerWorkerBackend(config=config, auth_token=_TEST_AUTH_TOKEN, storage_path=second_storage_root)
    monkeypatch.setattr(
        second_backend,
        "_wait_for_ready",
        lambda container: (
            f"http://127.0.0.1:{second_backend._container_host_port(container)}/api/sandbox-runner/execute"
        ),
    )

    first_handle = first_backend.ensure_worker(WorkerSpec("worker-a"), now=10.0)
    second_handle = second_backend.ensure_worker(WorkerSpec("worker-a"), now=20.0)

    assert first_handle.worker_id != second_handle.worker_id
    assert len(fake_client.containers.run_calls) == 2
    assert fake_client.containers.run_calls[0]["name"] != fake_client.containers.run_calls[1]["name"]


def test_docker_backend_recreates_container_when_storage_mount_does_not_match(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Worker reuse should fail closed when an existing container points at the wrong state root."""
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path)

    handle = backend.ensure_worker(WorkerSpec("worker-a"), now=10.0)
    existing_container = fake_client.containers.by_name[handle.worker_id]
    existing_container.attrs["Mounts"][0]["Source"] = str(tmp_path / "wrong-root")

    backend.ensure_worker(WorkerSpec("worker-a"), now=20.0)

    replacement_container = fake_client.containers.by_name[handle.worker_id]
    assert replacement_container is not existing_container
    assert existing_container.removed == 1
    assert len(fake_client.containers.run_calls) == 2
