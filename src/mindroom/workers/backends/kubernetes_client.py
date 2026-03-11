"""Kubernetes API helpers for the worker backend."""

from __future__ import annotations

import importlib
import time
from typing import TYPE_CHECKING, Protocol, cast

from mindroom.workers.backend import WorkerBackendError

_READY_POLL_INTERVAL_SECONDS = 1.0

if TYPE_CHECKING:
    from collections.abc import Callable


class _ApiStatusError(Exception):
    """Common protocol for Kubernetes API exceptions with HTTP-like status codes."""

    status: int


class _KubernetesMetadata(Protocol):
    """Minimal Deployment metadata surface used by the backend."""

    name: str
    annotations: dict[str, str] | None
    labels: dict[str, str]
    generation: int | None
    uid: str | None


class _KubernetesDeploymentSpec(Protocol):
    """Minimal Deployment spec surface used by the backend."""

    replicas: int | None


class _KubernetesDeploymentStatus(Protocol):
    """Minimal Deployment status surface used by the backend."""

    ready_replicas: int | None
    observed_generation: int | None


class _KubernetesDeployment(Protocol):
    """Minimal Deployment surface used by the backend."""

    metadata: _KubernetesMetadata
    spec: _KubernetesDeploymentSpec
    status: _KubernetesDeploymentStatus


class _KubernetesPodSpec(Protocol):
    """Minimal Pod spec surface used for control-plane node detection."""

    node_name: str | None


class _KubernetesPod(Protocol):
    """Minimal Pod surface used by the backend."""

    spec: _KubernetesPodSpec


class _KubernetesDeploymentList(Protocol):
    """Deployment list response surface used by the backend."""

    items: list[_KubernetesDeployment] | None


class _AppsApiProtocol(Protocol):
    """Apps API operations used by the backend."""

    def read_namespaced_deployment(self, name: str, namespace: str) -> _KubernetesDeployment: ...

    def create_namespaced_deployment(self, namespace: str, body: dict[str, object]) -> _KubernetesDeployment: ...

    def patch_namespaced_deployment(
        self,
        name: str,
        namespace: str,
        body: dict[str, object],
    ) -> _KubernetesDeployment: ...

    def delete_namespaced_deployment(self, name: str, namespace: str) -> None: ...

    def list_namespaced_deployment(self, namespace: str, label_selector: str) -> _KubernetesDeploymentList: ...


class _CoreApiProtocol(Protocol):
    """Core API operations used by the backend."""

    def read_namespaced_service(self, name: str, namespace: str) -> object: ...

    def create_namespaced_service(self, namespace: str, body: dict[str, object]) -> object: ...

    def patch_namespaced_service(self, name: str, namespace: str, body: dict[str, object]) -> object: ...

    def delete_namespaced_service(self, name: str, namespace: str) -> None: ...

    def read_namespaced_pod(self, name: str, namespace: str) -> _KubernetesPod: ...


def load_clients(
    *,
    apps_api: _AppsApiProtocol | None,
    core_api: _CoreApiProtocol | None,
    api_exception_cls: type[_ApiStatusError] | None,
) -> tuple[_AppsApiProtocol, _CoreApiProtocol, type[_ApiStatusError]]:
    """Load Kubernetes API clients or return already-injected test doubles."""
    if apps_api is not None and core_api is not None and api_exception_cls is not None:
        return apps_api, core_api, api_exception_cls

    try:
        kubernetes_config = importlib.import_module("kubernetes.config")
        kubernetes_client = importlib.import_module("kubernetes.client")
        kubernetes_exceptions = importlib.import_module("kubernetes.client.exceptions")
    except ModuleNotFoundError as exc:
        msg = "The 'kubernetes' package is required for the Kubernetes worker backend."
        raise WorkerBackendError(msg) from exc

    try:
        kubernetes_config.load_incluster_config()
    except Exception:
        kubernetes_config.load_kube_config()

    return (
        cast("_AppsApiProtocol", kubernetes_client.AppsV1Api()),
        cast("_CoreApiProtocol", kubernetes_client.CoreV1Api()),
        cast("type[_ApiStatusError]", kubernetes_exceptions.ApiException),
    )


def apply_service(
    *,
    core_api: _CoreApiProtocol,
    api_exception_cls: type[_ApiStatusError],
    namespace: str,
    service_name: str,
    manifest: dict[str, object],
) -> None:
    """Create-or-patch one worker Service."""
    try:
        core_api.read_namespaced_service(service_name, namespace)
    except api_exception_cls as exc:
        if exc.status != 404:
            raise
        try:
            core_api.create_namespaced_service(namespace, manifest)
        except api_exception_cls as create_exc:
            if create_exc.status != 409:
                raise
            core_api.patch_namespaced_service(service_name, namespace, manifest)
        return
    core_api.patch_namespaced_service(service_name, namespace, manifest)


