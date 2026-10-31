"""Tests for the Kubernetes worker backend."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import tempfile
import threading
import time
import weakref
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import asdict, replace
from functools import cache
from pathlib import Path
from types import MethodType, SimpleNamespace
from typing import TYPE_CHECKING, Self
from unittest.mock import MagicMock, patch

import httpx
import pytest
from structlog.testing import capture_logs

from mindroom.config.main import Config, load_config
from mindroom.constants import (
    DEFAULT_WORKER_GRANTABLE_CREDENTIALS,
    RuntimePaths,
    deserialize_runtime_paths,
    resolve_primary_runtime_paths,
    sandbox_startup_manifest_path,
    startup_manifest_sha256,
)
from mindroom.private_storage.private_instance_identity_store import ensure_private_instance_identity
from mindroom.runtime_env_policy import CREDENTIALS_ENCRYPTION_KEY_ENV
from mindroom.script_runs.models import script_worker_key_for_run
from mindroom.shared_directories.store import add_shared_directory_member, create_shared_directory
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    private_instance_scope_root_path,
    resolve_unscoped_worker_key,
    resolve_worker_key,
    worker_dir_name,
    worker_id_for_key,
    worker_root_path,
)
from mindroom.workers import progress as progress_module
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.backend_kinds import WorkerBackendKind, worker_backend_kind
from mindroom.workers.backends import kubernetes as kubernetes_backend_module
from mindroom.workers.backends import kubernetes_resources as kubernetes_resources_module
from mindroom.workers.backends import legacy_state_root_mounts
from mindroom.workers.backends.kubernetes import (
    KubernetesWorkerBackend,
    KubernetesWorkerBackendConfig,
    configure_kubernetes_worker_backend,
)
from mindroom.workers.backends.kubernetes_resources import (
    _ANNOTATION_PRIVATE_AGENT_NAMES,
    _ANNOTATION_RUNNER_TOKEN_HASH,
    _ANNOTATION_STARTUP_MANIFEST_HASH,
    _ANNOTATION_STATE_SCOPE_WORKER_KEY,
    ANNOTATION_TEMPLATE_HASH,
    ANNOTATION_WORKER_KEY,
    worker_auth_token,
)
from mindroom.workers.models import WorkerHandle, WorkerLaunchConfig, WorkerReadyProgress, WorkerSpec
from mindroom.workers.protocol import WORKER_PROTOCOL_VERSION
from mindroom.workers.runtime import build_worker_launch_config, configured_primary_worker_manager_identity

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.workers.backends.kubernetes_config import _WorkerSeccompProfile
    from mindroom.workers.models import ScriptResourceProfileName

_TEST_AUTH_TOKEN = "test-token"  # noqa: S105
_TEST_SCOPED_WORKER_KEY_A = "v1:tenant-123:shared:code"
_TEST_SCOPED_WORKER_KEY_B = "v1:tenant-123:shared:research"
_TEST_TOOL_VALIDATION_SNAPSHOT = {
    "calculator": {
        "config_fields": [],
        "agent_override_fields": [],
        "authored_override_validator": "default",
        "runtime_loadable": True,
    },
}


class _ControlledMonotonicClock:
    def __init__(self, initial_seconds: float = 0.0) -> None:
        self._now = initial_seconds
        self._condition = threading.Condition()
        self._listeners: list[Callable[[], None]] = []

    def monotonic(self) -> float:
        with self._condition:
            return self._now

    def sleep(self, seconds: float) -> None:
        self.wait_until(self.monotonic() + seconds)

    def wait_until(self, target_seconds: float) -> None:
        with self._condition:
            while self._now < target_seconds:
                self._condition.wait(timeout=0.1)

    def advance_to(self, target_seconds: float) -> None:
        with self._condition:
            assert target_seconds >= self._now
            self._now = target_seconds
            listeners = tuple(self._listeners)
            self._condition.notify_all()
        for listener in listeners:
            listener()

    def add_listener(self, listener: Callable[[], None]) -> None:
        with self._condition:
            self._listeners.append(listener)


class _ControlledCondition:
    def __init__(self, clock: _ControlledMonotonicClock) -> None:
        self._clock = clock
        self._condition = threading.Condition()
        self._wakeups = 0
        self._clock.add_listener(self._notify_from_clock)

    def __enter__(self) -> Self:
        self._condition.__enter__()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self._condition.__exit__(exc_type, exc, tb)

    def wait(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else self._clock.monotonic() + timeout
        while True:
            if self._wakeups > 0:
                self._wakeups -= 1
                return True
            if deadline is not None and self._clock.monotonic() >= deadline:
                return True
            self._condition.wait(timeout=0.1)

    def notify_all(self) -> None:
        self._wakeups += 1
        self._condition.notify_all()

    def _notify_from_clock(self) -> None:
        with self._condition:
            self._condition.notify_all()


def _load_startup_manifest(
    backend: KubernetesWorkerBackend,
    *,
    worker_key: str,
) -> dict[str, object]:
    manifest_path = sandbox_startup_manifest_path(
        backend.storage_root / f"workers/{worker_dir_name(worker_key)}",
    )
    return json.loads(manifest_path.read_text(encoding="utf-8"))


class _FakeApiError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


_MAPPING_KEYS = {"annotations", "labels", "matchLabels", "selector", "stringData", "data"}


def _encoded_secret_data(values: dict[str, str]) -> dict[str, str]:
    return {key: base64.b64encode(value.encode("utf-8")).decode("ascii") for key, value in values.items()}


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
        self.replica_sets: dict[str, object] = {}
        self.read_names: list[str] = []
        self.created_bodies: list[dict[str, object]] = []
        self.patched_bodies: list[tuple[str, dict[str, object]]] = []
        self.deleted_names: list[str] = []
        self.deleted_propagation_policies: list[str | None] = []
        self.list_label_selectors: list[str] = []
        self.raw_list_count = 0
        self.delete_read_lag_by_name: dict[str, int] = {}
        self._active_delete_read_lag_by_name: dict[str, int] = {}

    def read_namespaced_deployment(self, name: str, namespace: str) -> object:
        _ = namespace
        self.read_names.append(name)
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

    def patch_namespaced_deployment(
        self,
        name: str,
        namespace: str,
        body: dict[str, object],
        **_kwargs: object,
    ) -> object:
        _ = namespace
        self.patched_bodies.append((name, body))
        deployment = self.deployments.get(name)
        if deployment is None:
            raise _FakeApiError(404)
        metadata = body.get("metadata")
        if isinstance(metadata, dict):
            annotations = metadata.get("annotations")
            if isinstance(annotations, dict):
                deployment.metadata.annotations = {
                    **dict(deployment.metadata.annotations or {}),
                    **annotations,
                }
        spec = body.get("spec")
        if isinstance(spec, dict) and "replicas" in spec:
            deployment.spec.replicas = spec["replicas"]
            deployment.status.ready_replicas = spec["replicas"]
        deployment.metadata.generation += 1
        deployment.status.observed_generation = deployment.metadata.generation
        return deployment

    def delete_namespaced_deployment(
        self,
        name: str,
        namespace: str,
        *,
        propagation_policy: str | None = None,
    ) -> None:
        _ = namespace
        self.deleted_names.append(name)
        self.deleted_propagation_policies.append(propagation_policy)
        if self.delete_read_lag_by_name.get(name, 0) > 0:
            self._active_delete_read_lag_by_name[name] = self.delete_read_lag_by_name[name]
            return
        self.deployments.pop(name, None)

    def list_namespaced_deployment(
        self,
        namespace: str,
        *,
        label_selector: str,
        _preload_content: bool = True,
        _request_timeout: float | None = None,
        limit: int | None = None,
    ) -> object:
        _ = namespace
        if limit is not None:
            assert limit == 1
            assert _preload_content is False
            assert _request_timeout is not None
            assert 0.0 < _request_timeout <= 5.0
        self.list_label_selectors.append(label_selector)
        selectors: dict[str, str | None] = {}
        for expression in filter(None, (part.strip() for part in label_selector.split(","))):
            key, sep, value = expression.partition("=")
            selectors[key] = value if sep else None

        def matches_selector(deployment: object) -> bool:
            labels = deployment.metadata.labels
            return all(key in labels and (value is None or labels[key] == value) for key, value in selectors.items())

        deployments = [deployment for deployment in self.deployments.values() if matches_selector(deployment)]
        if _preload_content:
            return SimpleNamespace(items=deployments)
        self.raw_list_count += 1
        payload = {
            "items": [
                {
                    "metadata": {
                        "name": deployment.metadata.name,
                        "annotations": deployment.metadata.annotations,
                        "labels": deployment.metadata.labels,
                        "generation": deployment.metadata.generation,
                        "uid": deployment.metadata.uid,
                    },
                    "spec": {"replicas": deployment.spec.replicas},
                    "status": {
                        "readyReplicas": deployment.status.ready_replicas,
                        "observedGeneration": deployment.status.observed_generation,
                    },
                }
                for deployment in deployments
            ],
        }
        return _FakeRawResponse(json.dumps(payload).encode())

    def list_namespaced_replica_set(self, namespace: str, **kwargs: object) -> _FakeRawResponse:
        return _absence_inventory(self.replica_sets, namespace, **kwargs)


class _FakeRawResponse:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.released = False

    def release_conn(self) -> None:
        self.released = True


def _absence_inventory(items: dict[str, object], namespace: str, **kwargs: object) -> _FakeRawResponse:
    assert namespace == "chat"
    assert kwargs["limit"] == 1
    assert kwargs["_preload_content"] is False
    assert 0.0 < kwargs["_request_timeout"] <= 5.0
    assert kwargs["label_selector"] == "mindroom.ai/worker-id"
    matches = [item for item in items.values() if "mindroom.ai/worker-id" in item.metadata.labels]
    return _FakeRawResponse(json.dumps({"items": [{}] if matches else []}).encode())


class _FakeCoreApi:
    def __init__(self) -> None:
        self.services: dict[str, object] = {}
        self.secrets: dict[str, object] = {}
        self.pods: dict[str, object] = {}
        self.read_service_names: list[str] = []
        self.read_secret_names: list[str] = []
        self.created_bodies: list[dict[str, object]] = []
        self.created_secret_bodies: list[dict[str, object]] = []
        self.patched_secret_bodies: list[tuple[str, object]] = []
        self.deleted_secret_names: list[str] = []
        self.api_client = _FakeApiClient(self)

    def list_namespaced_pod(self, namespace: str, **kwargs: object) -> _FakeRawResponse:
        return _absence_inventory(self.pods, namespace, **kwargs)

    def read_namespaced_service(self, name: str, namespace: str) -> object:
        _ = namespace
        self.read_service_names.append(name)
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

    def read_namespaced_secret(self, name: str, namespace: str) -> object:
        _ = namespace
        self.read_secret_names.append(name)
        secret = self.secrets.get(name)
        if secret is None:
            raise _FakeApiError(404)
        return secret

    def create_namespaced_secret(self, namespace: str, body: dict[str, object]) -> object:
        _ = namespace
        self.created_secret_bodies.append(body)
        string_data = body.get("stringData")
        data = body.get("data")
        secret = _to_namespace(body)
        secret.data = _encoded_secret_data(string_data) if isinstance(string_data, dict) else {}
        if isinstance(data, dict):
            secret.data.update(data)
        secret.stringData = {}
        self.secrets[secret.metadata.name] = secret
        return secret

    def patch_namespaced_secret(self, name: str, namespace: str, body: dict[str, object], **kwargs: object) -> object:
        del namespace, kwargs
        self.patched_secret_bodies.append((name, body))
        secret = self.secrets.get(name)
        if secret is None:
            raise _FakeApiError(404)
        string_data = body.get("stringData")
        if isinstance(string_data, dict):
            current_data = dict(secret.data or {})
            current_data.update(_encoded_secret_data(string_data))
            secret.data = current_data
            secret.stringData = {}
        data = body.get("data")
        if isinstance(data, dict):
            current_data = dict(secret.data or {})
            for key, value in data.items():
                if value is None:
                    current_data.pop(key, None)
                else:
                    current_data[key] = value
            secret.data = current_data
        return secret

    def delete_namespaced_secret(self, name: str, namespace: str) -> None:
        _ = namespace
        self.deleted_secret_names.append(name)
        self.secrets.pop(name, None)

    def read_namespaced_pod(self, name: str, namespace: str) -> object:
        _ = namespace
        pod = self.pods.get(name)
        if pod is None:
            raise _FakeApiError(404)
        return pod


class _FakeApiClient:
    def __init__(self, core_api: _FakeCoreApi) -> None:
        self._core_api = core_api

    def select_header_accept(self, _content_types: list[str]) -> str:
        return "application/json"

    def call_api(
        self,
        _resource_path: str,
        _method: str,
        path_params: dict[str, str],
        _query_params: list[object],
        header_params: dict[str, str],
        *,
        body: object,
        **_kwargs: object,
    ) -> object:
        assert header_params["Content-Type"] == "application/merge-patch+json"
        name = path_params["name"]
        namespace = path_params["namespace"]
        return self._core_api.patch_namespaced_secret(name, namespace, body)


def test_kubernetes_deployment_snapshot_tolerates_missing_status() -> None:
    """A Deployment read before the controller writes status is unready, not fatal."""
    payload = {
        "metadata": {"name": "mindroom-worker", "labels": {}},
        "spec": {"replicas": 1},
    }

    snapshot = kubernetes_resources_module._deployment_snapshot(payload)

    assert snapshot.status.ready_replicas is None
    assert snapshot.status.observed_generation is None


def test_kubernetes_deployment_snapshot_tolerates_missing_labels() -> None:
    """Labels are carried but never read; their absence must not fail the pass."""
    payload = {
        "metadata": {"name": "mindroom-worker"},
        "spec": {"replicas": 1},
        "status": {},
    }

    assert kubernetes_resources_module._deployment_snapshot(payload).metadata.labels == {}


def test_kubernetes_deployment_snapshot_rejects_non_object_status() -> None:
    """A structurally wrong status is still a malformed payload."""
    payload = {
        "metadata": {"name": "mindroom-worker", "labels": {}},
        "spec": {"replicas": 1},
        "status": "ready",
    }

    with pytest.raises(WorkerBackendError, match="non-object status"):
        kubernetes_resources_module._deployment_snapshot(payload)


def test_script_recovery_contract_survives_image_upgrade() -> None:
    """A compatible main-image rollout does not invalidate a live script worker."""
    backend, _apps, _core = _backend()
    initial = backend.script_recovery_signature()
    backend.config = replace(
        backend.config,
        image="mindroom:upgraded",
        image_pull_policy="Always",
        image_pull_secrets=("private-registry-pull",),
    )

    assert backend.script_recovery_signature() == initial


def test_script_recovery_contract_rejects_a_runtime_class_change() -> None:
    """Existing scripts cannot cross a RuntimeClass boundary."""
    backend, _apps, _core = _backend()
    initial_signature = backend.script_recovery_signature()

    backend.config = replace(backend.config, runtime_class_name="sandboxed")

    assert backend.script_recovery_signature() != initial_signature
    assert backend._script_recovery_payload()["config"]["runtime_class_name"] == "sandboxed"


@pytest.mark.parametrize(
    "change",
    [
        {"extra_env": {"SCRIPT_ACCESS": "changed"}},
        {"storage_mount_path": "/changed/workspace"},
        {"enable_service_links": True},
    ],
)
def test_script_recovery_contract_rejects_changed_worker_authority(change: dict[str, object]) -> None:
    """Changed worker environment, mounts or network exposure cannot inherit old authority."""
    backend, _apps, _core = _backend()
    initial = backend.script_recovery_signature()
    backend.config = replace(backend.config, **change)

    assert backend.script_recovery_signature() != initial


def test_script_recovery_contract_rejects_rotated_worker_authentication() -> None:
    """A new primary token cannot silently adopt a worker still using the old token."""
    backend, _apps, _core = _backend()
    initial = backend.script_recovery_signature()
    backend.auth_token = "rotated-worker-auth"  # noqa: S105

    assert backend.script_recovery_signature() != initial
    assert "rotated-worker-auth" not in backend.script_recovery_signature()


def test_script_recovery_contract_ignores_credentials_encryption_key() -> None:
    """Workers hold no credential encryption key, so rotating it leaves script worker authority unchanged."""
    backend, _apps, _core = _backend()
    backend.runtime_paths = replace(backend.runtime_paths, process_env={CREDENTIALS_ENCRYPTION_KEY_ENV: "old-material"})
    initial = backend.script_recovery_signature()
    backend.runtime_paths = replace(backend.runtime_paths, process_env={CREDENTIALS_ENCRYPTION_KEY_ENV: "new-material"})

    assert backend.script_recovery_signature() == initial


def test_script_recovery_contract_ignores_user_resource_overrides() -> None:
    """Per-user main-worker resources are pod resources, not script recovery authority."""
    backend, _apps, _core = _backend()
    initial = backend.script_recovery_signature()
    backend.config = replace(backend.config, user_resources={"@alice:example.org": {"limits": {"memory": "4Gi"}}})

    assert backend.script_recovery_signature() == initial


def test_script_recovery_resources_track_only_the_selected_profile() -> None:
    """Recovery compares the run's pod resources without binding unrelated profiles."""
    profiles = {
        "small": {
            "requests": {"cpu": "100m", "memory": "128Mi"},
            "limits": {"cpu": "500m", "memory": "512Mi"},
        },
        "standard": {
            "requests": {"cpu": "200m", "memory": "512Mi"},
            "limits": {"cpu": "1", "memory": "2Gi"},
        },
        "large": {
            "requests": {"cpu": "500m", "memory": "2Gi"},
            "limits": {"cpu": "2", "memory": "8Gi"},
        },
    }
    backend, _apps, _core = _backend(script_resource_profiles=profiles)
    backend_signature = backend.script_recovery_signature()
    initial = backend.script_resource_recovery_authority("standard")
    legacy_resources = backend.script_resource_recovery_authority(None)
    backend.config = replace(
        backend.config,
        resource_requests={"cpu": "300m", "memory": "768Mi"},
        resource_limits={"cpu": "1500m", "memory": "3Gi"},
    )
    assert backend.script_recovery_signature() == backend_signature
    assert backend.script_resource_recovery_authority("standard") == initial
    assert backend.script_resource_recovery_authority(None) != legacy_resources

    unrelated = deepcopy(profiles)
    unrelated["large"]["limits"]["cpu"] = "4"
    backend.config = replace(backend.config, script_resource_profiles=unrelated)
    assert backend.script_resource_recovery_authority("standard") == initial

    selected = deepcopy(unrelated)
    selected["standard"]["limits"]["cpu"] = "1500m"
    backend.config = replace(backend.config, script_resource_profiles=selected)
    assert backend.script_resource_recovery_authority("standard") != initial


# The applied launch each test backend reads, standing in for its runtime's applied config; None means none yet.
_BACKEND_LAUNCHES: weakref.WeakKeyDictionary[KubernetesWorkerBackend, WorkerLaunchConfig | None] = (
    weakref.WeakKeyDictionary()
)


def _launch(
    runtime_paths: RuntimePaths,
    *,
    applied_config: Config | None = None,
    tool_validation_snapshot: dict[str, dict[str, object]] | None = None,
    worker_grantable_credentials: frozenset[str] | None = None,
) -> WorkerLaunchConfig:
    """Build the launch of ``applied_config``, else of the config at ``runtime_paths``, else of an empty config."""
    config = applied_config
    if config is None:
        config = load_config(runtime_paths) if runtime_paths.config_path.exists() else Config()
    if worker_grantable_credentials is not None:
        config = config.model_copy(
            update={
                "defaults": config.defaults.model_copy(
                    update={"worker_grantable_credentials": sorted(worker_grantable_credentials)},
                ),
            },
        )
    return build_worker_launch_config(
        config,
        tool_validation_snapshot=deepcopy(tool_validation_snapshot or _TEST_TOOL_VALIDATION_SNAPSHOT),
    )


def _launch_of(backend: KubernetesWorkerBackend) -> WorkerLaunchConfig:
    launch = _BACKEND_LAUNCHES[backend]
    if launch is None:
        msg = "Dedicated workers need the applied config, and none is available yet."
        raise WorkerBackendError(msg)
    return launch


def _spec(
    worker_key: str,
    *,
    private_agent_names: frozenset[str] | None = None,
    mirror_grantable_credentials: bool = True,
    state_scope_worker_key: str | None = None,
    resource_profile: ScriptResourceProfileName | None = None,
) -> WorkerSpec:
    """Return one worker request; the backend provisions it from the launch applied when it holds the worker's lock."""
    return WorkerSpec(
        worker_key,
        private_agent_names=private_agent_names,
        mirror_grantable_credentials=mirror_grantable_credentials,
        state_scope_worker_key=state_scope_worker_key,
        resource_profile=resource_profile,
    )


@cache
def _default_storage_root() -> Path:
    """Share one storage root across default backends, as their contract hashes expect, outside the checkout."""
    return Path(tempfile.mkdtemp())


def _backend(
    *,
    idle_timeout_seconds: float = 60.0,
    worker_port: int = 8766,
    storage_subpath_prefix: str = "workers",
    storage_mount_path: str = "/app/worker",
    node_name: str | None = None,
    colocate_with_control_plane_node: bool = False,
    name_prefix: str = "mindroom-worker",
    owner_deployment_name: str | None = None,
    runtime_paths: RuntimePaths | None = None,
    tool_validation_snapshot: dict[str, dict[str, object]] | None = None,
    worker_grantable_credentials: frozenset[str] | None = None,
    resource_requests: dict[str, str] | None = None,
    resource_limits: dict[str, str] | None = None,
    script_resource_profiles: dict[str, dict[str, dict[str, str]]] | None = None,
    default_script_resource_profile: str = "small",
    extra_env: dict[str, str] | None = None,
    extra_annotations: dict[str, str] | None = None,
    enable_service_links: bool = False,
    auth_secret_name: str | None = None,
    reconcile_pod_templates: bool = True,
    seccomp_profile: _WorkerSeccompProfile | None = None,
    runtime_class_name: str | None = None,
    applied_config: Config | None = None,
    tmp_size_limit: str | None = None,
    user_resources: dict[str, dict[str, dict[str, str]]] | None = None,
    image_pull_secrets: tuple[str, ...] = (),
    image_pull_policy: str = "IfNotPresent",
) -> tuple[KubernetesWorkerBackend, _FakeAppsApi, _FakeCoreApi]:
    profile_config: dict[str, object] = {}
    if script_resource_profiles is not None:
        profile_config = {
            "script_resource_profiles": script_resource_profiles,
            "default_script_resource_profile": default_script_resource_profile,
        }
    config = KubernetesWorkerBackendConfig(
        namespace="chat",
        image="ghcr.io/mindroom-ai/mindroom:latest",
        image_pull_policy=image_pull_policy,
        worker_port=worker_port,
        service_account_name="mindroom-worker",
        storage_pvc_name="mindroom-storage",
        storage_mount_path=storage_mount_path,
        storage_subpath_prefix=storage_subpath_prefix,
        idle_timeout_seconds=idle_timeout_seconds,
        ready_timeout_seconds=5.0,
        name_prefix=name_prefix,
        node_name=node_name,
        colocate_with_control_plane_node=colocate_with_control_plane_node,
        extra_env=extra_env or {},
        extra_labels={"mindroom.ai/tenant": "test"},
        extra_annotations=extra_annotations or {},
        owner_deployment_name=owner_deployment_name,
        resource_requests=resource_requests if resource_requests is not None else {"memory": "256Mi", "cpu": "100m"},
        resource_limits=resource_limits if resource_limits is not None else {"memory": "1Gi", "cpu": "500m"},
        **profile_config,
        enable_service_links=enable_service_links,
        auth_secret_name=auth_secret_name,
        reconcile_pod_templates=reconcile_pod_templates,
        seccomp_profile=seccomp_profile,
        runtime_class_name=runtime_class_name,
        tmp_size_limit=tmp_size_limit,
        user_resources=user_resources or {},
        image_pull_secrets=image_pull_secrets,
    )
    resolved_runtime_paths = runtime_paths or resolve_primary_runtime_paths(
        config_path=Path("config.yaml"),
        storage_path=_default_storage_root(),
    )
    backend = KubernetesWorkerBackend(
        runtime_paths=resolved_runtime_paths,
        config=config,
        auth_token=_TEST_AUTH_TOKEN,
        storage_root=resolved_runtime_paths.storage_root,
        current_launch=lambda: _launch_of(backend),
    )
    _BACKEND_LAUNCHES[backend] = _launch(
        resolved_runtime_paths,
        applied_config=applied_config,
        tool_validation_snapshot=tool_validation_snapshot,
        worker_grantable_credentials=worker_grantable_credentials,
    )
    apps_api = _FakeAppsApi()
    core_api = _FakeCoreApi()
    if auth_secret_name is not None:
        core_api.secrets[auth_secret_name] = SimpleNamespace(
            metadata=SimpleNamespace(
                name=auth_secret_name,
                annotations={},
                labels={},
                generation=1,
                uid=f"{auth_secret_name}-uid",
            ),
            stringData={},
            data={},
        )
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
    _answer_worker_health(backend, lambda: WORKER_PROTOCOL_VERSION)
    return backend, apps_api, core_api


def _answer_worker_health(backend: KubernetesWorkerBackend, protocol: Callable[[], object]) -> list[str]:
    """Answer each health read through a worker Service with ``protocol()``, recording the workers read."""
    reads: list[str] = []

    def _read(_self: object, worker_id: str, *, timeout_seconds: float) -> httpx.Response:
        assert timeout_seconds > 0
        reads.append(worker_id)
        return httpx.Response(
            200,
            json={"status": "ok", "mindroom_version": "test", "worker_protocol": protocol()},
        )

    backend._resources.read_worker_health = MethodType(_read, backend._resources)
    return reads


def _install_real_elapsed_wait_for_ready(
    backend: KubernetesWorkerBackend,
    *,
    ready_after_seconds: float,
    ready_gate: threading.Event | None = None,
    poll_interval_seconds: float = 0.01,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    on_iteration: Callable[[float], None] | None = None,
) -> None:
    object.__setattr__(
        backend.config,
        "ready_timeout_seconds",
        max(backend.config.ready_timeout_seconds, ready_after_seconds + 1.0),
    )

    def _ready(
        self: object,
        deployment_name: str,
        *,
        timeout_seconds: float,
        deployment_ready_fn: object,
        on_poll_tick: Callable[[], None] | None = None,
    ) -> object:
        del deployment_ready_fn
        started_at = monotonic()
        deadline = started_at + timeout_seconds
        while True:
            elapsed_seconds = monotonic() - started_at
            if on_iteration is not None:
                on_iteration(elapsed_seconds)
            if elapsed_seconds >= ready_after_seconds and (ready_gate is None or ready_gate.is_set()):
                deployment = self.read_deployment(deployment_name)
                assert deployment is not None
                return deployment
            assert monotonic() < deadline
            if on_poll_tick is not None:
                on_poll_tick()
            sleep(poll_interval_seconds)

    backend._resources.wait_for_ready = MethodType(_ready, backend._resources)


