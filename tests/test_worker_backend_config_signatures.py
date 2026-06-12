"""Isolated tests for the Docker and Kubernetes backend config cache signatures."""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING

import pytest

from mindroom.constants import resolve_primary_runtime_paths
from mindroom.runtime_env_policy import CREDENTIALS_ENCRYPTION_KEY_ENV
from mindroom.workers.backends.docker_config import docker_backend_config_signature
from mindroom.workers.backends.kubernetes_config import (
    KubernetesWorkerBackendConfig,
    credentials_encryption_key_hash,
    kubernetes_backend_config_signature,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths

_TEST_AUTH_TOKEN = "signature-test-token"  # noqa: S105
_TEST_ENCRYPTION_KEY = base64.urlsafe_b64encode(b"7" * 32).decode("ascii")

_MINIMAL_KUBERNETES_ENV = {
    "MINDROOM_WORKER_BACKEND": "kubernetes",
    "MINDROOM_KUBERNETES_WORKER_IMAGE": "test-image",
    "MINDROOM_KUBERNETES_WORKER_STORAGE_PVC_NAME": "test-pvc",
}
_FULL_KUBERNETES_ENV = {
    "MINDROOM_WORKER_BACKEND": "kubernetes",
    "MINDROOM_KUBERNETES_WORKER_NAMESPACE": "mindroom-workers",
    "MINDROOM_KUBERNETES_WORKER_IMAGE": "ghcr.io/mindroom-ai/mindroom:1.2.3",
    "MINDROOM_KUBERNETES_WORKER_IMAGE_PULL_POLICY": "Always",
    "MINDROOM_KUBERNETES_WORKER_PORT": "9001",
    "MINDROOM_KUBERNETES_WORKER_SERVICE_ACCOUNT_NAME": "worker-sa",
    "MINDROOM_KUBERNETES_WORKER_STORAGE_PVC_NAME": "worker-pvc",
    "MINDROOM_KUBERNETES_WORKER_STORAGE_MOUNT_PATH": "/srv/worker",
    "MINDROOM_KUBERNETES_WORKER_STORAGE_SUBPATH_PREFIX": "tenants",
    "MINDROOM_KUBERNETES_WORKER_CONFIG_MAP_NAME": "worker-config",
    "MINDROOM_KUBERNETES_WORKER_CONFIG_KEY": "worker.yaml",
    "MINDROOM_KUBERNETES_WORKER_CONFIG_PATH": "/srv/config/worker.yaml",
    "MINDROOM_KUBERNETES_WORKER_IDLE_TIMEOUT_SECONDS": "900",
    "MINDROOM_KUBERNETES_WORKER_READY_TIMEOUT_SECONDS": "120",
    "MINDROOM_KUBERNETES_WORKER_NAME_PREFIX": "tenant-worker",
    "MINDROOM_KUBERNETES_WORKER_NODE_NAME": "node-a",
    "MINDROOM_KUBERNETES_WORKER_COLOCATE_WITH_CONTROL_PLANE_NODE": "true",
    "MINDROOM_KUBERNETES_WORKER_ENV_JSON": '{"EXTRA_TWO": "2", "EXTRA_ONE": "1"}',
    "MINDROOM_KUBERNETES_WORKER_LABELS_JSON": '{"team": "platform"}',
    "MINDROOM_KUBERNETES_WORKER_ANNOTATIONS_JSON": '{"cluster-autoscaler.kubernetes.io/safe-to-evict": "false"}',
    "MINDROOM_KUBERNETES_WORKER_OWNER_DEPLOYMENT_NAME": "mindroom-primary",
    "MINDROOM_KUBERNETES_WORKER_MEMORY_REQUEST": "512Mi",
    "MINDROOM_KUBERNETES_WORKER_MEMORY_LIMIT": "2Gi",
    "MINDROOM_KUBERNETES_WORKER_CPU_REQUEST": "250m",
    "MINDROOM_KUBERNETES_WORKER_CPU_LIMIT": "1",
    "MINDROOM_KUBERNETES_WORKER_ENABLE_SERVICE_LINKS": "true",
    "MINDROOM_KUBERNETES_WORKER_AUTH_SECRET_NAME": "worker-auth",
    "MINDROOM_KUBERNETES_WORKER_RECONCILE_POD_TEMPLATES": "true",
    "MINDROOM_KUBERNETES_AGENT_VAULT_ENABLED": "true",
    "MINDROOM_KUBERNETES_AGENT_VAULT_CLI_IMAGE": "example.test/agent-vault:1",
    "MINDROOM_KUBERNETES_AGENT_VAULT_OWNER_EMAIL": "vault-owner@example.test",
    CREDENTIALS_ENCRYPTION_KEY_ENV: _TEST_ENCRYPTION_KEY,
}
_MINIMAL_DOCKER_ENV = {
    "MINDROOM_WORKER_BACKEND": "docker",
    "MINDROOM_DOCKER_WORKER_IMAGE": "ghcr.io/mindroom-ai/mindroom:latest",
}


def _runtime_paths(tmp_path: Path, env: dict[str, str]) -> RuntimePaths:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\n", encoding="utf-8")
    return resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env=env,
    )


