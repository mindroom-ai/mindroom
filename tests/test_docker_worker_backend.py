"""Tests for the Docker worker backend."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import textwrap
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal

import httpx
import pytest
import yaml
from structlog.testing import capture_logs

from mindroom.config.main import load_config
from mindroom.config.personal_agents import CreatedAgentEntry, GeneratedAgentEdits, PersonalAgentRoster
from mindroom.config.worker_projection import worker_config_data
from mindroom.constants import (
    RuntimePaths,
    deserialize_startup_manifest,
    resolve_primary_runtime_paths,
    resolve_runtime_paths,
    runtime_paths_with_storage_root,
    sandbox_startup_manifest_path,
)
from mindroom.credentials.manager import CredentialsManager, sync_shared_credentials_to_worker
from mindroom.personal_agents import _append_personal_agent_roster_entry, _write_personal_agent_roster
from mindroom.private_storage.private_instance_identity_store import ensure_private_instance_identity
from mindroom.runtime_env_policy import (
    SANDBOX_RUNTIME_ENV_BY_KEY,
    SHARED_CREDENTIALS_PATH_ENV,
    WORKER_COMPUTER_ENABLED_ENV,
)
from mindroom.script_runs.models import script_worker_key_for_run
from mindroom.shared_directories.store import add_shared_directory_member, create_shared_directory
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    agent_state_root_path,
    private_instance_scope_root_path,
    resolve_unscoped_worker_key,
    resolve_worker_key,
    worker_dir_name,
    worker_root_path,
)
from mindroom.workers import progress as progress_module
from mindroom.workers import worker_retirement as worker_retirement_module
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.backend_kinds import WorkerBackendKind, worker_backend_kind
from mindroom.workers.backends import docker as docker_backend_module
from mindroom.workers.backends import docker_projection as docker_projection_module
from mindroom.workers.backends.docker import (
    _WORKER_CONTROL_DIRNAME,
    DockerWorkerBackend,
    _load_docker_client_and_errors,
    _WorkerImageIncompatibleError,
    check_docker_workers_absent_for_storage_upgrade,
    configure_docker_worker_backend,
    ensure_docker_dependencies,
)
from mindroom.workers.backends.docker_config import (
    DockerWorkerBackendConfig,
    _default_docker_user_for_os,
    _read_docker_user,
)
from mindroom.workers.backends.docker_projection import (
    _PROJECTED_CONFIGS_DIRNAME,
    DockerProjectionManager,
)
from mindroom.workers.backends.legacy_state_root_mounts import _remove_docker_workers_mounting_state_roots
from mindroom.workers.backends.local import local_worker_state_paths_for_root
from mindroom.workers.compatibility import WorkerProtocolMismatch
from mindroom.workers.models import WorkerLaunchConfig, WorkerReadyProgress, WorkerSpec
from mindroom.workers.protocol import WORKER_PROTOCOL_VERSION
from mindroom.workers.runtime import build_worker_launch_config, configured_primary_worker_manager_identity
from mindroom.workspaces import resolve_agent_workspace_from_state_path
from tests.conftest import test_runtime_paths
from tests.test_personal_agents_config import BASE as PERSONAL_AGENTS_BASE
from tests.test_personal_agents_config import _entry as personal_agent_entry

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

_TEST_AUTH_TOKEN = "test-token"  # noqa: S105
_ROTATED_AUTH_TOKEN = "rotated-token"  # noqa: S105
_TEST_UNSCOPED_WORKER_KEY = "v1:default:unscoped:code"
_STALE_IMAGE_MISMATCH = WorkerProtocolMismatch(error="stale worker image", worker_is_newer=False)


def _control_metadata_path(storage_root: Path, worker_key: str) -> Path:
    """Return one worker's control metadata, which lives outside its mounted state root."""
    control_root = storage_root / "workers" / _WORKER_CONTROL_DIRNAME / worker_dir_name(worker_key)
    return control_root / "metadata" / "worker.json"


class _FakeDockerError(Exception):
    pass


class _FakeNotFoundError(_FakeDockerError):
    pass


class _FakeContainer:
    def __init__(
        self,
        *,
        name: str,
        image_identity: str,
        host_port: int,
        environment: dict[str, str],
        labels: dict[str, str],
        status: str = "running",
    ) -> None:
        self.name = name
        self.status = status
        self.id = f"{name}-id"
        self.attrs = {
            "Image": image_identity,
            "State": {"Status": status},
            "Config": {
                "Env": [f"{key}={value}" for key, value in sorted(environment.items())],
                "Labels": dict(labels),
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
        self.logs_output: bytes = b""

    def reload(self) -> None:
        self.attrs["State"]["Status"] = self.status

    def start(self) -> None:
        self.started += 1
        self.status = "running"
        self.reload()

    def stop(self, timeout: int = 10) -> None:
        assert timeout > 0
        self.stopped += 1
        self.status = "exited"
        self.reload()

    def remove(self, force: bool = True) -> None:
        del force
        self.removed += 1
        self.status = "removed"

    def logs(self, *, tail: int = 100) -> bytes:
        assert tail > 0
        return self.logs_output


class _FakeContainersApi:
    def __init__(self, *, images: _FakeImagesApi | None = None) -> None:
        self.by_name: dict[str, _FakeContainer] = {}
        self.created_containers: list[_FakeContainer] = []
        self.run_calls: list[dict[str, object]] = []
        self.list_calls: list[dict[str, object]] = []
        self.next_host_port = 43001
        self.images = images

    def get(self, name: str) -> _FakeContainer:
        container = self.by_name.get(name)
        if container is None or container.status == "removed":
            raise _FakeNotFoundError(name)
        return container

    def list(self, **kwargs: object) -> Sequence[_FakeContainer]:
        assert kwargs.get("all") is True
        raw_filters = kwargs.get("filters")
        applied_filters = dict(raw_filters) if isinstance(raw_filters, dict) else {}
        self.list_calls.append(applied_filters)
        label_filters = applied_filters.get("label", [])
        expected_labels = {
            item.split("=", 1)[0]: item.split("=", 1)[1]
            for item in label_filters
            if isinstance(item, str) and "=" in item
        }
        return [
            container
            for container in self.by_name.values()
            if container.status != "removed"
            and not any(container.attrs["Config"]["Labels"].get(key) != value for key, value in expected_labels.items())
        ]

    def run(self, image: str, **kwargs: object) -> _FakeContainer:
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
            image_identity=image_identity,
            host_port=host_port,
            environment=dict(environment) if isinstance(environment, dict) else {},
            labels=dict(labels) if isinstance(labels, dict) else {},
        )
        if isinstance(volumes, list):
            container.attrs["Mounts"] = [
                {
                    "Type": "bind",
                    "Source": source,
                    "Destination": destination,
                    "Mode": mode,
                    "RW": mode != "ro",
                }
                for source, destination, mode in (volume.rsplit(":", 2) for volume in volumes)
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
        self.pulls: list[str] = []
        self.pull_image_id: str | None = None
        self.gets: list[str] = []

    def get(self, name: str) -> _FakeImage:
        self.gets.append(name)
        image = self.by_name.get(name)
        if image is None:
            raise _FakeNotFoundError(name)
        return image

    def pull(self, name: str) -> _FakeImage:
        self.pulls.append(name)
        if self.pull_image_id is None:
            msg = f"pull failed: {name}"
            raise _FakeDockerError(msg)
        image = _FakeImage(self.pull_image_id)
        self.by_name[name] = image
        return image


class _FakeDockerClient:
    def __init__(self) -> None:
        self.api = SimpleNamespace(timeout=None)
        self.images = _FakeImagesApi()
        self.containers = _FakeContainersApi(images=self.images)


class _WorkerThreadContext(threading.local):
    worker_key: str | None = None


class _ConcurrentTagUpdate:
    def __init__(
        self,
        backend: DockerWorkerBackend,
        *,
        recovering_key: str,
        stale_reader_key: str,
    ) -> None:
        self.backend = backend
        self.recovering_key = recovering_key
        self.stale_reader_key = stale_reader_key
        self.recovering_container_name = backend._container_name_for_worker(recovering_key)
        self.stale_reader_container_name = backend._container_name_for_worker(stale_reader_key)
        self.original_configured_image_id = backend._configured_image_id
        self.stale_image_id = self.original_configured_image_id()
        self.thread_context = _WorkerThreadContext()
        self.stale_image_resolved = threading.Event()
        self.recovering_worker_ready = threading.Event()

    def configured_image_id(self) -> str | None:
        image_id = self.original_configured_image_id()
        if self.thread_context.worker_key == self.stale_reader_key:
            self.stale_image_resolved.set()
            assert self.recovering_worker_ready.wait(timeout=5.0)
            return self.stale_image_id
        return image_id

    def wait_for_ready(self, container: _FakeContainer, **_kwargs: object) -> None:
        if container.attrs["Image"] == "sha256:image-v1":
            raise _WorkerImageIncompatibleError(_STALE_IMAGE_MISMATCH)
        if self.thread_context.worker_key == self.recovering_key:
            self.recovering_worker_ready.set()

    def ensure_worker(self, worker_key: str) -> None:
        self.thread_context.worker_key = worker_key
        self.backend.ensure_worker(_spec(self.backend, worker_key), now=0.0)


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
    display_name: Helpers
    role: Help
    agents: [code]
    mode: collaborate
administrators:
  - "@owner:example.org"
room_defaults:
  invite_users:
    - "@owner:example.org"
rooms:
  lobby:
    display_name: Lobby
mindroom_user:
  username: mindroom
""".lstrip(),
        {
            "plugin_root": plugin_root,
            "knowledge_root": knowledge_root,
            "context_file": context_file,
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
    return """
knowledge_bases:
  "docs:a":
    path: ./knowledge_collision_a
  "docs+a":
    path: ./knowledge_collision_b
agents:
  alpha:
    display_name: Alpha
    role: Alpha test
    model: default
    worker_scope: shared
    knowledge_bases: ["docs:a", "docs+a"]
models:
  default:
    provider: openai
    id: test-model
""".lstrip()


def _volumes_by_source(volumes: object) -> dict[str, dict[str, str]]:
    """Index single-destination fixtures; alias tests inspect the full mount list."""
    assert isinstance(volumes, list)
    return {
        source: {"bind": destination, "mode": mode}
        for source, destination, mode in (volume.rsplit(":", 2) for volume in volumes)
    }


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
    return projection_root


def _assert_projected_config_snapshot(projection_root: Path, tmp_path: Path) -> None:
    projected_config = (projection_root / "config.yaml").read_text(encoding="utf-8")
    assert "plugins:\n- path: ./.mindroom-worker-assets/plugins/00-my-plugin" in projected_config
    assert "path: ./.mindroom-worker-assets/knowledge_bases/docs" in projected_config
    assert "context.md" not in projected_config

    assert (tmp_path / "agents/code/workspace/context.md").read_text(encoding="utf-8") == "# Context\n"
    projected_plugin_path = projection_root / ".mindroom-worker-assets" / "plugins" / "00-my-plugin" / "plugin.py"
    assert projected_plugin_path.read_text(encoding="utf-8") == "PLUGIN_VERSION = 'v1'\n"
    projected_knowledge_path = projection_root / ".mindroom-worker-assets" / "knowledge_bases" / "docs" / "guide.md"
    assert projected_knowledge_path.read_text(encoding="utf-8") == "# Guide v1\n"
    assert (projection_root / ".env").read_text(encoding="utf-8") == ""
    assert (projection_root / ".projection-ready").read_text(encoding="utf-8") == "ready\n"
    assert not (projection_root / ".mindroom-worker-assets" / "agents").exists()


def test_docker_worker_projection_rewrites_mapping_plugin_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Mapping-style plugin entries must be copied into the worker projection."""
    plugin_root = tmp_path / "plugins" / "mapped-plugin"
    plugin_root.mkdir(parents=True)
    (plugin_root / "plugin.py").write_text("PLUGIN_VERSION = 'mapped'\n", encoding="utf-8")
    config_text = """
plugins:
  - path: ./plugins/mapped-plugin
    enabled: true
agents: {}
models:
  default:
    provider: openai
    id: test-model
router:
  model: default
""".lstrip()

    backend, _fake_client, _credential_sync_calls = _backend(
        monkeypatch,
        tmp_path,
        config_text=config_text,
    )
    projection = backend._projection_manager._projected_config(
        local_worker_state_paths_for_root(tmp_path / "workers" / "mapped-plugin"),
        launch=_launch(backend._runtime_paths),
        materialize=True,
    )

    projected_config = (projection.root / "config.yaml").read_text(encoding="utf-8")
    assert "path: ./.mindroom-worker-assets/plugins/00-mapped-plugin" in projected_config
    assert "enabled: true" in projected_config
    projected_plugin_path = projection.root / ".mindroom-worker-assets" / "plugins" / "00-mapped-plugin" / "plugin.py"
    assert projected_plugin_path.read_text(encoding="utf-8") == "PLUGIN_VERSION = 'mapped'\n"


def test_docker_worker_projection_resolves_config_includes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A split host config projects as the fully merged config for the worker."""
    (tmp_path / "agents.yaml").write_text(
        "code:\n  display_name: Code\n  role: Writes code\n",
        encoding="utf-8",
    )
    config_text = (
        "agents: !include agents.yaml\n"
        "models:\n  default:\n    provider: openai\n    id: test-model\n"
        "router:\n  model: default\n"
    )

    backend, _fake_client, _credential_sync_calls = _backend(
        monkeypatch,
        tmp_path,
        config_text=config_text,
    )
    projection = backend._projection_manager._projected_config(
        local_worker_state_paths_for_root(tmp_path / "workers" / "split-config"),
        launch=_launch(backend._runtime_paths),
        materialize=True,
    )

    projected = yaml.safe_load((projection.root / "config.yaml").read_text(encoding="utf-8"))
    assert projected["agents"]["code"]["display_name"] == "Code"


def test_docker_worker_projection_includes_personal_agents(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The applied config carries the agents generated from personal agent templates into the projection."""
    paths = test_runtime_paths(tmp_path)
    _append_personal_agent_roster_entry(paths, personal_agent_entry())
    config_text = yaml.safe_dump(PERSONAL_AGENTS_BASE)

    backend, _fake_client, _credential_sync_calls = _backend(
        monkeypatch,
        tmp_path,
        config_text=config_text,
        runtime_paths=paths,
        storage_root=paths.storage_root,
    )
    projection = backend._projection_manager._projected_config(
        local_worker_state_paths_for_root(tmp_path / "workers" / "personal-agents"),
        launch=_launch(backend._runtime_paths),
        materialize=True,
    )

    projected = yaml.safe_load((projection.root / "config.yaml").read_text(encoding="utf-8"))
    assert projected["agents"]["mind_alice_smith"]["display_name"] == "Alice's Mind"
    assert "personal_agents" not in projected


def test_docker_worker_projection_includes_created_agents_and_edits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Created agents and owners' edits reach Docker workers through the applied config, scoped per worker."""
    paths = test_runtime_paths(tmp_path)
    helper = {"display_name": "Helper", "model": "default", "tools": ["shell"], "worker_scope": "shared"}
    config_data = PERSONAL_AGENTS_BASE | {"agents": {"helper": helper | {"created_agents_per_owner": 1}}}
    created = CreatedAgentEntry(
        agent_name="helper_research",
        creator="helper",
        owner="@alice:example.org",
        edits=GeneratedAgentEdits(display_name="Research"),
    )
    edited = personal_agent_entry(edits={"display_name": "Scout"})
    _write_personal_agent_roster(paths, PersonalAgentRoster(entries=[edited], created=[created]))
    backend, _fake_client, _credential_sync_calls = _backend(
        monkeypatch,
        tmp_path,
        config_text=yaml.safe_dump(config_data),
        runtime_paths=paths,
        storage_root=paths.storage_root,
    )
    launch = _launch(backend._runtime_paths)

    def projected_agents(worker_key: str | None) -> dict[str, dict[str, object]]:
        state = local_worker_state_paths_for_root(tmp_path / "workers" / str(worker_key))
        projection = backend._projection_manager._projected_config(
            state,
            launch=launch,
            worker_key=worker_key,
            materialize=True,
        )
        return yaml.safe_load((projection.root / "config.yaml").read_text(encoding="utf-8"))["agents"]

    agents = projected_agents(None)
    assert agents["helper_research"]["display_name"] == "Research"
    assert agents["mind_alice_smith"]["display_name"] == "Scout"
    assert list(projected_agents("v1:default:shared:helper_research")) == ["helper_research"]


def _backend(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    idle_timeout_seconds: float = 60.0,
    security_policy: Literal["runtime_default", "computer"] = "runtime_default",
    runtime: str | None = None,
    config_text: str = "agents: {}\n",
    runtime_paths: RuntimePaths | None = None,
    storage_root: Path | None = None,
    host_config_path: Path | None = None,
    name_prefix: str = "mindroom-worker",
) -> tuple[DockerWorkerBackend, _FakeDockerClient, list[tuple[str, frozenset[str]]]]:
    config = DockerWorkerBackendConfig(
        image="ghcr.io/mindroom-ai/mindroom:latest",
        worker_port=8766,
        storage_mount_path="/app/worker",
        config_path="/app/config-host/config.yaml",
        host_config_path=tmp_path / "config.yaml" if host_config_path is None else host_config_path,
        idle_timeout_seconds=idle_timeout_seconds,
        security_policy=security_policy,
        runtime=runtime,
        ready_timeout_seconds=5.0,
        name_prefix=name_prefix,
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
    sync_calls: list[tuple[str, frozenset[str]]] = []

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
        allowed_services: frozenset[str] = frozenset(),
        **_kwargs: object,
    ) -> None:
        sync_calls.append((worker_key, allowed_services))

    monkeypatch.setattr(
        "mindroom.workers.backends.docker.sync_shared_credentials_to_worker",
        _record_sync_call,
    )
    backend = _docker_backend(
        config=config,
        auth_token=_TEST_AUTH_TOKEN,
        storage_root=tmp_path if storage_root is None else storage_root,
        runtime_paths=runtime_paths,
    )
    monkeypatch.setattr(backend, "_wait_for_ready", lambda _container, **_kwargs: None)
    return backend, fake_client, sync_calls


def _launch(
    runtime_paths: RuntimePaths,
    *,
    tool_validation_snapshot: dict[str, dict[str, object]] | None = None,
) -> WorkerLaunchConfig:
    """Return the launch a primary that applied the config file of ``runtime_paths`` now would capture."""
    config = load_config(runtime_paths, tolerate_plugin_load_errors=True)
    return build_worker_launch_config(config, tool_validation_snapshot=tool_validation_snapshot or {})


def _docker_backend(**kwargs: Any) -> DockerWorkerBackend:  # noqa: ANN401
    """Build a backend whose applied launch is read from its config file, which these tests make the primary's."""
    if kwargs.get("runtime_paths") is None:
        # A primary whose environment declares nothing, booted from the backend's config file.
        kwargs["runtime_paths"] = resolve_primary_runtime_paths(
            config_path=kwargs["config"].host_config_path,
            storage_path=kwargs["storage_root"],
            process_env={},
        )
    backend = DockerWorkerBackend(current_launch=lambda: _applied_launch(backend), **kwargs)
    return backend


# The tool validation snapshot each test backend's applied launch carries; the config is the backend's config file.
_APPLIED_TOOL_SNAPSHOTS: weakref.WeakKeyDictionary[DockerWorkerBackend, dict[str, dict[str, object]] | None] = (
    weakref.WeakKeyDictionary()
)


def _applied_launch(backend: DockerWorkerBackend) -> WorkerLaunchConfig:
    """Return the launch the backend's primary applies now: its config file, with the snapshot last set."""
    if backend in _APPLIED_TOOL_SNAPSHOTS and _APPLIED_TOOL_SNAPSHOTS[backend] is None:
        msg = "Dedicated workers need the applied config, and none is available yet."
        raise WorkerBackendError(msg)
    return _launch(backend._runtime_paths, tool_validation_snapshot=_APPLIED_TOOL_SNAPSHOTS.get(backend))


def _spec(
    backend: DockerWorkerBackend,
    worker_key: str,
    *,
    private_agent_names: frozenset[str] | None = None,
    state_scope_worker_key: str | None = None,
    mirror_grantable_credentials: bool = True,
    tool_validation_snapshot: dict[str, dict[str, object]] | None = None,
) -> WorkerSpec:
    """Return one worker request, applying ``tool_validation_snapshot`` to the backend's launch when given.

    The request carries no launch: the backend reads the applied one.
    """
    if tool_validation_snapshot is not None:
        _APPLIED_TOOL_SNAPSHOTS[backend] = tool_validation_snapshot
    return WorkerSpec(
        worker_key,
        private_agent_names=private_agent_names,
        mirror_grantable_credentials=mirror_grantable_credentials,
        state_scope_worker_key=state_scope_worker_key,
    )


def _docker_preflight_runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={
            "MINDROOM_WORKER_BACKEND": "docker",
            "MINDROOM_DOCKER_WORKER_IMAGE": "ghcr.io/mindroom-ai/mindroom:latest",
            "MINDROOM_DOCKER_WORKER_LABELS_JSON": json.dumps({"mindroom.ai/tenant": "test"}),
        },
    )


@pytest.mark.parametrize("status", ["running", "exited"])
@pytest.mark.parametrize("label", ["test", "historical"])
def test_docker_storage_preflight_rejects_workers_without_changing_bytes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    status: str,
    label: str,
) -> None:
    """Any remaining container blocks relocation, even with stale labels and metadata."""
    runtime_paths = _docker_preflight_runtime_paths(tmp_path)
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, runtime_paths=runtime_paths)
    handle = backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=0.0)
    container = fake_client.containers.get(handle.worker_id)
    container.status = status
    container.attrs["Config"]["Labels"]["mindroom.ai/tenant"] = label
    metadata = _control_metadata_path(tmp_path, _TEST_UNSCOPED_WORKER_KEY)
    before = metadata.read_bytes()
    sentinel = worker_root_path(tmp_path, _TEST_UNSCOPED_WORKER_KEY) / "retained.bin"
    sentinel.write_bytes(b"retained worker bytes")

    with pytest.raises(WorkerBackendError, match=r"(?i)remove|absent"):
        check_docker_workers_absent_for_storage_upgrade(runtime_paths, timeout_seconds=5.0)

    assert container.stopped == 0
    assert container.removed == 0
    assert metadata.read_bytes() == before
    assert sentinel.read_bytes() == b"retained worker bytes"


@pytest.mark.parametrize("metadata_bytes", [None, b"invalid json", b"{}"])
def test_docker_storage_preflight_ignores_stale_metadata_and_foreign_namespace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    metadata_bytes: bytes | None,
) -> None:
    """Only the live runtime namespace decides absence; durable metadata stays untouched."""
    runtime_paths = _docker_preflight_runtime_paths(tmp_path)
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, runtime_paths=runtime_paths)
    handle = backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=0.0)
    container = fake_client.containers.get(handle.worker_id)
    container.attrs["Config"]["Labels"]["mindroom.ai/runtime-namespace"] = "another-runtime"
    metadata = _control_metadata_path(tmp_path, _TEST_UNSCOPED_WORKER_KEY)
    if metadata_bytes is None:
        metadata.unlink()
    else:
        metadata.write_bytes(metadata_bytes)

    check_docker_workers_absent_for_storage_upgrade(runtime_paths, timeout_seconds=5.0)

    assert container.stopped == 0
    assert container.removed == 0
    assert (metadata.read_bytes() if metadata.exists() else None) == metadata_bytes
    assert 0.0 < fake_client.api.timeout <= 5.0


@pytest.mark.parametrize("inventory", [None, {}, "", [object()]])
def test_docker_storage_preflight_rejects_unverified_inventory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    inventory: object,
) -> None:
    """Malformed or nonempty API results cannot authorize migration."""
    runtime_paths = _docker_preflight_runtime_paths(tmp_path)
    _worker_backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, runtime_paths=runtime_paths)
    monkeypatch.setattr(fake_client.containers, "list", lambda **_kwargs: inventory)
    with pytest.raises(WorkerBackendError):
        check_docker_workers_absent_for_storage_upgrade(runtime_paths, timeout_seconds=5.0)