def test_kubernetes_backend_loads_kubeconfig_from_runtime_env_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Primary client loading must use the same runtime KUBECONFIG selection as cleanup locators."""
    kubeconfig_path = tmp_path / "cluster.yaml"
    kubeconfig_path.write_text("apiVersion: v1\ncurrent-context: test\n", encoding="utf-8")
    env_path = tmp_path / ".env"
    env_path.write_text(f"KUBECONFIG={kubeconfig_path}\n", encoding="utf-8")
    runtime_paths = RuntimePaths(
        config_path=tmp_path / "config.yaml",
        config_dir=tmp_path,
        env_path=env_path,
        storage_root=tmp_path / "storage",
        control_state_root=tmp_path / "control",
        process_env={},
        env_file_values={"KUBECONFIG": str(kubeconfig_path)},
    )
    backend, _apps_api, _core_api = _backend(runtime_paths=runtime_paths)
    resources = backend._resources
    resources.apps_api = None
    resources.core_api = None
    resources.api_exception_cls = None
    kubernetes_config = SimpleNamespace(
        load_incluster_config=MagicMock(side_effect=RuntimeError("not in cluster")),
        load_kube_config=MagicMock(),
    )
    modules = {
        "kubernetes.config": kubernetes_config,
        "kubernetes.client": SimpleNamespace(AppsV1Api=MagicMock(), CoreV1Api=MagicMock()),
        "kubernetes.client.exceptions": SimpleNamespace(ApiException=_FakeApiError),
    }
    monkeypatch.setattr(
        kubernetes_resources_module.importlib,
        "import_module",
        lambda module_name: modules[module_name],
    )

    resources._load_clients()

    kubernetes_config.load_kube_config.assert_called_once_with(config_file=str(kubeconfig_path.resolve()))


def test_kubernetes_backend_ensures_worker_service_deployment_and_auth_secret(tmp_path: Path) -> None:  # noqa: PLR0915
    """Ensuring one worker should create runtime resources on shared storage."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=Path("config.yaml"),
        storage_path=tmp_path / "mindroom-test-storage",
    )
    backend, apps_api, core_api = _backend(
        runtime_paths=runtime_paths,
        owner_deployment_name="mindroom-demo",
    )
    worker_key = _TEST_SCOPED_WORKER_KEY_A

    handle = backend.ensure_worker(_spec(worker_key), now=10.0)

    assert handle.worker_key == worker_key
    assert handle.auth_token == worker_auth_token(_TEST_AUTH_TOKEN, worker_key)
    assert handle.auth_token != _TEST_AUTH_TOKEN
    assert handle.backend_name == "kubernetes"
    assert handle.endpoint.endswith("/api/sandbox-runner/execute")
    assert handle.debug_metadata["namespace"] == "chat"
    assert handle.debug_metadata["state_subpath"] == f"workers/{worker_dir_name(worker_key)}"
    assert handle.debug_metadata["service_name"] == handle.worker_id
    assert handle.status == "ready"

    assert len(core_api.created_bodies) == 1
    assert len(core_api.created_secret_bodies) == 1
    assert len(apps_api.created_bodies) == 1
    auth_secret = core_api.created_secret_bodies[0]
    deployment = apps_api.created_bodies[0]
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    env_by_name = {env["name"]: env for env in container["env"]}
    env_values = {env["name"]: env.get("value") for env in container["env"]}
    env_names = set(env_values)
    token_env = env_by_name["MINDROOM_SANDBOX_PROXY_TOKEN"]
    assert "MINDROOM_SANDBOX_DEDICATED_WORKER_KEY" in env_names
    assert "MINDROOM_SANDBOX_DEDICATED_WORKER_ROOT" in env_names
    assert "MINDROOM_STORAGE_PATH" in env_names
    assert "MINDROOM_SANDBOX_STARTUP_MANIFEST_PATH" in env_names
    assert "MINDROOM_SANDBOX_SHARED_STORAGE_ROOT" not in env_names
    # The runner must resolve executables from the image, not the worker-writable venv.
    assert "VIRTUAL_ENV" not in env_names
    assert "PATH" not in env_names
    assert "MINDROOM_SHARED_CREDENTIALS_PATH" in env_names
    assert token_env == {
        "name": "MINDROOM_SANDBOX_PROXY_TOKEN",
        "valueFrom": {
            "secretKeyRef": {
                "name": handle.worker_id,
                "key": "MINDROOM_SANDBOX_PROXY_TOKEN",
            },
        },
    }
    assert auth_secret["metadata"]["name"] == handle.worker_id
    assert auth_secret["stringData"] == {"MINDROOM_SANDBOX_PROXY_TOKEN": handle.auth_token}
    assert handle.auth_token not in json.dumps(deployment)
    assert deployment["spec"]["template"]["metadata"]["annotations"][_ANNOTATION_RUNNER_TOKEN_HASH]
    assert deployment["spec"]["template"]["metadata"]["annotations"][_ANNOTATION_RUNNER_TOKEN_HASH] != handle.auth_token
    assert env_values["MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE"] == "forkserver"
    assert env_values["MINDROOM_SANDBOX_RUNNER_PORT"] == "8766"
    manifest_path = env_values["MINDROOM_SANDBOX_STARTUP_MANIFEST_PATH"]
    assert manifest_path is not None
    startup_manifest = _load_startup_manifest(backend, worker_key=worker_key)
    validation_snapshot = startup_manifest["tool_validation_snapshot"]
    assert validation_snapshot == _TEST_TOOL_VALIDATION_SNAPSHOT
    expected_dedicated_root = f"/app/worker/workers/{worker_dir_name(worker_key)}"
    assert manifest_path == f"{expected_dedicated_root}/.runtime/startup_manifest.json"
    committed_runtime = deserialize_runtime_paths(startup_manifest["runtime_paths"])
    assert env_values["MINDROOM_STORAGE_PATH"] == expected_dedicated_root
    assert env_values["MINDROOM_SANDBOX_DEDICATED_WORKER_ROOT"] == expected_dedicated_root
    assert env_values["HOME"] == expected_dedicated_root
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
    assert auth_secret["metadata"]["ownerReferences"] == core_api.created_bodies[0]["metadata"]["ownerReferences"]
    template_annotations = deployment["spec"]["template"]["metadata"]["annotations"]
    assert set(template_annotations) == {
        _ANNOTATION_RUNNER_TOKEN_HASH,
        _ANNOTATION_STARTUP_MANIFEST_HASH,
        ANNOTATION_WORKER_KEY,
    }
    assert template_annotations[ANNOTATION_WORKER_KEY] == worker_key
    assert template_annotations[_ANNOTATION_RUNNER_TOKEN_HASH] == kubernetes_resources_module._worker_auth_token_hash(
        _TEST_AUTH_TOKEN,
        worker_key,
    )
    assert template_annotations[_ANNOTATION_STARTUP_MANIFEST_HASH] == startup_manifest_sha256(
        committed_runtime,
        tool_validation_snapshot=_TEST_TOOL_VALIDATION_SNAPSHOT,
        public_runtime=True,
    )
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
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }
    assert container["resources"]["requests"] == {"memory": "256Mi", "cpu": "100m"}
    assert container["resources"]["limits"] == {"memory": "1Gi", "cpu": "500m"}
    assert deployment["spec"]["template"]["spec"]["automountServiceAccountToken"] is False
    assert deployment["spec"]["template"]["spec"]["enableServiceLinks"] is False
    assert container["startupProbe"] == {
        "httpGet": {"path": "/healthz", "port": "api"},
        "periodSeconds": 5,
        "failureThreshold": 60,
    }


def test_kubernetes_worker_runs_the_sandbox_runner_under_tini(tmp_path: Path) -> None:
    """A container command replaces the image entrypoint, so tini must stay PID 1 to reap orphaned command children."""
    backend, apps_api, _core_api = _backend(
        runtime_paths=resolve_primary_runtime_paths(
            config_path=Path("config.yaml"),
            storage_path=tmp_path / "mindroom-test-storage",
        ),
    )

    backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    container = apps_api.created_bodies[0]["spec"]["template"]["spec"]["containers"][0]
    assert container["command"] == ["tini", "--", "/app/run-sandbox-runner.sh"]


def test_kubernetes_worker_tmp_has_an_eviction_size_limit(tmp_path: Path) -> None:
    """Tool code writing past 1 GiB to /tmp gets its own pod evicted instead of filling the node's ephemeral storage."""
    backend, apps_api, _core_api = _backend(
        runtime_paths=resolve_primary_runtime_paths(
            config_path=Path("config.yaml"),
            storage_path=tmp_path / "mindroom-test-storage",
        ),
    )

    backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    pod_spec = apps_api.created_bodies[0]["spec"]["template"]["spec"]
    volumes = {volume["name"]: volume for volume in pod_spec["volumes"]}
    assert volumes["worker-tmp"] == {"name": "worker-tmp", "emptyDir": {"sizeLimit": "1Gi"}}
    assert {"name": "worker-tmp", "mountPath": "/tmp"} in pod_spec["containers"][0]["volumeMounts"]  # noqa: S108
    assert pod_spec["containers"][0]["securityContext"]["readOnlyRootFilesystem"] is True


def test_kubernetes_worker_tmp_size_limit_also_sizes_container_ephemeral_storage(tmp_path: Path) -> None:
    """A configured /tmp size must not be undercut by a cluster-default container ephemeral-storage limit."""
    backend, apps_api, _core_api = _backend(
        runtime_paths=resolve_primary_runtime_paths(
            config_path=Path("config.yaml"),
            storage_path=tmp_path / "mindroom-test-storage",
        ),
        tmp_size_limit="8Gi",
    )

    backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    pod_spec = apps_api.created_bodies[0]["spec"]["template"]["spec"]
    volumes = {volume["name"]: volume for volume in pod_spec["volumes"]}
    assert volumes["worker-tmp"] == {"name": "worker-tmp", "emptyDir": {"sizeLimit": "8Gi"}}
    resources = pod_spec["containers"][0]["resources"]
    assert resources["requests"] == {"memory": "256Mi", "cpu": "100m", "ephemeral-storage": "8Gi"}
    assert resources["limits"] == {"memory": "1Gi", "cpu": "500m", "ephemeral-storage": "8Gi"}


def test_kubernetes_worker_localhost_seccomp_applies_only_to_main_container(tmp_path: Path) -> None:
    """A browser-compatible Localhost profile must not broaden pod-level or helper-container policy."""
    profile: _WorkerSeccompProfile = {"type": "Localhost", "localhostProfile": "profiles/worker-computer.json"}
    backend, apps_api, _core_api = _backend(
        runtime_paths=resolve_primary_runtime_paths(
            config_path=Path("config.yaml"),
            storage_path=tmp_path / "mindroom-test-storage",
        ),
        seccomp_profile=profile,
    )

    backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    pod_spec = apps_api.created_bodies[0]["spec"]["template"]["spec"]
    assert pod_spec["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    assert pod_spec["containers"][0]["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
        "seccompProfile": profile,
    }


def test_kubernetes_worker_runtime_class_preserves_sandbox_security_context(tmp_path: Path) -> None:
    """A RuntimeClass selects the pod runtime without weakening the existing sandbox settings."""
    backend, apps_api, _core_api = _backend(
        runtime_paths=resolve_primary_runtime_paths(
            config_path=Path("config.yaml"),
            storage_path=tmp_path / "mindroom-test-storage",
        ),
        runtime_class_name="sandboxed",
    )

    backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    pod_spec = apps_api.created_bodies[0]["spec"]["template"]["spec"]
    assert pod_spec["runtimeClassName"] == "sandboxed"
    assert pod_spec["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    assert pod_spec["containers"][0]["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }


def test_kubernetes_worker_omits_runtime_class_when_unset(tmp_path: Path) -> None:
    """The default pod template stays byte-compatible when no RuntimeClass is selected."""
    backend, apps_api, _core_api = _backend(
        runtime_paths=resolve_primary_runtime_paths(
            config_path=Path("config.yaml"),
            storage_path=tmp_path / "mindroom-test-storage",
        ),
    )

    backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    assert "runtimeClassName" not in apps_api.created_bodies[0]["spec"]["template"]["spec"]


def test_kubernetes_worker_pod_uses_image_pull_secrets_only_when_configured(tmp_path: Path) -> None:
    """Configured pull secrets recreate workers with them; unset secrets leave the pod template unchanged."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=Path("config.yaml"),
        storage_path=tmp_path / "mindroom-test-storage",
    )
    backend, apps_api, core_api = _backend(runtime_paths=runtime_paths)
    handle = backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=0.0)
    assert "imagePullSecrets" not in apps_api.created_bodies[0]["spec"]["template"]["spec"]

    updated_backend, _, _ = _backend(runtime_paths=runtime_paths, image_pull_secrets=("private-registry-pull",))
    _wire_fake_apis(updated_backend, apps_api, core_api)
    updated_backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    assert apps_api.deleted_names == [handle.worker_id]
    assert apps_api.created_bodies[-1]["spec"]["template"]["spec"]["imagePullSecrets"] == [
        {"name": "private-registry-pull"},
    ]


def test_kubernetes_worker_never_receives_credentials_encryption_key(tmp_path: Path) -> None:
    """Workers get no copy of the primary's credential encryption key in their env, Secret, or startup manifest."""
    encryption_key = base64.urlsafe_b64encode(b"0" * 32).decode("ascii")
    extra_env_key = base64.urlsafe_b64encode(b"1" * 32).decode("ascii")
    runtime_paths = resolve_primary_runtime_paths(
        config_path=Path("config.yaml"),
        storage_path=tmp_path / "mindroom-test-storage",
        process_env={
            CREDENTIALS_ENCRYPTION_KEY_ENV: encryption_key,
            "MINDROOM_KUBERNETES_WORKER_ENV_JSON": json.dumps({CREDENTIALS_ENCRYPTION_KEY_ENV: extra_env_key}),
        },
    )
    backend, apps_api, core_api = _backend(
        runtime_paths=runtime_paths,
        extra_env={CREDENTIALS_ENCRYPTION_KEY_ENV: extra_env_key},
    )
    worker_key = _TEST_SCOPED_WORKER_KEY_A

    backend.ensure_worker(_spec(worker_key), now=10.0)

    deployment = apps_api.created_bodies[0]
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    startup_manifest = _load_startup_manifest(backend, worker_key=worker_key)
    worker_objects = json.dumps([deployment, core_api.created_secret_bodies, startup_manifest])
    assert CREDENTIALS_ENCRYPTION_KEY_ENV not in {env["name"] for env in container["env"]}
    assert CREDENTIALS_ENCRYPTION_KEY_ENV not in core_api.created_secret_bodies[0]["stringData"]
    assert encryption_key not in worker_objects
    assert extra_env_key not in worker_objects
    assert hashlib.sha256(encryption_key.encode("utf-8")).hexdigest() not in worker_objects


def test_kubernetes_backend_config_signature_changes_with_credentials_encryption_key(tmp_path: Path) -> None:
    """Credential encryption key changes should invalidate cached Kubernetes managers without storing the key."""
    base_env = {
        "MINDROOM_KUBERNETES_WORKER_IMAGE": "test-image",
        "MINDROOM_KUBERNETES_WORKER_STORAGE_PVC_NAME": "test-pvc",
    }
    first_key = base64.urlsafe_b64encode(b"1" * 32).decode("ascii")
    second_key = base64.urlsafe_b64encode(b"2" * 32).decode("ascii")
    first_runtime_paths = resolve_primary_runtime_paths(
        config_path=Path("config.yaml"),
        storage_path=tmp_path / "mindroom-test-storage",
        process_env={**base_env, CREDENTIALS_ENCRYPTION_KEY_ENV: first_key},
    )
    second_runtime_paths = resolve_primary_runtime_paths(
        config_path=Path("config.yaml"),
        storage_path=tmp_path / "mindroom-test-storage",
        process_env={**base_env, CREDENTIALS_ENCRYPTION_KEY_ENV: second_key},
    )
    disabled_runtime_paths = resolve_primary_runtime_paths(
        config_path=Path("config.yaml"),
        storage_path=tmp_path / "mindroom-test-storage",
        process_env=base_env,
    )

    first_signature = configure_kubernetes_worker_backend(
        first_runtime_paths,
        auth_token=_TEST_AUTH_TOKEN,
        storage_root=first_runtime_paths.storage_root,
    ).signature
    second_signature = configure_kubernetes_worker_backend(
        second_runtime_paths,
        auth_token=_TEST_AUTH_TOKEN,
        storage_root=second_runtime_paths.storage_root,
    ).signature
    disabled_signature = configure_kubernetes_worker_backend(
        disabled_runtime_paths,
        auth_token=_TEST_AUTH_TOKEN,
        storage_root=disabled_runtime_paths.storage_root,
    ).signature

    first_key_hash = hashlib.sha256(first_key.encode("utf-8")).hexdigest()
    assert first_signature != second_signature
    assert first_signature != disabled_signature
    assert first_key_hash in first_signature
    assert first_key not in "\n".join(first_signature)
    assert second_key not in "\n".join(second_signature)


def test_kubernetes_backend_can_use_one_precreated_auth_secret(tmp_path: Path) -> None:
    """Shared-namespace charts should need RBAC only for one tenant-owned Secret."""
    encryption_key = base64.urlsafe_b64encode(b"0" * 32).decode("ascii")
    runtime_paths = resolve_primary_runtime_paths(
        config_path=Path("config.yaml"),
        storage_path=tmp_path / "mindroom-test-storage",
        process_env={CREDENTIALS_ENCRYPTION_KEY_ENV: encryption_key},
    )
    auth_secret_name = "mindroom-worker-auth-demo"  # noqa: S105
    backend, apps_api, core_api = _backend(runtime_paths=runtime_paths, auth_secret_name=auth_secret_name)
    worker_key = _TEST_SCOPED_WORKER_KEY_A

    handle = backend.ensure_worker(_spec(worker_key), now=10.0)

    deployment = apps_api.created_bodies[0]
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    env_by_name = {env["name"]: env for env in container["env"]}
    assert env_by_name["MINDROOM_SANDBOX_PROXY_TOKEN"] == {
        "name": "MINDROOM_SANDBOX_PROXY_TOKEN",
        "valueFrom": {
            "secretKeyRef": {
                "name": auth_secret_name,
                "key": handle.worker_id,
            },
        },
    }
    assert CREDENTIALS_ENCRYPTION_KEY_ENV not in env_by_name
    assert encryption_key not in json.dumps(deployment)
    assert core_api.created_secret_bodies == []
    expected_secret_data = _encoded_secret_data({handle.worker_id: handle.auth_token})
    assert core_api.patched_secret_bodies[0] == (
        auth_secret_name,
        {"data": {**expected_secret_data, f"{handle.worker_id}.credentials-encryption-key": None}},
    )
    assert core_api.secrets[auth_secret_name].data == expected_secret_data


def test_kubernetes_backend_cleanup_removes_only_own_key_from_tenant_auth_secret(tmp_path: Path) -> None:
    """Cleaning up one idle worker should null out only its key in the shared tenant Secret."""
    encryption_key = base64.urlsafe_b64encode(b"0" * 32).decode("ascii")
    runtime_paths = resolve_primary_runtime_paths(
        config_path=Path("config.yaml"),
        storage_path=tmp_path / "mindroom-test-storage",
        process_env={CREDENTIALS_ENCRYPTION_KEY_ENV: encryption_key},
    )
    auth_secret_name = "mindroom-worker-auth-demo"  # noqa: S105
    backend, _apps_api, core_api = _backend(runtime_paths=runtime_paths, auth_secret_name=auth_secret_name)
    other_worker_id = "mindroom-worker-other"
    core_api.secrets[auth_secret_name].data = {other_worker_id: "preexisting"}
    handle = backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    backend.cleanup_idle_workers(now=80.0)

    assert core_api.deleted_secret_names == []
    delete_patch = (
        auth_secret_name,
        {"data": {handle.worker_id: None, f"{handle.worker_id}.credentials-encryption-key": None}},
    )
    assert delete_patch in core_api.patched_secret_bodies
    tenant_secret = core_api.secrets[auth_secret_name]
    assert handle.worker_id not in tenant_secret.data
    assert f"{handle.worker_id}.credentials-encryption-key" not in tenant_secret.data
    assert tenant_secret.data[other_worker_id] == "preexisting"


def test_kubernetes_backend_retires_only_one_exact_run_worker_idempotently(tmp_path: Path) -> None:
    """Script retirement removes one run worker while preserving its canonical worker."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=Path("config.yaml"),
        storage_path=tmp_path / "mindroom-test-storage",
    )
    backend, apps_api, core_api = _backend(runtime_paths=runtime_paths)
    base_key = "v1:test:user_agent:@alice:example.test:watcher"
    run_key = script_worker_key_for_run(base_key, f"script-{'c' * 32}")
    ordinary = backend.ensure_worker(_spec(base_key, private_agent_names=frozenset()), now=1.0)
    retired = backend.ensure_worker(_spec(run_key, private_agent_names=frozenset()), now=1.0)
    ordinary_root = backend.storage_root / backend._state_subpath(base_key)
    retired_root = backend.storage_root / backend._state_subpath(run_key)

    backend.retire_worker(run_key)
    backend.retire_worker(run_key)

    assert retired.worker_id not in apps_api.deployments
    assert retired.worker_id not in core_api.services
    assert retired.worker_id not in core_api.secrets
    assert retired_root.exists() is False
    assert apps_api.deleted_propagation_policies[-2:] == ["Foreground", "Foreground"]
    assert ordinary.worker_id in apps_api.deployments
    assert ordinary.worker_id in core_api.services
    assert ordinary.worker_id in core_api.secrets
    assert ordinary_root.is_dir()
    assert [handle.worker_key for handle in backend.list_workers()] == [base_key]


@pytest.mark.parametrize("deleted_elsewhere", [False, True])
def test_kubernetes_retirement_waits_for_the_deployment_to_be_gone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    deleted_elsewhere: bool,
) -> None:
    """Retirement returns once the Deployment is gone, also when another actor deleted it first."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=Path("config.yaml"),
        storage_path=tmp_path / "mindroom-test-storage",
    )
    backend, apps_api, _core_api = _backend(runtime_paths=runtime_paths)
    run_key = script_worker_key_for_run("v1:test:user_agent:@alice:example.test:watcher", f"script-{'d' * 32}")
    retired = backend.ensure_worker(_spec(run_key, private_agent_names=frozenset()), now=1.0)
    if deleted_elsewhere:

        def delete_already_gone(name: str, *_args: object, **_kwargs: object) -> None:
            apps_api.deployments.pop(name, None)
            raise _FakeApiError(404)

        monkeypatch.setattr(apps_api, "delete_namespaced_deployment", delete_already_gone)
    else:
        apps_api.delete_read_lag_by_name[retired.worker_id] = 1

    backend.retire_worker(run_key)

    assert retired.worker_id not in apps_api.deployments
    assert (backend.storage_root / backend._state_subpath(run_key)).exists() is False


def test_kubernetes_backend_refuses_retirement_when_deployment_key_mismatches(tmp_path: Path) -> None:
    """A colliding Deployment name cannot authorize deletion for another worker key."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=Path("config.yaml"),
        storage_path=tmp_path / "mindroom-test-storage",
    )
    backend, apps_api, core_api = _backend(runtime_paths=runtime_paths)
    run_key = script_worker_key_for_run(
        "v1:test:user_agent:@alice:example.test:watcher",
        f"script-{'d' * 32}",
    )
    handle = backend.ensure_worker(_spec(run_key, private_agent_names=frozenset()), now=1.0)
    apps_api.deployments[handle.worker_id].metadata.annotations[ANNOTATION_WORKER_KEY] = "another-worker"
    state_root = backend.storage_root / backend._state_subpath(run_key)

    with pytest.raises(WorkerBackendError, match="does not match retirement key"):
        backend.retire_worker(run_key)

    assert handle.worker_id in apps_api.deployments
    assert handle.worker_id in core_api.services
    assert handle.worker_id in core_api.secrets
    assert state_root.is_dir()


def test_kubernetes_backend_refuses_retirement_without_exact_state_identity(tmp_path: Path) -> None:
    """Run state must identify its exact worker key before recursive deletion."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=Path("config.yaml"),
        storage_path=tmp_path / "mindroom-test-storage",
    )
    backend, apps_api, core_api = _backend(runtime_paths=runtime_paths)
    run_key = script_worker_key_for_run(
        "v1:test:user_agent:@alice:example.test:watcher",
        f"script-{'e' * 32}",
    )
    handle = backend.ensure_worker(_spec(run_key, private_agent_names=frozenset()), now=1.0)
    state_root = backend.storage_root / backend._state_subpath(run_key)
    sandbox_startup_manifest_path(state_root).write_text("{}", encoding="utf-8")

    with pytest.raises(WorkerBackendError, match="exact worker key"):
        backend.retire_worker(run_key)

    assert handle.worker_id in apps_api.deployments
    assert handle.worker_id in core_api.services
    assert handle.worker_id in core_api.secrets
    assert state_root.is_dir()


def test_kubernetes_backend_reapply_removes_worker_secret_key(tmp_path: Path) -> None:
    """Reapplying a worker Secret removes the encryption key an earlier release stored there."""
    encryption_key = base64.urlsafe_b64encode(b"0" * 32).decode("ascii")
    runtime_paths = resolve_primary_runtime_paths(
        config_path=Path("config.yaml"),
        storage_path=tmp_path / "mindroom-test-storage",
        process_env={CREDENTIALS_ENCRYPTION_KEY_ENV: encryption_key},
    )
    backend, _apps_api, core_api = _backend(runtime_paths=runtime_paths)
    handle = backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)
    core_api.secrets[handle.worker_id].data |= _encoded_secret_data({CREDENTIALS_ENCRYPTION_KEY_ENV: encryption_key})
    backend._invalidate_ready_worker(_TEST_SCOPED_WORKER_KEY_A)

    backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=20.0)

    assert core_api.secrets[handle.worker_id].data == _encoded_secret_data(
        {"MINDROOM_SANDBOX_PROXY_TOKEN": handle.auth_token},
    )


def test_kubernetes_backend_reapply_removes_shared_secret_key(tmp_path: Path) -> None:
    """Reapplying a shared Secret entry removes the worker encryption key an earlier release stored there."""
    encryption_key = base64.urlsafe_b64encode(b"0" * 32).decode("ascii")
    runtime_paths = resolve_primary_runtime_paths(
        config_path=Path("config.yaml"),
        storage_path=tmp_path / "mindroom-test-storage",
        process_env={CREDENTIALS_ENCRYPTION_KEY_ENV: encryption_key},
    )
    auth_secret_name = "mindroom-worker-auth-demo"  # noqa: S105
    backend, _apps_api, core_api = _backend(runtime_paths=runtime_paths, auth_secret_name=auth_secret_name)
    worker_id = backend._worker_id(_TEST_SCOPED_WORKER_KEY_A)
    encryption_secret_key = f"{worker_id}.credentials-encryption-key"
    core_api.secrets[auth_secret_name].data = _encoded_secret_data({encryption_secret_key: encryption_key})

    handle = backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    assert core_api.secrets[auth_secret_name].data == _encoded_secret_data({worker_id: handle.auth_token})


