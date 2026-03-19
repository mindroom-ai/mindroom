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

import pytest
import yaml

from mindroom.agents import _load_context_files
from mindroom.config.main import load_config
from mindroom.constants import (
    RuntimePaths,
    deserialize_runtime_paths,
    resolve_primary_runtime_paths,
    resolve_runtime_paths,
)
from mindroom.credentials import SHARED_CREDENTIALS_PATH_ENV
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    resolve_worker_key,
    worker_dir_name,
    worker_root_path,
)
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.backends._dedicated_worker_common import build_dedicated_worker_runtime_paths
from mindroom.workers.backends.docker import (
    DockerWorkerBackend,
    _load_docker_client_and_errors,
    ensure_docker_dependencies,
)
from mindroom.workers.backends.docker_config import (
    _default_docker_user_for_os,
    _DockerWorkerBackendConfig,
    _read_docker_user,
)
from mindroom.workers.backends.docker_projection import (
    _PROJECTED_CONFIGS_DIRNAME,
    _WORKER_CONFIG_STATE_DIRNAME,
    DockerProjectionManager,
)
from mindroom.workers.backends.local import local_worker_state_paths_for_root
from mindroom.workers.models import WorkerSpec
from mindroom.workers.runtime import primary_worker_backend_available, primary_worker_backend_name
from mindroom.workspaces import resolve_agent_workspace_from_state_path

_TEST_AUTH_TOKEN = "test-token"  # noqa: S105
_ROTATED_AUTH_TOKEN = "rotated-token"  # noqa: S105
_TEST_UNSCOPED_WORKER_KEY = "v1:default:unscoped:code"


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
    context_file = tmp_path / "agents" / "code" / "workspace" / "context.md"
    context_file.parent.mkdir(parents=True)
    context_file.write_text("# Context\n", encoding="utf-8")
    memory_root = tmp_path / "memory_files"
    memory_root.mkdir()
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
    knowledge_bases: [docs]
    context_files:
      - context.md
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
        },
    )


def _multi_agent_projected_config_fixture(tmp_path: Path) -> tuple[str, dict[str, Path]]:
    alpha_knowledge_root = tmp_path / "knowledge_alpha"
    alpha_knowledge_root.mkdir()
    (alpha_knowledge_root / "a.txt").write_text("alpha knowledge\n", encoding="utf-8")
    beta_knowledge_root = tmp_path / "knowledge_beta"
    beta_knowledge_root.mkdir()
    (beta_knowledge_root / "b.txt").write_text("beta knowledge\n", encoding="utf-8")
    alpha_context = tmp_path / "agents" / "alpha" / "workspace" / "alpha.md"
    alpha_context.parent.mkdir(parents=True)
    alpha_context.write_text("# Alpha\n", encoding="utf-8")
    beta_context = tmp_path / "agents" / "beta" / "workspace" / "beta.md"
    beta_context.parent.mkdir(parents=True)
    beta_context.write_text("# Beta\n", encoding="utf-8")
    memory_root = tmp_path / "memory_files"
    memory_root.mkdir()
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
      - alpha.md
  beta:
    display_name: Beta
    role: Beta test
    model: default
    worker_scope: shared
    knowledge_bases: [b]
    context_files:
      - beta.md
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
        },
    )


def _private_user_agent_projected_config_fixture(tmp_path: Path) -> tuple[str, dict[str, Path]]:
    alpha_knowledge_root = tmp_path / "knowledge_alpha"
    alpha_knowledge_root.mkdir()
    (alpha_knowledge_root / "a.txt").write_text("alpha knowledge\n", encoding="utf-8")
    beta_knowledge_root = tmp_path / "knowledge_beta"
    beta_knowledge_root.mkdir()
    (beta_knowledge_root / "b.txt").write_text("beta knowledge\n", encoding="utf-8")
    alpha_context = tmp_path / "agents" / "alpha" / "workspace" / "alpha.md"
    alpha_context.parent.mkdir(parents=True)
    alpha_context.write_text("# Alpha\n", encoding="utf-8")
    beta_context = tmp_path / "agents" / "beta" / "workspace" / "beta.md"
    beta_context.parent.mkdir(parents=True)
    beta_context.write_text("# Beta\n", encoding="utf-8")
    return (
        """
knowledge_bases:
  a:
    path: ./knowledge_alpha
  b:
    path: ./knowledge_beta
agents:
  alpha:
    display_name: Alpha
    role: Alpha test
    model: default
    private:
      per: user_agent
    knowledge_bases: [a]
    context_files:
      - alpha.md
  beta:
    display_name: Beta
    role: Beta test
    model: default
    worker_scope: shared
    knowledge_bases: [b]
    context_files:
      - beta.md
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
    worker_root = worker_root_path(tmp_path, _TEST_UNSCOPED_WORKER_KEY)
    state_root = str(worker_root)
    assert volumes[state_root]["bind"] == "/app/worker"

    projection_root = _projection_root(volumes)
    assert projection_root.parent == (
        worker_root_path(tmp_path, "__mindroom_root__").parent
        / _PROJECTED_CONFIGS_DIRNAME
        / worker_dir_name(_TEST_UNSCOPED_WORKER_KEY)
    )
    assert worker_root not in projection_root.parents
    assert str(tmp_path) not in volumes
    assert str(tmp_path / "mindroom_data") not in volumes
    assert str(projected_paths["plugin_root"].resolve()) not in volumes
    assert str(projected_paths["knowledge_root"].resolve()) not in volumes
    assert str(projected_paths["context_file"].resolve()) not in volumes
    assert str(projected_paths["memory_root"].resolve()) not in volumes
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
    assert (projection_root / ".env").read_text(encoding="utf-8") == ""
    assert (projection_root / ".projection-ready").read_text(encoding="utf-8") == "ready\n"
    assert (
        worker_root_path(tmp_path, _TEST_UNSCOPED_WORKER_KEY) / _WORKER_CONFIG_STATE_DIRNAME / "memory" / "file"
    ).is_dir()


def _backend(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    idle_timeout_seconds: float = 60.0,
    config_text: str = "agents: {}\n",
    runtime_paths: RuntimePaths | None = None,
    storage_path: Path | None = None,
    host_config_path: Path | None = None,
) -> tuple[DockerWorkerBackend, _FakeDockerClient, list[tuple[str, bool]]]:
    config = _DockerWorkerBackendConfig(
        image="ghcr.io/mindroom-ai/mindroom:latest",
        worker_port=8766,
        storage_mount_path="/app/worker",
        config_path="/app/config-host/config.yaml",
        host_config_path=tmp_path / "config.yaml" if host_config_path is None else host_config_path,
        idle_timeout_seconds=idle_timeout_seconds,
        ready_timeout_seconds=5.0,
        name_prefix="mindroom-worker",
        publish_host="127.0.0.1",
        endpoint_host="127.0.0.1",
        user="1000:1000",
        extra_env={"EXTRA_ENV": "present"},
        extra_labels={"mindroom.ai/tenant": "test"},
    )
    assert config.host_config_path is not None
    config.host_config_path.parent.mkdir(parents=True, exist_ok=True)
    config.host_config_path.write_text(config_text, encoding="utf-8")
    fake_client = _FakeDockerClient()
    fake_client.images.by_name[config.image] = _FakeImage("sha256:image-v1")
    sync_calls: list[tuple[str, bool]] = []

    monkeypatch.setattr(
        "mindroom.workers.backends.docker._load_docker_client_and_errors",
        lambda *_args, **_kwargs: (
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
    backend = DockerWorkerBackend(
        config=config,
        auth_token=_TEST_AUTH_TOKEN,
        storage_path=tmp_path if storage_path is None else storage_path,
        runtime_paths=runtime_paths,
    )
    monkeypatch.setattr(
        backend,
        "_wait_for_ready",
        lambda container: (f"http://127.0.0.1:{backend._container_host_port(container)}/api/sandbox-runner/execute"),
    )
    return backend, fake_client, sync_calls


def test_primary_worker_backend_available_uses_runtime_env_values(tmp_path: Path) -> None:
    """Docker backend availability should honor the explicit runtime context."""
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    (config_dir / ".env").write_text(
        (
            "MINDROOM_WORKER_BACKEND=docker\n"
            "MINDROOM_DOCKER_WORKER_IMAGE=test-image\n"
            "MINDROOM_SANDBOX_PROXY_TOKEN=test-token\n"
        ),
        encoding="utf-8",
    )
    runtime_paths = resolve_primary_runtime_paths(config_path=config_path)

    assert primary_worker_backend_name(runtime_paths) == "docker"
    assert runtime_paths.env_value("MINDROOM_DOCKER_WORKER_IMAGE") == "test-image"
    assert primary_worker_backend_available(
        runtime_paths,
        proxy_url=None,
        proxy_token=runtime_paths.env_value("MINDROOM_SANDBOX_PROXY_TOKEN"),
    )


def test_docker_worker_host_config_path_resolves_relative_to_runtime_config_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Relative Docker host-config paths should resolve against the runtime config directory, not cwd."""
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    config_path = config_dir / "config.yaml"
    config_path.write_text("agents: {}\n", encoding="utf-8")
    host_config_path = config_dir / "docker-host-config.yaml"
    host_config_path.write_text("agents: {}\n", encoding="utf-8")

    other_cwd = tmp_path / "other"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)

    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path,
        process_env={
            "MINDROOM_WORKER_BACKEND": "docker",
            "MINDROOM_DOCKER_WORKER_IMAGE": "ghcr.io/mindroom-ai/mindroom:latest",
            "MINDROOM_DOCKER_WORKER_HOST_CONFIG_PATH": "./docker-host-config.yaml",
        },
    )

    config = _DockerWorkerBackendConfig.from_runtime(runtime_paths)

    assert config.host_config_path == host_config_path.resolve()


