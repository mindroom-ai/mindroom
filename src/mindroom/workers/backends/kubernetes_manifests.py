"""Manifest and metadata helpers for the Kubernetes worker backend."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from mindroom.credentials import SHARED_CREDENTIALS_PATH_ENV
from mindroom.workers.backend import WorkerBackendError

if TYPE_CHECKING:
    from mindroom.workers.models import WorkerStatus

    from .kubernetes_config import KubernetesWorkerBackendConfig

_DEFAULT_NAME_PREFIX = "mindroom-worker"
_DEFAULT_RESOURCE_REQUESTS = {"memory": "256Mi", "cpu": "100m"}
_DEFAULT_RESOURCE_LIMITS = {"memory": "1Gi", "cpu": "500m"}

ANNOTATION_CREATED_AT = "mindroom.ai/created-at"
ANNOTATION_LAST_USED_AT = "mindroom.ai/last-used-at"
ANNOTATION_LAST_STARTED_AT = "mindroom.ai/last-started-at"
ANNOTATION_STARTUP_COUNT = "mindroom.ai/startup-count"
ANNOTATION_FAILURE_COUNT = "mindroom.ai/failure-count"
ANNOTATION_FAILURE_REASON = "mindroom.ai/failure-reason"
ANNOTATION_WORKER_KEY = "mindroom.ai/worker-key"
ANNOTATION_WORKER_STATUS = "mindroom.ai/worker-status"
ANNOTATION_STATE_SUBPATH = "mindroom.ai/state-subpath"

LABEL_COMPONENT = "mindroom.ai/component"
LABEL_COMPONENT_VALUE = "worker"
LABEL_MANAGED_BY = "app.kubernetes.io/managed-by"
LABEL_MANAGED_BY_VALUE = "mindroom"
LABEL_NAME = "app.kubernetes.io/name"
LABEL_NAME_VALUE = "mindroom-worker"
LABEL_WORKER_ID = "mindroom.ai/worker-id"

CONTAINER_NAME = "sandbox-runner"
TOKEN_ENV_NAME = "MINDROOM_SANDBOX_PROXY_TOKEN"  # noqa: S105
RUNNER_PORT_ENV_NAME = "MINDROOM_SANDBOX_RUNNER_PORT"
DEDICATED_WORKER_KEY_ENV = "MINDROOM_SANDBOX_DEDICATED_WORKER_KEY"
DEDICATED_WORKER_ROOT_ENV = "MINDROOM_SANDBOX_DEDICATED_WORKER_ROOT"


def worker_id_for_key(worker_key: str, *, prefix: str) -> str:
    """Return a DNS-safe Kubernetes resource name for one worker key."""
    digest = hashlib.sha256(worker_key.encode("utf-8")).hexdigest()[:24]
    normalized_prefix = prefix.strip().lower().strip("-") or _DEFAULT_NAME_PREFIX
    max_prefix_length = 63 - len(digest) - 1
    safe_prefix = normalized_prefix[:max_prefix_length].rstrip("-")
    if not safe_prefix:
        safe_prefix = _DEFAULT_NAME_PREFIX[:max_prefix_length].rstrip("-") or "worker"
    return f"{safe_prefix}-{digest}"


def service_host(service_name: str, namespace: str, port: int) -> str:
    """Return the cluster-local HTTP root for one worker Service."""
    return f"http://{service_name}.{namespace}.svc.cluster.local:{port}"


def parse_annotation_float(annotations: dict[str, str], key: str, default: float) -> float:
    """Parse one float annotation, falling back to a caller-provided default."""
    raw = annotations.get(key)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def parse_annotation_int(annotations: dict[str, str], key: str, default: int = 0) -> int:
    """Parse one integer annotation, falling back to a caller-provided default."""
    raw = annotations.get(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def labels(*, extra_labels: dict[str, str], worker_id: str) -> dict[str, str]:
    """Build the common label set for one worker deployment/service pair."""
    built_labels = {
        LABEL_COMPONENT: LABEL_COMPONENT_VALUE,
        LABEL_MANAGED_BY: LABEL_MANAGED_BY_VALUE,
        LABEL_NAME: LABEL_NAME_VALUE,
    }
    built_labels.update(extra_labels)
    built_labels[LABEL_WORKER_ID] = worker_id
    return built_labels


def list_selector(*, extra_labels: dict[str, str]) -> str:
    """Build the label selector used to list managed worker deployments."""
    selector_labels = {
        LABEL_COMPONENT: LABEL_COMPONENT_VALUE,
        LABEL_MANAGED_BY: LABEL_MANAGED_BY_VALUE,
        LABEL_NAME: LABEL_NAME_VALUE,
    }
    selector_labels.update(extra_labels)
    return ",".join(f"{key}={value}" for key, value in sorted(selector_labels.items()))


def metadata_annotations(
    *,
    worker_key: str,
    state_subpath: str,
    created_at: float,
    last_used_at: float,
    last_started_at: float | None,
    startup_count: int,
    failure_count: int,
    failure_reason: str | None,
    status: WorkerStatus,
) -> dict[str, str]:
    """Build persisted worker lifecycle metadata stored on Deployments."""
    annotations = {
        ANNOTATION_WORKER_KEY: worker_key,
        ANNOTATION_STATE_SUBPATH: state_subpath,
        ANNOTATION_CREATED_AT: str(created_at),
        ANNOTATION_LAST_USED_AT: str(last_used_at),
        ANNOTATION_STARTUP_COUNT: str(startup_count),
        ANNOTATION_FAILURE_COUNT: str(failure_count),
        ANNOTATION_WORKER_STATUS: status,
    }
    if last_started_at is not None:
        annotations[ANNOTATION_LAST_STARTED_AT] = str(last_started_at)
    if failure_reason:
        annotations[ANNOTATION_FAILURE_REASON] = failure_reason
    return annotations


def worker_env(
    *,
    config: KubernetesWorkerBackendConfig,
    auth_token: str | None,
    worker_key: str,
) -> list[dict[str, object]]:
    """Build the worker container environment."""
    env: list[dict[str, object]] = [
        {"name": "MINDROOM_SANDBOX_RUNNER_MODE", "value": "true"},
        {"name": "MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE", "value": "subprocess"},
        {"name": RUNNER_PORT_ENV_NAME, "value": str(config.worker_port)},
        {"name": "MINDROOM_STORAGE_PATH", "value": config.storage_mount_path},
        {
            "name": SHARED_CREDENTIALS_PATH_ENV,
            "value": f"{config.storage_mount_path}/.shared_credentials",
        },
        {"name": DEDICATED_WORKER_KEY_ENV, "value": worker_key},
        {"name": DEDICATED_WORKER_ROOT_ENV, "value": config.storage_mount_path},
        {"name": "HOME", "value": config.storage_mount_path},
    ]
    if config.config_map_name is not None:
        env.append({"name": "MINDROOM_CONFIG_PATH", "value": config.config_path})
    if config.token_secret_name is not None:
        env.append(
            {
                "name": TOKEN_ENV_NAME,
                "valueFrom": {
                    "secretKeyRef": {
                        "name": config.token_secret_name,
                        "key": config.token_secret_key,
                    },
                },
            },
        )
    elif auth_token is not None:
        env.append({"name": TOKEN_ENV_NAME, "value": auth_token})
    else:
        msg = "A worker auth token is required for Kubernetes workers."
        raise WorkerBackendError(msg)

    for name, value in sorted(config.extra_env.items()):
        env.append({"name": name, "value": value})
    return env


def volume_mounts(
    *,
    config: KubernetesWorkerBackendConfig,
    state_subpath: str,
) -> list[dict[str, object]]:
    """Build worker volume mounts."""
    mounts: list[dict[str, object]] = [
        {
            "name": "worker-storage",
            "mountPath": config.storage_mount_path,
            "subPath": state_subpath,
        },
    ]
    if config.config_map_name is not None:
        mounts.append(
            {
                "name": "worker-config",
                "mountPath": config.config_path,
                "subPath": config.config_key,
                "readOnly": True,
            },
        )
    return mounts


def volumes(config: KubernetesWorkerBackendConfig) -> list[dict[str, object]]:
    """Build worker volumes."""
    built_volumes: list[dict[str, object]] = [
        {
            "name": "worker-storage",
            "persistentVolumeClaim": {
                "claimName": config.storage_pvc_name,
            },
        },
    ]
    if config.config_map_name is not None:
        built_volumes.append(
            {
                "name": "worker-config",
                "configMap": {
                    "name": config.config_map_name,
                },
            },
        )
    return built_volumes


def service_manifest(
    *,
    config: KubernetesWorkerBackendConfig,
    worker_id: str,
    owner_reference: dict[str, object] | None,
) -> dict[str, object]:
    """Build the Service manifest for one worker."""
    worker_labels = labels(extra_labels=config.extra_labels, worker_id=worker_id)
    metadata: dict[str, object] = {
        "name": worker_id,
        "namespace": config.namespace,
        "labels": worker_labels,
    }
    if owner_reference is not None:
        metadata["ownerReferences"] = [owner_reference]
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": metadata,
        "spec": {
            "selector": worker_labels,
            "ports": [
                {
                    "name": "api",
                    "port": config.worker_port,
                    "targetPort": config.worker_port,
                },
            ],
        },
    }


def deployment_manifest(
    *,
    config: KubernetesWorkerBackendConfig,
    auth_token: str | None,
    worker_key: str,
    worker_id: str,
    state_subpath: str,
    annotations: dict[str, str],
    replicas: int,
    owner_reference: dict[str, object] | None,
    node_name: str | None,
) -> dict[str, object]:
    """Build the Deployment manifest for one worker."""
    worker_labels = labels(extra_labels=config.extra_labels, worker_id=worker_id)
    metadata: dict[str, object] = {
        "name": worker_id,
        "namespace": config.namespace,
        "labels": worker_labels,
        "annotations": annotations,
    }
    if owner_reference is not None:
        metadata["ownerReferences"] = [owner_reference]

    pod_spec: dict[str, object] = {
        "serviceAccountName": config.service_account_name,
        "securityContext": {
            "runAsUser": 1000,
            "runAsGroup": 1000,
            "fsGroup": 1000,
            "runAsNonRoot": True,
            "fsGroupChangePolicy": "OnRootMismatch",
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "containers": [
            {
                "name": CONTAINER_NAME,
                "image": config.image,
                "imagePullPolicy": config.image_pull_policy,
                "command": ["/app/run-sandbox-runner.sh"],
                "ports": [{"containerPort": config.worker_port, "name": "api"}],
                "env": worker_env(config=config, auth_token=auth_token, worker_key=worker_key),
                "volumeMounts": volume_mounts(config=config, state_subpath=state_subpath),
                "readinessProbe": {
                    "httpGet": {"path": "/healthz", "port": "api"},
                    "periodSeconds": 5,
                    "failureThreshold": 6,
                },
                "livenessProbe": {
                    "httpGet": {"path": "/healthz", "port": "api"},
                    "periodSeconds": 10,
                    "failureThreshold": 6,
                },
                "resources": {
                    "requests": dict(_DEFAULT_RESOURCE_REQUESTS),
                    "limits": dict(_DEFAULT_RESOURCE_LIMITS),
                },
                "securityContext": {
                    "allowPrivilegeEscalation": False,
                    "capabilities": {
                        "drop": ["ALL"],
                    },
                },
            },
        ],
        "volumes": volumes(config),
    }
    if node_name is not None:
        pod_spec["nodeName"] = node_name

    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": metadata,
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": worker_labels},
            "template": {
                "metadata": {
                    "labels": worker_labels,
                },
                "spec": pod_spec,
            },
        },
    }