def test_kubernetes_backend_startup_failure_removes_key_from_tenant_auth_secret(tmp_path: Path) -> None:
    """Failing to apply the worker Deployment should release the tenant-Secret key."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=Path("config.yaml"),
        storage_path=tmp_path / "mindroom-test-storage",
    )
    auth_secret_name = "mindroom-worker-auth-demo"  # noqa: S105
    backend, _apps_api, core_api = _backend(runtime_paths=runtime_paths, auth_secret_name=auth_secret_name)

    def apply_deployment_with_failure(**_kwargs: object) -> object:
        msg = "deployment apply failed"
        raise WorkerBackendError(msg)

    backend._resources.apply_deployment = apply_deployment_with_failure  # type: ignore[method-assign]
    worker_id = backend._worker_id(_TEST_SCOPED_WORKER_KEY_A)

    with pytest.raises(WorkerBackendError):
        backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    delete_patch = (
        auth_secret_name,
        {"data": {worker_id: None, f"{worker_id}.credentials-encryption-key": None}},
    )
    assert delete_patch in core_api.patched_secret_bodies
    assert worker_id not in core_api.secrets[auth_secret_name].data


def test_kubernetes_backend_recreate_failure_removes_orphaned_tenant_auth_secret_key(tmp_path: Path) -> None:
    """If recreate deletes the worker Deployment before failing, the shared Secret key must be released."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=Path("config.yaml"),
        storage_path=tmp_path / "mindroom-test-storage",
    )
    auth_secret_name = "mindroom-worker-auth-demo"  # noqa: S105
    backend, apps_api, core_api = _backend(
        runtime_paths=runtime_paths,
        auth_secret_name=auth_secret_name,
        idle_timeout_seconds=600.0,
    )
    handle = backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)
    deployment = apps_api.deployments[handle.worker_id]
    deployment.metadata.annotations[ANNOTATION_TEMPLATE_HASH] = "stale"

    def recreate_with_failure(deployment_name: str, _manifest: dict[str, object], *, timeout_seconds: float) -> None:
        _ = timeout_seconds
        apps_api.delete_namespaced_deployment(deployment_name, backend.config.namespace)
        msg = "deployment recreate failed"
        raise WorkerBackendError(msg)

    backend._resources._recreate_deployment = recreate_with_failure  # type: ignore[method-assign]

    with pytest.raises(WorkerBackendError, match="deployment recreate failed"):
        backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=310.0)

    assert handle.worker_id not in apps_api.deployments
    assert handle.worker_id not in core_api.secrets[auth_secret_name].stringData
    assert handle.worker_id not in core_api.secrets[auth_secret_name].data


def test_kubernetes_backend_shared_auth_secret_cleanup_ignores_missing_secret(tmp_path: Path) -> None:
    """Cleanup should stay idempotent if the shared tenant auth Secret was already removed."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=Path("config.yaml"),
        storage_path=tmp_path / "mindroom-test-storage",
    )
    auth_secret_name = "mindroom-worker-auth-demo"  # noqa: S105
    backend, _apps_api, core_api = _backend(runtime_paths=runtime_paths, auth_secret_name=auth_secret_name)
    handle = backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)
    core_api.secrets.pop(auth_secret_name)

    cleaned = backend.cleanup_idle_workers(now=80.0)

    assert [worker.worker_key for worker in cleaned] == [_TEST_SCOPED_WORKER_KEY_A]
    assert (
        auth_secret_name,
        {"data": {handle.worker_id: None, f"{handle.worker_id}.credentials-encryption-key": None}},
    ) in core_api.patched_secret_bodies


def test_kubernetes_backend_recreates_worker_when_startup_manifest_changes(tmp_path: Path) -> None:
    """Changing startup state should force a worker Deployment recreate."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=Path("config.yaml"),
        storage_path=tmp_path / "mindroom-test-storage",
    )
    initial_snapshot = deepcopy(_TEST_TOOL_VALIDATION_SNAPSHOT)
    updated_snapshot = deepcopy(_TEST_TOOL_VALIDATION_SNAPSHOT)
    updated_snapshot["search"] = {
        "config_fields": ["engine"],
        "agent_override_fields": [],
        "authored_override_validator": "default",
        "runtime_loadable": True,
    }
    backend, apps_api, core_api = _backend(
        runtime_paths=runtime_paths,
        owner_deployment_name="mindroom-demo",
        tool_validation_snapshot=initial_snapshot,
    )
    worker_key = _TEST_SCOPED_WORKER_KEY_A

    handle = backend.ensure_worker(_spec(worker_key), now=10.0)

    initial_deployment = apps_api.created_bodies[0]
    initial_manifest_hash = initial_deployment["spec"]["template"]["metadata"]["annotations"][
        _ANNOTATION_STARTUP_MANIFEST_HASH
    ]

    updated_backend, _, _ = _backend(
        runtime_paths=runtime_paths,
        owner_deployment_name="mindroom-demo",
        tool_validation_snapshot=updated_snapshot,
    )
    updated_backend._resources.apps_api = apps_api
    updated_backend._resources.core_api = core_api
    updated_backend._resources.api_exception_cls = _FakeApiError

    updated_backend.ensure_worker(_spec(worker_key), now=20.0)

    assert apps_api.deleted_names == [handle.worker_id]
    assert len(apps_api.created_bodies) == 2
    updated_deployment = apps_api.created_bodies[1]
    updated_manifest_hash = updated_deployment["spec"]["template"]["metadata"]["annotations"][
        _ANNOTATION_STARTUP_MANIFEST_HASH
    ]
    assert updated_manifest_hash != initial_manifest_hash


def test_kubernetes_backend_commits_parent_runtime_env_into_worker_payload(tmp_path: Path) -> None:
    """Dedicated worker startup payloads should keep worker control env while denying ambient provider and arbitrary runtime env."""
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.yaml"
    credentials_path = tmp_path / "google-credentials.json"
    credentials_path.write_text('{"type":"service_account"}\n', encoding="utf-8")
    storage_mount_path = tmp_path / "worker-storage"
    storage_mount_path.mkdir()
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-6-astra\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    (config_dir / ".env").write_text(
        (
            "MINDROOM_NAMESPACE=alpha1234\n"
            "MATRIX_HOMESERVER=http://dotenv-hs\n"
            "MATRIX_SERVER_NAME=alpha.example\n"
            "MATRIX_ACCESS_TOKEN=matrix-access-secret\n"
            "MATRIX_REGISTRATION_TOKEN=matrix-registration-secret\n"
            "CUSTOMER_ID=tenant-123\n"
            "ACCOUNT_ID=account-456\n"
            f"GOOGLE_APPLICATION_CREDENTIALS={credentials_path}\n"
            "GOOGLE_CLOUD_PROJECT=demo-project\n"
            "GOOGLE_CLOUD_LOCATION=us-central1\n"
            "OPENAI_BASE_URL=http://example.invalid/v1\n"
            "CUSTOM_API_TOKEN=custom-secret\n"
            "ANTHROPIC_API_KEY=sk-secret\n"
        ),
        encoding="utf-8",
    )
    runtime_paths = resolve_primary_runtime_paths(
        config_path=config_path,
        process_env={
            "MINDROOM_SANDBOX_CREDENTIAL_POLICY_JSON": '{"shell":["github"]}',
            "MINDROOM_SANDBOX_PROXY_TOKEN": "test-token",
            "MINDROOM_SANDBOX_PROXY_TOOLS": "*",
            "MINDROOM_SANDBOX_PROXY_URL": "http://runner.example.invalid",
            "MINDROOM_SANDBOX_SHARED_STORAGE_ROOT": str(tmp_path / "primary-shared-root"),
            "MINDROOM_LOCAL_CLIENT_SECRET": "client-secret",
        },
    )
    backend, _apps_api, _core_api = _backend(
        runtime_paths=runtime_paths,
        storage_mount_path=str(storage_mount_path),
    )

    backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    committed_runtime = deserialize_runtime_paths(
        _load_startup_manifest(backend, worker_key=_TEST_SCOPED_WORKER_KEY_A)["runtime_paths"],
    )
    state_subpath = Path("workers") / worker_dir_name(_TEST_SCOPED_WORKER_KEY_A)
    local_credentials_path = runtime_paths.storage_root / state_subpath / ".runtime" / credentials_path.name

    assert committed_runtime.env_value("MINDROOM_NAMESPACE") == "alpha1234"
    assert committed_runtime.env_value("MATRIX_HOMESERVER") == "http://dotenv-hs"
    assert committed_runtime.env_value("MATRIX_SERVER_NAME") == "alpha.example"
    assert committed_runtime.env_value("MATRIX_ACCESS_TOKEN") is None
    assert committed_runtime.env_value("MATRIX_REGISTRATION_TOKEN") is None
    assert committed_runtime.env_value("CUSTOMER_ID") == "tenant-123"
    assert committed_runtime.env_value("ACCOUNT_ID") == "account-456"
    assert committed_runtime.env_value("GOOGLE_APPLICATION_CREDENTIALS") is None
    assert committed_runtime.env_value("GOOGLE_CLOUD_PROJECT") is None
    assert committed_runtime.env_value("GOOGLE_CLOUD_LOCATION") is None
    assert committed_runtime.env_value("OPENAI_BASE_URL") is None
    assert committed_runtime.env_value("CUSTOM_API_TOKEN") is None
    assert committed_runtime.env_value("ANTHROPIC_API_KEY") is None
    assert committed_runtime.env_value("MINDROOM_SANDBOX_CREDENTIAL_POLICY_JSON") is None
    assert committed_runtime.env_value("MINDROOM_SANDBOX_PROXY_TOKEN") is None
    assert committed_runtime.env_value("MINDROOM_SANDBOX_PROXY_TOOLS") is None
    assert committed_runtime.env_value("MINDROOM_SANDBOX_PROXY_URL") is None
    assert committed_runtime.env_value("MINDROOM_SANDBOX_SHARED_STORAGE_ROOT") is None
    assert committed_runtime.env_value("MINDROOM_LOCAL_CLIENT_SECRET") is None
    assert not local_credentials_path.exists()


def test_kubernetes_backend_uses_provided_validation_snapshot(tmp_path: Path) -> None:
    """Worker env should reflect the authoritative snapshot passed into the backend."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=Path("config.yaml"),
        storage_path=tmp_path / "mindroom-test-storage",
    )
    custom_snapshot = {
        "worker_only": {
            "config_fields": [],
            "agent_override_fields": [],
            "authored_override_validator": "default",
            "runtime_loadable": False,
        },
    }
    backend, apps_api, _core_api = _backend(
        runtime_paths=runtime_paths,
        owner_deployment_name="mindroom-demo",
        tool_validation_snapshot=custom_snapshot,
    )

    backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    deployment = apps_api.created_bodies[0]
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    env_values = {env["name"]: env.get("value") for env in container["env"]}

    assert env_values["MINDROOM_SANDBOX_STARTUP_MANIFEST_PATH"] is not None
    assert (
        _load_startup_manifest(backend, worker_key=_TEST_SCOPED_WORKER_KEY_A)["tool_validation_snapshot"]
        == custom_snapshot
    )


def test_kubernetes_backend_drops_host_local_adc_path_when_not_mounted(tmp_path: Path) -> None:
    """Dedicated worker payloads must not serialize unusable host-local ADC paths."""
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-6-astra\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_primary_runtime_paths(
        config_path=config_path,
        process_env={"GOOGLE_APPLICATION_CREDENTIALS": "/host/path/adc.json"},
    )
    backend, _apps_api, _core_api = _backend(
        runtime_paths=runtime_paths,
        storage_mount_path=str(tmp_path / "not-mounted-storage"),
    )

    backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    committed_runtime = deserialize_runtime_paths(
        _load_startup_manifest(backend, worker_key=_TEST_SCOPED_WORKER_KEY_A)["runtime_paths"],
    )

    assert committed_runtime.env_value("GOOGLE_APPLICATION_CREDENTIALS") is None


@pytest.mark.parametrize(
    "config_relative_path",
    ["content-bundles/team-config/agent-config.yaml", "team-config/content/environments/prod/agent-config.yaml"],
)
def test_kubernetes_worker_never_receives_the_primary_config(tmp_path: Path, config_relative_path: str) -> None:
    """Workers keep the primary's config path for resolving snapshot paths but mount nothing that holds its config."""
    config_path = tmp_path / "storage" / config_relative_path
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-6-astra\n    api_key: sk-config-secret\n"
        "agents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    (config_path.parent / ".env").write_text("OPENAI_API_KEY=sk-env-secret\n", encoding="utf-8")
    runtime_paths = resolve_primary_runtime_paths(config_path=config_path, storage_path=tmp_path / "storage")
    backend, apps_api, _core_api = _backend(runtime_paths=runtime_paths, storage_mount_path="/app/agent_data")

    backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    deployment = apps_api.created_bodies[0]
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    env_by_name = {env["name"]: env for env in container["env"]}
    worker_config_path = f"/app/agent_data/{config_relative_path}"
    config_top_level = f"/app/agent_data/{config_relative_path.split('/', maxsplit=1)[0]}"
    assert env_by_name["MINDROOM_CONFIG_PATH"]["value"] == str(config_path.resolve())
    assert not any(
        worker_config_path.startswith(mount["mountPath"]) or mount["mountPath"].startswith(config_top_level)
        for mount in container["volumeMounts"]
    )
    assert deployment["spec"]["template"]["spec"]["volumes"] == [
        {"name": "worker-storage", "persistentVolumeClaim": {"claimName": "mindroom-storage"}},
        {"name": "worker-tmp", "emptyDir": {"sizeLimit": "1Gi"}},
    ]
    committed_runtime = deserialize_runtime_paths(
        _load_startup_manifest(backend, worker_key=_TEST_SCOPED_WORKER_KEY_A)["runtime_paths"],
    )
    assert committed_runtime.config_path == config_path.resolve()
    assert "sk-config-secret" not in json.dumps(deployment)
    assert "sk-env-secret" not in json.dumps(_load_startup_manifest(backend, worker_key=_TEST_SCOPED_WORKER_KEY_A))


def test_kubernetes_backend_availability_uses_runtime_env_values(tmp_path: Path) -> None:
    """Kubernetes backend availability should honor the explicit runtime context."""
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-6-astra\nagents: {}\nrouter:\n  model: default\n",
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

    assert worker_backend_kind(runtime_paths) is WorkerBackendKind.KUBERNETES
    assert runtime_paths.env_value("MINDROOM_KUBERNETES_WORKER_IMAGE") == "test-image"
    assert configured_primary_worker_manager_identity(runtime_paths) is not None


def test_kubernetes_backend_config_resource_envs_override_defaults(tmp_path: Path) -> None:
    """Resource request/limit env vars override the built-in defaults."""
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-6-astra\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    (config_dir / ".env").write_text(
        (
            "MINDROOM_WORKER_BACKEND=kubernetes\n"
            "MINDROOM_KUBERNETES_WORKER_IMAGE=test-image\n"
            "MINDROOM_KUBERNETES_WORKER_STORAGE_PVC_NAME=test-pvc\n"
            "MINDROOM_KUBERNETES_WORKER_MEMORY_REQUEST=2Gi\n"
            "MINDROOM_KUBERNETES_WORKER_MEMORY_LIMIT=8Gi\n"
            "MINDROOM_KUBERNETES_WORKER_CPU_REQUEST=500m\n"
            "MINDROOM_KUBERNETES_WORKER_CPU_LIMIT=2\n"
            "MINDROOM_KUBERNETES_WORKER_TMP_SIZE_LIMIT=8Gi\n"
        ),
        encoding="utf-8",
    )
    runtime_paths = resolve_primary_runtime_paths(config_path=config_path)

    config = KubernetesWorkerBackendConfig.from_runtime(runtime_paths)

    assert config.resource_requests == {"memory": "2Gi", "cpu": "500m"}
    assert config.resource_limits == {"memory": "8Gi", "cpu": "2"}
    assert config.tmp_size_limit == "8Gi"


def test_kubernetes_backend_config_resources_default_when_env_unset(tmp_path: Path) -> None:
    """Resource defaults kick in when no override env vars are present."""
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-6-astra\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    (config_dir / ".env").write_text(
        (
            "MINDROOM_WORKER_BACKEND=kubernetes\n"
            "MINDROOM_KUBERNETES_WORKER_IMAGE=test-image\n"
            "MINDROOM_KUBERNETES_WORKER_STORAGE_PVC_NAME=test-pvc\n"
        ),
        encoding="utf-8",
    )
    runtime_paths = resolve_primary_runtime_paths(config_path=config_path)

    config = KubernetesWorkerBackendConfig.from_runtime(runtime_paths)

    assert config.resource_requests == {"memory": "256Mi", "cpu": "100m"}
    assert config.resource_limits == {"memory": "1Gi", "cpu": "500m"}
    assert config.tmp_size_limit is None
    assert config.user_resources == {}
    assert config.enable_service_links is False


def test_kubernetes_backend_config_reads_all_three_script_resource_profiles(tmp_path: Path) -> None:
    """Runtime configuration carries exact administrator-owned quantities and the selected default."""
    profiles = {
        "small": {
            "requests": {"cpu": "50m", "memory": "128Mi"},
            "limits": {"cpu": "250m", "memory": "512Mi"},
        },
        "standard": {
            "requests": {"cpu": "200m", "memory": "512Mi"},
            "limits": {"cpu": "1", "memory": "2Gi"},
        },
        "large": {
            "requests": {"cpu": "750m", "memory": "2Gi"},
            "limits": {"cpu": "3", "memory": "10Gi"},
        },
    }
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "config.yaml"
    config_path.write_text("agents: {}\n", encoding="utf-8")
    (config_dir / ".env").write_text(
        "\n".join(
            (
                "MINDROOM_WORKER_BACKEND=kubernetes",
                "MINDROOM_KUBERNETES_WORKER_IMAGE=test-image",
                "MINDROOM_KUBERNETES_WORKER_STORAGE_PVC_NAME=test-pvc",
                "MINDROOM_KUBERNETES_DEFAULT_SCRIPT_RESOURCE_PROFILE=standard",
                f"MINDROOM_KUBERNETES_SCRIPT_RESOURCE_PROFILES_JSON={json.dumps(profiles)}",
            ),
        ),
        encoding="utf-8",
    )

    config = KubernetesWorkerBackendConfig.from_runtime(resolve_primary_runtime_paths(config_path=config_path))

    assert config.default_script_resource_profile == "standard"
    assert config.script_resource_profiles == profiles


def test_kubernetes_backend_config_rejects_partial_script_resource_profiles(tmp_path: Path) -> None:
    """Operators cannot accidentally expose a profile name without bounded quantities."""
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "config.yaml"
    config_path.write_text("agents: {}\n", encoding="utf-8")
    (config_dir / ".env").write_text(
        "MINDROOM_WORKER_BACKEND=kubernetes\n"
        "MINDROOM_KUBERNETES_WORKER_IMAGE=test-image\n"
        "MINDROOM_KUBERNETES_WORKER_STORAGE_PVC_NAME=test-pvc\n"
        'MINDROOM_KUBERNETES_SCRIPT_RESOURCE_PROFILES_JSON={"small": {}}',
        encoding="utf-8",
    )

    with pytest.raises(WorkerBackendError, match="exactly small, standard, and large"):
        KubernetesWorkerBackendConfig.from_runtime(resolve_primary_runtime_paths(config_path=config_path))


def test_kubernetes_backend_config_reads_user_resource_overrides(tmp_path: Path) -> None:
    """Per-user overrides may set any subset of cpu and memory requests and limits."""
    overrides = {
        "@alice:example.org": {"limits": {"memory": "4Gi"}},
        "@bob:example.org": {"requests": {"cpu": "1"}, "limits": {"cpu": "2", "memory": "8Gi"}},
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\n", encoding="utf-8")
    runtime_paths = resolve_primary_runtime_paths(
        config_path=config_path,
        process_env={
            "MINDROOM_WORKER_BACKEND": "kubernetes",
            "MINDROOM_KUBERNETES_WORKER_IMAGE": "test-image",
            "MINDROOM_KUBERNETES_WORKER_STORAGE_PVC_NAME": "test-pvc",
            "MINDROOM_KUBERNETES_WORKER_USER_RESOURCES_JSON": json.dumps(overrides),
        },
    )

    config = KubernetesWorkerBackendConfig.from_runtime(runtime_paths)

    assert config.user_resources == overrides


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("[]", "must contain a JSON object"),
        ('{"alice": {"limits": {"memory": "4Gi"}}}', "keys must be Matrix user IDs"),
        ('{"@alice:example.org": {"limit": {"memory": "4Gi"}}}', "must define requests, limits, or both"),
        ('{"@alice:example.org": {}}', "must define requests, limits, or both"),
        ('{"@alice:example.org": {"limits": {"ephemeral-storage": "4Gi"}}}', "non-empty cpu or memory"),
        ('{"@alice:example.org": {"limits": {"memory": 4}}}', "non-empty cpu or memory"),
    ],
)
def test_kubernetes_backend_config_rejects_invalid_user_resource_overrides(
    tmp_path: Path,
    raw: str,
    message: str,
) -> None:
    """Overrides stay as strict as the script resource profiles."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\n", encoding="utf-8")
    runtime_paths = resolve_primary_runtime_paths(
        config_path=config_path,
        process_env={
            "MINDROOM_WORKER_BACKEND": "kubernetes",
            "MINDROOM_KUBERNETES_WORKER_IMAGE": "test-image",
            "MINDROOM_KUBERNETES_WORKER_STORAGE_PVC_NAME": "test-pvc",
            "MINDROOM_KUBERNETES_WORKER_USER_RESOURCES_JSON": raw,
        },
    )

    with pytest.raises(WorkerBackendError, match=message):
        KubernetesWorkerBackendConfig.from_runtime(runtime_paths)


def test_kubernetes_backend_config_allows_service_links_override(tmp_path: Path) -> None:
    """Worker service-link env injection remains opt-in."""
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-6-astra\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    (config_dir / ".env").write_text(
        (
            "MINDROOM_WORKER_BACKEND=kubernetes\n"
            "MINDROOM_KUBERNETES_WORKER_IMAGE=test-image\n"
            "MINDROOM_KUBERNETES_WORKER_STORAGE_PVC_NAME=test-pvc\n"
            "MINDROOM_KUBERNETES_WORKER_ENABLE_SERVICE_LINKS=true\n"
        ),
        encoding="utf-8",
    )
    runtime_paths = resolve_primary_runtime_paths(config_path=config_path)

    config = KubernetesWorkerBackendConfig.from_runtime(runtime_paths)

    assert config.enable_service_links is True


def test_kubernetes_backend_renders_configured_resources_on_worker_container(tmp_path: Path) -> None:
    """Configured resource requests/limits land on the rendered worker container."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=Path("config.yaml"),
        storage_path=tmp_path / "mindroom-test-storage",
    )
    backend, apps_api, _core_api = _backend(
        runtime_paths=runtime_paths,
        resource_requests={"memory": "2Gi", "cpu": "500m"},
        resource_limits={"memory": "8Gi", "cpu": "2"},
    )

    backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    container = apps_api.created_bodies[0]["spec"]["template"]["spec"]["containers"][0]
    assert container["resources"]["requests"] == {"memory": "2Gi", "cpu": "500m"}
    assert container["resources"]["limits"] == {"memory": "8Gi", "cpu": "2"}


def test_kubernetes_worker_user_resources_apply_only_to_that_requesters_workers(tmp_path: Path) -> None:
    """A listed requester's workers merge its overrides; other workers and script runs keep their resources."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=Path("config.yaml"),
        storage_path=tmp_path / "mindroom-test-storage",
    )
    backend, apps_api, _core_api = _backend(
        runtime_paths=runtime_paths,
        user_resources={"@alice:example.org": {"requests": {"memory": "1Gi"}, "limits": {"memory": "4Gi"}}},
    )
    alice_user_agent_key = "v1:tenant-123:user_agent:~@alice:example.org:code"
    alice_resources = {"requests": {"memory": "1Gi", "cpu": "100m"}, "limits": {"memory": "4Gi", "cpu": "500m"}}
    global_resources = {"requests": {"memory": "256Mi", "cpu": "100m"}, "limits": {"memory": "1Gi", "cpu": "500m"}}
    script_key = script_worker_key_for_run(alice_user_agent_key, f"script-{'a' * 32}")
    cases = [
        (_spec("v1:tenant-123:user:~@alice:example.org"), alice_resources),
        (_spec(alice_user_agent_key, private_agent_names=frozenset()), alice_resources),
        (_spec("v1:tenant-123:user:~@bob:example.org"), global_resources),
        (_spec(_TEST_SCOPED_WORKER_KEY_A), global_resources),
        (
            _spec(
                script_key,
                private_agent_names=frozenset(),
                state_scope_worker_key=alice_user_agent_key,
                resource_profile="large",
            ),
            backend.config.script_resource_profiles["large"],
        ),
    ]

    for spec, expected in cases:
        handle = backend.ensure_worker(spec, now=10.0)
        deployment = next(b for b in apps_api.created_bodies if b["metadata"]["name"] == handle.worker_id)
        template_spec = deployment["spec"]["template"]["spec"]
        assert template_spec["containers"][0]["resources"] == expected, spec.worker_key


def test_kubernetes_script_worker_uses_selected_bounded_resource_profile(tmp_path: Path) -> None:
    """A script profile selects configured quantities without accepting quantities from the agent."""
    profiles = {
        "small": {
            "requests": {"cpu": "100m", "memory": "256Mi"},
            "limits": {"cpu": "500m", "memory": "1Gi"},
        },
        "standard": {
            "requests": {"cpu": "250m", "memory": "512Mi"},
            "limits": {"cpu": "1", "memory": "2Gi"},
        },
        "large": {
            "requests": {"cpu": "500m", "memory": "2Gi"},
            "limits": {"cpu": "2", "memory": "8Gi"},
        },
    }
    runtime_paths = resolve_primary_runtime_paths(
        config_path=Path("config.yaml"),
        storage_path=tmp_path / "mindroom-test-storage",
    )
    backend, apps_api, _core_api = _backend(
        runtime_paths=runtime_paths,
        script_resource_profiles=profiles,
    )

    backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A, resource_profile="large"), now=10.0)

    deployment = apps_api.created_bodies[0]
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert container["resources"] == profiles["large"]
    assert deployment["metadata"]["annotations"]["mindroom.ai/resource-profile"] == "large"
    assert asdict(backend.script_resource_profiles()) == {
        "default_profile": "small",
        "profiles": profiles,
    }


def test_kubernetes_backend_renders_service_links_override_in_worker_template() -> None:
    """Generated worker pod templates should carry the configured service-link setting."""
    backend, apps_api, _core_api = _backend(enable_service_links=True)

    backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    deployment = apps_api.created_bodies[0]
    assert deployment["spec"]["template"]["spec"]["enableServiceLinks"] is True


def test_kubernetes_backend_config_reads_worker_annotations_from_env(tmp_path: Path) -> None:
    """Worker pod annotations are configurable through runtime env."""
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-6-astra\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    (config_dir / ".env").write_text(
        (
            "MINDROOM_WORKER_BACKEND=kubernetes\n"
            "MINDROOM_KUBERNETES_WORKER_IMAGE=test-image\n"
            "MINDROOM_KUBERNETES_WORKER_STORAGE_PVC_NAME=test-pvc\n"
            'MINDROOM_KUBERNETES_WORKER_ANNOTATIONS_JSON={"cluster-autoscaler.kubernetes.io/safe-to-evict":"false"}\n'
        ),
        encoding="utf-8",
    )
    runtime_paths = resolve_primary_runtime_paths(config_path=config_path)

    config = KubernetesWorkerBackendConfig.from_runtime(runtime_paths)

    assert config.extra_annotations == {
        "cluster-autoscaler.kubernetes.io/safe-to-evict": "false",
    }


def test_kubernetes_backend_renders_configured_annotations_on_worker_pod_template(tmp_path: Path) -> None:
    """Configured annotations land on the rendered worker pod template."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=Path("config.yaml"),
        storage_path=tmp_path / "mindroom-test-storage",
    )
    backend, apps_api, _core_api = _backend(
        runtime_paths=runtime_paths,
        extra_annotations={
            "cluster-autoscaler.kubernetes.io/safe-to-evict": "false",
            ANNOTATION_WORKER_KEY: "user-supplied-value",
            _ANNOTATION_STARTUP_MANIFEST_HASH: "user-supplied-value",
        },
    )

    backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    annotations = apps_api.created_bodies[0]["spec"]["template"]["metadata"]["annotations"]
    startup_manifest = _load_startup_manifest(backend, worker_key=_TEST_SCOPED_WORKER_KEY_A)
    committed_runtime = deserialize_runtime_paths(startup_manifest["runtime_paths"])
    assert annotations["cluster-autoscaler.kubernetes.io/safe-to-evict"] == "false"
    assert annotations[ANNOTATION_WORKER_KEY] == _TEST_SCOPED_WORKER_KEY_A
    assert annotations[_ANNOTATION_STARTUP_MANIFEST_HASH] == startup_manifest_sha256(
        committed_runtime,
        tool_validation_snapshot=_TEST_TOOL_VALIDATION_SNAPSHOT,
        public_runtime=True,
    )
    assert annotations[ANNOTATION_WORKER_KEY] != "user-supplied-value"
    assert annotations[_ANNOTATION_STARTUP_MANIFEST_HASH] != "user-supplied-value"