@pytest.mark.parametrize(
    ("label_name"),
    [
        "mindroom.ai/launch-config-hash",
        "mindroom.ai/worker-key",
        "mindroom.ai/component",
    ],
)
def test_docker_worker_backend_rejects_reserved_extra_labels(
    tmp_path: Path,
    label_name: str,
) -> None:
    """Reserved Docker labels must fail fast instead of overriding backend-owned metadata."""
    with pytest.raises(WorkerBackendError, match="extra labels cannot override reserved labels"):
        _DockerWorkerBackendConfig(
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
            extra_labels={label_name: "user-value"},
        )


@pytest.mark.parametrize(
    ("label_name"),
    [
        "mindroom.ai/launch-config-hash",
        "mindroom.ai/worker-key",
        "mindroom.ai/component",
    ],
)
def test_docker_worker_config_rejects_reserved_extra_labels_from_env(
    tmp_path: Path,
    label_name: str,
) -> None:
    """Runtime env loading should reject reserved Docker labels before backend creation."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\n", encoding="utf-8")

    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path,
        process_env={
            "MINDROOM_WORKER_BACKEND": "docker",
            "MINDROOM_DOCKER_WORKER_IMAGE": "ghcr.io/mindroom-ai/mindroom:latest",
            "MINDROOM_DOCKER_WORKER_LABELS_JSON": json.dumps({label_name: "user-value"}),
        },
    )

    with pytest.raises(WorkerBackendError, match="extra labels cannot override reserved labels"):
        _DockerWorkerBackendConfig.from_runtime(runtime_paths)


@pytest.mark.parametrize(
    ("storage_mount_path", "config_path"),
    [
        ("/app/worker", "/app/worker/config.yaml"),
        ("/app/worker", "/app/worker/nested/config.yaml"),
        ("/app/worker/config-host", "/app/worker/config.yaml"),
    ],
)
def test_docker_worker_backend_rejects_overlapping_config_mount_targets(
    tmp_path: Path,
    storage_mount_path: str,
    config_path: str,
) -> None:
    """Projected config mounts must stay disjoint from the writable worker state root."""
    with pytest.raises(WorkerBackendError, match="config_path must mount outside the worker storage root"):
        _DockerWorkerBackendConfig(
            image="ghcr.io/mindroom-ai/mindroom:latest",
            worker_port=8766,
            storage_mount_path=storage_mount_path,
            config_path=config_path,
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


@pytest.mark.parametrize(
    ("storage_mount_path", "config_path"),
    [
        ("/app/worker", "/app/worker/config.yaml"),
        ("/app/worker", "/app/worker/nested/config.yaml"),
        ("/app/worker/config-host", "/app/worker/config.yaml"),
    ],
)
def test_docker_worker_config_rejects_overlapping_config_mount_targets_from_env(
    tmp_path: Path,
    storage_mount_path: str,
    config_path: str,
) -> None:
    """Runtime env loading should reject overlapping Docker config and worker-state mount targets."""
    runtime_config_path = tmp_path / "config.yaml"
    runtime_config_path.write_text("agents: {}\n", encoding="utf-8")

    runtime_paths = resolve_runtime_paths(
        config_path=runtime_config_path,
        storage_path=tmp_path,
        process_env={
            "MINDROOM_WORKER_BACKEND": "docker",
            "MINDROOM_DOCKER_WORKER_IMAGE": "ghcr.io/mindroom-ai/mindroom:latest",
            "MINDROOM_DOCKER_WORKER_STORAGE_MOUNT_PATH": storage_mount_path,
            "MINDROOM_DOCKER_WORKER_CONFIG_PATH": config_path,
        },
    )

    with pytest.raises(WorkerBackendError, match="config_path must mount outside the worker storage root"):
        _DockerWorkerBackendConfig.from_runtime(runtime_paths)


def _projection_signature_for_hash_seed(hash_seed: str, workspace_root: Path) -> dict[str, object]:
    script = (
        textwrap.dedent(
            """
        import json
        from pathlib import Path

        import yaml

        from mindroom.constants import resolve_runtime_paths
        from mindroom.workers.backends.docker_config import _DockerWorkerBackendConfig
        from mindroom.workers.backends.docker_projection import DockerProjectionManager
        from mindroom.workers.backends.local import local_worker_state_paths_for_root

        tmp_path = Path(__WORKSPACE_ROOT__)
        runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
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
        manager = DockerProjectionManager(
            config=config,
            projected_configs_root=tmp_path / "projections",
            runtime_paths=runtime_paths,
        )
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

    handle = backend.ensure_worker(WorkerSpec(_TEST_UNSCOPED_WORKER_KEY), now=10.0)

    assert handle.worker_key == _TEST_UNSCOPED_WORKER_KEY
    assert handle.backend_name == "docker"
    assert handle.status == "ready"
    assert handle.endpoint.endswith("/api/sandbox-runner/execute")
    assert handle.debug_metadata["state_root"] == str(worker_root_path(tmp_path, _TEST_UNSCOPED_WORKER_KEY))
    assert sync_calls == [(_TEST_UNSCOPED_WORKER_KEY, True)]

    run_call = fake_client.containers.run_calls[0]
    env = run_call["environment"]
    assert isinstance(env, dict)
    assert env["MINDROOM_SANDBOX_RUNNER_MODE"] == "true"
    assert env["MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE"] == "subprocess"
    assert env["MINDROOM_SANDBOX_PROXY_TOKEN"] == _TEST_AUTH_TOKEN
    assert env["MINDROOM_SANDBOX_DEDICATED_WORKER_KEY"] == _TEST_UNSCOPED_WORKER_KEY
    assert env["MINDROOM_SANDBOX_DEDICATED_WORKER_ROOT"] == "/app/worker"
    assert env["MINDROOM_SANDBOX_SHARED_STORAGE_ROOT"] == "/app/shared-storage"
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
    assert labels["mindroom.ai/worker-key"] == _TEST_UNSCOPED_WORKER_KEY
    assert labels["mindroom.ai/runtime-namespace"]
    assert labels["mindroom.ai/tenant"] == "test"

    metadata_path = worker_root_path(tmp_path, _TEST_UNSCOPED_WORKER_KEY) / "metadata" / "worker.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["status"] == "ready"
    assert metadata["startup_count"] == 1


def test_docker_backend_projects_assets_from_runtime_storage_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Projected Docker assets should resolve ${MINDROOM_STORAGE_PATH} from the active runtime."""
    runtime_storage = (tmp_path / "runtime-storage").resolve()
    plugin_root = runtime_storage / "plugins" / "runtime-plugin"
    plugin_root.mkdir(parents=True)
    (plugin_root / "plugin.py").write_text("PLUGIN_VERSION = 'runtime'\n", encoding="utf-8")
    knowledge_root = runtime_storage / "knowledge_docs"
    knowledge_root.mkdir()
    (knowledge_root / "guide.md").write_text("# Runtime Guide\n", encoding="utf-8")
    context_file = runtime_storage / "agents" / "code" / "workspace" / "context.md"
    context_file.parent.mkdir(parents=True, exist_ok=True)
    context_file.write_text("# Runtime Context\n", encoding="utf-8")

    config_path = tmp_path / "config.yaml"
    runtime_paths = resolve_runtime_paths(config_path=config_path, storage_path=runtime_storage)
    backend, fake_client, _sync_calls = _backend(
        monkeypatch,
        tmp_path,
        config_text="""
plugins:
  - ${MINDROOM_STORAGE_PATH}/plugins/runtime-plugin
knowledge_bases:
  docs:
    path: ${MINDROOM_STORAGE_PATH}/knowledge_docs
agents:
  code:
    display_name: Code
    role: Test
    model: default
    knowledge_bases: [docs]
    context_files:
      - context.md
