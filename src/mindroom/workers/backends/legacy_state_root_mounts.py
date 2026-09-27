"""Stop sandbox workers that mounted whole agent state roots, once at primary startup."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, cast

from mindroom.logging_config import get_logger
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.backends import docker, kubernetes
from mindroom.workers.backends._lifecycle import mark_worker_idle
from mindroom.workers.backends.kubernetes_resources import (
    ANNOTATION_WORKSPACE_TEMPLATE_HASH,
    apply_lifecycle_annotations,
    lifecycle_from_annotations,
)
from mindroom.workers.runtime import primary_worker_backend_name

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.constants import RuntimePaths
    from mindroom.workers.backends.kubernetes_resources import KubernetesResourceManager

logger = get_logger(__name__)

# Every release's Kubernetes backend writes these keys on its worker Deployments and their pods.
_ANNOTATION_TEMPLATE_HASH = "mindroom.ai/template-hash"
_LABEL_WORKER_ID = "mindroom.ai/worker-id"
# Bounds each Kubernetes call, so an unreachable API server fails startup instead of stalling it.
_REQUEST_TIMEOUT_SECONDS = 30.0
# Covers the default 30s termination grace period of a stopped worker pod.
_POD_EXIT_TIMEOUT_SECONDS = 60.0
_POD_POLL_INTERVAL_SECONDS = 1.0


def retire_state_root_worker_mounts(runtime_paths: RuntimePaths) -> None:
    """Stop every worker that still mounts whole state roots before the primary serves, raising if any may run."""
    backend_name = primary_worker_backend_name(runtime_paths)
    if backend_name == "docker":
        _remove_docker_workers_mounting_state_roots(runtime_paths)
    elif backend_name == "kubernetes":
        _stop_kubernetes_workers_mounting_state_roots(kubernetes.standalone_resource_manager(runtime_paths))


def _warn_to_check_for_links(backend_name: str, workers: list[str]) -> None:
    if workers:
        logger.warning(
            "Stopped sandbox workers that mounted whole agent state roots; "
            "check agent state roots for links they may have planted, as the migration guide describes",
            backend=backend_name,
            workers=workers,
        )


# LEGACY_COMPAT: Docker worker containers that mount whole agent state roots.
# Legacy format: containers in this runtime namespace without the mindroom.ai/storage-layout label, created by
#   releases that bind-mounted agents/<agent> and private_instances/<scope> writable.
# Last legacy release: v2026.9.326; replacement: v2026.9.327 mounts only workspaces and labels its containers.
# Handling: before the primary serves anything, remove every such container, running or stopped, so the next ensure
#   recreates it with workspace mounts; durable worker state stays. A container that cannot be removed fails startup
#   after the others were attempted, so the primary restarts until none remain.
# Coverage: tests/test_docker_worker_backend.py::test_docker_startup_removes_workers_mounting_state_roots;
#   tests/test_docker_worker_backend.py::test_docker_retirement_keeps_removing_old_containers_after_one_fails.
def _remove_docker_workers_mounting_state_roots(runtime_paths: RuntimePaths) -> None:
    """Remove this runtime's containers created before workers mounted only workspaces."""
    removed: list[str] = []
    failed: list[str] = []
    for container in docker.list_docker_worker_containers(runtime_paths):
        labels = cast("dict[str, dict[str, str] | None]", container.attrs.get("Config") or {}).get("Labels") or {}
        if labels.get(docker.LABEL_STORAGE_LAYOUT) == docker.LABEL_STORAGE_LAYOUT_VALUE:
            continue
        try:
            container.remove(force=True)
        except Exception as exc:
            failed.append(f"{container.id} ({exc})")
            continue
        removed.append(container.id)
    _warn_to_check_for_links("docker", removed)
    if failed:
        msg = f"Failed to remove Docker workers that mount whole state roots: {', '.join(failed)}"
        raise WorkerBackendError(msg)


