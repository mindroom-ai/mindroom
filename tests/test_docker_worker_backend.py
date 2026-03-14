"""Tests for the Docker worker backend."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import yaml

from mindroom.tool_system.worker_routing import worker_dir_name, worker_root_path
from mindroom.workers.backends.docker import DockerWorkerBackend, _load_docker_client_and_errors
from mindroom.workers.backends.docker_config import (
    _default_docker_user_for_os,
    _DockerWorkerBackendConfig,
    _read_docker_user,
)
from mindroom.workers.backends.docker_projection import _PROJECTED_CONFIGS_DIRNAME, _WORKER_CONFIG_STATE_DIRNAME
from mindroom.workers.models import WorkerSpec

if TYPE_CHECKING:
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
        image_identity: str,
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
            "Image": image_identity,
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
    def __init__(
        self,
        *,
        images: _FakeImagesApi | None = None,
        auto_pull_missing_image: bool = False,
    ) -> None:
        self.by_name: dict[str, _FakeContainer] = {}
        self.created_containers: list[_FakeContainer] = []
        self.run_calls: list[dict[str, object]] = []
        self.next_host_port = 43001
        self.images = images
        self.auto_pull_missing_image = auto_pull_missing_image

    def get(self, name: str) -> _FakeContainer:
        container = self.by_name.get(name)
        if container is None or container.status == "removed":
            raise _FakeNotFoundError(name)
        return container

    def run(self, image: str, **kwargs: object) -> _FakeContainer:
        if self.auto_pull_missing_image and self.images is not None and image not in self.images.by_name:
            self.images.by_name[image] = _FakeImage("sha256:auto-pulled-image")

        image_identity = image
        if self.images is not None:
            docker_image = self.images.by_name.get(image)
            if docker_image is not None:
                image_identity = docker_image.id

        host_port = self.next_host_port
        self.next_host_port += 1
        environment = kwargs.get("environment")
        labels = kwargs.get("labels")
        volumes = kwargs.get("volumes")
        container = _FakeContainer(
            name=str(kwargs["name"]),
            image=image,
            image_identity=image_identity,
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
        self.created_containers.append(container)
        self.run_calls.append({"image": image, **kwargs})
        return container


class _FakeImage:
    def __init__(self, image_id: str) -> None:
        self.id = image_id


class _FakeImagesApi:
    def __init__(self) -> None:
        self.by_name: dict[str, _FakeImage] = {}

    def get(self, name: str) -> _FakeImage:
        image = self.by_name.get(name)
        if image is None:
            raise _FakeNotFoundError(name)
        return image


class _FakeDockerClient:
    def __init__(self, *, auto_pull_missing_image: bool = False) -> None:
        self.images = _FakeImagesApi()
        self.containers = _FakeContainersApi(
            images=self.images,
            auto_pull_missing_image=auto_pull_missing_image,
        )


def _noop_sync_shared_credentials(*_args: object, **_kwargs: object) -> None:
    return None


def _projected_config_fixture(tmp_path: Path) -> tuple[str, dict[str, Path]]:
    plugin_root = tmp_path / "plugins" / "my-plugin"
    plugin_root.mkdir(parents=True)
    (plugin_root / "plugin.py").write_text("PLUGIN_VERSION = 'v1'\n", encoding="utf-8")
    knowledge_root = tmp_path / "knowledge_docs"
    knowledge_root.mkdir()
    (knowledge_root / "guide.md").write_text("# Guide v1\n", encoding="utf-8")
    context_file = tmp_path / "context.md"
    context_file.write_text("# Context\n", encoding="utf-8")
    memory_root = tmp_path / "memory_files"
    memory_root.mkdir()
    agent_memory_root = tmp_path / "agent_memory" / "code"
    agent_memory_root.mkdir(parents=True)
    primary_runtime_data = tmp_path / "mindroom_data" / "credentials"
    primary_runtime_data.mkdir(parents=True)
    (primary_runtime_data / "secret.json").write_text('{"api_key":"leak"}', encoding="utf-8")
    return (
        """
plugins:
  - ./plugins/my-plugin