models:
  default:
    provider: openai
    id: test-model
""".lstrip(),
        runtime_paths=runtime_paths,
        storage_path=runtime_storage,
    )

    backend.ensure_worker(WorkerSpec(_TEST_UNSCOPED_WORKER_KEY), now=10.0)

    volumes = fake_client.containers.run_calls[0]["volumes"]
    assert isinstance(volumes, dict)
    projection_root = _projection_root(volumes)
    projected_config = (projection_root / "config.yaml").read_text(encoding="utf-8")

    assert "plugins:\n- ./.mindroom-worker-assets/plugins/00-runtime-plugin" in projected_config
    assert "path: ./.mindroom-worker-assets/knowledge_bases/docs" in projected_config
    assert "- ./.mindroom-worker-assets/agents/code/context_files/00-context.md" in projected_config
    assert (projection_root / ".mindroom-worker-assets" / "plugins" / "00-runtime-plugin" / "plugin.py").read_text(
        encoding="utf-8",
    ) == "PLUGIN_VERSION = 'runtime'\n"
    assert (projection_root / ".mindroom-worker-assets" / "knowledge_bases" / "docs" / "guide.md").read_text(
        encoding="utf-8",
    ) == "# Runtime Guide\n"
    assert (
        projection_root / ".mindroom-worker-assets" / "agents" / "code" / "context_files" / "00-context.md"
    ).read_text(encoding="utf-8") == "# Runtime Context\n"


def test_docker_backend_rejects_symlinked_projected_directory_assets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Projected directory assets must fail closed instead of dereferencing symlinked children."""
    knowledge_root = tmp_path / "knowledge_docs"
    knowledge_root.mkdir()
    (knowledge_root / "guide.md").write_text("# Guide\n", encoding="utf-8")
    secret_path = tmp_path / ".env"
    secret_path.write_text("TOP_SECRET=shh\n", encoding="utf-8")
    (knowledge_root / "leak.env").symlink_to(secret_path)
    backend, _fake_client, _sync_calls = _backend(
        monkeypatch,
        tmp_path,
        config_text="""
knowledge_bases:
  docs:
    path: ./knowledge_docs
agents:
  code:
    display_name: Code
    role: Test
    model: default
    knowledge_bases: [docs]
models:
  default:
    provider: openai
    id: test-model
""".lstrip(),
    )

    with pytest.raises(WorkerBackendError, match="Docker worker asset must not contain symlinks"):
        backend.ensure_worker(WorkerSpec(_TEST_UNSCOPED_WORKER_KEY), now=10.0)


def test_docker_backend_rejects_symlinked_projected_file_assets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Projected file assets must reject symlink roots instead of escaping the agent workspace."""
    secret_path = tmp_path / "secret.md"
    secret_path.write_text("secret\n", encoding="utf-8")
    context_path = tmp_path / "agents" / "code" / "workspace" / "context.md"
    context_path.parent.mkdir(parents=True)
    context_path.symlink_to(secret_path)
    backend, _fake_client, _sync_calls = _backend(
        monkeypatch,
        tmp_path,
        config_text="""
agents:
  code:
    display_name: Code
    role: Test
    model: default
    context_files:
      - context.md
models:
  default:
    provider: openai
    id: test-model
""".lstrip(),
    )

    with pytest.raises(WorkerBackendError, match="Agent-owned paths must stay within"):
        backend.ensure_worker(WorkerSpec(_TEST_UNSCOPED_WORKER_KEY), now=10.0)


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

    backend.ensure_worker(WorkerSpec(_TEST_UNSCOPED_WORKER_KEY), now=10.0)

    assert synced_storage_roots == [tmp_path.resolve()]


def test_docker_backend_syncs_shared_credentials_from_runtime_shared_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Shared-credential mirroring should honor explicit runtime mirror paths."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\n", encoding="utf-8")
    shared_credentials_path = (tmp_path / "shared-credentials").resolve()
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path,
        process_env={SHARED_CREDENTIALS_PATH_ENV: str(shared_credentials_path)},
    )
    backend, _fake_client, _sync_calls = _backend(monkeypatch, tmp_path, runtime_paths=runtime_paths)
    synced_shared_paths: list[Path | None] = []

    def _capture_runtime_shared_path(
        _worker_key: str,
        **kwargs: object,
    ) -> None:
        credentials_manager = kwargs.get("credentials_manager")
        synced_shared_paths.append(
            None if credentials_manager is None else credentials_manager.shared_base_path,
        )

    monkeypatch.setattr(
        "mindroom.workers.backends.docker.sync_shared_credentials_to_worker",
        _capture_runtime_shared_path,
    )

    backend.ensure_worker(WorkerSpec(_TEST_UNSCOPED_WORKER_KEY), now=10.0)

    assert synced_shared_paths == [shared_credentials_path]


def test_docker_backend_commits_parent_runtime_env_into_worker_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Dedicated Docker workers should receive the committed public startup runtime only."""
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.yaml"
    config_text = "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n"
    config_path.write_text(config_text, encoding="utf-8")
    credentials_path = tmp_path / "google-credentials.json"
    credentials_path.write_text('{"type":"service_account"}\n', encoding="utf-8")
    runtime_storage = (tmp_path / "runtime-storage").resolve()
    (config_dir / ".env").write_text(
        (
            "MINDROOM_NAMESPACE=alpha1234\n"
            "MATRIX_HOMESERVER=http://dotenv-hs\n"
            "MATRIX_SERVER_NAME=alpha.example\n"
            "BROWSER_EXECUTABLE_PATH=/usr/bin/chromium\n"
            f"GOOGLE_APPLICATION_CREDENTIALS={credentials_path}\n"
            "GOOGLE_CLOUD_PROJECT=demo-project\n"
            "GOOGLE_CLOUD_LOCATION=us-central1\n"
            "ANTHROPIC_API_KEY=sk-secret\n"
        ),
        encoding="utf-8",
    )
    runtime_paths = resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=runtime_storage,
        process_env={
            "MINDROOM_SANDBOX_PROXY_TOKEN": "test-token",
            "MINDROOM_LOCAL_CLIENT_SECRET": "client-secret",
        },
    )
    backend, fake_client, _sync_calls = _backend(
        monkeypatch,
        tmp_path,
        config_text=config_text,
        runtime_paths=runtime_paths,
        storage_path=runtime_storage,
        host_config_path=config_path,
    )

    backend.ensure_worker(WorkerSpec(_TEST_UNSCOPED_WORKER_KEY), now=10.0)

    run_call = fake_client.containers.run_calls[0]
    env = run_call["environment"]
    assert isinstance(env, dict)
    committed_runtime = deserialize_runtime_paths(json.loads(env["MINDROOM_RUNTIME_PATHS_JSON"]))
    local_credentials_path = (
        worker_root_path(runtime_storage, _TEST_UNSCOPED_WORKER_KEY) / ".runtime" / credentials_path.name
    )

    assert env["MINDROOM_CONFIG_PATH"] == "/app/config-host/config.yaml"
    assert committed_runtime.config_path == Path("/app/config-host/config.yaml")
    assert committed_runtime.env_value("MINDROOM_NAMESPACE") == "alpha1234"
    assert committed_runtime.env_value("MATRIX_HOMESERVER") == "http://dotenv-hs"
    assert committed_runtime.env_value("MATRIX_SERVER_NAME") == "alpha.example"
    assert committed_runtime.env_value("BROWSER_EXECUTABLE_PATH") == "/usr/bin/chromium"
    assert (
        committed_runtime.env_value("GOOGLE_APPLICATION_CREDENTIALS") == "/app/worker/.runtime/google-credentials.json"
    )
    assert committed_runtime.env_value("GOOGLE_CLOUD_PROJECT") == "demo-project"
    assert committed_runtime.env_value("GOOGLE_CLOUD_LOCATION") == "us-central1"
    assert committed_runtime.env_value("ANTHROPIC_API_KEY") is None
    assert committed_runtime.env_value("MINDROOM_SANDBOX_PROXY_TOKEN") is None
    assert committed_runtime.env_value("MINDROOM_LOCAL_CLIENT_SECRET") is None
    assert local_credentials_path.read_text(encoding="utf-8") == '{"type":"service_account"}\n'