def _written_by_older_release(annotations: Mapping[str, str]) -> bool:
    template_hash = annotations.get(_ANNOTATION_TEMPLATE_HASH)
    return template_hash is None or annotations.get(ANNOTATION_WORKSPACE_TEMPLATE_HASH) != template_hash


# LEGACY_COMPAT: Kubernetes worker Deployments whose pods mount whole agent state roots.
# Legacy format: worker Deployments whose mindroom.ai/template-hash is not the mindroom.ai/workspace-template-hash
#   this release records beside it, because an older release, including after a downgrade, wrote a template that
#   mounted agents/<agent> and private_instances/<scope> writable.
# Last legacy release: v2026.9.326; replacement: v2026.9.327 mounts only workspaces and records its template hash.
# Handling: before the primary serves anything, scale every such running Deployment to zero as idle cleanup does and
#   wait up to 60 seconds for the pods of all of them to exit, ignoring evicted and completed pod objects. Any failure
#   fails startup after the other Deployments were attempted, so the primary restarts until none run. Scaled-down
#   Deployments stay; ensure recreates any Deployment whose template this release did not write.
# Coverage: tests/test_kubernetes_worker_backend.py::test_kubernetes_startup_stops_workers_whose_template_mounts_state_roots;
#   tests/test_kubernetes_worker_backend.py::test_kubernetes_pod_wait_ignores_finished_pods.
def _stop_kubernetes_workers_mounting_state_roots(resources: KubernetesResourceManager) -> None:
    """Scale to zero every worker whose pod template this release did not write, and wait for its pods to exit."""
    legacy = [
        deployment
        for deployment in resources.list_deployments(request_timeout=_REQUEST_TIMEOUT_SECONDS)
        if _written_by_older_release(deployment.metadata.annotations or {})
    ]
    apps_api, _core_api = resources.api_clients()
    now = time.time()
    stopped: list[str] = []
    failed: list[str] = []
    for deployment in legacy:
        if not deployment.spec.replicas:
            continue
        annotations = dict(deployment.metadata.annotations or {})
        apply_lifecycle_annotations(annotations, mark_worker_idle(lifecycle_from_annotations(annotations, now=now)))
        try:
            apps_api.patch_namespaced_deployment(
                deployment.metadata.name,
                resources.config.namespace,
                {"metadata": {"annotations": annotations}, "spec": {"replicas": 0}},
                _request_timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            failed.append(f"{deployment.metadata.name} ({exc})")
            continue
        stopped.append(deployment.metadata.name)
    _warn_to_check_for_links("kubernetes", stopped)
    if failed:
        msg = f"Could not stop workers that mount whole state roots: {', '.join(failed)}"
        raise WorkerBackendError(msg)
    _wait_for_pods_to_exit(resources, {deployment.metadata.name for deployment in legacy})


def _wait_for_pods_to_exit(resources: KubernetesResourceManager, worker_ids: set[str]) -> None:
    _apps_api, core_api = resources.api_clients()
    deadline = time.monotonic() + _POD_EXIT_TIMEOUT_SECONDS
    while worker_ids:
        response = core_api.list_namespaced_pod(
            resources.config.namespace,
            label_selector=_LABEL_WORKER_ID,
            _preload_content=False,
            _request_timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        try:
            pods = json.loads(response.data).get("items") or []
        finally:
            response.release_conn()
        # Evicted and completed pod objects wait for garbage collection but no longer run; terminating pods still do.
        worker_ids &= {
            pod.get("metadata", {}).get("labels", {}).get(_LABEL_WORKER_ID)
            for pod in pods
            if pod.get("status", {}).get("phase") not in {"Succeeded", "Failed"}
        }
        if not worker_ids:
            return
        if time.monotonic() >= deadline:
            msg = (
                f"Old Kubernetes worker pods did not stop within {_POD_EXIT_TIMEOUT_SECONDS:.0f}s: {sorted(worker_ids)}"
            )
            raise WorkerBackendError(msg)
        time.sleep(_POD_POLL_INTERVAL_SECONDS)