knowledge_bases:
  docs:
    path: ./knowledge_docs
memory:
  backend: file
  file:
    path: ./memory_files
agents:
  code:
    display_name: Code
    role: Test
    model: default
    context_files:
      - ./context.md
    memory_file_path: ./agent_memory/code
models:
  default:
    provider: openai
    id: test-model
    api_key: sk-model-secret
voice:
  enabled: true
  stt:
    provider: openai
    model: whisper-1
    api_key: sk-voice-secret
teams:
  helpers:
    agents: [code]
    mode: collaborate
cultures:
  engineering:
    description: Keep things clean
    agents: [code]
    mode: automatic
authorization:
  global_users:
    - "@owner:example.org"
matrix_room_access:
  mode: single_user_private
mindroom_user:
  username: mindroom
""".lstrip(),
        {
            "plugin_root": plugin_root,
            "knowledge_root": knowledge_root,
            "context_file": context_file,
            "memory_root": memory_root,
            "agent_memory_root": agent_memory_root,
        },
    )


def _multi_agent_projected_config_fixture(tmp_path: Path) -> tuple[str, dict[str, Path]]:
    alpha_knowledge_root = tmp_path / "knowledge_alpha"
    alpha_knowledge_root.mkdir()
    (alpha_knowledge_root / "a.txt").write_text("alpha knowledge\n", encoding="utf-8")
    beta_knowledge_root = tmp_path / "knowledge_beta"
    beta_knowledge_root.mkdir()
    (beta_knowledge_root / "b.txt").write_text("beta knowledge\n", encoding="utf-8")
    alpha_context = tmp_path / "alpha.md"
    alpha_context.write_text("# Alpha\n", encoding="utf-8")
    beta_context = tmp_path / "beta.md"
    beta_context.write_text("# Beta\n", encoding="utf-8")
    memory_root = tmp_path / "memory_files"
    memory_root.mkdir()
    alpha_memory_root = tmp_path / "agent_memory" / "alpha"
    alpha_memory_root.mkdir(parents=True)
    beta_memory_root = tmp_path / "agent_memory" / "beta"
    beta_memory_root.mkdir(parents=True)
    return (
        """
knowledge_bases:
  a:
    path: ./knowledge_alpha
  b:
    path: ./knowledge_beta
memory:
  backend: file
  file:
    path: ./memory_files
agents:
  alpha:
    display_name: Alpha
    role: Alpha test
    model: default
    worker_scope: shared
    knowledge_bases: [a]
    context_files:
      - ./alpha.md
    memory_file_path: ./agent_memory/alpha
  beta:
    display_name: Beta
    role: Beta test
    model: default
    worker_scope: shared
    knowledge_bases: [b]
    context_files:
      - ./beta.md
    memory_file_path: ./agent_memory/beta
models:
  default:
    provider: openai
    id: test-model
""".lstrip(),
        {
            "alpha_context": alpha_context,
            "beta_context": beta_context,
            "alpha_knowledge_root": alpha_knowledge_root,
            "beta_knowledge_root": beta_knowledge_root,
            "alpha_memory_root": alpha_memory_root,
            "beta_memory_root": beta_memory_root,
        },
    )


def _knowledge_base_collision_fixture(tmp_path: Path) -> str:
    first_root = tmp_path / "knowledge_collision_a"
    first_root.mkdir()
    (first_root / "a.txt").write_text("first\n", encoding="utf-8")
    second_root = tmp_path / "knowledge_collision_b"
    second_root.mkdir()
    (second_root / "b.txt").write_text("second\n", encoding="utf-8")
    memory_root = tmp_path / "memory_files"
    memory_root.mkdir()
    return """
knowledge_bases:
  "docs/a":
    path: ./knowledge_collision_a
  "docs:a":
    path: ./knowledge_collision_b
memory:
  backend: file
  file:
    path: ./memory_files
agents:
  alpha:
    display_name: Alpha
    role: Alpha test
    model: default
    worker_scope: shared
    knowledge_bases: ["docs/a", "docs:a"]
models:
  default:
    provider: openai
    id: test-model