def test_docker_backend_commits_relative_file_backed_secrets_into_worker_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Dedicated Docker workers should preserve relative *_FILE secrets by copying them into worker state."""
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.yaml"
    config_text = "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n"
    config_path.write_text(config_text, encoding="utf-8")
    secret_path = config_dir / "secrets" / "openai.key"
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    secret_path.write_text("sk-relative\n", encoding="utf-8")
    runtime_storage = (tmp_path / "runtime-storage").resolve()
    (config_dir / ".env").write_text("OPENAI_API_KEY_FILE=secrets/openai.key\n", encoding="utf-8")
    runtime_paths = resolve_primary_runtime_paths(config_path=config_path, storage_path=runtime_storage)
    backend, fake_client, _sync_calls = _backend(
        monkeypatch,
        tmp_path,
        config_text=config_text,
        runtime_paths=runtime_paths,
        storage_path=runtime_storage,
        host_config_path=config_path,
    )

    backend.ensure_worker(WorkerSpec(_TEST_UNSCOPED_WORKER_KEY), now=10.0)

    run_call = fake_client.containers.run_calls[0]
    env = run_call["environment"]
    assert isinstance(env, dict)
    committed_runtime = deserialize_runtime_paths(json.loads(env["MINDROOM_RUNTIME_PATHS_JSON"]))
    local_secret_copy = (
        worker_root_path(runtime_storage, _TEST_UNSCOPED_WORKER_KEY)
        / ".runtime"
        / "file-secrets"
        / "OPENAI_API_KEY_FILE"
        / "openai.key"
    )

    assert (
        committed_runtime.env_value("OPENAI_API_KEY_FILE")
        == "/app/worker/.runtime/file-secrets/OPENAI_API_KEY_FILE/openai.key"
    )
    assert local_secret_copy.read_text(encoding="utf-8") == "sk-relative\n"


def test_docker_backend_rejects_symlinked_google_application_credentials_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Explicit ADC handoff should reject symlinked host paths for consistency with other worker copies."""
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.yaml"
    config_text = "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n"
    config_path.write_text(config_text, encoding="utf-8")
    runtime_storage = (tmp_path / "runtime-storage").resolve()
    real_credentials_path = tmp_path / "real-adc.json"
    real_credentials_path.write_text('{"type":"service_account"}\n', encoding="utf-8")
    symlinked_credentials_path = tmp_path / "adc-link.json"
    symlinked_credentials_path.symlink_to(real_credentials_path)
    (config_dir / ".env").write_text(
        f"GOOGLE_APPLICATION_CREDENTIALS={symlinked_credentials_path}\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=runtime_storage,
        process_env={"GOOGLE_APPLICATION_CREDENTIALS": str(symlinked_credentials_path)},
    )
    backend, _fake_client, _sync_calls = _backend(
        monkeypatch,
        tmp_path,
        config_text=config_text,
        runtime_paths=runtime_paths,
        storage_path=runtime_storage,
        host_config_path=config_path,
    )

    with pytest.raises(
        WorkerBackendError,
        match="Docker worker GOOGLE_APPLICATION_CREDENTIALS must not contain symlinks",
    ):
        backend.ensure_worker(WorkerSpec(_TEST_UNSCOPED_WORKER_KEY), now=10.0)


def test_docker_backend_rejects_symlinked_google_application_credentials_destination(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Explicit ADC handoff should reject worker-owned destination symlinks."""
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.yaml"
    config_text = "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n"
    config_path.write_text(config_text, encoding="utf-8")
    runtime_storage = (tmp_path / "runtime-storage").resolve()
    credentials_path = tmp_path / "google-credentials.json"
    credentials_path.write_text('{"type":"service_account"}\n', encoding="utf-8")
    victim_path = tmp_path / "victim.txt"
    victim_path.write_text("victim\n", encoding="utf-8")
    destination_path = worker_root_path(runtime_storage, _TEST_UNSCOPED_WORKER_KEY) / ".runtime" / credentials_path.name
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    destination_path.symlink_to(victim_path)
    (config_dir / ".env").write_text(
        f"GOOGLE_APPLICATION_CREDENTIALS={credentials_path}\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=runtime_storage,
        process_env={"GOOGLE_APPLICATION_CREDENTIALS": str(credentials_path)},
    )
    backend, _fake_client, _sync_calls = _backend(
        monkeypatch,
        tmp_path,
        config_text=config_text,
        runtime_paths=runtime_paths,
        storage_path=runtime_storage,
        host_config_path=config_path,
    )

    with pytest.raises(
        WorkerBackendError,
        match="Docker worker GOOGLE_APPLICATION_CREDENTIALS destination must stay within the worker state root",
    ):
        backend.ensure_worker(WorkerSpec(_TEST_UNSCOPED_WORKER_KEY), now=10.0)

    assert victim_path.read_text(encoding="utf-8") == "victim\n"


def test_docker_backend_redacts_projected_config_secrets_and_support_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Projected worker config should redact secrets and strip unrelated runtime state."""
    config_text, _projected_paths = _projected_config_fixture(tmp_path)
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, config_text=config_text)

    backend.ensure_worker(WorkerSpec(_TEST_UNSCOPED_WORKER_KEY), now=10.0)

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

    def _ensure(runtime_paths: RuntimePaths | None = None) -> None:
        captured["installed"] = True
        captured["runtime_paths"] = runtime_paths

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
    assert captured["runtime_paths"] is None
    assert client is fake_client
    assert errors is fake_errors


def test_ensure_docker_dependencies_uses_explicit_runtime_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Docker dependency bootstrap should honor the active runtime's config-adjacent .env."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("MINDROOM_NO_AUTO_INSTALL_TOOLS=true\n", encoding="utf-8")
    runtime_paths = resolve_runtime_paths(config_path=config_path, process_env={})
    captured: dict[str, object] = {}

    def _ensure_optional_deps(dependencies: list[str], extra_name: str, provided_runtime_paths: RuntimePaths) -> None:
        captured["dependencies"] = dependencies
        captured["extra_name"] = extra_name
        captured["runtime_paths"] = provided_runtime_paths

    monkeypatch.setattr("mindroom.workers.backends.docker.ensure_optional_deps", _ensure_optional_deps)

    ensure_docker_dependencies(runtime_paths)

    assert captured["dependencies"] == ["docker"]
    assert captured["extra_name"] == "docker"
    assert captured["runtime_paths"] is runtime_paths
    assert runtime_paths.env_value("MINDROOM_NO_AUTO_INSTALL_TOOLS") == "true"


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

    backend.ensure_worker(WorkerSpec(_TEST_UNSCOPED_WORKER_KEY), now=10.0)

    metadata_path = worker_root_path(tmp_path, _TEST_UNSCOPED_WORKER_KEY) / "metadata" / "worker.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["last_used_at"] = 0.0
    metadata["status"] = "ready"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    cleaned = backend.cleanup_idle_workers(now=100.0)

    assert [worker.worker_key for worker in cleaned] == [_TEST_UNSCOPED_WORKER_KEY]
    assert cleaned[0].status == "idle"

    container = next(iter(fake_client.containers.by_name.values()))
    assert container.stopped == 1
    assert container.status == "exited"

    worker_file = worker_root_path(tmp_path, _TEST_UNSCOPED_WORKER_KEY) / "workspace" / "note.txt"
    worker_file.parent.mkdir(parents=True, exist_ok=True)
    worker_file.write_text("still here", encoding="utf-8")
    assert worker_file.read_text(encoding="utf-8") == "still here"


def test_docker_backend_evict_without_preserving_state_removes_container_and_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Evicting without preserving state should remove both the container and state root."""
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path)

    backend.ensure_worker(WorkerSpec(_TEST_UNSCOPED_WORKER_KEY), now=10.0)
    worker_root = worker_root_path(tmp_path, _TEST_UNSCOPED_WORKER_KEY)
    volumes = fake_client.containers.run_calls[0]["volumes"]
    assert isinstance(volumes, dict)
    projection_root = _projection_root(volumes)

    result = backend.evict_worker(_TEST_UNSCOPED_WORKER_KEY, preserve_state=False, now=20.0)

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

    backend.ensure_worker(WorkerSpec(_TEST_UNSCOPED_WORKER_KEY), now=10.0)

    handle = backend.record_failure(_TEST_UNSCOPED_WORKER_KEY, "boom", now=11.0)

    assert handle.status == "failed"
    assert handle.failure_count == 1
    assert handle.failure_reason == "boom"

    container = next(iter(fake_client.containers.by_name.values()))
    assert container.stopped == 1
    assert container.status == "exited"


def test_docker_worker_config_rejects_reserved_extra_env_names_from_env(tmp_path: Path) -> None:
    """Docker worker env JSON should not override backend-owned control variables."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\n", encoding="utf-8")

    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path,
        process_env={
            "MINDROOM_WORKER_BACKEND": "docker",
            "MINDROOM_DOCKER_WORKER_IMAGE": "ghcr.io/mindroom-ai/mindroom:latest",
            "MINDROOM_DOCKER_WORKER_ENV_JSON": json.dumps(
                {
                    "MINDROOM_RUNTIME_PATHS_JSON": "override",
                    "MINDROOM_STORAGE_PATH": str((tmp_path / "escape").resolve()),
                },
            ),
        },
    )

    with pytest.raises(
        WorkerBackendError,
        match="MINDROOM_RUNTIME_PATHS_JSON, MINDROOM_STORAGE_PATH",
    ):
        _DockerWorkerBackendConfig.from_runtime(runtime_paths)