@pytest.mark.parametrize("failure", ["api", "malformed", "timeout"])
def test_docker_storage_preflight_blocks_unavailable_or_late_inventory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: str,
) -> None:
    """An API error or exhausted overall deadline cannot authorize migration."""
    runtime_paths = _docker_preflight_runtime_paths(tmp_path)
    _worker_backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, runtime_paths=runtime_paths)
    now = [0.0]
    monkeypatch.setattr("mindroom.workers.backends.docker.time.monotonic", lambda: now[0])

    def inventory(**kwargs: object) -> list[object]:
        assert kwargs["all"] is True
        assert kwargs["sparse"] is True
        if failure == "api":
            message = "unavailable"
            raise _FakeDockerError(message)
        if failure == "malformed":
            message = "invalid API payload"
            raise ValueError(message)
        now[0] = 6.0
        return []

    monkeypatch.setattr(fake_client.containers, "list", inventory)
    with pytest.raises(WorkerBackendError):
        check_docker_workers_absent_for_storage_upgrade(runtime_paths, timeout_seconds=5.0)


def test_docker_startup_retirement_installs_the_docker_extra_like_first_use(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Retirement runs before any first use, so it must auto-install the Docker SDK just as first use would."""
    runtime_paths = _docker_preflight_runtime_paths(tmp_path)
    _backend(monkeypatch, tmp_path, runtime_paths=runtime_paths)
    loader = docker_backend_module._load_docker_client_and_errors
    requested: list[bool] = []

    def record_loader(*args: object, ensure_dependencies: bool = True, **kwargs: object) -> object:
        requested.append(ensure_dependencies)
        return loader(*args, **kwargs)

    monkeypatch.setattr(docker_backend_module, "_load_docker_client_and_errors", record_loader)
    _remove_docker_workers_mounting_state_roots(runtime_paths)

    assert requested == [True]


def test_docker_startup_removes_workers_mounting_state_roots(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Containers from releases that mounted whole state roots are removed and recreated on their next ensure."""
    runtime_paths = _docker_preflight_runtime_paths(tmp_path)
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, runtime_paths=runtime_paths)
    current = backend.ensure_worker(_spec(backend, "v1:default:shared:current"), now=0.0)
    legacy = backend.ensure_worker(_spec(backend, "v1:default:shared:legacy"), now=0.0)
    foreign = backend.ensure_worker(_spec(backend, "v1:default:shared:foreign"), now=0.0)
    legacy_container = fake_client.containers.get(legacy.worker_id)
    foreign_container = fake_client.containers.get(foreign.worker_id)
    # Older releases never labeled their containers with the storage layout.
    del legacy_container.attrs["Config"]["Labels"]["mindroom.ai/storage-layout"]
    del foreign_container.attrs["Config"]["Labels"]["mindroom.ai/storage-layout"]
    foreign_container.attrs["Config"]["Labels"]["mindroom.ai/runtime-namespace"] = "another-runtime"
    sentinel = worker_root_path(tmp_path, "v1:default:shared:legacy") / "retained.bin"
    sentinel.write_bytes(b"retained worker bytes")

    with capture_logs() as logs:
        _remove_docker_workers_mounting_state_roots(runtime_paths)
    [warning] = [entry for entry in logs if entry["log_level"] == "warning"]
    assert warning["workers"] == [legacy_container.id]

    assert legacy_container.removed == 1
    assert fake_client.containers.get(current.worker_id).removed == 0
    assert foreign_container.removed == 0
    assert sentinel.read_bytes() == b"retained worker bytes"
    backend.ensure_worker(_spec(backend, "v1:default:shared:legacy"), now=10.0)
    recreated = fake_client.containers.get(legacy.worker_id)
    assert recreated is not legacy_container
    assert recreated.attrs["Config"]["Labels"]["mindroom.ai/storage-layout"] == "workspaces"


def test_docker_retirement_keeps_removing_old_containers_after_one_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A container Docker refuses to remove never shields the old containers listed after it."""
    runtime_paths = _docker_preflight_runtime_paths(tmp_path)
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, runtime_paths=runtime_paths)
    containers = []
    for name in ("first", "second", "third"):
        handle = backend.ensure_worker(_spec(backend, f"v1:default:shared:{name}"), now=0.0)
        container = fake_client.containers.get(handle.worker_id)
        del container.attrs["Config"]["Labels"]["mindroom.ai/storage-layout"]
        containers.append(container)

    def refuse_removal(force: bool = True) -> None:
        del force
        message = "removal of container is already in progress"
        raise _FakeDockerError(message)

    monkeypatch.setattr(containers[0], "remove", refuse_removal)

    with pytest.raises(WorkerBackendError, match=containers[0].id):
        _remove_docker_workers_mounting_state_roots(runtime_paths)

    assert [container.removed for container in containers[1:]] == [1, 1]


def _committed_startup_runtime(backend: DockerWorkerBackend, worker_key: str) -> RuntimePaths:
    """Return the runtime one Docker worker boots from, as its startup manifest records it."""
    manifest_path = sandbox_startup_manifest_path(backend._worker_paths(worker_key).state.root)
    runtime_paths, _tool_validation_snapshot = deserialize_startup_manifest(
        json.loads(manifest_path.read_text(encoding="utf-8")),
    )
    return runtime_paths


def _use_real_wait_for_ready(monkeypatch: pytest.MonkeyPatch, backend: DockerWorkerBackend) -> None:
    monkeypatch.setattr(
        backend,
        "_wait_for_ready",
        DockerWorkerBackend._wait_for_ready.__get__(backend),
    )


class _ProgressClock:
    """The warmup progress reporter's monotonic clock, which only the test moves."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


def _control_progress_clock(monkeypatch: pytest.MonkeyPatch) -> _ProgressClock:
    clock = _ProgressClock()
    monkeypatch.setattr(progress_module, "time", SimpleNamespace(monotonic=clock.monotonic))
    return clock


def _slow_configured_image_pull(
    monkeypatch: pytest.MonkeyPatch,
    backend: DockerWorkerBackend,
    clock: _ProgressClock,
) -> None:
    """Make pulling the configured image take two seconds on ``clock``, past the cold-start grace period."""
    pull = backend._pull_configured_image

    def _slow_pull() -> str:
        clock.now += 2.0
        return pull()

    monkeypatch.setattr(backend, "_pull_configured_image", _slow_pull)


def _install_health(monkeypatch: pytest.MonkeyPatch, answer: Callable[[str], httpx.Response]) -> None:
    """Answer every Docker worker health check with ``answer(url)``."""

    class _HealthClient:
        def __init__(self, *, timeout: float) -> None:
            assert timeout > 0

        def __enter__(self) -> _HealthClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def get(self, url: str) -> httpx.Response:
            return answer(url)

    monkeypatch.setattr("mindroom.workers.backends.docker.httpx.Client", _HealthClient)


def _install_health_by_image(
    monkeypatch: pytest.MonkeyPatch,
    fake_client: _FakeDockerClient,
    *,
    stale_protocol: int = 0,
) -> None:
    def _answer(url: str) -> httpx.Response:
        port = url.split(":")[2].split("/", maxsplit=1)[0]
        container = next(
            (
                item
                for item in fake_client.containers.by_name.values()
                if item.status == "running"
                and item.attrs["NetworkSettings"]["Ports"]["8766/tcp"][0]["HostPort"] == port
            ),
            None,
        )
        assert container is not None, f"no running fake container publishes host port {port}"
        protocol = WORKER_PROTOCOL_VERSION if container.attrs["Image"] == "sha256:image-v2" else stale_protocol
        return httpx.Response(200, json={"status": "ok", "mindroom_version": "test", "worker_protocol": protocol})

    _install_health(monkeypatch, _answer)


def _install_stale_health(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_health(
        monkeypatch,
        lambda _url: httpx.Response(200, json={"status": "ok", "mindroom_version": "test", "worker_protocol": 0}),
    )


def test_docker_backend_availability_uses_runtime_env_values(tmp_path: Path) -> None:
    """Docker backend availability should honor the explicit runtime context."""
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-6-astra\nagents: {}\nrouter:\n  model: default\n",
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

    assert worker_backend_kind(runtime_paths) is WorkerBackendKind.DOCKER
    assert runtime_paths.env_value("MINDROOM_DOCKER_WORKER_IMAGE") == "test-image"
    assert configured_primary_worker_manager_identity(runtime_paths) is not None


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

    config = DockerWorkerBackendConfig.from_runtime(runtime_paths)

    assert config.host_config_path == host_config_path.resolve()


def test_docker_worker_config_reads_the_env_file_under_blank_exports(tmp_path: Path) -> None:
    """A blank export does not hide the `.env` worker settings, and an exported empty user still means the image's."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\n", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "MINDROOM_WORKER_BACKEND=docker\nMINDROOM_DOCKER_WORKER_IMAGE=ghcr.io/mindroom-ai/mindroom:env\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path,
        process_env={
            "MINDROOM_WORKER_BACKEND": "",
            "MINDROOM_DOCKER_WORKER_IMAGE": "",
            "MINDROOM_DOCKER_WORKER_USER": "",
        },
    )

    config = DockerWorkerBackendConfig.from_runtime(runtime_paths)

    assert (config.image, config.user) == ("ghcr.io/mindroom-ai/mindroom:env", None)


def test_docker_worker_host_config_path_rejects_directories(tmp_path: Path) -> None:
    """Docker host-config path must point at a file when explicitly configured."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\n", encoding="utf-8")
    host_config_dir = tmp_path / "host-config"
    host_config_dir.mkdir()
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path,
        process_env={
            "MINDROOM_WORKER_BACKEND": "docker",
            "MINDROOM_DOCKER_WORKER_IMAGE": "ghcr.io/mindroom-ai/mindroom:latest",
            "MINDROOM_DOCKER_WORKER_HOST_CONFIG_PATH": str(host_config_dir),
        },
    )

    with pytest.raises(WorkerBackendError, match="points to a directory"):
        DockerWorkerBackendConfig.from_runtime(runtime_paths)


def test_runtime_paths_with_storage_root_updates_primary_path_env(tmp_path: Path) -> None:
    """Rebased primary runtimes should keep their exported path contract internally consistent."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\n", encoding="utf-8")
    runtime_paths = resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "runtime-a",
        process_env={
            "MINDROOM_WORKER_BACKEND": "docker",
            "MINDROOM_DOCKER_WORKER_IMAGE": "ghcr.io/mindroom-ai/mindroom:latest",
        },
    )

    rebased = runtime_paths_with_storage_root(runtime_paths, tmp_path / "runtime-b")

    assert rebased.storage_root == (tmp_path / "runtime-b").resolve()
    assert rebased.process_env["MINDROOM_CONFIG_PATH"] == str(config_path.resolve())
    assert rebased.process_env["MINDROOM_STORAGE_PATH"] == str((tmp_path / "runtime-b").resolve())


def test_docker_backend_signature_ignores_original_runtime_storage_root_when_explicit_storage_root_matches(
    tmp_path: Path,
) -> None:
    """The Docker manager signature should depend on the effective backend storage root, not stale callers."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\n", encoding="utf-8")
    env = {
        "MINDROOM_WORKER_BACKEND": "docker",
        "MINDROOM_DOCKER_WORKER_IMAGE": "ghcr.io/mindroom-ai/mindroom:latest",
    }

    first_runtime = resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "runtime-a",
        process_env=env,
    )
    second_runtime = resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "runtime-b",
        process_env=env,
    )

    shared_storage_root = tmp_path / "docker-storage"
    assert (
        configure_docker_worker_backend(
            first_runtime,
            auth_token=_TEST_AUTH_TOKEN,
            storage_root=shared_storage_root,
        ).signature
        == configure_docker_worker_backend(
            second_runtime,
            auth_token=_TEST_AUTH_TOKEN,
            storage_root=shared_storage_root,
        ).signature
    )


def test_docker_backend_host_config_path_reanchors_projection_and_runtime_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The host config path re-anchors the applied config's relative assets and sibling .env; its content is unused."""
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    runtime_config_path = runtime_dir / "config.yaml"
    runtime_config_path.write_text(
        "plugins:\n  - ./plugin.py\nmodels:\n  default:\n    provider: openai\n    id: test-model\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    (runtime_dir / "plugin.py").write_text("PLUGIN = 1\n", encoding="utf-8")
    (runtime_dir / ".env").write_text("BROWSER_EXECUTABLE_PATH=/runtime/browser\n", encoding="utf-8")

    # The same config directory as the Docker host sees it; a plugin only this file lists is never projected.
    host_config_dir = tmp_path / "host-config"
    host_config_dir.mkdir()
    host_config_path = host_config_dir / "docker-host-config.yaml"
    host_config_path.write_text(
        "plugins:\n  - ./host-only.py\nmodels:\n  default:\n    provider: openai\n    id: test-model\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    (host_config_dir / "plugin.py").write_text("PLUGIN = 1\n", encoding="utf-8")
    (host_config_dir / "host-only.py").write_text("PLUGIN = 2\n", encoding="utf-8")
    (host_config_dir / ".env").write_text("BROWSER_EXECUTABLE_PATH=/hostcfg/browser\n", encoding="utf-8")

    runtime_paths = resolve_runtime_paths(
        config_path=runtime_config_path,
        storage_path=tmp_path / "storage",
        process_env={
            "MINDROOM_WORKER_BACKEND": "docker",
            "MINDROOM_DOCKER_WORKER_IMAGE": "ghcr.io/mindroom-ai/mindroom:latest",
            "MINDROOM_DOCKER_WORKER_HOST_CONFIG_PATH": str(host_config_path),
        },
    )
    config = DockerWorkerBackendConfig.from_runtime(runtime_paths)
    fake_client = _FakeDockerClient()
    fake_client.images.by_name[config.image] = _FakeImage("sha256:image-v1")
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

    backend = _docker_backend(
        config=config,
        auth_token=_TEST_AUTH_TOKEN,
        storage_root=tmp_path / "storage",
        runtime_paths=runtime_paths,
    )
    projection = backend._projection_manager._projected_config(
        local_worker_state_paths_for_root(tmp_path / "workers" / "projection-test"),
        launch=_launch(runtime_paths),
        materialize=False,
    )

    assert backend._runtime_paths.config_path == host_config_path.resolve()
    assert backend._runtime_paths.env_value("BROWSER_EXECUTABLE_PATH") == "/hostcfg/browser"
    assert ".mindroom-worker-assets/plugins/00-plugin.py" in projection.projected_yaml
    assert [(asset.relative_path.as_posix(), asset.host_path) for asset in projection.assets] == [
        (".mindroom-worker-assets/plugins/00-plugin.py", (host_config_dir / "plugin.py").resolve()),
    ]
    assert "host-only" not in projection.projected_yaml


def test_docker_backend_config_signature_supports_nested_relative_host_config_paths(tmp_path: Path) -> None:
    """Nested relative host-config paths should resolve once and remain usable during signature generation."""
    config_dir = tmp_path / "cfg"
    nested_dir = config_dir / "nested"
    nested_dir.mkdir(parents=True)
    config_path = config_dir / "config.yaml"
    host_config_path = nested_dir / "docker-host-config.yaml"
    config_path.write_text("agents: {}\n", encoding="utf-8")
    host_config_path.write_text("agents: {}\n", encoding="utf-8")

    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={
            "MINDROOM_WORKER_BACKEND": "docker",
            "MINDROOM_DOCKER_WORKER_IMAGE": "ghcr.io/mindroom-ai/mindroom:latest",
            "MINDROOM_DOCKER_WORKER_HOST_CONFIG_PATH": "./nested/docker-host-config.yaml",
        },
    )

    assert DockerWorkerBackendConfig.from_runtime(runtime_paths).host_config_path == host_config_path.resolve()
    signature = configure_docker_worker_backend(
        runtime_paths,
        auth_token=_TEST_AUTH_TOKEN,
        storage_root=tmp_path / "storage",
    ).signature

    assert signature[0] == "docker"


def test_docker_backend_config_signature_keeps_primary_runtime_env_when_reanchoring_host_config(
    tmp_path: Path,
) -> None:
    """Host-config rebasing should not drop Docker backend env that only exists in the primary runtime .env."""
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    hostcfg_dir = tmp_path / "hostcfg"
    hostcfg_dir.mkdir()
    runtime_config_path = runtime_dir / "config.yaml"
    runtime_config_path.write_text("agents: {}\n", encoding="utf-8")
    (runtime_dir / ".env").write_text("MINDROOM_DOCKER_WORKER_IMAGE=image-from-runtime-env\n", encoding="utf-8")
    host_config_path = hostcfg_dir / "docker-host-config.yaml"
    host_config_path.write_text("agents: {}\n", encoding="utf-8")

    runtime_paths = resolve_runtime_paths(
        config_path=runtime_config_path,
        storage_path=tmp_path / "storage",
        process_env={
            "MINDROOM_WORKER_BACKEND": "docker",
            "MINDROOM_DOCKER_WORKER_HOST_CONFIG_PATH": str(host_config_path),
        },
    )

    signature = configure_docker_worker_backend(
        runtime_paths,
        auth_token=_TEST_AUTH_TOKEN,
        storage_root=tmp_path / "storage",
    ).signature

    assert signature[0] == "docker"


def test_docker_worker_config_rejects_wildcard_endpoint_host_from_publish_host(tmp_path: Path) -> None:
    """Wildcard bind hosts must not become the client-facing worker endpoint host."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\n", encoding="utf-8")

    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path,
        process_env={
            "MINDROOM_WORKER_BACKEND": "docker",
            "MINDROOM_DOCKER_WORKER_IMAGE": "ghcr.io/mindroom-ai/mindroom:latest",
            "MINDROOM_DOCKER_WORKER_PUBLISH_HOST": "0.0.0.0",  # noqa: S104 - wildcard bind is the scenario under test
        },
    )

    with pytest.raises(WorkerBackendError, match=r"endpoint_host cannot be 0\.0\.0\.0"):
        DockerWorkerBackendConfig.from_runtime(runtime_paths)


def test_docker_worker_config_allows_explicit_endpoint_host_with_wildcard_publish_host(tmp_path: Path) -> None:
    """Operators may bind broadly as long as the returned worker endpoint is explicit and reachable."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\n", encoding="utf-8")

    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path,
        process_env={
            "MINDROOM_WORKER_BACKEND": "docker",
            "MINDROOM_DOCKER_WORKER_IMAGE": "ghcr.io/mindroom-ai/mindroom:latest",
            "MINDROOM_DOCKER_WORKER_PUBLISH_HOST": "0.0.0.0",  # noqa: S104 - wildcard bind is the scenario under test
            "MINDROOM_DOCKER_WORKER_ENDPOINT_HOST": "127.0.0.1",
        },
    )

    config = DockerWorkerBackendConfig.from_runtime(runtime_paths)

    assert config.publish_host == "0.0.0.0"  # noqa: S104 - wildcard bind is the scenario under test
    assert config.endpoint_host == "127.0.0.1"


@pytest.mark.parametrize(
    ("label_name"),
    [
        "mindroom.ai/launch-config-hash",
        "mindroom.ai/worker-key",
        "mindroom.ai/component",
        # An operator override would make startup remove every container as one from an older release.
        "mindroom.ai/storage-layout",
    ],
)
def test_docker_worker_backend_rejects_reserved_extra_labels(
    tmp_path: Path,
    label_name: str,
) -> None:
    """Reserved Docker labels must fail fast instead of overriding backend-owned metadata."""
    with pytest.raises(WorkerBackendError, match="extra labels cannot override reserved labels"):
        DockerWorkerBackendConfig(
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
        DockerWorkerBackendConfig.from_runtime(runtime_paths)


def test_docker_worker_config_rejects_malformed_json_mapping_env(tmp_path: Path) -> None:
    """Malformed Docker JSON env values should fail closed instead of silently becoming empty mappings."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\n", encoding="utf-8")
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path,
        process_env={
            "MINDROOM_WORKER_BACKEND": "docker",
            "MINDROOM_DOCKER_WORKER_IMAGE": "ghcr.io/mindroom-ai/mindroom:latest",
            "MINDROOM_DOCKER_WORKER_ENV_JSON": "{not-json",
        },
    )

    with pytest.raises(WorkerBackendError, match="MINDROOM_DOCKER_WORKER_ENV_JSON must contain a JSON object"):
        DockerWorkerBackendConfig.from_runtime(runtime_paths)


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
        DockerWorkerBackendConfig(
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
        DockerWorkerBackendConfig.from_runtime(runtime_paths)


def _projection_signature_for_hash_seed(hash_seed: str, workspace_root: Path) -> dict[str, object]:
    repo_root = Path(__file__).resolve().parents[1]
    existing_pythonpath = os.environ.get("PYTHONPATH")
    pythonpath_entries = [str(repo_root / "src")]
    if existing_pythonpath:
        pythonpath_entries.append(existing_pythonpath)
    script = (
        textwrap.dedent(
            """
        import json
        from pathlib import Path

        import yaml

        from mindroom.config.main import load_config
        from mindroom.constants import resolve_runtime_paths
        from mindroom.workers.backends.docker_config import DockerWorkerBackendConfig
        from mindroom.workers.backends.docker_projection import DockerProjectionManager
        from mindroom.workers.backends.local import local_worker_state_paths_for_root
        from mindroom.workers.runtime import build_worker_launch_config

        tmp_path = Path(__WORKSPACE_ROOT__)
        runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
        config = DockerWorkerBackendConfig(
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
        launch = build_worker_launch_config(load_config(runtime_paths), tool_validation_snapshot={})
        projection = manager._projected_config(
            paths,
            launch=launch,
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
        cwd=repo_root,
        env={
            **os.environ,
            "PYTHONHASHSEED": hash_seed,
            "PYTHONPATH": os.pathsep.join(pythonpath_entries),
        },
        text=True,
    )
    return json.loads(completed.stdout.splitlines()[-1])


def test_docker_backend_ensures_worker_container_and_bind_mount(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Ensuring one worker should project only configured config assets into the container."""
    config_text, projected_paths = _projected_config_fixture(tmp_path)
    backend, fake_client, sync_calls = _backend(monkeypatch, tmp_path, config_text=config_text)

    handle = backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)

    assert handle.worker_key == _TEST_UNSCOPED_WORKER_KEY
    assert handle.backend_name == "docker"
    assert handle.status == "ready"
    assert handle.endpoint.endswith("/api/sandbox-runner/execute")
    assert handle.debug_metadata["state_root"] == str(worker_root_path(tmp_path, _TEST_UNSCOPED_WORKER_KEY))
    assert sync_calls == [(_TEST_UNSCOPED_WORKER_KEY, frozenset())]

    run_call = fake_client.containers.run_calls[0]
    env = run_call["environment"]
    assert isinstance(env, dict)
    assert env["MINDROOM_SANDBOX_RUNNER_MODE"] == "true"
    assert env["MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE"] == "forkserver"
    assert env["MINDROOM_SANDBOX_PROXY_TOKEN"] == handle.auth_token
    assert env["MINDROOM_SANDBOX_DEDICATED_WORKER_KEY"] == _TEST_UNSCOPED_WORKER_KEY
    assert env["MINDROOM_SANDBOX_DEDICATED_WORKER_ROOT"] == "/app/worker"
    assert env["MINDROOM_SHARED_CREDENTIALS_PATH"] == "/app/worker/.shared_credentials"
    assert env["MINDROOM_CONFIG_PATH"] == "/app/config-host/config.yaml"
    assert env["HOME"] == "/app/worker/agents/code/workspace"
    assert env["EXTRA_ENV"] == "present"

    volumes = _volumes_by_source(run_call["volumes"])
    projection_root = _assert_projected_worker_mounts(tmp_path, volumes, projected_paths)
    _assert_projected_config_snapshot(projection_root, tmp_path)
    assert run_call["user"] == "1000:1000"
    assert run_call["ports"] == {"8766/tcp": ("127.0.0.1", None)}
    assert "cap_drop" not in run_call
    assert "security_opt" not in run_call

    labels = run_call["labels"]
    assert isinstance(labels, dict)
    assert labels["mindroom.ai/component"] == "worker"
    assert "mindroom.ai/worker-key" not in labels
    assert labels["mindroom.ai/runtime-namespace"]
    assert labels["mindroom.ai/tenant"] == "test"

    metadata_path = _control_metadata_path(tmp_path, _TEST_UNSCOPED_WORKER_KEY)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["status"] == "ready"
    assert metadata["startup_count"] == 1


@pytest.mark.parametrize("computer_enabled", [False, True])
def test_docker_computer_worker_uses_argument_filtered_browser_seccomp_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    computer_enabled: bool,
) -> None:
    """Computer workers retain syscall filtering while allowing Chromium's namespace sandbox."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "state",
        process_env={WORKER_COMPUTER_ENABLED_ENV: str(computer_enabled).lower()},
    )
    backend, fake_client, _sync_calls = _backend(
        monkeypatch,
        tmp_path,
        runtime_paths=runtime_paths,
        security_policy="computer",
    )

    backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)

    assert fake_client.containers.run_calls[0]["cap_drop"] == ["ALL"]
    security_opt = fake_client.containers.run_calls[0]["security_opt"]
    assert isinstance(security_opt, list)
    assert security_opt[0] == "no-new-privileges:true"
    assert len(security_opt) == 2
    profile = json.loads(str(security_opt[1]).removeprefix("seccomp="))
    assert profile["defaultAction"] == "SCMP_ACT_ERRNO"
    assert set(profile) == {"defaultAction", "defaultErrnoRet", "syscalls"}
    assert "includes" not in json.dumps(profile)
    assert "excludes" not in json.dumps(profile)
    assert not any(
        rule["action"] == "SCMP_ACT_ALLOW"
        and not rule.get("args")
        and {"clone", "unshare", "setns"}.intersection(rule["names"])
        for rule in profile["syscalls"]
    )
    namespace_rules = [
        rule
        for rule in profile["syscalls"]
        if rule["action"] == "SCMP_ACT_ALLOW" and rule.get("args") and "clone" in rule["names"]
    ]
    assert {
        (rule["args"][0]["value"], rule["args"][0].get("valueTwo"))
        for rule in namespace_rules
        if rule["args"][0]["op"] == "SCMP_CMP_MASKED_EQ"
    }.issuperset(
        {
            (0x7E020000, 0x10000000),
            (0x7E020000, 0x20000000),
            (0x7E020000, 0x70000000),
        },
    )
    allowed_names = {
        name for rule in profile["syscalls"] if rule["action"] == "SCMP_ACT_ALLOW" for name in rule["names"]
    }
    assert {
        "bpf",
        "delete_module",
        "init_module",
        "mount",
        "perf_event_open",
        "reboot",
        "setns",
        "swapon",
        "swapoff",
        "syslog",
        "umount2",
    }.isdisjoint(allowed_names)
    assert "chroot" in allowed_names


def test_docker_backend_writes_authoritative_worker_validation_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Docker workers should inherit primary validation state instead of rebuilding it per call."""
    validation_snapshot = {"shell": {"name": "shell", "config_fields": []}}
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path)
    backend.config = replace(
        backend.config,
        extra_env={"MINDROOM_SANDBOX_STARTUP_MANIFEST_PATH": "/untrusted/manifest.json"},
    )

    backend.ensure_worker(
        _spec(backend, _TEST_UNSCOPED_WORKER_KEY, tool_validation_snapshot=validation_snapshot),
        now=10.0,
    )

    worker_root = worker_root_path(tmp_path, _TEST_UNSCOPED_WORKER_KEY)
    manifest = json.loads((worker_root / ".runtime" / "startup_manifest.json").read_text(encoding="utf-8"))
    assert manifest["tool_validation_snapshot"] == validation_snapshot
    assert manifest["runtime_paths"]["storage_root"] == "/app/worker"
    env = fake_client.containers.run_calls[0]["environment"]
    assert isinstance(env, dict)
    assert env["MINDROOM_SANDBOX_STARTUP_MANIFEST_PATH"] == "/app/worker/.runtime/startup_manifest.json"


def test_docker_workers_run_with_read_only_root_filesystem(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Tool code must not rewrite the image's /app tree, which the runner imports from and keeps across restarts."""
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path)

    backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)

    run_call = fake_client.containers.run_calls[0]
    assert run_call["read_only"] is True
    assert run_call["tmpfs"] == {"/tmp": "rw,nosuid,nodev,mode=1777,size=1g"}  # noqa: S108