""".lstrip()


def _projection_root(volumes: dict[str, dict[str, str]]) -> Path:
    return next(Path(source) for source, spec in volumes.items() if spec["bind"] == "/app/config-host")


def _assert_projected_worker_mounts(
    tmp_path: Path,
    volumes: dict[str, dict[str, str]],
    projected_paths: dict[str, Path],
) -> Path:
    worker_root = worker_root_path(tmp_path, "worker-a")
    state_root = str(worker_root)
    assert volumes[state_root]["bind"] == "/app/worker"

    projection_root = _projection_root(volumes)
    assert projection_root.parent == (
        worker_root_path(tmp_path, "__mindroom_root__").parent
        / _PROJECTED_CONFIGS_DIRNAME
        / worker_dir_name("worker-a")
    )
    assert worker_root not in projection_root.parents
    assert str(tmp_path) not in volumes
    assert str(tmp_path / "mindroom_data") not in volumes
    assert str(projected_paths["plugin_root"].resolve()) not in volumes
    assert str(projected_paths["knowledge_root"].resolve()) not in volumes
    assert str(projected_paths["context_file"].resolve()) not in volumes
    assert str(projected_paths["memory_root"].resolve()) not in volumes
    assert str(projected_paths["agent_memory_root"].resolve()) not in volumes
    return projection_root


def _assert_projected_config_snapshot(projection_root: Path, tmp_path: Path) -> None:
    projected_config = (projection_root / "config.yaml").read_text(encoding="utf-8")
    assert "plugins:\n- ./.mindroom-worker-assets/plugins/00-my-plugin" in projected_config
    assert "path: ./.mindroom-worker-assets/knowledge_bases/docs" in projected_config
    assert f"path: /app/worker/{_WORKER_CONFIG_STATE_DIRNAME}/memory/file" in projected_config
    assert "- ./.mindroom-worker-assets/agents/code/context_files/00-context.md" in projected_config

    projected_context_path = (
        projection_root / ".mindroom-worker-assets" / "agents" / "code" / "context_files" / "00-context.md"
    )
    assert projected_context_path.read_text(encoding="utf-8") == "# Context\n"
    projected_plugin_path = projection_root / ".mindroom-worker-assets" / "plugins" / "00-my-plugin" / "plugin.py"
    assert projected_plugin_path.read_text(encoding="utf-8") == "PLUGIN_VERSION = 'v1'\n"
    projected_knowledge_path = projection_root / ".mindroom-worker-assets" / "knowledge_bases" / "docs" / "guide.md"
    assert projected_knowledge_path.read_text(encoding="utf-8") == "# Guide v1\n"
    assert (
        f"memory_file_path: /app/worker/{_WORKER_CONFIG_STATE_DIRNAME}/agents/code/memory_file_path" in projected_config
    )
    assert (projection_root / ".env").read_text(encoding="utf-8") == ""
    assert (projection_root / ".projection-ready").read_text(encoding="utf-8") == "ready\n"
    assert (worker_root_path(tmp_path, "worker-a") / _WORKER_CONFIG_STATE_DIRNAME / "memory" / "file").is_dir()
    assert (
        worker_root_path(tmp_path, "worker-a") / _WORKER_CONFIG_STATE_DIRNAME / "agents" / "code" / "memory_file_path"
    ).is_dir()


def _backend(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    idle_timeout_seconds: float = 60.0,
    config_text: str = "agents: {}\n",
) -> tuple[DockerWorkerBackend, _FakeDockerClient, list[tuple[str, bool]]]:
    config = _DockerWorkerBackendConfig(
        image="ghcr.io/mindroom-ai/mindroom:latest",
        worker_port=8766,
        storage_mount_path="/app/worker",
        config_path="/app/config-host/config.yaml",
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
    config.host_config_path.write_text(config_text, encoding="utf-8")
    fake_client = _FakeDockerClient()
    fake_client.images.by_name[config.image] = _FakeImage("sha256:image-v1")
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

    def _record_sync_call(
        worker_key: str,
        include_ui_credentials: bool = False,
        **_kwargs: object,
    ) -> None:
        sync_calls.append((worker_key, include_ui_credentials))

    monkeypatch.setattr(
        "mindroom.workers.backends.docker.sync_shared_credentials_to_worker",
        _record_sync_call,
    )
    backend = DockerWorkerBackend(config=config, auth_token=_TEST_AUTH_TOKEN, storage_path=tmp_path)
    monkeypatch.setattr(
        backend,
        "_wait_for_ready",
        lambda container: (f"http://127.0.0.1:{backend._container_host_port(container)}/api/sandbox-runner/execute"),
    )
    return backend, fake_client, sync_calls


def _projection_signature_for_hash_seed(hash_seed: str, workspace_root: Path) -> dict[str, object]:
    script = (
        textwrap.dedent(
            """
        import json
        from pathlib import Path

        import yaml

        from mindroom.workers.backends.docker_config import _DockerWorkerBackendConfig
        from mindroom.workers.backends.docker_projection import DockerProjectionManager
        from mindroom.workers.backends.local import local_worker_state_paths_for_root

        tmp_path = Path(__WORKSPACE_ROOT__)
        config = _DockerWorkerBackendConfig(
            image="ghcr.io/mindroom-ai/mindroom:latest",
            worker_port=8766,
            storage_mount_path="/app/worker",
            config_path="/app/config-host/config.yaml",
            host_config_path=tmp_path / "config.yaml",
            idle_timeout_seconds=60.0,
            ready_timeout_seconds=5.0,
            name_prefix="mindroom-worker",
            publish_host="127.0.0.1",
            endpoint_host="127.0.0.1",
            user="1000:1000",
            extra_env={},
            extra_labels={},
        )
        manager = DockerProjectionManager(config=config, projected_configs_root=tmp_path / "projections")
        paths = local_worker_state_paths_for_root(tmp_path / "workers" / "worker-a")
        projection = manager.projected_config(
            paths,
            worker_key="v1:default:shared:alpha",
            materialize=False,
        )
        projected_config = yaml.safe_load(projection.projected_yaml)
        print(
            json.dumps(
                {
                    "projection_root": projection.root.name,
                    "knowledge_bases": list(projected_config["knowledge_bases"]),
                },
            ),
        )
        """,
        )
        .lstrip()
        .replace("__WORKSPACE_ROOT__", json.dumps(str(workspace_root)))
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "PYTHONHASHSEED": hash_seed},
        text=True,
    )
    return json.loads(completed.stdout)


def test_docker_backend_ensures_worker_container_and_bind_mount(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Ensuring one worker should project only configured config assets into the container."""
    config_text, projected_paths = _projected_config_fixture(tmp_path)
    backend, fake_client, sync_calls = _backend(monkeypatch, tmp_path, config_text=config_text)

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
    assert env["MINDROOM_CONFIG_PATH"] == "/app/config-host/config.yaml"
    assert env["EXTRA_ENV"] == "present"

    volumes = run_call["volumes"]
    assert isinstance(volumes, dict)
    projection_root = _assert_projected_worker_mounts(tmp_path, volumes, projected_paths)
    _assert_projected_config_snapshot(projection_root, tmp_path)
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