def test_build_dedicated_worker_runtime_paths_rejects_reserved_extra_env_names(tmp_path: Path) -> None:
    """Dedicated worker runtime payloads should reject reserved extra env names."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\n", encoding="utf-8")
    runtime_paths = resolve_runtime_paths(config_path=config_path, storage_path=tmp_path)

    with pytest.raises(WorkerBackendError, match="MINDROOM_STORAGE_PATH"):
        build_dedicated_worker_runtime_paths(
            runtime_paths=runtime_paths,
            backend_name="Docker",
            worker_key=_TEST_UNSCOPED_WORKER_KEY,
            config_path=Path("/app/config-host/config.yaml"),
            dedicated_root=Path("/app/worker"),
            local_dedicated_root=tmp_path / "worker-root",
            worker_port=8766,
            shared_storage_root="/app/shared-storage",
            extra_env={"MINDROOM_STORAGE_PATH": str((tmp_path / "escape").resolve())},
        )


def test_docker_projected_context_files_load_in_worker_runtime(tmp_path: Path) -> None:
    """Projected Docker context files should still load through the worker runtime."""
    config_text, _projected_paths = _projected_config_fixture(tmp_path)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(config_text, encoding="utf-8")
    runtime_paths = resolve_runtime_paths(config_path=config_path, storage_path=tmp_path)
    config = _DockerWorkerBackendConfig(
        image="ghcr.io/mindroom-ai/mindroom:latest",
        worker_port=8766,
        storage_mount_path="/app/worker",
        config_path="/app/config-host/config.yaml",
        host_config_path=config_path,
        idle_timeout_seconds=60.0,
        ready_timeout_seconds=5.0,
        name_prefix="mindroom-worker",
        publish_host="127.0.0.1",
        endpoint_host="127.0.0.1",
        user="1000:1000",
        extra_env={},
        extra_labels={},
    )
    manager = DockerProjectionManager(
        config=config,
        projected_configs_root=tmp_path / _PROJECTED_CONFIGS_DIRNAME,
        runtime_paths=runtime_paths,
    )
    worker_paths = local_worker_state_paths_for_root(worker_root_path(tmp_path, _TEST_UNSCOPED_WORKER_KEY))
    projection = manager.projected_config(worker_paths, worker_key=_TEST_UNSCOPED_WORKER_KEY, materialize=True)
    projected_config = yaml.safe_load(projection.projected_yaml)
    projected_context_file = projected_config["agents"]["code"]["context_files"][0]
    worker_runtime = build_dedicated_worker_runtime_paths(
        runtime_paths=runtime_paths,
        backend_name="Docker",
        worker_key=_TEST_UNSCOPED_WORKER_KEY,
        config_path=projection.root / "config.yaml",
        dedicated_root=worker_paths.root,
        local_dedicated_root=worker_paths.root,
        worker_port=8766,
        shared_storage_root=str(worker_paths.root),
        extra_env={},
    )

    loaded = _load_context_files(
        [projected_context_file],
        worker_runtime,
        agent_name="code",
        storage_path=worker_runtime.storage_root,
    )

    assert len(loaded) == 1
    assert loaded[0].kind == "personality"
    assert loaded[0].title == "00-context.md"
    assert loaded[0].body == "# Context"


def test_docker_backend_recreates_container_when_launch_config_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Token and launch-config changes should force worker recreation."""
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path)

    first_handle = backend.ensure_worker(WorkerSpec(_TEST_UNSCOPED_WORKER_KEY), now=10.0)
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

    updated_backend.ensure_worker(WorkerSpec(_TEST_UNSCOPED_WORKER_KEY), now=20.0)

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

    first_handle = backend.ensure_worker(WorkerSpec(_TEST_UNSCOPED_WORKER_KEY), now=10.0)
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

    second_handle = updated_backend.ensure_worker(WorkerSpec(_TEST_UNSCOPED_WORKER_KEY), now=20.0)

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
        lambda *_args, **_kwargs: (
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

    first_handle = first_backend.ensure_worker(WorkerSpec(_TEST_UNSCOPED_WORKER_KEY), now=10.0)
    second_handle = second_backend.ensure_worker(WorkerSpec(_TEST_UNSCOPED_WORKER_KEY), now=20.0)

    assert first_handle.worker_id != second_handle.worker_id
    assert len(fake_client.containers.run_calls) == 2
    assert fake_client.containers.run_calls[0]["name"] != fake_client.containers.run_calls[1]["name"]


def test_docker_backend_recreates_container_when_storage_mount_does_not_match(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Worker reuse should fail closed when an existing container points at the wrong state root."""
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path)

    handle = backend.ensure_worker(WorkerSpec(_TEST_UNSCOPED_WORKER_KEY), now=10.0)
    existing_container = fake_client.containers.by_name[handle.worker_id]
    existing_container.attrs["Mounts"][0]["Source"] = str(tmp_path / "wrong-root")

    backend.ensure_worker(WorkerSpec(_TEST_UNSCOPED_WORKER_KEY), now=20.0)

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

    handle = backend.ensure_worker(WorkerSpec(_TEST_UNSCOPED_WORKER_KEY), now=10.0)
    existing_container = fake_client.containers.by_name[handle.worker_id]

    (tmp_path / "config.yaml").write_text("agents:\n  code:\n    tools: [shell]\n", encoding="utf-8")

    backend.ensure_worker(WorkerSpec(_TEST_UNSCOPED_WORKER_KEY), now=20.0)

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

    handle = backend.ensure_worker(WorkerSpec(_TEST_UNSCOPED_WORKER_KEY), now=10.0)
    existing_container = fake_client.containers.by_name[handle.worker_id]
    first_volumes = fake_client.containers.run_calls[0]["volumes"]
    assert isinstance(first_volumes, dict)
    first_projection_root = _projection_root(first_volumes)

    updated_context_file = projected_paths["context_file"].with_suffix(".updated.md")
    updated_context_file.write_text("# Updated Context\n", encoding="utf-8")
    updated_context_file.replace(projected_paths["context_file"])

    backend.ensure_worker(WorkerSpec(_TEST_UNSCOPED_WORKER_KEY), now=20.0)

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

    handle = backend.ensure_worker(WorkerSpec(_TEST_UNSCOPED_WORKER_KEY), now=10.0)
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

    backend.ensure_worker(WorkerSpec(_TEST_UNSCOPED_WORKER_KEY), now=20.0)

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

    assert projected_paths["alpha_context"].resolve() not in projection_root.parents
    assert projected_paths["beta_context"].resolve() not in projection_root.parents
    assert projected_paths["alpha_knowledge_root"].resolve() not in projection_root.parents
    assert projected_paths["beta_knowledge_root"].resolve() not in projection_root.parents


def test_docker_backend_projects_context_files_from_canonical_agent_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Shared workers should project context files from the agent workspace, not the config dir."""
    (tmp_path / "README.md").write_text("CONFIG ROOT FILE\n", encoding="utf-8")
    workspace_readme = tmp_path / "agents" / "alpha" / "workspace" / "README.md"
    workspace_readme.parent.mkdir(parents=True)
    workspace_readme.write_text("AGENT WORKSPACE FILE\n", encoding="utf-8")
    backend, fake_client, _sync_calls = _backend(
        monkeypatch,
        tmp_path,
        config_text="""
agents:
  alpha:
    display_name: Alpha
    role: Alpha test
    model: default
    worker_scope: shared
    context_files:
      - README.md
models:
  default:
    provider: openai
    id: test-model
""".lstrip(),
    )

    backend.ensure_worker(WorkerSpec("v1:default:shared:alpha"), now=10.0)

    volumes = fake_client.containers.run_calls[0]["volumes"]
    assert isinstance(volumes, dict)
    projection_root = _projection_root(volumes)
    projected_readme = (
        projection_root / ".mindroom-worker-assets" / "agents" / "alpha" / "context_files" / "00-README.md"
    )

    assert projected_readme.read_text(encoding="utf-8") == "AGENT WORKSPACE FILE\n"


def test_docker_backend_projects_only_private_user_agent_assets_for_private_agent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Private user-agent workers should not snapshot unrelated agents or knowledge bases."""
    config_text, projected_paths = _private_user_agent_projected_config_fixture(tmp_path)
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, config_text=config_text)
    worker_key = resolve_worker_key(
        "user_agent",
        ToolExecutionIdentity(
            channel="matrix",
            agent_name="alpha",
            requester_id="@alice:example.org",
            room_id="!room:example.org",
            thread_id=None,
            resolved_thread_id=None,
            session_id=None,
            tenant_id="tenant-123",
        ),
        agent_name="alpha",
    )
    assert worker_key is not None

    backend.ensure_worker(WorkerSpec(worker_key, private_agent_names=frozenset({"alpha"})), now=10.0)

    volumes = fake_client.containers.run_calls[0]["volumes"]
    assert isinstance(volumes, dict)
    projection_root = _projection_root(volumes)
    projected_config_data = yaml.safe_load((projection_root / "config.yaml").read_text(encoding="utf-8"))

    assert set(projected_config_data["agents"]) == {"alpha"}
    assert set(projected_config_data["knowledge_bases"]) == {"a"}
    assert (
        projection_root / ".mindroom-worker-assets" / "agents" / "alpha" / "context_files" / "00-alpha.md"
    ).read_text(encoding="utf-8") == "# Alpha\n"
    assert not (
        projection_root / ".mindroom-worker-assets" / "agents" / "beta" / "context_files" / "00-beta.md"
    ).exists()
    assert (projection_root / ".mindroom-worker-assets" / "knowledge_bases" / "a" / "a.txt").read_text(
        encoding="utf-8",
    ) == "alpha knowledge\n"
    assert not (projection_root / ".mindroom-worker-assets" / "knowledge_bases" / "b" / "b.txt").exists()

    assert projected_paths["alpha_context"].resolve() not in projection_root.parents
    assert projected_paths["beta_context"].resolve() not in projection_root.parents
    assert projected_paths["alpha_knowledge_root"].resolve() not in projection_root.parents
    assert projected_paths["beta_knowledge_root"].resolve() not in projection_root.parents


def test_docker_backend_projects_private_template_dirs_for_private_agents(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Projected configs should keep private template scaffolds usable inside Docker workers."""
    template_dir = tmp_path / "template"
    template_dir.mkdir()
    (template_dir / "README.md").write_text("template scaffold\n", encoding="utf-8")
    backend, fake_client, _sync_calls = _backend(
        monkeypatch,
        tmp_path,
        config_text="""
agents:
  alpha:
    display_name: Alpha
    role: Test
    model: default
    private:
      per: user_agent
      template_dir: ./template
models:
  default:
    provider: openai
    id: test-model
""".lstrip(),
    )
    worker_key = resolve_worker_key(
        "user_agent",
        ToolExecutionIdentity(
            channel="matrix",
            agent_name="alpha",
            requester_id="@alice:example.org",
            room_id="!room:example.org",
            thread_id=None,
            resolved_thread_id=None,
            session_id=None,
            tenant_id="tenant-123",
        ),
        agent_name="alpha",
    )
    assert worker_key is not None

    backend.ensure_worker(WorkerSpec(worker_key, private_agent_names=frozenset({"alpha"})), now=10.0)

    volumes = fake_client.containers.run_calls[0]["volumes"]
    assert isinstance(volumes, dict)
    projection_root = _projection_root(volumes)
    projected_config = yaml.safe_load((projection_root / "config.yaml").read_text(encoding="utf-8"))

    assert projected_config["agents"]["alpha"]["private"]["template_dir"] == (
        "./.mindroom-worker-assets/agents/alpha/private/template_dir"
    )
    assert (
        projection_root / ".mindroom-worker-assets" / "agents" / "alpha" / "private" / "template_dir" / "README.md"
    ).read_text(encoding="utf-8") == "template scaffold\n"

    projected_runtime_paths = resolve_runtime_paths(
        config_path=projection_root / "config.yaml",
        storage_path=tmp_path / "projected-storage",
    )
    projected_runtime_config = load_config(projected_runtime_paths)
    workspace = resolve_agent_workspace_from_state_path(
        "alpha",
        projected_runtime_config,
        runtime_paths=projected_runtime_paths,
        state_storage_path=tmp_path / "private-root",
        use_state_storage_path=True,
        create=True,
    )

    assert workspace is not None
    assert (workspace.root / "README.md").read_text(encoding="utf-8") == "template scaffold\n"


def test_docker_backend_shared_worker_mounts_canonical_agent_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Shared workers should mount the canonical shared agent root into the container."""
    config_text, _projected_paths = _multi_agent_projected_config_fixture(tmp_path)
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, config_text=config_text)
    worker_key = resolve_worker_key(
        "shared",
        ToolExecutionIdentity(
            channel="matrix",
            agent_name="alpha",
            requester_id="@alice:example.org",
            room_id="!room:example.org",
            thread_id=None,
            resolved_thread_id=None,
            session_id=None,
            tenant_id="tenant-123",
        ),
        agent_name="alpha",
    )

    backend.ensure_worker(WorkerSpec(worker_key), now=10.0)

    volumes = fake_client.containers.run_calls[0]["volumes"]
    assert isinstance(volumes, dict)
    expected_agent_root = (tmp_path / "agents" / "alpha").resolve()
    assert volumes[str(expected_agent_root)] == {
        "bind": "/app/shared-storage/agents/alpha",
        "mode": "rw",
    }


def test_docker_backend_user_agent_requires_explicit_private_visibility(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """User-agent workers should fail closed without an explicit private-agent visibility set."""
    config_text, _projected_paths = _multi_agent_projected_config_fixture(tmp_path)
    backend, _fake_client, _sync_calls = _backend(monkeypatch, tmp_path, config_text=config_text)
    worker_key = resolve_worker_key(
        "user_agent",
        ToolExecutionIdentity(
            channel="matrix",
            agent_name="alpha",
            requester_id="@alice:example.org",
            room_id="!room:example.org",
            thread_id=None,
            resolved_thread_id=None,
            session_id=None,
            tenant_id="tenant-123",
        ),
        agent_name="alpha",
    )

    with pytest.raises(WorkerBackendError, match="user_agent workers require explicit private-agent visibility"):
        backend.ensure_worker(WorkerSpec(worker_key), now=10.0)


def test_docker_backend_rejects_unknown_worker_keys_for_scoped_mounts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Malformed worker keys must not fall back to mounting the whole storage root."""
    config_text, _projected_paths = _multi_agent_projected_config_fixture(tmp_path)
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, config_text=config_text)

    with pytest.raises(WorkerBackendError, match="Unsupported worker key"):
        backend.ensure_worker(WorkerSpec("not-a-valid-worker-key"), now=10.0)
    assert fake_client.containers.run_calls == []


def test_docker_backend_rejects_stale_scoped_worker_keys_for_projection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Scoped worker keys must fail closed when they no longer resolve to any configured agent."""
    config_text, _projected_paths = _multi_agent_projected_config_fixture(tmp_path)
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, config_text=config_text)

    with pytest.raises(WorkerBackendError, match="does not match any configured agent policy"):
        backend.ensure_worker(WorkerSpec("v1:default:shared:missing"), now=10.0)
    assert fake_client.containers.run_calls == []


def test_docker_backend_rejects_scoped_worker_keys_that_no_longer_match_agent_policy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A stale shared worker key must not broaden projection after an agent switches to user_agent isolation."""
    config_text, _projected_paths = _private_user_agent_projected_config_fixture(tmp_path)
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, config_text=config_text)

    with pytest.raises(WorkerBackendError, match="does not match any configured agent policy"):
        backend.ensure_worker(WorkerSpec("v1:default:shared:alpha"), now=10.0)
    assert fake_client.containers.run_calls == []


