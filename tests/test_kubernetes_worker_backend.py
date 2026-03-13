"""Tests for the Kubernetes worker backend."""

from __future__ import annotations

from copy import deepcopy
from types import MethodType, SimpleNamespace

import pytest

from mindroom.tool_system.worker_routing import worker_dir_name
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.backends.kubernetes import KubernetesWorkerBackend, _KubernetesWorkerBackendConfig
from mindroom.workers.models import WorkerSpec

_TEST_TOKEN_SECRET_NAME = "mindroom-secrets"  # noqa: S105
_TEST_TOKEN_SECRET_KEY = "sandbox_proxy_token"  # noqa: S105
_TEST_AUTH_TOKEN = "test-token"  # noqa: S105


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
        self.list_label_selectors: list[str] = []

    def read_namespaced_deployment(self, name: str, namespace: str) -> object:
        _ = namespace
        deployment = self.deployments.get(name)
        if deployment is None:
            raise _FakeApiError(404)
        return deployment

    def create_namespaced_deployment(self, namespace: str, body: dict[str, object]) -> object:
        _ = namespace
        self.created_bodies.append(body)
        deployment = _to_namespace(body)
        deployment.metadata.generation = 1
        deployment.metadata.uid = f"{deployment.metadata.name}-uid"
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
        if isinstance(spec, dict) and "replicas" in spec:
            deployment.spec.replicas = spec["replicas"]
            deployment.status.ready_replicas = spec["replicas"]
        deployment.metadata.generation += 1
        deployment.status.observed_generation = deployment.metadata.generation
        return deployment

    def delete_namespaced_deployment(self, name: str, namespace: str) -> None:
        _ = namespace
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
    node_name: str | None = None,
    colocate_with_control_plane_node: bool = False,
    name_prefix: str = "mindroom-worker",
    owner_deployment_name: str | None = None,
) -> tuple[KubernetesWorkerBackend, _FakeAppsApi, _FakeCoreApi]:
    config = _KubernetesWorkerBackendConfig(
        namespace="chat",
        image="ghcr.io/mindroom-ai/mindroom:latest",
        image_pull_policy="IfNotPresent",
        worker_port=worker_port,
        service_account_name="mindroom-worker",
        storage_pvc_name="mindroom-storage",
        storage_mount_path="/app/worker",
        storage_subpath_prefix="workers",
        config_map_name="mindroom-config",
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
    backend = KubernetesWorkerBackend(config=config, auth_token=_TEST_AUTH_TOKEN)
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
    env_values = {env["name"]: env.get("value") for env in container["env"]}
    env_names = set(env_values)
    assert "MINDROOM_SANDBOX_DEDICATED_WORKER_KEY" in env_names
    assert "MINDROOM_SANDBOX_DEDICATED_WORKER_ROOT" in env_names
    assert "MINDROOM_STORAGE_PATH" in env_names
    assert "VIRTUAL_ENV" in env_names
    assert "PATH" in env_names
    assert "MINDROOM_SHARED_CREDENTIALS_PATH" in env_names
    assert "MINDROOM_SANDBOX_PROXY_TOKEN" in env_names
    assert env_values["MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE"] == "subprocess"
    assert env_values["MINDROOM_SANDBOX_RUNNER_PORT"] == "8766"
    expected_dedicated_root = f"/app/worker/workers/{worker_dir_name('worker-a')}"
    assert env_values["MINDROOM_STORAGE_PATH"] == "/app/worker"
    assert env_values["MINDROOM_SANDBOX_DEDICATED_WORKER_ROOT"] == expected_dedicated_root
    assert env_values["HOME"] == expected_dedicated_root
    assert env_values["VIRTUAL_ENV"] == f"{expected_dedicated_root}/venv"
    assert env_values["PATH"].startswith(f"{expected_dedicated_root}/venv/bin:")
    assert env_values["MINDROOM_SHARED_CREDENTIALS_PATH"] == "/app/worker/.shared_credentials"
    assert "subPath" not in container["volumeMounts"][0]
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


def test_kubernetes_backend_requires_configured_owner_deployment_to_exist() -> None:
    """Configured owner deployments should fail closed when they cannot be resolved."""
    backend, _apps_api, _core_api = _backend(owner_deployment_name="mindroom-missing")
    assert isinstance(backend._resources.apps_api, _FakeAppsApi)
    backend._resources.apps_api.deployments.pop("mindroom-missing")

    with pytest.raises(WorkerBackendError, match="owner deployment 'mindroom-missing' was not found"):
        backend.ensure_worker(WorkerSpec("worker-a"), now=10.0)


def test_kubernetes_backend_honors_custom_worker_port() -> None:
    """Dedicated workers should wire the configured port through env, service, and probes."""
    backend, apps_api, core_api = _backend(worker_port=9777)

    handle = backend.ensure_worker(WorkerSpec("worker-a"), now=10.0)

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
    handle = backend.ensure_worker(WorkerSpec("worker-a"), now=0.0)
    deployment = apps_api.deployments[handle.worker_id]
    deployment.metadata.annotations["mindroom.ai/last-used-at"] = "0.0"
    deployment.metadata.annotations["mindroom.ai/worker-status"] = "ready"

    cleaned = backend.cleanup_idle_workers(now=10.0)

    assert len(cleaned) == 1
    assert cleaned[0].worker_key == "worker-a"
    assert cleaned[0].status == "idle"
    assert deployment.spec.replicas == 0
    assert handle.worker_id not in core_api.services


def test_kubernetes_backend_cleanup_is_idempotent_for_already_idle_workers() -> None:
    """Cleanup should not report or patch workers that are already scaled to zero."""
    backend, apps_api, _core_api = _backend(idle_timeout_seconds=5.0)
    handle = backend.ensure_worker(WorkerSpec("worker-a"), now=0.0)
    deployment = apps_api.deployments[handle.worker_id]
    deployment.metadata.annotations["mindroom.ai/last-used-at"] = "0.0"
    deployment.metadata.annotations["mindroom.ai/worker-status"] = "ready"

    first_cleaned = backend.cleanup_idle_workers(now=10.0)
    patch_count_after_first_cleanup = len(apps_api.patched_bodies)
    second_cleaned = backend.cleanup_idle_workers(now=11.0)

    assert [worker.worker_key for worker in first_cleaned] == ["worker-a"]
    assert second_cleaned == []
    assert len(apps_api.patched_bodies) == patch_count_after_first_cleanup


def test_kubernetes_backend_evict_without_preserving_state_deletes_runtime_resources() -> None:
    """Non-preserving eviction should delete the worker service and deployment resources."""
    backend, apps_api, core_api = _backend()
    handle = backend.ensure_worker(WorkerSpec("worker-a"), now=0.0)

    evicted = backend.evict_worker("worker-a", preserve_state=False, now=5.0)

    assert evicted is None
    assert handle.worker_id not in apps_api.deployments
    assert handle.worker_id not in core_api.services


def test_kubernetes_backend_preserving_evict_deletes_service_but_keeps_deployment() -> None:
    """Idle-preserving eviction should scale down the worker and release its Service."""
    backend, apps_api, core_api = _backend()
    handle = backend.ensure_worker(WorkerSpec("worker-a"), now=0.0)

    evicted = backend.evict_worker("worker-a", preserve_state=True, now=5.0)

    assert evicted is not None
    assert evicted.status == "idle"
    assert handle.worker_id in apps_api.deployments
    assert apps_api.deployments[handle.worker_id].spec.replicas == 0
    assert handle.worker_id not in core_api.services


def test_kubernetes_backend_list_workers_is_scoped_to_backend_labels() -> None:
    """Worker discovery should stay confined to this backend's label set within a shared namespace."""
    backend, apps_api, _core_api = _backend()
    handle = backend.ensure_worker(WorkerSpec("worker-a"), now=0.0)

    unrelated_body = deepcopy(apps_api.created_bodies[0])
    unrelated_name = "mindroom-worker-unrelated"
    unrelated_body["metadata"]["name"] = unrelated_name
    unrelated_body["metadata"]["annotations"]["mindroom.ai/worker-key"] = "worker-b"
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
    handle = backend.ensure_worker(WorkerSpec("worker-a"), now=10.0)

    touched = backend.touch_worker("worker-a", now=25.0)

    assert touched is not None
    patch_name, patch_body = apps_api.patched_bodies[-1]
    assert patch_name == handle.worker_id
    assert patch_body["metadata"]["annotations"]["mindroom.ai/last-used-at"] == "25.0"
    assert "template" not in patch_body.get("spec", {})


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

    backend.ensure_worker(WorkerSpec("worker-a"), now=10.0)

    deployment = apps_api.created_bodies[0]
    assert deployment["spec"]["template"]["spec"]["nodeName"] == "gke-chat-node-1"


def test_kubernetes_backend_uses_explicit_worker_node_name_when_configured() -> None:
    """Dedicated workers should honor an explicit node pin without querying the control-plane pod."""
    backend, apps_api, _core_api = _backend(node_name="gke-chat-node-2")

    backend.ensure_worker(WorkerSpec("worker-a"), now=10.0)

    deployment = apps_api.created_bodies[0]
    assert deployment["spec"]["template"]["spec"]["nodeName"] == "gke-chat-node-2"


def test_kubernetes_backend_does_not_pin_workers_when_colocation_disabled() -> None:
    """RWX-capable deployments should be able to omit node pinning entirely."""
    backend, apps_api, _core_api = _backend()

    backend.ensure_worker(WorkerSpec("worker-a"), now=10.0)

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
        backend.ensure_worker(WorkerSpec("worker-a"), now=10.0)

    worker_id = next(iter(apps_api.deployments))
    deployment = apps_api.deployments[worker_id]
    assert deployment.metadata.annotations["mindroom.ai/worker-status"] == "failed"
    assert deployment.metadata.annotations["mindroom.ai/failure-reason"] == error_message
    assert deployment.metadata.annotations["mindroom.ai/failure-count"] == "1"
    assert deployment.spec.replicas == 0

    handle = backend.get_worker("worker-a", now=11.0)
    assert handle is not None
    assert handle.status == "failed"
    assert handle.failure_reason == error_message
    assert worker_id not in _core_api.services


def test_kubernetes_backend_keeps_digest_when_worker_name_prefix_is_long() -> None:
    """Long prefixes must still preserve the per-worker digest so names remain unique."""
    long_prefix = "mindroom-worker-prefix-that-is-intentionally-way-too-long-for-a-kubernetes-name"
    backend, apps_api, _core_api = _backend(name_prefix=long_prefix)

    first = backend.ensure_worker(WorkerSpec("worker-a"), now=10.0)
    second = backend.ensure_worker(WorkerSpec("worker-b"), now=20.0)

    assert first.worker_id != second.worker_id
    assert len(first.worker_id) <= 63
    assert len(second.worker_id) <= 63