def test_docker_backend_syncs_shared_credentials_from_runtime_storage_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Shared-credential mirroring should use the active runtime storage root."""
    backend, _fake_client, _sync_calls = _backend(monkeypatch, tmp_path)
    synced_storage_roots: list[Path | None] = []

    def _capture_runtime_storage_root(
        _worker_key: str,
        **kwargs: object,
    ) -> None:
        credentials_manager = kwargs.get("credentials_manager")
        synced_storage_roots.append(
            None if credentials_manager is None else credentials_manager.storage_root,
        )

    monkeypatch.setattr(
        "mindroom.workers.backends.docker.sync_shared_credentials_to_worker",
        _capture_runtime_storage_root,
    )

    backend.ensure_worker(WorkerSpec("worker-a"), now=10.0)

    assert synced_storage_roots == [tmp_path.resolve()]


def test_docker_backend_redacts_projected_config_secrets_and_support_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Projected worker config should redact secrets and strip unrelated runtime state."""
    config_text, _projected_paths = _projected_config_fixture(tmp_path)
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, config_text=config_text)

    backend.ensure_worker(WorkerSpec("worker-a"), now=10.0)

    volumes = fake_client.containers.run_calls[0]["volumes"]
    assert isinstance(volumes, dict)
    projection_root = _projection_root(volumes)
    projected_config = yaml.safe_load((projection_root / "config.yaml").read_text(encoding="utf-8"))

    assert projected_config["models"]["default"]["api_key"] == "__REDACTED__"
    assert projected_config["voice"]["stt"]["api_key"] == "__REDACTED__"
    assert projected_config["teams"] == {}
    assert projected_config["cultures"] == {}
    assert projected_config["authorization"] == {}
    assert projected_config["matrix_room_access"] == {}
    assert projected_config["mindroom_user"] is None