def apply_deployment(
    *,
    apps_api: _AppsApiProtocol,
    api_exception_cls: type[_ApiStatusError],
    namespace: str,
    deployment_name: str,
    manifest: dict[str, object],
) -> None:
    """Create-or-patch one worker Deployment."""
    try:
        apps_api.read_namespaced_deployment(deployment_name, namespace)
    except api_exception_cls as exc:
        if exc.status != 404:
            raise
        try:
            apps_api.create_namespaced_deployment(namespace, manifest)
        except api_exception_cls as create_exc:
            if create_exc.status != 409:
                raise
            apps_api.patch_namespaced_deployment(deployment_name, namespace, manifest)
        return
    apps_api.patch_namespaced_deployment(deployment_name, namespace, manifest)


def patch_deployment(
    *,
    apps_api: _AppsApiProtocol,
    namespace: str,
    deployment_name: str,
    replicas: int | None = None,
    annotations: dict[str, str] | None = None,
) -> None:
    """Patch Deployment metadata and/or scale."""
    body: dict[str, object] = {}
    if annotations is not None:
        body["metadata"] = {"annotations": annotations}
    if replicas is not None:
        body["spec"] = {"replicas": replicas}
    apps_api.patch_namespaced_deployment(deployment_name, namespace, body)


def read_deployment(
    *,
    apps_api: _AppsApiProtocol,
    api_exception_cls: type[_ApiStatusError],
    namespace: str,
    deployment_name: str,
) -> _KubernetesDeployment | None:
    """Read one Deployment, returning ``None`` for 404s."""
    try:
        return apps_api.read_namespaced_deployment(deployment_name, namespace)
    except api_exception_cls as exc:
        if exc.status == 404:
            return None
        raise


def list_deployments(
    *,
    apps_api: _AppsApiProtocol,
    namespace: str,
    label_selector: str,
) -> list[_KubernetesDeployment]:
    """List Deployments matching the worker backend selector."""
    response = apps_api.list_namespaced_deployment(namespace, label_selector=label_selector)
    return list(response.items or [])


def delete_deployment(
    *,
    apps_api: _AppsApiProtocol,
    api_exception_cls: type[_ApiStatusError],
    namespace: str,
    deployment_name: str,
) -> None:
    """Delete one worker Deployment, ignoring 404s."""
    try:
        apps_api.delete_namespaced_deployment(deployment_name, namespace)
    except api_exception_cls as exc:
        if exc.status != 404:
            raise


def delete_service(
    *,
    core_api: _CoreApiProtocol,
    api_exception_cls: type[_ApiStatusError],
    namespace: str,
    service_name: str,
) -> None:
    """Delete one worker Service, ignoring 404s."""
    try:
        core_api.delete_namespaced_service(service_name, namespace)
    except api_exception_cls as exc:
        if exc.status != 404:
            raise


def read_pod_node_name(
    *,
    core_api: _CoreApiProtocol,
    api_exception_cls: type[_ApiStatusError],
    namespace: str,
    pod_name: str,
) -> str | None:
    """Read one Pod and return its node name, ignoring 404s."""
    try:
        pod = core_api.read_namespaced_pod(pod_name, namespace)
    except api_exception_cls as exc:
        if exc.status == 404:
            return None
        raise
    return pod.spec.node_name


def wait_for_ready(
    *,
    deployment_name: str,
    timeout_seconds: float,
    read_deployment_fn: Callable[[str], _KubernetesDeployment | None],
    deployment_ready_fn: Callable[[_KubernetesDeployment], bool],
) -> _KubernetesDeployment:
    """Poll a worker Deployment until it becomes ready or times out."""
    deadline = time.time() + timeout_seconds
    while True:
        deployment = read_deployment_fn(deployment_name)
        if deployment is None:
            msg = f"Kubernetes worker deployment '{deployment_name}' disappeared during startup."
            raise WorkerBackendError(msg)
        if deployment_ready_fn(deployment):
            return deployment
        if time.time() >= deadline:
            msg = f"Kubernetes worker '{deployment_name}' did not become ready within {timeout_seconds:.0f}s."
            raise WorkerBackendError(msg)
        time.sleep(_READY_POLL_INTERVAL_SECONDS)