def test_kubernetes_backend_omits_backend_config_env_from_worker_env_and_manifest(tmp_path: Path) -> None:
    """Primary-side backend config carriers do not reach worker pods or startup manifests."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=Path("config.yaml"),
        storage_path=tmp_path / "mindroom-test-storage",
        process_env={
            "MINDROOM_KUBERNETES_WORKER_ENV_JSON": json.dumps({"OPENAI_API_KEY": "nested-secret"}),
            "MINDROOM_KUBERNETES_WORKER_LABELS_JSON": "{}",
            "MINDROOM_KUBERNETES_WORKER_ANNOTATIONS_JSON": "{}",
            "MINDROOM_KUBERNETES_WORKER_IMAGE": "ghcr.io/mindroom-ai/mindroom:latest",
            "MINDROOM_KUBERNETES_WORKER_STORAGE_PVC_NAME": "mindroom-storage",
            "MINDROOM_SHARED_CREDENTIALS_PATH": str(tmp_path / "primary-shared-credentials"),
            "MATRIX_HOMESERVER": "https://matrix.example.invalid",
        },
    )
    backend, apps_api, _core_api = _backend(
        runtime_paths=runtime_paths,
        extra_env={
            "HOME": "/unsafe/home",
            "MINDROOM_API_KEY": "runtime-api-key",
            "MINDROOM_CONFIG_PATH": "/unsafe/config.yaml",
            "MINDROOM_KUBERNETES_WORKER_ENV_JSON": json.dumps({"ANTHROPIC_API_KEY": "nested-secret"}),
            "MINDROOM_KUBERNETES_WORKER_AUTH_SECRET_NAME": "primary-worker-auth",
            "MINDROOM_LOCAL_CLIENT_SECRET": "runtime-client-secret",
            "MINDROOM_SANDBOX_PROXY_TOKEN": "extra-env-token",
            "MINDROOM_SANDBOX_DEDICATED_WORKER_ROOT": "/unsafe/root",
            "MINDROOM_SANDBOX_RUNNER_SUBPROCESS_TIMEOUT_SECONDS": "45",
            "MINDROOM_SHARED_CREDENTIALS_PATH": "/unsafe/shared-credentials",
            "MINDROOM_STORAGE_PATH": "/unsafe/storage",
            "PATH": "/unsafe/bin",
            "VIRTUAL_ENV": "/unsafe/venv",
            "MINDROOM_WORKER_TOOL_VALUE": "visible",
        },
    )

    backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    container = apps_api.created_bodies[0]["spec"]["template"]["spec"]["containers"][0]
    env_values = {env["name"]: env.get("value") for env in container["env"]}
    startup_manifest = _load_startup_manifest(backend, worker_key=_TEST_SCOPED_WORKER_KEY_A)
    committed_runtime = deserialize_runtime_paths(startup_manifest["runtime_paths"])
    committed_env = dict(committed_runtime.process_env) | dict(committed_runtime.env_file_values)
    expected_worker_root = f"/app/worker/workers/{worker_dir_name(_TEST_SCOPED_WORKER_KEY_A)}"
    env_names = [env["name"] for env in container["env"]]

    for name in (
        "MINDROOM_KUBERNETES_WORKER_ENV_JSON",
        "MINDROOM_KUBERNETES_WORKER_LABELS_JSON",
        "MINDROOM_KUBERNETES_WORKER_ANNOTATIONS_JSON",
        "MINDROOM_KUBERNETES_WORKER_IMAGE",
        "MINDROOM_KUBERNETES_WORKER_STORAGE_PVC_NAME",
        "MINDROOM_KUBERNETES_WORKER_AUTH_SECRET_NAME",
        "MINDROOM_API_KEY",
        "MINDROOM_LOCAL_CLIENT_SECRET",
    ):
        assert name not in env_values
        assert name not in committed_env

    assert env_values["MINDROOM_WORKER_TOOL_VALUE"] == "visible"
    assert committed_runtime.env_value("MINDROOM_WORKER_TOOL_VALUE") == "visible"
    assert env_values["MINDROOM_SANDBOX_RUNNER_SUBPROCESS_TIMEOUT_SECONDS"] == "45"
    assert committed_runtime.env_value("MINDROOM_SANDBOX_RUNNER_SUBPROCESS_TIMEOUT_SECONDS") == "45"
    assert committed_runtime.env_value("MINDROOM_SANDBOX_RUNNER_MODE") == "true"
    assert committed_runtime.env_value("MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE") == "forkserver"
    assert committed_runtime.env_value("MINDROOM_SANDBOX_RUNNER_PORT") == "8766"
    assert env_names.count("MINDROOM_SANDBOX_PROXY_TOKEN") == 1
    assert env_values["MINDROOM_SANDBOX_PROXY_TOKEN"] is None
    assert committed_runtime.env_value("MINDROOM_SANDBOX_PROXY_TOKEN") is None
    assert env_names.count("MINDROOM_SANDBOX_DEDICATED_WORKER_ROOT") == 1
    assert env_values["HOME"] == expected_worker_root
    assert env_values["MINDROOM_CONFIG_PATH"] != "/unsafe/config.yaml"
    assert env_values["MINDROOM_STORAGE_PATH"] == expected_worker_root
    assert "PATH" not in env_values
    assert "VIRTUAL_ENV" not in env_values
    assert committed_runtime.env_value("MINDROOM_SANDBOX_DEDICATED_WORKER_KEY") == _TEST_SCOPED_WORKER_KEY_A
    assert committed_runtime.env_value("MINDROOM_SANDBOX_DEDICATED_WORKER_ROOT") == expected_worker_root
    assert committed_runtime.env_value("MINDROOM_SHARED_CREDENTIALS_PATH") == (
        f"{expected_worker_root}/.shared_credentials"
    )
    assert committed_runtime.env_value("MINDROOM_CONFIG_PATH") != "/unsafe/config.yaml"
    assert committed_runtime.env_value("MINDROOM_STORAGE_PATH") == expected_worker_root
    assert committed_runtime.env_value("MINDROOM_KUBERNETES_WORKER_STORAGE_SUBPATH_PREFIX") == "workers"


def test_kubernetes_backend_rejects_unknown_worker_keys_for_scoped_mounts() -> None:
    """Malformed worker keys must not fall back to mounting the whole storage root."""
    backend, _apps_api, _core_api = _backend()

    with pytest.raises(WorkerBackendError, match="Unsupported worker key"):
        backend.ensure_worker(_spec("legacy-worker"), now=10.0)


def test_kubernetes_backend_requires_configured_owner_deployment_to_exist() -> None:
    """Configured owner deployments should fail closed when they cannot be resolved."""
    backend, _apps_api, _core_api = _backend(owner_deployment_name="mindroom-missing")
    assert isinstance(backend._resources.apps_api, _FakeAppsApi)
    backend._resources.apps_api.deployments.pop("mindroom-missing")

    with pytest.raises(WorkerBackendError, match="owner deployment 'mindroom-missing' was not found"):
        backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)


def test_kubernetes_backend_honors_custom_worker_port() -> None:
    """Dedicated workers should wire the configured port through env, service, and probes."""
    backend, apps_api, core_api = _backend(worker_port=9777)

    handle = backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

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


@pytest.mark.parametrize(
    "worker_key",
    ["v1:tenant-123:shared:code", resolve_unscoped_worker_key(agent_name="code")],
    ids=["shared", "unscoped"],
)
def test_kubernetes_backend_mounts_only_the_workspace_for_agent_workers(tmp_path: Path, worker_key: str) -> None:
    """Shared and unscoped dedicated workers mount their agent workspace, never the agent state root above it."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
    )
    backend, apps_api, _core_api = _backend(runtime_paths=runtime_paths)

    backend.ensure_worker(_spec(worker_key), now=10.0)

    deployment = apps_api.created_bodies[0]
    volume_mounts = deployment["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
    mount_paths = {mount["mountPath"]: mount.get("subPath") for mount in volume_mounts}
    expected_worker_root = f"/app/worker/workers/{worker_dir_name(worker_key)}"

    assert {path: subpath for path, subpath in mount_paths.items() if subpath and subpath.startswith("agents")} == {
        "/app/worker/agents/code/workspace": "agents/code/workspace",
    }
    assert next(mount for mount in volume_mounts if mount["mountPath"] == "/app/worker/agents/code/workspace") == {
        "name": "worker-storage",
        "mountPath": "/app/worker/agents/code/workspace",
        "subPath": "agents/code/workspace",
    }
    # The primary creates the shared workspace before kubelet could create it as root.
    assert (runtime_paths.storage_root / "agents" / "code" / "workspace").is_dir()
    assert mount_paths[expected_worker_root] == f"workers/{worker_dir_name(worker_key)}"
    assert "/app/worker/credentials" not in mount_paths
    assert "/app/worker/.shared_credentials" not in mount_paths
    assert "/app/worker/agents" not in mount_paths

    container = deployment["spec"]["template"]["spec"]["containers"][0]
    env_values = {env["name"]: env.get("value") for env in container["env"]}
    assert env_values["MINDROOM_STORAGE_PATH"] == expected_worker_root
    assert env_values["MINDROOM_SHARED_CREDENTIALS_PATH"] == f"{expected_worker_root}/.shared_credentials"


def test_kubernetes_backend_mounts_assigned_knowledge_outside_shared_agent_root_read_only(tmp_path: Path) -> None:
    """Assigned knowledge under shared storage should be visible without widening agent storage."""
    storage_root = tmp_path / "storage"
    knowledge_root = storage_root / "knowledge" / "shared-docs"
    knowledge_root.mkdir(parents=True)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
agents:
  code:
    display_name: Code
    role: Code test
    model: default
    worker_scope: shared
    knowledge_bases: [shared_docs]
knowledge_bases:
  shared_docs:
    path: ./storage/knowledge/shared-docs
    include_patterns: [docs/**/*.md]
models:
  default:
    provider: openai
    id: gpt-6-astra
router:
  model: default
""".lstrip(),
        encoding="utf-8",
    )
    runtime_paths = resolve_primary_runtime_paths(config_path=config_path, storage_path=storage_root)
    backend, apps_api, _core_api = _backend(runtime_paths=runtime_paths)

    backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    deployment = apps_api.created_bodies[0]
    volume_mounts = deployment["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
    knowledge_mount = next(
        mount for mount in volume_mounts if mount["mountPath"] == "/app/worker/knowledge/shared-docs"
    )
    assert knowledge_mount == {
        "name": "worker-storage",
        "mountPath": "/app/worker/knowledge/shared-docs",
        "subPath": "knowledge/shared-docs",
        "readOnly": True,
    }


@pytest.mark.parametrize(
    ("source", "planted"),
    [
        ("agents/other/workspace/docs", "link"),
        ("agents/other/workspace/docs", "directory"),
        ("workers/other/docs", "directory"),
        ("shared_directories/other/report/docs", "directory"),
        ("knowledge/docs", "link"),
    ],
)
def test_kubernetes_backend_never_mounts_knowledge_another_worker_could_redirect(
    tmp_path: Path,
    source: str,
    planted: str,
) -> None:
    """Knowledge is planned from its configured path: never through a link, never inside what other workers write."""
    storage_root = tmp_path / "storage"
    victim = storage_root / "private_instances" / "someone" / "data"
    victim.mkdir(parents=True)
    docs = storage_root / source
    docs.parent.mkdir(parents=True)
    if planted == "link":
        docs.symlink_to(victim, target_is_directory=True)
    else:
        docs.mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "agents:\n"
        "  code:\n    display_name: Code\n    worker_scope: shared\n    knowledge_bases: [docs]\n"
        "  other:\n    display_name: Other\n    worker_scope: shared\n"
        f"knowledge_bases:\n  docs:\n    path: ${{MINDROOM_STORAGE_PATH}}/{source}\n"
        "models:\n  default:\n    provider: openai\n    id: test-model\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_primary_runtime_paths(config_path=config_path, storage_path=storage_root)
    backend, apps_api, _core_api = _backend(runtime_paths=runtime_paths)

    with capture_logs() as logs:
        backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    subpaths = {str(mount["subPath"]) for mount in _storage_mounts(apps_api.created_bodies[0]).values()}
    assert not any(subpath.startswith(("private_instances", "agents/other", source)) for subpath in subpaths)
    assert any(entry["log_level"] in {"warning", "error"} and "knowledge" in entry["event"] for entry in logs)


def test_kubernetes_backend_normalizes_knowledge_paths_and_logs_skipped_ones(tmp_path: Path) -> None:
    """A configured path with ``..`` still mounts, and a source outside worker storage is skipped with a warning."""
    storage_root = tmp_path / "storage"
    (storage_root / "knowledge" / "docs").mkdir(parents=True)
    outside = tmp_path / "outside-docs"
    outside.mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "agents:\n  code:\n    display_name: Code\n    worker_scope: shared\n    knowledge_bases: [docs, outside]\n"
        "knowledge_bases:\n  docs:\n    path: ./config-dir/../storage/knowledge/docs\n"
        f"  outside:\n    path: {outside}\n"
        "models:\n  default:\n    provider: openai\n    id: test-model\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_primary_runtime_paths(config_path=config_path, storage_path=storage_root)
    backend, apps_api, _core_api = _backend(runtime_paths=runtime_paths)

    with capture_logs() as logs:
        backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    assert _storage_mounts(apps_api.created_bodies[0])["/app/worker/knowledge/docs"]["subPath"] == "knowledge/docs"
    assert any(entry.get("knowledge_base") == "outside" for entry in logs if entry["log_level"] == "warning")


def test_kubernetes_backend_projects_shared_agent_for_narrower_user_agent_worker(tmp_path: Path) -> None:
    """A script-isolated worker should retain its shared agent's state and assigned knowledge."""
    storage_root = tmp_path / "storage"
    knowledge_root = storage_root / "knowledge" / "watcher-docs"
    knowledge_root.mkdir(parents=True)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
agents:
  watcher:
    display_name: Watcher
    role: Watcher test
    model: default
    worker_scope: shared
    knowledge_bases: [watcher_docs]
knowledge_bases:
  watcher_docs:
    path: {knowledge_root}
models:
  default:
    provider: openai
    id: gpt-6-astra
router:
  model: default
""".lstrip(),
        encoding="utf-8",
    )
    runtime_paths = resolve_primary_runtime_paths(config_path=config_path, storage_path=storage_root)
    backend, apps_api, _core_api = _backend(runtime_paths=runtime_paths)
    worker_key = "v1:tenant-123:user_agent:@alice:example.org:watcher"

    backend.ensure_worker(
        _spec(worker_key, private_agent_names=frozenset()),
        now=10.0,
    )

    deployment = apps_api.created_bodies[0]
    mount_paths = {
        mount["mountPath"] for mount in deployment["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
    }
    assert "/app/worker/agents/watcher/workspace" in mount_paths
    assert "/app/worker/agents/watcher" not in mount_paths
    assert "/app/worker/knowledge/watcher-docs" in mount_paths


def test_kubernetes_backend_matches_normalized_agent_name_for_knowledge_mount(tmp_path: Path) -> None:
    """Knowledge selection should match the normalized agent name encoded in a worker key."""
    storage_root = tmp_path / "storage"
    knowledge_root = storage_root / "knowledge" / "shared-docs"
    knowledge_root.mkdir(parents=True)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
agents:
  My_Agent_:
    display_name: Code
    role: Code test
    model: default
    worker_scope: shared
    knowledge_bases: [shared_docs]
knowledge_bases:
  shared_docs:
    path: {knowledge_root}
models:
  default:
    provider: openai
    id: gpt-6-astra
router:
  model: default
""".lstrip(),
        encoding="utf-8",
    )
    runtime_paths = resolve_primary_runtime_paths(config_path=config_path, storage_path=storage_root)
    backend, apps_api, _core_api = _backend(runtime_paths=runtime_paths)
    worker_key = "v1:tenant-123:shared:My_Agent"

    backend.ensure_worker(_spec(worker_key), now=10.0)

    deployment = apps_api.created_bodies[0]
    volume_mounts = deployment["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
    assert any(mount.get("subPath") == "knowledge/shared-docs" for mount in volume_mounts)


def test_kubernetes_backend_rejects_ambiguous_normalized_agent_names(tmp_path: Path) -> None:
    """A worker key must never select one arbitrary agent from a normalized-name collision."""
    knowledge_root = tmp_path / "knowledge"
    knowledge_root.mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
agents:
  _My_Agent:
    display_name: First
    model: default
    worker_scope: shared
    knowledge_bases: [shared_docs]
  My_Agent:
    display_name: Second
    model: default
    worker_scope: shared
    knowledge_bases: [shared_docs]
knowledge_bases:
  shared_docs:
    path: {knowledge_root}
models:
  default:
    provider: openai
    id: gpt-6-astra
router:
  model: default
""".lstrip(),
        encoding="utf-8",
    )
    runtime_paths = resolve_primary_runtime_paths(config_path=config_path, storage_path=tmp_path / "storage")
    backend, _apps_api, _core_api = _backend(runtime_paths=runtime_paths)

    with pytest.raises(WorkerBackendError, match="ambiguous normalized agent name"):
        backend.ensure_worker(_spec("v1:tenant-123:shared:My_Agent"), now=10.0)


def test_kubernetes_backend_does_not_duplicate_knowledge_already_visible_in_agent_root(tmp_path: Path) -> None:
    """Knowledge below an existing scoped mount should use that mount without a nested duplicate."""
    storage_root = tmp_path / "storage"
    knowledge_root = storage_root / "agents" / "code" / "workspace" / "knowledge"
    knowledge_root.mkdir(parents=True)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
agents:
  code:
    display_name: Code
    role: Code test
    model: default
    worker_scope: shared
    knowledge_bases: [workspace_docs]
knowledge_bases:
  workspace_docs:
    path: {knowledge_root}
models:
  default:
    provider: openai
    id: gpt-6-astra
router:
  model: default
""".lstrip(),
        encoding="utf-8",
    )
    runtime_paths = resolve_primary_runtime_paths(config_path=config_path, storage_path=storage_root)
    backend, apps_api, _core_api = _backend(runtime_paths=runtime_paths)

    backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    deployment = apps_api.created_bodies[0]
    volume_mounts = deployment["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
    mount_paths = [mount["mountPath"] for mount in volume_mounts]
    assert "/app/worker/agents/code/workspace" in mount_paths
    assert "/app/worker/agents/code/workspace/knowledge" not in mount_paths


def _write_code_agent_knowledge_config(tmp_path: Path, knowledge_path: Path) -> RuntimePaths:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
agents:
  code:
    display_name: Code
    role: Code test
    model: default
    worker_scope: shared
    knowledge_bases: [agent_docs]
knowledge_bases:
  agent_docs:
    path: {knowledge_path}
models:
  default:
    provider: openai
    id: gpt-6-astra
router:
  model: default
""".lstrip(),
        encoding="utf-8",
    )
    return resolve_primary_runtime_paths(config_path=config_path, storage_path=tmp_path / "storage")


def test_kubernetes_drift_check_never_creates_workspace_mount_sources(tmp_path: Path) -> None:
    """Planning a pod template only reads storage; directories are created at ensure time alone."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
    )
    backend, _apps_api, _core_api = _backend(runtime_paths=runtime_paths)
    worker_key = "v1:tenant-123:shared:code"
    handle = backend.ensure_worker(_spec(worker_key), now=10.0)
    agent_root = runtime_paths.storage_root / "agents" / "code"
    shutil.rmtree(agent_root)
    before = sorted(runtime_paths.storage_root.rglob("*"))
    deployment = backend._resources.read_deployment(handle.worker_id)
    assert deployment is not None

    assert backend._resources.deployment_template_drifted(
        deployment,
        worker_key=worker_key,
        worker_id=handle.worker_id,
        state_subpath=backend._state_subpath(worker_key),
        launch=_launch_of(backend),
        private_agent_names=None,
    )

    assert not agent_root.exists()
    assert sorted(runtime_paths.storage_root.rglob("*")) == before


def test_kubernetes_backend_mounts_knowledge_beside_the_workspace_read_only(tmp_path: Path) -> None:
    """Knowledge in the agent state root but outside the workspace stays visible, read-only."""
    knowledge_root = tmp_path / "storage" / "agents" / "code" / "knowledge"
    knowledge_root.mkdir(parents=True)
    backend, apps_api, _core_api = _backend(runtime_paths=_write_code_agent_knowledge_config(tmp_path, knowledge_root))

    backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    volume_mounts = apps_api.created_bodies[0]["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
    agent_mounts = [mount for mount in volume_mounts if str(mount.get("subPath", "")).startswith("agents/")]
    assert agent_mounts == [
        {
            "name": "worker-storage",
            "mountPath": "/app/worker/agents/code/workspace",
            "subPath": "agents/code/workspace",
        },
        {
            "name": "worker-storage",
            "mountPath": "/app/worker/agents/code/knowledge",
            "subPath": "agents/code/knowledge",
            "readOnly": True,
        },
    ]


def test_kubernetes_backend_rejects_knowledge_containing_the_workspace(tmp_path: Path) -> None:
    """Knowledge rooted at the agent state root would re-expose it, so it fails closed."""
    agent_root = tmp_path / "storage" / "agents" / "code"
    agent_root.mkdir(parents=True)
    backend, apps_api, _core_api = _backend(runtime_paths=_write_code_agent_knowledge_config(tmp_path, agent_root))

    with pytest.raises(WorkerBackendError, match="knowledge mount overlaps existing mountPath"):
        backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    assert apps_api.created_bodies == []


def test_kubernetes_backend_rejects_knowledge_mount_containing_scoped_storage(tmp_path: Path) -> None:
    """A broad knowledge mount must not shadow scoped mounts or expose sibling storage."""
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
agents:
  code:
    display_name: Code
    role: Code test
    model: default
    worker_scope: shared
    knowledge_bases: [unsafe_root]
knowledge_bases:
  unsafe_root:
    path: {storage_root}
models:
  default:
    provider: openai
    id: gpt-6-astra
router:
  model: default
""".lstrip(),
        encoding="utf-8",
    )
    runtime_paths = resolve_primary_runtime_paths(config_path=config_path, storage_path=storage_root)
    backend, apps_api, _core_api = _backend(runtime_paths=runtime_paths)

    with pytest.raises(WorkerBackendError, match="knowledge mount overlaps existing mountPath"):
        backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    assert apps_api.created_bodies == []


def test_kubernetes_backend_rejects_overlapping_assigned_knowledge_mounts(tmp_path: Path) -> None:
    """Nested assigned sources should fail closed even if raw config bypassed typed validation."""
    storage_root = tmp_path / "storage"
    parent_root = storage_root / "knowledge" / "project"
    child_root = parent_root / "docs"
    child_root.mkdir(parents=True)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
agents:
  code:
    display_name: Code
    role: Code test
    model: default
    worker_scope: shared
    knowledge_bases: [project, project_docs]
knowledge_bases:
  project:
    path: {parent_root}
  project_docs:
    path: {storage_root / "knowledge" / "other"}
models:
  default:
    provider: openai
    id: gpt-6-astra
router:
  model: default
""".lstrip(),
        encoding="utf-8",
    )
    runtime_paths = resolve_primary_runtime_paths(config_path=config_path, storage_path=storage_root)
    validated = load_config(runtime_paths)
    unvalidated_knowledge = {
        **validated.knowledge_bases,
        "project_docs": validated.knowledge_bases["project_docs"].model_copy(update={"path": str(child_root)}),
    }
    backend, apps_api, _core_api = _backend(
        runtime_paths=runtime_paths,
        applied_config=validated.model_copy(update={"knowledge_bases": unvalidated_knowledge}),
    )

    with pytest.raises(WorkerBackendError, match="knowledge mount paths overlap") as exc_info:
        backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    assert "/app/worker/knowledge/project" in str(exc_info.value)
    assert "/app/worker/knowledge/project/docs" in str(exc_info.value)
    assert apps_api.created_bodies == []


@pytest.mark.parametrize("knowledge_dir", ["tmp", "tmp/docs"])
def test_kubernetes_backend_rejects_knowledge_mount_overlapping_tmp_mount(tmp_path: Path, knowledge_dir: str) -> None:
    """Knowledge mounts must not overlap the fixed /tmp mount, which is not on the storage PVC."""
    storage_root = tmp_path / "storage"
    knowledge_root = storage_root / knowledge_dir
    knowledge_root.mkdir(parents=True)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
agents:
  code:
    display_name: Code
    role: Code test
    model: default
    worker_scope: shared
    knowledge_bases: [unsafe_docs]
knowledge_bases:
  unsafe_docs:
    path: {knowledge_root}
models:
  default:
    provider: openai
    id: gpt-6-astra
router:
  model: default