def test_docker_workers_keep_the_image_init_entrypoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Only the image command is replaced, so the image's tini entrypoint stays PID 1 and reaps orphaned children."""
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path)

    backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)

    run_call = fake_client.containers.run_calls[0]
    assert run_call["command"] == ["/app/run-sandbox-runner.sh"]
    assert "entrypoint" not in run_call


def test_docker_workers_run_under_the_configured_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A sandboxed runtime such as gVisor or Kata keeps worker code off the host kernel."""
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, runtime="runsc")

    backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)

    assert fake_client.containers.run_calls[0]["runtime"] == "runsc"


@pytest.mark.parametrize(("raw_runtime", "expected_runtime"), [(" runsc ", "runsc"), ("", None)])
def test_docker_worker_runtime_reads_from_env(
    tmp_path: Path,
    raw_runtime: str,
    expected_runtime: str | None,
) -> None:
    """A blank runtime keeps the Docker daemon's default runtime."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={
            "MINDROOM_WORKER_BACKEND": "docker",
            "MINDROOM_DOCKER_WORKER_IMAGE": "ghcr.io/mindroom-ai/mindroom:latest",
            "MINDROOM_DOCKER_WORKER_RUNTIME": raw_runtime,
        },
    )

    assert DockerWorkerBackendConfig.from_runtime(runtime_paths).runtime == expected_runtime


def test_docker_workers_reach_the_docker_host_by_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Minimal-mode shells call the MindRoom API back through the Docker host by default."""
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path)

    backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)

    assert fake_client.containers.run_calls[0]["extra_hosts"] == {"host.docker.internal": "host-gateway"}


def test_docker_workers_mount_the_startup_manifest_directory_read_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Tool code must not rewrite the manifest the runner boots from, so `.runtime` is a read-only bind."""
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path)

    handle = backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)

    worker_root = handle.debug_metadata["state_root"]
    run_call = fake_client.containers.run_calls[0]
    assert f"{worker_root}/.runtime:/app/worker/.runtime:ro" in run_call["volumes"]
    assert run_call["environment"]["MINDROOM_SANDBOX_STARTUP_MANIFEST_PATH"] == (
        "/app/worker/.runtime/startup_manifest.json"
    )


def test_docker_worker_manifest_holds_no_docker_backend_settings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Primary-side Docker backend settings never reach the startup manifest that worker code can read."""
    backend_env = {
        "MINDROOM_WORKER_BACKEND": "docker",
        "MINDROOM_DOCKER_WORKER_HOST_CONFIG_PATH": "/srv/primary/config.yaml",
        "MINDROOM_DOCKER_WORKER_ENV_JSON": json.dumps({"EXTRA_ENV": "present"}),
        "MINDROOM_DOCKER_WORKER_LABELS_JSON": json.dumps({"team": "ops"}),
        "MINDROOM_DOCKER_WORKER_PUBLISH_HOST": "127.0.0.1",
    }
    (tmp_path / ".env").write_text(
        "".join(f"{name}={value!r}\n" for name, value in backend_env.items()),
        encoding="utf-8",
    )
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={"MATRIX_HOMESERVER": "https://chat.example.test", **backend_env},
    )
    backend, _fake_client, _sync_calls = _backend(monkeypatch, tmp_path, runtime_paths=runtime_paths)

    backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)

    committed = _committed_startup_runtime(backend, _TEST_UNSCOPED_WORKER_KEY)
    assert committed.process_env["MATRIX_HOMESERVER"] == "https://chat.example.test"
    leaked = {*committed.process_env, *committed.env_file_values} & set(backend_env)
    assert leaked == set()


@pytest.mark.parametrize(
    ("constant", "changed"),
    [
        ("_WORKER_TMPFS", {"/tmp": "rw,nosuid,nodev,mode=1777,size=2g"}),  # noqa: S108
        ("_WORKER_EXTRA_HOSTS", {"host.docker.internal": "host-gateway", "api.internal": "host-gateway"}),
        ("_WORKER_COMMAND", ["/app/run-sandbox-runner.sh", "--verbose"]),
    ],
    ids=["tmpfs", "extra-hosts", "command"],
)
def test_docker_backend_recreates_a_container_whose_run_arguments_changed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    constant: str,
    changed: object,
) -> None:
    """Any change to the arguments a container is run with replaces it, even when the image is unchanged."""
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path)
    handle = backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)
    first_container = fake_client.containers.by_name[handle.worker_id]

    monkeypatch.setattr(docker_backend_module, constant, changed)
    backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=20.0)

    assert first_container.removed == 1
    assert len(fake_client.containers.run_calls) == 2


@pytest.mark.parametrize(
    ("changed_env", "recreated"),
    [({"MATRIX_HOMESERVER": "https://new.example.test"}, True), ({"OPENAI_API_KEY": "sk-rotated"}, False)],
    ids=["public-env-changed", "secret-changed"],
)
def test_docker_backend_recreates_container_when_its_startup_manifest_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    changed_env: dict[str, str],
    *,
    recreated: bool,
) -> None:
    """A running worker boots from its manifest once, so a change to what the manifest would hold replaces it."""
    base_env = {"MATRIX_HOMESERVER": "https://old.example.test", "OPENAI_API_KEY": "sk-original"}

    def runtime_paths(process_env: dict[str, str]) -> RuntimePaths:
        return resolve_primary_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path,
            process_env=process_env,
        )

    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, runtime_paths=runtime_paths(base_env))
    handle = backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)
    first_container = fake_client.containers.by_name[handle.worker_id]
    # A changed primary environment builds a new backend, as the worker manager keys backends on it.
    restarted = _docker_backend(
        config=backend.config,
        auth_token=_TEST_AUTH_TOKEN,
        storage_root=tmp_path,
        runtime_paths=runtime_paths({**base_env, **changed_env}),
    )
    monkeypatch.setattr(restarted, "_wait_for_ready", lambda _container, **_kwargs: None)

    restarted.ensure_worker(_spec(restarted, _TEST_UNSCOPED_WORKER_KEY), now=20.0)

    assert first_container.removed == int(recreated)
    assert len(fake_client.containers.run_calls) == 1 + int(recreated)
    manifest = json.loads(
        sandbox_startup_manifest_path(Path(str(handle.debug_metadata["state_root"]))).read_text(encoding="utf-8"),
    )
    expected_homeserver = changed_env.get("MATRIX_HOMESERVER", base_env["MATRIX_HOMESERVER"])
    assert manifest["runtime_paths"]["process_env"]["MATRIX_HOMESERVER"] == expected_homeserver


def test_docker_backend_mirrors_exactly_the_launch_grantable_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The applied config's grantable credentials are the only shared credentials a worker receives."""
    backend, _fake_client, sync_calls = _backend(
        monkeypatch,
        tmp_path,
        config_text="defaults:\n  worker_grantable_credentials: [openai, github_private]\nagents: {}\n",
    )
    assert _applied_launch(backend).grantable_credentials == frozenset({"openai", "github_private"})

    backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)

    assert sync_calls == [(_TEST_UNSCOPED_WORKER_KEY, frozenset({"openai", "github_private"}))]


def test_docker_backend_mirrors_credentials_the_primary_environment_declares(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A granted credential the primary's `.env` declares reaches the worker, wherever the worker's config file lives."""
    primary_dir = tmp_path / "primary"
    primary_dir.mkdir()
    (primary_dir / ".env").write_text("GITHUB_TOKEN=ghp-primary\n", encoding="utf-8")
    config_text = "defaults:\n  worker_grantable_credentials: [github_private]\nagents: {}\n"
    (primary_dir / "config.yaml").write_text(config_text, encoding="utf-8")
    runtime_paths = resolve_primary_runtime_paths(
        config_path=primary_dir / "config.yaml",
        storage_path=tmp_path,
        process_env={},
    )
    host_config_path = tmp_path / "host-config" / "config.yaml"
    backend, _fake_client, _sync_calls = _backend(
        monkeypatch,
        tmp_path,
        config_text=config_text,
        runtime_paths=runtime_paths,
        host_config_path=host_config_path,
    )
    monkeypatch.setattr(docker_backend_module, "sync_shared_credentials_to_worker", sync_shared_credentials_to_worker)

    backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)

    mirror = CredentialsManager(tmp_path / "credentials").for_worker(_TEST_UNSCOPED_WORKER_KEY).shared_manager()
    assert (mirror.load_credentials("github_private") or {}).get("token") == "ghp-primary"


def test_docker_script_worker_profile_mirrors_no_global_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A script-specific Docker worker denies automatic credential mirroring."""
    backend, _fake_client, sync_calls = _backend(
        monkeypatch,
        tmp_path,
        config_text="defaults:\n  worker_grantable_credentials: [openai, github_private]\nagents: {}\n",
    )

    backend.ensure_worker(
        _spec(backend, _TEST_UNSCOPED_WORKER_KEY, mirror_grantable_credentials=False),
        now=10.0,
    )

    assert sync_calls == [(_TEST_UNSCOPED_WORKER_KEY, frozenset())]


def test_docker_backend_refuses_to_launch_before_a_config_is_applied(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Without an applied config, a request fails before any container, credential, or state change."""
    backend, fake_client, sync_calls = _backend(monkeypatch, tmp_path)
    fake_client.images.gets.clear()
    _APPLIED_TOOL_SNAPSHOTS[backend] = None

    with pytest.raises(WorkerBackendError, match="applied config"):
        backend.ensure_worker(WorkerSpec(_TEST_UNSCOPED_WORKER_KEY), now=10.0)

    assert fake_client.containers.run_calls == []
    assert fake_client.images.gets == []
    assert sync_calls == []
    assert not _control_metadata_path(tmp_path, _TEST_UNSCOPED_WORKER_KEY).exists()
    assert not worker_root_path(tmp_path, _TEST_UNSCOPED_WORKER_KEY).exists()


def test_docker_backend_keeps_run_pinned_worker_roots_out_of_sibling_containers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Two script worker keys create distinct containers that mount only their own run roots."""
    config_text = """
agents:
  watcher:
    display_name: Watcher
    role: Watch values
    model: default
models:
  default:
    provider: openai
    id: test-model
""".lstrip()
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, config_text=config_text)
    base_key = "v1:default:user_agent:@alice:example.test:watcher"
    broad_key = script_worker_key_for_run(base_key, f"script-{'a' * 32}")
    narrow_key = script_worker_key_for_run(base_key, f"script-{'b' * 32}")

    broad = backend.ensure_worker(_spec(backend, broad_key, private_agent_names=frozenset()), now=10.0)
    narrow = backend.ensure_worker(_spec(backend, narrow_key, private_agent_names=frozenset()), now=10.0)

    assert broad.worker_id != narrow.worker_id
    broad_root = worker_root_path(tmp_path, broad_key)
    narrow_root = worker_root_path(tmp_path, narrow_key)
    assert broad.debug_metadata["state_root"] == str(broad_root)
    assert narrow.debug_metadata["state_root"] == str(narrow_root)
    broad_volumes = _volumes_by_source(fake_client.containers.run_calls[0]["volumes"])
    narrow_volumes = _volumes_by_source(fake_client.containers.run_calls[1]["volumes"])
    assert str(broad_root) in broad_volumes
    assert str(narrow_root) not in broad_volumes
    assert str(narrow_root) in narrow_volumes
    assert str(broad_root) not in narrow_volumes


def test_docker_backend_reports_a_slow_start_to_its_caller(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A start past the grace period reports ``cold_start``, then ``waiting`` at each interval, then ``ready``."""
    backend, _fake_client, _sync_calls = _backend(monkeypatch, tmp_path)
    clock = _control_progress_clock(monkeypatch)

    def _ready_after_six_seconds(_container: _FakeContainer, *, on_poll_tick: Callable[[], None] | None) -> None:
        assert on_poll_tick is not None
        for elapsed_seconds in range(1, 7):
            clock.now = float(elapsed_seconds)
            on_poll_tick()

    monkeypatch.setattr(backend, "_wait_for_ready", _ready_after_six_seconds)
    progress_events: list[WorkerReadyProgress] = []

    handle = backend.ensure_worker(
        _spec(backend, _TEST_UNSCOPED_WORKER_KEY),
        now=10.0,
        progress_sink=progress_events.append,
    )

    assert handle.worker_key == _TEST_UNSCOPED_WORKER_KEY
    assert handle.status == "ready"
    assert [event.phase for event in progress_events] == ["cold_start", "waiting", "ready"]
    cold_start, waiting, ready = progress_events
    assert cold_start.elapsed_seconds >= 1.5
    assert waiting.elapsed_seconds >= 5.0
    assert ready.elapsed_seconds == 6.0
    assert {event.worker_key for event in progress_events} == {_TEST_UNSCOPED_WORKER_KEY}
    assert {event.backend_name for event in progress_events} == {"docker"}


def test_docker_backend_reports_nothing_for_a_start_within_the_grace_period(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A worker that is ready before the grace period ends starts without a warmup notice."""
    backend, _fake_client, _sync_calls = _backend(monkeypatch, tmp_path)
    _control_progress_clock(monkeypatch)
    progress_events: list[WorkerReadyProgress] = []

    handle = backend.ensure_worker(
        _spec(backend, _TEST_UNSCOPED_WORKER_KEY),
        now=10.0,
        progress_sink=progress_events.append,
    )

    assert handle.status == "ready"
    assert handle.startup_count == 1
    assert progress_events == []


def test_docker_backend_reports_no_progress_when_a_running_worker_fails_its_health_check(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A worker that was already running never reported a start, so its failure reaches the caller only as an error."""
    backend, _fake_client, _sync_calls = _backend(monkeypatch, tmp_path)
    clock = _control_progress_clock(monkeypatch)
    backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)

    def _unhealthy(_container: _FakeContainer, *, on_poll_tick: Callable[[], None] | None) -> None:
        assert on_poll_tick is None
        clock.now = 2.0
        msg = "worker stopped answering"
        raise WorkerBackendError(msg)

    monkeypatch.setattr(backend, "_wait_for_ready", _unhealthy)
    progress_events: list[WorkerReadyProgress] = []

    with pytest.raises(WorkerBackendError, match="worker stopped answering"):
        backend.ensure_worker(
            _spec(backend, _TEST_UNSCOPED_WORKER_KEY),
            now=11.0,
            progress_sink=progress_events.append,
        )

    assert progress_events == []


def test_docker_backend_sends_a_concurrent_caller_the_progress_of_the_start_it_waits_for(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A caller that waits behind another's cold start gets its latest phase at once, then every later one."""
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path)
    clock = _control_progress_clock(monkeypatch)
    spec = _spec(backend, _TEST_UNSCOPED_WORKER_KEY)
    first_events: list[WorkerReadyProgress] = []
    second_events: list[WorkerReadyProgress] = []
    first_saw_cold_start = threading.Event()
    second_saw_cold_start = threading.Event()

    def first_sink(progress: WorkerReadyProgress) -> None:
        first_events.append(progress)
        if progress.phase == "cold_start":
            first_saw_cold_start.set()

    def second_sink(progress: WorkerReadyProgress) -> None:
        second_events.append(progress)
        if progress.phase == "cold_start":
            second_saw_cold_start.set()

    def _ready_once_second_caller_waits(
        _container: _FakeContainer,
        *,
        on_poll_tick: Callable[[], None] | None,
    ) -> None:
        if on_poll_tick is None:
            # The second caller finds the worker the first one started already running.
            return
        clock.now = 2.0
        on_poll_tick()
        assert second_saw_cold_start.wait(timeout=5.0)

    monkeypatch.setattr(backend, "_wait_for_ready", _ready_once_second_caller_waits)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(backend.ensure_worker, spec, now=10.0, progress_sink=first_sink)
        assert first_saw_cold_start.wait(timeout=5.0)
        second = executor.submit(backend.ensure_worker, spec, now=11.0, progress_sink=second_sink)
        first_handle = first.result(timeout=5.0)
        second_handle = second.result(timeout=5.0)

    assert [event.phase for event in first_events] == ["cold_start", "ready"]
    assert [event.phase for event in second_events] == ["cold_start", "ready"]
    assert second_handle.worker_id == first_handle.worker_id
    assert len(fake_client.containers.created_containers) == 1


def test_docker_worker_readiness_poll_wakes_the_progress_reporter_on_each_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Every unanswered health check wakes the reporter before the next poll."""
    backend, _fake_client, _sync_calls = _backend(monkeypatch, tmp_path)
    container = _FakeContainer(
        name="mindroom-worker-slow",
        image_identity="sha256:slow",
        host_port=44999,
        environment={},
        labels={},
    )
    answers = iter(
        [
            httpx.Response(503),
            httpx.Response(503),
            httpx.Response(200, json={"status": "ok", "worker_protocol": WORKER_PROTOCOL_VERSION}),
        ],
    )

    _install_health(monkeypatch, lambda _url: next(answers))
    monkeypatch.setattr(docker_backend_module, "_READY_POLL_INTERVAL_SECONDS", 0.0)
    ticks: list[None] = []

    DockerWorkerBackend._wait_for_ready(backend, container, on_poll_tick=lambda: ticks.append(None))

    assert len(ticks) == 2


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
    (runtime_storage / "agents" / "code" / "workspace").mkdir(parents=True)

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
models:
  default:
    provider: openai
    id: test-model
""".lstrip(),
        runtime_paths=runtime_paths,
        storage_root=runtime_storage,
    )

    backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)

    volumes = _volumes_by_source(fake_client.containers.run_calls[0]["volumes"])
    projection_root = _projection_root(volumes)
    projected_config = (projection_root / "config.yaml").read_text(encoding="utf-8")

    assert "plugins:\n- path: ./.mindroom-worker-assets/plugins/00-runtime-plugin" in projected_config
    assert "path: ./.mindroom-worker-assets/knowledge_bases/docs" in projected_config
    assert (projection_root / ".mindroom-worker-assets" / "plugins" / "00-runtime-plugin" / "plugin.py").read_text(
        encoding="utf-8",
    ) == "PLUGIN_VERSION = 'runtime'\n"
    assert (projection_root / ".mindroom-worker-assets" / "knowledge_bases" / "docs" / "guide.md").read_text(
        encoding="utf-8",
    ) == "# Runtime Guide\n"
    assert volumes[str(runtime_storage / "agents/code/workspace")]["bind"] == "/app/worker/agents/code/workspace"


_KNOWLEDGE_CONFIG = """
knowledge_bases:
  docs:
    path: ${{MINDROOM_STORAGE_PATH}}/{source}
agents:
  code:
    display_name: Code
    knowledge_bases: [docs]
  other:
    display_name: Other
models:
  default:
    provider: openai
    id: test-model
"""


@pytest.mark.parametrize(
    ("source", "planted"),
    [
        ("agents/other/workspace/docs", "link"),
        ("agents/other/workspace/docs", "directory"),
        ("private_instances/someone/docs", "directory"),
        ("workers/other/docs", "directory"),
        ("shared_directories/other/report/docs", "directory"),
        ("knowledge/docs", "link"),
    ],
)
def test_docker_projection_never_copies_knowledge_another_worker_could_redirect(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    source: str,
    planted: str,
) -> None:
    """Knowledge is projected from its configured path: never through a link, never from what other workers write."""
    storage = tmp_path / "storage"
    victim = storage / "private_instances" / "victim" / "data"
    victim.mkdir(parents=True)
    (victim / "secret.md").write_text("victim-only note\n", encoding="utf-8")
    docs = storage / source
    docs.parent.mkdir(parents=True, exist_ok=True)
    if planted == "link":
        docs.symlink_to(victim, target_is_directory=True)
    else:
        docs.mkdir()
        (docs / "secret.md").write_text("victim-only note\n", encoding="utf-8")
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=storage)
    backend, fake_client, _sync_calls = _backend(
        monkeypatch,
        tmp_path,
        config_text=_KNOWLEDGE_CONFIG.format(source=source),
        runtime_paths=runtime_paths,
        storage_root=storage,
    )

    with capture_logs() as logs:
        backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)

    projection_root = _projection_root(_volumes_by_source(fake_client.containers.run_calls[0]["volumes"]))
    assert "path: ./.mindroom-worker-assets/knowledge_bases/docs" in (projection_root / "config.yaml").read_text()
    assert not any("victim-only" in path.read_text() for path in projection_root.rglob("*.md"))
    assert any(entry["log_level"] in {"warning", "error"} and "knowledge" in entry["event"] for entry in logs)


