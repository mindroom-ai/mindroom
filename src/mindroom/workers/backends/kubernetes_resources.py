"""Resource and manifest helpers for the Kubernetes worker backend.

Worker egress note: this provisioning path intentionally does not read the
static egress allowlist owned by ``mindroom.egress.policy``. It only passes
the deployment's proxy settings into worker pods; the egress proxy
deployed by the chart enforces the combined policy — static allowlist plus
approved temporary grants — centrally on actual traffic. The seam shared with
the policy layer is the worker key: grants minted by the approved-egress tool
for ``subject_type=worker_key`` must address the same worker key this backend
stamps on pods (``ANNOTATION_WORKER_KEY``), which both sides resolve through
``mindroom.tool_system.worker_routing``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import importlib
import json
import math
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

import httpx

from mindroom import constants
from mindroom.constants import RuntimePaths, config_relative_path, runtime_paths_with_storage_root
from mindroom.logging_config import get_logger
from mindroom.path_confinement import open_directory_within_root
from mindroom.runtime_env_policy import (
    CREDENTIALS_ENCRYPTION_KEY_ENV,
    KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY,
    SANDBOX_RUNTIME_ENV_BY_KEY,
    VENDOR_TELEMETRY_ENV_VALUES,
    isolated_worker_runtime_env,
    worker_extra_env,
)
from mindroom.tool_system.worker_routing import (
    WORKER_SHARED_CREDENTIALS_DIRNAME,
    worker_log_id,
    written_by_other_workers,
)
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.backends._dedicated_worker_common import (
    SharedDirectoryMount,
    agent_names_addressed_by_worker_key,
    plan_scoped_workspace_mounts,
    plan_shared_directory_mounts,
    resolve_state_scope_worker_key,
    stable_json_digest,
    validate_unique_worker_visible_paths,
)
from mindroom.workers.backends._lifecycle import WorkerLifecycleState
from mindroom.workers.backends.dedicated_launch import (
    DedicatedWorkerLayout,
    dedicated_worker_runner_env,
    dedicated_worker_startup_manifest_hash,
    dedicated_worker_startup_runtime_paths,
    write_dedicated_worker_startup_manifest,
)
from mindroom.workers.backends.kubernetes_config import resolve_kubeconfig_paths
from mindroom.workers.backends.kubernetes_pod_names import (
    SANDBOX_RUNNER_CONTAINER_NAME,
    WORKER_STORAGE_VOLUME_NAME,
    WORKER_TMP_VOLUME_NAME,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.agent_policy import ResolvedAgentPolicy
    from mindroom.config.main import Config
    from mindroom.workers.models import WorkerLaunchConfig, WorkerStatus

    from .kubernetes_config import KubernetesWorkerBackendConfig

logger = get_logger(__name__)

_READY_POLL_INTERVAL_SECONDS = 1.0
_DELETE_POLL_INTERVAL_SECONDS = 0.2
_HOSTNAME_ENV = "HOSTNAME"

_ANNOTATION_CREATED_AT = "mindroom.ai/created-at"
_ANNOTATION_LAST_USED_AT = "mindroom.ai/last-used-at"
_ANNOTATION_LAST_STARTED_AT = "mindroom.ai/last-started-at"
_ANNOTATION_STARTUP_COUNT = "mindroom.ai/startup-count"
_ANNOTATION_FAILURE_COUNT = "mindroom.ai/failure-count"
_ANNOTATION_FAILURE_REASON = "mindroom.ai/failure-reason"
ANNOTATION_WORKER_KEY = "mindroom.ai/worker-key"
_ANNOTATION_WORKER_STATUS = "mindroom.ai/worker-status"
ANNOTATION_STATE_SUBPATH = "mindroom.ai/state-subpath"
_ANNOTATION_STARTUP_MANIFEST_HASH = "mindroom.ai/startup-manifest-hash"
_ANNOTATION_RUNNER_TOKEN_HASH = "mindroom.ai/runner-token-hash"  # noqa: S105
ANNOTATION_TEMPLATE_HASH = "mindroom.ai/template-hash"
_ANNOTATION_PRIVATE_AGENT_NAMES = "mindroom.ai/private-agent-names"
_ANNOTATION_STATE_SCOPE_WORKER_KEY = "mindroom.ai/state-scope-worker-key"
_ANNOTATION_RESOURCE_PROFILE = "mindroom.ai/resource-profile"
# The template hash this release wrote, so a template an older release rewrote after a downgrade reads as old.
ANNOTATION_WORKSPACE_TEMPLATE_HASH = "mindroom.ai/workspace-template-hash"

_LABEL_COMPONENT = "mindroom.ai/component"
_LABEL_COMPONENT_VALUE = "worker"
_LABEL_MANAGED_BY = "app.kubernetes.io/managed-by"
_LABEL_MANAGED_BY_VALUE = "mindroom"
_LABEL_NAME = "app.kubernetes.io/name"
_LABEL_NAME_VALUE = "mindroom-worker"
# Every release's Kubernetes backend writes this label on its worker Deployments and their pods.
LABEL_WORKER_ID = "mindroom.ai/worker-id"
# /tmp is the only writable path outside the volumes on the read-only root filesystem.
# It stays disk-backed, so its size limit is not a hard write limit like the Docker worker
# tmpfs: kubelet measures usage periodically and evicts only this pod once it exceeds the
# limit, instead of the node running out of ephemeral storage.
_WORKER_TMP_SIZE_LIMIT = "1Gi"
_CONTAINER_NAME = SANDBOX_RUNNER_CONTAINER_NAME
_KUBERNETES_STORAGE_SUBPATH_PREFIX_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["storage_subpath_prefix"]
_WORKER_TOKEN_PURPOSE = b"mindroom-kubernetes-worker-token-v1"
_CREDENTIALS_ENCRYPTION_KEY_SECRET_SUFFIX = "credentials-encryption-key"  # noqa: S105


def _extend_unique_named_pod_entries(
    entries: list[dict[str, object]],
    extra_entries: tuple[dict[str, object], ...],
    *,
    field_name: str,
) -> None:
    existing_names = {name.strip() for entry in entries if isinstance(name := entry.get("name"), str) and name.strip()}
    for index, extra_entry in enumerate(extra_entries):
        name = extra_entry.get("name")
        if not isinstance(name, str) or not name.strip():
            msg = f"{field_name}[{index}].name must be a non-empty string."
            raise WorkerBackendError(msg)
        name = name.strip()
        if name in existing_names:
            msg = f"{field_name}[{index}].name duplicates existing pod entry: {name}."
            raise WorkerBackendError(msg)
        existing_names.add(name)
        entries.append(dict(extra_entry))


class _ApiStatusError(Exception):
    status: int


class _KubernetesMetadata(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def annotations(self) -> dict[str, str] | None: ...

    @property
    def labels(self) -> dict[str, str]: ...

    @property
    def generation(self) -> int | None: ...

    @property
    def uid(self) -> str | None: ...


class _KubernetesDeploymentSpec(Protocol):
    @property
    def replicas(self) -> int | None: ...


class _KubernetesDeploymentStatus(Protocol):
    @property
    def ready_replicas(self) -> int | None: ...

    @property
    def observed_generation(self) -> int | None: ...


class KubernetesDeployment(Protocol):
    """Minimal Deployment surface used by the backend."""

    @property
    def metadata(self) -> _KubernetesMetadata: ...  # noqa: D102

    @property
    def spec(self) -> _KubernetesDeploymentSpec: ...  # noqa: D102

    @property
    def status(self) -> _KubernetesDeploymentStatus: ...  # noqa: D102


@dataclass(slots=True)
class _DeploymentMetadataSnapshot:
    name: str
    annotations: dict[str, str] | None
    labels: dict[str, str]
    generation: int | None
    uid: str | None


@dataclass(slots=True)
class _DeploymentSpecSnapshot:
    replicas: int | None


@dataclass(slots=True)
class _DeploymentStatusSnapshot:
    ready_replicas: int | None
    observed_generation: int | None


@dataclass(slots=True)
class _DeploymentSnapshot:
    metadata: _DeploymentMetadataSnapshot
    spec: _DeploymentSpecSnapshot
    status: _DeploymentStatusSnapshot


class _KubernetesPodSpec(Protocol):
    node_name: str | None


class _KubernetesPod(Protocol):
    spec: _KubernetesPodSpec


class _KubernetesRawResponse(Protocol):
    data: bytes

    def release_conn(self) -> None: ...


class _AppsApiProtocol(Protocol):
    def read_namespaced_deployment(self, name: str, namespace: str) -> KubernetesDeployment: ...

    def create_namespaced_deployment(self, namespace: str, body: dict[str, object]) -> KubernetesDeployment: ...

    def patch_namespaced_deployment(
        self,
        name: str,
        namespace: str,
        body: dict[str, object],
        _request_timeout: float | None = None,
    ) -> KubernetesDeployment: ...

    def delete_namespaced_deployment(
        self,
        name: str,
        namespace: str,
        **kwargs: str,
    ) -> None: ...

    def list_namespaced_deployment(
        self,
        namespace: str,
        *,
        label_selector: str,
        _preload_content: bool = True,
        _request_timeout: float | None = None,
        limit: int | None = None,
    ) -> _KubernetesRawResponse: ...

    def list_namespaced_replica_set(self, namespace: str, **kwargs: object) -> _KubernetesRawResponse: ...


class _KubernetesApiClientProtocol(Protocol):
    def select_header_accept(self, content_types: list[str]) -> str: ...

    def call_api(
        self,
        resource_path: str,
        method: str,
        path_params: dict[str, str],
        query_params: list[object],
        header_params: dict[str, str],
        *,
        body: object,
        post_params: list[object],
        files: dict[str, object],
        response_type: str,
        auth_settings: list[str],
        _return_http_data_only: bool,
        _preload_content: bool,
        _request_timeout: object | None,
        collection_formats: dict[str, str],
    ) -> object: ...


class _CoreApiProtocol(Protocol):
    api_client: _KubernetesApiClientProtocol

    def read_namespaced_service(self, name: str, namespace: str) -> object: ...

    def create_namespaced_service(self, namespace: str, body: dict[str, object]) -> object: ...

    def patch_namespaced_service(self, name: str, namespace: str, body: dict[str, object]) -> object: ...

    def delete_namespaced_service(self, name: str, namespace: str) -> None: ...

    def read_namespaced_secret(self, name: str, namespace: str) -> object: ...

    def create_namespaced_secret(self, namespace: str, body: dict[str, object]) -> object: ...

    def delete_namespaced_secret(self, name: str, namespace: str) -> None: ...

    def list_namespaced_pod(self, namespace: str, **kwargs: object) -> _KubernetesRawResponse: ...

    def read_namespaced_pod(self, name: str, namespace: str) -> _KubernetesPod: ...


def service_host(service_name: str, namespace: str, port: int) -> str:
    """Return the cluster-local HTTP root for one worker Service."""
    return f"http://{service_name}.{namespace}.svc.cluster.local:{port}"


def worker_auth_token(shared_token: str | None, worker_key: str) -> str | None:
    """Derive the bearer token accepted by one dedicated worker runner."""
    if shared_token is None:
        return None
    key = shared_token.encode("utf-8")
    payload = _WORKER_TOKEN_PURPOSE + b"\0" + worker_key.encode("utf-8")
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def _worker_auth_token_hash(shared_token: str | None, worker_key: str) -> str | None:
    """Return a stable non-secret marker for the derived worker token."""
    token = worker_auth_token(shared_token, worker_key)
    if token is None:
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _secret_data_value(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def _worker_credentials_encryption_key_secret_key(worker_id: str) -> str:
    return f"{worker_id}.{_CREDENTIALS_ENCRYPTION_KEY_SECRET_SUFFIX}"


def _parse_annotation_float[DefaultT: float | None](
    annotations: Mapping[str, str],
    key: str,
    default: DefaultT,
) -> float | DefaultT:
    """Parse one float annotation, falling back to ``default`` when it is missing, blank, or not a number."""
    raw = annotations.get(key)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _parse_annotation_int(annotations: Mapping[str, str], key: str) -> int:
    """Parse one integer annotation, falling back to zero when it is missing, blank, or not a number."""
    raw = annotations.get(key)
    if not raw:
        return 0
    try:
        return int(raw)
    except ValueError:
        return 0


def _lifecycle_annotations(state: WorkerLifecycleState) -> dict[str, str]:
    """Serialize every lifecycle field of one worker to Deployment annotations.

    An unset value is written blank rather than omitted, so a merge patch clears the old value
    (it is read back as ``None``).
    """
    return {
        _ANNOTATION_CREATED_AT: str(state.created_at),
        _ANNOTATION_LAST_USED_AT: str(state.last_used_at),
        _ANNOTATION_STARTUP_COUNT: str(state.startup_count),
        _ANNOTATION_FAILURE_COUNT: str(state.failure_count),
        _ANNOTATION_WORKER_STATUS: state.status,
        _ANNOTATION_LAST_STARTED_AT: "" if state.last_started_at is None else str(state.last_started_at),
        _ANNOTATION_FAILURE_REASON: state.failure_reason or "",
    }


_USAGE_ANNOTATIONS = frozenset({_ANNOTATION_LAST_USED_AT, _ANNOTATION_WORKER_STATUS, _ANNOTATION_FAILURE_REASON})


def usage_annotations(state: WorkerLifecycleState) -> dict[str, str]:
    """Serialize only what a touch changes: last use, status, and failure reason.

    Start and failure history stay with the writers that read the Deployment first, so a manager whose cached view
    is older, such as one replaced after a kubeconfig rotation, never rolls back another manager's restart.
    """
    return {name: value for name, value in _lifecycle_annotations(state).items() if name in _USAGE_ANNOTATIONS}


def metadata_annotations(*, worker_key: str, state_subpath: str, state: WorkerLifecycleState) -> dict[str, str]:
    """Build the worker identity and lifecycle annotations a Deployment is applied with."""
    return {
        ANNOTATION_WORKER_KEY: worker_key,
        ANNOTATION_STATE_SUBPATH: state_subpath,
        **_lifecycle_annotations(state),
    }


def with_lifecycle_transition(
    annotations: Mapping[str, str],
    transition: Callable[[WorkerLifecycleState], WorkerLifecycleState],
    *,
    now: float,
) -> dict[str, str]:
    """Return ``annotations`` with the lifecycle they record replaced by its ``transition``."""
    return {**annotations, **_lifecycle_annotations(transition(lifecycle_from_annotations(annotations, now=now)))}


def lifecycle_from_annotations(annotations: Mapping[str, str], *, now: float) -> WorkerLifecycleState:
    """Parse the lifecycle state persisted on a worker Deployment's annotations."""
    last_used_at = _parse_annotation_float(annotations, _ANNOTATION_LAST_USED_AT, now)
    return WorkerLifecycleState(
        created_at=_parse_annotation_float(annotations, _ANNOTATION_CREATED_AT, last_used_at),
        last_used_at=last_used_at,
        status=cast("WorkerStatus", annotations.get(_ANNOTATION_WORKER_STATUS, "starting")),
        last_started_at=_parse_annotation_float(annotations, _ANNOTATION_LAST_STARTED_AT, None),
        startup_count=_parse_annotation_int(annotations, _ANNOTATION_STARTUP_COUNT),
        failure_count=_parse_annotation_int(annotations, _ANNOTATION_FAILURE_COUNT),
        failure_reason=annotations.get(_ANNOTATION_FAILURE_REASON) or None,
    )