""".lstrip(),
        encoding="utf-8",
    )
    runtime_paths = resolve_primary_runtime_paths(config_path=config_path, storage_path=storage_root)
    backend, apps_api, _core_api = _backend(runtime_paths=runtime_paths, storage_mount_path="/")

    with pytest.raises(WorkerBackendError, match="knowledge mount overlaps existing mountPath"):
        backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    assert apps_api.created_bodies == []


def test_kubernetes_backend_does_not_mount_unassigned_knowledge(tmp_path: Path) -> None:
    """Configured knowledge must stay hidden unless assigned to an agent addressed by the worker."""
    storage_root = tmp_path / "storage"
    assigned_root = storage_root / "knowledge" / "assigned"
    unassigned_root = storage_root / "knowledge" / "unassigned"
    assigned_root.mkdir(parents=True)
    unassigned_root.mkdir(parents=True)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
agents:
  code:
    display_name: Code
    role: Code test
    model: default
    worker_scope: shared
    knowledge_bases: [assigned_docs]
knowledge_bases:
  assigned_docs:
    path: {assigned_root}
  unassigned_docs:
    path: {unassigned_root}
models:
  default:
    provider: openai
    id: gpt-6-astra
router:
  model: default
""".lstrip(),
        encoding="utf-8",
    )
    runtime_paths = resolve_primary_runtime_paths(config_path=config_path, storage_path=storage_root)
    backend, apps_api, _core_api = _backend(runtime_paths=runtime_paths)

    backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    deployment = apps_api.created_bodies[0]
    mount_paths = {
        mount["mountPath"] for mount in deployment["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
    }
    assert "/app/worker/knowledge/assigned" in mount_paths
    assert "/app/worker/knowledge/unassigned" not in mount_paths


def test_kubernetes_backend_ignores_assigned_knowledge_outside_worker_storage(tmp_path: Path) -> None:
    """Unsupported sources outside the shared PVC should not change existing scoped mounts."""
    storage_root = tmp_path / "storage"
    external_root = tmp_path / "external-docs"
    external_root.mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
agents:
  code:
    display_name: Code
    role: Code test
    model: default
    worker_scope: shared
    knowledge_bases: [external_docs]
knowledge_bases:
  external_docs:
    path: {external_root}
models:
  default:
    provider: openai
    id: gpt-6-astra
router:
  model: default
""".lstrip(),
        encoding="utf-8",
    )
    runtime_paths = resolve_primary_runtime_paths(config_path=config_path, storage_path=storage_root)
    backend, apps_api, _core_api = _backend(runtime_paths=runtime_paths)

    backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    deployment = apps_api.created_bodies[0]
    volume_mounts = deployment["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
    mount_paths = {mount["mountPath"] for mount in volume_mounts}
    expected_worker_root = f"/app/worker/workers/{worker_dir_name(_TEST_SCOPED_WORKER_KEY_A)}"
    assert mount_paths == {
        "/app/worker/agents/code/workspace",
        expected_worker_root,
        f"{expected_worker_root}/.shared_credentials",
        f"{expected_worker_root}/.runtime",
        "/tmp",  # noqa: S108
    }


def test_kubernetes_backend_user_worker_mounts_knowledge_for_all_addressed_agents(tmp_path: Path) -> None:
    """One user-scoped worker should see assignments from user-scoped agents, not other scopes."""
    storage_root = tmp_path / "storage"
    for dirname in ("alpha", "beta", "gamma"):
        (storage_root / "knowledge" / dirname).mkdir(parents=True)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
agents:
  alpha:
    display_name: Alpha
    role: Alpha test
    model: default
    worker_scope: user
    knowledge_bases: [alpha_docs]
  beta:
    display_name: Beta
    role: Beta test
    model: default
    worker_scope: user
    knowledge_bases: [beta_docs]
  gamma:
    display_name: Gamma
    role: Gamma test
    model: default
    worker_scope: shared
    knowledge_bases: [gamma_docs]
knowledge_bases:
  alpha_docs:
    path: {storage_root / "knowledge" / "alpha"}
  beta_docs:
    path: {storage_root / "knowledge" / "beta"}
  gamma_docs:
    path: {storage_root / "knowledge" / "gamma"}
models:
  default:
    provider: openai
    id: gpt-6-astra
router:
  model: default
""".lstrip(),
        encoding="utf-8",
    )
    runtime_paths = resolve_primary_runtime_paths(config_path=config_path, storage_path=storage_root)
    backend, apps_api, _core_api = _backend(runtime_paths=runtime_paths)
    worker_key = "v1:tenant-123:user:@alice:example.org"

    backend.ensure_worker(_spec(worker_key), now=10.0)

    deployment = apps_api.created_bodies[0]
    mount_paths = {
        mount["mountPath"] for mount in deployment["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
    }
    assert "/app/worker/knowledge/alpha" in mount_paths
    assert "/app/worker/knowledge/beta" in mount_paths
    assert "/app/worker/knowledge/gamma" not in mount_paths


def test_kubernetes_backend_recreates_worker_when_knowledge_mounts_change(tmp_path: Path) -> None:
    """Knowledge mount changes must alter the pod-template hash and recreate a stale Deployment."""
    storage_root = tmp_path / "storage"
    knowledge_root = storage_root / "knowledge" / "shared-docs"
    knowledge_root.mkdir(parents=True)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
agents:
  code:
    display_name: Code
    role: Code test
    model: default
    worker_scope: shared
knowledge_bases:
  shared_docs:
    path: {knowledge_root}
models:
  default:
    provider: openai
    id: gpt-6-astra
router:
  model: default
""".lstrip(),
        encoding="utf-8",
    )
    runtime_paths = resolve_primary_runtime_paths(config_path=config_path, storage_path=storage_root)
    backend, apps_api, core_api = _backend(runtime_paths=runtime_paths)
    backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)
    initial_hash = apps_api.created_bodies[0]["metadata"]["annotations"][ANNOTATION_TEMPLATE_HASH]

    config_path.write_text(
        f"""
agents:
  code:
    display_name: Code
    role: Code test
    model: default
    worker_scope: shared
    knowledge_bases: [shared_docs]
knowledge_bases:
  shared_docs:
    path: {knowledge_root}
models:
  default:
    provider: openai
    id: gpt-6-astra
router:
  model: default
""".lstrip(),
        encoding="utf-8",
    )
    updated_backend, _updated_apps_api, _updated_core_api = _backend(runtime_paths=runtime_paths)
    updated_backend._resources.apps_api = apps_api
    updated_backend._resources.core_api = core_api
    updated_backend._resources.api_exception_cls = _FakeApiError

    updated_backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=20.0)

    recreated = apps_api.created_bodies[-1]
    updated_hash = recreated["metadata"]["annotations"][ANNOTATION_TEMPLATE_HASH]
    assert apps_api.deleted_names == [recreated["metadata"]["name"]]
    assert updated_hash != initial_hash
    volume_mounts = recreated["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
    assert any(mount.get("subPath") == "knowledge/shared-docs" for mount in volume_mounts)


def test_kubernetes_backend_uses_custom_worker_prefix_for_storage_path() -> None:
    """Custom worker prefixes should only affect the dedicated worker storage root."""
    backend, apps_api, _core_api = _backend(storage_subpath_prefix="sandbox-workers")
    worker_key = "v1:tenant-123:shared:code"

    backend.ensure_worker(_spec(worker_key), now=10.0)

    deployment = apps_api.created_bodies[0]
    env_values = {
        env["name"]: env.get("value") for env in deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    expected_worker_root = f"/app/worker/sandbox-workers/{worker_dir_name(worker_key)}"

    assert env_values["MINDROOM_STORAGE_PATH"] == expected_worker_root


def test_kubernetes_backend_mounts_primary_written_directories_read_only() -> None:
    """Worker code must not be able to rewrite or relink the credential mirror or the startup manifest."""
    backend, apps_api, _core_api = _backend()
    worker_key = "v1:tenant-123:shared:code"

    backend.ensure_worker(_spec(worker_key), now=10.0)

    deployment = apps_api.created_bodies[0]
    volume_mounts = deployment["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
    mounts_by_path = {mount["mountPath"]: mount for mount in volume_mounts}
    expected_worker_root = f"/app/worker/workers/{worker_dir_name(worker_key)}"

    assert mounts_by_path[expected_worker_root].get("readOnly") is None
    for dirname in (".shared_credentials", ".runtime"):
        assert mounts_by_path[f"{expected_worker_root}/{dirname}"] == {
            "name": mounts_by_path[expected_worker_root]["name"],
            "mountPath": f"{expected_worker_root}/{dirname}",
            "subPath": f"workers/{worker_dir_name(worker_key)}/{dirname}",
            "readOnly": True,
        }


def test_kubernetes_backend_refuses_symlinked_startup_manifest_directory(tmp_path: Path) -> None:
    """A `.runtime` link planted by an older worker must never redirect the primary's manifest write."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=Path("config.yaml"),
        storage_path=tmp_path / "storage",
    )
    backend, apps_api, _core_api = _backend(runtime_paths=runtime_paths)
    victim = tmp_path / "victim"
    victim.mkdir()
    runtime_dir = worker_root_path(runtime_paths.storage_root, _TEST_SCOPED_WORKER_KEY_A) / ".runtime"
    runtime_dir.parent.mkdir(parents=True)
    runtime_dir.symlink_to(victim, target_is_directory=True)

    with pytest.raises(WorkerBackendError, match="real directory"):
        backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    assert list(victim.iterdir()) == []
    assert apps_api.created_bodies == []


def test_kubernetes_backend_prepares_mirror_before_applying_deployment(tmp_path: Path) -> None:
    """Kubelet must find a primary-owned mirror directory, never a planted link, when it mounts the pod."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=Path("config.yaml"),
        storage_path=tmp_path / "storage",
    )
    backend, _apps_api, _core_api = _backend(runtime_paths=runtime_paths)
    mirror = worker_root_path(runtime_paths.storage_root, _TEST_SCOPED_WORKER_KEY_A) / ".shared_credentials"
    mirror.parent.mkdir(parents=True)
    mirror.symlink_to(runtime_paths.storage_root / "credentials", target_is_directory=True)
    apply_deployment = backend._resources.apply_deployment
    mirror_states: list[tuple[bool, bool]] = []

    def record_mirror_then_apply(**kwargs: object) -> object:
        mirror_states.append((mirror.is_symlink(), mirror.is_dir()))
        return apply_deployment(**kwargs)

    backend._resources.apply_deployment = record_mirror_then_apply

    backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    assert mirror_states == [(False, True)]


def test_kubernetes_backend_user_scope_mounts_only_user_scope_agent_roots(tmp_path: Path) -> None:
    """User-scope workers must not mount the agents tree or agent roots on other scopes."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
agents:
  alpha:
    display_name: Alpha
    role: Alpha test
    model: default
    worker_scope: user
  beta:
    display_name: Beta
    role: Beta test
    model: default
    worker_scope: shared
  gamma:
    display_name: Gamma
    role: Gamma test
    model: default
  delta:
    display_name: Delta
    role: Delta test
    model: default
    worker_scope: user_agent
  epsilon:
    display_name: Epsilon
    role: Epsilon test
    model: default
    private:
      per: user
models:
  default:
    provider: openai
    id: gpt-6-astra
router:
  model: default
""".lstrip(),
        encoding="utf-8",
    )
    runtime_paths = resolve_primary_runtime_paths(config_path=config_path, storage_path=tmp_path / "storage")
    backend, apps_api, _core_api = _backend(runtime_paths=runtime_paths)
    worker_key = "v1:tenant-123:user:~@alice:example.org"

    backend.ensure_worker(_spec(worker_key), now=10.0)

    deployment = apps_api.created_bodies[0]
    volume_mounts = deployment["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
    mount_paths = {mount["mountPath"]: mount.get("subPath") for mount in volume_mounts}
    expected_worker_root = f"/app/worker/workers/{worker_dir_name(worker_key)}"
    expected_private_root = f"/app/worker/private_instances/{worker_dir_name(worker_key)}"

    agent_mounts = {
        path: sub_path
        for path, sub_path in mount_paths.items()
        if path.startswith("/app/worker/agents") or (sub_path or "").startswith("agents")
    }
    assert agent_mounts == {"/app/worker/agents/alpha/workspace": "agents/alpha/workspace"}
    # The per-user private agent has no workspace yet, so nothing below its scope is mounted or created.
    assert not any("private_instances" in (sub_path or "") for sub_path in mount_paths.values())
    assert not (runtime_paths.storage_root / "private_instances").exists()
    assert mount_paths[expected_worker_root] == f"workers/{worker_dir_name(worker_key)}"
    assert "/app/worker/credentials" not in mount_paths
    assert "/app/worker/.shared_credentials" not in mount_paths

    ensure_private_instance_identity(
        runtime_paths.storage_root,
        worker_key=worker_key,
        requester_id="@alice:example.org",
    )
    private_workspace = (
        private_instance_scope_root_path(runtime_paths.storage_root, worker_key) / "epsilon" / "epsilon_data"
    )
    private_workspace.mkdir(parents=True)
    backend.ensure_worker(_spec(worker_key), now=20.0)

    volume_mounts = apps_api.created_bodies[-1]["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
    private_mounts = {
        mount["mountPath"]: mount["subPath"]
        for mount in volume_mounts
        if "private_instances" in str(mount.get("subPath"))
    }
    assert private_mounts == {
        f"{expected_private_root}/epsilon/epsilon_data": f"private_instances/{worker_dir_name(worker_key)}/epsilon/epsilon_data",
    }


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
        backend.ensure_worker(_spec(worker_key), now=10.0)


def test_kubernetes_backend_rejects_private_user_agent_worker_without_target_visibility(tmp_path: Path) -> None:
    """Private user-agent workers must fail closed until the targeted private agent is explicitly visible."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
agents:
  alpha:
    display_name: Alpha
    role: Alpha test
    model: default
    private:
      per: user_agent
models:
  default:
    provider: openai
    id: gpt-6-astra
router:
  model: default
""".lstrip(),
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
        backend.ensure_worker(_spec(worker_key, private_agent_names=frozenset()), now=10.0)
    assert apps_api.created_bodies == []


def _storage_mounts(body: dict[str, object]) -> dict[str, dict[str, object]]:
    volume_mounts = body["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
    return {mount["mountPath"]: mount for mount in volume_mounts if mount["name"] == "worker-storage"}


def test_kubernetes_backend_user_agent_mounts_only_the_existing_private_workspace(tmp_path: Path) -> None:
    """A private user-agent worker mounts its private workspace, never the scope or state root holding the identity."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
    )
    backend, apps_api, _core_api = _backend(runtime_paths=runtime_paths)
    worker_key = "v1:tenant-123:user_agent:~@alice:localhost:mind"
    spec = _spec(worker_key, private_agent_names=frozenset({"mind"}))
    scope_root = private_instance_scope_root_path(backend.storage_root, worker_key)

    backend.ensure_worker(spec, now=10.0)

    # Before the primary materializes the workspace nothing private is mounted, and the
    # scope stays absent so its identity record can still be written first.
    assert not any(
        "private_instances" in str(mount.get("subPath"))
        for mount in _storage_mounts(apps_api.created_bodies[-1]).values()
    )
    assert "/app/worker/agents/mind/workspace" not in _storage_mounts(apps_api.created_bodies[-1])
    assert not scope_root.exists()

    _materialize_private_workspace(backend.storage_root, worker_key, "@alice:localhost")
    backend.ensure_worker(spec, now=20.0)

    private_mounts = {
        path: mount
        for path, mount in _storage_mounts(apps_api.created_bodies[-1]).items()
        if "private_instances" in path
    }
    assert private_mounts == {
        f"/app/worker/private_instances/{scope_root.name}/mind/mind_data": {
            "name": "worker-storage",
            "mountPath": f"/app/worker/private_instances/{scope_root.name}/mind/mind_data",
            "subPath": f"private_instances/{scope_root.name}/mind/mind_data",
        },
    }


def test_kubernetes_backend_never_mounts_a_linked_workspace(tmp_path: Path) -> None:
    """A workspace, or a directory above it, replaced by a link is left unmounted instead of followed."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
    )
    backend, apps_api, _core_api = _backend(runtime_paths=runtime_paths)
    worker_key = "v1:tenant-123:user_agent:~@mallory:localhost:mind"
    victim_key = "v1:tenant-123:user_agent:~@alice:localhost:mind"
    for key, requester in ((worker_key, "@mallory:localhost"), (victim_key, "@alice:localhost")):
        _materialize_private_workspace(backend.storage_root, key, requester)
    attacker_agent_root = private_instance_scope_root_path(backend.storage_root, worker_key) / "mind"
    attacker_agent_root.rename(attacker_agent_root.with_name("mind-moved"))
    attacker_agent_root.symlink_to(private_instance_scope_root_path(backend.storage_root, victim_key) / "mind")

    with capture_logs() as logs:
        backend.ensure_worker(_spec(worker_key, private_agent_names=frozenset({"mind"})), now=10.0)

    assert not any("private_instances" in path for path in _storage_mounts(apps_api.created_bodies[-1]))
    assert any(entry["log_level"] == "warning" and "link" in entry["event"] for entry in logs)


def test_kubernetes_backend_historical_mount_uses_canonical_pvc_source(tmp_path: Path) -> None:
    """Both worker destinations bind the real canonical directory on the PVC."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
    )
    backend, apps_api, _core_api = _backend(runtime_paths=runtime_paths)
    worker_key = "v1:default:user_agent:~@alice:example.org:mind"
    _materialize_private_workspace(backend.storage_root, worker_key, "@alice:example.org")
    canonical = private_instance_scope_root_path(backend.storage_root, worker_key)
    legacy = private_instance_scope_root_path(backend.storage_root, "v1:default:user_agent:@alice:example.org:mind")
    legacy.symlink_to(canonical.name, target_is_directory=True)

    backend.ensure_worker(_spec(worker_key, private_agent_names=frozenset({"mind"})), now=10.0)

    deployment = apps_api.created_bodies[0]
    mounts = deployment["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
    assert {
        (mount["mountPath"], mount["subPath"]) for mount in mounts if "/private_instances/" in mount["mountPath"]
    } == {
        (
            f"/app/worker/private_instances/{canonical.name}/mind/mind_data",
            f"private_instances/{canonical.name}/mind/mind_data",
        ),
        (
            f"/app/worker/private_instances/{legacy.name}/mind/mind_data",
            f"private_instances/{canonical.name}/mind/mind_data",
        ),
    }
    assert all(mount["mountPath"] != "/app/worker/private_instances" for mount in mounts)


def test_kubernetes_script_worker_mounts_the_owning_private_workspace(tmp_path: Path) -> None:
    """A script worker mounts only its owning scope's private workspace, not the scope or identity record."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
    )
    backend, apps_api, _core_api = _backend(runtime_paths=runtime_paths)
    state_scope_worker_key = "v1:tenant-123:user_agent:~@alice:localhost:mind"
    run_id = f"script-{'a' * 32}"
    worker_key = script_worker_key_for_run(state_scope_worker_key, run_id)
    _materialize_private_workspace(backend.storage_root, state_scope_worker_key, "@alice:localhost")

    backend.ensure_worker(
        _spec(
            worker_key,
            private_agent_names=frozenset({"mind"}),
            state_scope_worker_key=state_scope_worker_key,
        ),
        now=10.0,
    )

    deployment = apps_api.created_bodies[0]
    volume_mounts = deployment["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
    mount_paths = {mount["mountPath"]: mount.get("subPath") for mount in volume_mounts}
    expected_private_root = f"/app/worker/private_instances/{worker_dir_name(state_scope_worker_key)}"
    expected_private_subpath = f"private_instances/{worker_dir_name(state_scope_worker_key)}"
    expected_run_root = f"/app/worker/workers/{worker_dir_name(worker_key)}"

    assert deployment["metadata"]["annotations"][_ANNOTATION_STATE_SCOPE_WORKER_KEY] == state_scope_worker_key
    assert expected_private_root not in mount_paths
    assert {path: subpath for path, subpath in mount_paths.items() if "private_instances" in path} == {
        f"{expected_private_root}/mind/mind_data": f"{expected_private_subpath}/mind/mind_data",
    }
    assert mount_paths[expected_run_root] == f"workers/{worker_dir_name(worker_key)}"
    assert f"private_instances/{worker_dir_name(worker_key)}/mind" not in set(mount_paths.values())


def test_kubernetes_script_worker_rejects_an_unrelated_state_scope() -> None:
    """A worker spec cannot use the state-scope field to mount another identity's files."""
    backend, apps_api, _core_api = _backend()
    state_scope_worker_key = "v1:tenant-123:user_agent:@alice:example.org:mind"
    worker_key = script_worker_key_for_run(state_scope_worker_key, f"script-{'a' * 32}")

    with pytest.raises(WorkerBackendError, match="does not own the requested worker key"):
        backend.ensure_worker(
            _spec(
                worker_key,
                private_agent_names=frozenset({"mind"}),
                state_scope_worker_key="v1:tenant-123:user_agent:@mallory:example.org:mind",
            ),
            now=10.0,
        )

    assert apps_api.created_bodies == []


def _materialize_private_workspace(storage_root: Path, worker_key: str, requester_id: str) -> Path:
    ensure_private_instance_identity(storage_root, worker_key=worker_key, requester_id=requester_id)
    workspace = private_instance_scope_root_path(storage_root, worker_key) / "mind" / "mind_data"
    workspace.mkdir(parents=True)
    return workspace


@pytest.mark.parametrize("materialized", ["after_ready_then_touched", "during_cold_start"])
def test_kubernetes_user_worker_mounts_a_private_workspace_that_materializes_later(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    materialized: str,
) -> None:
    """The ready cache compares the mounts the pod got, so a workspace created after planning recreates the pod."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "agents:\n  notes:\n    display_name: Notes\n    private:\n      per: user\n"
        "models:\n  default:\n    provider: openai\n    id: test-model\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_primary_runtime_paths(config_path=config_path, storage_path=tmp_path / "storage")
    backend, apps_api, _core_api = _backend(runtime_paths=runtime_paths)
    worker_key = "v1:tenant-123:user:~@alice:localhost"
    workspace = private_instance_scope_root_path(backend.storage_root, worker_key) / "notes" / "notes_data"
    mount_path = f"/app/worker/{workspace.relative_to(backend.storage_root).as_posix()}"
    if materialized == "during_cold_start":
        wait_for_ready = backend._resources.wait_for_ready

        def materialize_while_starting(*args: object, **kwargs: object) -> object:
            workspace.mkdir(parents=True, exist_ok=True)
            return wait_for_ready(*args, **kwargs)

        monkeypatch.setattr(backend._resources, "wait_for_ready", materialize_while_starting)

    backend.ensure_worker(_spec(worker_key), now=10.0)
    assert mount_path not in _storage_mounts(apps_api.created_bodies[-1])
    if materialized == "after_ready_then_touched":
        workspace.mkdir(parents=True)
        backend.touch_worker(worker_key, now=15.0)
    backend.ensure_worker(_spec(worker_key), now=20.0)

    assert len(apps_api.created_bodies) == 2
    assert mount_path in _storage_mounts(apps_api.created_bodies[-1])


def test_kubernetes_failure_from_a_replaced_pod_never_stops_its_replacement(tmp_path: Path) -> None:
    """A request that failed on the pod a workspace change replaced must not scale down the new Deployment."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "agents:\n  notes:\n    display_name: Notes\n    private:\n      per: user\n"
        "models:\n  default:\n    provider: openai\n    id: test-model\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_primary_runtime_paths(config_path=config_path, storage_path=tmp_path / "storage")
    backend, apps_api, _core_api = _backend(runtime_paths=runtime_paths)
    worker_key = "v1:tenant-123:user:~@alice:localhost"
    old_handle = backend.ensure_worker(_spec(worker_key), now=10.0)
    (private_instance_scope_root_path(backend.storage_root, worker_key) / "notes" / "notes_data").mkdir(parents=True)
    new_handle = backend.ensure_worker(_spec(worker_key), now=20.0)
    assert new_handle.startup_count != old_handle.startup_count

    backend.record_failure(worker_key, "connection reset", now=21.0, startup_count=old_handle.startup_count)
    assert apps_api.deployments[new_handle.worker_id].spec.replicas == 1

    backend.record_failure(worker_key, "runner crashed", now=22.0, startup_count=new_handle.startup_count)
    assert apps_api.deployments[new_handle.worker_id].spec.replicas == 0


def test_kubernetes_shared_worker_reuse_skips_mount_planning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only user workers can gain a workspace while running, so other cached workers never re-plan on reuse."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
    )
    backend, _apps_api, _core_api = _backend(runtime_paths=runtime_paths)
    backend.ensure_worker(_spec("v1:tenant-123:shared:helper"), now=10.0)
    monkeypatch.setattr(
        kubernetes_backend_module,
        "plan_scoped_workspace_mounts",
        lambda **_kwargs: pytest.fail("cached shared workers must not re-plan their mounts"),
    )

    backend.ensure_worker(_spec("v1:tenant-123:shared:helper"), now=20.0)


def test_kubernetes_backend_recreates_user_agent_deployment_when_private_visibility_changes(tmp_path: Path) -> None:
    """Changing private visibility should recreate the Deployment instead of relying on patch semantics."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
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

    assert worker_key is not None
    private_workspace = _materialize_private_workspace(backend.storage_root, worker_key, "@alice:example.org")

    backend.ensure_worker(_spec(worker_key, private_agent_names=frozenset()), now=10.0)
    backend.ensure_worker(_spec(worker_key, private_agent_names=frozenset({"mind"})), now=20.0)

    assert len(apps_api.created_bodies) == 2
    recreated_worker_id = apps_api.created_bodies[-1]["metadata"]["name"]
    assert apps_api.deleted_names == [recreated_worker_id]
    assert "/app/worker/agents/mind/workspace" in _storage_mounts(apps_api.created_bodies[0])
    mount_paths = {path: mount["subPath"] for path, mount in _storage_mounts(apps_api.created_bodies[-1]).items()}
    relative_workspace = private_workspace.relative_to(backend.storage_root).as_posix()

    assert mount_paths[f"/app/worker/{relative_workspace}"] == relative_workspace
    assert f"/app/worker/private_instances/{worker_dir_name(worker_key)}" not in mount_paths
    assert not any(path.startswith("/app/worker/agents/") for path in mount_paths)


def test_kubernetes_backend_waits_for_deployment_deletion_before_recreate(tmp_path: Path) -> None:
    """Template-drift replacement should wait for actual deletion instead of patching a terminating Deployment."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
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

    assert worker_key is not None
    private_workspace = _materialize_private_workspace(backend.storage_root, worker_key, "@alice:example.org")

    backend.ensure_worker(_spec(worker_key, private_agent_names=frozenset()), now=10.0)
    worker_id = apps_api.created_bodies[0]["metadata"]["name"]
    apps_api.delete_read_lag_by_name[worker_id] = 1

    backend.ensure_worker(_spec(worker_key, private_agent_names=frozenset({"mind"})), now=20.0)

    assert apps_api.deleted_names == [worker_id]
    assert len(apps_api.created_bodies) == 2
    relative_workspace = private_workspace.relative_to(backend.storage_root).as_posix()
    assert (
        _storage_mounts(apps_api.created_bodies[-1])[f"/app/worker/{relative_workspace}"]["subPath"]
        == relative_workspace
    )


@pytest.mark.parametrize("worker_key", ["v1:tenant-123:unscoped:general", "v1:tenant-123:user:@alice:example.org"])
def test_kubernetes_backend_mirrors_shared_credentials_for_unscoped_and_scoped_workers(
    monkeypatch: pytest.MonkeyPatch,
    worker_key: str,
) -> None:
    """Unscoped and scoped dedicated workers mirror shared UI credentials through the same allowlisted sync."""
    backend, _apps_api, _core_api = _backend()
    sync_calls: list[tuple[str, frozenset[str] | None]] = []

    def _record_sync(
        worker_key: str,
        *,
        allowed_services: frozenset[str] | None = None,
        credentials_manager: object | None = None,
    ) -> None:
        del credentials_manager
        sync_calls.append((worker_key, allowed_services))

    monkeypatch.setattr(
        "mindroom.workers.backends.kubernetes.sync_shared_credentials_to_worker",
        _record_sync,
    )

    backend.ensure_worker(_spec(worker_key), now=10.0)

    assert sync_calls == [(worker_key, DEFAULT_WORKER_GRANTABLE_CREDENTIALS)]


def test_kubernetes_backend_uses_configured_worker_grantable_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dedicated workers should use the config-authored worker credential allowlist."""
    backend, _apps_api, _core_api = _backend(
        worker_grantable_credentials=frozenset({"openai", "github_private"}),
    )
    sync_calls: list[frozenset[str] | None] = []

    def _record_sync(
        worker_key: str,
        *,
        allowed_services: frozenset[str] | None = None,
        credentials_manager: object | None = None,
    ) -> None:
        del worker_key, credentials_manager
        sync_calls.append(allowed_services)

    monkeypatch.setattr(
        "mindroom.workers.backends.kubernetes.sync_shared_credentials_to_worker",
        _record_sync,
    )

    backend.ensure_worker(_spec("v1:tenant-123:user:@alice:example.org"), now=10.0)
    backend.ensure_worker(_spec("v1:tenant-123:user:@alice:example.org"), now=20.0)

    assert _launch_of(backend).grantable_credentials == frozenset({"openai", "github_private"})
    assert sync_calls == [frozenset({"openai", "github_private"})] * 2


def test_kubernetes_backend_uses_empty_worker_grantable_credentials_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dedicated workers should pass through an explicit deny-all worker credential allowlist."""
    backend, _apps_api, _core_api = _backend(worker_grantable_credentials=frozenset())
    sync_calls: list[frozenset[str] | None] = []

    def _record_sync(
        worker_key: str,
        *,
        allowed_services: frozenset[str] | None = None,
        credentials_manager: object | None = None,
    ) -> None:
        del worker_key, credentials_manager
        sync_calls.append(allowed_services)

    monkeypatch.setattr(
        "mindroom.workers.backends.kubernetes.sync_shared_credentials_to_worker",
        _record_sync,
    )

    backend.ensure_worker(_spec("v1:tenant-123:user:@alice:example.org"), now=10.0)

    assert sync_calls == [frozenset()]


