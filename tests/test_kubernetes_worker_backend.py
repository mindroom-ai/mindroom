"""Tests for the Kubernetes worker backend."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from types import MethodType, SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from mindroom.constants import deserialize_runtime_paths, resolve_primary_runtime_paths
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    _private_instance_state_root_path,
    resolve_unscoped_worker_key,
    resolve_worker_key,
    worker_dir_name,
)
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.backends.kubernetes import KubernetesWorkerBackend, _KubernetesWorkerBackendConfig
from mindroom.workers.models import WorkerSpec
from mindroom.workers.runtime import primary_worker_backend_available, primary_worker_backend_name

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths

_TEST_TOKEN_SECRET_NAME = "mindroom-secrets"  # noqa: S105
_TEST_TOKEN_SECRET_KEY = "sandbox_proxy_token"  # noqa: S105
_TEST_AUTH_TOKEN = "test-token"  # noqa: S105
_TEST_SCOPED_WORKER_KEY_A = "v1:tenant-123:shared:code"
_TEST_SCOPED_WORKER_KEY_B = "v1:tenant-123:shared:research"


class _FakeApiError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


_MAPPING_KEYS = {"annotations", "labels", "matchLabels", "selector"}


def _to_namespace(value: object, *, key: str | None = None) -> object:
    if isinstance(value, dict):
        if key in _MAPPING_KEYS:
            return deepcopy(value)
        return SimpleNamespace(**{item_key: _to_namespace(item, key=item_key) for item_key, item in value.items()})
    if isinstance(value, list):
        return [_to_namespace(item) for item in value]
    return value


class _FakeAppsApi:
    def __init__(self) -> None:
        self.deployments: dict[str, object] = {}
        self.created_bodies: list[dict[str, object]] = []
        self.patched_bodies: list[tuple[str, dict[str, object]]] = []
        self.deleted_names: list[str] = []
        self.list_label_selectors: list[str] = []
        self.delete_read_lag_by_name: dict[str, int] = {}
        self._active_delete_read_lag_by_name: dict[str, int] = {}

    def read_namespaced_deployment(self, name: str, namespace: str) -> object:
        _ = namespace
        deployment = self.deployments.get(name)
        if deployment is None:
            raise _FakeApiError(404)
        remaining_delete_reads = self._active_delete_read_lag_by_name.get(name)
        if remaining_delete_reads is not None:
            if remaining_delete_reads <= 0:
                self._active_delete_read_lag_by_name.pop(name, None)
                self.deployments.pop(name, None)
                raise _FakeApiError(404)
            self._active_delete_read_lag_by_name[name] = remaining_delete_reads - 1
        return deployment

    def create_namespaced_deployment(self, namespace: str, body: dict[str, object]) -> object:
        _ = namespace
        self.created_bodies.append(body)
        deployment = _to_namespace(body)
        deployment.metadata.generation = 1
        deployment.metadata.uid = f"{deployment.metadata.name}-uid"
        deployment.status = SimpleNamespace(ready_replicas=body["spec"]["replicas"], observed_generation=1)
        self._active_delete_read_lag_by_name.pop(deployment.metadata.name, None)
        self.deployments[deployment.metadata.name] = deployment
        return deployment

    def patch_namespaced_deployment(self, name: str, namespace: str, body: dict[str, object]) -> object:
        _ = namespace
        self.patched_bodies.append((name, body))
        deployment = self.read_namespaced_deployment(name, namespace)
        metadata = body.get("metadata")
        if isinstance(metadata, dict):
            annotations = metadata.get("annotations")
            if isinstance(annotations, dict):
                deployment.metadata.annotations = annotations
        spec = body.get("spec")
        if isinstance(spec, dict) and "replicas" in spec:
            deployment.spec.replicas = spec["replicas"]
            deployment.status.ready_replicas = spec["replicas"]
        deployment.metadata.generation += 1
        deployment.status.observed_generation = deployment.metadata.generation
        return deployment

    def delete_namespaced_deployment(self, name: str, namespace: str) -> None:
        _ = namespace
        self.deleted_names.append(name)
        if self.delete_read_lag_by_name.get(name, 0) > 0:
            self._active_delete_read_lag_by_name[name] = self.delete_read_lag_by_name[name]
            return
        self.deployments.pop(name, None)

    def list_namespaced_deployment(self, namespace: str, label_selector: str) -> object:
        _ = namespace
        self.list_label_selectors.append(label_selector)
        selectors = {}
        for expression in filter(None, (part.strip() for part in label_selector.split(","))):
            key, sep, value = expression.partition("=")
            if not sep:
                continue
            selectors[key] = value

        def matches_selector(deployment: object) -> bool:
            labels = deployment.metadata.labels
            return all(labels.get(key) == value for key, value in selectors.items())

        return SimpleNamespace(
            items=[deployment for deployment in self.deployments.values() if matches_selector(deployment)],
        )


class _FakeCoreApi:
    def __init__(self) -> None:
        self.services: dict[str, object] = {}
        self.pods: dict[str, object] = {}
        self.created_bodies: list[dict[str, object]] = []

    def read_namespaced_service(self, name: str, namespace: str) -> object:
        _ = namespace
        service = self.services.get(name)
        if service is None:
            raise _FakeApiError(404)
        return service

    def create_namespaced_service(self, namespace: str, body: dict[str, object]) -> object:
        _ = namespace
        self.created_bodies.append(body)
        service = _to_namespace(body)
        self.services[service.metadata.name] = service
        return service

    def patch_namespaced_service(self, name: str, namespace: str, body: dict[str, object]) -> object:
        _ = namespace
        service = _to_namespace(body)
        self.services[name] = service
        return service

    def delete_namespaced_service(self, name: str, namespace: str) -> None:
        _ = namespace
        self.services.pop(name, None)

    def read_namespaced_pod(self, name: str, namespace: str) -> object:
        _ = namespace
        pod = self.pods.get(name)
        if pod is None:
            raise _FakeApiError(404)
        return pod


def _backend(
    *,
    idle_timeout_seconds: float = 60.0,
    worker_port: int = 8766,
    storage_subpath_prefix: str = "workers",
    storage_mount_path: str = "/app/worker",
    config_map_name: str | None = "mindroom-config",
    node_name: str | None = None,
    colocate_with_control_plane_node: bool = False,
    name_prefix: str = "mindroom-worker",
    owner_deployment_name: str | None = None,
    runtime_paths: RuntimePaths | None = None,
) -> tuple[KubernetesWorkerBackend, _FakeAppsApi, _FakeCoreApi]:
    config = _KubernetesWorkerBackendConfig(
        namespace="chat",
        image="ghcr.io/mindroom-ai/mindroom:latest",
        image_pull_policy="IfNotPresent",
        worker_port=worker_port,
        service_account_name="mindroom-worker",
        storage_pvc_name="mindroom-storage",
        storage_mount_path=storage_mount_path,
        storage_subpath_prefix=storage_subpath_prefix,
        config_map_name=config_map_name,
        config_key="config.yaml",
        config_path="/app/config.yaml",
        token_secret_name=_TEST_TOKEN_SECRET_NAME,
        token_secret_key=_TEST_TOKEN_SECRET_KEY,
        idle_timeout_seconds=idle_timeout_seconds,
        ready_timeout_seconds=5.0,
        name_prefix=name_prefix,
        node_name=node_name,
        colocate_with_control_plane_node=colocate_with_control_plane_node,
        extra_env={},
        extra_labels={"mindroom.ai/tenant": "test"},
        owner_deployment_name=owner_deployment_name,
    )
    resolved_runtime_paths = runtime_paths or resolve_primary_runtime_paths(
        config_path=Path("config.yaml"),
        storage_path=Path("mindroom-test-storage").resolve(),
    )
    backend = KubernetesWorkerBackend(
        runtime_paths=resolved_runtime_paths,
        config=config,
        auth_token=_TEST_AUTH_TOKEN,
        storage_root=resolved_runtime_paths.storage_root,
    )
    apps_api = _FakeAppsApi()
    core_api = _FakeCoreApi()
    backend._resources.apps_api = apps_api
    backend._resources.core_api = core_api
    backend._resources.api_exception_cls = _FakeApiError
    if owner_deployment_name is not None:
        apps_api.deployments[owner_deployment_name] = SimpleNamespace(
            metadata=SimpleNamespace(
                name=owner_deployment_name,
                annotations={},
                labels={},
                generation=1,
                uid=f"{owner_deployment_name}-uid",
            ),
            spec=SimpleNamespace(replicas=1),
            status=SimpleNamespace(ready_replicas=1, observed_generation=1),
        )
    return backend, apps_api, core_api


def test_kubernetes_backend_ensures_worker_service_and_deployment() -> None:
    """Ensuring one worker should create a service/deployment pair on shared storage."""
    backend, apps_api, core_api = _backend(owner_deployment_name="mindroom-demo")
    worker_key = _TEST_SCOPED_WORKER_KEY_A

    handle = backend.ensure_worker(WorkerSpec(worker_key), now=10.0)

    assert handle.worker_key == worker_key
    assert handle.backend_name == "kubernetes"
    assert handle.endpoint.endswith("/api/sandbox-runner/execute")
    assert handle.debug_metadata["namespace"] == "chat"
    assert handle.debug_metadata["state_subpath"] == f"workers/{worker_dir_name(worker_key)}"
    assert handle.debug_metadata["service_name"] == handle.worker_id
    assert handle.status == "ready"

    assert len(core_api.created_bodies) == 1
    assert len(apps_api.created_bodies) == 1
    deployment = apps_api.created_bodies[0]
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    env_values = {env["name"]: env.get("value") for env in container["env"]}
    env_names = set(env_values)
    assert "MINDROOM_SANDBOX_DEDICATED_WORKER_KEY" in env_names
    assert "MINDROOM_SANDBOX_DEDICATED_WORKER_ROOT" in env_names
    assert "MINDROOM_STORAGE_PATH" in env_names
    assert "MINDROOM_RUNTIME_PATHS_JSON" in env_names
    assert "MINDROOM_SANDBOX_SHARED_STORAGE_ROOT" in env_names
    assert "VIRTUAL_ENV" in env_names
    assert "PATH" in env_names
    assert "MINDROOM_SHARED_CREDENTIALS_PATH" in env_names
    assert "MINDROOM_SANDBOX_PROXY_TOKEN" in env_names
    assert env_values["MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE"] == "subprocess"
    assert env_values["MINDROOM_SANDBOX_RUNNER_PORT"] == "8766"
    expected_dedicated_root = f"/app/worker/workers/{worker_dir_name(worker_key)}"
    committed_runtime = deserialize_runtime_paths(json.loads(env_values["MINDROOM_RUNTIME_PATHS_JSON"]))
    assert env_values["MINDROOM_STORAGE_PATH"] == expected_dedicated_root
    assert env_values["MINDROOM_SANDBOX_DEDICATED_WORKER_ROOT"] == expected_dedicated_root
    assert env_values["MINDROOM_SANDBOX_SHARED_STORAGE_ROOT"] == "/app/worker"
    assert env_values["HOME"] == expected_dedicated_root
    assert env_values["VIRTUAL_ENV"] == f"{expected_dedicated_root}/venv"
    assert env_values["PATH"].startswith(f"{expected_dedicated_root}/venv/bin:")
    assert env_values["MINDROOM_SHARED_CREDENTIALS_PATH"] == f"{expected_dedicated_root}/.shared_credentials"
    assert committed_runtime.storage_root == Path(expected_dedicated_root)
    assert committed_runtime.env_value("MINDROOM_SANDBOX_DEDICATED_WORKER_KEY") == worker_key
    assert committed_runtime.env_value("MINDROOM_SANDBOX_DEDICATED_WORKER_ROOT") == expected_dedicated_root
    assert (
        deployment["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] == "mindroom-storage"
    )
    assert deployment["metadata"]["labels"]["mindroom.ai/tenant"] == "test"
    assert deployment["metadata"]["ownerReferences"] == [
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "name": "mindroom-demo",
            "uid": "mindroom-demo-uid",
            "controller": False,
            "blockOwnerDeletion": False,
        },
    ]
    assert core_api.created_bodies[0]["metadata"]["ownerReferences"] == [
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "name": "mindroom-demo",
            "uid": "mindroom-demo-uid",
            "controller": False,
            "blockOwnerDeletion": False,
        },
    ]
    assert "annotations" not in deployment["spec"]["template"]["metadata"]
    assert deployment["spec"]["template"]["spec"]["securityContext"] == {
        "runAsUser": 1000,
        "runAsGroup": 1000,
        "fsGroup": 1000,
        "runAsNonRoot": True,
        "fsGroupChangePolicy": "OnRootMismatch",
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    assert container["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
    }
    assert container["resources"]["requests"] == {"memory": "256Mi", "cpu": "100m"}
    assert container["resources"]["limits"] == {"memory": "1Gi", "cpu": "500m"}
    assert container["startupProbe"] == {
        "httpGet": {"path": "/healthz", "port": "api"},
        "periodSeconds": 5,
        "failureThreshold": 60,
    }


def test_kubernetes_backend_commits_parent_runtime_env_into_worker_payload(tmp_path: Path) -> None:
    """Dedicated worker startup payloads should preserve non-secret runtime settings only."""
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.yaml"
    credentials_path = tmp_path / "google-credentials.json"
    credentials_path.write_text('{"type":"service_account"}\n', encoding="utf-8")
    storage_mount_path = tmp_path / "worker-storage"
    storage_mount_path.mkdir()
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    (config_dir / ".env").write_text(
        (
            "MINDROOM_NAMESPACE=alpha1234\n"
            "MATRIX_HOMESERVER=http://dotenv-hs\n"
            "MATRIX_SERVER_NAME=alpha.example\n"
            f"GOOGLE_APPLICATION_CREDENTIALS={credentials_path}\n"
            "GOOGLE_CLOUD_PROJECT=demo-project\n"
            "GOOGLE_CLOUD_LOCATION=us-central1\n"
            "ANTHROPIC_API_KEY=sk-secret\n"
        ),
        encoding="utf-8",
    )
    runtime_paths = resolve_primary_runtime_paths(
        config_path=config_path,
        process_env={
            "MINDROOM_SANDBOX_PROXY_TOKEN": "test-token",
            "MINDROOM_LOCAL_CLIENT_SECRET": "client-secret",
        },
    )
    backend, apps_api, _core_api = _backend(
        runtime_paths=runtime_paths,
        storage_mount_path=str(storage_mount_path),
    )

    backend.ensure_worker(WorkerSpec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    deployment = apps_api.created_bodies[0]
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    env_values = {env["name"]: env.get("value") for env in container["env"]}
    committed_runtime = deserialize_runtime_paths(json.loads(env_values["MINDROOM_RUNTIME_PATHS_JSON"]))
    state_subpath = Path("workers") / worker_dir_name(_TEST_SCOPED_WORKER_KEY_A)
    expected_worker_root = Path(env_values["MINDROOM_STORAGE_PATH"])
    expected_credentials_path = expected_worker_root / ".runtime" / credentials_path.name
    local_credentials_path = runtime_paths.storage_root / state_subpath / ".runtime" / credentials_path.name

    assert committed_runtime.env_value("MINDROOM_NAMESPACE") == "alpha1234"
    assert committed_runtime.env_value("MATRIX_HOMESERVER") == "http://dotenv-hs"
    assert committed_runtime.env_value("MATRIX_SERVER_NAME") == "alpha.example"
    assert committed_runtime.env_value("GOOGLE_APPLICATION_CREDENTIALS") == str(expected_credentials_path)
    assert committed_runtime.env_value("GOOGLE_CLOUD_PROJECT") == "demo-project"
    assert committed_runtime.env_value("GOOGLE_CLOUD_LOCATION") == "us-central1"
    assert committed_runtime.env_value("ANTHROPIC_API_KEY") is None
    assert committed_runtime.env_value("MINDROOM_SANDBOX_PROXY_TOKEN") is None
    assert committed_runtime.env_value("MINDROOM_LOCAL_CLIENT_SECRET") is None
    assert local_credentials_path.read_text(encoding="utf-8") == '{"type":"service_account"}\n'


def test_kubernetes_backend_drops_host_local_adc_path_when_not_mounted(tmp_path: Path) -> None:
    """Dedicated worker payloads must not serialize unusable host-local ADC paths."""
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_primary_runtime_paths(
        config_path=config_path,
        process_env={"GOOGLE_APPLICATION_CREDENTIALS": "/host/path/adc.json"},
    )
    backend, apps_api, _core_api = _backend(
        runtime_paths=runtime_paths,
        storage_mount_path=str(tmp_path / "not-mounted-storage"),
    )

    backend.ensure_worker(WorkerSpec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    deployment = apps_api.created_bodies[0]
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    env_values = {env["name"]: env.get("value") for env in container["env"]}
    committed_runtime = deserialize_runtime_paths(json.loads(env_values["MINDROOM_RUNTIME_PATHS_JSON"]))

    assert committed_runtime.env_value("GOOGLE_APPLICATION_CREDENTIALS") is None


def test_kubernetes_backend_drops_host_local_generic_file_secret_when_not_mounted(tmp_path: Path) -> None:
    """Dedicated worker payloads must not retain unusable host-local generic *_FILE secrets."""
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_primary_runtime_paths(
        config_path=config_path,
        process_env={"OPENAI_API_KEY_FILE": "/host/path/openai.key"},
    )
    backend, apps_api, _core_api = _backend(
        runtime_paths=runtime_paths,
        storage_mount_path=str(tmp_path / "not-mounted-storage"),
    )

    backend.ensure_worker(WorkerSpec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    deployment = apps_api.created_bodies[0]
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    env_values = {env["name"]: env.get("value") for env in container["env"]}
    committed_runtime = deserialize_runtime_paths(json.loads(env_values["MINDROOM_RUNTIME_PATHS_JSON"]))

    assert committed_runtime.env_value("OPENAI_API_KEY_FILE") is None
    assert "OPENAI_API_KEY_FILE" not in committed_runtime.env_file_values


def test_kubernetes_backend_commits_relative_file_backed_secrets_into_worker_payload(tmp_path: Path) -> None:
    """Dedicated Kubernetes workers should preserve relative *_FILE secrets by copying them into worker state."""
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    secret_path = config_dir / "secrets" / "openai.key"
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    secret_path.write_text("sk-relative\n", encoding="utf-8")
    storage_mount_path = tmp_path / "worker-storage"
    storage_mount_path.mkdir()
    (config_dir / ".env").write_text("OPENAI_API_KEY_FILE=secrets/openai.key\n", encoding="utf-8")
    runtime_paths = resolve_primary_runtime_paths(config_path=config_path)
    backend, apps_api, _core_api = _backend(
        runtime_paths=runtime_paths,
        storage_mount_path=str(storage_mount_path),
    )

    backend.ensure_worker(WorkerSpec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    deployment = apps_api.created_bodies[0]
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    env_values = {env["name"]: env.get("value") for env in container["env"]}
    committed_runtime = deserialize_runtime_paths(json.loads(env_values["MINDROOM_RUNTIME_PATHS_JSON"]))
    expected_worker_root = Path(env_values["MINDROOM_STORAGE_PATH"])
    local_secret_copy = (
        runtime_paths.storage_root
        / "workers"
        / worker_dir_name(_TEST_SCOPED_WORKER_KEY_A)
        / ".runtime"
        / "file-secrets"
        / "OPENAI_API_KEY_FILE"
        / "openai.key"
    )

    assert committed_runtime.env_value("OPENAI_API_KEY_FILE") == str(
        expected_worker_root / ".runtime" / "file-secrets" / "OPENAI_API_KEY_FILE" / "openai.key",
    )
    assert local_secret_copy.read_text(encoding="utf-8") == "sk-relative\n"


def test_kubernetes_backend_maps_adc_path_through_local_storage_root_when_mount_paths_differ(tmp_path: Path) -> None:
    """Dedicated workers should copy ADC into the local shared storage root even when pod mount paths differ."""
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    credentials_path = tmp_path / "adc.json"
    credentials_path.write_text('{"type":"service_account"}\n', encoding="utf-8")
    local_storage_root = tmp_path / "local-shared-storage"
    local_storage_root.mkdir()
    runtime_paths = resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=local_storage_root,
        process_env={"GOOGLE_APPLICATION_CREDENTIALS": str(credentials_path)},
    )
    backend, apps_api, _core_api = _backend(
        runtime_paths=runtime_paths,
        storage_mount_path="/app/worker",
    )

    backend.ensure_worker(WorkerSpec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    deployment = apps_api.created_bodies[0]
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    env_values = {env["name"]: env.get("value") for env in container["env"]}
    committed_runtime = deserialize_runtime_paths(json.loads(env_values["MINDROOM_RUNTIME_PATHS_JSON"]))
    state_subpath = Path("workers") / worker_dir_name(_TEST_SCOPED_WORKER_KEY_A)
    local_adc_copy = local_storage_root / state_subpath / ".runtime" / credentials_path.name

    assert (
        committed_runtime.env_value("GOOGLE_APPLICATION_CREDENTIALS")
        == f"/app/worker/{state_subpath}/.runtime/{credentials_path.name}"
    )
    assert local_adc_copy.read_text(encoding="utf-8") == '{"type":"service_account"}\n'


def test_kubernetes_backend_rejects_symlinked_google_application_credentials_path(tmp_path: Path) -> None:
    """Explicit ADC handoff should reject symlinked host paths for consistency with other worker copies."""
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    real_credentials_path = tmp_path / "real-adc.json"
    real_credentials_path.write_text('{"type":"service_account"}\n', encoding="utf-8")
    symlinked_credentials_path = tmp_path / "adc-link.json"
    symlinked_credentials_path.symlink_to(real_credentials_path)
    runtime_paths = resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "local-shared-storage",
        process_env={"GOOGLE_APPLICATION_CREDENTIALS": str(symlinked_credentials_path)},
    )
    backend, _apps_api, _core_api = _backend(runtime_paths=runtime_paths)

    with pytest.raises(
        WorkerBackendError,
        match="Kubernetes worker GOOGLE_APPLICATION_CREDENTIALS must not contain symlinks",
    ):
        backend.ensure_worker(WorkerSpec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)


def test_kubernetes_backend_rejects_symlinked_google_application_credentials_destination(
    tmp_path: Path,
) -> None:
    """Explicit ADC handoff should reject worker-owned destination symlinks."""
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    credentials_path = tmp_path / "google-credentials.json"
    credentials_path.write_text('{"type":"service_account"}\n', encoding="utf-8")
    local_storage_root = (tmp_path / "local-shared-storage").resolve()
    local_storage_root.mkdir()
    victim_path = tmp_path / "victim.txt"
    victim_path.write_text("victim\n", encoding="utf-8")
    destination_path = (
        local_storage_root / "workers" / worker_dir_name(_TEST_SCOPED_WORKER_KEY_A) / ".runtime" / credentials_path.name
    )
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    destination_path.symlink_to(victim_path)
    runtime_paths = resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=local_storage_root,
        process_env={"GOOGLE_APPLICATION_CREDENTIALS": str(credentials_path)},
    )
    backend, _apps_api, _core_api = _backend(runtime_paths=runtime_paths)

    with pytest.raises(
        WorkerBackendError,
        match="Kubernetes worker GOOGLE_APPLICATION_CREDENTIALS destination must stay within the worker state root",
    ):
        backend.ensure_worker(WorkerSpec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    assert victim_path.read_text(encoding="utf-8") == "victim\n"


def test_kubernetes_backend_preserves_primary_config_path_without_configmap(tmp_path: Path) -> None:
    """Dedicated worker payloads should keep the primary runtime config path when no ConfigMap is mounted."""
    config_path = tmp_path / "workspace-config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_primary_runtime_paths(config_path=config_path, storage_path=tmp_path / "storage")
    backend, apps_api, _core_api = _backend(runtime_paths=runtime_paths, config_map_name=None)

    backend.ensure_worker(WorkerSpec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    deployment = apps_api.created_bodies[0]
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    env_values = {env["name"]: env.get("value") for env in container["env"]}
    committed_runtime = deserialize_runtime_paths(json.loads(env_values["MINDROOM_RUNTIME_PATHS_JSON"]))

    assert committed_runtime.config_path == config_path.resolve()


def test_primary_worker_backend_available_uses_runtime_env_values(tmp_path: Path) -> None:
    """Kubernetes backend availability should honor the explicit runtime context."""
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    (config_dir / ".env").write_text(
        (
            "MINDROOM_WORKER_BACKEND=kubernetes\n"
            "MINDROOM_KUBERNETES_WORKER_IMAGE=test-image\n"
            "MINDROOM_KUBERNETES_WORKER_STORAGE_PVC_NAME=test-pvc\n"
            "MINDROOM_SANDBOX_PROXY_TOKEN=test-token\n"
        ),
        encoding="utf-8",
    )
    runtime_paths = resolve_primary_runtime_paths(config_path=config_path)

    assert primary_worker_backend_name(runtime_paths) == "kubernetes"
    assert runtime_paths.env_value("MINDROOM_KUBERNETES_WORKER_IMAGE") == "test-image"
    assert primary_worker_backend_available(
        runtime_paths,
        proxy_url=None,
        proxy_token=runtime_paths.env_value("MINDROOM_SANDBOX_PROXY_TOKEN"),
    )


def test_kubernetes_backend_rejects_unknown_worker_keys_for_scoped_mounts() -> None:
    """Malformed worker keys must not fall back to mounting the whole storage root."""
    backend, _apps_api, _core_api = _backend()

    with pytest.raises(WorkerBackendError, match="Unsupported worker key"):
        backend.ensure_worker(WorkerSpec("legacy-worker"), now=10.0)


def test_kubernetes_backend_requires_configured_owner_deployment_to_exist() -> None:
    """Configured owner deployments should fail closed when they cannot be resolved."""
    backend, _apps_api, _core_api = _backend(owner_deployment_name="mindroom-missing")
    assert isinstance(backend._resources.apps_api, _FakeAppsApi)
    backend._resources.apps_api.deployments.pop("mindroom-missing")

    with pytest.raises(WorkerBackendError, match="owner deployment 'mindroom-missing' was not found"):
        backend.ensure_worker(WorkerSpec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)


def test_kubernetes_backend_honors_custom_worker_port() -> None:
    """Dedicated workers should wire the configured port through env, service, and probes."""
    backend, apps_api, core_api = _backend(worker_port=9777)

    handle = backend.ensure_worker(WorkerSpec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    service = core_api.created_bodies[0]
    deployment = apps_api.created_bodies[0]
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    env_values = {env["name"]: env.get("value") for env in container["env"]}

    assert handle.endpoint == f"http://{handle.worker_id}.chat.svc.cluster.local:9777/api/sandbox-runner/execute"
    assert env_values["MINDROOM_SANDBOX_RUNNER_PORT"] == "9777"
    assert service["spec"]["ports"] == [{"name": "api", "port": 9777, "targetPort": 9777}]
    assert container["ports"] == [{"containerPort": 9777, "name": "api"}]
    assert container["readinessProbe"]["httpGet"]["port"] == "api"
    assert container["livenessProbe"]["httpGet"]["port"] == "api"


def test_kubernetes_backend_mounts_only_scoped_agent_root_for_shared_workers() -> None:
    """Shared-scope dedicated workers should mount only their agent root, not the whole agents tree."""
    backend, apps_api, _core_api = _backend()
    worker_key = "v1:tenant-123:shared:code"

    backend.ensure_worker(WorkerSpec(worker_key), now=10.0)

    deployment = apps_api.created_bodies[0]
    volume_mounts = deployment["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
    mount_paths = {mount["mountPath"]: mount.get("subPath") for mount in volume_mounts}
    expected_worker_root = f"/app/worker/workers/{worker_dir_name(worker_key)}"

    assert mount_paths["/app/worker/agents/code"] == "agents/code"
    assert mount_paths[expected_worker_root] == f"workers/{worker_dir_name(worker_key)}"
    assert "/app/worker/credentials" not in mount_paths
    assert "/app/worker/.shared_credentials" not in mount_paths
    assert "/app/worker/agents" not in mount_paths

    container = deployment["spec"]["template"]["spec"]["containers"][0]
    env_values = {env["name"]: env.get("value") for env in container["env"]}
    assert env_values["MINDROOM_STORAGE_PATH"] == expected_worker_root
    assert env_values["MINDROOM_SANDBOX_SHARED_STORAGE_ROOT"] == "/app/worker"
    assert env_values["MINDROOM_SHARED_CREDENTIALS_PATH"] == f"{expected_worker_root}/.shared_credentials"


def test_kubernetes_backend_keeps_shared_storage_root_for_custom_worker_prefix() -> None:
    """Custom worker prefixes should not change the shared storage root env."""
    backend, apps_api, _core_api = _backend(storage_subpath_prefix="sandbox-workers")
    worker_key = "v1:tenant-123:shared:code"

    backend.ensure_worker(WorkerSpec(worker_key), now=10.0)

    deployment = apps_api.created_bodies[0]
    env_values = {
        env["name"]: env.get("value") for env in deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    expected_worker_root = f"/app/worker/sandbox-workers/{worker_dir_name(worker_key)}"

    assert env_values["MINDROOM_STORAGE_PATH"] == expected_worker_root
    assert env_values["MINDROOM_SANDBOX_SHARED_STORAGE_ROOT"] == "/app/worker"


def test_kubernetes_backend_mounts_broad_agents_tree_for_user_scope() -> None:
    """User-scope workers should see shared agents plus their own private-instance namespace."""
    backend, apps_api, _core_api = _backend()
    worker_key = "v1:tenant-123:user:@alice:example.org"

    backend.ensure_worker(WorkerSpec(worker_key), now=10.0)

    deployment = apps_api.created_bodies[0]
    volume_mounts = deployment["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
    mount_paths = {mount["mountPath"]: mount.get("subPath") for mount in volume_mounts}
    expected_worker_root = f"/app/worker/workers/{worker_dir_name(worker_key)}"
    expected_private_root = f"/app/worker/private_instances/{worker_dir_name(worker_key)}"

    assert mount_paths["/app/worker/agents"] == "agents"
    assert mount_paths[expected_private_root] == f"private_instances/{worker_dir_name(worker_key)}"
    assert mount_paths[expected_worker_root] == f"workers/{worker_dir_name(worker_key)}"
    assert "/app/worker/credentials" not in mount_paths
    assert "/app/worker/.shared_credentials" not in mount_paths


def test_kubernetes_backend_user_agent_mounts_require_explicit_private_visibility(tmp_path: Path) -> None:
    """User-agent mounts should fail closed without explicit private visibility."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
    )
    backend, _apps_api, _core_api = _backend(runtime_paths=runtime_paths)
    worker_key = resolve_worker_key(
        "user_agent",
        ToolExecutionIdentity(
            channel="matrix",
            agent_name="mind",
            requester_id="@alice:example.org",
            room_id="!room:example.org",
            thread_id=None,
            resolved_thread_id=None,
            session_id=None,
            tenant_id="tenant-123",
        ),
        agent_name="mind",
    )

    with pytest.raises(WorkerBackendError, match="user_agent workers require explicit private-agent visibility"):
        backend.ensure_worker(WorkerSpec(worker_key), now=10.0)