def test_docker_backend_rejects_unknown_unscoped_worker_keys_for_projection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Unknown unscoped worker keys must not broaden the projected config."""
    config_text, _projected_paths = _multi_agent_projected_config_fixture(tmp_path)
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, config_text=config_text)

    with pytest.raises(WorkerBackendError, match="does not match any configured agent policy"):
        backend.ensure_worker(WorkerSpec("v1:default:unscoped:missing"), now=10.0)
    assert fake_client.containers.run_calls == []


def test_docker_backend_rejects_stale_unscoped_worker_keys_for_projection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A stale unscoped worker key must fail closed after an agent changes isolation scope."""
    config_text, _projected_paths = _private_user_agent_projected_config_fixture(tmp_path)
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, config_text=config_text)

    with pytest.raises(WorkerBackendError, match="does not match any configured agent policy"):
        backend.ensure_worker(WorkerSpec("v1:default:unscoped:alpha"), now=10.0)
    assert fake_client.containers.run_calls == []


def test_docker_backend_rejects_stale_user_agent_worker_keys_for_projection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A stale user-agent worker key must fail closed instead of broadening projection."""
    config_text, _projected_paths = _private_user_agent_projected_config_fixture(tmp_path)
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, config_text=config_text)

    with pytest.raises(WorkerBackendError, match="does not match any configured agent policy"):
        backend.ensure_worker(
            WorkerSpec(
                "v1:tenant-123:user_agent:@alice:example.org:missing",
                private_agent_names=frozenset({"missing"}),
            ),
            now=10.0,
        )
    assert fake_client.containers.run_calls == []


def test_docker_backend_rejects_user_agent_worker_keys_when_agent_is_shared(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A user-agent worker key must fail closed when the agent still exists but is not user_agent scoped."""
    config_text, _projected_paths = _multi_agent_projected_config_fixture(tmp_path)
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, config_text=config_text)

    with pytest.raises(WorkerBackendError, match="does not match any configured agent policy"):
        backend.ensure_worker(
            WorkerSpec(
                "v1:default:user_agent:@alice:example.org:alpha",
                private_agent_names=frozenset({"alpha"}),
            ),
            now=10.0,
        )
    assert fake_client.containers.run_calls == []