def test_kubernetes_script_worker_profile_mirrors_no_global_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A script-specific Kubernetes worker denies automatic credential mirroring."""
    backend, _apps_api, _core_api = _backend(
        worker_grantable_credentials=frozenset({"openai", "github_private"}),
    )
    sync_calls: list[frozenset[str] | None] = []

    def _record_sync(
        worker_key: str,
        *,
        allowed_services: frozenset[str] | None = None,
        credentials_manager: object | None = None,
    ) -> None:
        del worker_key, credentials_manager
        sync_calls.append(allowed_services)

    monkeypatch.setattr(
        "mindroom.workers.backends.kubernetes.sync_shared_credentials_to_worker",
        _record_sync,
    )

    backend.ensure_worker(
        _spec(
            "v1:tenant-123:user:@alice:example.org",
            mirror_grantable_credentials=False,
        ),
        now=10.0,
    )
    backend.ensure_worker(
        _spec(
            "v1:tenant-123:user:@alice:example.org",
            mirror_grantable_credentials=False,
        ),
        now=20.0,
    )

    assert sync_calls == [frozenset()] * 2


def test_kubernetes_backend_cleanup_scales_idle_workers_to_zero() -> None:
    """Idle cleanup should scale dedicated workers to zero while keeping their metadata."""
    backend, apps_api, core_api = _backend(idle_timeout_seconds=5.0)
    handle = backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=0.0)
    deployment = apps_api.deployments[handle.worker_id]
    deployment.metadata.annotations["mindroom.ai/last-used-at"] = "0.0"
    deployment.metadata.annotations["mindroom.ai/worker-status"] = "ready"

    cleaned = backend.cleanup_idle_workers(now=10.0)

    assert len(cleaned) == 1
    assert cleaned[0].worker_key == _TEST_SCOPED_WORKER_KEY_A
    assert cleaned[0].status == "idle"
    assert deployment.spec.replicas == 0
    assert apps_api.deployments[handle.worker_id].metadata.annotations["mindroom.ai/worker-status"] == "idle"
    assert handle.worker_id not in core_api.services
    assert handle.worker_id not in core_api.secrets


@pytest.mark.parametrize("stored_status", ["ready", "starting", "failed"])
def test_kubernetes_backend_unready_workers_respect_idle_expiry(stored_status: str) -> None:
    """Only previously ready workers expire after losing pod readiness."""
    backend, apps_api, core_api = _backend(idle_timeout_seconds=5.0)
    handle = backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=0.0)
    deployment = apps_api.deployments[handle.worker_id]
    deployment.status.ready_replicas = 0
    deployment.metadata.annotations["mindroom.ai/worker-status"] = stored_status

    assert backend.cleanup_idle_workers(now=4.0) == []
    assert deployment.spec.replicas == 1
    cleaned = backend.cleanup_idle_workers(now=10.0)

    if stored_status == "ready":
        assert [worker.worker_id for worker in cleaned] == [handle.worker_id]
        assert deployment.spec.replicas == 0
        assert handle.worker_id not in core_api.services
        assert handle.worker_id not in core_api.secrets
    else:
        assert cleaned == []
        assert deployment.spec.replicas == 1
        assert handle.worker_id in core_api.services
        assert handle.worker_id in core_api.secrets


def test_kubernetes_backend_cleanup_skips_locked_worker() -> None:
    """Maintenance must leave an in-flight ensure alone and continue other cleanup."""
    backend, apps_api, _core_api = _backend(idle_timeout_seconds=5.0)
    held = backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=0.0)
    free = backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_B), now=0.0)

    with backend._worker_locks.for_key(held.worker_key):
        cleaned = backend.cleanup_idle_workers(now=10.0)

    assert [worker.worker_id for worker in cleaned] == [free.worker_id]
    assert apps_api.deployments[held.worker_id].spec.replicas == 1


@pytest.mark.parametrize("change", ["touch", "ensure", "delete"])
def test_kubernetes_backend_cleanup_revalidates_list_snapshot(change: str) -> None:
    """A stale idle nomination cannot undo newer worker use or recreate deleted state."""
    backend, apps_api, core_api = _backend(idle_timeout_seconds=5.0)
    handle = backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=0.0)
    stale_deployment = deepcopy(apps_api.deployments[handle.worker_id])
    if change == "touch":
        backend.touch_worker(handle.worker_key, now=9.0)
    elif change == "ensure":
        backend.ensure_worker(_spec(handle.worker_key), now=9.0)
    else:
        del apps_api.deployments[handle.worker_id]

    cleaned = backend._cleanup_idle_deployments([stale_deployment], now=10.0)

    assert cleaned == []
    assert handle.worker_id in core_api.services
    assert handle.worker_id in core_api.secrets
    if change != "delete":
        assert apps_api.deployments[handle.worker_id].spec.replicas == 1


def test_kubernetes_backend_cleanup_is_idempotent_for_already_idle_workers() -> None:
    """Cleanup should not report or patch workers that are already scaled to zero."""
    backend, apps_api, _core_api = _backend(idle_timeout_seconds=5.0)
    handle = backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=0.0)
    deployment = apps_api.deployments[handle.worker_id]
    deployment.metadata.annotations["mindroom.ai/last-used-at"] = "0.0"
    deployment.metadata.annotations["mindroom.ai/worker-status"] = "ready"

    first_cleaned = backend.cleanup_idle_workers(now=10.0)
    patch_count_after_first_cleanup = len(apps_api.patched_bodies)
    second_cleaned = backend.cleanup_idle_workers(now=11.0)

    assert [worker.worker_key for worker in first_cleaned] == [_TEST_SCOPED_WORKER_KEY_A]
    assert second_cleaned == []
    assert len(apps_api.patched_bodies) == patch_count_after_first_cleanup


def test_kubernetes_backend_cleanup_idle_deletes_service_but_keeps_deployment() -> None:
    """Idle cleanup should scale down the worker and release its Service."""
    backend, apps_api, core_api = _backend()
    handle = backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=0.0)

    cleaned = backend.cleanup_idle_workers(now=80.0)

    assert [worker.worker_key for worker in cleaned] == [_TEST_SCOPED_WORKER_KEY_A]
    assert cleaned[0].status == "idle"
    assert handle.worker_id in apps_api.deployments
    assert apps_api.deployments[handle.worker_id].spec.replicas == 0
    assert handle.worker_id not in core_api.services
    assert handle.worker_id not in core_api.secrets


def _wire_fake_apis(backend: KubernetesWorkerBackend, apps_api: _FakeAppsApi, core_api: _FakeCoreApi) -> None:
    backend._resources.apps_api = apps_api
    backend._resources.core_api = core_api
    backend._resources.api_exception_cls = _FakeApiError


def _kubernetes_api_call_counts(apps_api: _FakeAppsApi, core_api: _FakeCoreApi) -> tuple[int, ...]:
    return (
        len(apps_api.read_names),
        len(apps_api.created_bodies),
        len(apps_api.patched_bodies),
        len(apps_api.deleted_names),
        len(core_api.read_service_names),
        len(core_api.created_bodies),
        len(core_api.read_secret_names),
        len(core_api.created_secret_bodies),
        len(core_api.patched_secret_bodies),
        len(core_api.deleted_secret_names),
    )


def test_kubernetes_backend_reuses_recent_ready_worker_with_one_metadata_patch() -> None:
    """A recently validated ready worker should skip cluster reconciliation."""
    backend, apps_api, core_api = _backend()
    first = backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)
    calls_after_first_ensure = _kubernetes_api_call_counts(apps_api, core_api)

    second = backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=20.0)

    assert second.worker_id == first.worker_id
    assert second.last_used_at == 20.0
    calls_after_second_ensure = _kubernetes_api_call_counts(apps_api, core_api)
    assert tuple(
        after - before for before, after in zip(calls_after_first_ensure, calls_after_second_ensure, strict=True)
    ) == (
        0,
        0,
        1,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
    )
    assert apps_api.deployments[first.worker_id].metadata.annotations["mindroom.ai/last-used-at"] == "20.0"


def _write_two_shared_agents_config(tmp_path: Path, *, research_scope: str) -> RuntimePaths:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
agents:
  code:
    display_name: Code
    role: Code test
    model: default
    worker_scope: shared
  research:
    display_name: Research
    role: Research test
    model: default
    worker_scope: {research_scope}
models:
  default:
    provider: openai
    id: gpt-6-astra
router:
  model: default
""".lstrip(),
        encoding="utf-8",
    )
    return resolve_primary_runtime_paths(config_path=config_path, storage_path=tmp_path / "storage")


def test_kubernetes_backend_refuses_to_provision_before_a_config_is_applied(tmp_path: Path) -> None:
    """A dedicated worker never starts without the applied config's launch, and nothing is touched first."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
    )
    backend, apps_api, core_api = _backend(runtime_paths=runtime_paths)
    launch = _launch_of(backend)
    _BACKEND_LAUNCHES[backend] = None
    progress: list[WorkerReadyProgress] = []

    with pytest.raises(WorkerBackendError, match="applied config"):
        backend.ensure_worker(WorkerSpec(_TEST_SCOPED_WORKER_KEY_A), now=10.0, progress_sink=progress.append)

    assert _kubernetes_api_call_counts(apps_api, core_api) == (0,) * 10
    assert progress == []
    assert not runtime_paths.storage_root.exists()

    # Nor does a refused request evict the ready worker an applied launch cached.
    _BACKEND_LAUNCHES[backend] = launch
    backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=20.0)
    _BACKEND_LAUNCHES[backend] = None
    with pytest.raises(WorkerBackendError, match="applied config"):
        backend.ensure_worker(WorkerSpec(_TEST_SCOPED_WORKER_KEY_A), now=30.0)
    _BACKEND_LAUNCHES[backend] = launch
    deployment_reads = len(apps_api.read_names)
    backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=40.0)
    assert len(apps_api.read_names) == deployment_reads


def test_kubernetes_backend_provisions_a_waiting_request_from_the_launch_applied_when_it_gets_the_lock(
    tmp_path: Path,
) -> None:
    """A request queued behind the worker's lock while a newer config is applied provisions from the newer one."""
    runtime_paths = _write_two_shared_agents_config(tmp_path, research_scope="shared")
    backend, apps_api, _core_api = _backend(runtime_paths=runtime_paths)
    newer = _launch(_write_two_shared_agents_config(tmp_path, research_scope="user"))
    worker_lock = backend._worker_locks.for_key(_TEST_SCOPED_WORKER_KEY_A)
    handles: list[WorkerHandle] = []
    with worker_lock:
        waiting = threading.Thread(
            target=lambda: handles.append(backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)),
        )
        waiting.start()
        _BACKEND_LAUNCHES[backend] = newer
    waiting.join(timeout=30)
    assert not waiting.is_alive()

    deployment = apps_api.deployments[handles[0].worker_id]
    newer_backend, newer_apps_api, _ = _backend(runtime_paths=runtime_paths)
    _BACKEND_LAUNCHES[newer_backend] = newer
    newer_handle = newer_backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)
    assert (
        deployment.metadata.annotations[ANNOTATION_TEMPLATE_HASH]
        == newer_apps_api.deployments[newer_handle.worker_id].metadata.annotations[ANNOTATION_TEMPLATE_HASH]
    )


def test_kubernetes_backend_reprovisions_a_cached_worker_when_the_launch_changes(tmp_path: Path) -> None:
    """A new launch goes through provisioning, and another agent's change keeps this worker's pod."""
    runtime_paths = _write_two_shared_agents_config(tmp_path, research_scope="shared")
    backend, apps_api, _core_api = _backend(runtime_paths=runtime_paths)
    first = backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)
    first_template_hash = apps_api.deployments[first.worker_id].metadata.annotations[ANNOTATION_TEMPLATE_HASH]
    deployment_reads_before = len(apps_api.read_names)

    changed = _launch(_write_two_shared_agents_config(tmp_path, research_scope="user"))
    assert changed.digest != _launch_of(backend).digest
    _BACKEND_LAUNCHES[backend] = changed
    backend.ensure_worker(WorkerSpec(_TEST_SCOPED_WORKER_KEY_A), now=20.0)

    # Provisioning reads the live Deployment, which the cached path never does.
    assert len(apps_api.read_names) > deployment_reads_before
    assert apps_api.deleted_names == []
    assert len(apps_api.created_bodies) == 1
    assert apps_api.deployments[first.worker_id].metadata.annotations[ANNOTATION_TEMPLATE_HASH] == first_template_hash


def test_kubernetes_backend_reuses_a_cached_worker_for_an_equal_launch(tmp_path: Path) -> None:
    """A launch rebuilt from the same applied config compares equal, so the cached worker is reused."""
    runtime_paths = _write_two_shared_agents_config(tmp_path, research_scope="shared")
    backend, apps_api, core_api = _backend(runtime_paths=runtime_paths)
    backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)
    calls_before = _kubernetes_api_call_counts(apps_api, core_api)

    rebuilt = _launch(runtime_paths)
    assert rebuilt is not _launch_of(backend)
    _BACKEND_LAUNCHES[backend] = rebuilt
    backend.ensure_worker(WorkerSpec(_TEST_SCOPED_WORKER_KEY_A), now=20.0)

    calls_after = _kubernetes_api_call_counts(apps_api, core_api)
    assert tuple(after - before for before, after in zip(calls_before, calls_after, strict=True)) == (
        0,
        0,
        1,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
    )


def test_kubernetes_backend_reconciles_when_cached_worker_disappears() -> None:
    """A missing cached Deployment should fall back to full reconciliation."""
    backend, apps_api, _core_api = _backend()
    first = backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)
    apps_api.deployments.pop(first.worker_id)

    recreated = backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=20.0)

    assert recreated.status == "ready"
    assert len(apps_api.created_bodies) == 2


def test_kubernetes_backend_periodically_revalidates_cached_ready_worker() -> None:
    """The ready cache should bound how long external cluster drift can stay hidden."""
    backend, apps_api, core_api = _backend(idle_timeout_seconds=600.0)
    backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)
    calls_after_first_ensure = _kubernetes_api_call_counts(apps_api, core_api)

    backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=309.0)
    calls_before_revalidation = _kubernetes_api_call_counts(apps_api, core_api)
    assert calls_before_revalidation[0] == calls_after_first_ensure[0]
    assert calls_before_revalidation[2] == calls_after_first_ensure[2] + 1
    assert calls_before_revalidation[4:] == calls_after_first_ensure[4:]

    backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=310.0)
    calls_after_revalidation = _kubernetes_api_call_counts(apps_api, core_api)
    assert calls_after_revalidation[0] > calls_before_revalidation[0]
    assert calls_after_revalidation[4] > calls_before_revalidation[4]
    assert calls_after_revalidation[6] > calls_before_revalidation[6]


def test_kubernetes_backend_cached_touch_uses_one_metadata_patch() -> None:
    """A cached touch should persist usage without reading or reconciling cluster objects."""
    backend, apps_api, _core_api = _backend()
    handle = backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)
    patch_count_after_ensure = len(apps_api.patched_bodies)
    read_count_after_ensure = len(apps_api.read_names)

    first_touch = backend.touch_worker(_TEST_SCOPED_WORKER_KEY_A, now=11.0)
    second_touch = backend.touch_worker(_TEST_SCOPED_WORKER_KEY_A, now=12.0)

    assert first_touch is not None
    assert second_touch is not None
    assert second_touch.last_used_at == 12.0
    assert len(apps_api.patched_bodies) == patch_count_after_ensure + 2
    assert len(apps_api.read_names) == read_count_after_ensure
    assert backend.list_workers(now=12.0)[0].last_used_at == 12.0
    assert apps_api.deployments[handle.worker_id].metadata.annotations["mindroom.ai/last-used-at"] == "12.0"
    assert apps_api.deployments[handle.worker_id].metadata.annotations[ANNOTATION_WORKER_KEY] == (
        _TEST_SCOPED_WORKER_KEY_A
    )


def test_kubernetes_backend_cached_touch_keeps_lifecycle_history_another_manager_wrote() -> None:
    """A cached touch writes only usage, so a restart another manager recorded on the Deployment survives it."""
    backend, apps_api, _core_api = _backend()
    handle = backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)
    annotations = apps_api.deployments[handle.worker_id].metadata.annotations
    # Another manager, for example one built after a kubeconfig rotation, restarted the worker meanwhile.
    annotations["mindroom.ai/startup-count"] = "2"
    annotations["mindroom.ai/last-started-at"] = "11.5"
    annotations["mindroom.ai/failure-count"] = "1"

    backend.touch_worker(_TEST_SCOPED_WORKER_KEY_A, now=12.0)

    annotations = apps_api.deployments[handle.worker_id].metadata.annotations
    assert annotations["mindroom.ai/last-used-at"] == "12.0"
    assert annotations["mindroom.ai/worker-status"] == "ready"
    assert (
        annotations["mindroom.ai/startup-count"],
        annotations["mindroom.ai/last-started-at"],
        annotations["mindroom.ai/failure-count"],
    ) == ("2", "11.5", "1")


def test_kubernetes_backend_failure_invalidates_ready_cache() -> None:
    """A failed worker must take the full restart path on its next ensure."""
    backend, apps_api, core_api = _backend()
    backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)
    backend.record_failure(_TEST_SCOPED_WORKER_KEY_A, "worker failed", now=11.0)
    calls_after_failure = _kubernetes_api_call_counts(apps_api, core_api)

    restarted = backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=12.0)

    assert restarted.status == "ready"
    assert restarted.startup_count == 2
    assert _kubernetes_api_call_counts(apps_api, core_api) != calls_after_failure


@pytest.mark.parametrize("crashed", [False, True])
def test_kubernetes_backend_reconciles_drifted_idle_worker_template(tmp_path: Path, *, crashed: bool) -> None:
    """Reconciliation should recreate scaled-down workers whose pod template drifted from current config."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=Path("config.yaml"),
        storage_path=tmp_path / "mindroom-test-storage",
    )
    backend, apps_api, core_api = _backend(runtime_paths=runtime_paths)
    handle = backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=0.0)
    if crashed:
        apps_api.deployments[handle.worker_id].status.ready_replicas = 0
    else:
        backend.cleanup_idle_workers(now=80.0)

    updated_backend, _, _ = _backend(runtime_paths=runtime_paths, resource_limits={"memory": "2Gi", "cpu": "1"})
    _wire_fake_apis(updated_backend, apps_api, core_api)

    reconciled = updated_backend.maintain_workers(now=90.0).reconciled

    assert [worker.worker_key for worker in reconciled] == [_TEST_SCOPED_WORKER_KEY_A]
    assert reconciled[0].status == "idle"
    assert apps_api.deleted_names == [handle.worker_id]
    assert len(apps_api.created_bodies) == 2
    recreated = apps_api.created_bodies[1]
    assert recreated["spec"]["replicas"] == 0
    container = recreated["spec"]["template"]["spec"]["containers"][0]
    assert container["resources"]["limits"] == {"memory": "2Gi", "cpu": "1"}
    assert recreated["metadata"]["annotations"]["mindroom.ai/created-at"] == "0.0"


def test_kubernetes_maintenance_without_a_launch_cleans_idle_workers_but_never_reconciles(tmp_path: Path) -> None:
    """Before an applied config exists, maintenance still scales idle workers down but renders nothing."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=Path("config.yaml"),
        storage_path=tmp_path / "mindroom-test-storage",
    )
    backend, apps_api, core_api = _backend(runtime_paths=runtime_paths)
    handle = backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=0.0)
    updated_backend, _, _ = _backend(runtime_paths=runtime_paths, resource_limits={"memory": "2Gi", "cpu": "1"})
    _wire_fake_apis(updated_backend, apps_api, core_api)

    _BACKEND_LAUNCHES[updated_backend] = None
    maintenance = updated_backend.maintain_workers(now=90.0)

    assert [worker.worker_key for worker in maintenance.cleaned] == [_TEST_SCOPED_WORKER_KEY_A]
    assert maintenance.reconciled == ()
    assert apps_api.deployments[handle.worker_id].spec.replicas == 0
    assert apps_api.deleted_names == []
    assert len(apps_api.created_bodies) == 1


def test_kubernetes_maintenance_reconciles_against_the_launch_it_is_given(tmp_path: Path) -> None:
    """A scaled-down worker is re-rendered from the pass's launch, here a new tool validation snapshot."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=Path("config.yaml"),
        storage_path=tmp_path / "mindroom-test-storage",
    )
    backend, apps_api, _core_api = _backend(runtime_paths=runtime_paths)
    handle = backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=0.0)
    backend.cleanup_idle_workers(now=80.0)
    assert backend.maintain_workers(now=85.0).reconciled == ()
    updated_snapshot = deepcopy(_TEST_TOOL_VALIDATION_SNAPSHOT)
    updated_snapshot["calculator"]["runtime_loadable"] = False
    updated_launch = _launch(runtime_paths, tool_validation_snapshot=updated_snapshot)

    _BACKEND_LAUNCHES[backend] = updated_launch
    reconciled = backend.maintain_workers(now=90.0).reconciled

    assert [worker.worker_key for worker in reconciled] == [_TEST_SCOPED_WORKER_KEY_A]
    assert apps_api.deleted_names == [handle.worker_id]
    recreated_annotations = apps_api.created_bodies[1]["spec"]["template"]["metadata"]["annotations"]
    assert recreated_annotations[_ANNOTATION_STARTUP_MANIFEST_HASH] == startup_manifest_sha256(
        backend._resources._worker_runtime_paths(
            worker_key=_TEST_SCOPED_WORKER_KEY_A,
            state_subpath=backend._state_subpath(_TEST_SCOPED_WORKER_KEY_A),
        ),
        tool_validation_snapshot=updated_snapshot,
        public_runtime=True,
    )


def test_kubernetes_backend_recreates_ready_worker_for_changed_runtime_class(tmp_path: Path) -> None:
    """An existing worker cannot be adopted across a RuntimeClass boundary."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=Path("config.yaml"),
        storage_path=tmp_path / "mindroom-test-storage",
    )
    backend, apps_api, core_api = _backend(runtime_paths=runtime_paths)
    handle = backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=0.0)
    updated_backend, _, _ = _backend(runtime_paths=runtime_paths, runtime_class_name="sandboxed")
    _wire_fake_apis(updated_backend, apps_api, core_api)

    recreated = updated_backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    assert recreated.worker_key == _TEST_SCOPED_WORKER_KEY_A
    assert apps_api.deleted_names == [handle.worker_id]
    assert apps_api.created_bodies[-1]["spec"]["template"]["spec"]["runtimeClassName"] == "sandboxed"


def test_kubernetes_backend_maintenance_lists_deployments_once(tmp_path: Path) -> None:
    """Cleanup and reconciliation should share one lightweight Kubernetes list response."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=Path("config.yaml"),
        storage_path=tmp_path / "mindroom-test-storage",
    )
    backend, apps_api, core_api = _backend(runtime_paths=runtime_paths)
    backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=0.0)
    updated_backend, _, _ = _backend(runtime_paths=runtime_paths, resource_limits={"memory": "2Gi", "cpu": "1"})
    _wire_fake_apis(updated_backend, apps_api, core_api)
    list_count_before = len(apps_api.list_label_selectors)

    maintenance = updated_backend.maintain_workers(now=80.0)

    assert len(apps_api.list_label_selectors) == list_count_before + 1
    assert apps_api.raw_list_count == 1
    assert [worker.worker_key for worker in maintenance.cleaned] == [_TEST_SCOPED_WORKER_KEY_A]
    assert [worker.worker_key for worker in maintenance.reconciled] == [_TEST_SCOPED_WORKER_KEY_A]


def test_kubernetes_backend_reconcile_defers_running_workers(tmp_path: Path) -> None:
    """Reconciliation should leave running workers to the ensure-time template-hash check."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=Path("config.yaml"),
        storage_path=tmp_path / "mindroom-test-storage",
    )
    backend, apps_api, core_api = _backend(runtime_paths=runtime_paths)
    backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=0.0)

    updated_backend, _, _ = _backend(runtime_paths=runtime_paths, resource_limits={"memory": "2Gi", "cpu": "1"})
    _wire_fake_apis(updated_backend, apps_api, core_api)

    reconciled = updated_backend.maintain_workers(now=10.0).reconciled

    assert reconciled == ()
    assert apps_api.deleted_names == []
    assert len(apps_api.created_bodies) == 1


def test_kubernetes_backend_reconcile_leaves_unchanged_templates_alone(tmp_path: Path) -> None:
    """Reconciliation should not touch scaled-down workers whose template still matches current config."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=Path("config.yaml"),
        storage_path=tmp_path / "mindroom-test-storage",
    )
    backend, apps_api, core_api = _backend(runtime_paths=runtime_paths)
    backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=0.0)
    backend.cleanup_idle_workers(now=80.0)
    patch_count_after_cleanup = len(apps_api.patched_bodies)

    unchanged_backend, _, _ = _backend(runtime_paths=runtime_paths)
    _wire_fake_apis(unchanged_backend, apps_api, core_api)

    reconciled = unchanged_backend.maintain_workers(now=90.0).reconciled

    assert reconciled == ()
    assert apps_api.deleted_names == []
    assert len(apps_api.created_bodies) == 1
    assert len(apps_api.patched_bodies) == patch_count_after_cleanup


def test_kubernetes_backend_reconcile_disabled_by_config(tmp_path: Path) -> None:
    """Disabling pod-template reconciliation should keep drifted scaled-down workers untouched."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=Path("config.yaml"),
        storage_path=tmp_path / "mindroom-test-storage",
    )
    backend, apps_api, core_api = _backend(runtime_paths=runtime_paths)
    backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=0.0)
    backend.cleanup_idle_workers(now=80.0)

    updated_backend, _, _ = _backend(
        runtime_paths=runtime_paths,
        resource_limits={"memory": "2Gi", "cpu": "1"},
        reconcile_pod_templates=False,
    )
    _wire_fake_apis(updated_backend, apps_api, core_api)

    reconciled = updated_backend.maintain_workers(now=90.0).reconciled

    assert reconciled == ()
    assert apps_api.deleted_names == []
    assert len(apps_api.created_bodies) == 1


