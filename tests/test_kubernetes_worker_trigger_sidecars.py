"""Tests for Kubernetes worker trigger sidecar plumbing."""

from __future__ import annotations

import json
import shutil
import subprocess
from copy import deepcopy
from typing import TYPE_CHECKING, Any, cast

import pytest
import yaml

from mindroom.constants import resolve_runtime_paths
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.backends.kubernetes_config import KubernetesWorkerBackendConfig
from mindroom.workers.backends.kubernetes_resources import KubernetesResourceManager

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths

_EXTRA_CONTAINERS_ENV = "MINDROOM_KUBERNETES_WORKER_EXTRA_CONTAINERS_JSON"
_EXTRA_VOLUMES_ENV = "MINDROOM_KUBERNETES_WORKER_EXTRA_VOLUMES_JSON"
_TEST_AUTH_TOKEN = "test-token"  # noqa: S105


def _runtime_paths(tmp_path: Path, process_env: dict[str, str]) -> RuntimePaths:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\n", encoding="utf-8")
    return resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env=process_env,
    )


def _base_env(**overrides: str) -> dict[str, str]:
    return {
        "MINDROOM_WORKER_BACKEND": "kubernetes",
        "MINDROOM_KUBERNETES_WORKER_IMAGE": "test-image",
        "MINDROOM_KUBERNETES_WORKER_STORAGE_PVC_NAME": "test-pvc",
        **overrides,
    }


def _worker_config(
    *,
    extra_containers: tuple[dict[str, object], ...] = (),
    extra_volumes: tuple[dict[str, object], ...] = (),
) -> KubernetesWorkerBackendConfig:
    return KubernetesWorkerBackendConfig(
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
        idle_timeout_seconds=60.0,
        ready_timeout_seconds=5.0,
        name_prefix="mindroom-worker",
        node_name=None,
        colocate_with_control_plane_node=False,
        extra_env={},
        extra_labels={},
        extra_annotations={},
        owner_deployment_name=None,
        resource_requests={"memory": "256Mi", "cpu": "100m"},
        resource_limits={"memory": "1Gi", "cpu": "500m"},
        enable_service_links=False,
        auth_secret_name=None,
        reconcile_pod_templates=True,
        agent_vault=None,
        extra_containers=extra_containers,
        extra_volumes=extra_volumes,
    )


def _pod_template(
    tmp_path: Path,
    *,
    extra_containers: tuple[dict[str, object], ...],
    extra_volumes: tuple[dict[str, object], ...],
) -> dict[str, object]:
    manager = KubernetesResourceManager(
        runtime_paths=_runtime_paths(tmp_path, {}),
        config=_worker_config(extra_containers=extra_containers, extra_volumes=extra_volumes),
        auth_token=_TEST_AUTH_TOKEN,
        storage_root=tmp_path / "storage",
        tool_validation_snapshot={},
        worker_grantable_credentials=frozenset(),
    )
    return manager._pod_template(
        worker_key="v1:tenant-123:shared:general",
        worker_id="mindroom-worker-test",
        state_subpath="workers/test",
        startup_manifest_path="/app/worker/workers/test/.mindroom-startup.json",
        startup_manifest_hash="startup-hash",
        private_agent_names=None,
    )