def test_docker_backend_projects_only_user_scoped_assets_for_requester_worker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Requester-scoped Docker workers should only snapshot agents that resolve to worker_scope=user."""
    for name, contents in {
        "alpha": "alpha knowledge\n",
        "beta": "beta knowledge\n",
        "gamma": "gamma knowledge\n",
        "delta": "delta knowledge\n",
    }.items():
        knowledge_root = tmp_path / f"knowledge_{name}"
        knowledge_root.mkdir()
        (knowledge_root / f"{name}.txt").write_text(contents, encoding="utf-8")
        context_path = tmp_path / "agents" / name / "workspace" / f"{name}.md"
        context_path.parent.mkdir(parents=True, exist_ok=True)
        context_path.write_text(f"# {name.title()}\n", encoding="utf-8")

    backend, fake_client, _sync_calls = _backend(
        monkeypatch,
        tmp_path,
        config_text="""
knowledge_bases:
  a:
    path: ./knowledge_alpha
  b:
    path: ./knowledge_beta
  c:
    path: ./knowledge_gamma
  d:
    path: ./knowledge_delta
agents:
  alpha:
    display_name: Alpha
    role: Alpha test
    model: default
    worker_scope: user
    knowledge_bases: [a]
    context_files:
      - alpha.md
  beta:
    display_name: Beta
    role: Beta test
    model: default
    worker_scope: shared
    knowledge_bases: [b]
    context_files:
      - beta.md
  gamma:
    display_name: Gamma
    role: Gamma test
    model: default
    knowledge_bases: [c]
    context_files:
      - gamma.md
  delta:
    display_name: Delta
    role: Delta test
    model: default
    private:
      per: user
    knowledge_bases: [d]
    context_files:
      - delta.md
models:
  default:
    provider: openai
    id: test-model
""".lstrip(),
    )

    backend.ensure_worker(WorkerSpec("v1:tenant-123:user:@alice:example.org"), now=10.0)

    volumes = fake_client.containers.run_calls[0]["volumes"]
    assert isinstance(volumes, dict)
    projection_root = _projection_root(volumes)
    projected_config_data = yaml.safe_load((projection_root / "config.yaml").read_text(encoding="utf-8"))

    assert set(projected_config_data["agents"]) == {"alpha", "delta"}
    assert set(projected_config_data["knowledge_bases"]) == {"a", "d"}
    assert (
        projection_root / ".mindroom-worker-assets" / "agents" / "alpha" / "context_files" / "00-alpha.md"
    ).read_text(encoding="utf-8") == "# Alpha\n"
    assert (
        projection_root / ".mindroom-worker-assets" / "agents" / "delta" / "context_files" / "00-delta.md"
    ).read_text(encoding="utf-8") == "# Delta\n"
    assert not (
        projection_root / ".mindroom-worker-assets" / "agents" / "beta" / "context_files" / "00-beta.md"
    ).exists()
    assert not (
        projection_root / ".mindroom-worker-assets" / "agents" / "gamma" / "context_files" / "00-gamma.md"
    ).exists()
    assert (projection_root / ".mindroom-worker-assets" / "knowledge_bases" / "a" / "alpha.txt").read_text(
        encoding="utf-8",
    ) == "alpha knowledge\n"
    assert (projection_root / ".mindroom-worker-assets" / "knowledge_bases" / "d" / "delta.txt").read_text(
        encoding="utf-8",
    ) == "delta knowledge\n"
    assert not (projection_root / ".mindroom-worker-assets" / "knowledge_bases" / "b" / "beta.txt").exists()
    assert not (projection_root / ".mindroom-worker-assets" / "knowledge_bases" / "c" / "gamma.txt").exists()


def test_docker_backend_rejects_user_worker_keys_without_user_scoped_agents(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Requester-scoped workers must fail closed when no configured agent resolves to worker_scope=user."""
    config_text, _projected_paths = _multi_agent_projected_config_fixture(tmp_path)
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, config_text=config_text)

    with pytest.raises(WorkerBackendError, match="does not match any configured agent policy"):
        backend.ensure_worker(WorkerSpec("v1:tenant-123:user:@alice:example.org"), now=10.0)
    assert fake_client.containers.run_calls == []


def test_docker_backend_user_agent_mounts_private_root_from_worker_spec(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """User-agent workers should mount their private instance root when explicitly visible."""
    config_text, _projected_paths = _private_user_agent_projected_config_fixture(tmp_path)
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, config_text=config_text)
    worker_key = resolve_worker_key(
        "user_agent",
        ToolExecutionIdentity(
            channel="matrix",
            agent_name="alpha",
            requester_id="@alice:example.org",
            room_id="!room:example.org",
            thread_id=None,
            resolved_thread_id=None,
            session_id=None,
            tenant_id="tenant-123",
        ),
        agent_name="alpha",
    )

    backend.ensure_worker(WorkerSpec(worker_key, private_agent_names=frozenset({"alpha"})), now=10.0)

    volumes = fake_client.containers.run_calls[0]["volumes"]
    assert isinstance(volumes, dict)
    expected_private_root = (tmp_path / "private_instances" / worker_dir_name(worker_key) / "alpha").resolve()
    assert volumes[str(expected_private_root)] == {
        "bind": f"/app/shared-storage/private_instances/{worker_dir_name(worker_key)}/alpha",
        "mode": "rw",
    }
    assert all(spec["bind"] != "/app/shared-storage/agents/alpha" for spec in volumes.values())


def test_docker_backend_recreates_user_agent_container_when_private_visibility_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Changing user-agent private visibility should recreate the container."""
    config_text, _projected_paths = _private_user_agent_projected_config_fixture(tmp_path)
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, config_text=config_text)
    worker_key = resolve_worker_key(
        "user_agent",
        ToolExecutionIdentity(
            channel="matrix",
            agent_name="alpha",
            requester_id="@alice:example.org",
            room_id="!room:example.org",
            thread_id=None,
            resolved_thread_id=None,
            session_id=None,
            tenant_id="tenant-123",
        ),
        agent_name="alpha",
    )

    first_handle = backend.ensure_worker(WorkerSpec(worker_key, private_agent_names=frozenset()), now=10.0)
    first_container = fake_client.containers.by_name[first_handle.worker_id]

    backend.ensure_worker(WorkerSpec(worker_key, private_agent_names=frozenset({"alpha"})), now=20.0)

    replacement_container = fake_client.containers.by_name[first_handle.worker_id]
    assert replacement_container is not first_container
    assert first_container.removed == 1
    assert len(fake_client.containers.run_calls) == 2
    second_volumes = fake_client.containers.run_calls[-1]["volumes"]
    assert isinstance(second_volumes, dict)
    expected_private_root = (tmp_path / "private_instances" / worker_dir_name(worker_key) / "alpha").resolve()
    assert second_volumes[str(expected_private_root)] == {
        "bind": f"/app/shared-storage/private_instances/{worker_dir_name(worker_key)}/alpha",
        "mode": "rw",
    }


def test_docker_backend_redacts_authorization_headers_in_projected_model_extra_kwargs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Projected config should redact auth headers nested under model extra_kwargs."""
    backend, fake_client, _sync_calls = _backend(
        monkeypatch,
        tmp_path,
        config_text="""
agents:
  code:
    display_name: Code
    role: Test
    model: default
    worker_scope: shared
models:
  default:
    provider: openai
    id: test-model
    extra_kwargs:
      headers:
        Authorization: Bearer super-secret-token
        X-Trace-Id: keep-me
""".lstrip(),
    )

    backend.ensure_worker(WorkerSpec("v1:default:shared:code"), now=10.0)

    volumes = fake_client.containers.run_calls[0]["volumes"]
    assert isinstance(volumes, dict)
    projection_root = _projection_root(volumes)
    projected_config = yaml.safe_load((projection_root / "config.yaml").read_text(encoding="utf-8"))

    assert projected_config["models"]["default"]["extra_kwargs"]["headers"]["Authorization"] == "__REDACTED__"
    assert projected_config["models"]["default"]["extra_kwargs"]["headers"]["X-Trace-Id"] == "keep-me"


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