def test_kubernetes_backend_reconcile_uses_persisted_private_visibility(tmp_path: Path) -> None:
    """Reconciliation should rebuild the private workspace mount from persisted visibility."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=Path("config.yaml"),
        storage_path=tmp_path / "mindroom-test-storage",
    )
    backend, apps_api, core_api = _backend(runtime_paths=runtime_paths)
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
    assert worker_key is not None
    private_workspace = _materialize_private_workspace(backend.storage_root, worker_key, "@alice:example.org")
    backend.ensure_worker(_spec(worker_key, private_agent_names=frozenset({"mind"})), now=0.0)
    backend.cleanup_idle_workers(now=80.0)

    unchanged_backend, _, _ = _backend(runtime_paths=runtime_paths)
    _wire_fake_apis(unchanged_backend, apps_api, core_api)
    assert unchanged_backend.maintain_workers(now=90.0).reconciled == ()

    updated_backend, _, _ = _backend(runtime_paths=runtime_paths, resource_limits={"memory": "2Gi", "cpu": "1"})
    _wire_fake_apis(updated_backend, apps_api, core_api)

    reconciled = updated_backend.maintain_workers(now=100.0).reconciled

    assert [worker.worker_key for worker in reconciled] == [worker_key]
    recreated = apps_api.created_bodies[-1]
    assert recreated["metadata"]["annotations"][_ANNOTATION_PRIVATE_AGENT_NAMES] == '["mind"]'
    assert f"/app/worker/{private_workspace.relative_to(backend.storage_root).as_posix()}" in _storage_mounts(recreated)


def test_kubernetes_backend_reconcile_uses_persisted_script_state_scope(tmp_path: Path) -> None:
    """Template reconciliation must preserve a script worker's private workspace mount."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=Path("config.yaml"),
        storage_path=tmp_path / "mindroom-test-storage",
    )
    backend, apps_api, core_api = _backend(runtime_paths=runtime_paths)
    state_scope_worker_key = "v1:tenant-123:user_agent:~@alice:example.org:mind"
    worker_key = script_worker_key_for_run(state_scope_worker_key, f"script-{'a' * 32}")
    private_workspace = _materialize_private_workspace(
        backend.storage_root,
        state_scope_worker_key,
        "@alice:example.org",
    )
    backend.ensure_worker(
        _spec(
            worker_key,
            private_agent_names=frozenset({"mind"}),
            state_scope_worker_key=state_scope_worker_key,
        ),
        now=0.0,
    )
    backend.cleanup_idle_workers(now=80.0)

    updated_backend, _, _ = _backend(runtime_paths=runtime_paths, resource_limits={"memory": "2Gi", "cpu": "1"})
    _wire_fake_apis(updated_backend, apps_api, core_api)

    reconciled = updated_backend.maintain_workers(now=100.0).reconciled

    assert [worker.worker_key for worker in reconciled] == [worker_key]
    recreated = apps_api.created_bodies[-1]
    assert recreated["metadata"]["annotations"][_ANNOTATION_STATE_SCOPE_WORKER_KEY] == state_scope_worker_key
    assert f"/app/worker/{private_workspace.relative_to(backend.storage_root).as_posix()}" in _storage_mounts(recreated)


def test_kubernetes_backend_reconcile_revalidates_live_state_before_recreating(tmp_path: Path) -> None:
    """A worker provisioned between the list snapshot and the lock must not be scaled back down."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=Path("config.yaml"),
        storage_path=tmp_path / "mindroom-test-storage",
    )
    backend, apps_api, core_api = _backend(runtime_paths=runtime_paths)
    handle = backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=0.0)
    backend.cleanup_idle_workers(now=80.0)
    stale_deployment = deepcopy(apps_api.deployments[handle.worker_id])

    updated_backend, _, _ = _backend(runtime_paths=runtime_paths, resource_limits={"memory": "2Gi", "cpu": "1"})
    _wire_fake_apis(updated_backend, apps_api, core_api)
    updated_backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=85.0)

    def _stale_snapshot() -> list[object]:
        return [stale_deployment]

    updated_backend._resources.list_deployments = _stale_snapshot

    reconciled = updated_backend.maintain_workers(now=90.0).reconciled

    assert reconciled == ()
    live_deployment = apps_api.deployments[handle.worker_id]
    assert int(live_deployment.spec.replicas) == 1
    assert live_deployment.metadata.annotations[ANNOTATION_WORKER_KEY] == _TEST_SCOPED_WORKER_KEY_A
    assert (
        live_deployment.metadata.annotations[ANNOTATION_TEMPLATE_HASH]
        != stale_deployment.metadata.annotations[ANNOTATION_TEMPLATE_HASH]
    )


def test_kubernetes_backend_reconcile_defers_user_agent_workers_without_persisted_visibility(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """User-agent workers without persisted visibility defer to ensure-time recreation without warning each pass."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=Path("config.yaml"),
        storage_path=tmp_path / "mindroom-test-storage",
    )
    backend, apps_api, core_api = _backend(runtime_paths=runtime_paths)
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
    handle = backend.ensure_worker(_spec(worker_key, private_agent_names=frozenset({"mind"})), now=0.0)
    backend.cleanup_idle_workers(now=80.0)
    del apps_api.deployments[handle.worker_id].metadata.annotations[_ANNOTATION_PRIVATE_AGENT_NAMES]

    updated_backend, _, _ = _backend(runtime_paths=runtime_paths, resource_limits={"memory": "2Gi", "cpu": "1"})
    _wire_fake_apis(updated_backend, apps_api, core_api)

    with caplog.at_level("WARNING", logger=kubernetes_backend_module.__name__):
        reconciled = updated_backend.maintain_workers(now=90.0).reconciled

    assert reconciled == ()
    assert apps_api.deleted_names == []
    assert len(apps_api.created_bodies) == 1
    assert caplog.records == []


def test_kubernetes_backend_config_reads_reconcile_pod_templates_from_env(tmp_path: Path) -> None:
    """Pod-template reconciliation defaults on and is disabled through runtime env."""
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-6-astra\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    base_env = (
        "MINDROOM_WORKER_BACKEND=kubernetes\n"
        "MINDROOM_KUBERNETES_WORKER_IMAGE=test-image\n"
        "MINDROOM_KUBERNETES_WORKER_STORAGE_PVC_NAME=test-pvc\n"
    )
    (config_dir / ".env").write_text(base_env, encoding="utf-8")

    default_config = KubernetesWorkerBackendConfig.from_runtime(resolve_primary_runtime_paths(config_path=config_path))

    assert default_config.reconcile_pod_templates is True

    (config_dir / ".env").write_text(
        base_env + "MINDROOM_KUBERNETES_WORKER_RECONCILE_POD_TEMPLATES=false\n",
        encoding="utf-8",
    )

    disabled_config = KubernetesWorkerBackendConfig.from_runtime(
        resolve_primary_runtime_paths(config_path=config_path),
    )

    assert disabled_config.reconcile_pod_templates is False


def test_kubernetes_backend_list_workers_is_scoped_to_backend_labels() -> None:
    """Worker discovery should stay confined to this backend's label set within a shared namespace."""
    backend, apps_api, _core_api = _backend()
    handle = backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=0.0)

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


@pytest.mark.parametrize("replicas", [0, 1])
@pytest.mark.parametrize("custom_labels", [False, True])
def test_storage_preflight_rejects_deployments_without_mutation(replicas: int, custom_labels: bool) -> None:
    """Remaining controllers block migration even when scaled down or custom labels changed."""
    backend, apps_api, core_api = _backend(owner_deployment_name="mindroom-primary")
    handle = backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=0.0)
    deployment = apps_api.deployments[handle.worker_id]
    deployment.spec.replicas = replicas
    if custom_labels:
        deployment.metadata.labels["mindroom.ai/tenant"] = "historical"
        deployment.metadata.labels["mindroom.ai/component"] = "custom"
    sentinel = backend.storage_root / "credentials" / "sentinel.bin"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_bytes(b"retained credential bytes")
    services, secrets = deepcopy(core_api.services), deepcopy(core_api.secrets)
    with pytest.raises(WorkerBackendError, match=r"(?i)remove|absent"):
        backend._resources.check_workers_absent_for_storage_upgrade(timeout_seconds=5.0)
    assert apps_api.deleted_names == []
    assert apps_api.deployments[handle.worker_id] is deployment
    assert core_api.services == services
    assert core_api.secrets == secrets
    assert sentinel.read_bytes() == b"retained credential bytes"


@pytest.mark.parametrize("kind", ["replicaset", "pod"])
def test_storage_preflight_rejects_orphaned_controllers_and_terminating_pods(kind: str) -> None:
    """Deleting a Deployment does not prove its child controllers and Pods have disappeared."""
    backend, apps_api, core_api = _backend()
    item = _to_namespace(
        {
            "metadata": {
                "name": "lingering-worker",
                "labels": {"mindroom.ai/worker-id": "lingering-worker", "mindroom.ai/component": "custom"},
                "deletionTimestamp": "2026-01-01T00:00:00Z",
            },
        },
    )
    inventory = apps_api.replica_sets if kind == "replicaset" else core_api.pods
    inventory["lingering-worker"] = item
    with pytest.raises(WorkerBackendError, match=r"(?i)remove|absent"):
        backend._resources.check_workers_absent_for_storage_upgrade(timeout_seconds=5.0)
    assert apps_api.deleted_names == []
    assert inventory["lingering-worker"] is item


def test_storage_preflight_accepts_empty_worker_inventory_without_owner() -> None:
    """Absence does not need ownership proof and ignores non-worker resources."""
    backend, apps_api, core_api = _backend()
    core_api.pods["other"] = _to_namespace({"metadata": {"name": "other", "labels": {}}})
    backend._resources.check_workers_absent_for_storage_upgrade(timeout_seconds=5.0)
    assert apps_api.deleted_names == []
    assert core_api.pods["other"].metadata.name == "other"


@pytest.mark.parametrize("kind", ["deployment", "replicaset", "pod"])
@pytest.mark.parametrize(
    "payload",
    [b"invalid", b"{}", b'{"items": null}', b'{"items": [], "metadata": {"continue": "next"}}'],
)
def test_storage_preflight_rejects_invalid_or_incomplete_inventory(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    payload: bytes,
) -> None:
    """Only a complete empty list can establish absence for each resource kind."""
    backend, apps_api, core_api = _backend()
    api = core_api if kind == "pod" else apps_api
    response = _FakeRawResponse(payload)
    monkeypatch.setattr(
        api,
        f"list_namespaced_{kind if kind != 'replicaset' else 'replica_set'}",
        lambda *_args, **_kwargs: response,
    )
    with pytest.raises(WorkerBackendError):
        backend._resources.check_workers_absent_for_storage_upgrade(timeout_seconds=5.0)
    assert response.released
    assert apps_api.deleted_names == []


@pytest.mark.parametrize("kind", ["deployment", "replica_set", "pod"])
def test_storage_preflight_blocks_api_failure(monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    """Missing list permission or an unavailable API blocks startup without mutation."""
    backend, apps_api, core_api = _backend()
    api = core_api if kind == "pod" else apps_api

    def fail(*_args: object, **_kwargs: object) -> None:
        raise _FakeApiError(403)

    monkeypatch.setattr(api, f"list_namespaced_{kind}", fail)
    with pytest.raises(WorkerBackendError):
        backend._resources.check_workers_absent_for_storage_upgrade(timeout_seconds=5.0)
    assert apps_api.deleted_names == []


def test_storage_preflight_uses_one_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """A late empty response cannot authorize migration after the deadline."""
    backend, apps_api, _core_api = _backend()
    now = [0.0]
    monkeypatch.setattr(kubernetes_resources_module.time, "monotonic", lambda: now[0])

    def late(*_args: object, **kwargs: object) -> _FakeRawResponse:
        assert 0.0 < kwargs["_request_timeout"] <= 5.0
        assert kwargs["limit"] == 1
        now[0] = 6.0
        return _FakeRawResponse(b'{"items": []}')

    monkeypatch.setattr(apps_api, "list_namespaced_deployment", late)
    with pytest.raises(WorkerBackendError, match="timed out"):
        backend._resources.check_workers_absent_for_storage_upgrade(timeout_seconds=5.0)


def test_kubernetes_backend_touch_only_patches_deployment_metadata() -> None:
    """Refreshing worker usage must not mutate the pod template and trigger a rollout."""
    backend, apps_api, _core_api = _backend()
    handle = backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    touched = backend.touch_worker(_TEST_SCOPED_WORKER_KEY_A, now=25.0)

    assert touched is not None
    patch_name, patch_body = apps_api.patched_bodies[-1]
    assert patch_name == handle.worker_id
    assert patch_body["metadata"]["annotations"]["mindroom.ai/last-used-at"] == "25.0"
    assert "template" not in patch_body.get("spec", {})


def test_kubernetes_backend_touch_revives_idle_worker_and_clears_stale_failure_reason() -> None:
    """A touch must revive an idle worker and drop a lingering failure reason."""
    backend, apps_api, _core_api = _backend()
    handle = backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)
    deployment = apps_api.deployments[handle.worker_id]
    deployment.metadata.annotations["mindroom.ai/worker-status"] = "idle"
    deployment.metadata.annotations["mindroom.ai/failure-reason"] = "boom"

    touched = backend.touch_worker(_TEST_SCOPED_WORKER_KEY_A, now=25.0)

    assert touched is not None
    assert touched.status == "ready"
    assert touched.failure_reason is None


def test_kubernetes_backend_reensure_after_failure_clears_the_failure_reason() -> None:
    """A worker started again after a failure reports no stale failure reason, in its handle or in listings."""
    backend, apps_api, _core_api = _backend()
    handle = backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)
    backend.record_failure(_TEST_SCOPED_WORKER_KEY_A, "boom", now=11.0)

    restarted = backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=12.0)

    assert (restarted.status, restarted.failure_reason) == ("ready", None)
    listed = backend.list_workers(now=13.0)[0]
    assert (listed.status, listed.failure_reason) == ("ready", None)
    assert apps_api.deployments[handle.worker_id].metadata.annotations.get("mindroom.ai/failure-reason", "") == ""


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

    backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    deployment = apps_api.created_bodies[0]
    assert deployment["spec"]["template"]["spec"]["nodeName"] == "gke-chat-node-1"


def test_kubernetes_backend_uses_explicit_worker_node_name_when_configured() -> None:
    """Dedicated workers should honor an explicit node pin without querying the control-plane pod."""
    backend, apps_api, _core_api = _backend(node_name="gke-chat-node-2")

    backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    deployment = apps_api.created_bodies[0]
    assert deployment["spec"]["template"]["spec"]["nodeName"] == "gke-chat-node-2"


def test_kubernetes_backend_does_not_pin_workers_when_colocation_disabled() -> None:
    """RWX-capable deployments should be able to omit node pinning entirely."""
    backend, apps_api, _core_api = _backend()

    backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

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
        on_poll_tick: object | None = None,
    ) -> object:
        del timeout_seconds, deployment_ready_fn, on_poll_tick
        raise WorkerBackendError(error_message)

    backend._resources.wait_for_ready = MethodType(_boom, backend._resources)

    with pytest.raises(WorkerBackendError, match=error_message):
        backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    worker_id = next(iter(apps_api.deployments))
    deployment = apps_api.deployments[worker_id]
    assert deployment.metadata.annotations["mindroom.ai/worker-status"] == "failed"
    assert deployment.metadata.annotations["mindroom.ai/failure-reason"] == error_message
    assert deployment.metadata.annotations["mindroom.ai/failure-count"] == "1"
    assert deployment.spec.replicas == 0

    handle = backend.list_workers(now=11.0)[0]
    assert handle.status == "failed"
    assert handle.failure_reason == error_message
    assert worker_id not in _core_api.services
    assert worker_id not in _core_api.secrets


@pytest.mark.parametrize("stale_protocol", [0, 3], ids=["no-protocol", "before-environment-removals"])
def test_kubernetes_stale_worker_is_recreated_before_a_call_reaches_it(stale_protocol: int) -> None:
    """A new worker pod that speaks an older protocol is replaced once, so its new pod pulls the image again."""
    backend, apps_api, _core_api = _backend()
    reads = _answer_worker_health(
        backend,
        lambda: WORKER_PROTOCOL_VERSION if len(apps_api.created_bodies) > 1 else stale_protocol,
    )

    handle = backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    assert handle.status == "ready"
    assert handle.startup_count == 2
    assert apps_api.deleted_names == [handle.worker_id]
    assert len(apps_api.created_bodies) == 2
    assert reads == [handle.worker_id, handle.worker_id]


def test_kubernetes_stale_running_worker_is_recreated_after_primary_restart() -> None:
    """A worker left running from an older release under the same image tag is replaced on its next use."""
    backend, apps_api, _core_api = _backend()
    first_handle = backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)
    assert first_handle.startup_count == 1
    # Manager replacement, as on a primary restart, forgets which workers were already checked.
    backend.shutdown()
    _answer_worker_health(backend, lambda: WORKER_PROTOCOL_VERSION if len(apps_api.created_bodies) > 1 else 3)

    recovered_handle = backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=20.0)

    assert recovered_handle.status == "ready"
    assert recovered_handle.startup_count == 2
    assert recovered_handle.last_started_at == 20.0
    assert apps_api.deleted_names == [first_handle.worker_id]
    assert len(apps_api.created_bodies) == 2


def test_kubernetes_stale_worker_replacement_is_not_retried() -> None:
    """A replacement pod with the same protocol mismatch fails the worker after one recreate."""
    backend, apps_api, core_api = _backend()
    _answer_worker_health(backend, lambda: 3)

    with pytest.raises(WorkerBackendError, match=f"expected worker protocol {WORKER_PROTOCOL_VERSION}, got 3") as exc:
        backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    assert str(exc.value).startswith("Kubernetes worker image 'ghcr.io/mindroom-ai/mindroom:latest' is incompatible")
    assert "Use a worker image built for this MindRoom release." in str(exc.value)
    assert str(exc.value).endswith(
        "set the worker image pull policy to Always (https://docs.mindroom.chat/deployment/kubernetes/#worker-settings).",
    )
    worker_id = next(iter(apps_api.deployments))
    assert apps_api.deleted_names == [worker_id]
    assert len(apps_api.created_bodies) == 2
    deployment = apps_api.deployments[worker_id]
    assert deployment.metadata.annotations["mindroom.ai/worker-status"] == "failed"
    assert deployment.spec.replicas == 0
    assert worker_id not in core_api.services


def test_kubernetes_stale_running_worker_is_refused_when_its_replacement_is_still_stale() -> None:
    """A ready worker from an older release whose replacement lands on a cached old image is marked failed."""
    backend, apps_api, core_api = _backend()
    first_handle = backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)
    assert first_handle.status == "ready"
    # Manager replacement, as on a primary restart, forgets which workers were already checked.
    backend.shutdown()
    # Under the default `IfNotPresent` the replacement pod reuses the node's cached old image.
    _answer_worker_health(backend, lambda: 3)
    _install_real_elapsed_wait_for_ready(backend, ready_after_seconds=1.6)
    events: list[WorkerReadyProgress] = []

    with pytest.raises(WorkerBackendError, match=f"expected worker protocol {WORKER_PROTOCOL_VERSION}, got 3"):
        backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=20.0, progress_sink=events.append)

    worker_id = first_handle.worker_id
    assert apps_api.deleted_names == [worker_id]
    assert len(apps_api.created_bodies) == 2
    deployment = apps_api.deployments[worker_id]
    assert deployment.metadata.annotations["mindroom.ai/worker-status"] == "failed"
    assert deployment.spec.replicas == 0
    assert worker_id not in core_api.services
    assert [event.phase for event in events] == ["cold_start", "failed"]


def test_kubernetes_stale_worker_refusal_under_always_blames_no_cached_copy() -> None:
    """Under pull policy Always a replacement pulls the tag again, so a stale replacement means the tag itself is old."""
    backend, apps_api, _core_api = _backend(image_pull_policy="Always")
    _answer_worker_health(backend, lambda: 3)

    with pytest.raises(WorkerBackendError, match="got 3") as exc:
        backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    assert str(exc.value).endswith("Use a worker image built for this MindRoom release.")
    assert len(apps_api.created_bodies) == 2


@pytest.mark.parametrize("image_pull_policy", ["IfNotPresent", "Always"])
def test_kubernetes_newer_worker_refusal_says_to_run_the_primary_on_its_release(image_pull_policy: str) -> None:
    """A worker that started from a newer tag than the primary is refused with the fix on the primary's side."""
    backend, apps_api, _core_api = _backend(image_pull_policy=image_pull_policy)
    newer_protocol = WORKER_PROTOCOL_VERSION + 1
    _answer_worker_health(backend, lambda: newer_protocol)

    with pytest.raises(WorkerBackendError, match=f"got {newer_protocol} ") as exc:
        backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    assert str(exc.value).endswith(
        "The worker image comes from a newer MindRoom release: "
        "upgrade this runtime to the worker's release, or pin both to one release tag. "
        "For example, when this runtime's pod runs the same mutable tag such as ':latest' and pulls it on every start, "
        "restarting the pod picks up the worker's release.",
    )
    assert "pull policy" not in str(exc.value)
    assert len(apps_api.created_bodies) == 2


def test_kubernetes_worker_health_read_waits_for_a_successful_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Service may route nowhere for a moment after the Deployment turns ready, so the read retries."""
    backend, _apps_api, _core_api = _backend()
    answers: list[httpx.Response | httpx.HTTPError] = [
        httpx.ConnectError("no endpoints yet"),
        httpx.Response(503),
        httpx.Response(200, json={"status": "ok"}),
    ]
    urls: list[str] = []

    class _HealthClient:
        def __init__(self, *, timeout: float) -> None:
            assert timeout > 0

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def get(self, url: str) -> httpx.Response:
            urls.append(url)
            answer = answers.pop(0)
            if isinstance(answer, httpx.HTTPError):
                raise answer
            return answer

    monkeypatch.setattr(kubernetes_resources_module.httpx, "Client", _HealthClient)
    monkeypatch.setattr(kubernetes_resources_module, "_READY_POLL_INTERVAL_SECONDS", 0.0)

    response = kubernetes_resources_module.KubernetesResourceManager.read_worker_health(
        backend._resources,
        "mindroom-worker-abc",
        timeout_seconds=5.0,
    )

    assert response.status_code == 200
    assert urls == ["http://mindroom-worker-abc.chat.svc.cluster.local:8766/healthz"] * 3


def test_kubernetes_worker_health_read_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """A worker that never answers its health check fails the read instead of handing out its endpoint."""
    backend, _apps_api, _core_api = _backend()

    class _UnreachableClient:
        def __init__(self, *, timeout: float) -> None:
            assert timeout > 0

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def get(self, _url: str) -> httpx.Response:
            message = "connection refused"
            raise httpx.ConnectError(message)

    monkeypatch.setattr(kubernetes_resources_module.httpx, "Client", _UnreachableClient)
    monkeypatch.setattr(kubernetes_resources_module, "_READY_POLL_INTERVAL_SECONDS", 0.0)

    with pytest.raises(WorkerBackendError, match="did not answer its health check"):
        kubernetes_resources_module.KubernetesResourceManager.read_worker_health(
            backend._resources,
            "mindroom-worker-abc",
            timeout_seconds=0.01,
        )


def test_kubernetes_backend_progress_respects_real_elapsed_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cold-start progress should fire on the 1.5s grace deadline even with 1.0s polling."""
    backend, _apps_api, _core_api = _backend()
    events: list[WorkerReadyProgress] = []
    cold_start_seen = threading.Event()
    ensure_worker_returned = threading.Event()
    first_poll_seen = threading.Event()
    clock = _ControlledMonotonicClock()
    handle: list[object] = []
    errors: list[BaseException] = []

    def sink(progress: WorkerReadyProgress) -> None:
        events.append(progress)
        if progress.phase == "cold_start":
            cold_start_seen.set()

    monkeypatch.setattr(progress_module, "time", SimpleNamespace(monotonic=clock.monotonic))
    monkeypatch.setattr(
        progress_module,
        "threading",
        SimpleNamespace(Condition=lambda: _ControlledCondition(clock), Thread=threading.Thread),
    )
    _install_real_elapsed_wait_for_ready(
        backend,
        ready_after_seconds=1.7,
        poll_interval_seconds=kubernetes_resources_module._READY_POLL_INTERVAL_SECONDS,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        on_iteration=lambda _elapsed_seconds: first_poll_seen.set(),
    )

    def ensure_worker() -> None:
        try:
            handle.append(
                backend.ensure_worker(
                    _spec(_TEST_SCOPED_WORKER_KEY_A),
                    now=10.0,
                    progress_sink=sink,
                ),
            )
        except BaseException as exc:  # pragma: no cover - raised explicitly below
            errors.append(exc)
        finally:
            ensure_worker_returned.set()

    worker_thread = threading.Thread(target=ensure_worker)
    worker_thread.start()

    assert first_poll_seen.wait(timeout=1.0)
    clock.advance_to(1.0)
    assert not cold_start_seen.wait(timeout=0.1)
    assert not ensure_worker_returned.is_set()

    clock.advance_to(1.5)
    assert cold_start_seen.wait(timeout=1.0)
    assert not ensure_worker_returned.is_set()

    clock.advance_to(1.7)
    assert not ensure_worker_returned.is_set()

    clock.advance_to(2.0)
    worker_thread.join(timeout=1.0)
    assert ensure_worker_returned.is_set()

    if errors:
        raise errors[0]

    assert len(handle) == 1
    assert [event.phase for event in events] == ["cold_start", "ready"]
    assert 1.5 <= events[0].elapsed_seconds <= 1.7
    assert events[1].elapsed_seconds >= 2.0

    assert handle[0].worker_key == _TEST_SCOPED_WORKER_KEY_A


def test_kubernetes_backend_skips_progress_for_warm_worker() -> None:
    """Warm ready workers should stay silent even when a sink is provided."""
    backend, _apps_api, _core_api = _backend()
    backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)
    events: list[WorkerReadyProgress] = []

    backend.ensure_worker(
        _spec(_TEST_SCOPED_WORKER_KEY_A),
        now=11.0,
        progress_sink=events.append,
    )

    assert events == []


def test_kubernetes_backend_reports_progress_for_recreated_ready_deployment(tmp_path: Path) -> None:
    """Recreating a ready deployment should emit cold-start progress for the real startup lifecycle."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=Path("config.yaml"),
        storage_path=tmp_path / "mindroom-test-storage",
    )
    initial_snapshot = deepcopy(_TEST_TOOL_VALIDATION_SNAPSHOT)
    updated_snapshot = deepcopy(_TEST_TOOL_VALIDATION_SNAPSHOT)
    updated_snapshot["search"] = {
        "config_fields": ["engine"],
        "agent_override_fields": [],
        "authored_override_validator": "default",
        "runtime_loadable": True,
    }
    backend, apps_api, core_api = _backend(
        runtime_paths=runtime_paths,
        tool_validation_snapshot=initial_snapshot,
    )
    backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    updated_backend, _, _ = _backend(
        runtime_paths=runtime_paths,
        tool_validation_snapshot=updated_snapshot,
    )
    updated_backend._resources.apps_api = apps_api
    updated_backend._resources.core_api = core_api
    updated_backend._resources.api_exception_cls = _FakeApiError
    _install_real_elapsed_wait_for_ready(updated_backend, ready_after_seconds=1.6)

    events: list[WorkerReadyProgress] = []
    updated_backend.ensure_worker(
        _spec(_TEST_SCOPED_WORKER_KEY_A),
        now=11.0,
        progress_sink=events.append,
    )

    assert [event.phase for event in events] == ["cold_start", "ready"]


def test_kubernetes_backend_recreated_ready_deployment_refreshes_startup_metadata(tmp_path: Path) -> None:
    """Recreating a ready deployment should refresh startup metadata instead of reusing stale values."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=Path("config.yaml"),
        storage_path=tmp_path / "mindroom-test-storage",
    )
    initial_snapshot = deepcopy(_TEST_TOOL_VALIDATION_SNAPSHOT)
    updated_snapshot = deepcopy(_TEST_TOOL_VALIDATION_SNAPSHOT)
    updated_snapshot["search"] = {
        "config_fields": ["engine"],
        "agent_override_fields": [],
        "authored_override_validator": "default",
        "runtime_loadable": True,
    }
    backend, apps_api, core_api = _backend(
        runtime_paths=runtime_paths,
        tool_validation_snapshot=initial_snapshot,
    )
    backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    updated_backend, _, _ = _backend(
        runtime_paths=runtime_paths,
        tool_validation_snapshot=updated_snapshot,
    )
    updated_backend._resources.apps_api = apps_api
    updated_backend._resources.core_api = core_api
    updated_backend._resources.api_exception_cls = _FakeApiError
    _install_real_elapsed_wait_for_ready(updated_backend, ready_after_seconds=1.6)

    handle = updated_backend.ensure_worker(
        _spec(_TEST_SCOPED_WORKER_KEY_A),
        now=11.0,
    )

    assert handle.last_started_at == 11.0
    assert handle.startup_count == 2