def _render_runtime_chart(tmp_path: Path, values: dict[str, object]) -> list[dict[str, Any]]:
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm is required for rendered chart checks")
    values_path = tmp_path / "values.yaml"
    values_path.write_text(yaml.safe_dump(values), encoding="utf-8")
    completed = subprocess.run(
        [
            helm,
            "template",
            "mindroom-runtime",
            "cluster/k8s/runtime",
            "--values",
            str(values_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    completed.check_returncode()
    return [doc for doc in yaml.safe_load_all(completed.stdout) if isinstance(doc, dict)]


def _resource(docs: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    for doc in docs:
        metadata = doc.get("metadata")
        if doc.get("kind") == kind and isinstance(metadata, dict) and metadata.get("name") == name:
            return doc
    msg = f"{kind}/{name} was not rendered"
    raise AssertionError(msg)


def _container(deployment: dict[str, Any], name: str) -> dict[str, Any]:
    containers = deployment["spec"]["template"]["spec"]["containers"]
    for container in containers:
        if container["name"] == name:
            return cast("dict[str, Any]", container)
    msg = f"container {name} was not rendered"
    raise AssertionError(msg)


def _env_by_name(container: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {env["name"]: env for env in container["env"]}


def test_kubernetes_config_reads_trigger_sidecars_and_volumes_from_env(tmp_path: Path) -> None:
    """Kubernetes worker config should parse JSON sidecars and sidecar-owned volumes."""
    extra_containers = [
        {
            "name": "trigger-listener",
            "image": "ghcr.io/mindroom-ai/trigger-listener:latest",
            "imagePullPolicy": "IfNotPresent",
            "command": ["/bin/trigger-listener"],
            "args": ["--watch", "/trigger/inbox"],
            "env": [{"name": "TRIGGER_DIR", "value": "/trigger/inbox"}],
            "volumeMounts": [{"name": "trigger-state", "mountPath": "/trigger/inbox"}],
            "resources": {"requests": {"cpu": "10m", "memory": "32Mi"}},
            "securityContext": {"allowPrivilegeEscalation": False},
        },
    ]
    extra_volumes = [{"name": "trigger-state", "emptyDir": {}}]
    runtime_paths = _runtime_paths(
        tmp_path,
        _base_env(
            **{
                _EXTRA_CONTAINERS_ENV: json.dumps(extra_containers),
                _EXTRA_VOLUMES_ENV: json.dumps(extra_volumes),
            },
        ),
    )

    config = KubernetesWorkerBackendConfig.from_runtime(runtime_paths)

    assert config.extra_containers == tuple(extra_containers)
    assert config.extra_volumes == tuple(extra_volumes)


@pytest.mark.parametrize(
    ("env_name", "raw_value"),
    [
        (_EXTRA_CONTAINERS_ENV, '{"name":"trigger-listener","image":"busybox"}'),
        (_EXTRA_CONTAINERS_ENV, "[1]"),
        (_EXTRA_CONTAINERS_ENV, '[{"name":"trigger-listener"}]'),
        (_EXTRA_CONTAINERS_ENV, '[{"name":"trigger-listener","image":"busybox","ports":[]}]'),
        (_EXTRA_VOLUMES_ENV, '{"name":"trigger-state","emptyDir":{}}'),
        (_EXTRA_VOLUMES_ENV, "[1]"),
        (_EXTRA_VOLUMES_ENV, '[{"name":"trigger-state"}]'),
        (_EXTRA_VOLUMES_ENV, '[{"name":"trigger-state","secret":{},"configMap":{}}]'),
        (_EXTRA_VOLUMES_ENV, '[{"name":"trigger-state","emptyDir":{},"hostPath":{"path":"/tmp"}}]'),
    ],
)
def test_kubernetes_config_rejects_invalid_trigger_sidecar_json(
    tmp_path: Path,
    env_name: str,
    raw_value: str,
) -> None:
    """Invalid sidecar or volume JSON should fail loudly at config resolution."""
    runtime_paths = _runtime_paths(tmp_path, _base_env(**{env_name: raw_value}))

    with pytest.raises(WorkerBackendError, match=env_name):
        KubernetesWorkerBackendConfig.from_runtime(runtime_paths)


def test_worker_pod_template_appends_trigger_sidecar_and_volume_without_main_mount(tmp_path: Path) -> None:
    """Extra volumes should be available to opted-in sidecars without mounting into the sandbox container."""
    extra_container = {
        "name": "trigger-listener",
        "image": "ghcr.io/mindroom-ai/trigger-listener:latest",
        "volumeMounts": [{"name": "trigger-state", "mountPath": "/trigger/inbox"}],
        "env": [{"name": "TRIGGER_DIR", "value": "/trigger/inbox"}],
    }
    extra_volume = {"name": "trigger-state", "emptyDir": {}}

    template = _pod_template(
        tmp_path,
        extra_containers=(deepcopy(extra_container),),
        extra_volumes=(deepcopy(extra_volume),),
    )

    pod_spec = cast("dict[str, Any]", template["spec"])
    containers = cast("list[dict[str, Any]]", pod_spec["containers"])
    volumes = cast("list[dict[str, Any]]", pod_spec["volumes"])
    main_mount_names = {mount["name"] for mount in containers[0]["volumeMounts"]}

    assert [container["name"] for container in containers] == ["sandbox-runner", "trigger-listener"]
    assert containers[1] == extra_container
    assert volumes[-1] == extra_volume
    assert "trigger-state" not in main_mount_names


def test_runtime_chart_exposes_kubernetes_worker_trigger_sidecar_env_json(tmp_path: Path) -> None:
    """Runtime chart should pass configured worker sidecars and volumes through JSON env vars."""
    extra_container = {
        "name": "trigger-listener",
        "image": "ghcr.io/mindroom-ai/trigger-listener:latest",
        "volumeMounts": [{"name": "trigger-state", "mountPath": "/trigger/inbox"}],
    }
    extra_volume = {"name": "trigger-state", "emptyDir": {}}
    docs = _render_runtime_chart(
        tmp_path,
        {
            "eventCache": {"postgres": {"auth": {"password": "test-password"}}},
            "workers": {
                "backend": "kubernetes",
                "sandbox": {"proxyToken": {"value": "test-token"}},
                "kubernetes": {
                    "extraContainers": [extra_container],
                    "extraVolumes": [extra_volume],
                },
            },
        },
    )
    deployment = _resource(docs, "Deployment", "mindroom-runtime")
    env = _env_by_name(_container(deployment, "mindroom"))

    assert json.loads(env[_EXTRA_CONTAINERS_ENV]["value"]) == [extra_container]
    assert json.loads(env[_EXTRA_VOLUMES_ENV]["value"]) == [extra_volume]