def test_kubernetes_backend_user_agent_mounts_private_root_from_worker_spec() -> None:
    """User-agent workers should mount their private root from the explicit worker spec visibility."""
    backend, apps_api, _core_api = _backend()
    worker_key = resolve_worker_key(
        "user_agent",
        ToolExecutionIdentity(
            channel="matrix",
            agent_name="mind",
            requester_id="@alice:example.org",
            room_id="!room:example.org",
            thread_id=None,
            resolved_thread_id=None,
            session_id=None,
            tenant_id="tenant-123",
        ),
        agent_name="mind",
    )

    backend.ensure_worker(WorkerSpec(worker_key, private_agent_names=frozenset({"mind"})), now=10.0)

    deployment = apps_api.created_bodies[0]
    volume_mounts = deployment["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
    mount_paths = {mount["mountPath"]: mount.get("subPath") for mount in volume_mounts}
    expected_private_root = str(
        _private_instance_state_root_path(
            Path("/app/worker"),
            worker_key=worker_key,
            agent_name="mind",
        ),
    )
    expected_private_subpath = f"private_instances/{worker_dir_name(worker_key)}/mind"

    assert mount_paths[expected_private_root] == expected_private_subpath
    assert "/app/worker/agents/mind" not in mount_paths
    assert f"/app/worker/private_instances/{worker_dir_name(worker_key)}" not in mount_paths


def test_kubernetes_backend_rejects_private_user_agent_deployment_without_target_visibility(
    tmp_path: Path,
) -> None:
    """Private user-agent workers must fail closed until the targeted private agent is explicitly visible."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "agents:\n  mind:\n    private:\n      per: user_agent\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
    )
    backend, apps_api, _core_api = _backend(runtime_paths=runtime_paths)
    worker_key = resolve_worker_key(
        "user_agent",
        ToolExecutionIdentity(
            channel="matrix",
            agent_name="mind",
            requester_id="@alice:example.org",
            room_id="!room:example.org",
            thread_id=None,
            resolved_thread_id=None,
            session_id=None,
            tenant_id="tenant-123",
        ),
        agent_name="mind",
    )

    with pytest.raises(WorkerBackendError, match="missing from explicit private-agent visibility"):
        backend.ensure_worker(WorkerSpec(worker_key, private_agent_names=frozenset()), now=10.0)
    assert apps_api.created_bodies == []

    backend.ensure_worker(WorkerSpec(worker_key, private_agent_names=frozenset({"mind"})), now=20.0)

    assert len(apps_api.created_bodies) == 1
    created = apps_api.created_bodies[0]
    volume_mounts = created["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
    mount_paths = {mount["mountPath"]: mount.get("subPath") for mount in volume_mounts}
    expected_private_root = str(
        _private_instance_state_root_path(
            Path("/app/worker"),
            worker_key=worker_key,
            agent_name="mind",
        ),
    )
    expected_private_subpath = f"private_instances/{worker_dir_name(worker_key)}/mind"

    assert mount_paths[expected_private_root] == expected_private_subpath
    assert "/app/worker/agents/mind" not in mount_paths


def test_kubernetes_backend_waits_for_deployment_deletion_before_recreate() -> None:
    """Template-drift replacement should wait for actual deletion instead of patching a terminating Deployment."""
    backend, apps_api, _core_api = _backend()
    worker_key = resolve_worker_key(
        "user_agent",
        ToolExecutionIdentity(
            channel="matrix",
            agent_name="mind",
            requester_id="@alice:example.org",
            room_id="!room:example.org",
            thread_id=None,
            resolved_thread_id=None,
            session_id=None,
            tenant_id="tenant-123",
        ),
        agent_name="mind",
    )

    backend.ensure_worker(WorkerSpec(worker_key, private_agent_names=frozenset({"mind"})), now=10.0)
    worker_id = apps_api.created_bodies[0]["metadata"]["name"]
    apps_api.delete_read_lag_by_name[worker_id] = 1
    apps_api.deployments[worker_id].metadata.annotations["mindroom.ai/template-hash"] = "stale"

    backend.ensure_worker(WorkerSpec(worker_key, private_agent_names=frozenset({"mind"})), now=20.0)

    assert apps_api.deleted_names == [worker_id]
    assert len(apps_api.created_bodies) == 2
    recreated = apps_api.created_bodies[-1]
    volume_mounts = recreated["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
    mount_paths = {mount["mountPath"]: mount.get("subPath") for mount in volume_mounts}
    expected_private_root = str(
        _private_instance_state_root_path(
            Path("/app/worker"),
            worker_key=worker_key,
            agent_name="mind",
        ),
    )

    assert mount_paths[expected_private_root] == f"private_instances/{worker_dir_name(worker_key)}/mind"


def test_kubernetes_backend_mounts_only_scoped_agent_root_for_unscoped_workers() -> None:
    """Unscoped dedicated workers should mount only the addressed agent root."""
    backend, apps_api, _core_api = _backend()
    worker_key = resolve_unscoped_worker_key(agent_name="general")

    backend.ensure_worker(WorkerSpec(worker_key), now=10.0)

    deployment = apps_api.created_bodies[0]
    volume_mounts = deployment["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
    mount_paths = {mount["mountPath"]: mount.get("subPath") for mount in volume_mounts}
    expected_worker_root = f"/app/worker/workers/{worker_dir_name(worker_key)}"

    assert mount_paths["/app/worker/agents/general"] == "agents/general"
    assert mount_paths[expected_worker_root] == f"workers/{worker_dir_name(worker_key)}"
    assert "/app/worker/agents" not in mount_paths
    assert "/app/worker/credentials" not in mount_paths
    assert "/app/worker/.shared_credentials" not in mount_paths


def test_kubernetes_backend_seeds_ui_shared_credentials_for_unscoped_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unscoped dedicated workers should mirror shared UI credentials into their shared layer."""
    backend, _apps_api, _core_api = _backend()
    sync_calls: list[tuple[str, bool]] = []

    def _record_sync(
        worker_key: str,
        *,
        include_ui_credentials: bool,
        credentials_manager: object | None = None,
    ) -> None:
        del credentials_manager
        sync_calls.append((worker_key, include_ui_credentials))

    monkeypatch.setattr(
        "mindroom.workers.backends.kubernetes.sync_shared_credentials_to_worker",
        _record_sync,
    )

    backend.ensure_worker(WorkerSpec("v1:tenant-123:unscoped:general"), now=10.0)

    assert sync_calls == [("v1:tenant-123:unscoped:general", True)]


def test_kubernetes_backend_keeps_scoped_workers_on_env_only_shared_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scoped dedicated workers should mirror only env-backed shared credentials."""
    backend, _apps_api, _core_api = _backend()
    sync_calls: list[tuple[str, bool]] = []

    def _record_sync(
        worker_key: str,
        *,
        include_ui_credentials: bool,
        credentials_manager: object | None = None,
    ) -> None:
        del credentials_manager
        sync_calls.append((worker_key, include_ui_credentials))

    monkeypatch.setattr(
        "mindroom.workers.backends.kubernetes.sync_shared_credentials_to_worker",
        _record_sync,
    )

    backend.ensure_worker(WorkerSpec("v1:tenant-123:user:@alice:example.org"), now=10.0)

    assert sync_calls == [("v1:tenant-123:user:@alice:example.org", False)]


def test_kubernetes_backend_cleanup_scales_idle_workers_to_zero() -> None:
    """Idle cleanup should scale dedicated workers to zero while keeping their metadata."""
    backend, apps_api, core_api = _backend(idle_timeout_seconds=5.0)
    handle = backend.ensure_worker(WorkerSpec(_TEST_SCOPED_WORKER_KEY_A), now=0.0)
    deployment = apps_api.deployments[handle.worker_id]
    deployment.metadata.annotations["mindroom.ai/last-used-at"] = "0.0"
    deployment.metadata.annotations["mindroom.ai/worker-status"] = "ready"

    cleaned = backend.cleanup_idle_workers(now=10.0)

    assert len(cleaned) == 1
    assert cleaned[0].worker_key == _TEST_SCOPED_WORKER_KEY_A
    assert cleaned[0].status == "idle"
    assert deployment.spec.replicas == 0
    assert handle.worker_id not in core_api.services


def test_kubernetes_backend_cleanup_is_idempotent_for_already_idle_workers() -> None:
    """Cleanup should not report or patch workers that are already scaled to zero."""
    backend, apps_api, _core_api = _backend(idle_timeout_seconds=5.0)
    handle = backend.ensure_worker(WorkerSpec(_TEST_SCOPED_WORKER_KEY_A), now=0.0)
    deployment = apps_api.deployments[handle.worker_id]
    deployment.metadata.annotations["mindroom.ai/last-used-at"] = "0.0"
    deployment.metadata.annotations["mindroom.ai/worker-status"] = "ready"

    first_cleaned = backend.cleanup_idle_workers(now=10.0)
    patch_count_after_first_cleanup = len(apps_api.patched_bodies)
    second_cleaned = backend.cleanup_idle_workers(now=11.0)

    assert [worker.worker_key for worker in first_cleaned] == [_TEST_SCOPED_WORKER_KEY_A]
    assert second_cleaned == []
    assert len(apps_api.patched_bodies) == patch_count_after_first_cleanup


def test_kubernetes_backend_evict_without_preserving_state_deletes_runtime_resources() -> None:
    """Non-preserving eviction should delete the worker service and deployment resources."""
    backend, apps_api, core_api = _backend()
    handle = backend.ensure_worker(WorkerSpec(_TEST_SCOPED_WORKER_KEY_A), now=0.0)

    evicted = backend.evict_worker(_TEST_SCOPED_WORKER_KEY_A, preserve_state=False, now=5.0)

    assert evicted is None
    assert handle.worker_id not in apps_api.deployments
    assert handle.worker_id not in core_api.services


def test_kubernetes_backend_preserving_evict_deletes_service_but_keeps_deployment() -> None:
    """Idle-preserving eviction should scale down the worker and release its Service."""
    backend, apps_api, core_api = _backend()
    handle = backend.ensure_worker(WorkerSpec(_TEST_SCOPED_WORKER_KEY_A), now=0.0)

    evicted = backend.evict_worker(_TEST_SCOPED_WORKER_KEY_A, preserve_state=True, now=5.0)

    assert evicted is not None
    assert evicted.status == "idle"
    assert handle.worker_id in apps_api.deployments
    assert apps_api.deployments[handle.worker_id].spec.replicas == 0
    assert handle.worker_id not in core_api.services


def test_kubernetes_backend_preserving_evict_clears_stale_failure_reason() -> None:
    """Workers that leave the failed state should not keep stale failure annotations."""
    backend, apps_api, core_api = _backend()
    handle = backend.ensure_worker(WorkerSpec(_TEST_SCOPED_WORKER_KEY_A), now=0.0)

    backend.record_failure(_TEST_SCOPED_WORKER_KEY_A, "boom", now=1.0)
    evicted = backend.evict_worker(_TEST_SCOPED_WORKER_KEY_A, preserve_state=True, now=5.0)

    assert evicted is not None
    assert evicted.status == "idle"
    assert evicted.failure_reason is None
    assert handle.worker_id in apps_api.deployments
    assert handle.worker_id not in core_api.services
    assert "mindroom.ai/failure-reason" not in apps_api.deployments[handle.worker_id].metadata.annotations

    touched = backend.get_worker(_TEST_SCOPED_WORKER_KEY_A, now=6.0)
    assert touched is not None
    assert touched.status == "idle"
    assert touched.failure_reason is None


def test_kubernetes_backend_list_workers_is_scoped_to_backend_labels() -> None:
    """Worker discovery should stay confined to this backend's label set within a shared namespace."""
    backend, apps_api, _core_api = _backend()
    handle = backend.ensure_worker(WorkerSpec(_TEST_SCOPED_WORKER_KEY_A), now=0.0)

    unrelated_body = deepcopy(apps_api.created_bodies[0])
    unrelated_name = "mindroom-worker-unrelated"
    unrelated_body["metadata"]["name"] = unrelated_name
    unrelated_body["metadata"]["annotations"]["mindroom.ai/worker-key"] = _TEST_SCOPED_WORKER_KEY_B
    unrelated_body["metadata"]["labels"]["mindroom.ai/tenant"] = "other"
    unrelated_body["metadata"]["labels"]["mindroom.ai/worker-id"] = unrelated_name
    unrelated_body["spec"]["selector"]["matchLabels"]["mindroom.ai/tenant"] = "other"
    unrelated_body["spec"]["selector"]["matchLabels"]["mindroom.ai/worker-id"] = unrelated_name
    unrelated_body["spec"]["template"]["metadata"]["labels"]["mindroom.ai/tenant"] = "other"
    unrelated_body["spec"]["template"]["metadata"]["labels"]["mindroom.ai/worker-id"] = unrelated_name
    unrelated_body["spec"]["replicas"] = 1
    unrelated = _to_namespace(unrelated_body)
    unrelated.metadata.generation = 1
    unrelated.status = SimpleNamespace(ready_replicas=1, observed_generation=1)
    apps_api.deployments[unrelated_name] = unrelated

    workers = backend.list_workers(now=10.0)

    assert [worker.worker_key for worker in workers] == [handle.worker_key]
    assert apps_api.list_label_selectors[-1] == (
        "app.kubernetes.io/managed-by=mindroom,"
        "app.kubernetes.io/name=mindroom-worker,"
        "mindroom.ai/component=worker,"
        "mindroom.ai/tenant=test"
    )


def test_kubernetes_backend_touch_only_patches_deployment_metadata() -> None:
    """Refreshing worker usage must not mutate the pod template and trigger a rollout."""
    backend, apps_api, _core_api = _backend()
    handle = backend.ensure_worker(WorkerSpec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    touched = backend.touch_worker(_TEST_SCOPED_WORKER_KEY_A, now=25.0)

    assert touched is not None
    patch_name, patch_body = apps_api.patched_bodies[-1]
    assert patch_name == handle.worker_id
    assert patch_body["metadata"]["annotations"]["mindroom.ai/last-used-at"] == "25.0"
    assert "template" not in patch_body.get("spec", {})


def test_kubernetes_backend_reuses_ready_deployment_without_incrementing_startups() -> None:
    """Ensuring an already-ready worker should keep its original startup metadata."""
    backend, apps_api, _core_api = _backend()
    first = backend.ensure_worker(WorkerSpec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    second = backend.ensure_worker(WorkerSpec(_TEST_SCOPED_WORKER_KEY_A), now=20.0)

    assert second.worker_id == first.worker_id
    deployment = apps_api.deployments[first.worker_id]
    assert deployment.metadata.annotations["mindroom.ai/startup-count"] == "1"
    assert deployment.metadata.annotations["mindroom.ai/last-started-at"] == "10.0"
    assert deployment.metadata.annotations["mindroom.ai/last-used-at"] == "20.0"
    assert deployment.metadata.annotations["mindroom.ai/worker-status"] == "ready"


def test_kubernetes_backend_pins_workers_to_control_plane_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dedicated workers should co-locate with the control-plane pod when using a shared RWO PVC."""
    backend, apps_api, core_api = _backend(colocate_with_control_plane_node=True)
    core_api.pods["mindroom-control-plane"] = SimpleNamespace(
        metadata=SimpleNamespace(name="mindroom-control-plane"),
        spec=SimpleNamespace(node_name="gke-chat-node-1"),
    )
    monkeypatch.setenv("HOSTNAME", "mindroom-control-plane")

    backend.ensure_worker(WorkerSpec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    deployment = apps_api.created_bodies[0]
    assert deployment["spec"]["template"]["spec"]["nodeName"] == "gke-chat-node-1"


def test_kubernetes_backend_uses_explicit_worker_node_name_when_configured() -> None:
    """Dedicated workers should honor an explicit node pin without querying the control-plane pod."""
    backend, apps_api, _core_api = _backend(node_name="gke-chat-node-2")

    backend.ensure_worker(WorkerSpec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    deployment = apps_api.created_bodies[0]
    assert deployment["spec"]["template"]["spec"]["nodeName"] == "gke-chat-node-2"


def test_kubernetes_backend_does_not_pin_workers_when_colocation_disabled() -> None:
    """RWX-capable deployments should be able to omit node pinning entirely."""
    backend, apps_api, _core_api = _backend()

    backend.ensure_worker(WorkerSpec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    deployment = apps_api.created_bodies[0]
    assert "nodeName" not in deployment["spec"]["template"]["spec"]


def test_kubernetes_backend_records_failed_startup_state() -> None:
    """Workers that never become ready should surface as failed instead of starting forever."""
    backend, apps_api, _core_api = _backend()
    error_message = "worker never became ready"

    def _boom(
        _self: object,
        _deployment_name: str,
        *,
        timeout_seconds: float,
        deployment_ready_fn: object,
    ) -> object:
        del timeout_seconds, deployment_ready_fn
        raise WorkerBackendError(error_message)

    backend._resources.wait_for_ready = MethodType(_boom, backend._resources)

    with pytest.raises(WorkerBackendError, match=error_message):
        backend.ensure_worker(WorkerSpec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    worker_id = next(iter(apps_api.deployments))
    deployment = apps_api.deployments[worker_id]
    assert deployment.metadata.annotations["mindroom.ai/worker-status"] == "failed"
    assert deployment.metadata.annotations["mindroom.ai/failure-reason"] == error_message
    assert deployment.metadata.annotations["mindroom.ai/failure-count"] == "1"
    assert deployment.spec.replicas == 0

    handle = backend.get_worker(_TEST_SCOPED_WORKER_KEY_A, now=11.0)
    assert handle is not None
    assert handle.status == "failed"
    assert handle.failure_reason == error_message
    assert worker_id not in _core_api.services


def test_kubernetes_backend_keeps_digest_when_worker_name_prefix_is_long() -> None:
    """Long prefixes must still preserve the per-worker digest so names remain unique."""
    long_prefix = "mindroom-worker-prefix-that-is-intentionally-way-too-long-for-a-kubernetes-name"
    backend, _apps_api, _core_api = _backend(name_prefix=long_prefix)

    first = backend.ensure_worker(WorkerSpec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)
    second = backend.ensure_worker(WorkerSpec(_TEST_SCOPED_WORKER_KEY_B), now=20.0)

    assert first.worker_id != second.worker_id
    assert len(first.worker_id) <= 63
    assert len(second.worker_id) <= 63