def _legacy_kubernetes_backend_config_signature(
    runtime_paths: RuntimePaths,
    *,
    auth_token: str | None,
    storage_root: Path | None = None,
) -> tuple[str, ...]:
    """Hand-assembled pre-refactor signature, kept verbatim as the equivalence oracle."""
    config = KubernetesWorkerBackendConfig.from_runtime(runtime_paths)
    credentials_encryption_key = runtime_paths.env_value(CREDENTIALS_ENCRYPTION_KEY_ENV)
    credentials_encryption_key_marker = credentials_encryption_key_hash(credentials_encryption_key) or ""
    extra_env_json = json.dumps(config.extra_env, sort_keys=True, separators=(",", ":"))
    extra_labels_json = json.dumps(config.extra_labels, sort_keys=True, separators=(",", ":"))
    extra_annotations_json = json.dumps(config.extra_annotations, sort_keys=True, separators=(",", ":"))
    resource_requests_json = json.dumps(config.resource_requests, sort_keys=True, separators=(",", ":"))
    resource_limits_json = json.dumps(config.resource_limits, sort_keys=True, separators=(",", ":"))
    return (
        "kubernetes",
        config.namespace,
        config.image,
        config.image_pull_policy,
        str(config.worker_port),
        config.service_account_name,
        config.storage_pvc_name,
        config.storage_mount_path,
        config.storage_subpath_prefix,
        config.config_map_name or "",
        config.config_key,
        config.config_path,
        str(config.idle_timeout_seconds),
        str(config.ready_timeout_seconds),
        config.name_prefix,
        config.node_name or "",
        str(config.colocate_with_control_plane_node),
        extra_env_json,
        extra_labels_json,
        extra_annotations_json,
        config.owner_deployment_name or "",
        resource_requests_json,
        resource_limits_json,
        str(config.enable_service_links),
        config.auth_secret_name or "",
        str(config.reconcile_pod_templates),
        config.agent_vault.signature() if config.agent_vault is not None else "",
        credentials_encryption_key_marker,
        auth_token or "",
        str(storage_root.expanduser().resolve()) if storage_root is not None else "",
    )


@pytest.mark.parametrize("env", [_MINIMAL_KUBERNETES_ENV, _FULL_KUBERNETES_ENV])
@pytest.mark.parametrize("auth_token", [None, _TEST_AUTH_TOKEN])
@pytest.mark.parametrize("with_storage_root", [False, True])
def test_kubernetes_signature_matches_legacy_hand_assembly(
    tmp_path: Path,
    env: dict[str, str],
    auth_token: str | None,
    *,
    with_storage_root: bool,
) -> None:
    """The refactored Kubernetes assembly must reproduce the pre-refactor tuple values exactly."""
    runtime_paths = _runtime_paths(tmp_path, env)
    storage_root = runtime_paths.storage_root if with_storage_root else None

    assert kubernetes_backend_config_signature(
        runtime_paths,
        auth_token=auth_token,
        storage_root=storage_root,
    ) == _legacy_kubernetes_backend_config_signature(
        runtime_paths,
        auth_token=auth_token,
        storage_root=storage_root,
    )