def test_docker_projection_maps_workspace_paths_without_resolving_them(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Knowledge inside the mounted workspace maps to the mount, so a swap cannot become an asset."""
    storage = tmp_path / "storage"
    victim = storage / "private_instances" / "victim" / "code" / "code_data"
    victim.mkdir(parents=True)
    (victim / "secret.md").write_text("victim-only note\n", encoding="utf-8")
    workspace = storage / "agents" / "code" / "workspace"
    (workspace / "kb").mkdir(parents=True)
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=storage)
    backend, fake_client, _sync_calls = _backend(
        monkeypatch,
        tmp_path,
        config_text=(
            "knowledge_bases:\n  docs:\n    path: ${MINDROOM_STORAGE_PATH}/agents/code/workspace/kb\n"
            "agents:\n  code:\n    display_name: Code\n    knowledge_bases: [docs]\n"
            "models:\n  default:\n    provider: openai\n    id: test-model\n"
        ),
        runtime_paths=runtime_paths,
        storage_root=storage,
    )
    swaps = {workspace / "kb": victim}
    is_symlink = Path.is_symlink

    def swap_after_check(path: Path) -> bool:
        checked = is_symlink(path)
        if (target := swaps.pop(path, None)) is not None:
            # Worker code swaps the entry for a link right after the link check saw a plain entry.
            path.rename(path.with_name(f"{path.name}-moved"))
            path.symlink_to(target, target_is_directory=target.is_dir())
        return checked

    monkeypatch.setattr(Path, "is_symlink", swap_after_check)

    backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)

    projection_root = _projection_root(_volumes_by_source(fake_client.containers.run_calls[0]["volumes"]))
    projected_config = (projection_root / "config.yaml").read_text(encoding="utf-8")
    assert "path: /app/worker/agents/code/workspace/kb" in projected_config
    assert not any(
        "victim-only" in path.read_text(encoding="utf-8") for path in projection_root.rglob("*") if path.is_file()
    )


def test_docker_projection_copy_refuses_an_entry_swapped_after_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Projected assets are copied through no-follow descriptors, so a file swapped for a link is never copied."""
    storage = tmp_path / "storage"
    docs = storage / "knowledge" / "docs"
    docs.mkdir(parents=True)
    (docs / "guide.md").write_text("# Guide\n", encoding="utf-8")
    victim = tmp_path / "victim.md"
    victim.write_text("victim-only note\n", encoding="utf-8")
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=storage)
    backend, _fake_client, _sync_calls = _backend(
        monkeypatch,
        tmp_path,
        config_text=_KNOWLEDGE_CONFIG.format(source="knowledge/docs"),
        runtime_paths=runtime_paths,
        storage_root=storage,
    )
    validate = docker_projection_module.validate_local_copy_source_dir

    def validate_then_swap(source_dir: Path, **kwargs: object) -> Path:
        validated = validate(source_dir, **kwargs)
        if validated.name == "docs":
            (docs / "guide.md").unlink()
            (docs / "guide.md").symlink_to(victim)
        return validated

    monkeypatch.setattr(docker_projection_module, "validate_local_copy_source_dir", validate_then_swap)

    with pytest.raises((WorkerBackendError, OSError, ValueError)):
        backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)

    assert not any("victim-only" in path.read_text() for path in storage.rglob("guide.md") if not path.is_symlink())


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
        backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)


def test_docker_backend_rejects_symlinked_projected_file_assets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Projected file assets must reject symlink roots instead of copying the link target."""
    secret_path = tmp_path / "secret.md"
    secret_path.write_text("secret\n", encoding="utf-8")
    (tmp_path / "guide.md").symlink_to(secret_path)
    backend, _fake_client, _sync_calls = _backend(
        monkeypatch,
        tmp_path,
        config_text="""
knowledge_bases:
  docs:
    path: ./guide.md
agents:
  code:
    display_name: Code
    knowledge_bases: [docs]
models:
  default:
    provider: openai
    id: test-model
""".lstrip(),
        storage_root=tmp_path / "storage",
    )

    with pytest.raises(WorkerBackendError, match="Docker worker asset must not contain symlinks"):
        backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)


def test_docker_backend_mounts_shared_credential_mirror_read_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Worker code must not be able to delete or relink the primary's credential mirror."""
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path)

    backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)

    volumes = _volumes_by_source(fake_client.containers.run_calls[0]["volumes"])
    worker_root = worker_root_path(tmp_path, _TEST_UNSCOPED_WORKER_KEY)
    assert volumes[str(worker_root)] == {"bind": "/app/worker", "mode": "rw"}
    assert volumes[str(worker_root / ".shared_credentials")] == {
        "bind": "/app/worker/.shared_credentials",
        "mode": "ro",
    }


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

    backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)

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

    backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)

    assert synced_shared_paths == [shared_credentials_path]


def test_docker_backend_commits_parent_runtime_env_into_worker_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Dedicated Docker workers should receive the committed public startup runtime only."""
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.yaml"
    config_text = (
        "models:\n  default:\n    provider: openai\n    id: gpt-6-astra\nagents: {}\nrouter:\n  model: default\n"
    )
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
        storage_root=runtime_storage,
        host_config_path=config_path,
    )

    backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)

    run_call = fake_client.containers.run_calls[0]
    env = run_call["environment"]
    assert isinstance(env, dict)
    committed_runtime = _committed_startup_runtime(backend, _TEST_UNSCOPED_WORKER_KEY)
    local_credentials_path = (
        worker_root_path(runtime_storage, _TEST_UNSCOPED_WORKER_KEY) / ".runtime" / credentials_path.name
    )

    assert env["MINDROOM_CONFIG_PATH"] == "/app/config-host/config.yaml"
    assert committed_runtime.config_path == Path("/app/config-host/config.yaml")
    assert committed_runtime.env_value("MINDROOM_NAMESPACE") == "alpha1234"
    assert committed_runtime.env_value("MATRIX_HOMESERVER") == "http://dotenv-hs"
    assert committed_runtime.env_value("MATRIX_SERVER_NAME") == "alpha.example"
    assert committed_runtime.env_value("BROWSER_EXECUTABLE_PATH") == "/usr/bin/chromium"
    assert committed_runtime.env_value("GOOGLE_APPLICATION_CREDENTIALS") is None
    assert committed_runtime.env_value("GOOGLE_CLOUD_PROJECT") == "demo-project"
    assert committed_runtime.env_value("GOOGLE_CLOUD_LOCATION") == "us-central1"
    assert committed_runtime.env_value("ANTHROPIC_API_KEY") is None
    assert committed_runtime.env_value("MINDROOM_SANDBOX_PROXY_TOKEN") is None
    assert committed_runtime.env_value("MINDROOM_LOCAL_CLIENT_SECRET") is None
    assert not local_credentials_path.exists()


def test_docker_backend_excludes_internal_file_secrets_from_worker_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Dedicated Docker workers must not copy control-plane or GitHub file secrets into worker startup env."""
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.yaml"
    config_text = (
        "models:\n  default:\n    provider: openai\n    id: gpt-6-astra\nagents: {}\nrouter:\n  model: default\n"
    )
    config_path.write_text(config_text, encoding="utf-8")
    secret_path = tmp_path / "control-secret.txt"
    secret_path.write_text("supersecret\n", encoding="utf-8")
    runtime_storage = (tmp_path / "runtime-storage").resolve()
    runtime_paths = resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=runtime_storage,
        process_env={
            "GITHUB_TOKEN_FILE": str(secret_path),
            "MINDROOM_API_KEY_FILE": str(secret_path),
            "MINDROOM_LOCAL_CLIENT_SECRET_FILE": str(secret_path),
        },
    )
    backend, fake_client, _sync_calls = _backend(
        monkeypatch,
        tmp_path,
        config_text=config_text,
        runtime_paths=runtime_paths,
        storage_root=runtime_storage,
        host_config_path=config_path,
    )

    backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)

    run_call = fake_client.containers.run_calls[0]
    env = run_call["environment"]
    assert isinstance(env, dict)
    committed_runtime = _committed_startup_runtime(backend, _TEST_UNSCOPED_WORKER_KEY)
    worker_runtime_root = worker_root_path(runtime_storage, _TEST_UNSCOPED_WORKER_KEY)

    assert committed_runtime.env_value("GITHUB_TOKEN_FILE") is None
    assert committed_runtime.env_value("MINDROOM_API_KEY_FILE") is None
    assert committed_runtime.env_value("MINDROOM_LOCAL_CLIENT_SECRET_FILE") is None
    assert not (worker_runtime_root / ".runtime" / "file-secrets" / "GITHUB_TOKEN_FILE").exists()
    assert not (worker_runtime_root / ".runtime" / "file-secrets" / "MINDROOM_API_KEY_FILE").exists()
    assert not (worker_runtime_root / ".runtime" / "file-secrets" / "MINDROOM_LOCAL_CLIENT_SECRET_FILE").exists()


@pytest.mark.parametrize("source", ["env-file", "process-env"])
def test_docker_backend_excludes_relative_file_backed_secrets_from_worker_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    source: str,
) -> None:
    """Dedicated Docker workers must not ambient-copy *_FILE secrets from config-adjacent .env or process env."""
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.yaml"
    config_text = (
        "models:\n  default:\n    provider: openai\n    id: gpt-6-astra\nagents: {}\nrouter:\n  model: default\n"
    )
    config_path.write_text(config_text, encoding="utf-8")
    secret_path = config_dir / "secrets" / "openai.key"
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    secret_path.write_text("sk-relative\n", encoding="utf-8")
    runtime_storage = (tmp_path / "runtime-storage").resolve()
    process_env: dict[str, str] = {}
    if source == "env-file":
        (config_dir / ".env").write_text("OPENAI_API_KEY_FILE=secrets/openai.key\n", encoding="utf-8")
    else:
        process_env["OPENAI_API_KEY_FILE"] = "secrets/openai.key"
    runtime_paths = resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=runtime_storage,
        process_env=process_env,
    )
    backend, _fake_client, _sync_calls = _backend(
        monkeypatch,
        tmp_path,
        config_text=config_text,
        runtime_paths=runtime_paths,
        storage_root=runtime_storage,
        host_config_path=config_path,
    )

    backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)

    committed_runtime = _committed_startup_runtime(backend, _TEST_UNSCOPED_WORKER_KEY)
    local_secret_copy = (
        worker_root_path(runtime_storage, _TEST_UNSCOPED_WORKER_KEY)
        / ".runtime"
        / "file-secrets"
        / "OPENAI_API_KEY_FILE"
        / "openai.key"
    )

    assert committed_runtime.env_value("OPENAI_API_KEY_FILE") is None
    assert not local_secret_copy.exists()


def test_docker_backend_preserves_container_config_path_without_host_projection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Dedicated Docker workers without a host config mount must keep the in-container config path."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-6-astra\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_primary_runtime_paths(config_path=config_path, storage_path=tmp_path / "storage")
    config = DockerWorkerBackendConfig(
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
    fake_client.images.by_name[config.image] = _FakeImage("sha256:image-v1")

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
    backend = _docker_backend(
        config=config,
        auth_token=_TEST_AUTH_TOKEN,
        storage_root=tmp_path / "storage",
        runtime_paths=runtime_paths,
    )
    monkeypatch.setattr(backend, "_wait_for_ready", lambda _container, **_kwargs: None)

    backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)

    run_call = fake_client.containers.run_calls[0]
    env = run_call["environment"]
    assert isinstance(env, dict)
    assert "MINDROOM_CONFIG_PATH" not in env
    committed_runtime = _committed_startup_runtime(backend, _TEST_UNSCOPED_WORKER_KEY)
    assert committed_runtime.config_path == Path("/app/config-host/config.yaml")


def test_docker_backend_ignores_symlinked_google_application_credentials_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Ambient ADC paths are not shared with Docker workers, so no host path copy is attempted."""
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.yaml"
    config_text = (
        "models:\n  default:\n    provider: openai\n    id: gpt-6-astra\nagents: {}\nrouter:\n  model: default\n"
    )
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
    backend, fake_client, _sync_calls = _backend(
        monkeypatch,
        tmp_path,
        config_text=config_text,
        runtime_paths=runtime_paths,
        storage_root=runtime_storage,
        host_config_path=config_path,
    )

    backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)

    run_call = fake_client.containers.run_calls[0]
    env = run_call["environment"]
    assert isinstance(env, dict)
    committed_runtime = _committed_startup_runtime(backend, _TEST_UNSCOPED_WORKER_KEY)
    assert committed_runtime.env_value("GOOGLE_APPLICATION_CREDENTIALS") is None


def test_docker_backend_does_not_overwrite_google_application_credentials_destination(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Ambient ADC paths must not create or overwrite worker-owned secret files."""
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.yaml"
    config_text = (
        "models:\n  default:\n    provider: openai\n    id: gpt-6-astra\nagents: {}\nrouter:\n  model: default\n"
    )
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
    backend, fake_client, _sync_calls = _backend(
        monkeypatch,
        tmp_path,
        config_text=config_text,
        runtime_paths=runtime_paths,
        storage_root=runtime_storage,
        host_config_path=config_path,
    )

    backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)

    run_call = fake_client.containers.run_calls[0]
    env = run_call["environment"]
    assert isinstance(env, dict)
    committed_runtime = _committed_startup_runtime(backend, _TEST_UNSCOPED_WORKER_KEY)
    assert committed_runtime.env_value("GOOGLE_APPLICATION_CREDENTIALS") is None
    assert destination_path.is_symlink()
    assert victim_path.read_text(encoding="utf-8") == "victim\n"


def test_docker_backend_projects_only_the_config_fields_workers_resolve(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The worker-readable config keeps only runner fields, so secrets in any other field never reach it."""
    (tmp_path / "plugins" / "my-plugin").mkdir(parents=True)
    (tmp_path / "knowledge_docs").mkdir()
    config_text = """
plugins:
  - path: ./plugins/my-plugin
    settings:
      key: plugin-setting-secret
knowledge_bases:
  docs:
    path: ./knowledge_docs
    git:
      repo_url: https://oauth2:git-url-secret@git.example.org/docs.git
memory:
  backend: file
  embedder:
    provider: openai
    config:
      api_key: embedder-secret
mcp_servers:
  files:
    transport: stdio
    command: npx
    args: [--api-key, mcp-arg-secret]
    env:
      AWS_SECRET_ACCESS_KEY: mcp-env-secret
  remote:
    transport: streamable-http
    url: https://user:mcp-url-secret@mcp.example.org/mcp
    headers:
      X-Upstream-Key: mcp-header-secret
event_journal:
  backend: postgres
  database_url: postgresql://mindroom:journal-dsn-secret@db/x
defaults:
  file_access: unrestricted
  tools:
    - custom_api:
        base_url: https://user:default-tool-override-secret@api.example.org
agents:
  code:
    display_name: Code
    role: Test
    model: default
    instructions: [agent-instructions-secret]
    knowledge_bases: [docs]
    tools:
      - shell
      - custom_api:
          base_url: https://user:tool-override-secret@api.example.org
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
administrators:
  - "@owner-secret:example.org"
""".lstrip()
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, config_text=config_text)

    backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)

    volumes = _volumes_by_source(fake_client.containers.run_calls[0]["volumes"])
    projected_text = (_projection_root(volumes) / "config.yaml").read_text(encoding="utf-8")
    assert "secret" not in projected_text
    assert yaml.safe_load(projected_text) == {
        "defaults": {"file_access": "unrestricted", "tools": ["custom_api"]},
        "agents": {
            "code": {"display_name": "Code", "knowledge_bases": ["docs"], "tools": ["shell", "custom_api"]},
        },
        "plugins": [{"path": "./.mindroom-worker-assets/plugins/00-my-plugin"}],
        "memory": {"backend": "file"},
        "knowledge_bases": {"docs": {"path": "./.mindroom-worker-assets/knowledge_bases/docs"}},
    }


def test_worker_config_data_error_for_a_malformed_tool_entry_omits_its_values() -> None:
    """A tool entry the worker projection cannot read fails without repeating the credential written into it."""
    with pytest.raises(ValueError, match="Tool entries must be") as exc_info:
        worker_config_data({"agents": {"code": {"tools": [{"openbb": {}, "extra": "inline-secret-value"}]}}})

    assert "inline-secret-value" not in str(exc_info.value)


def test_load_docker_client_auto_installs_optional_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Docker backend startup should ensure the optional Docker SDK before importing it."""
    captured: dict[str, object] = {}
    fake_client = object()
    fake_errors = SimpleNamespace(DockerException=_FakeDockerError, NotFound=_FakeNotFoundError)
    runtime_paths = _docker_preflight_runtime_paths(tmp_path)

    def _ensure(provided_runtime_paths: RuntimePaths) -> None:
        captured["installed"] = True
        captured["runtime_paths"] = provided_runtime_paths

    def _import_module(name: str) -> object:
        if name == "docker":
            return SimpleNamespace(from_env=lambda **_kwargs: fake_client)
        if name == "docker.errors":
            return fake_errors
        msg = f"Unexpected import: {name}"
        raise AssertionError(msg)

    monkeypatch.setattr("mindroom.workers.backends.docker.ensure_docker_dependencies", _ensure)
    monkeypatch.setattr("mindroom.workers.backends.docker.importlib.import_module", _import_module)

    client, errors = _load_docker_client_and_errors(runtime_paths=runtime_paths)

    assert captured["installed"] is True
    assert captured["runtime_paths"] is runtime_paths
    assert client is fake_client
    assert errors is fake_errors


def test_load_docker_client_applies_migration_request_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Migration client construction carries the remaining overall deadline."""
    captured: dict[str, object] = {}
    fake_client = object()
    fake_errors = SimpleNamespace(DockerException=_FakeDockerError, NotFound=_FakeNotFoundError)
    runtime_paths = _docker_preflight_runtime_paths(tmp_path)

    def from_env(**kwargs: object) -> object:
        captured.update(kwargs)
        return fake_client

    def import_module(name: str) -> object:
        if name == "docker":
            return SimpleNamespace(from_env=from_env)
        if name == "docker.errors":
            return fake_errors
        msg = f"Unexpected import: {name}"
        raise AssertionError(msg)

    monkeypatch.setattr(
        "mindroom.workers.backends.docker.ensure_docker_dependencies",
        lambda _paths: pytest.fail("migration client must not install dependencies"),
    )
    monkeypatch.setattr("mindroom.workers.backends.docker.importlib.import_module", import_module)

    client, _errors = _load_docker_client_and_errors(
        runtime_paths=runtime_paths,
        timeout_seconds=4.5,
        ensure_dependencies=False,
    )

    assert client is fake_client
    assert captured == {"environment": runtime_paths.process_env, "timeout": 4.5}


def test_ensure_docker_dependencies_uses_explicit_runtime_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Docker dependency bootstrap should honor the active runtime's config-adjacent .env."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-6-astra\nagents: {}\nrouter:\n  model: default\n",
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
    monkeypatch.setattr("mindroom.workers.backends.docker_config.os.getuid", lambda: 501)
    monkeypatch.setattr("mindroom.workers.backends.docker_config.os.getgid", lambda: 20)

    assert _default_docker_user_for_os("posix") == "501:20"


def test_read_docker_user_defaults_to_image_user_on_windows() -> None:
    """Windows should leave the container user unset and use the image default."""
    assert _default_docker_user_for_os("nt") is None


def test_read_docker_user_env_override_wins() -> None:
    """Explicit Docker worker user config should override platform defaults."""
    assert _read_docker_user({"MINDROOM_DOCKER_WORKER_USER": "2001:3001"}) == "2001:3001"


def test_docker_backend_cleanup_stops_idle_workers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Idle cleanup should stop running containers while retaining worker state."""
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, idle_timeout_seconds=60.0)

    backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)

    metadata_path = _control_metadata_path(tmp_path, _TEST_UNSCOPED_WORKER_KEY)
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


def test_docker_backend_retires_only_one_exact_run_worker_idempotently(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Docker retirement removes one run container and root without touching its ordinary worker."""
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path)
    base_key = "v1:t:user_agent:alice:watcher"
    run_key = script_worker_key_for_run(base_key, f"script-{'c' * 32}")
    ordinary = backend.ensure_worker(_spec(backend, base_key, private_agent_names=frozenset()), now=1.0)
    retired = backend.ensure_worker(_spec(backend, run_key, private_agent_names=frozenset()), now=1.0)
    retired_root = worker_root_path(tmp_path, run_key)
    retired_projection_root = backend._projection_manager._worker_projected_configs_root(
        local_worker_state_paths_for_root(retired_root),
    )
    (retired_root / "workspace").mkdir(parents=True, exist_ok=True)
    (retired_root / "workspace" / "note.txt").write_text("retire me", encoding="utf-8")

    backend.retire_worker(run_key)
    backend.retire_worker(run_key)

    assert fake_client.containers.by_name[retired.worker_id].removed == 1
    assert fake_client.containers.by_name[ordinary.worker_id].removed == 0
    assert retired_root.exists() is False
    assert retired_projection_root.exists() is False
    assert worker_root_path(tmp_path, base_key).is_dir()
    assert [handle.worker_key for handle in backend.list_workers()] == [base_key]


def test_docker_backend_refuses_retirement_when_live_container_key_mismatches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A stale container name cannot authorize deletion when its live worker identity differs."""
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path)
    base_key = "v1:t:user_agent:alice:watcher"
    run_key = script_worker_key_for_run(base_key, f"script-{'f' * 32}")
    handle = backend.ensure_worker(_spec(backend, run_key, private_agent_names=frozenset()), now=1.0)
    container = fake_client.containers.by_name[handle.worker_id]
    raw_env = container.attrs["Config"]["Env"]
    worker_key_env = SANDBOX_RUNTIME_ENV_BY_KEY["dedicated_worker_key"]
    container.attrs["Config"]["Env"] = [
        f"{worker_key_env}={base_key}" if entry.startswith(f"{worker_key_env}=") else entry for entry in raw_env
    ]

    with pytest.raises(WorkerBackendError, match="does not match retirement key"):
        backend.retire_worker(run_key)

    assert container.removed == 0
    assert worker_root_path(tmp_path, run_key).is_dir()


def _foreign_container(fake_client: _FakeDockerClient, name: str) -> _FakeContainer:
    """Register one unrelated container (a homeserver, a database) on the same Docker daemon."""
    container = _FakeContainer(
        name=name,
        image_identity="sha256:foreign",
        host_port=8008,
        environment={},
        labels={},
    )
    fake_client.containers.by_name[name] = container
    return container


def _write_mounted_worker_metadata(storage_root: Path, worker_key: str, overrides: dict[str, object]) -> None:
    """Write metadata inside one worker's bind-mounted root, as its tool code could."""
    control_metadata = json.loads(_control_metadata_path(storage_root, worker_key).read_text(encoding="utf-8"))
    mounted_metadata_path = worker_root_path(storage_root, worker_key) / "metadata" / "worker.json"
    mounted_metadata_path.parent.mkdir(parents=True, exist_ok=True)
    mounted_metadata_path.write_text(json.dumps(control_metadata | overrides), encoding="utf-8")