def _template_hash(template: dict[str, object]) -> str:
    """Return a stable hash for one Deployment pod template."""
    return stable_json_digest(template)


def parse_private_agent_names_annotation(annotations: dict[str, str]) -> frozenset[str] | None:
    """Parse the persisted private-agent visibility annotation, ``None`` when absent."""
    raw = annotations.get(_ANNOTATION_PRIVATE_AGENT_NAMES)
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, list):
        return None
    return frozenset(name for name in parsed if isinstance(name, str))


def parse_state_scope_worker_key_annotation(annotations: dict[str, str]) -> str | None:
    """Parse the persisted state-scope worker key, ``None`` when absent."""
    value = annotations.get(_ANNOTATION_STATE_SCOPE_WORKER_KEY)
    return value or None


def parse_resource_profile_annotation(annotations: dict[str, str]) -> str | None:
    """Parse the persisted bounded resource profile, ``None`` for ordinary workers."""
    value = annotations.get(_ANNOTATION_RESOURCE_PROFILE)
    return value or None


def _labels(*, extra_labels: dict[str, str], worker_id: str) -> dict[str, str]:
    labels = {
        _LABEL_COMPONENT: _LABEL_COMPONENT_VALUE,
        _LABEL_MANAGED_BY: _LABEL_MANAGED_BY_VALUE,
        _LABEL_NAME: _LABEL_NAME_VALUE,
    }
    labels.update(extra_labels)
    labels[LABEL_WORKER_ID] = worker_id
    return labels