def test_load_docker_client_auto_installs_optional_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Docker backend startup should ensure the optional Docker SDK before importing it."""
    captured: dict[str, object] = {}
    fake_client = object()
    fake_errors = SimpleNamespace(DockerException=_FakeDockerError, NotFound=_FakeNotFoundError)

    def _ensure() -> None:
        captured["installed"] = True

    def _import_module(name: str) -> object:
        if name == "docker":
            return SimpleNamespace(from_env=lambda: fake_client)
        if name == "docker.errors":
            return fake_errors
        msg = f"Unexpected import: {name}"
        raise AssertionError(msg)

    monkeypatch.setattr("mindroom.workers.backends.docker.ensure_docker_dependencies", _ensure)
    monkeypatch.setattr("mindroom.workers.backends.docker.importlib.import_module", _import_module)

    client, errors = _load_docker_client_and_errors()

    assert captured["installed"] is True
    assert client is fake_client
    assert errors is fake_errors


def test_read_docker_user_defaults_to_current_posix_uid_gid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset Docker worker user should follow the current POSIX runtime user."""
    monkeypatch.delenv("MINDROOM_DOCKER_WORKER_USER", raising=False)
    monkeypatch.setattr("mindroom.workers.backends.docker_config.os.getuid", lambda: 501)
    monkeypatch.setattr("mindroom.workers.backends.docker_config.os.getgid", lambda: 20)

    assert _default_docker_user_for_os("posix") == "501:20"


def test_read_docker_user_defaults_to_image_user_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows should leave the container user unset and use the image default."""
    monkeypatch.delenv("MINDROOM_DOCKER_WORKER_USER", raising=False)

    assert _default_docker_user_for_os("nt") is None


def test_read_docker_user_env_override_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit Docker worker user config should override platform defaults."""
    monkeypatch.setenv("MINDROOM_DOCKER_WORKER_USER", "2001:3001")

    assert _read_docker_user() == "2001:3001"


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
    volumes = fake_client.containers.run_calls[0]["volumes"]
    assert isinstance(volumes, dict)
    projection_root = _projection_root(volumes)

    result = backend.evict_worker("worker-a", preserve_state=False, now=20.0)

    assert result is None
    assert not worker_root.exists()
    assert not projection_root.exists()
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
        config_path="/app/config-host/config.yaml",
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