def test_docker_worker_cannot_retarget_the_backend_from_its_mounted_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Metadata written inside a worker's own mount never decides which container the primary touches."""
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, idle_timeout_seconds=60.0)
    victim_key = "v1:default:unscoped:victim"
    attacker = backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)
    victim = backend.ensure_worker(_spec(backend, victim_key), now=10.0)
    victim_container = fake_client.containers.by_name[victim.worker_id]
    foreign_container = _foreign_container(fake_client, "synapse")

    attacker_state_root = worker_root_path(tmp_path, _TEST_UNSCOPED_WORKER_KEY)
    control_metadata_path = _control_metadata_path(tmp_path, _TEST_UNSCOPED_WORKER_KEY)
    assert control_metadata_path.is_file()
    assert not control_metadata_path.is_relative_to(attacker_state_root)
    assert list(attacker_state_root.rglob("worker.json")) == []

    _write_mounted_worker_metadata(
        tmp_path,
        _TEST_UNSCOPED_WORKER_KEY,
        {"container_name": "synapse", "worker_id": "synapse", "worker_key": victim_key},
    )

    reused = backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=20.0)
    backend.cleanup_idle_workers(now=10_000.0)

    assert reused.worker_id == attacker.worker_id
    assert victim_container.removed == 0
    assert fake_client.containers.by_name[attacker.worker_id].removed == 0
    assert sorted(handle.worker_key for handle in backend.list_workers()) == [
        _TEST_UNSCOPED_WORKER_KEY,
        victim_key,
    ]

    backend.shutdown()

    assert foreign_container.removed == 0
    assert foreign_container.stopped == 0


def test_docker_backend_leaves_foreign_containers_named_by_control_records_untouched(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A recorded container name only ever reaps this backend's own container for that worker key."""
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, idle_timeout_seconds=60.0)
    backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)
    foreign_container = _foreign_container(fake_client, "synapse")
    metadata_path = _control_metadata_path(tmp_path, _TEST_UNSCOPED_WORKER_KEY)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["container_name"] = "synapse"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=20.0)
    backend.cleanup_idle_workers(now=10_000.0)

    assert foreign_container.removed == 0
    assert foreign_container.stopped == 0


def test_docker_backend_refuses_foreign_container_holding_the_derived_worker_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An unowned container answering to the derived name fails the request instead of being destroyed."""
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path)
    squatter = _foreign_container(fake_client, backend._container_name_for_worker(_TEST_UNSCOPED_WORKER_KEY))

    with pytest.raises(WorkerBackendError, match="not owned by this MindRoom worker runtime"):
        backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)

    assert squatter.removed == 0
    assert squatter.stopped == 0
    assert fake_client.containers.run_calls == []


def test_docker_backend_refuses_symlinked_startup_manifest_directory_in_worker_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A directory symlink planted in a worker's own mount cannot redirect the primary's manifest write."""
    snapshot: dict[str, dict[str, object]] = {"shell": {"available": True}}
    backend, _fake_client, _sync_calls = _backend(monkeypatch, tmp_path)
    victim_key = "v1:default:unscoped:victim"
    backend.ensure_worker(_spec(backend, victim_key, tool_validation_snapshot=snapshot), now=10.0)
    victim_control_metadata = _control_metadata_path(tmp_path, victim_key)
    victim_metadata_before = victim_control_metadata.read_bytes()

    runtime_dir = worker_root_path(tmp_path, _TEST_UNSCOPED_WORKER_KEY) / ".runtime"
    runtime_dir.parent.mkdir(parents=True, exist_ok=True)
    runtime_dir.symlink_to(victim_control_metadata.parent, target_is_directory=True)

    with pytest.raises(WorkerBackendError, match="real directory"):
        backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY, tool_validation_snapshot=snapshot), now=20.0)

    assert victim_control_metadata.read_bytes() == victim_metadata_before
    assert not (victim_control_metadata.parent / "startup_manifest.json").exists()


def test_docker_backend_replaces_symlinked_startup_manifest_atomically(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A file symlink planted as the manifest is replaced by a complete manifest instead of being followed."""
    snapshot: dict[str, dict[str, object]] = {"shell": {"available": True}}
    backend, _fake_client, _sync_calls = _backend(monkeypatch, tmp_path)
    victim_key = "v1:default:unscoped:victim"
    backend.ensure_worker(_spec(backend, victim_key, tool_validation_snapshot=snapshot), now=10.0)
    victim_control_metadata = _control_metadata_path(tmp_path, victim_key)
    victim_metadata_before = victim_control_metadata.read_bytes()

    manifest_path = sandbox_startup_manifest_path(worker_root_path(tmp_path, _TEST_UNSCOPED_WORKER_KEY))
    manifest_path.parent.mkdir(parents=True)
    manifest_path.symlink_to(victim_control_metadata)

    backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY, tool_validation_snapshot=snapshot), now=20.0)

    assert victim_control_metadata.read_bytes() == victim_metadata_before
    assert not manifest_path.is_symlink()
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["tool_validation_snapshot"] == {
        "shell": {"available": True},
    }
    assert [entry.name for entry in manifest_path.parent.iterdir()] == [manifest_path.name]


def test_docker_backend_retires_worker_despite_tampered_mounted_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Retirement identity comes from control state, so a worker cannot pin its own state tree."""
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path)
    run_key = script_worker_key_for_run("v1:t:user_agent:alice:watcher", f"script-{'d' * 32}")
    handle = backend.ensure_worker(_spec(backend, run_key, private_agent_names=frozenset()), now=1.0)
    _write_mounted_worker_metadata(tmp_path, run_key, {"worker_key": "v1:t:user_agent:mallory:watcher"})

    backend.retire_worker(run_key)

    assert fake_client.containers.by_name[handle.worker_id].removed == 1
    assert worker_root_path(tmp_path, run_key).exists() is False
    assert _control_metadata_path(tmp_path, run_key).exists() is False


def _restart_backend(backend: DockerWorkerBackend, storage_root: Path) -> DockerWorkerBackend:
    """Construct a new primary over the same storage root and fake Docker daemon."""
    return _docker_backend(config=backend.config, auth_token=_TEST_AUTH_TOKEN, storage_root=storage_root)


def _move_control_record_into_mounted_root(storage_root: Path, worker_key: str, overrides: dict[str, object]) -> None:
    """Rewrite one worker into the pre-control-directory layout its last release left on disk."""
    _write_mounted_worker_metadata(storage_root, worker_key, overrides)
    control_metadata_path = _control_metadata_path(storage_root, worker_key)
    control_metadata_path.unlink()
    control_metadata_path.parent.rmdir()
    control_metadata_path.parent.parent.rmdir()


def test_docker_backend_adopts_legacy_mounted_worker_records(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Workers recorded inside their mount before the upgrade stay managed, trusting only their digest-bound key."""
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, idle_timeout_seconds=60.0)
    handle = backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)
    legacy_container = fake_client.containers.by_name[handle.worker_id]
    foreign_container = _foreign_container(fake_client, "synapse")
    _move_control_record_into_mounted_root(
        tmp_path,
        _TEST_UNSCOPED_WORKER_KEY,
        {"container_name": "synapse", "worker_id": "synapse", "status": "failed", "last_used_at": 1e12},
    )
    assert backend.list_workers() == []

    upgraded = _restart_backend(backend, tmp_path)

    adopted = json.loads(_control_metadata_path(tmp_path, _TEST_UNSCOPED_WORKER_KEY).read_text(encoding="utf-8"))
    assert adopted["worker_key"] == _TEST_UNSCOPED_WORKER_KEY
    assert adopted["container_name"] == handle.worker_id
    assert adopted["status"] == "idle"
    assert adopted["last_used_at"] < 1e12
    assert [listed.worker_id for listed in upgraded.list_workers()] == [handle.worker_id]

    cleaned = upgraded.cleanup_idle_workers(now=adopted["last_used_at"] + 120.0)

    assert [worker.worker_key for worker in cleaned] == [_TEST_UNSCOPED_WORKER_KEY]
    assert legacy_container.stopped == 1
    assert foreign_container.stopped == 0

    upgraded.retire_worker(_TEST_UNSCOPED_WORKER_KEY)

    assert legacy_container.removed == 1
    assert foreign_container.removed == 0
    assert worker_root_path(tmp_path, _TEST_UNSCOPED_WORKER_KEY).exists() is False
    assert _control_metadata_path(tmp_path, _TEST_UNSCOPED_WORKER_KEY).exists() is False


def test_docker_backend_drops_retired_keys_from_old_control_records(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A control record from the last release keeps its worker and history, and its old container is replaced once."""
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path)
    handle = backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)
    container = fake_client.containers.by_name[handle.worker_id]
    record_path = _control_metadata_path(tmp_path, _TEST_UNSCOPED_WORKER_KEY)
    current_record = json.loads(record_path.read_text(encoding="utf-8")) | {"startup_count": 3}
    old_hash = "0" * 64
    old_record = current_record | {
        "endpoint": f"http://127.0.0.1:{backend._container_host_port(container)}/api/sandbox-runner/execute",
        "backend_name": "docker",
        "host_port": backend._container_host_port(container),
        "container_id": container.id,
        "image": backend.config.image,
        "publish_host": "127.0.0.1",
        "worker_port": 8766,
        "launch_config_hash": old_hash,
        "startup_manifest_hash": "1" * 64,
    }
    record_path.write_text(json.dumps(old_record), encoding="utf-8")
    # The container that release started carries its launch hash in the older form.
    container.attrs["Config"]["Labels"]["mindroom.ai/launch-config-hash"] = old_hash
    unreadable_path = _control_metadata_path(tmp_path, "v1:default:unscoped:broken")
    unreadable_path.parent.mkdir(parents=True)
    unreadable_path.write_text("{malformed", encoding="utf-8")

    upgraded = _restart_backend(backend, tmp_path)

    assert json.loads(record_path.read_text(encoding="utf-8")) == current_record
    assert unreadable_path.read_text(encoding="utf-8") == "{malformed"
    [listed] = upgraded.list_workers(now=20.0)
    assert (listed.worker_key, listed.created_at, listed.startup_count) == (_TEST_UNSCOPED_WORKER_KEY, 10.0, 3)
    monkeypatch.setattr(upgraded, "_wait_for_ready", lambda _container, **_kwargs: None)
    upgraded.ensure_worker(_spec(upgraded, _TEST_UNSCOPED_WORKER_KEY), now=30.0)
    upgraded.ensure_worker(_spec(upgraded, _TEST_UNSCOPED_WORKER_KEY), now=40.0)
    assert container.removed == 1
    assert len(fake_client.containers.run_calls) == 2


@pytest.mark.parametrize("tampering", ["foreign_key", "symlinked_record", "malformed_record"])
def test_docker_backend_skips_unverifiable_legacy_worker_records(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    tampering: str,
) -> None:
    """A legacy mounted record never claims another worker's key or follows a planted link."""
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path)
    victim_key = "v1:default:unscoped:victim"
    victim = backend.ensure_worker(_spec(backend, victim_key), now=10.0)
    backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)
    _move_control_record_into_mounted_root(tmp_path, victim_key, {})
    _move_control_record_into_mounted_root(tmp_path, _TEST_UNSCOPED_WORKER_KEY, {})
    victim_record = worker_root_path(tmp_path, victim_key) / "metadata" / "worker.json"
    attacker_record = worker_root_path(tmp_path, _TEST_UNSCOPED_WORKER_KEY) / "metadata" / "worker.json"
    victim_record.unlink()
    if tampering == "foreign_key":
        attacker_record.write_text(json.dumps({"worker_key": victim_key}), encoding="utf-8")
    elif tampering == "symlinked_record":
        victim_record.write_text(json.dumps({"worker_key": victim_key}), encoding="utf-8")
        attacker_record.unlink()
        attacker_record.symlink_to(victim_record)
    else:
        attacker_record.write_text("{malformed", encoding="utf-8")

    upgraded = _restart_backend(backend, tmp_path)

    assert _control_metadata_path(tmp_path, _TEST_UNSCOPED_WORKER_KEY).exists() is False
    expected_victims = [victim_key] if tampering == "symlinked_record" else []
    assert [listed.worker_key for listed in upgraded.list_workers()] == expected_victims
    with pytest.raises(WorkerBackendError, match="missing the control identity metadata"):
        upgraded.retire_worker(_TEST_UNSCOPED_WORKER_KEY)
    assert fake_client.containers.by_name[victim.worker_id].removed == 0


def test_docker_backend_refuses_symlinked_run_root_with_malformed_target_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Malformed metadata cannot redirect Docker worker-state retirement through a run-key symlink."""
    backend, _fake_client, _sync_calls = _backend(monkeypatch, tmp_path)
    base_key = "v1:t:user_agent:alice:watcher"
    run_key = script_worker_key_for_run(base_key, f"script-{'1' * 32}")
    target_root = worker_root_path(tmp_path, base_key)
    (target_root / "metadata").mkdir(parents=True)
    (target_root / "metadata" / "worker.json").write_text("{malformed", encoding="utf-8")
    sentinel = target_root / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    worker_root_path(tmp_path, run_key).symlink_to(target_root, target_is_directory=True)

    with pytest.raises(WorkerBackendError, match="symbolic link"):
        backend.retire_worker(run_key)

    assert sentinel.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("metadata_contents", [None, "{malformed"])
def test_docker_backend_refuses_existing_run_root_without_exact_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    metadata_contents: str | None,
) -> None:
    """An existing Docker state root needs readable exact-key metadata before recursive deletion."""
    backend, _fake_client, _sync_calls = _backend(monkeypatch, tmp_path)
    run_key = script_worker_key_for_run("v1:t:user_agent:alice:watcher", f"script-{'2' * 32}")
    state_root = worker_root_path(tmp_path, run_key)
    state_root.mkdir(parents=True)
    sentinel = state_root / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    if metadata_contents is not None:
        metadata_file = _control_metadata_path(tmp_path, run_key)
        metadata_file.parent.mkdir(parents=True)
        metadata_file.write_text(metadata_contents, encoding="utf-8")

    with pytest.raises(WorkerBackendError, match="identity metadata"):
        backend.retire_worker(run_key)

    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_docker_backend_refuses_worker_root_swapped_after_identity_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Docker retirement cannot recursively delete a replacement after validating exact state."""
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path)
    run_key = script_worker_key_for_run("v1:t:user_agent:alice:watcher", f"script-{'3' * 32}")
    handle = backend.ensure_worker(_spec(backend, run_key, private_agent_names=frozenset()), now=1.0)
    state_root = worker_root_path(tmp_path, run_key)
    replacement_root = tmp_path / "replacement"
    replacement_root.mkdir()
    sentinel = replacement_root / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    retired_root = tmp_path / "retired-original"
    swapped = False
    original_stat = worker_retirement_module.os.stat

    def stat_after_swap(
        path: str | bytes,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> object:
        nonlocal swapped
        if path == state_root.name and dir_fd is not None and not swapped:
            descriptor_root = Path(f"/proc/self/fd/{dir_fd}").resolve()
            if descriptor_root == backend._workers_root:
                state_root.rename(retired_root)
                replacement_root.rename(state_root)
                replacement_root.symlink_to(state_root, target_is_directory=True)
                swapped = True
        return original_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(worker_retirement_module.os, "stat", stat_after_swap)

    with pytest.raises(WorkerBackendError, match="changed during retirement"):
        backend.retire_worker(run_key)

    assert fake_client.containers.by_name[handle.worker_id].removed == 0
    assert swapped is True
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_docker_backend_refuses_projection_root_swapped_after_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Docker retirement cannot recursively delete a replacement projection root."""
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path)
    run_key = script_worker_key_for_run("v1:t:user_agent:alice:watcher", f"script-{'4' * 32}")
    handle = backend.ensure_worker(_spec(backend, run_key, private_agent_names=frozenset()), now=1.0)
    worker_name = worker_dir_name(run_key)
    projected_configs_root = backend._projection_manager._projected_configs_root
    projection_root = projected_configs_root / worker_name
    projection_root.mkdir(parents=True, exist_ok=True)
    (projection_root / "old.txt").write_text("old", encoding="utf-8")
    replacement_root = tmp_path / "replacement-projection"
    replacement_root.mkdir()
    (replacement_root / "keep.txt").write_text("keep", encoding="utf-8")
    retired_projection_root = tmp_path / "retired-projection"
    swapped = False
    original_stat = worker_retirement_module.os.stat

    def stat_after_swap(
        path: str | bytes,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> object:
        nonlocal swapped
        if path == worker_name and dir_fd is not None and not swapped:
            descriptor_root = Path(f"/proc/self/fd/{dir_fd}").resolve()
            if descriptor_root == projected_configs_root:
                projection_root.rename(retired_projection_root)
                replacement_root.rename(projection_root)
                swapped = True
        return original_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(worker_retirement_module.os, "stat", stat_after_swap)

    with pytest.raises(WorkerBackendError, match="changed during retirement"):
        backend.retire_worker(run_key)

    assert fake_client.containers.by_name[handle.worker_id].removed == 1
    assert swapped is True
    assert worker_root_path(tmp_path, run_key).is_dir()
    assert (projection_root / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_docker_backend_records_failure_and_stops_container(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Recording a failure should stop the worker and persist failure metadata."""
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path)

    backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)

    handle = backend.record_failure(_TEST_UNSCOPED_WORKER_KEY, "boom", now=11.0)

    assert handle.status == "failed"
    assert handle.failure_count == 1
    assert handle.failure_reason == "boom"

    container = next(iter(fake_client.containers.by_name.values()))
    assert container.stopped == 1
    assert container.status == "exited"


def test_docker_backend_records_a_credential_mirroring_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A worker whose credentials cannot be mirrored fails like any other start, so its caller is told."""
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path)

    def _failing_sync(*_args: object, **_kwargs: object) -> None:
        msg = "credential store unreadable"
        raise OSError(msg)

    monkeypatch.setattr("mindroom.workers.backends.docker.sync_shared_credentials_to_worker", _failing_sync)
    _control_progress_clock(monkeypatch)
    progress_events: list[WorkerReadyProgress] = []

    with pytest.raises(WorkerBackendError, match="credential store unreadable"):
        backend.ensure_worker(
            _spec(backend, _TEST_UNSCOPED_WORKER_KEY),
            now=10.0,
            progress_sink=progress_events.append,
        )

    assert [event.phase for event in progress_events] == ["failed"]
    assert progress_events[0].error == "credential store unreadable"
    assert fake_client.containers.run_calls == []
    [listed] = backend.list_workers(now=11.0)
    assert listed.status == "failed"
    assert listed.failure_reason == "credential store unreadable"


def test_docker_worker_ready_failure_surfaces_container_logs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Startup log diagnostics should be useful without leaking secrets or huge payloads."""
    backend, _fake_client, _sync_calls = _backend(monkeypatch, tmp_path)
    container = _FakeContainer(
        name="mindroom-worker-x",
        image_identity="sha256:x",
        host_port=44999,
        environment={},
        labels={},
        status="exited",
    )
    container.logs_output = (
        b"OPENAI_API_KEY=sk-secret-value\nValidationError: agents.code.display_name Field required\n" + (b"x" * 5000)
    )

    # Call the real implementation; _backend patches the instance attribute.
    with pytest.raises(WorkerBackendError) as exc_info:
        DockerWorkerBackend._wait_for_ready(backend, container)
    message = str(exc_info.value)
    assert "display_name" in message
    assert "sk-secret-value" not in message
    assert "OPENAI_API_KEY=***redacted***" in message
    assert "... [truncated]" in message
    assert len(message) < 4300


def test_warm_ensure_resolves_image_once_and_detects_next_call_tag_change(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Avoid repeated daemon lookups without caching image identity across calls."""
    backend, client, _ = _backend(monkeypatch, tmp_path)
    spec = _spec(backend, _TEST_UNSCOPED_WORKER_KEY)
    backend.ensure_worker(spec)
    original = client.containers.created_containers[-1]
    client.images.gets.clear()

    backend.ensure_worker(spec)

    assert client.containers.by_name[original.name] is original
    assert client.images.gets == [backend.config.image]
    client.images.by_name[backend.config.image] = _FakeImage("sha256:image-v2")
    backend.ensure_worker(spec)
    assert original.removed == 1
    assert client.containers.created_containers[-1].attrs["Image"] == "sha256:image-v2"


def test_docker_worker_readiness_rejects_incompatible_image(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A successful stale health endpoint must not make the worker ready."""
    backend, _fake_client, _sync_calls = _backend(monkeypatch, tmp_path)
    container = _FakeContainer(
        name="mindroom-worker-stale",
        image_identity="sha256:stale",
        host_port=44999,
        environment={},
        labels={},
    )

    _install_health(monkeypatch, lambda _url: httpx.Response(200, json={"status": "ok"}))

    with pytest.raises(
        WorkerBackendError,
        match=r"^Docker worker image '.+' is incompatible.*expected worker protocol",
    ):
        DockerWorkerBackend._wait_for_ready(backend, container)


@pytest.mark.parametrize("stale_protocol", [0, 3], ids=["no-protocol", "before-environment-removals"])
def test_docker_worker_stale_image_pulls_and_relaunches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stale_protocol: int,
) -> None:
    """A stale-image protocol mismatch pulls the configured image and relaunches once, before any call reaches it."""
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path)
    fake_client.images.pull_image_id = "sha256:image-v2"
    _use_real_wait_for_ready(monkeypatch, backend)
    _slow_configured_image_pull(monkeypatch, backend, _control_progress_clock(monkeypatch))

    _install_health_by_image(monkeypatch, fake_client, stale_protocol=stale_protocol)

    progress_events: list[WorkerReadyProgress] = []

    handle = backend.ensure_worker(
        _spec(backend, _TEST_UNSCOPED_WORKER_KEY),
        now=0.0,
        progress_sink=progress_events.append,
    )

    assert fake_client.images.pulls == [backend.config.image]
    assert handle.status == "ready"
    assert handle.startup_count == 2
    assert handle.last_started_at == 0.0
    assert [event.phase for event in progress_events] == ["cold_start", "ready"]
    created = fake_client.containers.created_containers
    assert [item.attrs["Image"] for item in created] == ["sha256:image-v1", "sha256:image-v2"]
    assert created[0].removed == 1


def test_docker_worker_stale_image_recovery_updates_reused_worker_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Replacing a previously ready stale worker records a new startup attempt."""
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path)
    first_handle = backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)
    assert first_handle.startup_count == 1

    fake_client.images.pull_image_id = "sha256:image-v2"
    _use_real_wait_for_ready(monkeypatch, backend)
    _slow_configured_image_pull(monkeypatch, backend, _control_progress_clock(monkeypatch))
    _install_health_by_image(monkeypatch, fake_client)
    progress_events: list[WorkerReadyProgress] = []

    recovered_handle = backend.ensure_worker(
        _spec(backend, _TEST_UNSCOPED_WORKER_KEY),
        now=20.0,
        progress_sink=progress_events.append,
    )

    assert recovered_handle.startup_count == 2
    assert recovered_handle.last_started_at == 20.0
    assert [event.phase for event in progress_events] == ["cold_start", "ready"]
    created = fake_client.containers.created_containers
    assert [item.attrs["Image"] for item in created] == ["sha256:image-v1", "sha256:image-v2"]
    assert created[0].removed == 1


def test_docker_worker_stale_image_failed_pull_keeps_compatibility_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """When the pull fails, the original compatibility guidance still surfaces."""
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path)
    _use_real_wait_for_ready(monkeypatch, backend)
    _install_stale_health(monkeypatch)

    with pytest.raises(WorkerBackendError, match="expected worker protocol") as exc_info:
        backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=0.0)

    assert str(exc_info.value).endswith(
        f"Pulling '{backend.config.image}' failed: pull failed: {backend.config.image}",
    )
    assert fake_client.images.pulls == [backend.config.image]


def test_docker_worker_stale_replacement_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A replacement with the same protocol mismatch fails after one pull."""
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path)
    fake_client.images.pull_image_id = "sha256:image-v2"
    _use_real_wait_for_ready(monkeypatch, backend)
    _install_stale_health(monkeypatch)

    with pytest.raises(WorkerBackendError, match="expected worker protocol"):
        backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=0.0)

    assert fake_client.images.pulls == [backend.config.image]
    assert len(fake_client.containers.created_containers) == 2