def test_kubernetes_backend_recreate_metadata_patch_failure_is_normalized(tmp_path: Path) -> None:
    """Recreate metadata patch failures should still record failed startup state and raise WorkerBackendError."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=Path("config.yaml"),
        storage_path=tmp_path / "mindroom-test-storage",
    )
    initial_snapshot = deepcopy(_TEST_TOOL_VALIDATION_SNAPSHOT)
    updated_snapshot = deepcopy(_TEST_TOOL_VALIDATION_SNAPSHOT)
    updated_snapshot["search"] = {
        "config_fields": ["engine"],
        "agent_override_fields": [],
        "authored_override_validator": "default",
        "runtime_loadable": True,
    }
    backend, apps_api, core_api = _backend(
        runtime_paths=runtime_paths,
        tool_validation_snapshot=initial_snapshot,
    )
    backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    updated_backend, _, _ = _backend(
        runtime_paths=runtime_paths,
        tool_validation_snapshot=updated_snapshot,
    )
    updated_backend._resources.apps_api = apps_api
    updated_backend._resources.core_api = core_api
    updated_backend._resources.api_exception_cls = _FakeApiError

    original_patch_deployment = updated_backend._resources.patch_deployment
    first_patch_attempt = True

    def patch_deployment_with_failure(
        deployment_name: str,
        *,
        replicas: int | None = None,
        annotations: dict[str, str] | None = None,
    ) -> None:
        nonlocal first_patch_attempt
        if first_patch_attempt:
            first_patch_attempt = False
            msg = "refresh metadata failed"
            raise RuntimeError(msg)
        original_patch_deployment(
            deployment_name,
            replicas=replicas,
            annotations=annotations,
        )

    updated_backend._resources.patch_deployment = patch_deployment_with_failure

    events: list[WorkerReadyProgress] = []
    with pytest.raises(WorkerBackendError, match="refresh metadata failed"):
        updated_backend.ensure_worker(
            _spec(_TEST_SCOPED_WORKER_KEY_A),
            now=11.0,
            progress_sink=events.append,
        )

    worker_id = next(iter(apps_api.deployments))
    deployment = apps_api.deployments[worker_id]
    assert deployment.metadata.annotations["mindroom.ai/worker-status"] == "failed"
    assert deployment.metadata.annotations["mindroom.ai/failure-reason"] == "refresh metadata failed"
    assert deployment.metadata.annotations["mindroom.ai/startup-count"] == "2"
    assert deployment.metadata.annotations["mindroom.ai/last-started-at"] == "11.0"
    assert [event.phase for event in events] == ["failed"]


def test_kubernetes_backend_ready_metadata_patch_failure_is_normalized(tmp_path: Path) -> None:
    """A failed ready-status patch should fail ensure_worker without tearing down a healthy worker."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=Path("config.yaml"),
        storage_path=tmp_path / "mindroom-test-storage",
    )
    initial_snapshot = deepcopy(_TEST_TOOL_VALIDATION_SNAPSHOT)
    updated_snapshot = deepcopy(_TEST_TOOL_VALIDATION_SNAPSHOT)
    updated_snapshot["search"] = {
        "config_fields": ["engine"],
        "agent_override_fields": [],
        "authored_override_validator": "default",
        "runtime_loadable": True,
    }
    backend, apps_api, core_api = _backend(
        runtime_paths=runtime_paths,
        tool_validation_snapshot=initial_snapshot,
    )
    backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    updated_backend, _, _ = _backend(
        runtime_paths=runtime_paths,
        tool_validation_snapshot=updated_snapshot,
    )
    updated_backend._resources.apps_api = apps_api
    updated_backend._resources.core_api = core_api
    updated_backend._resources.api_exception_cls = _FakeApiError
    _install_real_elapsed_wait_for_ready(updated_backend, ready_after_seconds=1.6)

    original_patch_deployment = updated_backend._resources.patch_deployment

    def patch_deployment_with_ready_failure(
        deployment_name: str,
        *,
        replicas: int | None = None,
        annotations: dict[str, str] | None = None,
    ) -> None:
        if annotations is not None and annotations.get("mindroom.ai/worker-status") == "ready":
            msg = "ready patch failed"
            raise RuntimeError(msg)
        original_patch_deployment(
            deployment_name,
            replicas=replicas,
            annotations=annotations,
        )

    updated_backend._resources.patch_deployment = patch_deployment_with_ready_failure

    events: list[WorkerReadyProgress] = []
    with pytest.raises(WorkerBackendError, match="ready patch failed"):
        updated_backend.ensure_worker(
            _spec(_TEST_SCOPED_WORKER_KEY_A),
            now=11.0,
            progress_sink=events.append,
        )

    worker_id = next(iter(apps_api.deployments))
    deployment = apps_api.deployments[worker_id]
    assert deployment.spec.replicas == 1
    assert worker_id in core_api.services
    assert deployment.metadata.annotations["mindroom.ai/worker-status"] != "failed"
    assert [event.phase for event in events] == ["cold_start", "failed"]


@pytest.mark.parametrize(
    ("failure_target", "error_message"),
    [
        ("apply_deployment", "deployment reconcile failed"),
        ("apply_service", "service apply failed"),
    ],
)
def test_kubernetes_backend_warm_reconcile_failures_are_non_destructive(
    tmp_path: Path,
    failure_target: str,
    error_message: str,
) -> None:
    """Warm reconcile failures should fail ensure_worker without tearing down the existing worker."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=Path("config.yaml"),
        storage_path=tmp_path / "mindroom-test-storage",
    )
    backend, apps_api, core_api = _backend(runtime_paths=runtime_paths, idle_timeout_seconds=600.0)
    handle = backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)

    if failure_target == "apply_deployment":

        def apply_deployment_with_failure(**_kwargs: object) -> object:
            msg = error_message
            raise RuntimeError(msg)

        backend._resources.apply_deployment = apply_deployment_with_failure
    else:

        def apply_service_with_failure(worker_id: str) -> None:
            _ = worker_id
            msg = error_message
            raise RuntimeError(msg)

        backend._resources.apply_service = apply_service_with_failure

    events: list[WorkerReadyProgress] = []
    with pytest.raises(WorkerBackendError, match=error_message):
        backend.ensure_worker(
            _spec(_TEST_SCOPED_WORKER_KEY_A),
            now=310.0,
            progress_sink=events.append,
        )

    deployment = apps_api.deployments[handle.worker_id]
    assert deployment.spec.replicas == 1
    assert handle.worker_id in core_api.services
    assert deployment.metadata.annotations["mindroom.ai/worker-status"] != "failed"
    assert [event.phase for event in events] == []


@pytest.mark.parametrize(
    ("failure_target", "error_message"),
    [
        ("apply_service", "service apply failed"),
        ("wait_for_ready", "worker never became ready"),
    ],
)
def test_kubernetes_backend_existing_starting_worker_failures_record_failure(
    tmp_path: Path,
    failure_target: str,
    error_message: str,
) -> None:
    """Existing starting workers should still be marked failed when recovery fails."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=Path("config.yaml"),
        storage_path=tmp_path / "mindroom-test-storage",
    )
    backend, apps_api, core_api = _backend(runtime_paths=runtime_paths, idle_timeout_seconds=600.0)
    handle = backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)
    deployment = apps_api.deployments[handle.worker_id]
    deployment.metadata.annotations["mindroom.ai/worker-status"] = "starting"
    deployment.metadata.annotations.pop("mindroom.ai/failure-reason", None)
    deployment.status.ready_replicas = 0

    if failure_target == "apply_service":

        def apply_service_with_failure(worker_id: str) -> None:
            _ = worker_id
            msg = error_message
            raise RuntimeError(msg)

        backend._resources.apply_service = apply_service_with_failure
    else:

        def wait_for_ready_with_failure(
            _worker_id: str,
            *,
            timeout_seconds: float,
            deployment_ready_fn: object,
            on_poll_tick: object | None = None,
        ) -> object:
            del timeout_seconds, deployment_ready_fn, on_poll_tick
            msg = error_message
            raise WorkerBackendError(msg)

        backend._resources.wait_for_ready = wait_for_ready_with_failure

    events: list[WorkerReadyProgress] = []
    with pytest.raises(WorkerBackendError, match=error_message):
        backend.ensure_worker(
            _spec(_TEST_SCOPED_WORKER_KEY_A),
            now=310.0,
            progress_sink=events.append,
        )

    deployment = apps_api.deployments[handle.worker_id]
    assert deployment.metadata.annotations["mindroom.ai/worker-status"] == "failed"
    assert deployment.metadata.annotations["mindroom.ai/failure-reason"] == error_message
    assert deployment.spec.replicas == 0
    assert handle.worker_id not in core_api.services
    assert handle.worker_id not in core_api.secrets
    assert events[-1].phase == "failed"


@pytest.mark.parametrize(
    ("failure_target", "error_message"),
    [
        ("sync_shared_credentials", "sync credentials failed"),
        ("apply_service", "service apply failed"),
    ],
)
def test_kubernetes_backend_prestartup_failures_are_normalized(
    tmp_path: Path,
    failure_target: str,
    error_message: str,
) -> None:
    """Fresh startup failures before readiness polling should still persist failed worker state."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=Path("config.yaml"),
        storage_path=tmp_path / "mindroom-test-storage",
    )
    backend, apps_api, _core_api = _backend(runtime_paths=runtime_paths)

    if failure_target == "sync_shared_credentials":
        failure_context = patch(
            "mindroom.workers.backends.kubernetes.sync_shared_credentials_to_worker",
            side_effect=RuntimeError(error_message),
        )
    else:

        def apply_service_with_failure(worker_id: str) -> None:
            _ = worker_id
            msg = error_message
            raise RuntimeError(msg)

        backend._resources.apply_service = apply_service_with_failure
        failure_context = nullcontext()

    events: list[WorkerReadyProgress] = []
    with failure_context, pytest.raises(WorkerBackendError, match=error_message):
        backend.ensure_worker(
            _spec(_TEST_SCOPED_WORKER_KEY_A),
            now=10.0,
            progress_sink=events.append,
        )

    worker_id = backend._worker_id(_TEST_SCOPED_WORKER_KEY_A)
    deployment = apps_api.deployments[worker_id]
    assert deployment.metadata.annotations["mindroom.ai/worker-status"] == "failed"
    assert deployment.metadata.annotations["mindroom.ai/failure-reason"] == error_message
    assert deployment.metadata.annotations["mindroom.ai/startup-count"] == "1"
    assert deployment.metadata.annotations["mindroom.ai/last-started-at"] == "10.0"
    assert [event.phase for event in events] == ["failed"]


def test_kubernetes_backend_apply_deployment_failure_is_normalized(tmp_path: Path) -> None:
    """Deployment apply failures should surface as WorkerBackendError and emit failed progress."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=Path("config.yaml"),
        storage_path=tmp_path / "mindroom-test-storage",
    )
    backend, _apps_api, core_api = _backend(runtime_paths=runtime_paths)

    def apply_deployment_with_failure(**_kwargs: object) -> object:
        msg = "deployment apply failed"
        raise RuntimeError(msg)

    backend._resources.apply_deployment = apply_deployment_with_failure

    events: list[WorkerReadyProgress] = []
    with pytest.raises(WorkerBackendError, match="deployment apply failed"):
        backend.ensure_worker(
            _spec(_TEST_SCOPED_WORKER_KEY_A),
            now=10.0,
            progress_sink=events.append,
        )

    assert [event.phase for event in events] == ["failed"]
    assert backend._worker_id(_TEST_SCOPED_WORKER_KEY_A) not in core_api.secrets


def test_kubernetes_backend_reports_failed_cold_start_progress() -> None:
    """Failed cold starts should surface a terminal failed progress event with the error."""
    backend, _apps_api, _core_api = _backend()
    events: list[WorkerReadyProgress] = []
    error_message = "worker never became ready"

    def _boom(
        _self: object,
        _deployment_name: str,
        *,
        timeout_seconds: float,
        deployment_ready_fn: object,
        on_poll_tick: Callable[[], None] | None = None,
    ) -> object:
        del deployment_ready_fn
        assert on_poll_tick is not None
        started_at = time.monotonic()
        deadline = started_at + timeout_seconds
        while True:
            elapsed_seconds = time.monotonic() - started_at
            if elapsed_seconds >= 2.0:
                raise WorkerBackendError(error_message)
            assert time.monotonic() < deadline
            on_poll_tick()
            time.sleep(0.01)

    backend._resources.wait_for_ready = MethodType(_boom, backend._resources)

    with pytest.raises(WorkerBackendError, match=error_message):
        backend.ensure_worker(
            _spec(_TEST_SCOPED_WORKER_KEY_A),
            now=10.0,
            progress_sink=events.append,
        )

    # Setup before the polling stub can already emit waiting.
    phases = [event.phase for event in events]
    assert phases[0] == "cold_start"
    assert phases[-1] == "failed"
    assert all(phase == "waiting" for phase in phases[1:-1])
    assert events[-1].error == error_message


def test_kubernetes_backend_ignores_progress_when_sink_is_absent() -> None:
    """Cold starts should still succeed when the first caller has no sink."""
    backend, _apps_api, _core_api = _backend()

    def _ready(
        self: object,
        deployment_name: str,
        *,
        timeout_seconds: float,
        deployment_ready_fn: object,
        on_poll_tick: object | None = None,
    ) -> object:
        del timeout_seconds, deployment_ready_fn
        assert on_poll_tick is not None
        return self.read_deployment(deployment_name)

    backend._resources.wait_for_ready = MethodType(_ready, backend._resources)

    backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0, progress_sink=None)


def test_kubernetes_backend_replays_progress_snapshot_to_late_joining_waiter() -> None:
    """A waiter that joins after cold_start should replay that state before terminal ready."""
    backend, _apps_api, _core_api = _backend()
    worker_key = _TEST_SCOPED_WORKER_KEY_A
    first_events: list[WorkerReadyProgress] = []
    second_events: list[WorkerReadyProgress] = []
    cold_start_seen = threading.Event()
    ready_gate = threading.Event()
    second_registered = threading.Event()
    errors: list[BaseException] = []
    handles: dict[str, object] = {}

    def first_sink(progress: WorkerReadyProgress) -> None:
        first_events.append(progress)
        if progress.phase == "cold_start":
            cold_start_seen.set()

    def second_sink(progress: WorkerReadyProgress) -> None:
        second_events.append(progress)
        if progress.phase == "cold_start":
            # Only the snapshot replayed when this late waiter subscribes can carry cold_start to it.
            second_registered.set()

    _install_real_elapsed_wait_for_ready(
        backend,
        ready_after_seconds=1.6,
        ready_gate=ready_gate,
    )

    def ensure_worker(name: str, *, progress_sink: Callable[[WorkerReadyProgress], None], now: float) -> None:
        try:
            handles[name] = backend.ensure_worker(
                _spec(worker_key),
                now=now,
                progress_sink=progress_sink,
            )
        except BaseException as exc:  # pragma: no cover - raised explicitly below
            errors.append(exc)

    first_thread = threading.Thread(
        target=ensure_worker,
        args=("first",),
        kwargs={"progress_sink": first_sink, "now": 10.0},
    )
    second_thread = threading.Thread(
        target=ensure_worker,
        args=("second",),
        kwargs={"progress_sink": second_sink, "now": 11.0},
    )

    first_thread.start()
    assert cold_start_seen.wait(timeout=3.0)
    second_thread.start()
    assert second_registered.wait(timeout=1.0)
    ready_gate.set()
    first_thread.join()
    second_thread.join()

    if errors:
        raise errors[0]

    assert [event.phase for event in first_events] == ["cold_start", "ready"]
    assert [event.phase for event in second_events] == ["cold_start", "ready"]
    assert handles["first"].worker_id == handles["second"].worker_id


def test_kubernetes_backend_keeps_digest_when_worker_name_prefix_is_long() -> None:
    """Long prefixes must still preserve the per-worker digest so names remain unique."""
    long_prefix = "mindroom-worker-prefix-that-is-intentionally-way-too-long-for-a-kubernetes-name"
    backend, _apps_api, _core_api = _backend(name_prefix=long_prefix)

    first = backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_A), now=10.0)
    second = backend.ensure_worker(_spec(_TEST_SCOPED_WORKER_KEY_B), now=20.0)

    assert first.worker_id != second.worker_id
    assert len(first.worker_id) <= 63
    assert len(second.worker_id) <= 63


@pytest.mark.parametrize(
    ("prefix", "expected_prefix"),
    [("Agent_Worker.v2", "agent-worker-v2"), ("._-", "mindroom-worker")],
)
def test_worker_id_normalizes_the_name_prefix_to_a_dns_label(prefix: str, expected_prefix: str) -> None:
    """A configured prefix is lowercased with non-DNS characters replaced, and an empty result falls back."""
    worker_id = worker_id_for_key(_TEST_SCOPED_WORKER_KEY_A, prefix=prefix)

    assert re.fullmatch(rf"{expected_prefix}-[0-9a-f]{{24}}", worker_id)


def test_storage_preflight_reports_remaining_workers_in_paginated_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    """A partial page already containing a worker gives actionable removal guidance."""
    backend, apps_api, _core_api = _backend()
    response = _FakeRawResponse(b'{"items": [{}], "metadata": {"continue": "next"}}')
    monkeypatch.setattr(apps_api, "list_namespaced_deployment", lambda *_args, **_kwargs: response)
    with pytest.raises(WorkerBackendError, match=r"(?i)remove.*worker"):
        backend._resources.check_workers_absent_for_storage_upgrade(timeout_seconds=5.0)
    assert response.released
    assert apps_api.deleted_names == []


def _stamp_legacy_template(apps_api: _FakeAppsApi, worker_id: str, agent_name: str, *, keep_marker: bool) -> None:
    """Replace one Deployment with the one an older release wrote: a whole state root and that template's hash."""
    body = deepcopy(next(body for body in reversed(apps_api.created_bodies) if body["metadata"]["name"] == worker_id))
    for mount in body["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]:
        if mount.get("subPath") == f"agents/{agent_name}/workspace":
            mount.update(subPath=f"agents/{agent_name}", mountPath=f"/app/worker/agents/{agent_name}")
    annotations = body["metadata"]["annotations"]
    annotations["mindroom.ai/template-hash"] = kubernetes_resources_module._template_hash(body["spec"]["template"])
    if not keep_marker:
        del annotations["mindroom.ai/workspace-template-hash"]
    apps_api.deployments.pop(worker_id)
    apps_api.create_namespaced_deployment("chat", body)


@pytest.mark.parametrize(("phase", "gone"), [("Failed", True), ("Succeeded", True), ("Running", False)])
def test_kubernetes_pod_wait_ignores_finished_pods(
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    gone: bool,
) -> None:
    """An evicted or completed pod object left for garbage collection no longer runs, unlike a terminating one."""
    backend, _apps_api, core_api = _backend()
    pod = {"metadata": {"labels": {"mindroom.ai/worker-id": "old-worker"}}, "status": {"phase": phase}}
    monkeypatch.setattr(
        core_api,
        "list_namespaced_pod",
        lambda *_args, **_kwargs: _FakeRawResponse(json.dumps({"items": [pod]}).encode()),
    )
    monkeypatch.setattr(legacy_state_root_mounts, "_POD_EXIT_TIMEOUT_SECONDS", 0)

    if gone:
        legacy_state_root_mounts._wait_for_pods_to_exit(backend._resources, {"old-worker"})
    else:
        with pytest.raises(WorkerBackendError, match="did not stop"):
            legacy_state_root_mounts._wait_for_pods_to_exit(backend._resources, {"old-worker"})


def test_kubernetes_ensure_never_serves_a_live_old_template_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Until an old-template Deployment is gone, ensure fails; once it can be replaced, only the new template serves."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
    )
    backend, apps_api, _core_api = _backend(runtime_paths=runtime_paths)
    handle = backend.ensure_worker(_spec("v1:tenant-123:shared:legacy"), now=10.0)
    _stamp_legacy_template(apps_api, handle.worker_id, "legacy", keep_marker=False)
    backend._invalidate_ready_worker(handle.worker_key)
    delete_deployment = apps_api.delete_namespaced_deployment

    def cluster_refuses_delete(*_args: object, **_kwargs: object) -> None:
        raise _FakeApiError(500)

    monkeypatch.setattr(apps_api, "delete_namespaced_deployment", cluster_refuses_delete)
    with pytest.raises(WorkerBackendError):
        backend.ensure_worker(_spec(handle.worker_key), now=20.0)

    monkeypatch.setattr(apps_api, "delete_namespaced_deployment", delete_deployment)
    backend.ensure_worker(_spec(handle.worker_key), now=30.0)

    assert handle.worker_id in apps_api.deleted_names
    served = apps_api.created_bodies[-1]
    assert "/app/worker/agents/legacy/workspace" in _storage_mounts(served)
    assert "/app/worker/agents/legacy" not in _storage_mounts(served)
    annotations = apps_api.deployments[handle.worker_id].metadata.annotations
    assert annotations["mindroom.ai/workspace-template-hash"] == annotations["mindroom.ai/template-hash"]


def test_kubernetes_startup_stops_workers_whose_template_mounts_state_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Old-template workers stop, even after a downgrade kept this release's marker, and their pods exit first."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
    )
    backend, apps_api, core_api = _backend(runtime_paths=runtime_paths)
    handles = {
        name: backend.ensure_worker(_spec(f"v1:tenant-123:shared:{name}"), now=10.0)
        for name in ("current", "legacy", "downgraded", "idle", "broken")
    }
    for name in ("legacy", "downgraded", "idle", "broken"):
        # A downgrade rewrites the template and its hash but leaves this release's marker behind.
        _stamp_legacy_template(apps_api, handles[name].worker_id, name, keep_marker=name == "downgraded")
    apps_api.deployments[handles["idle"].worker_id].spec.replicas = 0
    pod_lists: list[object] = []

    def list_pods(namespace: str, **kwargs: object) -> _FakeRawResponse:
        assert namespace == "chat"
        pod_lists.append(kwargs["_request_timeout"])
        running = [handles["idle"].worker_id, "unrelated-worker"] if len(pod_lists) < 2 else ["unrelated-worker"]
        items = [{"metadata": {"labels": {"mindroom.ai/worker-id": worker_id}}} for worker_id in running]
        return _FakeRawResponse(json.dumps({"items": items}).encode())

    patch_deployment = apps_api.patch_namespaced_deployment
    patch_timeouts: list[object] = []

    def patch_unless_broken(name: str, namespace: str, body: dict[str, object], **kwargs: object) -> object:
        patch_timeouts.append(kwargs.get("_request_timeout"))
        if name == handles["broken"].worker_id:
            raise _FakeApiError(500)
        return patch_deployment(name, namespace, body)

    monkeypatch.setattr(core_api, "list_namespaced_pod", list_pods)
    monkeypatch.setattr(apps_api, "patch_namespaced_deployment", patch_unless_broken)
    monkeypatch.setattr(legacy_state_root_mounts, "_POD_POLL_INTERVAL_SECONDS", 0)

    # One Deployment that cannot be patched never leaves the others running, and fails startup.
    with capture_logs() as logs, pytest.raises(WorkerBackendError, match=handles["broken"].worker_id):
        legacy_state_root_mounts._stop_kubernetes_workers_mounting_state_roots(backend._resources)
    [warning] = [entry for entry in logs if entry["log_level"] == "warning"]
    assert warning["workers"] == [handles["legacy"].worker_id, handles["downgraded"].worker_id]
    assert patch_timeouts
    assert all(patch_timeouts)
    assert apps_api.deployments[handles["broken"].worker_id].spec.replicas == 1
    for name in ("legacy", "downgraded"):
        deployment = apps_api.deployments[handles[name].worker_id]
        assert deployment.spec.replicas == 0
        assert deployment.metadata.annotations["mindroom.ai/worker-status"] == "idle"
    assert apps_api.deployments[handles["current"].worker_id].spec.replicas == 1

    # The restarted primary stops what failed and waits for every old worker's pods, including scaled-down ones.
    handles["broken"] = handles["current"]
    legacy_state_root_mounts._stop_kubernetes_workers_mounting_state_roots(backend._resources)
    assert len(pod_lists) == 2
    assert all(pod_lists)

    # Pods that never exit fail the bounded wait, and so startup.
    monkeypatch.setattr(legacy_state_root_mounts, "_POD_EXIT_TIMEOUT_SECONDS", 0)
    pod_lists.clear()
    with pytest.raises(WorkerBackendError, match="did not stop"):
        legacy_state_root_mounts._stop_kubernetes_workers_mounting_state_roots(backend._resources)


def _shared_directory_backend(tmp_path: Path) -> tuple[KubernetesWorkerBackend, _FakeAppsApi, RuntimePaths]:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
agents:
  alpha:
    display_name: Alpha
    worker_scope: shared
  beta:
    display_name: Beta
    worker_scope: shared
models:
  default:
    provider: openai
    id: gpt-6-astra
router:
  model: default
""".lstrip(),
        encoding="utf-8",
    )
    runtime_paths = resolve_primary_runtime_paths(config_path=config_path, storage_path=tmp_path / "storage")
    backend, apps_api, _core_api = _backend(runtime_paths=runtime_paths)
    create_shared_directory(runtime_paths, owner="alpha", name="report")
    return backend, apps_api, runtime_paths


def _share_mounts(body: dict[str, object]) -> list[dict[str, object]]:
    volume_mounts = body["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]  # type: ignore[index]
    return [mount for mount in volume_mounts if "shared_directories" in str(mount["mountPath"])]


def test_kubernetes_workers_mount_shared_directories_for_owner_and_members(tmp_path: Path) -> None:
    """The owner's pod mounts its shared directory writable and an invited agent's pod read-only."""
    backend, apps_api, runtime_paths = _shared_directory_backend(tmp_path)
    add_shared_directory_member(runtime_paths, owner="alpha", name="report", member="beta")

    backend.ensure_worker(_spec("v1:tenant-123:shared:alpha"), now=10.0)
    backend.ensure_worker(_spec("v1:tenant-123:shared:beta"), now=10.0)

    alpha_body, beta_body = apps_api.created_bodies
    share_mount = {
        "name": "worker-storage",
        "mountPath": "/app/worker/shared_directories/alpha/report",
        "subPath": "shared_directories/alpha/report",
    }
    assert _share_mounts(alpha_body) == [share_mount]
    assert _share_mounts(beta_body) == [{**share_mount, "readOnly": True}]


def test_kubernetes_ready_cache_rechecks_shared_directories(tmp_path: Path) -> None:
    """A cached ready worker is not reused after an invitation, so its Deployment gains the mount."""
    backend, apps_api, runtime_paths = _shared_directory_backend(tmp_path)
    first = backend.ensure_worker(_spec("v1:tenant-123:shared:beta"), now=10.0)
    backend.ensure_worker(_spec("v1:tenant-123:shared:beta"), now=15.0)
    assert len(apps_api.created_bodies) == 1
    assert _share_mounts(apps_api.created_bodies[0]) == []

    add_shared_directory_member(runtime_paths, owner="alpha", name="report", member="beta")
    backend.ensure_worker(_spec("v1:tenant-123:shared:beta"), now=20.0)

    assert apps_api.deleted_names == [first.worker_id]
    assert _share_mounts(apps_api.created_bodies[-1]) == [
        {
            "name": "worker-storage",
            "mountPath": "/app/worker/shared_directories/alpha/report",
            "subPath": "shared_directories/alpha/report",
            "readOnly": True,
        },
    ]