def test_docker_backend_recreates_container_when_host_config_contents_change(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Worker reuse should fail closed when the mounted config file changes contents."""
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path)

    handle = backend.ensure_worker(WorkerSpec("worker-a"), now=10.0)
    existing_container = fake_client.containers.by_name[handle.worker_id]

    (tmp_path / "config.yaml").write_text("agents:\n  code:\n    tools: [shell]\n", encoding="utf-8")

    backend.ensure_worker(WorkerSpec("worker-a"), now=20.0)

    replacement_container = fake_client.containers.by_name[handle.worker_id]
    assert replacement_container is not existing_container
    assert existing_container.removed == 1
    assert len(fake_client.containers.run_calls) == 2


def test_docker_backend_recreates_container_when_projected_file_asset_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Changing a projected single-file asset should rotate the worker."""
    config_text, projected_paths = _projected_config_fixture(tmp_path)
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, config_text=config_text)

    handle = backend.ensure_worker(WorkerSpec("worker-a"), now=10.0)
    existing_container = fake_client.containers.by_name[handle.worker_id]
    first_volumes = fake_client.containers.run_calls[0]["volumes"]
    assert isinstance(first_volumes, dict)
    first_projection_root = _projection_root(first_volumes)

    updated_context_file = projected_paths["context_file"].with_suffix(".updated.md")
    updated_context_file.write_text("# Updated Context\n", encoding="utf-8")
    updated_context_file.replace(projected_paths["context_file"])

    backend.ensure_worker(WorkerSpec("worker-a"), now=20.0)

    replacement_container = fake_client.containers.by_name[handle.worker_id]
    assert replacement_container is not existing_container
    assert existing_container.removed == 1
    assert len(fake_client.containers.run_calls) == 2

    second_volumes = fake_client.containers.run_calls[-1]["volumes"]
    assert isinstance(second_volumes, dict)
    second_projection_root = _projection_root(second_volumes)
    assert second_projection_root != first_projection_root
    projected_context_path = (
        second_projection_root / ".mindroom-worker-assets" / "agents" / "code" / "context_files" / "00-context.md"
    )
    assert projected_context_path.read_text(encoding="utf-8") == "# Updated Context\n"


def test_docker_backend_recreates_container_when_projected_directory_asset_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Changing a projected directory asset should rotate the worker."""
    config_text, projected_paths = _projected_config_fixture(tmp_path)
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, config_text=config_text)

    handle = backend.ensure_worker(WorkerSpec("worker-a"), now=10.0)
    existing_container = fake_client.containers.by_name[handle.worker_id]
    first_volumes = fake_client.containers.run_calls[0]["volumes"]
    assert isinstance(first_volumes, dict)
    first_projection_root = _projection_root(first_volumes)

    replacement_knowledge_root = projected_paths["knowledge_root"].with_name("knowledge_docs.updated")
    replacement_knowledge_root.mkdir()
    (replacement_knowledge_root / "guide.md").write_text("# Guide v2\n", encoding="utf-8")
    archived_knowledge_root = projected_paths["knowledge_root"].with_name("knowledge_docs.previous")
    projected_paths["knowledge_root"].replace(archived_knowledge_root)
    replacement_knowledge_root.replace(projected_paths["knowledge_root"])

    removal_checks: list[bool] = []
    original_remove_container = backend._remove_container

    def _assert_old_projection_survives_until_removal(container: object) -> None:
        assert container is existing_container
        removal_checks.append(first_projection_root.exists())
        original_remove_container(container)

    monkeypatch.setattr(backend, "_remove_container", _assert_old_projection_survives_until_removal)

    backend.ensure_worker(WorkerSpec("worker-a"), now=20.0)

    replacement_container = fake_client.containers.by_name[handle.worker_id]
    assert replacement_container is not existing_container
    assert existing_container.removed == 1
    assert removal_checks == [True]
    assert len(fake_client.containers.run_calls) == 2

    second_volumes = fake_client.containers.run_calls[-1]["volumes"]
    assert isinstance(second_volumes, dict)
    second_projection_root = _projection_root(second_volumes)
    assert second_projection_root != first_projection_root
    assert not first_projection_root.exists()
    projected_knowledge_path = (
        second_projection_root / ".mindroom-worker-assets" / "knowledge_bases" / "docs" / "guide.md"
    )
    assert projected_knowledge_path.read_text(encoding="utf-8") == "# Guide v2\n"


def test_docker_backend_projects_only_agent_specific_assets_for_shared_worker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Agent-scoped workers should not snapshot unrelated agents' projected assets."""
    config_text, projected_paths = _multi_agent_projected_config_fixture(tmp_path)
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, config_text=config_text)
    worker_key = "v1:default:shared:alpha"

    backend.ensure_worker(WorkerSpec(worker_key), now=10.0)

    volumes = fake_client.containers.run_calls[0]["volumes"]
    assert isinstance(volumes, dict)
    projection_root = _projection_root(volumes)
    projected_config = (projection_root / "config.yaml").read_text(encoding="utf-8")
    projected_config_data = yaml.safe_load(projected_config)

    assert "- ./.mindroom-worker-assets/agents/alpha/context_files/00-alpha.md" in projected_config
    assert ".mindroom-worker-assets/agents/beta/context_files/00-beta.md" not in projected_config
    assert "path: ./.mindroom-worker-assets/knowledge_bases/a" in projected_config
    assert "path: ./.mindroom-worker-assets/knowledge_bases/b" not in projected_config
    assert (
        f"memory_file_path: /app/worker/{_WORKER_CONFIG_STATE_DIRNAME}/agents/alpha/memory_file_path"
        in projected_config
    )
    assert f"/app/worker/{_WORKER_CONFIG_STATE_DIRNAME}/agents/beta/memory_file_path" not in projected_config
    assert set(projected_config_data["agents"]) == {"alpha"}
    assert set(projected_config_data["knowledge_bases"]) == {"a"}

    projected_alpha_context = (
        projection_root / ".mindroom-worker-assets" / "agents" / "alpha" / "context_files" / "00-alpha.md"
    )
    assert projected_alpha_context.read_text(encoding="utf-8") == "# Alpha\n"
    assert not (
        projection_root / ".mindroom-worker-assets" / "agents" / "beta" / "context_files" / "00-beta.md"
    ).exists()

    projected_alpha_knowledge = projection_root / ".mindroom-worker-assets" / "knowledge_bases" / "a" / "a.txt"
    assert projected_alpha_knowledge.read_text(encoding="utf-8") == "alpha knowledge\n"
    assert not (projection_root / ".mindroom-worker-assets" / "knowledge_bases" / "b" / "b.txt").exists()

    worker_config_state_root = worker_root_path(tmp_path, worker_key) / _WORKER_CONFIG_STATE_DIRNAME / "agents"
    assert (worker_config_state_root / "alpha" / "memory_file_path").is_dir()
    assert not (worker_config_state_root / "beta" / "memory_file_path").exists()

    assert projected_paths["alpha_context"].resolve() not in projection_root.parents
    assert projected_paths["beta_context"].resolve() not in projection_root.parents
    assert projected_paths["alpha_knowledge_root"].resolve() not in projection_root.parents
    assert projected_paths["beta_knowledge_root"].resolve() not in projection_root.parents