def test_docker_projection_hash_changes_when_container_config_filename_changes(tmp_path: Path) -> None:
    """Renaming the container-side config file should materialize a fresh projection snapshot."""
    host_config_path = tmp_path / "config.yaml"
    host_config_path.write_text(
        """
models:
  default:
    provider: openai
    id: test-model
agents: {}
router:
  model: default
""".lstrip(),
        encoding="utf-8",
    )
    runtime_paths = resolve_runtime_paths(config_path=host_config_path, storage_path=tmp_path)
    paths = local_worker_state_paths_for_root(tmp_path / "workers" / "projection-test")
    base_config = _DockerWorkerBackendConfig(
        image="ghcr.io/mindroom-ai/mindroom:latest",
        worker_port=8766,
        storage_mount_path="/app/worker",
        config_path="/app/config-host/config.yaml",
        host_config_path=host_config_path,
        idle_timeout_seconds=60.0,
        ready_timeout_seconds=5.0,
        name_prefix="mindroom-worker",
        publish_host="127.0.0.1",
        endpoint_host="127.0.0.1",
        user="1000:1000",
        extra_env={},
        extra_labels={},
    )

    first_manager = DockerProjectionManager(
        config=base_config,
        projected_configs_root=tmp_path / "projections",
        runtime_paths=runtime_paths,
    )
    first_projection = first_manager.projected_config(paths, materialize=True)

    renamed_manager = DockerProjectionManager(
        config=replace(base_config, config_path="/app/config-host/alt.yaml"),
        projected_configs_root=tmp_path / "projections",
        runtime_paths=runtime_paths,
    )
    renamed_projection = renamed_manager.projected_config(paths, materialize=True)

    assert renamed_projection.root != first_projection.root
    assert not first_projection.root.exists()
    assert (renamed_projection.root / "alt.yaml").is_file()
    assert not (renamed_projection.root / "config.yaml").exists()


def test_docker_projection_hash_changes_when_projected_file_mode_changes(tmp_path: Path) -> None:
    """Changing only a projected file mode should rebuild the snapshot and preserve the new mode."""
    plugin_dir = tmp_path / "plugins" / "demo"
    plugin_dir.mkdir(parents=True)
    plugin_file = plugin_dir / "helper.sh"
    plugin_file.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    plugin_file.chmod(0o644)

    host_config_path = tmp_path / "config.yaml"
    host_config_path.write_text(
        """
plugins:
  - ./plugins/demo
models:
  default:
    provider: openai
    id: test-model
agents: {}
router:
  model: default
""".lstrip(),
        encoding="utf-8",
    )
    runtime_paths = resolve_runtime_paths(config_path=host_config_path, storage_path=tmp_path)
    paths = local_worker_state_paths_for_root(tmp_path / "workers" / "projection-test")
    config = _DockerWorkerBackendConfig(
        image="ghcr.io/mindroom-ai/mindroom:latest",
        worker_port=8766,
        storage_mount_path="/app/worker",
        config_path="/app/config-host/config.yaml",
        host_config_path=host_config_path,
        idle_timeout_seconds=60.0,
        ready_timeout_seconds=5.0,
        name_prefix="mindroom-worker",
        publish_host="127.0.0.1",
        endpoint_host="127.0.0.1",
        user="1000:1000",
        extra_env={},
        extra_labels={},
    )
    manager = DockerProjectionManager(
        config=config,
        projected_configs_root=tmp_path / "projections",
        runtime_paths=runtime_paths,
    )

    first_projection = manager.projected_config(paths, materialize=True)
    first_projected_file = first_projection.root / ".mindroom-worker-assets" / "plugins" / "00-demo" / "helper.sh"
    assert first_projected_file.stat().st_mode & 0o777 == 0o644

    plugin_file.chmod(0o755)

    second_projection = manager.projected_config(paths, materialize=True)
    second_projected_file = second_projection.root / ".mindroom-worker-assets" / "plugins" / "00-demo" / "helper.sh"

    assert second_projection.root != first_projection.root
    assert not first_projection.root.exists()
    assert second_projected_file.stat().st_mode & 0o777 == 0o755


def test_docker_projection_hash_changes_when_projected_directory_mode_changes(tmp_path: Path) -> None:
    """Changing only a projected directory mode should rebuild the snapshot and preserve the new mode."""
    plugin_dir = tmp_path / "plugins" / "demo"
    plugin_dir.mkdir(parents=True)
    plugin_dir.chmod(0o755)
    (plugin_dir / "helper.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")

    host_config_path = tmp_path / "config.yaml"
    host_config_path.write_text(
        """
plugins:
  - ./plugins/demo
models:
  default:
    provider: openai
    id: test-model
agents: {}
router:
  model: default
""".lstrip(),
        encoding="utf-8",
    )
    runtime_paths = resolve_runtime_paths(config_path=host_config_path, storage_path=tmp_path)
    paths = local_worker_state_paths_for_root(tmp_path / "workers" / "projection-test")
    config = _DockerWorkerBackendConfig(
        image="ghcr.io/mindroom-ai/mindroom:latest",
        worker_port=8766,
        storage_mount_path="/app/worker",
        config_path="/app/config-host/config.yaml",
        host_config_path=host_config_path,
        idle_timeout_seconds=60.0,
        ready_timeout_seconds=5.0,
        name_prefix="mindroom-worker",
        publish_host="127.0.0.1",
        endpoint_host="127.0.0.1",
        user="1000:1000",
        extra_env={},
        extra_labels={},
    )
    manager = DockerProjectionManager(
        config=config,
        projected_configs_root=tmp_path / "projections",
        runtime_paths=runtime_paths,
    )

    first_projection = manager.projected_config(paths, materialize=True)
    first_projected_dir = first_projection.root / ".mindroom-worker-assets" / "plugins" / "00-demo"
    assert first_projected_dir.stat().st_mode & 0o777 == 0o755

    plugin_dir.chmod(0o700)

    second_projection = manager.projected_config(paths, materialize=True)
    second_projected_dir = second_projection.root / ".mindroom-worker-assets" / "plugins" / "00-demo"

    assert second_projection.root != first_projection.root
    assert not first_projection.root.exists()
    assert second_projected_dir.stat().st_mode & 0o777 == 0o700


def test_docker_backend_rebuilds_incomplete_projection_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Interrupted projection writes should be repaired instead of being reused forever."""
    config_text, _projected_paths = _projected_config_fixture(tmp_path)
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, config_text=config_text)

    handle = backend.ensure_worker(WorkerSpec(_TEST_UNSCOPED_WORKER_KEY), now=10.0)
    existing_container = fake_client.containers.by_name[handle.worker_id]
    first_volumes = fake_client.containers.run_calls[0]["volumes"]
    assert isinstance(first_volumes, dict)
    projection_root = _projection_root(first_volumes)

    (projection_root / ".projection-ready").unlink()
    (projection_root / "config.yaml").write_text("broken: true\n", encoding="utf-8")

    backend.ensure_worker(WorkerSpec(_TEST_UNSCOPED_WORKER_KEY), now=20.0)

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
        lambda *_args, **_kwargs: (
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

    first_handle = backend.ensure_worker(WorkerSpec(_TEST_UNSCOPED_WORKER_KEY), now=10.0)
    first_container = fake_client.containers.by_name[first_handle.worker_id]

    second_handle = backend.ensure_worker(WorkerSpec(_TEST_UNSCOPED_WORKER_KEY), now=20.0)

    assert second_handle.worker_id == first_handle.worker_id
    assert fake_client.containers.by_name[second_handle.worker_id] is first_container
    assert len(fake_client.containers.run_calls) == 1
    assert first_container.removed == 0
    metadata_path = worker_root_path(tmp_path, _TEST_UNSCOPED_WORKER_KEY) / "metadata" / "worker.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["startup_count"] == 1
    assert metadata["last_started_at"] == 10.0
    assert metadata["last_used_at"] == 20.0
    assert metadata["status"] == "ready"


def test_docker_backend_recreates_container_when_same_tag_resolves_to_new_image_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Rebuilding the same image tag locally should rotate the worker on the next ensure."""
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path)

    handle = backend.ensure_worker(WorkerSpec(_TEST_UNSCOPED_WORKER_KEY), now=10.0)
    existing_container = fake_client.containers.by_name[handle.worker_id]

    fake_client.images.by_name[backend.config.image] = _FakeImage("sha256:image-v2")

    backend.ensure_worker(WorkerSpec(_TEST_UNSCOPED_WORKER_KEY), now=20.0)

    replacement_container = fake_client.containers.by_name[handle.worker_id]
    assert replacement_container is not existing_container
    assert existing_container.removed == 1
    assert len(fake_client.containers.run_calls) == 2
