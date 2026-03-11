"""Tests for the Kubernetes worker backend."""

from __future__ import annotations

from types import SimpleNamespace

from mindroom.tool_system.worker_routing import worker_dir_name
from mindroom.workers.backends.kubernetes import KubernetesWorkerBackend, KubernetesWorkerBackendConfig
from mindroom.workers.models import WorkerSpec


class _FakeApiException(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


def _to_namespace(value: object) -> object:
    if isinstance(value, dict):
        return SimpleNamespace(**{key: _to_namespace(item) for key, item in value.items()})
    if isinstance(value, list):
        return [_to_namespace(item) for item in value]
    return value


class _FakeAppsApi:
    def __init__(self) -> None:
        self.deployments: dict[str, object] = {}
        self.created_bodies: list[dict[str, object]] = []
        self.patched_bodies: list[tuple[str, dict[str, object]]] = []

    def read_namespaced_deployment(self, name: str, namespace: str) -> object:
        _ = namespace
        deployment = self.deployments.get(name)
        if deployment is None:
            raise _FakeApiException(404)
        return deployment

    def create_namespaced_deployment(self, namespace: str, body: dict[str, object]) -> object:
        _ = namespace
        self.created_bodies.append(body)
        deployment = _to_namespace(body)
        deployment.metadata.generation = 1
        deployment.status = SimpleNamespace(ready_replicas=body["spec"]["replicas"], observed_generation=1)
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
        if isinstance(spec, dict):
            if "replicas" in spec:
                deployment.spec.replicas = spec["replicas"]
                deployment.status.ready_replicas = spec["replicas"]
            template = spec.get("template")
            if isinstance(template, dict):
                template_meta = template.get("metadata")
                if isinstance(template_meta, dict) and isinstance(template_meta.get("annotations"), dict):
                    deployment.spec.template.metadata.annotations = template_meta["annotations"]
        deployment.metadata.generation = getattr(deployment.metadata, "generation", 1) + 1
        deployment.status.observed_generation = deployment.metadata.generation
        return deployment

    def delete_namespaced_deployment(self, name: str, namespace: str) -> None:
        _ = namespace
        self.deployments.pop(name, None)

    def list_namespaced_deployment(self, namespace: str, label_selector: str) -> object:
        _ = namespace, label_selector
        return SimpleNamespace(items=list(self.deployments.values()))


class _FakeCoreApi:
    def __init__(self) -> None:
        self.services: dict[str, object] = {}
        self.created_bodies: list[dict[str, object]] = []

    def read_namespaced_service(self, name: str, namespace: str) -> object:
        _ = namespace
        service = self.services.get(name)
        if service is None:
            raise _FakeApiException(404)
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


def _backend(*, idle_timeout_seconds: float = 60.0) -> tuple[KubernetesWorkerBackend, _FakeAppsApi, _FakeCoreApi]:
    config = KubernetesWorkerBackendConfig(
        namespace="chat",
        image="ghcr.io/mindroom-ai/mindroom:latest",
        image_pull_policy="IfNotPresent",
        worker_port=8766,
        service_account_name="mindroom-worker",
        storage_pvc_name="mindroom-storage",
        storage_mount_path="/app/worker",
        storage_subpath_prefix="workers",
        config_map_name="mindroom-config",
        config_key="config.yaml",
        config_path="/app/config.yaml",
        token_secret_name="mindroom-secrets",
        token_secret_key="sandbox_proxy_token",
        idle_timeout_seconds=idle_timeout_seconds,
        ready_timeout_seconds=5.0,
        name_prefix="mindroom-worker",
        extra_env={},
        extra_labels={"mindroom.ai/tenant": "test"},
    )
    backend = KubernetesWorkerBackend(config=config, auth_token="test-token")
    apps_api = _FakeAppsApi()
    core_api = _FakeCoreApi()
    backend._apps_api = apps_api
    backend._core_api = core_api
    backend._api_exception_cls = _FakeApiException
    return backend, apps_api, core_api


def test_kubernetes_backend_ensures_worker_service_and_deployment() -> None:
    """Ensuring one worker should create a service/deployment pair with a dedicated mounted subpath."""
    backend, apps_api, core_api = _backend()

    handle = backend.ensure_worker(WorkerSpec("worker-a"), now=10.0)

    assert handle.worker_key == "worker-a"
    assert handle.backend_name == "kubernetes"
    assert handle.endpoint.endswith("/api/sandbox-runner/execute")
    assert handle.debug_metadata["namespace"] == "chat"
    assert handle.debug_metadata["state_subpath"] == f"workers/{worker_dir_name('worker-a')}"
    assert handle.debug_metadata["service_name"] == handle.worker_id
    assert handle.status == "ready"

    assert len(core_api.created_bodies) == 1
    assert len(apps_api.created_bodies) == 1
    deployment = apps_api.created_bodies[0]
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    env_names = {env["name"] for env in container["env"]}
    assert "MINDROOM_SANDBOX_DEDICATED_WORKER_KEY" in env_names
    assert "MINDROOM_SANDBOX_DEDICATED_WORKER_ROOT" in env_names
    assert "MINDROOM_STORAGE_PATH" in env_names
    assert "MINDROOM_SANDBOX_PROXY_TOKEN" in env_names
    assert container["volumeMounts"][0]["subPath"] == f"workers/{worker_dir_name('worker-a')}"
    assert deployment["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] == "mindroom-storage"


def test_kubernetes_backend_cleanup_scales_idle_workers_to_zero() -> None:
    """Idle cleanup should scale dedicated workers to zero while keeping their metadata."""
    backend, apps_api, _core_api = _backend(idle_timeout_seconds=5.0)
    handle = backend.ensure_worker(WorkerSpec("worker-a"), now=0.0)
    deployment = apps_api.deployments[handle.worker_id]
    deployment.metadata.annotations["mindroom.ai/last-used-at"] = "0.0"
    deployment.metadata.annotations["mindroom.ai/worker-status"] = "ready"

    cleaned = backend.cleanup_idle_workers(now=10.0)

    assert len(cleaned) == 1
    assert cleaned[0].worker_key == "worker-a"
    assert cleaned[0].status == "idle"
    assert deployment.spec.replicas == 0


def test_kubernetes_backend_evict_without_preserving_state_deletes_runtime_resources() -> None:
    """Non-preserving eviction should delete the worker service and deployment resources."""
    backend, apps_api, core_api = _backend()
    handle = backend.ensure_worker(WorkerSpec("worker-a"), now=0.0)

    evicted = backend.evict_worker("worker-a", preserve_state=False, now=5.0)

    assert evicted is None
    assert handle.worker_id not in apps_api.deployments
    assert handle.worker_id not in core_api.services