def test_docker_projection_hash_is_stable_across_hash_seeds(tmp_path: Path) -> None:
    """Projection hashes should not change across interpreter restarts for the same config."""
    (tmp_path / "kb_a").mkdir()
    (tmp_path / "kb_a" / "a.txt").write_text("a\n", encoding="utf-8")
    (tmp_path / "kb_b").mkdir()
    (tmp_path / "kb_b" / "b.txt").write_text("b\n", encoding="utf-8")
    (tmp_path / "config.yaml").write_text(
        """
knowledge_bases:
  a:
    path: ./kb_a
  b:
    path: ./kb_b
agents:
  alpha:
    display_name: Alpha
    role: Alpha test
    model: default
    worker_scope: shared
    knowledge_bases: [a, b]
models:
  default:
    provider: openai
    id: test-model
""".lstrip(),
        encoding="utf-8",
    )

    first_signature = _projection_signature_for_hash_seed("1", tmp_path)
    second_signature = _projection_signature_for_hash_seed("2", tmp_path)

    assert first_signature == second_signature
    assert first_signature["knowledge_bases"] == ["a", "b"]


def test_docker_backend_rebuilds_incomplete_projection_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Interrupted projection writes should be repaired instead of being reused forever."""
    config_text, _projected_paths = _projected_config_fixture(tmp_path)
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, config_text=config_text)

    handle = backend.ensure_worker(WorkerSpec("worker-a"), now=10.0)
    existing_container = fake_client.containers.by_name[handle.worker_id]
    first_volumes = fake_client.containers.run_calls[0]["volumes"]
    assert isinstance(first_volumes, dict)
    projection_root = _projection_root(first_volumes)

    (projection_root / ".projection-ready").unlink()
    (projection_root / "config.yaml").write_text("broken: true\n", encoding="utf-8")

    backend.ensure_worker(WorkerSpec("worker-a"), now=20.0)

    second_volumes = fake_client.containers.run_calls[-1]["volumes"]
    assert isinstance(second_volumes, dict)
    second_projection_root = _projection_root(second_volumes)
    replacement_container = fake_client.containers.by_name[handle.worker_id]

    assert second_projection_root == projection_root
    assert replacement_container is not existing_container
    assert existing_container.removed == 1
    assert (projection_root / ".projection-ready").read_text(encoding="utf-8") == "ready\n"
    rebuilt_config = (projection_root / "config.yaml").read_text(encoding="utf-8")
    assert "broken: true" not in rebuilt_config
    assert "plugins:" in rebuilt_config


def test_docker_backend_disambiguates_colliding_projected_knowledge_base_ids(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Projected knowledge-base directories should stay unique when IDs sanitize the same way."""
    backend, fake_client, _sync_calls = _backend(
        monkeypatch,
        tmp_path,
        config_text=_knowledge_base_collision_fixture(tmp_path),
    )

    backend.ensure_worker(WorkerSpec("v1:default:shared:alpha"), now=10.0)

    volumes = fake_client.containers.run_calls[0]["volumes"]
    assert isinstance(volumes, dict)
    projection_root = _projection_root(volumes)
    projected_knowledge_root = projection_root / ".mindroom-worker-assets" / "knowledge_bases"
    projected_dirs = sorted(path.name for path in projected_knowledge_root.iterdir())

    assert "docs-a" in projected_dirs
    assert any(path_name.startswith("docs-a-") for path_name in projected_dirs)
    assert len(projected_dirs) == 2
    assert (projected_knowledge_root / "docs-a" / "a.txt").read_text(encoding="utf-8") == "first\n"
    colliding_dir = next(path_name for path_name in projected_dirs if path_name != "docs-a")
    assert (projected_knowledge_root / colliding_dir / "b.txt").read_text(encoding="utf-8") == "second\n"