def test_docker_worker_from_a_newer_release_is_replaced_once_without_a_pull(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Only a newer runtime fixes a newer worker, so its one replacement pulls nothing and then fails with that advice."""
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path)
    fake_client.images.pull_image_id = "sha256:image-v2"
    _use_real_wait_for_ready(monkeypatch, backend)
    _install_health_by_image(monkeypatch, fake_client, stale_protocol=WORKER_PROTOCOL_VERSION + 1)

    with pytest.raises(WorkerBackendError, match="comes from a newer MindRoom release"):
        backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=0.0)

    assert fake_client.images.pulls == []
    created = fake_client.containers.created_containers
    assert [item.attrs["Image"] for item in created] == ["sha256:image-v1", "sha256:image-v1"]
    assert created[0].removed == 1


def test_docker_worker_failed_stale_replacement_restarts_the_pulled_image_on_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failed replacement should be restarted without recreating the pulled image container."""
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path)
    fake_client.images.pull_image_id = "sha256:image-v2"
    readiness_calls = 0

    def _wait_for_ready(container: _FakeContainer, **_kwargs: object) -> None:
        nonlocal readiness_calls
        readiness_calls += 1
        if container.attrs["Image"] == "sha256:image-v1":
            raise _WorkerImageIncompatibleError(_STALE_IMAGE_MISMATCH)
        if readiness_calls == 2:
            message = "replacement failed"
            raise WorkerBackendError(message)

    monkeypatch.setattr(backend, "_wait_for_ready", _wait_for_ready)

    with pytest.raises(WorkerBackendError, match="replacement failed"):
        backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=0.0)

    replacement = fake_client.containers.created_containers[-1]
    assert replacement.attrs["Image"] == "sha256:image-v2"

    backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=1.0)

    assert fake_client.containers.by_name[replacement.name] is replacement
    assert replacement.started == 1
    assert replacement.removed == 0
    assert len(fake_client.containers.created_containers) == 2


def test_docker_worker_concurrent_tag_update_converges_each_ensure_on_the_pulled_image(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Concurrent ensures each resolve the configured image themselves and converge on the pulled one."""
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path)
    recovering_key = _TEST_UNSCOPED_WORKER_KEY
    stale_reader_key = "v1:default:unscoped:research"
    fake_client.images.pull_image_id = "sha256:image-v2"
    race = _ConcurrentTagUpdate(
        backend,
        recovering_key=recovering_key,
        stale_reader_key=stale_reader_key,
    )
    monkeypatch.setattr(backend, "_configured_image_id", race.configured_image_id)
    monkeypatch.setattr(backend, "_wait_for_ready", race.wait_for_ready)

    with ThreadPoolExecutor(max_workers=2) as executor:
        stale_reader = executor.submit(race.ensure_worker, stale_reader_key)
        assert race.stale_image_resolved.wait(timeout=5.0)
        recovering_worker = executor.submit(race.ensure_worker, recovering_key)
        recovering_worker.result(timeout=5.0)
        stale_reader.result(timeout=5.0)

    replacement = fake_client.containers.by_name[race.recovering_container_name]
    stale_reader_container = fake_client.containers.by_name[race.stale_reader_container_name]
    assert replacement.attrs["Image"] == "sha256:image-v2"
    assert stale_reader_container.attrs["Image"] == "sha256:image-v2"

    created_count = len(fake_client.containers.created_containers)
    backend.ensure_worker(_spec(backend, recovering_key), now=1.0)
    backend.ensure_worker(_spec(backend, stale_reader_key), now=1.0)
    assert len(fake_client.containers.created_containers) == created_count


def test_docker_backend_cleanup_reaps_abandoned_failed_container(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Idle cleanup should reap the exited container of a failed, idle-timed-out worker."""
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, idle_timeout_seconds=60.0)
    backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=0.0)
    backend.record_failure(_TEST_UNSCOPED_WORKER_KEY, "boom", now=1.0)
    container = next(iter(fake_client.containers.by_name.values()))

    # A recently failed worker keeps its exited container for a quick restart.
    backend.cleanup_idle_workers(now=10.0)
    assert container.removed == 0

    # Once past the idle timeout it is abandoned, so the container is reaped.
    backend.cleanup_idle_workers(now=1.0 + 61.0)
    assert container.removed == 1


def test_docker_backend_restart_clears_stale_failure_reason(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Workers that leave the failed state should not keep advertising the old failure message."""
    backend, _fake_client, _sync_calls = _backend(monkeypatch, tmp_path)

    backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)
    backend.record_failure(_TEST_UNSCOPED_WORKER_KEY, "boom", now=11.0)

    restarted = backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=12.0)
    assert restarted.status == "ready"
    assert restarted.failure_reason is None


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
                    "MINDROOM_SANDBOX_PROXY_TOKEN": "override",
                    "MINDROOM_STORAGE_PATH": str((tmp_path / "escape").resolve()),
                },
            ),
        },
    )

    with pytest.raises(
        WorkerBackendError,
        match="MINDROOM_SANDBOX_PROXY_TOKEN, MINDROOM_STORAGE_PATH",
    ):
        DockerWorkerBackendConfig.from_runtime(runtime_paths)


@pytest.mark.parametrize("credentials_in_env_file", [False, True], ids=["exported", "env-file-under-blank-export"])
def test_docker_backend_rejects_extra_env_replacing_primary_secret_path_variables(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    credentials_in_env_file: bool,
) -> None:
    """Dedicated worker extra env must not replace the primary's secret-path variables the worker inherits.

    The primary's `GOOGLE_APPLICATION_CREDENTIALS` counts as set when its `.env` names it under a blank export.
    """
    config_path = tmp_path / "config.yaml"
    secret_path = tmp_path / "secrets" / "openai.key"
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    secret_path.write_text("sk-test\n", encoding="utf-8")
    credentials_path = tmp_path / "adc.json"
    credentials_path.write_text('{"type":"service_account"}\n', encoding="utf-8")
    if credentials_in_env_file:
        (tmp_path / ".env").write_text(f"GOOGLE_APPLICATION_CREDENTIALS={credentials_path}\n", encoding="utf-8")
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path,
        process_env={
            "OPENAI_API_KEY_FILE": str(secret_path),
            "GOOGLE_APPLICATION_CREDENTIALS": "" if credentials_in_env_file else str(credentials_path),
        },
    )
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, runtime_paths=runtime_paths)
    overriding = _docker_backend(
        config=replace(
            backend.config,
            extra_env={"GOOGLE_APPLICATION_CREDENTIALS": "/override.json", "OPENAI_API_KEY_FILE": "/override.key"},
        ),
        auth_token=_TEST_AUTH_TOKEN,
        storage_root=tmp_path,
        runtime_paths=runtime_paths,
    )

    with pytest.raises(WorkerBackendError, match="GOOGLE_APPLICATION_CREDENTIALS, OPENAI_API_KEY_FILE"):
        overriding.ensure_worker(_spec(overriding, _TEST_UNSCOPED_WORKER_KEY), now=10.0)

    assert fake_client.containers.run_calls == []


@pytest.mark.parametrize("worker_scope", ["shared", "unscoped"])
def test_docker_projection_accepts_normalized_agent_names_in_worker_keys(
    tmp_path: Path,
    worker_scope: str,
) -> None:
    """Projection should match configured agents by the same normalized name used in worker keys."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        (
            """
agents:
  _my_agent:
    display_name: Test
    role: Test
    model: default
"""
            + ("    worker_scope: shared\n" if worker_scope == "shared" else "")
            + """
models:
  default:
    provider: openai
    id: test-model
router:
  model: default
"""
        ).lstrip(),
        encoding="utf-8",
    )
    runtime_paths = resolve_runtime_paths(config_path=config_path, storage_path=tmp_path)
    config = DockerWorkerBackendConfig(
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
    worker_paths = local_worker_state_paths_for_root(tmp_path / "workers" / f"normalized-{worker_scope}")
    if worker_scope == "shared":
        worker_key = resolve_worker_key(
            "shared",
            ToolExecutionIdentity(
                channel="matrix",
                agent_name="_my_agent",
                requester_id=None,
                room_id="!room:example.org",
                thread_id=None,
                resolved_thread_id=None,
                session_id=None,
                tenant_id="default",
            ),
            agent_name="_my_agent",
        )
    else:
        worker_key = resolve_unscoped_worker_key("_my_agent")

    projection = manager._projected_config(
        worker_paths,
        launch=_launch(runtime_paths),
        worker_key=worker_key,
        materialize=False,
    )
    projected_config = yaml.safe_load(projection.projected_yaml)

    assert list(projected_config["agents"]) == ["_my_agent"]


def test_docker_projection_keeps_no_references_to_stripped_agents(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Projected worker configs must stay loadable after projection strips other agents.

    References to stripped agents fail Config validation inside the worker
    ("calls.agents references unknown agent(s): ..."), crash-looping every
    dedicated worker start.
    """
    backend, fake_client, _sync_calls = _backend(
        monkeypatch,
        tmp_path,
        config_text="""
agents:
  alpha:
    display_name: Alpha
    role: Test
    model: default
    worker_scope: shared
    delegate_to: [beta]
  beta:
    display_name: Beta
    role: Test
    model: default
    worker_scope: shared
calls:
  enabled: true
  profiles:
    realtime-profile:
      backend: realtime
      model: test-realtime-model
      credentials_service: openai
      voice: test-voice
  agents:
    alpha: realtime-profile
    beta: realtime-profile
models:
  default:
    provider: openai
    id: test-model
""".lstrip(),
    )

    backend.ensure_worker(_spec(backend, "v1:default:shared:alpha"), now=10.0)

    volumes = _volumes_by_source(fake_client.containers.run_calls[0]["volumes"])
    projection_root = _projection_root(volumes)
    projected_config = yaml.safe_load((projection_root / "config.yaml").read_text(encoding="utf-8"))

    assert list(projected_config["agents"]) == ["alpha"]
    assert projected_config["agents"]["alpha"]["delegate_to"] == []
    assert "calls" not in projected_config

    # A dedicated worker loads its projection in runner mode, which runs no model.
    projected_runtime_paths = resolve_runtime_paths(
        config_path=projection_root / "config.yaml",
        storage_path=tmp_path / "projected-storage",
        process_env={"MINDROOM_SANDBOX_RUNNER_MODE": "true"},
    )

    assert set(load_config(projected_runtime_paths).agents) == {"alpha"}


def test_docker_backend_recreates_container_when_launch_config_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Token and launch-config changes should force worker recreation."""
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path)

    first_handle = backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)
    first_container = fake_client.containers.by_name[first_handle.worker_id]

    updated_config = replace(
        backend.config,
        user="2000:2000",
        runtime="runsc",
        extra_env={"EXTRA_ENV": "updated"},
    )
    updated_backend = _docker_backend(config=updated_config, auth_token=_ROTATED_AUTH_TOKEN, storage_root=tmp_path)
    monkeypatch.setattr(updated_backend, "_wait_for_ready", lambda _container, **_kwargs: None)

    second_handle = updated_backend.ensure_worker(_spec(updated_backend, _TEST_UNSCOPED_WORKER_KEY), now=20.0)

    second_container = fake_client.containers.by_name[first_handle.worker_id]
    assert second_container is not first_container
    assert first_container.removed == 1
    assert len(fake_client.containers.run_calls) == 2

    second_run_call = fake_client.containers.run_calls[-1]
    second_env = second_run_call["environment"]
    assert isinstance(second_env, dict)
    assert second_handle.auth_token != first_handle.auth_token
    assert second_env["MINDROOM_SANDBOX_PROXY_TOKEN"] == second_handle.auth_token
    assert second_env["EXTRA_ENV"] == "updated"
    assert second_run_call["user"] == "2000:2000"
    assert second_run_call["runtime"] == "runsc"


def test_docker_backend_recreates_computer_container_when_its_seccomp_profile_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A worker run under an older Computer security profile is replaced by one run under the current profile."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={WORKER_COMPUTER_ENABLED_ENV: "true"},
    )
    backend, fake_client, _sync_calls = _backend(
        monkeypatch,
        tmp_path,
        runtime_paths=runtime_paths,
        security_policy="computer",
    )
    first_handle = backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)
    first_container = fake_client.containers.by_name[first_handle.worker_id]
    updated_options = ["no-new-privileges:true", 'seccomp={"defaultAction":"SCMP_ACT_ERRNO"}']
    monkeypatch.setattr(docker_backend_module, "docker_worker_security_options", lambda: list(updated_options))

    backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=20.0)

    assert first_container.removed == 1
    assert fake_client.containers.run_calls[-1]["security_opt"] == updated_options


@pytest.mark.parametrize(
    ("computer_enabled", "security_policy"),
    [(False, "runtime_default"), (False, "computer"), (True, "computer")],
)
def test_docker_backend_reuses_container_with_expected_runtime_security(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    computer_enabled: bool,
    security_policy: Literal["runtime_default", "computer"],
) -> None:
    """An unchanged default or computer security policy permits worker reuse."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "state",
        process_env={WORKER_COMPUTER_ENABLED_ENV: "true"} if computer_enabled else {},
    )
    backend, fake_client, _sync_calls = _backend(
        monkeypatch,
        tmp_path,
        runtime_paths=runtime_paths,
        security_policy=security_policy,
    )
    first_handle = backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)
    first_container = fake_client.containers.by_name[first_handle.worker_id]

    second_handle = backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=20.0)

    assert second_handle.worker_id == first_handle.worker_id
    assert first_container.removed == 0
    assert len(fake_client.containers.run_calls) == 1


def test_docker_backend_recreates_container_when_validation_snapshot_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A surviving worker must restart when its authoritative validation state changes."""
    first_snapshot = {"shell": {"name": "shell", "config_fields": []}}
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path)
    first_handle = backend.ensure_worker(
        _spec(backend, _TEST_UNSCOPED_WORKER_KEY, tool_validation_snapshot=first_snapshot),
        now=10.0,
    )
    first_container = fake_client.containers.by_name[first_handle.worker_id]

    second_snapshot = {
        **first_snapshot,
        "file": {"name": "file", "config_fields": []},
    }
    backend.ensure_worker(
        _spec(backend, _TEST_UNSCOPED_WORKER_KEY, tool_validation_snapshot=second_snapshot),
        now=20.0,
    )

    second_container = fake_client.containers.by_name[first_handle.worker_id]
    assert second_container is not first_container
    assert first_container.removed == 1
    manifest_path = worker_root_path(tmp_path, _TEST_UNSCOPED_WORKER_KEY) / ".runtime" / "startup_manifest.json"
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["tool_validation_snapshot"] == second_snapshot


def test_docker_backend_recreates_container_when_name_prefix_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Changing the configured name prefix should recreate the worker with a new identity."""
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path)

    first_handle = backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)
    first_container = fake_client.containers.by_name[first_handle.worker_id]

    updated_config = replace(backend.config, name_prefix="other-prefix")
    updated_backend = _docker_backend(config=updated_config, auth_token=_TEST_AUTH_TOKEN, storage_root=tmp_path)
    monkeypatch.setattr(updated_backend, "_wait_for_ready", lambda _container, **_kwargs: None)

    second_handle = updated_backend.ensure_worker(_spec(updated_backend, _TEST_UNSCOPED_WORKER_KEY), now=20.0)

    assert second_handle.worker_id != first_handle.worker_id
    assert first_container.removed == 1
    assert len(fake_client.containers.run_calls) == 2
    assert fake_client.containers.run_calls[0]["name"] != fake_client.containers.run_calls[1]["name"]


@pytest.mark.parametrize(
    ("name_prefix", "names_prefix", "warned"),
    [
        ("benchmark-harness-run-20261018-0042-ab", "benchmark-harness-run-20261018-0042-ab", False),
        ("benchmark-harness-run-20261018-0042-abc", "benchmark-harness-run-20261018-0042-ab", True),
        ("benchmark-harness-run-20261018-0042-a-bc", "benchmark-harness-run-20261018-0042-a", True),
    ],
    ids=["38-characters-kept", "39-characters-cut", "cut-after-a-dash"],
)
def test_docker_backend_warns_once_when_container_names_cut_the_name_prefix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name_prefix: str,
    names_prefix: str,
    warned: bool,
) -> None:
    """Names keep 38 prefix characters, so a longer prefix is cut in every name and the backend says so once."""
    with capture_logs() as logs:
        backend, _fake_client, _sync_calls = _backend(monkeypatch, tmp_path, name_prefix=name_prefix)
        backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)
        handle = backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=20.0)

    assert handle.worker_id.rsplit("-", 1)[0] == names_prefix
    prefix_warnings = [entry for entry in logs if "MINDROOM_DOCKER_WORKER_NAME_PREFIX" in entry["event"]]
    assert [(entry["log_level"], entry["container_name_prefix"]) for entry in prefix_warnings] == (
        [("warning", names_prefix)] if warned else []
    )


def test_docker_backend_uses_distinct_container_names_for_different_storage_roots(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Separate runtimes should not share Docker worker identities."""
    first_backend, first_client, _sync_calls = _backend(monkeypatch, tmp_path, storage_root=tmp_path / "runtime-a")
    second_backend, second_client, _sync_calls = _backend(monkeypatch, tmp_path, storage_root=tmp_path / "runtime-b")

    first_handle = first_backend.ensure_worker(_spec(first_backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)
    second_handle = second_backend.ensure_worker(_spec(second_backend, _TEST_UNSCOPED_WORKER_KEY), now=20.0)

    assert first_handle.worker_id != second_handle.worker_id
    assert first_client.containers.run_calls[0]["name"] != second_client.containers.run_calls[0]["name"]


def test_docker_backend_shutdown_removes_running_containers_and_keeps_idle_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Backend shutdown should remove managed containers before the manager is discarded."""
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path)

    handle = backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)
    container = fake_client.containers.by_name[handle.worker_id]

    backend.shutdown()

    assert container.removed == 1
    idle_handle = backend.list_workers(now=20.0)[0]
    assert idle_handle.status == "idle"
    assert idle_handle.failure_reason is None


def _edit_config_file(path: Path, edit: Callable[[dict[str, Any]], None]) -> None:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    edit(data)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param(lambda data: data["agents"]["beta"].update(role="Renamed beta"), id="other-agent-prompt"),
        pytest.param(lambda data: data["agents"]["beta"].update(knowledge_bases=[]), id="other-agent-knowledge"),
        pytest.param(
            lambda data: data["knowledge_bases"]["b"].update(path="./knowledge_alpha"),
            id="other-knowledge-base",
        ),
        pytest.param(lambda data: data["models"]["default"].update(id="other-model"), id="model"),
        pytest.param(lambda data: data["agents"]["alpha"].update(role="New prompt"), id="own-prompt"),
    ],
)
def test_docker_worker_keeps_running_when_a_config_edit_changes_nothing_it_reads(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    edit: Callable[[dict[str, Any]], None],
) -> None:
    """Edits to models, prompts, or other agents leave a worker's projection, and so its container, alone."""
    config_text, _projected_paths = _multi_agent_projected_config_fixture(tmp_path)
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, config_text=config_text)
    worker_key = "v1:default:shared:alpha"
    handle = backend.ensure_worker(_spec(backend, worker_key), now=10.0)
    container = fake_client.containers.by_name[handle.worker_id]

    _edit_config_file(tmp_path / "config.yaml", edit)
    backend.ensure_worker(_spec(backend, worker_key), now=20.0)

    assert fake_client.containers.by_name[handle.worker_id] is container
    assert container.removed == 0
    assert len(fake_client.containers.run_calls) == 1


def test_docker_worker_restarts_when_its_agent_knowledge_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A knowledge base added to the worker's own agent reaches it through a replacement container."""
    config_text, _projected_paths = _multi_agent_projected_config_fixture(tmp_path)
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, config_text=config_text)
    worker_key = "v1:default:shared:alpha"
    handle = backend.ensure_worker(_spec(backend, worker_key), now=10.0)
    container = fake_client.containers.by_name[handle.worker_id]

    _edit_config_file(tmp_path / "config.yaml", lambda data: data["agents"]["alpha"].update(knowledge_bases=["a", "b"]))
    backend.ensure_worker(_spec(backend, worker_key), now=20.0)

    assert container.removed == 1
    assert len(fake_client.containers.run_calls) == 2
    projection_root = _projection_root(_volumes_by_source(fake_client.containers.run_calls[-1]["volumes"]))
    projected = yaml.safe_load((projection_root / "config.yaml").read_text(encoding="utf-8"))
    assert set(projected["knowledge_bases"]) == {"a", "b"}


def test_docker_projection_leaves_the_shared_launch_config_data_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Projection rewrites paths and filters agents on its own copy, so every worker sees the same launch."""
    config_text, _projected_paths = _projected_config_fixture(tmp_path)
    backend, _fake_client, _sync_calls = _backend(monkeypatch, tmp_path, config_text=config_text)
    launch = _launch(backend._runtime_paths)
    before = copy.deepcopy(launch.config_data)
    paths = local_worker_state_paths_for_root(tmp_path / "workers" / "projection-test")

    first = backend._projection_manager._projected_config(paths, launch=launch, worker_key=_TEST_UNSCOPED_WORKER_KEY)
    second = backend._projection_manager._projected_config(paths, launch=launch, worker_key=_TEST_UNSCOPED_WORKER_KEY)

    assert launch.config_data == before
    assert first.root == second.root
    assert "./.mindroom-worker-assets/knowledge_bases/docs" in first.projected_yaml


@pytest.mark.parametrize("worker_scope", ["shared", "user_agent"])
@pytest.mark.parametrize("changed_asset", ["history", "context"])
def test_docker_worker_keeps_running_when_mounted_agent_data_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    worker_scope: str,
    changed_asset: str,
) -> None:
    """A chat export or context edit must not replace a live browser worker."""
    workspace = tmp_path / "agents" / "alpha" / "workspace"
    history = workspace / "thread_exports" / "thread.yaml"
    history.parent.mkdir(parents=True)
    history.write_text("messages: []\n")
    context = workspace / "AGENTS.md"
    context.write_text("Initial context\n")
    backend, fake_client, _sync_calls = _backend(
        monkeypatch,
        tmp_path,
        config_text=f"""
agents:
  alpha:
    display_name: Alpha
    worker_scope: {worker_scope}
    knowledge_bases: [threads]
    context_files: [AGENTS.md]
knowledge_bases:
  threads:
    path: ./agents/alpha/workspace/thread_exports
models:
  default:
    provider: openai
    id: test-model
""",
    )
    key = "v1:default:shared:alpha" if worker_scope == "shared" else "v1:default:user_agent:@alice:example.org:alpha"
    spec = _spec(backend, key, private_agent_names=frozenset())
    first = backend.ensure_worker(spec, now=10.0)
    container = fake_client.containers.created_containers[0]
    (history if changed_asset == "history" else context).write_text("Updated conversation content\n")
    second = backend.ensure_worker(spec, now=20.0)
    assert container.status == "running"
    assert second.endpoint == first.endpoint
    assert len(fake_client.containers.created_containers) == 1
    projection = next(Path(m["Source"]) for m in container.attrs["Mounts"] if m["Destination"] == "/app/config-host")
    projected = yaml.safe_load((projection / "config.yaml").read_text())
    assert projected["knowledge_bases"]["threads"]["path"] == "/app/worker/agents/alpha/workspace/thread_exports"
    # Context files are primary prompt input, so workers never see them.
    assert "context_files" not in projected["agents"]["alpha"]


def test_docker_backend_recreates_container_when_projected_file_asset_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Changing a projected single-file asset should rotate the worker."""
    config_text, projected_paths = _projected_config_fixture(tmp_path)
    config_data = yaml.safe_load(config_text)
    config_data["knowledge_bases"]["docs"]["path"] = "./knowledge_docs/guide.md"
    config_text = yaml.safe_dump(config_data)
    external_file = projected_paths["knowledge_root"] / "guide.md"
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, config_text=config_text)

    handle = backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)
    existing_container = fake_client.containers.by_name[handle.worker_id]
    first_volumes = _volumes_by_source(fake_client.containers.run_calls[0]["volumes"])
    first_projection_root = _projection_root(first_volumes)

    updated_file = external_file.with_suffix(".updated.md")
    updated_file.write_text("# Updated Guide\n", encoding="utf-8")
    updated_file.replace(external_file)

    backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=20.0)

    replacement_container = fake_client.containers.by_name[handle.worker_id]
    assert replacement_container is not existing_container
    assert existing_container.removed == 1
    assert len(fake_client.containers.run_calls) == 2

    second_volumes = _volumes_by_source(fake_client.containers.run_calls[-1]["volumes"])
    second_projection_root = _projection_root(second_volumes)
    assert second_projection_root != first_projection_root
    projected_file = second_projection_root / ".mindroom-worker-assets" / "knowledge_bases" / "docs"
    assert projected_file.read_text(encoding="utf-8") == "# Updated Guide\n"