def _list_selector(*, extra_labels: dict[str, str]) -> str:
    selector = {
        _LABEL_COMPONENT: _LABEL_COMPONENT_VALUE,
        _LABEL_MANAGED_BY: _LABEL_MANAGED_BY_VALUE,
        _LABEL_NAME: _LABEL_NAME_VALUE,
    }
    selector.update(extra_labels)
    return ",".join(f"{key}={value}" for key, value in sorted(selector.items()))


def _optional_int(value: object, *, field_name: str) -> int | None:
    """Read one optional integer from a Kubernetes JSON response."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"Kubernetes Deployment list returned invalid {field_name}."
        raise WorkerBackendError(msg)
    return value


def _required_mapping(payload: Mapping[str, object], field_name: str) -> Mapping[str, object]:
    """Read one required object from a Kubernetes Deployment payload."""
    value = payload.get(field_name)
    if not isinstance(value, Mapping):
        msg = f"Kubernetes Deployment list returned an item without an object {field_name}."
        raise WorkerBackendError(msg)
    return cast("Mapping[str, object]", value)


def _optional_mapping(payload: Mapping[str, object], field_name: str) -> Mapping[str, object]:
    """Read one optional object, treating absence as empty."""
    value = payload.get(field_name)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        msg = f"Kubernetes Deployment list returned a non-object {field_name}."
        raise WorkerBackendError(msg)
    return cast("Mapping[str, object]", value)


def _string_mapping(value: object, *, field_name: str) -> dict[str, str]:
    """Read an optional string-to-string Kubernetes metadata mapping."""
    if value is None:
        return {}
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) or not isinstance(item, str) for key, item in value.items()
    ):
        msg = f"Kubernetes Deployment list returned invalid {field_name}."
        raise WorkerBackendError(msg)
    return dict(cast("Mapping[str, str]", value))


def _deployment_snapshot(payload: object) -> KubernetesDeployment:
    """Project one raw Deployment payload onto fields used by maintenance."""
    if not isinstance(payload, Mapping):
        msg = "Kubernetes Deployment list returned a non-object item."
        raise WorkerBackendError(msg)
    payload_mapping = cast("Mapping[str, object]", payload)
    metadata = _required_mapping(payload_mapping, "metadata")
    spec = _required_mapping(payload_mapping, "spec")
    # status is populated by the deployment controller, so a Deployment read
    # before that first write has none. Absence means "not ready yet", which the
    # None fields below already express -- never a reason to fail the whole pass.
    status = _optional_mapping(payload_mapping, "status")
    name = metadata.get("name")
    if not isinstance(name, str) or not name:
        msg = "Kubernetes Deployment list returned an item without metadata.name."
        raise WorkerBackendError(msg)
    uid = metadata.get("uid")
    if uid is not None and not isinstance(uid, str):
        msg = "Kubernetes Deployment list returned invalid metadata.uid."
        raise WorkerBackendError(msg)
    annotations = _string_mapping(metadata.get("annotations"), field_name="metadata.annotations")
    return _DeploymentSnapshot(
        metadata=_DeploymentMetadataSnapshot(
            name=name,
            annotations=annotations,
            labels=_string_mapping(metadata.get("labels"), field_name="metadata.labels"),
            generation=_optional_int(metadata.get("generation"), field_name="metadata.generation"),
            uid=uid,
        ),
        spec=_DeploymentSpecSnapshot(
            replicas=_optional_int(spec.get("replicas"), field_name="spec.replicas"),
        ),
        status=_DeploymentStatusSnapshot(
            ready_replicas=_optional_int(status.get("readyReplicas"), field_name="status.readyReplicas"),
            observed_generation=_optional_int(
                status.get("observedGeneration"),
                field_name="status.observedGeneration",
            ),
        ),
    )


@dataclass(frozen=True, slots=True)
class _KnowledgeStorageMountPlan:
    relative_path: Path
    mount_path: Path


def _first_overlapping_path(path: Path, candidates: tuple[Path, ...]) -> Path | None:
    return next(
        (candidate for candidate in candidates if path.is_relative_to(candidate) or candidate.is_relative_to(path)),
        None,
    )


def _plan_knowledge_storage_mounts(
    relative_paths: tuple[Path, ...],
    *,
    mounted_storage_root: Path,
    existing_mounts: tuple[dict[str, object], ...],
    worker_key: str,
    worker_roots: Path,
) -> tuple[_KnowledgeStorageMountPlan, ...]:
    storage_mount_paths = tuple(
        Path(cast("str", mount["mountPath"]))
        for mount in existing_mounts
        if mount["name"] == WORKER_STORAGE_VOLUME_NAME
    )
    other_mount_paths = tuple(
        Path(cast("str", mount["mountPath"]))
        for mount in existing_mounts
        if mount["name"] != WORKER_STORAGE_VOLUME_NAME
    )
    plans: list[_KnowledgeStorageMountPlan] = []
    for relative_path in relative_paths:
        mount_path = mounted_storage_root / relative_path
        if collision := _first_overlapping_path(mount_path, other_mount_paths):
            msg = (
                f"Kubernetes knowledge mount overlaps existing mountPath for worker {worker_log_id(worker_key)}: "
                f"{mount_path} and {collision}"
            )
            raise WorkerBackendError(msg)
        if any(mount_path.is_relative_to(existing_path) for existing_path in storage_mount_paths):
            continue
        if written_by_other_workers(relative_path, worker_roots):
            # kubelet follows links inside the volume when it mounts, so another worker could redirect it.
            logger.error(
                "Refusing to mount knowledge inside a directory other sandbox workers write",
                worker=worker_log_id(worker_key),
                path=str(relative_path),
            )
            continue
        if collision := _first_overlapping_path(mount_path, storage_mount_paths):
            msg = (
                f"Kubernetes knowledge mount overlaps existing mountPath for worker {worker_log_id(worker_key)}: "
                f"{mount_path} and {collision}"
            )
            raise WorkerBackendError(msg)
        if collision := _first_overlapping_path(
            mount_path,
            tuple(plan.mount_path for plan in plans),
        ):
            msg = f"Kubernetes knowledge mount paths overlap for worker {worker_log_id(worker_key)}: {mount_path} and {collision}"
            raise WorkerBackendError(msg)
        plans.append(_KnowledgeStorageMountPlan(relative_path=relative_path, mount_path=mount_path))
    return tuple(plans)


class KubernetesResourceManager:
    """Own Kubernetes API access, manifest construction, and cached cluster metadata."""

    def __init__(
        self,
        *,
        runtime_paths: RuntimePaths,
        config: KubernetesWorkerBackendConfig,
        auth_token: str | None,
        storage_root: Path,
    ) -> None:
        """Initialize one resource manager for a concrete backend configuration."""
        self.runtime_paths = runtime_paths
        self.config = config
        self.auth_token = auth_token
        self.storage_root = storage_root.expanduser().resolve()
        self.apps_api: _AppsApiProtocol | None = None
        self.core_api: _CoreApiProtocol | None = None
        self.api_exception_cls: type[_ApiStatusError] | None = None
        self._control_plane_node_name: str | None = None
        self._control_plane_node_name_loaded = False
        self._owner_reference: dict[str, object] | None = None
        self._owner_reference_loaded = False

    @property
    def _apps(self) -> _AppsApiProtocol:
        self._load_clients()
        assert self.apps_api is not None
        return self.apps_api

    @property
    def _core(self) -> _CoreApiProtocol:
        self._load_clients()
        assert self.core_api is not None
        return self.core_api

    @property
    def _api_exception(self) -> type[_ApiStatusError]:
        self._load_clients()
        assert self.api_exception_cls is not None
        return self.api_exception_cls

    def api_clients(self) -> tuple[_AppsApiProtocol, _CoreApiProtocol]:
        """Return the loaded apps and core API clients."""
        return self._apps, self._core

    def list_deployments(self, *, request_timeout: float | None = None) -> list[KubernetesDeployment]:
        """List lightweight worker snapshots without Kubernetes model deserialization."""
        response = self._apps.list_namespaced_deployment(
            self.config.namespace,
            label_selector=_list_selector(extra_labels=self.config.extra_labels),
            _preload_content=False,
            _request_timeout=request_timeout,
        )
        try:
            payload = json.loads(response.data)
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as exc:
            msg = "Kubernetes Deployment list returned invalid JSON."
            raise WorkerBackendError(msg) from exc
        finally:
            response.release_conn()
        if not isinstance(payload, Mapping) or not isinstance(payload.get("items"), list):
            msg = "Kubernetes Deployment list returned an invalid items payload."
            raise WorkerBackendError(msg)
        return [_deployment_snapshot(item) for item in payload["items"]]

    def read_deployment(self, deployment_name: str) -> KubernetesDeployment | None:
        """Read one Deployment, returning ``None`` for 404s."""
        try:
            return self._apps.read_namespaced_deployment(deployment_name, self.config.namespace)
        except self._api_exception as exc:
            if exc.status == 404:
                return None
            raise

    def apply_service(self, worker_id: str) -> None:
        """Create-or-patch one worker Service."""
        self._apply_object(
            read_fn=self._core.read_namespaced_service,
            create_fn=self._core.create_namespaced_service,
            patch_fn=self._core.patch_namespaced_service,
            resource_name=worker_id,
            manifest=self._service_manifest(worker_id),
        )

    def apply_auth_secret(self, *, worker_key: str, worker_id: str) -> None:
        """Create-or-patch one worker Secret containing its derived runner token."""
        worker_token = self._worker_auth_token(worker_key)
        if self.config.auth_secret_name is not None:
            self._patch_secret_merge(
                self.config.auth_secret_name,
                {"data": self._shared_auth_secret_data(worker_id=worker_id, worker_token=worker_token)},
            )
            return
        manifest = self._auth_secret_manifest(worker_key=worker_key, worker_id=worker_id)
        try:
            self._core.read_namespaced_secret(worker_id, self.config.namespace)
        except self._api_exception as exc:
            if exc.status != 404:
                raise
            try:
                self._core.create_namespaced_secret(self.config.namespace, manifest)
            except self._api_exception as create_exc:
                if create_exc.status != 409:
                    raise
                self._patch_secret_merge(worker_id, self._auth_secret_patch(worker_key=worker_key, worker_id=worker_id))
            return
        self._patch_secret_merge(worker_id, self._auth_secret_patch(worker_key=worker_key, worker_id=worker_id))

    def apply_deployment(
        self,
        *,
        worker_key: str,
        worker_id: str,
        state_subpath: str,
        annotations: dict[str, str],
        replicas: int,
        launch: WorkerLaunchConfig,
        private_agent_names: frozenset[str] | None = None,
        state_scope_worker_key: str | None = None,
        resource_profile: str | None = None,
        recreate: bool = False,
    ) -> bool:
        """Create-or-patch one worker Deployment, recreating it for pod-template drift or when asked.

        Returns whether the Deployment was recreated.
        """
        manifest = self._deployment_manifest(
            worker_key=worker_key,
            worker_id=worker_id,
            state_subpath=state_subpath,
            annotations=annotations,
            replicas=replicas,
            launch=launch,
            private_agent_names=private_agent_names,
            state_scope_worker_key=state_scope_worker_key,
            resource_profile=resource_profile,
        )
        existing = self.read_deployment(worker_id)
        if existing is not None:
            existing_annotations = existing.metadata.annotations or {}
            desired_metadata = cast("dict[str, object]", manifest.get("metadata", {}))
            desired_annotations = cast("dict[str, str]", desired_metadata.get("annotations", {}))
            if (
                recreate
                or existing_annotations.get(ANNOTATION_TEMPLATE_HASH) != desired_annotations[ANNOTATION_TEMPLATE_HASH]
            ):
                self._recreate_deployment(worker_id, manifest, timeout_seconds=self.config.ready_timeout_seconds)
                return True
        self._apply_object(
            read_fn=self._apps.read_namespaced_deployment,
            create_fn=self._apps.create_namespaced_deployment,
            patch_fn=self._apps.patch_namespaced_deployment,
            resource_name=worker_id,
            manifest=manifest,
        )
        return False

    def patch_deployment(
        self,
        deployment_name: str,
        *,
        replicas: int | None = None,
        annotations: dict[str, str] | None = None,
    ) -> None:
        """Patch Deployment metadata and/or scale."""
        body: dict[str, object] = {}
        if annotations is not None:
            existing = self.read_deployment(deployment_name)
            merged_annotations = dict(existing.metadata.annotations or {}) if existing is not None else {}
            merged_annotations.update(annotations)
            body["metadata"] = {"annotations": merged_annotations}
        if replicas is not None:
            body["spec"] = {"replicas": replicas}
        self._apps.patch_namespaced_deployment(deployment_name, self.config.namespace, body)

    def patch_deployment_annotations(self, deployment_name: str, *, annotations: dict[str, str]) -> bool:
        """Merge selected Deployment annotations, returning false when it disappeared."""
        try:
            self._apps.patch_namespaced_deployment(
                deployment_name,
                self.config.namespace,
                {"metadata": {"annotations": annotations}},
            )
        except self._api_exception as exc:
            if exc.status == 404:
                return False
            raise
        return True

    def delete_deployment(self, deployment_name: str, *, timeout_seconds: float) -> None:
        """Delete one worker Deployment, ignoring 404s, and wait until its dependent pods terminate."""
        try:
            self._apps.delete_namespaced_deployment(
                deployment_name,
                self.config.namespace,
                propagation_policy="Foreground",
            )
        except self._api_exception as exc:
            if exc.status != 404:
                raise
        self._wait_for_deployment_absent(deployment_name, timeout_seconds=timeout_seconds)

    def check_workers_absent_for_storage_upgrade(self, *, timeout_seconds: float) -> None:
        """Require all worker controllers and Pods absent throughout this namespace."""
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            msg = "Kubernetes worker preflight timeout must be a positive finite number."
            raise WorkerBackendError(msg)
        deadline = time.monotonic() + timeout_seconds

        def remaining() -> float:
            value = deadline - time.monotonic()
            if value <= 0:
                msg = "Kubernetes worker preflight timed out before absence was verified."
                raise WorkerBackendError(msg)
            return value

        try:
            inventory_readers = (
                self._apps.list_namespaced_deployment,
                self._apps.list_namespaced_replica_set,
                self._core.list_namespaced_pod,
            )
        except Exception as exc:
            msg = f"Failed to initialize Kubernetes worker absence verification: {exc}"
            raise WorkerBackendError(msg) from exc
        # worker-id is stamped after extra labels and inherited by child resources.
        # Even scaled-down controllers and terminating Pods must be removed first.
        for list_resources in inventory_readers:
            try:
                response = list_resources(
                    self.config.namespace,
                    label_selector=LABEL_WORKER_ID,
                    limit=1,
                    _preload_content=False,
                    _request_timeout=remaining(),
                )
                try:
                    payload = json.loads(response.data)
                finally:
                    response.release_conn()
            except Exception as exc:
                msg = f"Failed to verify Kubernetes worker absence: {exc}"
                raise WorkerBackendError(msg) from exc
            remaining()
            if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
                msg = "Kubernetes worker preflight returned an invalid inventory."
                raise WorkerBackendError(msg)
            if payload["items"]:
                msg = (
                    "Remove all Kubernetes worker Deployments, ReplicaSets, and Pods carrying "
                    f"{LABEL_WORKER_ID} in namespace '{self.config.namespace}' before the private-storage upgrade."
                )
                raise WorkerBackendError(msg)
            metadata = payload.get("metadata", {})
            if not isinstance(metadata, dict) or metadata.get("continue"):
                msg = "Kubernetes worker preflight returned an incomplete inventory."
                raise WorkerBackendError(msg)

    def delete_service(self, service_name: str) -> None:
        """Delete one worker Service, ignoring 404s."""
        self._delete_object(self._core.delete_namespaced_service, service_name)

    def delete_secret(self, secret_name: str) -> None:
        """Delete one worker auth Secret, ignoring 404s."""
        if self.config.auth_secret_name is not None:
            try:
                self._patch_secret_merge(
                    self.config.auth_secret_name,
                    {
                        "data": {
                            secret_name: None,
                            # Nulls the legacy key entry described above _worker_auth_secret_data.
                            _worker_credentials_encryption_key_secret_key(secret_name): None,
                        },
                    },
                )
            except self._api_exception as exc:
                if exc.status != 404:
                    raise
            return
        self._delete_object(self._core.delete_namespaced_secret, secret_name)

    def _patch_secret_merge(self, secret_name: str, body: dict[str, object]) -> None:
        api_client = self._core.api_client
        api_client.call_api(
            "/api/v1/namespaces/{namespace}/secrets/{name}",
            "PATCH",
            {"name": secret_name, "namespace": self.config.namespace},
            [],
            {
                "Accept": api_client.select_header_accept(
                    ["application/json", "application/yaml", "application/vnd.kubernetes.protobuf", "application/cbor"],
                ),
                "Content-Type": "application/merge-patch+json",
            },
            body=body,
            post_params=[],
            files={},
            response_type="V1Secret",
            auth_settings=["BearerToken"],
            _return_http_data_only=True,
            _preload_content=True,
            _request_timeout=None,
            collection_formats={},
        )

    def _recreate_deployment(
        self,
        deployment_name: str,
        manifest: dict[str, object],
        *,
        timeout_seconds: float,
    ) -> None:
        """Delete one Deployment, wait until it and its pods are gone, and create it again from the manifest."""
        self.delete_deployment(deployment_name, timeout_seconds=timeout_seconds)
        deadline = time.time() + timeout_seconds
        while True:
            try:
                self._apps.create_namespaced_deployment(self.config.namespace, manifest)
            except self._api_exception as exc:
                if exc.status != 409:
                    raise
                if time.time() >= deadline:
                    msg = (
                        f"Kubernetes worker deployment '{deployment_name}' did not finish deleting "
                        f"within {timeout_seconds:.0f}s before recreate."
                    )
                    raise WorkerBackendError(msg) from exc
                time.sleep(_DELETE_POLL_INTERVAL_SECONDS)
            else:
                return

    def _wait_for_deployment_absent(self, deployment_name: str, *, timeout_seconds: float) -> None:
        """Poll until one Deployment is fully gone after delete has been requested."""
        deadline = time.time() + timeout_seconds
        while True:
            if self.read_deployment(deployment_name) is None:
                return
            if time.time() >= deadline:
                msg = (
                    f"Kubernetes worker deployment '{deployment_name}' did not finish deleting "
                    f"within {timeout_seconds:.0f}s."
                )
                raise WorkerBackendError(msg)
            time.sleep(_DELETE_POLL_INTERVAL_SECONDS)

    def wait_for_ready(
        self,
        deployment_name: str,
        *,
        timeout_seconds: float,
        deployment_ready_fn: Callable[[KubernetesDeployment], bool],
        on_poll_tick: Callable[[], None] | None = None,
    ) -> KubernetesDeployment:
        """Poll a worker Deployment until it becomes ready or times out."""
        deadline = time.time() + timeout_seconds
        while True:
            deployment = self.read_deployment(deployment_name)
            if deployment is None:
                msg = f"Kubernetes worker deployment '{deployment_name}' disappeared during startup."
                raise WorkerBackendError(msg)
            if deployment_ready_fn(deployment):
                return deployment
            if time.time() >= deadline:
                msg = f"Kubernetes worker '{deployment_name}' did not become ready within {timeout_seconds:.0f}s."
                raise WorkerBackendError(msg)
            if on_poll_tick is not None:
                on_poll_tick()
            time.sleep(_READY_POLL_INTERVAL_SECONDS)

    def read_worker_health(self, worker_id: str, *, timeout_seconds: float) -> httpx.Response:
        """Return the first successful ``/healthz`` answer from one worker through its Service."""
        healthz_url = f"{service_host(worker_id, self.config.namespace, self.config.worker_port)}/healthz"
        deadline = time.time() + timeout_seconds
        with httpx.Client(timeout=min(5.0, timeout_seconds)) as client:
            while True:
                # The Service can still route nowhere for a moment after its Deployment turns ready.
                try:
                    response = client.get(healthz_url)
                except httpx.HTTPError:
                    response = None
                if response is not None and response.is_success:
                    return response
                if time.time() >= deadline:
                    msg = f"Kubernetes worker '{worker_id}' did not answer its health check within {timeout_seconds:.0f}s."
                    raise WorkerBackendError(msg)
                time.sleep(_READY_POLL_INTERVAL_SECONDS)

    def _apply_object(
        self,
        *,
        read_fn: Callable[[str, str], object],
        create_fn: Callable[[str, dict[str, object]], object],
        patch_fn: Callable[[str, str, dict[str, object]], object],
        resource_name: str,
        manifest: dict[str, object],
    ) -> None:
        try:
            read_fn(resource_name, self.config.namespace)
        except self._api_exception as exc:
            if exc.status != 404:
                raise
            try:
                create_fn(self.config.namespace, manifest)
            except self._api_exception as create_exc:
                if create_exc.status != 409:
                    raise
                patch_fn(resource_name, self.config.namespace, manifest)
            return
        patch_fn(resource_name, self.config.namespace, manifest)

    def _delete_object(self, delete_fn: Callable[[str, str], None], resource_name: str) -> None:
        try:
            delete_fn(resource_name, self.config.namespace)
        except self._api_exception as exc:
            if exc.status != 404:
                raise

    def _load_clients(self) -> None:
        if self.apps_api is not None and self.core_api is not None and self.api_exception_cls is not None:
            return
        try:
            kubernetes_config = importlib.import_module("kubernetes.config")
            kubernetes_client = importlib.import_module("kubernetes.client")
            kubernetes_exceptions = importlib.import_module("kubernetes.client.exceptions")
        except ModuleNotFoundError as exc:
            msg = "The Kubernetes worker backend needs the 'kubernetes' package: install mindroom[kubernetes]."
            raise WorkerBackendError(msg) from exc

        try:
            kubernetes_config.load_incluster_config()
        except Exception:
            kubeconfig_paths = resolve_kubeconfig_paths(self.runtime_paths)
            kubernetes_config.load_kube_config(
                config_file=os.pathsep.join(str(path) for path in kubeconfig_paths),
            )

        self.apps_api = cast("_AppsApiProtocol", kubernetes_client.AppsV1Api())
        self.core_api = cast("_CoreApiProtocol", kubernetes_client.CoreV1Api())
        self.api_exception_cls = cast("type[_ApiStatusError]", kubernetes_exceptions.ApiException)

    def _service_manifest(self, worker_id: str) -> dict[str, object]:
        worker_labels = _labels(extra_labels=self.config.extra_labels, worker_id=worker_id)
        metadata: dict[str, object] = {
            "name": worker_id,
            "namespace": self.config.namespace,
            "labels": worker_labels,
        }
        owner_reference = self._owner_reference_or_none()
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
                        "port": self.config.worker_port,
                        "targetPort": self.config.worker_port,
                    },
                ],
            },
        }

    def _auth_secret_manifest(self, *, worker_key: str, worker_id: str) -> dict[str, object]:
        worker_token = self._worker_auth_token(worker_key)
        worker_labels = _labels(extra_labels=self.config.extra_labels, worker_id=worker_id)
        metadata: dict[str, object] = {
            "name": worker_id,
            "namespace": self.config.namespace,
            "labels": worker_labels,
        }
        owner_reference = self._owner_reference_or_none()
        if owner_reference is not None:
            metadata["ownerReferences"] = [owner_reference]
        return {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": metadata,
            "type": "Opaque",
            "stringData": {SANDBOX_RUNTIME_ENV_BY_KEY["proxy_token"]: worker_token},
        }

    def _auth_secret_patch(self, *, worker_key: str, worker_id: str) -> dict[str, object]:
        worker_token = self._worker_auth_token(worker_key)
        worker_labels = _labels(extra_labels=self.config.extra_labels, worker_id=worker_id)
        metadata: dict[str, object] = {"labels": worker_labels}
        owner_reference = self._owner_reference_or_none()
        if owner_reference is not None:
            metadata["ownerReferences"] = [owner_reference]
        return {
            "metadata": metadata,
            "type": "Opaque",
            "data": self._worker_auth_secret_data(worker_token=worker_token),
        }

    # LEGACY_COMPAT: Worker auth Secrets that hold the primary's credential encryption key.
    # Legacy format: a per-worker Secret's MINDROOM_CREDENTIALS_ENCRYPTION_KEY entry, or a shared worker-auth Secret's
    #   <worker id>.credentials-encryption-key entry, written whenever the primary had credential encryption enabled.
    # Last legacy release: v2026.10.39; replacement: v2026.10.40 gives workers no key and writes neither entry.
    # Handling: every Secret apply and the worker cleanup that deletes a Secret entry patch the entry to null, so the
    #   key leaves each worker's Secret the next time the primary ensures or cleans up that worker.
    # Coverage: tests/test_kubernetes_worker_backend.py::test_kubernetes_worker_never_receives_credentials_encryption_key;
    #   tests/test_kubernetes_worker_backend.py::test_kubernetes_backend_reapply_removes_worker_secret_key;
    #   tests/test_kubernetes_worker_backend.py::test_kubernetes_backend_reapply_removes_shared_secret_key;
    #   tests/test_kubernetes_worker_backend.py::test_kubernetes_backend_cleanup_removes_only_own_key_from_tenant_auth_secret.
    def _worker_auth_secret_data(self, *, worker_token: str) -> dict[str, str | None]:
        return {
            SANDBOX_RUNTIME_ENV_BY_KEY["proxy_token"]: _secret_data_value(worker_token),
            CREDENTIALS_ENCRYPTION_KEY_ENV: None,
        }

    def _shared_auth_secret_data(self, *, worker_id: str, worker_token: str) -> dict[str, str | None]:
        # Nulls the legacy key entry described above _worker_auth_secret_data.
        return {
            worker_id: _secret_data_value(worker_token),
            _worker_credentials_encryption_key_secret_key(worker_id): None,
        }

    def deployment_template_drifted(
        self,
        deployment: KubernetesDeployment,
        *,
        worker_key: str,
        worker_id: str,
        state_subpath: str,
        launch: WorkerLaunchConfig,
        private_agent_names: frozenset[str] | None,
        state_scope_worker_key: str | None = None,
        resource_profile: str | None = None,
    ) -> bool:
        """Return whether one Deployment's recorded pod template differs from the one ``launch`` renders."""
        startup_manifest_hash = self._startup_manifest_hash(
            worker_key=worker_key,
            state_subpath=state_subpath,
            tool_validation_snapshot=launch.tool_validation_snapshot,
        )
        template = self._pod_template(
            worker_key=worker_key,
            worker_id=worker_id,
            state_subpath=state_subpath,
            startup_manifest_hash=startup_manifest_hash,
            launch=launch,
            private_agent_names=private_agent_names,
            state_scope_worker_key=state_scope_worker_key,
            resource_profile=resource_profile,
        )
        recorded_hash = dict(deployment.metadata.annotations or {}).get(ANNOTATION_TEMPLATE_HASH)
        return recorded_hash != _template_hash(template)

    def _pod_template(
        self,
        *,
        worker_key: str,
        worker_id: str,
        state_subpath: str,
        startup_manifest_hash: str,
        launch: WorkerLaunchConfig,
        private_agent_names: frozenset[str] | None,
        state_scope_worker_key: str | None = None,
        resource_profile: str | None = None,
    ) -> dict[str, object]:
        resolved_state_scope_worker_key = resolve_state_scope_worker_key(worker_key, state_scope_worker_key)
        # Per-user overrides follow the owning scope, so a CLI turn gets its requester's resources.
        resource_requests, resource_limits = self.config.resources_for_profile(
            resource_profile,
            worker_key=resolved_state_scope_worker_key,
        )
        if self.config.tmp_size_limit is not None:
            # kubelet also counts /tmp against the pod's summed container ephemeral-storage
            # limits, and some clusters (GKE Autopilot) inject a 1 GiB default when unset.
            resource_requests["ephemeral-storage"] = self.config.tmp_size_limit
            resource_limits["ephemeral-storage"] = self.config.tmp_size_limit
        worker_labels = _labels(extra_labels=self.config.extra_labels, worker_id=worker_id)
        template_annotations = dict(self.config.extra_annotations)
        template_annotations[ANNOTATION_WORKER_KEY] = worker_key
        template_annotations[_ANNOTATION_STARTUP_MANIFEST_HASH] = startup_manifest_hash
        token_hash = _worker_auth_token_hash(self.auth_token, worker_key)
        if token_hash is None:
            msg = "A worker auth token is required for Kubernetes workers."
            raise WorkerBackendError(msg)
        template_annotations[_ANNOTATION_RUNNER_TOKEN_HASH] = token_hash
        template_spec: dict[str, object] = {
            "serviceAccountName": self.config.service_account_name,
            "automountServiceAccountToken": False,
            "enableServiceLinks": self.config.enable_service_links,
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
                    "name": _CONTAINER_NAME,
                    "image": self.config.image,
                    "imagePullPolicy": self.config.image_pull_policy,
                    # A container command replaces the image's tini entrypoint, so name tini here:
                    # PID 1 must reap the children a killed command's process group leaves behind.
                    "command": ["tini", "--", "/app/run-sandbox-runner.sh"],
                    "ports": [{"containerPort": self.config.worker_port, "name": "api"}],
                    "env": self._worker_env(
                        worker_key=worker_key,
                        worker_id=worker_id,
                        state_subpath=state_subpath,
                    ),
                    "volumeMounts": self._volume_mounts(
                        worker_key,
                        state_subpath,
                        private_agent_names,
                        launch=launch,
                        state_scope_worker_key=state_scope_worker_key,
                    ),
                    "readinessProbe": {
                        "httpGet": {"path": "/healthz", "port": "api"},
                        "periodSeconds": 5,
                        "failureThreshold": 6,
                    },
                    "startupProbe": {
                        "httpGet": {"path": "/healthz", "port": "api"},
                        "periodSeconds": 5,
                        "failureThreshold": 60,
                    },
                    "livenessProbe": {
                        "httpGet": {"path": "/healthz", "port": "api"},
                        "periodSeconds": 10,
                        "failureThreshold": 6,
                    },
                    "resources": {
                        "requests": resource_requests,
                        "limits": resource_limits,
                    },
                    # The image's /app tree stays writable by the runtime user for
                    # trusted primaries, so worker pods mount the root filesystem
                    # read-only: tool code must not replace runner code the runner
                    # imports later. Only volumes and the /tmp emptyDir stay writable.
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "readOnlyRootFilesystem": True,
                        "capabilities": {"drop": ["ALL"]},
                        **(
                            {"seccompProfile": dict(self.config.seccomp_profile)}
                            if self.config.seccomp_profile is not None
                            else {}
                        ),
                    },
                },
            ],
            "volumes": self._volumes(),
        }
        containers = cast("list[dict[str, object]]", template_spec["containers"])
        _extend_unique_named_pod_entries(
            containers,
            self.config.extra_containers,
            field_name="extra_containers",
        )
        if self.config.runtime_class_name is not None:
            template_spec["runtimeClassName"] = self.config.runtime_class_name
        # Added only when configured so the default template, and its hash, stay unchanged.
        if self.config.image_pull_secrets:
            template_spec["imagePullSecrets"] = [{"name": name} for name in self.config.image_pull_secrets]
        node_name = self._worker_node_name_or_none()
        if node_name is not None:
            template_spec["nodeName"] = node_name
        return {
            "metadata": {
                "labels": worker_labels,
                "annotations": template_annotations,
            },
            "spec": template_spec,
        }

    def _deployment_manifest(
        self,
        *,
        worker_key: str,
        worker_id: str,
        state_subpath: str,
        annotations: dict[str, str],
        replicas: int,
        launch: WorkerLaunchConfig,
        private_agent_names: frozenset[str] | None = None,
        state_scope_worker_key: str | None = None,
        resource_profile: str | None = None,
    ) -> dict[str, object]:
        worker_labels = _labels(extra_labels=self.config.extra_labels, worker_id=worker_id)
        startup_manifest_hash = self._write_startup_manifest(
            worker_key=worker_key,
            state_subpath=state_subpath,
            tool_validation_snapshot=launch.tool_validation_snapshot,
        )
        template = self._pod_template(
            worker_key=worker_key,
            worker_id=worker_id,
            state_subpath=state_subpath,
            startup_manifest_hash=startup_manifest_hash,
            launch=launch,
            private_agent_names=private_agent_names,
            state_scope_worker_key=state_scope_worker_key,
            resource_profile=resource_profile,
        )
        metadata: dict[str, object] = {
            "name": worker_id,
            "namespace": self.config.namespace,
            "labels": worker_labels,
        }
        owner_reference = self._owner_reference_or_none()
        if owner_reference is not None:
            metadata["ownerReferences"] = [owner_reference]
        desired_annotations = dict(annotations)
        desired_annotations[ANNOTATION_TEMPLATE_HASH] = _template_hash(template)
        desired_annotations[ANNOTATION_WORKSPACE_TEMPLATE_HASH] = desired_annotations[ANNOTATION_TEMPLATE_HASH]
        if private_agent_names is not None:
            desired_annotations[_ANNOTATION_PRIVATE_AGENT_NAMES] = json.dumps(
                sorted(private_agent_names),
                separators=(",", ":"),
            )
        if state_scope_worker_key is not None:
            desired_annotations[_ANNOTATION_STATE_SCOPE_WORKER_KEY] = state_scope_worker_key
        else:
            desired_annotations.pop(_ANNOTATION_STATE_SCOPE_WORKER_KEY, None)
        if resource_profile is not None:
            desired_annotations[_ANNOTATION_RESOURCE_PROFILE] = resource_profile
        else:
            desired_annotations.pop(_ANNOTATION_RESOURCE_PROFILE, None)
        metadata["annotations"] = desired_annotations

        return {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": metadata,
            "spec": {
                "replicas": replicas,
                "selector": {"matchLabels": worker_labels},
                "template": template,
            },
        }

    def _worker_env(
        self,
        *,
        worker_key: str,
        worker_id: str,
        state_subpath: str,
    ) -> list[dict[str, object]]:
        layout = self._worker_layout(state_subpath)
        # No PATH or VIRTUAL_ENV here: the long-lived runner resolves uv, node,
        # chromium, and Xvnc from the image, never from the worker-writable venv.
        # Tool subprocesses get the worker venv from worker_subprocess_env.
        runner_env: list[dict[str, object]] = [
            {"name": name, "value": value}
            for name, value in dedicated_worker_runner_env(layout, worker_key=worker_key).items()
        ]
        env: list[dict[str, object]] = [
            *runner_env,
            {"name": "HOME", "value": layout.dedicated_root},
            self._worker_token_env(worker_id=worker_id),
        ]
        for name, value in sorted(worker_extra_env(self.config.extra_env).items()):
            env.append({"name": name, "value": value})
        for name, value in sorted(VENDOR_TELEMETRY_ENV_VALUES.items()):
            env.append({"name": name, "value": value})
        return env

    def _worker_token_env(self, *, worker_id: str) -> dict[str, object]:
        return {
            "name": SANDBOX_RUNTIME_ENV_BY_KEY["proxy_token"],
            "valueFrom": {
                "secretKeyRef": {
                    "name": self.config.auth_secret_name or worker_id,
                    "key": worker_id
                    if self.config.auth_secret_name is not None
                    else SANDBOX_RUNTIME_ENV_BY_KEY["proxy_token"],
                },
            },
        }

    def _worker_auth_token(self, worker_key: str) -> str:
        worker_token = worker_auth_token(self.auth_token, worker_key)
        if worker_token is None:
            msg = "A worker auth token is required for Kubernetes workers."
            raise WorkerBackendError(msg)
        return worker_token

    def _startup_manifest_hash(
        self,
        *,
        worker_key: str,
        state_subpath: str,
        tool_validation_snapshot: Mapping[str, object],
    ) -> str:
        """Return the content hash of one worker's startup manifest without writing it."""
        return dedicated_worker_startup_manifest_hash(
            self._worker_runtime_paths(worker_key=worker_key, state_subpath=state_subpath),
            tool_validation_snapshot=tool_validation_snapshot,
        )

    def _write_startup_manifest(
        self,
        *,
        worker_key: str,
        state_subpath: str,
        tool_validation_snapshot: Mapping[str, object],
    ) -> str:
        """Write one worker's startup manifest into its state directory and return the manifest's hash."""
        return write_dedicated_worker_startup_manifest(
            (self.storage_root / state_subpath).resolve(),
            self._worker_runtime_paths(worker_key=worker_key, state_subpath=state_subpath),
            tool_validation_snapshot=tool_validation_snapshot,
        )

    def _worker_layout(self, state_subpath: str) -> DedicatedWorkerLayout:
        """Return where one worker's runner finds its state below the storage mount, and the primary's config path.

        Nothing is mounted at the config path: a worker uses it only to resolve config-relative snapshot paths,
        never receives the primary's config file, its directory, or its `.env`, and takes agent settings only
        from the allowlisted snapshot each request carries.
        """
        return DedicatedWorkerLayout(
            dedicated_root=f"{self.config.storage_mount_path}/{state_subpath}".rstrip("/"),
            config_path=str(self.runtime_paths.config_path.expanduser().resolve()),
            worker_port=self.config.worker_port,
        )

    def _worker_runtime_paths(self, *, worker_key: str, state_subpath: str) -> RuntimePaths:
        # Kubernetes passes on only what isolated workers may inherit, so its manifests keep fewer
        # provider settings than Docker's; the runner recovers the shared storage root from the subpath prefix.
        isolated = constants.isolated_runtime_paths(self.runtime_paths)
        worker_env = {
            _KUBERNETES_STORAGE_SUBPATH_PREFIX_ENV: self.config.storage_subpath_prefix,
            **worker_extra_env(self.config.extra_env),
            **VENDOR_TELEMETRY_ENV_VALUES,
        }
        return dedicated_worker_startup_runtime_paths(
            self._worker_layout(state_subpath),
            worker_key=worker_key,
            process_env={**isolated.process_env, **isolated_worker_runtime_env(worker_env)},
            env_file_values=isolated.env_file_values,
        )

    def _volume_mounts(
        self,
        worker_key: str,
        state_subpath: str,
        private_agent_names: frozenset[str] | None,
        *,
        launch: WorkerLaunchConfig,
        state_scope_worker_key: str | None = None,
    ) -> list[dict[str, object]]:
        agent_policies = launch.agent_policies
        mounts = self._scoped_storage_mounts(
            worker_key,
            state_subpath,
            private_agent_names=private_agent_names,
            agent_policies=agent_policies,
            state_scope_worker_key=state_scope_worker_key,
        )
        mounts.extend(
            {
                "name": WORKER_STORAGE_VOLUME_NAME,
                "mountPath": str(shared_mount.worker_visible_path),
                "subPath": str(shared_mount.local_path.relative_to(self.storage_root)),
                **({"readOnly": True} if shared_mount.read_only else {}),
            }
            for shared_mount in self.plan_shared_directory_mounts(
                worker_key,
                state_scope_worker_key=state_scope_worker_key,
                agent_policies=agent_policies,
            )
        )
        mounts.append({"name": WORKER_TMP_VOLUME_NAME, "mountPath": "/tmp"})  # noqa: S108
        mounts.extend(
            self._knowledge_storage_mounts(
                worker_key,
                config=launch.config,
                agent_policies=agent_policies,
                mounted_storage_root=Path(self.config.storage_mount_path),
                existing_mounts=tuple(mounts),
            ),
        )
        validate_unique_worker_visible_paths(
            (str(mount["mountPath"]) for mount in mounts),
            worker_key=worker_key,
            duplicate_label="Kubernetes mountPath",
        )
        return mounts

    def _volumes(self) -> list[dict[str, object]]:
        volumes: list[dict[str, object]] = [
            {
                "name": WORKER_STORAGE_VOLUME_NAME,
                "persistentVolumeClaim": {"claimName": self.config.storage_pvc_name},
            },
            {
                "name": WORKER_TMP_VOLUME_NAME,
                "emptyDir": {"sizeLimit": self.config.tmp_size_limit or _WORKER_TMP_SIZE_LIMIT},
            },
        ]
        _extend_unique_named_pod_entries(
            volumes,
            self.config.extra_volumes,
            field_name="extra_volumes",
        )
        return volumes

    def _worker_node_name_or_none(self) -> str | None:
        if self.config.node_name is not None:
            return self.config.node_name
        if not self.config.colocate_with_control_plane_node:
            return None
        if self._control_plane_node_name_loaded:
            return self._control_plane_node_name

        pod_name = os.getenv(_HOSTNAME_ENV, "").strip()
        if not pod_name:
            self._control_plane_node_name_loaded = True
            return None
        try:
            pod = self._core.read_namespaced_pod(pod_name, self.config.namespace)
        except self._api_exception as exc:
            if exc.status != 404:
                raise
            pod = None
        self._control_plane_node_name = None if pod is None else pod.spec.node_name
        self._control_plane_node_name_loaded = True
        return self._control_plane_node_name

    def _owner_reference_or_none(self) -> dict[str, object] | None:
        if self._owner_reference_loaded:
            return self._owner_reference
        if self.config.owner_deployment_name is None:
            self._owner_reference_loaded = True
            return None

        owner_deployment = self.read_deployment(self.config.owner_deployment_name)
        if owner_deployment is None:
            msg = f"Configured Kubernetes worker owner deployment '{self.config.owner_deployment_name}' was not found."
            raise WorkerBackendError(msg)

        owner_uid = owner_deployment.metadata.uid
        if owner_uid is None or not owner_uid.strip():
            msg = (
                f"Configured Kubernetes worker owner deployment '{self.config.owner_deployment_name}' is missing a UID."
            )
            raise WorkerBackendError(msg)

        self._owner_reference = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "name": self.config.owner_deployment_name,
            "uid": owner_uid,
            "controller": False,
            "blockOwnerDeletion": False,
        }
        self._owner_reference_loaded = True
        return self._owner_reference

    def _scoped_storage_mounts(
        self,
        worker_key: str,
        state_subpath: str,
        *,
        private_agent_names: frozenset[str] | None,
        agent_policies: Mapping[str, ResolvedAgentPolicy],
        state_scope_worker_key: str | None = None,
    ) -> list[dict[str, object]]:
        mounts = self._workspace_storage_mounts(
            worker_key,
            private_agent_names=private_agent_names,
            agent_policies=agent_policies,
            state_scope_worker_key=state_scope_worker_key,
        )
        mounts.append(
            {
                "name": WORKER_STORAGE_VOLUME_NAME,
                "mountPath": f"{self.config.storage_mount_path}/{state_subpath}",
                "subPath": state_subpath,
            },
        )
        # The worker root is writable so tools can persist state, but the primary keeps
        # mirroring credentials into `.shared_credentials` on every ensure and writes the
        # startup manifest the runner boots from into `.runtime`. Mounting those
        # directories read-only stops worker code from rewriting them or replacing them
        # with links elsewhere.
        for read_only_subpath in (
            f"{state_subpath}/{WORKER_SHARED_CREDENTIALS_DIRNAME}",
            str(constants.sandbox_startup_manifest_path(Path(state_subpath)).parent),
        ):
            read_only_mount: dict[str, object] = {
                "name": WORKER_STORAGE_VOLUME_NAME,
                "mountPath": f"{self.config.storage_mount_path}/{read_only_subpath}",
                "subPath": read_only_subpath,
                "readOnly": True,
            }
            mounts.append(read_only_mount)
        validate_unique_worker_visible_paths(
            (str(mount["mountPath"]) for mount in mounts),
            worker_key=worker_key,
            duplicate_label="Kubernetes mountPath",
        )
        return mounts

    def plan_shared_directory_mounts(
        self,
        worker_key: str,
        *,
        state_scope_worker_key: str | None,
        agent_policies: Mapping[str, ResolvedAgentPolicy],
    ) -> tuple[SharedDirectoryMount, ...]:
        """Return the shared directories one worker mounts from the storage volume."""
        return plan_shared_directory_mounts(
            worker_key=resolve_state_scope_worker_key(worker_key, state_scope_worker_key),
            runtime_paths=runtime_paths_with_storage_root(self.runtime_paths, self.storage_root),
            worker_visible_shared_storage_root=Path(self.config.storage_mount_path),
            resolved_agent_policies=agent_policies,
        )

    def _workspace_storage_mounts(
        self,
        worker_key: str,
        *,
        private_agent_names: frozenset[str] | None,
        agent_policies: Mapping[str, ResolvedAgentPolicy],
        state_scope_worker_key: str | None,
    ) -> list[dict[str, object]]:
        return [
            {
                "name": WORKER_STORAGE_VOLUME_NAME,
                "mountPath": str(workspace_mount.worker_visible_path),
                "subPath": str(workspace_mount.local_path.relative_to(self.storage_root)),
            }
            for workspace_mount in plan_scoped_workspace_mounts(
                worker_key=resolve_state_scope_worker_key(worker_key, state_scope_worker_key),
                local_shared_storage_root=self.storage_root,
                worker_visible_shared_storage_root=Path(self.config.storage_mount_path),
                private_agent_names=private_agent_names,
                resolved_agent_policies=agent_policies,
            )
        ]

    def _knowledge_storage_mounts(
        self,
        worker_key: str,
        *,
        config: Config,
        agent_policies: Mapping[str, ResolvedAgentPolicy],
        mounted_storage_root: Path,
        existing_mounts: tuple[dict[str, object], ...],
    ) -> list[dict[str, object]]:
        plans = _plan_knowledge_storage_mounts(
            self._assigned_knowledge_storage_paths(worker_key, config=config, agent_policies=agent_policies),
            mounted_storage_root=mounted_storage_root,
            existing_mounts=existing_mounts,
            worker_key=worker_key,
            worker_roots=Path(self.config.storage_subpath_prefix),
        )
        return [
            {
                "name": WORKER_STORAGE_VOLUME_NAME,
                "mountPath": str(plan.mount_path),
                "subPath": str(plan.relative_path),
                "readOnly": True,
            }
            for plan in plans
        ]

    def _assigned_knowledge_storage_paths(
        self,
        worker_key: str,
        *,
        config: Config,
        agent_policies: Mapping[str, ResolvedAgentPolicy],
    ) -> tuple[Path, ...]:
        base_ids: set[str] = set()
        for agent_name in agent_names_addressed_by_worker_key(worker_key, agent_policies):
            agent_config = config.agents.get(agent_name)
            if agent_config is not None:
                base_ids.update(agent_config.knowledge_bases)

        relative_paths: set[Path] = set()
        for base_id in base_ids:
            base_config = config.knowledge_bases.get(base_id)
            if base_config is None:
                continue
            try:
                source_path = Path(os.path.normpath(config_relative_path(base_config.path, self.runtime_paths)))
                relative_path = source_path.relative_to(self.storage_root)
            except ValueError:
                logger.warning("Not mounting knowledge outside the worker storage root", knowledge_base=base_id)
                continue
            try:
                with open_directory_within_root(self.storage_root, relative_path):
                    relative_paths.add(relative_path)
            except (OSError, ValueError):
                logger.warning(
                    "Not mounting knowledge that is missing or reached through a link",
                    knowledge_base=base_id,
                    path=str(relative_path),
                )
        ordered_paths = list(relative_paths)
        ordered_paths.sort(key=lambda path: path.as_posix())
        return tuple(ordered_paths)