def test_kubernetes_signature_is_stable_for_identical_config(tmp_path: Path) -> None:
    """Resolving the same env twice must yield identical cache signatures."""
    first = _runtime_paths(tmp_path, _FULL_KUBERNETES_ENV)
    second = _runtime_paths(tmp_path, dict(_FULL_KUBERNETES_ENV))

    assert kubernetes_backend_config_signature(
        first,
        auth_token=_TEST_AUTH_TOKEN,
        storage_root=first.storage_root,
    ) == kubernetes_backend_config_signature(
        second,
        auth_token=_TEST_AUTH_TOKEN,
        storage_root=second.storage_root,
    )


@pytest.mark.parametrize(
    ("env_name", "changed_value"),
    [
        ("MINDROOM_KUBERNETES_WORKER_NAMESPACE", "other-namespace"),
        ("MINDROOM_KUBERNETES_WORKER_IMAGE", "ghcr.io/mindroom-ai/mindroom:9.9.9"),
        ("MINDROOM_KUBERNETES_WORKER_PORT", "9100"),
        ("MINDROOM_KUBERNETES_WORKER_SERVICE_ACCOUNT_NAME", "other-sa"),
        ("MINDROOM_KUBERNETES_WORKER_STORAGE_PVC_NAME", "other-pvc"),
        ("MINDROOM_KUBERNETES_WORKER_CONFIG_MAP_NAME", "other-config"),
        ("MINDROOM_KUBERNETES_WORKER_IDLE_TIMEOUT_SECONDS", "60"),
        ("MINDROOM_KUBERNETES_WORKER_NODE_NAME", "node-b"),
        ("MINDROOM_KUBERNETES_WORKER_COLOCATE_WITH_CONTROL_PLANE_NODE", "false"),
        ("MINDROOM_KUBERNETES_WORKER_ENV_JSON", '{"EXTRA_ONE": "changed"}'),
        ("MINDROOM_KUBERNETES_WORKER_LABELS_JSON", '{"team": "other"}'),
        ("MINDROOM_KUBERNETES_WORKER_ANNOTATIONS_JSON", '{"changed": "true"}'),
        ("MINDROOM_KUBERNETES_WORKER_OWNER_DEPLOYMENT_NAME", "other-owner"),
        ("MINDROOM_KUBERNETES_WORKER_MEMORY_LIMIT", "4Gi"),
        ("MINDROOM_KUBERNETES_WORKER_CPU_REQUEST", "500m"),
        ("MINDROOM_KUBERNETES_WORKER_ENABLE_SERVICE_LINKS", "false"),
        ("MINDROOM_KUBERNETES_WORKER_AUTH_SECRET_NAME", "other-auth"),
        ("MINDROOM_KUBERNETES_WORKER_RECONCILE_POD_TEMPLATES", "false"),
        ("MINDROOM_KUBERNETES_AGENT_VAULT_CLI_IMAGE", "example.test/agent-vault:2"),
        (CREDENTIALS_ENCRYPTION_KEY_ENV, base64.urlsafe_b64encode(b"8" * 32).decode("ascii")),
    ],
)
def test_kubernetes_signature_changes_when_one_field_changes(
    tmp_path: Path,
    env_name: str,
    changed_value: str,
) -> None:
    """Changing any single config field must invalidate the cache signature."""
    base_paths = _runtime_paths(tmp_path, {**_FULL_KUBERNETES_ENV})
    changed_paths = _runtime_paths(tmp_path, {**_FULL_KUBERNETES_ENV, env_name: changed_value})

    assert _FULL_KUBERNETES_ENV[env_name] != changed_value
    assert kubernetes_backend_config_signature(
        base_paths,
        auth_token=_TEST_AUTH_TOKEN,
        storage_root=None,
    ) != kubernetes_backend_config_signature(
        changed_paths,
        auth_token=_TEST_AUTH_TOKEN,
        storage_root=None,
    )