def test_docker_backend_recreates_container_when_projected_directory_asset_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Changing a projected directory asset should rotate the worker."""
    config_text, projected_paths = _projected_config_fixture(tmp_path)
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, config_text=config_text)

    handle = backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)
    existing_container = fake_client.containers.by_name[handle.worker_id]
    first_volumes = _volumes_by_source(fake_client.containers.run_calls[0]["volumes"])
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

    backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=20.0)

    replacement_container = fake_client.containers.by_name[handle.worker_id]
    assert replacement_container is not existing_container
    assert existing_container.removed == 1
    assert removal_checks == [True]
    assert len(fake_client.containers.run_calls) == 2

    second_volumes = _volumes_by_source(fake_client.containers.run_calls[-1]["volumes"])
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

    backend.ensure_worker(_spec(backend, worker_key), now=10.0)

    volumes = _volumes_by_source(fake_client.containers.run_calls[0]["volumes"])
    projection_root = _projection_root(volumes)
    projected_config = (projection_root / "config.yaml").read_text(encoding="utf-8")
    projected_config_data = yaml.safe_load(projected_config)

    assert "alpha.md" not in projected_config
    assert "path: ./.mindroom-worker-assets/knowledge_bases/a" in projected_config
    assert "path: ./.mindroom-worker-assets/knowledge_bases/b" not in projected_config
    assert set(projected_config_data["agents"]) == {"alpha"}
    assert set(projected_config_data["knowledge_bases"]) == {"a"}

    assert volumes[str((tmp_path / "agents/alpha/workspace").resolve())]["bind"] == "/app/worker/agents/alpha/workspace"
    assert not (projection_root / ".mindroom-worker-assets" / "agents").exists()

    projected_alpha_knowledge = projection_root / ".mindroom-worker-assets" / "knowledge_bases" / "a" / "a.txt"
    assert projected_alpha_knowledge.read_text(encoding="utf-8") == "alpha knowledge\n"
    assert not (projection_root / ".mindroom-worker-assets" / "knowledge_bases" / "b" / "b.txt").exists()

    assert projected_paths["alpha_context"].resolve() not in projection_root.parents
    assert projected_paths["beta_context"].resolve() not in projection_root.parents
    assert projected_paths["alpha_knowledge_root"].resolve() not in projection_root.parents
    assert projected_paths["beta_knowledge_root"].resolve() not in projection_root.parents


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

    backend.ensure_worker(_spec(backend, worker_key, private_agent_names=frozenset({"alpha"})), now=10.0)

    volumes = _volumes_by_source(fake_client.containers.run_calls[0]["volumes"])
    projection_root = _projection_root(volumes)
    projected_config_data = yaml.safe_load((projection_root / "config.yaml").read_text(encoding="utf-8"))

    assert set(projected_config_data["agents"]) == {"alpha"}
    assert set(projected_config_data["knowledge_bases"]) == {"a"}
    assert not (projection_root / ".mindroom-worker-assets" / "agents").exists()
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

    backend.ensure_worker(_spec(backend, worker_key, private_agent_names=frozenset({"alpha"})), now=10.0)

    volumes = _volumes_by_source(fake_client.containers.run_calls[0]["volumes"])
    projection_root = _projection_root(volumes)
    projected_config = yaml.safe_load((projection_root / "config.yaml").read_text(encoding="utf-8"))

    assert projected_config["agents"]["alpha"]["private"]["template_dir"] == (
        "./.mindroom-worker-assets/agents/alpha/private/template_dir"
    )
    assert (
        projection_root / ".mindroom-worker-assets" / "agents" / "alpha" / "private" / "template_dir" / "README.md"
    ).read_text(encoding="utf-8") == "template scaffold\n"

    # A dedicated worker loads its projection in runner mode, which runs no model.
    projected_runtime_paths = resolve_runtime_paths(
        config_path=projection_root / "config.yaml",
        storage_path=tmp_path / "projected-storage",
        process_env={"MINDROOM_SANDBOX_RUNNER_MODE": "true"},
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


def test_docker_backend_shared_worker_mounts_only_the_canonical_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Shared workers mount the canonical workspace, created before Docker could create it as root, and use it as HOME."""
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

    backend.ensure_worker(_spec(backend, worker_key), now=10.0)

    volumes = _volumes_by_source(fake_client.containers.run_calls[0]["volumes"])
    expected_agent_root = (tmp_path / "agents" / "alpha").resolve()
    assert str(expected_agent_root) not in volumes
    assert (expected_agent_root / "workspace").is_dir()
    assert volumes[str(expected_agent_root / "workspace")] == {
        "bind": "/app/worker/agents/alpha/workspace",
        "mode": "rw",
    }
    env = fake_client.containers.run_calls[0]["environment"]
    assert isinstance(env, dict)
    assert env["HOME"] == "/app/worker/agents/alpha/workspace"


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
        backend.ensure_worker(_spec(backend, worker_key), now=10.0)


def test_docker_backend_user_agent_home_is_neutral_without_visibility(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """User-agent HOME should come from worker-key scope, not visibility metadata."""
    config_text, _projected_paths = _private_user_agent_projected_config_fixture(tmp_path)
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

    assert worker_key is not None
    assert backend._container_env(worker_key)["HOME"] == "/app/worker"


def test_docker_backend_rejects_unknown_worker_keys_for_scoped_mounts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Malformed worker keys must not fall back to mounting the whole storage root."""
    config_text, _projected_paths = _multi_agent_projected_config_fixture(tmp_path)
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, config_text=config_text)

    with pytest.raises(WorkerBackendError, match="Unsupported worker key"):
        backend.ensure_worker(_spec(backend, "not-a-valid-worker-key"), now=10.0)
    assert fake_client.containers.run_calls == []


def test_docker_backend_rejects_stale_scoped_worker_keys_for_projection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Scoped worker keys must fail closed when they no longer resolve to any configured agent."""
    config_text, _projected_paths = _multi_agent_projected_config_fixture(tmp_path)
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, config_text=config_text)

    with pytest.raises(WorkerBackendError, match="does not match any configured agent policy"):
        backend.ensure_worker(_spec(backend, "v1:default:shared:missing"), now=10.0)
    assert fake_client.containers.run_calls == []


def test_docker_backend_rejects_scoped_worker_keys_that_no_longer_match_agent_policy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A stale shared worker key must not broaden projection after an agent switches to user_agent isolation."""
    config_text, _projected_paths = _private_user_agent_projected_config_fixture(tmp_path)
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, config_text=config_text)

    with pytest.raises(WorkerBackendError, match="does not match any configured agent policy"):
        backend.ensure_worker(_spec(backend, "v1:default:shared:alpha"), now=10.0)
    assert fake_client.containers.run_calls == []


def test_docker_backend_rejects_unknown_unscoped_worker_keys_for_projection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Unknown unscoped worker keys must not broaden the projected config."""
    config_text, _projected_paths = _multi_agent_projected_config_fixture(tmp_path)
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, config_text=config_text)

    with pytest.raises(WorkerBackendError, match="does not match any configured agent policy"):
        backend.ensure_worker(_spec(backend, "v1:default:unscoped:missing"), now=10.0)
    assert fake_client.containers.run_calls == []


def test_docker_backend_rejects_stale_unscoped_worker_keys_for_projection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A stale unscoped worker key must fail closed after an agent changes isolation scope."""
    config_text, _projected_paths = _private_user_agent_projected_config_fixture(tmp_path)
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, config_text=config_text)

    with pytest.raises(WorkerBackendError, match="does not match any configured agent policy"):
        backend.ensure_worker(_spec(backend, "v1:default:unscoped:alpha"), now=10.0)
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
            _spec(
                backend,
                "v1:tenant-123:user_agent:@alice:example.org:missing",
                private_agent_names=frozenset({"missing"}),
            ),
            now=10.0,
        )
    assert fake_client.containers.run_calls == []


def test_docker_backend_projects_shared_agent_for_narrower_user_agent_worker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A script-isolated worker should project its agent even when normal tools use shared scope."""
    config_text, _projected_paths = _multi_agent_projected_config_fixture(tmp_path)
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, config_text=config_text)
    worker_key = "v1:default:user_agent:@alice:example.org:alpha"

    backend.ensure_worker(
        _spec(backend, worker_key, private_agent_names=frozenset()),
        now=10.0,
    )

    volumes = _volumes_by_source(fake_client.containers.run_calls[0]["volumes"])
    projected_config = yaml.safe_load((_projection_root(volumes) / "config.yaml").read_text(encoding="utf-8"))
    assert list(projected_config["agents"]) == ["alpha"]
    assert str(agent_state_root_path(tmp_path, "alpha") / "workspace") in volumes
    assert str(agent_state_root_path(tmp_path, "alpha")) not in volumes


def test_docker_backend_precreates_nested_storage_mount_targets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Docker must not create nested worker mount targets with daemon ownership."""
    config_text, _projected_paths = _multi_agent_projected_config_fixture(tmp_path)
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, config_text=config_text)
    worker_key = "v1:default:user_agent:@alice:example.org:alpha"
    worker_root = worker_root_path(tmp_path, worker_key)
    original_run = fake_client.containers.run

    def run_after_asserting_mount_target(image: str, **kwargs: object) -> _FakeContainer:
        assert (worker_root / "agents" / "alpha" / "workspace").is_dir()
        return original_run(image, **kwargs)

    monkeypatch.setattr(fake_client.containers, "run", run_after_asserting_mount_target)

    backend.ensure_worker(
        _spec(backend, worker_key, private_agent_names=frozenset()),
        now=10.0,
    )

    assert (worker_root / "agents" / "alpha" / "workspace").is_dir()
    assert len(fake_client.containers.run_calls) == 1

    backend.retire_worker(worker_key)

    assert not worker_root.exists()


def test_docker_backend_refuses_a_linked_nested_mount_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Worker code owns its worker root, so a link planted where a mount target belongs is refused, not followed."""
    config_text, _projected_paths = _multi_agent_projected_config_fixture(tmp_path)
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, config_text=config_text)
    worker_key = "v1:default:user_agent:@alice:example.org:alpha"
    worker_root = worker_root_path(tmp_path, worker_key)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    worker_root.mkdir(parents=True)
    (worker_root / "agents").symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(WorkerBackendError, match="must be a real directory"):
        backend.ensure_worker(_spec(backend, worker_key, private_agent_names=frozenset()), now=10.0)

    assert fake_client.containers.run_calls == []
    assert list(elsewhere.iterdir()) == []


def test_docker_backend_never_restarts_a_worker_over_a_link_planted_at_a_nested_mount_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Docker resolves bind destinations again on every start, so a stopped worker's nested targets are walked first."""
    config_text, _projected_paths = _multi_agent_projected_config_fixture(tmp_path)
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, config_text=config_text)
    worker_key = "v1:default:user_agent:@alice:example.org:alpha"
    spec = _spec(backend, worker_key, private_agent_names=frozenset())
    handle = backend.ensure_worker(spec, now=10.0)
    container = fake_client.containers.by_name[handle.worker_id]
    container.stop()
    # Worker code renamed the parent it owns and left a link where the workspace mount lands.
    agents = worker_root_path(tmp_path, worker_key) / "agents"
    agents.rename(agents.with_name("agents.old"))
    (agents / "alpha").mkdir(parents=True)
    (agents / "alpha" / "workspace").symlink_to("/app", target_is_directory=True)

    with pytest.raises(WorkerBackendError, match="must be a real directory"):
        backend.ensure_worker(spec, now=20.0)

    assert container.started == 0
    assert len(fake_client.containers.run_calls) == 1


def test_docker_backend_never_mounts_a_missing_or_linked_private_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Docker would create a missing source as root, and a linked one would expose another instance."""
    config_text, _projected_paths = _private_user_agent_projected_config_fixture(tmp_path)
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, config_text=config_text)
    worker_key = "v1:default:user_agent:~@mallory:localhost:alpha"
    spec = _spec(backend, worker_key, private_agent_names=frozenset({"alpha"}))

    backend.ensure_worker(spec, now=10.0)

    first_volumes = _volumes_by_source(fake_client.containers.run_calls[-1]["volumes"])
    assert not any("private_instances" in source for source in first_volumes)
    assert not (tmp_path / "private_instances").exists()

    victim_workspace = _materialize_private_workspace(
        tmp_path,
        "v1:default:user_agent:~@alice:localhost:alpha",
        "@alice:localhost",
    )
    ensure_private_instance_identity(tmp_path, worker_key=worker_key, requester_id="@mallory:localhost")
    (private_instance_scope_root_path(tmp_path, worker_key) / "alpha").symlink_to(
        victim_workspace.parent,
        target_is_directory=True,
    )
    backend.ensure_worker(spec, now=20.0)

    volumes = _volumes_by_source(fake_client.containers.run_calls[-1]["volumes"])
    assert not any("private_instances" in source for source in volumes)
    assert not any("private_instances" in volume["bind"] for volume in volumes.values())


def test_docker_backend_rejects_ambiguous_normalized_user_agent_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A normalized script worker key cannot silently select the first matching agent."""
    config_text = """
agents:
  foo:
    display_name: Foo
    tools: [script]
  foo_:
    display_name: Foo underscore
    tools: [script]
models:
  default:
    provider: openai
    id: test-model
router:
  model: default
"""
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, config_text=config_text)

    with pytest.raises(WorkerBackendError, match="ambiguous normalized agent name"):
        backend.ensure_worker(
            _spec(
                backend,
                "v1:default:user_agent:@alice:example.org:foo",
                private_agent_names=frozenset(),
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

    backend.ensure_worker(_spec(backend, "v1:tenant-123:user:@alice:example.org"), now=10.0)

    volumes = _volumes_by_source(fake_client.containers.run_calls[0]["volumes"])
    projection_root = _projection_root(volumes)
    projected_config_data = yaml.safe_load((projection_root / "config.yaml").read_text(encoding="utf-8"))

    agent_binds = {source: spec["bind"] for source, spec in volumes.items() if "/agents" in spec["bind"]}
    alpha_workspace = str((tmp_path / "agents" / "alpha" / "workspace").resolve())
    assert agent_binds == {alpha_workspace: "/app/worker/agents/alpha/workspace"}
    assert volumes[alpha_workspace]["mode"] == "rw"
    assert set(projected_config_data["agents"]) == {"alpha", "delta"}
    assert set(projected_config_data["knowledge_bases"]) == {"a", "d"}
    # Context files are primary prompt input, so no agent's context files reach the worker.
    assert "context_files" not in projected_config_data["agents"]["alpha"]
    assert "context_files" not in projected_config_data["agents"]["delta"]
    assert not (projection_root / ".mindroom-worker-assets" / "agents").exists()
    assert (projection_root / ".mindroom-worker-assets" / "knowledge_bases" / "a" / "alpha.txt").read_text(
        encoding="utf-8",
    ) == "alpha knowledge\n"
    assert (projection_root / ".mindroom-worker-assets" / "knowledge_bases" / "d" / "delta.txt").read_text(
        encoding="utf-8",
    ) == "delta knowledge\n"
    assert not (projection_root / ".mindroom-worker-assets" / "knowledge_bases" / "b" / "beta.txt").exists()
    assert not (projection_root / ".mindroom-worker-assets" / "knowledge_bases" / "c" / "gamma.txt").exists()


def test_docker_backend_creates_the_shared_workspaces_of_user_scoped_agents_before_mounting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A requester workstation mounts every shared user-scoped agent's workspace, created first if it is missing."""
    config_text = """
agents:
  alpha:
    display_name: Alpha
    worker_scope: user
models:
  default:
    provider: openai
    id: test-model
""".lstrip()
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, config_text=config_text)
    workspace = tmp_path / "agents" / "alpha" / "workspace"
    assert not workspace.exists()

    backend.ensure_worker(_spec(backend, "v1:tenant-123:user:@alice:example.org"), now=10.0)

    assert workspace.is_dir()
    volumes = _volumes_by_source(fake_client.containers.run_calls[0]["volumes"])
    assert volumes[str(workspace)]["bind"] == "/app/worker/agents/alpha/workspace"


def test_docker_backend_rejects_user_worker_keys_without_user_scoped_agents(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Requester-scoped workers must fail closed when no configured agent resolves to worker_scope=user."""
    config_text, _projected_paths = _multi_agent_projected_config_fixture(tmp_path)
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, config_text=config_text)

    with pytest.raises(WorkerBackendError, match="does not match any configured agent policy"):
        backend.ensure_worker(_spec(backend, "v1:tenant-123:user:@alice:example.org"), now=10.0)
    assert fake_client.containers.run_calls == []


def _materialize_private_workspace(storage_root: Path, worker_key: str, requester_id: str) -> Path:
    ensure_private_instance_identity(storage_root, worker_key=worker_key, requester_id=requester_id)
    workspace = private_instance_scope_root_path(storage_root, worker_key) / "alpha" / "alpha_data"
    workspace.mkdir(parents=True)
    return workspace


def test_docker_backend_user_agent_mounts_only_the_private_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A private user-agent worker mounts only its private workspace, never the scope holding the identity record."""
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

    assert worker_key is not None
    private_workspace = _materialize_private_workspace(tmp_path, worker_key, "@alice:example.org")

    backend.ensure_worker(_spec(backend, worker_key, private_agent_names=frozenset({"alpha"})), now=10.0)

    volumes = _volumes_by_source(fake_client.containers.run_calls[0]["volumes"])
    expected_private_root = (tmp_path / "private_instances" / worker_dir_name(worker_key)).resolve()
    assert str(expected_private_root) not in volumes
    assert {source: spec for source, spec in volumes.items() if "private_instances" in source} == {
        str(private_workspace.resolve()): {
            "bind": f"/app/worker/private_instances/{worker_dir_name(worker_key)}/alpha/alpha_data",
            "mode": "rw",
        },
    }
    assert all(not spec["bind"].startswith("/app/worker/agents/") for spec in volumes.values())
    env = fake_client.containers.run_calls[0]["environment"]
    assert isinstance(env, dict)
    assert env["HOME"] == "/app/worker"


def test_docker_backend_mounts_verified_historical_scope_twice(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Both historical and canonical destinations retain the same source, including on worker reuse."""
    config_text, _projected_paths = _private_user_agent_projected_config_fixture(tmp_path)
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, config_text=config_text)
    worker_key = "v1:default:user_agent:~@alice:example.org:alpha"
    private_workspace = _materialize_private_workspace(tmp_path, worker_key, "@alice:example.org")
    canonical = private_instance_scope_root_path(tmp_path, worker_key)
    legacy = private_instance_scope_root_path(tmp_path, "v1:default:user_agent:@alice:example.org:alpha")
    legacy.symlink_to(canonical.name, target_is_directory=True)
    worker_root = worker_root_path(tmp_path, worker_key)
    original_run = fake_client.containers.run

    def run_with_prepared_alias_targets(image: str, **kwargs: object) -> _FakeContainer:
        for scope in (canonical, legacy):
            assert (worker_root / "private_instances" / scope.name / "alpha" / "alpha_data").is_dir()
        return original_run(image, **kwargs)

    monkeypatch.setattr(fake_client.containers, "run", run_with_prepared_alias_targets)
    spec = _spec(backend, worker_key, private_agent_names=frozenset({"alpha"}))
    backend.ensure_worker(spec, now=10.0)
    backend.ensure_worker(spec, now=11.0)

    assert len(fake_client.containers.run_calls) == 1
    mounts = fake_client.containers.created_containers[0].attrs["Mounts"]
    source = str(private_workspace)
    assert {
        (mount["Source"], mount["Destination"], mount["RW"])
        for mount in mounts
        if "private_instances" in mount["Source"]
    } == {
        (source, f"/app/worker/private_instances/{canonical.name}/alpha/alpha_data", True),
        (source, f"/app/worker/private_instances/{legacy.name}/alpha/alpha_data", True),
    }
    assert all(mount["Destination"] != "/app/worker/private_instances" for mount in mounts)


def test_docker_script_worker_mounts_the_owning_private_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A script worker mounts only its owning scope's private workspace, not the scope or its identity record."""
    config_text, _projected_paths = _private_user_agent_projected_config_fixture(tmp_path)
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, config_text=config_text)
    state_scope_worker_key = "v1:tenant-123:user_agent:~@alice:example.org:alpha"
    worker_key = script_worker_key_for_run(state_scope_worker_key, f"script-{'a' * 32}")
    private_workspace = _materialize_private_workspace(tmp_path, state_scope_worker_key, "@alice:example.org")

    backend.ensure_worker(
        _spec(
            backend,
            worker_key,
            private_agent_names=frozenset({"alpha"}),
            state_scope_worker_key=state_scope_worker_key,
        ),
        now=10.0,
    )

    volumes = _volumes_by_source(fake_client.containers.run_calls[0]["volumes"])
    expected_private_root = (tmp_path / "private_instances" / worker_dir_name(state_scope_worker_key)).resolve()
    expected_run_root = worker_root_path(tmp_path, worker_key)
    assert str(expected_private_root) not in volumes
    assert volumes[str(private_workspace.resolve())] == {
        "bind": f"/app/worker/private_instances/{worker_dir_name(state_scope_worker_key)}/alpha/alpha_data",
        "mode": "rw",
    }
    assert str(expected_run_root) in volumes
    assert str(tmp_path / "private_instances" / worker_dir_name(worker_key) / "alpha") not in volumes


def test_docker_backend_rejects_private_user_agent_container_without_target_visibility(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Private user-agent workers must fail closed until the targeted private agent is explicitly visible."""
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

    with pytest.raises(WorkerBackendError, match="missing from explicit private-agent visibility"):
        backend.ensure_worker(_spec(backend, worker_key, private_agent_names=frozenset()), now=10.0)
    assert fake_client.containers.run_calls == []

    assert worker_key is not None
    private_workspace = _materialize_private_workspace(tmp_path, worker_key, "@alice:example.org")
    handle = backend.ensure_worker(_spec(backend, worker_key, private_agent_names=frozenset({"alpha"})), now=20.0)
    assert len(fake_client.containers.run_calls) == 1
    second_volumes = _volumes_by_source(fake_client.containers.run_calls[0]["volumes"])
    assert second_volumes[str(private_workspace.resolve())] == {
        "bind": f"/app/worker/private_instances/{worker_dir_name(worker_key)}/alpha/alpha_data",
        "mode": "rw",
    }
    assert handle.status == "ready"


def test_docker_worker_without_a_running_container_has_no_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Only a running container publishes a port, and the control record keeps no copy of one."""
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, idle_timeout_seconds=60.0)
    handle = backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)
    container = fake_client.containers.by_name[handle.worker_id]
    assert handle.endpoint == f"http://127.0.0.1:{backend._container_host_port(container)}/api/sandbox-runner/execute"

    container.stop()
    [stopped] = backend.list_workers(include_idle=True, now=20.0)
    del fake_client.containers.by_name[handle.worker_id]
    [missing] = backend.list_workers(include_idle=True, now=20.0)

    for listed in (stopped, missing):
        assert listed.status == "idle"
        assert listed.endpoint == ""
        assert listed.debug_metadata["host_port"] == ""
        assert listed.debug_metadata["api_root"] == ""
    assert stopped.debug_metadata["container_id"] == container.id
    assert missing.debug_metadata["container_id"] == ""
    record = json.loads(_control_metadata_path(tmp_path, _TEST_UNSCOPED_WORKER_KEY).read_text(encoding="utf-8"))
    assert {"endpoint", "host_port", "container_id"}.isdisjoint(record)


@pytest.mark.parametrize("ports", [{}, {"8766/tcp": [{"HostIp": "127.0.0.1", "HostPort": "not-a-port"}]}])
def test_docker_worker_restarted_without_a_published_port_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ports: dict[str, object],
) -> None:
    """A restarted container that publishes no usable port fails the ensure instead of serving no endpoint."""
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path)
    handle = backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)
    container = fake_client.containers.by_name[handle.worker_id]
    container.stop()
    container.attrs["NetworkSettings"]["Ports"] = ports

    with pytest.raises(WorkerBackendError, match="missing a published port"):
        backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=20.0)