def test_docker_backend_reuses_container_after_first_run_pulls_missing_image(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The first auto-pull should not force a second worker recreation on the next ensure."""
    config = _DockerWorkerBackendConfig(
        image="ghcr.io/mindroom-ai/mindroom:latest",
        worker_port=8766,
        storage_mount_path="/app/worker",
        config_path="/app/config-host/config.yaml",
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
    fake_client = _FakeDockerClient(auto_pull_missing_image=True)

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

    backend = DockerWorkerBackend(config=config, auth_token=_TEST_AUTH_TOKEN, storage_path=tmp_path)
    monkeypatch.setattr(
        backend,
        "_wait_for_ready",
        lambda container: (f"http://127.0.0.1:{backend._container_host_port(container)}/api/sandbox-runner/execute"),
    )

    first_handle = backend.ensure_worker(WorkerSpec("worker-a"), now=10.0)
    first_container = fake_client.containers.by_name[first_handle.worker_id]

    second_handle = backend.ensure_worker(WorkerSpec("worker-a"), now=20.0)

    assert second_handle.worker_id == first_handle.worker_id
    assert fake_client.containers.by_name[second_handle.worker_id] is first_container
    assert len(fake_client.containers.run_calls) == 1
    assert first_container.removed == 0


def test_docker_backend_recreates_container_when_same_tag_resolves_to_new_image_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Rebuilding the same image tag locally should rotate the worker on the next ensure."""
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path)

    handle = backend.ensure_worker(WorkerSpec("worker-a"), now=10.0)
    existing_container = fake_client.containers.by_name[handle.worker_id]

    fake_client.images.by_name[backend.config.image] = _FakeImage("sha256:image-v2")

    backend.ensure_worker(WorkerSpec("worker-a"), now=20.0)

    replacement_container = fake_client.containers.by_name[handle.worker_id]
    assert replacement_container is not existing_container
    assert existing_container.removed == 1
    assert len(fake_client.containers.run_calls) == 2