def test_kubernetes_signature_changes_with_auth_token_and_storage_root(tmp_path: Path) -> None:
    """Auth token and storage root changes must invalidate the cache signature."""
    runtime_paths = _runtime_paths(tmp_path, _MINIMAL_KUBERNETES_ENV)
    base = kubernetes_backend_config_signature(
        runtime_paths,
        auth_token=_TEST_AUTH_TOKEN,
        storage_root=runtime_paths.storage_root,
    )

    assert base != kubernetes_backend_config_signature(
        runtime_paths,
        auth_token="rotated-token",  # noqa: S106
        storage_root=runtime_paths.storage_root,
    )
    assert base != kubernetes_backend_config_signature(
        runtime_paths,
        auth_token=_TEST_AUTH_TOKEN,
        storage_root=tmp_path / "other-storage",
    )


def test_docker_signature_is_stable_for_identical_config(tmp_path: Path) -> None:
    """Resolving the same Docker env twice must yield identical cache signatures."""
    first = _runtime_paths(tmp_path, _MINIMAL_DOCKER_ENV)
    second = _runtime_paths(tmp_path, dict(_MINIMAL_DOCKER_ENV))

    assert docker_backend_config_signature(
        first,
        auth_token=_TEST_AUTH_TOKEN,
        storage_path=first.storage_root,
    ) == docker_backend_config_signature(
        second,
        auth_token=_TEST_AUTH_TOKEN,
        storage_path=second.storage_root,
    )


@pytest.mark.parametrize(
    ("env_name", "changed_value"),
    [
        ("MINDROOM_DOCKER_WORKER_IMAGE", "ghcr.io/mindroom-ai/mindroom:9.9.9"),
        ("MINDROOM_DOCKER_WORKER_PORT", "9100"),
        ("MINDROOM_DOCKER_WORKER_NAME_PREFIX", "other-prefix"),
        ("MINDROOM_DOCKER_WORKER_ENV_JSON", '{"EXTRA_ONE": "changed"}'),
        ("MINDROOM_DOCKER_WORKER_LABELS_JSON", '{"team": "other"}'),
    ],
)
def test_docker_signature_changes_when_one_field_changes(
    tmp_path: Path,
    env_name: str,
    changed_value: str,
) -> None:
    """Changing any single Docker config field must invalidate the cache signature."""
    base_paths = _runtime_paths(tmp_path, {**_MINIMAL_DOCKER_ENV})
    changed_paths = _runtime_paths(tmp_path, {**_MINIMAL_DOCKER_ENV, env_name: changed_value})
    storage_path = tmp_path / "shared-storage"

    assert docker_backend_config_signature(
        base_paths,
        auth_token=_TEST_AUTH_TOKEN,
        storage_path=storage_path,
    ) != docker_backend_config_signature(
        changed_paths,
        auth_token=_TEST_AUTH_TOKEN,
        storage_path=storage_path,
    )


def test_docker_signature_changes_with_auth_token_and_grantable_credentials(tmp_path: Path) -> None:
    """Auth token and grantable-credential changes must invalidate the Docker cache signature."""
    runtime_paths = _runtime_paths(tmp_path, _MINIMAL_DOCKER_ENV)
    base = docker_backend_config_signature(
        runtime_paths,
        auth_token=_TEST_AUTH_TOKEN,
        storage_path=runtime_paths.storage_root,
    )

    assert base != docker_backend_config_signature(
        runtime_paths,
        auth_token="rotated-token",  # noqa: S106
        storage_path=runtime_paths.storage_root,
    )
    assert base != docker_backend_config_signature(
        runtime_paths,
        auth_token=_TEST_AUTH_TOKEN,
        storage_path=runtime_paths.storage_root,
        worker_grantable_credentials=frozenset({"OPENAI_API_KEY"}),
    )