def test_docker_worker_recorded_as_starting_reports_starting_with_or_without_its_container(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A worker another start is still bringing up reports starting, even past its idle timeout."""
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, idle_timeout_seconds=60.0)
    handle = backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)
    metadata_path = _control_metadata_path(tmp_path, _TEST_UNSCOPED_WORKER_KEY)
    record = json.loads(metadata_path.read_text(encoding="utf-8"))
    record["status"] = "starting"
    metadata_path.write_text(json.dumps(record), encoding="utf-8")

    [running] = backend.list_workers(include_idle=True, now=100.0)
    fake_client.containers.by_name[handle.worker_id].stop()
    [stopped] = backend.list_workers(include_idle=True, now=100.0)

    assert running.status == "starting"
    assert stopped.status == "starting"


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
    base_config = DockerWorkerBackendConfig(
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
    first_projection = first_manager._projected_config(paths, launch=_launch(runtime_paths), materialize=True)

    renamed_manager = DockerProjectionManager(
        config=replace(base_config, config_path="/app/config-host/alt.yaml"),
        projected_configs_root=tmp_path / "projections",
        runtime_paths=runtime_paths,
    )
    renamed_projection = renamed_manager._projected_config(paths, launch=_launch(runtime_paths), materialize=True)

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
    config = DockerWorkerBackendConfig(
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

    first_projection = manager._projected_config(paths, launch=_launch(runtime_paths), materialize=True)
    first_projected_file = first_projection.root / ".mindroom-worker-assets" / "plugins" / "00-demo" / "helper.sh"
    assert first_projected_file.stat().st_mode & 0o777 == 0o644

    plugin_file.chmod(0o755)

    second_projection = manager._projected_config(paths, launch=_launch(runtime_paths), materialize=True)
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
    config = DockerWorkerBackendConfig(
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

    first_projection = manager._projected_config(paths, launch=_launch(runtime_paths), materialize=True)
    first_projected_dir = first_projection.root / ".mindroom-worker-assets" / "plugins" / "00-demo"
    assert first_projected_dir.stat().st_mode & 0o777 == 0o755

    plugin_dir.chmod(0o700)

    second_projection = manager._projected_config(paths, launch=_launch(runtime_paths), materialize=True)
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

    handle = backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)
    existing_container = fake_client.containers.by_name[handle.worker_id]
    first_volumes = _volumes_by_source(fake_client.containers.run_calls[0]["volumes"])
    projection_root = _projection_root(first_volumes)

    (projection_root / ".projection-ready").unlink()
    (projection_root / "config.yaml").write_text("broken: true\n", encoding="utf-8")

    backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=20.0)

    second_volumes = _volumes_by_source(fake_client.containers.run_calls[-1]["volumes"])
    second_projection_root = _projection_root(second_volumes)
    replacement_container = fake_client.containers.by_name[handle.worker_id]

    assert second_projection_root == projection_root
    assert replacement_container is not existing_container
    assert existing_container.removed == 1
    assert (projection_root / ".projection-ready").read_text(encoding="utf-8") == "ready\n"
    rebuilt_config = (projection_root / "config.yaml").read_text(encoding="utf-8")
    assert "broken: true" not in rebuilt_config
    assert "plugins:" in rebuilt_config


def test_docker_backend_rebuilds_corrupted_ready_projection_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A leftover ready marker must not make the backend trust a projection missing required files."""
    config_text, _projected_paths = _projected_config_fixture(tmp_path)
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, config_text=config_text)

    handle = backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)
    existing_container = fake_client.containers.by_name[handle.worker_id]
    first_volumes = _volumes_by_source(fake_client.containers.run_calls[0]["volumes"])
    projection_root = _projection_root(first_volumes)

    (projection_root / "config.yaml").unlink()
    assert (projection_root / ".projection-ready").is_file()

    backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=20.0)

    replacement_container = fake_client.containers.by_name[handle.worker_id]
    assert replacement_container is not existing_container
    assert existing_container.removed == 1
    assert len(fake_client.containers.run_calls) == 2
    assert (projection_root / ".projection-ready").read_text(encoding="utf-8") == "ready\n"
    rebuilt_config = (projection_root / "config.yaml").read_text(encoding="utf-8")
    assert "plugins:" in rebuilt_config
    assert "memory:" in rebuilt_config


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

    backend.ensure_worker(_spec(backend, "v1:default:shared:alpha"), now=10.0)

    volumes = _volumes_by_source(fake_client.containers.run_calls[0]["volumes"])
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
    """The first run pulls a missing image, and the next ensure reuses the container it started."""
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path)
    del fake_client.images.by_name[backend.config.image]
    fake_client.images.pull_image_id = "sha256:pulled-image"

    first_handle = backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)
    first_container = fake_client.containers.by_name[first_handle.worker_id]

    second_handle = backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=20.0)

    assert fake_client.images.pulls == [backend.config.image]
    assert fake_client.containers.run_calls[0]["image"] == "sha256:pulled-image"
    assert second_handle.worker_id == first_handle.worker_id
    assert fake_client.containers.by_name[second_handle.worker_id] is first_container
    assert len(fake_client.containers.run_calls) == 1
    assert first_container.removed == 0
    metadata_path = _control_metadata_path(tmp_path, _TEST_UNSCOPED_WORKER_KEY)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["startup_count"] == 1
    assert metadata["last_started_at"] == 10.0
    assert metadata["last_used_at"] == 20.0
    assert metadata["status"] == "ready"


def test_docker_backend_reuses_container_after_its_image_leaves_the_local_daemon(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A pruned local image keeps a running worker whose container still records the image it started from."""
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path)
    snapshot: dict[str, dict[str, object]] = {"shell": {"available": True}}
    first_handle = backend.ensure_worker(
        _spec(backend, _TEST_UNSCOPED_WORKER_KEY, tool_validation_snapshot=snapshot),
        now=10.0,
    )
    first_container = fake_client.containers.by_name[first_handle.worker_id]
    del fake_client.images.by_name[backend.config.image]

    backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY, tool_validation_snapshot=snapshot), now=20.0)

    assert first_container.removed == 0
    assert len(fake_client.containers.run_calls) == 1


def test_docker_backend_restarts_a_stopped_worker_whose_image_left_the_local_daemon(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A stopped worker's own image proves it current, so an idle-stopped worker restarts without the registry."""
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path)
    handle = backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)
    first_container = fake_client.containers.by_name[handle.worker_id]
    first_container.stop()
    del fake_client.images.by_name[backend.config.image]

    backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=20.0)

    assert fake_client.images.pulls == []
    assert first_container.removed == 0
    assert len(fake_client.containers.run_calls) == 1


def test_docker_workers_get_their_own_runner_tokens(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A runner token taken from one requester's worker must not authenticate to another requester's worker."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={SANDBOX_RUNTIME_ENV_BY_KEY["proxy_token"]: _TEST_AUTH_TOKEN},
    )
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, runtime_paths=runtime_paths)
    keys = ("v1:default:user_agent:@alice:example.org:code", "v1:default:user_agent:@bob:example.org:code")
    handles = [backend.ensure_worker(_spec(backend, key, private_agent_names=frozenset()), now=10.0) for key in keys]

    assert handles[0].auth_token != handles[1].auth_token
    for call, handle in zip(fake_client.containers.run_calls, handles, strict=True):
        assert call["environment"][SANDBOX_RUNTIME_ENV_BY_KEY["proxy_token"]] == handle.auth_token
        assert _TEST_AUTH_TOKEN not in json.dumps(call, default=str)


@pytest.mark.parametrize("flag", [None, "false"])
def test_docker_ordinary_workers_run_without_computer_security_options(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    flag: str | None,
) -> None:
    """Without the Computer opt-in, a worker keeps Docker's default security and is reused while nothing changes."""
    env = {
        "MINDROOM_WORKER_BACKEND": "docker",
        "MINDROOM_DOCKER_WORKER_IMAGE": "ghcr.io/mindroom-ai/mindroom:latest",
        "MINDROOM_DOCKER_WORKER_USER": "1000:1000",
    }
    if flag is not None:
        env[WORKER_COMPUTER_ENABLED_ENV] = flag
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env=env,
    )
    backend, client, _ = _backend(monkeypatch, tmp_path, runtime_paths=runtime_paths)

    first = backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)
    second = backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=20.0)

    assert "cap_drop" not in client.containers.run_calls[0]
    assert "security_opt" not in client.containers.run_calls[0]
    assert second.worker_id == first.worker_id
    assert client.containers.by_name[first.worker_id].removed == 0
    assert len(client.containers.run_calls) == 1


def test_docker_launch_hash_label_digests_the_whole_run_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The launch-hash label is the digest of every ``containers.run`` argument, the image ID included, but itself."""
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path)

    handle = backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)

    run_call = copy.deepcopy(fake_client.containers.run_calls[0])
    launch_hash = run_call["labels"].pop("mindroom.ai/launch-config-hash")
    canonical_call = json.dumps(run_call, sort_keys=True, separators=(",", ":"))
    assert launch_hash == hashlib.sha256(canonical_call.encode()).hexdigest()
    assert run_call["image"] == "sha256:image-v1"
    assert run_call["labels"]["mindroom.ai/image-reference"] == backend.config.image
    manifest_path = sandbox_startup_manifest_path(Path(handle.debug_metadata["state_root"]))
    assert (
        run_call["labels"]["mindroom.ai/startup-manifest-hash"]
        == hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    )


def test_docker_backend_pulls_a_changed_image_that_is_not_local_and_replaces_the_worker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A running worker's own image never stands in for a newly configured image, which is pulled to replace it."""
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path)
    handle = backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)
    first_container = fake_client.containers.by_name[handle.worker_id]
    fake_client.images.pull_image_id = "sha256:image-next"
    changed = _docker_backend(
        config=replace(backend.config, image="ghcr.io/mindroom-ai/mindroom:next"),
        auth_token=_TEST_AUTH_TOKEN,
        storage_root=tmp_path,
    )
    monkeypatch.setattr(changed, "_wait_for_ready", lambda _container, **_kwargs: None)

    changed.ensure_worker(_spec(changed, _TEST_UNSCOPED_WORKER_KEY), now=20.0)

    assert fake_client.images.pulls == ["ghcr.io/mindroom-ai/mindroom:next"]
    assert first_container.removed == 1
    run_call = fake_client.containers.run_calls[-1]
    assert run_call["image"] == "sha256:image-next"
    assert run_call["labels"]["mindroom.ai/image-reference"] == "ghcr.io/mindroom-ai/mindroom:next"


@pytest.mark.parametrize("initial_policy", ["runtime_default", "computer"])
def test_docker_security_policy_changes_identity_and_replaces_worker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    initial_policy: Literal["runtime_default", "computer"],
) -> None:
    """The operator policy replaces workers in either direction with Computer disabled."""
    env = {
        "MINDROOM_DOCKER_WORKER_IMAGE": "ghcr.io/mindroom-ai/mindroom:latest",
        WORKER_COMPUTER_ENABLED_ENV: "false",
        "MINDROOM_DOCKER_WORKER_SECURITY_POLICY": initial_policy,
    }
    paths = resolve_primary_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env=env)
    backend, client, _ = _backend(monkeypatch, tmp_path, runtime_paths=paths, security_policy=initial_policy)
    first = backend.ensure_worker(_spec(backend, _TEST_UNSCOPED_WORKER_KEY), now=10.0)
    container = client.containers.by_name[first.worker_id]
    changed_policy = "computer" if initial_policy == "runtime_default" else "runtime_default"
    changed_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={**env, "MINDROOM_DOCKER_WORKER_SECURITY_POLICY": changed_policy},
    )
    replacement = _docker_backend(
        config=replace(backend.config, security_policy=changed_policy),
        auth_token=_TEST_AUTH_TOKEN,
        storage_root=tmp_path,
        runtime_paths=changed_paths,
    )
    monkeypatch.setattr(replacement, "_wait_for_ready", backend._wait_for_ready)
    assert (
        configure_docker_worker_backend(paths, auth_token=_TEST_AUTH_TOKEN, storage_root=paths.storage_root).signature
        != configure_docker_worker_backend(
            changed_paths,
            auth_token=_TEST_AUTH_TOKEN,
            storage_root=changed_paths.storage_root,
        ).signature
    )

    replacement.ensure_worker(_spec(replacement, _TEST_UNSCOPED_WORKER_KEY), now=20.0)

    assert container.removed == 1
    assert len(client.containers.run_calls) == 2
    latest = client.containers.run_calls[-1]
    if initial_policy == "computer":
        assert "cap_drop" not in latest
        assert "security_opt" not in latest
    else:
        assert latest["cap_drop"] == ["ALL"]
        assert latest["security_opt"][0] == "no-new-privileges:true"


@pytest.mark.parametrize("policy", [None, "runtime_default"])
@pytest.mark.parametrize(("primary_flag", "worker_flag"), [("true", None), ("false", " YES "), ("true", "false")])
def test_docker_computer_requires_explicit_security_policy(
    tmp_path: Path,
    policy: str | None,
    primary_flag: str,
    worker_flag: str | None,
) -> None:
    """Enabling Computer cannot silently select or bypass the operator security policy."""
    env = {"MINDROOM_DOCKER_WORKER_IMAGE": "test-image", WORKER_COMPUTER_ENABLED_ENV: primary_flag}
    if worker_flag is not None:
        env["MINDROOM_DOCKER_WORKER_ENV_JSON"] = json.dumps({WORKER_COMPUTER_ENABLED_ENV: worker_flag})
    if policy is not None:
        env["MINDROOM_DOCKER_WORKER_SECURITY_POLICY"] = policy
    paths = resolve_primary_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env=env)
    with pytest.raises(WorkerBackendError, match="MINDROOM_DOCKER_WORKER_SECURITY_POLICY=computer"):
        DockerWorkerBackendConfig.from_runtime(paths)


@pytest.mark.parametrize("direct", [False, True])
def test_docker_security_policy_rejects_unknown_choice(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    direct: bool,
) -> None:
    """Neither env parsing nor supplied config can bypass the bounded policy choices."""
    backend, _, _ = _backend(monkeypatch, tmp_path)
    paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={
            "MINDROOM_DOCKER_WORKER_IMAGE": "test-image",
            "MINDROOM_DOCKER_WORKER_SECURITY_POLICY": "unconfined",
        },
    )
    if direct:
        with pytest.raises(WorkerBackendError, match="MINDROOM_DOCKER_WORKER_SECURITY_POLICY"):
            replace(backend.config, security_policy="unconfined")
    else:
        with pytest.raises(WorkerBackendError, match="MINDROOM_DOCKER_WORKER_SECURITY_POLICY"):
            DockerWorkerBackendConfig.from_runtime(paths)


@pytest.mark.parametrize("worker_override", [False, True])
def test_docker_direct_backend_rejects_incompatible_computer_policy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    worker_override: bool,
) -> None:
    """A supplied default config cannot enable Computer even when env claims a compatible policy."""
    backend, _, _ = _backend(monkeypatch, tmp_path)
    paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={
            WORKER_COMPUTER_ENABLED_ENV: "false" if worker_override else "true",
            "MINDROOM_DOCKER_WORKER_SECURITY_POLICY": "computer",
        },
    )
    config = (
        replace(backend.config, extra_env={WORKER_COMPUTER_ENABLED_ENV: "true"}) if worker_override else backend.config
    )

    def forbidden_client(**_kwargs: object) -> None:
        pytest.fail("Incompatible policy reached Docker client construction")

    monkeypatch.setattr("mindroom.workers.backends.docker._load_docker_client_and_errors", forbidden_client)
    with pytest.raises(WorkerBackendError, match="MINDROOM_DOCKER_WORKER_SECURITY_POLICY=computer"):
        _docker_backend(config=config, auth_token=_TEST_AUTH_TOKEN, storage_root=tmp_path, runtime_paths=paths)


@pytest.mark.parametrize(
    ("computer_enabled", "worker_flag", "policy", "expected"),
    [
        (False, None, None, "runtime_default"),
        (False, None, "runtime_default", "runtime_default"),
        (False, None, "computer", "computer"),
        (True, None, "computer", "computer"),
        (False, "false", None, "runtime_default"),
        (False, "true", "computer", "computer"),
    ],
)
def test_docker_security_policy_resolution(
    tmp_path: Path,
    *,
    computer_enabled: bool,
    worker_flag: str | None,
    policy: str | None,
    expected: str,
) -> None:
    """Availability cannot override the explicit pool policy or its historical default."""
    env = {
        "MINDROOM_DOCKER_WORKER_IMAGE": "test-image",
        "MINDROOM_DOCKER_WORKER_USER": "1000:1000",
        WORKER_COMPUTER_ENABLED_ENV: str(computer_enabled).lower(),
    }
    if worker_flag is not None:
        env["MINDROOM_DOCKER_WORKER_ENV_JSON"] = json.dumps({WORKER_COMPUTER_ENABLED_ENV: worker_flag})
    if policy is not None:
        env["MINDROOM_DOCKER_WORKER_SECURITY_POLICY"] = policy
    paths = resolve_primary_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env=env)
    assert DockerWorkerBackendConfig.from_runtime(paths).security_policy == expected


@pytest.mark.parametrize("user", ["0", "0:0", "000:1000", "+0:0", "-0:0", "root", "root:users"])
@pytest.mark.parametrize("worker_override", [False, True])
def test_docker_computer_rejects_root_user(tmp_path: Path, user: str, *, worker_override: bool) -> None:
    """Known root identities fail configuration before sandboxed Chromium starts."""
    env = {
        "MINDROOM_DOCKER_WORKER_IMAGE": "test-image",
        "MINDROOM_DOCKER_WORKER_SECURITY_POLICY": "computer",
        "MINDROOM_DOCKER_WORKER_USER": user,
        WORKER_COMPUTER_ENABLED_ENV: "false" if worker_override else "true",
    }
    if worker_override:
        env["MINDROOM_DOCKER_WORKER_ENV_JSON"] = json.dumps({WORKER_COMPUTER_ENABLED_ENV: "true"})
    paths = resolve_primary_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env=env)
    with pytest.raises(WorkerBackendError, match="non-root"):
        DockerWorkerBackendConfig.from_runtime(paths)


def test_docker_computer_rejects_inherited_root_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The POSIX host-user default must pass the same Computer identity check."""
    monkeypatch.setattr("mindroom.workers.backends.docker_config.os.getuid", lambda: 0)
    env = {
        "MINDROOM_DOCKER_WORKER_IMAGE": "test-image",
        "MINDROOM_DOCKER_WORKER_SECURITY_POLICY": "computer",
        WORKER_COMPUTER_ENABLED_ENV: "true",
    }
    paths = resolve_primary_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env=env)
    with pytest.raises(WorkerBackendError, match="non-root"):
        DockerWorkerBackendConfig.from_runtime(paths)


@pytest.mark.parametrize("user", ["0:0", "root"])
def test_docker_direct_computer_rejects_root_before_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    user: str,
) -> None:
    """Supplied configuration cannot skip the user check at backend construction."""
    backend, _, _ = _backend(monkeypatch, tmp_path)
    paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={WORKER_COMPUTER_ENABLED_ENV: "true"},
    )
    config = replace(backend.config, user=user, security_policy="computer")

    def forbidden_client(**_kwargs: object) -> None:
        pytest.fail("Root Computer configuration reached Docker client construction")

    monkeypatch.setattr("mindroom.workers.backends.docker._load_docker_client_and_errors", forbidden_client)
    with pytest.raises(WorkerBackendError, match="non-root"):
        _docker_backend(config=config, auth_token=_TEST_AUTH_TOKEN, storage_root=tmp_path, runtime_paths=paths)


@pytest.mark.parametrize(
    ("enabled", "policy", "user"),
    [
        (False, "runtime_default", "root"),
        (False, "computer", "0:0"),
        (True, "computer", "1000:0"),
        (True, "computer", "mindroom:users"),
        (True, "computer", ""),
    ],
)
def test_docker_computer_identity_validation_preserves_supported_users(
    tmp_path: Path,
    *,
    enabled: bool,
    policy: str,
    user: str,
) -> None:
    """Ordinary workers, nonroot identities and the image default remain configurable."""
    paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={
            "MINDROOM_DOCKER_WORKER_IMAGE": "test-image",
            "MINDROOM_DOCKER_WORKER_SECURITY_POLICY": policy,
            "MINDROOM_DOCKER_WORKER_USER": user,
            WORKER_COMPUTER_ENABLED_ENV: str(enabled).lower(),
        },
    )
    assert DockerWorkerBackendConfig.from_runtime(paths).user == (user or None)


def _shared_worker_key(agent_name: str) -> str:
    worker_key = resolve_worker_key(
        "shared",
        ToolExecutionIdentity(
            channel="matrix",
            agent_name=agent_name,
            requester_id="@alice:example.org",
            room_id="!room:example.org",
            thread_id=None,
            resolved_thread_id=None,
            session_id=None,
            tenant_id="tenant-123",
        ),
        agent_name=agent_name,
    )
    assert worker_key is not None
    return worker_key


def test_docker_workers_mount_shared_directories_for_owner_and_members(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The owner's worker mounts its shared directory read-write and an invited agent's worker read-only."""
    config_text, _projected_paths = _multi_agent_projected_config_fixture(tmp_path)
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, config_text=config_text)
    create_shared_directory(backend._runtime_paths, owner="alpha", name="report")
    add_shared_directory_member(backend._runtime_paths, owner="alpha", name="report", member="beta")
    share = str((tmp_path / "shared_directories" / "alpha" / "report").resolve())

    backend.ensure_worker(_spec(backend, _shared_worker_key("alpha")), now=10.0)
    backend.ensure_worker(_spec(backend, _shared_worker_key("beta")), now=10.0)

    alpha_volumes, beta_volumes = (
        _volumes_by_source(run_call["volumes"]) for run_call in fake_client.containers.run_calls
    )
    assert alpha_volumes[share] == {"bind": "/app/worker/shared_directories/alpha/report", "mode": "rw"}
    assert beta_volumes[share] == {"bind": "/app/worker/shared_directories/alpha/report", "mode": "ro"}


def test_docker_worker_restarts_after_an_invitation_changes_its_mounts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An invitation reaches a running worker by recreating it on its next use."""
    config_text, _projected_paths = _multi_agent_projected_config_fixture(tmp_path)
    backend, fake_client, _sync_calls = _backend(monkeypatch, tmp_path, config_text=config_text)
    create_shared_directory(backend._runtime_paths, owner="alpha", name="report")
    handle = backend.ensure_worker(_spec(backend, _shared_worker_key("beta")), now=10.0)
    existing_container = fake_client.containers.by_name[handle.worker_id]

    backend.ensure_worker(_spec(backend, _shared_worker_key("beta")), now=20.0)
    assert fake_client.containers.by_name[handle.worker_id] is existing_container

    add_shared_directory_member(backend._runtime_paths, owner="alpha", name="report", member="beta")
    backend.ensure_worker(_spec(backend, _shared_worker_key("beta")), now=30.0)

    assert existing_container.removed == 1
    assert len(fake_client.containers.run_calls) == 2
