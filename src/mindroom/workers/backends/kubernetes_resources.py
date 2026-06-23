"""Resource and manifest helpers for the Kubernetes worker backend.

Worker egress note: this provisioning path intentionally does not read the
static egress allowlist owned by ``mindroom.egress.policy``. It only wires
proxy credentials (URL, minted token, CA) into worker pods; the egress proxy
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
import os
import posixpath
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol, cast

import yaml

from mindroom import constants
from mindroom.constants import RuntimePaths
from mindroom.runtime_env_policy import (
    CREDENTIALS_ENCRYPTION_KEY_ENV,
    KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY,
    SANDBOX_RUNTIME_ENV_BY_KEY,
    SANDBOX_STARTUP_MANIFEST_PATH_ENV,
    SHARED_CREDENTIALS_PATH_ENV,
    VENDOR_TELEMETRY_ENV_VALUES,
    WORKER_EGRESS_PROXY_ENV_BY_KEY,
    credentials_encryption_key_value,
    worker_extra_env,
)
from mindroom.tool_system.worker_routing import worker_id_for_key
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.backends._dedicated_worker_common import (
    plan_scoped_visible_state_roots,
    resolved_agent_policies_from_config_data,
    validate_unique_worker_visible_paths,
)
from mindroom.workers.backends._lifecycle import WorkerLifecycleState
from mindroom.workers.backends.kubernetes_config import (
    credentials_encryption_key_hash,
    is_kubernetes_worker_backend_config_env_name,
)
from mindroom.workers.backends.kubernetes_pod_names import (
    AGENT_VAULT_BOOTSTRAP_VOLUME_NAME,
    AGENT_VAULT_CA_VOLUME_NAME,
    AGENT_VAULT_MINT_CONTAINER_NAME,
    AGENT_VAULT_TOKEN_VOLUME_NAME,
    SANDBOX_RUNNER_CONTAINER_NAME,
    WORKER_CONFIG_VOLUME_NAME,
    WORKER_STORAGE_VOLUME_NAME,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.agent_policy import ResolvedAgentPolicy
    from mindroom.workers.models import WorkerStatus

    from .kubernetes_config import KubernetesAgentVaultConfig, KubernetesWorkerBackendConfig

_READY_POLL_INTERVAL_SECONDS = 1.0
_DELETE_POLL_INTERVAL_SECONDS = 0.2
_HOSTNAME_ENV = "HOSTNAME"

ANNOTATION_CREATED_AT = "mindroom.ai/created-at"
ANNOTATION_LAST_USED_AT = "mindroom.ai/last-used-at"
ANNOTATION_LAST_STARTED_AT = "mindroom.ai/last-started-at"
ANNOTATION_STARTUP_COUNT = "mindroom.ai/startup-count"
ANNOTATION_FAILURE_COUNT = "mindroom.ai/failure-count"
ANNOTATION_FAILURE_REASON = "mindroom.ai/failure-reason"
ANNOTATION_WORKER_KEY = "mindroom.ai/worker-key"
ANNOTATION_WORKER_STATUS = "mindroom.ai/worker-status"
ANNOTATION_STATE_SUBPATH = "mindroom.ai/state-subpath"
_ANNOTATION_STARTUP_MANIFEST_HASH = "mindroom.ai/startup-manifest-hash"
_ANNOTATION_RUNNER_TOKEN_HASH = "mindroom.ai/runner-token-hash"  # noqa: S105
_ANNOTATION_CREDENTIALS_ENCRYPTION_KEY_HASH = "mindroom.ai/credentials-encryption-key-hash"
_ANNOTATION_TEMPLATE_HASH = "mindroom.ai/template-hash"
_ANNOTATION_PRIVATE_AGENT_NAMES = "mindroom.ai/private-agent-names"

_LABEL_COMPONENT = "mindroom.ai/component"
_LABEL_COMPONENT_VALUE = "worker"
_LABEL_MANAGED_BY = "app.kubernetes.io/managed-by"
_LABEL_MANAGED_BY_VALUE = "mindroom"
_LABEL_NAME = "app.kubernetes.io/name"
_LABEL_NAME_VALUE = "mindroom-worker"
_LABEL_WORKER_ID = "mindroom.ai/worker-id"
# Agent Vault per-worker egress: an init container in the worker pod mints the
# worker's proxy-role token into a shared in-pod volume; the sandbox runner
# composes http://<token>:<vault>@<proxy host> for python/shell. No separate bridge pod.
_AGENT_VAULT_TOKEN_MOUNT_DIR = "/agent-vault"  # noqa: S105
_AGENT_VAULT_TOKEN_FILE = "token"  # noqa: S105
_AGENT_VAULT_TOKEN_PATH = f"{_AGENT_VAULT_TOKEN_MOUNT_DIR}/{_AGENT_VAULT_TOKEN_FILE}"
_AGENT_VAULT_BOOTSTRAP_MOUNT_PATH = "/agent-vault-bootstrap"
_AGENT_VAULT_OWNER_PASSWORD_SECRET_KEY = "AGENT_VAULT_OWNER_PASSWORD"  # noqa: S105
_AGENT_VAULT_WORKER_CA_MOUNT_DIR = "/etc/agent-vault"
_AGENT_VAULT_WORKER_CA_FILE = "ca.pem"
_AGENT_VAULT_WORKER_CA_PATH = f"{_AGENT_VAULT_WORKER_CA_MOUNT_DIR}/{_AGENT_VAULT_WORKER_CA_FILE}"
# Worker pod env consumed by the sandbox runner to compose python/shell proxy env.
_WORKER_EGRESS_PROXY_URL_ENV = WORKER_EGRESS_PROXY_ENV_BY_KEY["proxy_url"]
_WORKER_EGRESS_PROXY_TOKEN_FILE_ENV = WORKER_EGRESS_PROXY_ENV_BY_KEY["token_file"]
_WORKER_EGRESS_PROXY_VAULT_ENV = WORKER_EGRESS_PROXY_ENV_BY_KEY["vault"]
_WORKER_EGRESS_PROXY_CA_FILE_ENV = WORKER_EGRESS_PROXY_ENV_BY_KEY["ca_file"]
# HOME is kept on the init container's own ephemeral filesystem (not the shared
# token volume) so the owner CLI session never lands on a volume the
# agent-executing container can read. Only the minted proxy token is written to
# the shared volume.
_AGENT_VAULT_MINT_SCRIPT = """\
set -eu
export HOME=/tmp/agent-vault-mint-home
mkdir -p "$HOME"
deadline=$(($(date +%s) + 180))
until wget -q -O /dev/null "$AGENT_VAULT_API_URL/health"; do
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "Agent Vault did not become ready at $AGENT_VAULT_API_URL" >&2
    exit 1
  fi
  sleep 2
done
agent-vault auth login \\
  --address "$AGENT_VAULT_API_URL" \\
  --email "$AGENT_VAULT_OWNER_EMAIL" \\
  --password-stdin < "{bootstrap_path}/{owner_password_key}" > /dev/null
agent-vault vault create "$AGENT_VAULT_VAULT" > /dev/null 2>&1 || true
agent-vault owner vault join "$AGENT_VAULT_VAULT" > /dev/null 2>&1 || true
if ! agent-vault agent create "$AGENT_VAULT_VAULT" \\
  --vault "$AGENT_VAULT_VAULT:proxy" \\
  --token-only > "{token_path}"; then
  agent-vault agent rotate "$AGENT_VAULT_VAULT" --token-only > "{token_path}"
fi
if ! agent-vault vault agent add "$AGENT_VAULT_VAULT" \\
  --vault "$AGENT_VAULT_VAULT" \\
  --role proxy > /dev/null; then
  agent-vault vault agent set-role "$AGENT_VAULT_VAULT" \\
    --vault "$AGENT_VAULT_VAULT" \\
    --role proxy > /dev/null
fi
test -s "{token_path}"
"""

_CONTAINER_NAME = SANDBOX_RUNNER_CONTAINER_NAME
_KUBERNETES_STORAGE_SUBPATH_PREFIX_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["storage_subpath_prefix"]
_DEFAULT_CONTAINER_PATH = "/app/.venv/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin"
_WORKER_TOKEN_PURPOSE = b"mindroom-kubernetes-worker-token-v1"
_CREDENTIALS_ENCRYPTION_KEY_SECRET_SUFFIX = "credentials-encryption-key"  # noqa: S105


@dataclass(frozen=True, slots=True)
class DeploymentApplyResult:
    """Describe whether applying one worker Deployment forced a recreate."""

    recreated: bool


class _ApiStatusError(Exception):
    status: int


class _KubernetesMetadata(Protocol):
    name: str
    annotations: dict[str, str] | None
    labels: dict[str, str]
    generation: int | None
    uid: str | None


class _KubernetesDeploymentSpec(Protocol):
    replicas: int | None


class _KubernetesDeploymentStatus(Protocol):
    ready_replicas: int | None
    observed_generation: int | None


class KubernetesDeployment(Protocol):
    """Minimal Deployment surface used by the backend."""

    metadata: _KubernetesMetadata
    spec: _KubernetesDeploymentSpec
    status: _KubernetesDeploymentStatus


class _KubernetesPodSpec(Protocol):
    node_name: str | None


class _KubernetesPod(Protocol):
    spec: _KubernetesPodSpec


class _KubernetesDeploymentList(Protocol):
    items: list[KubernetesDeployment] | None


class _AppsApiProtocol(Protocol):
    def read_namespaced_deployment(self, name: str, namespace: str) -> KubernetesDeployment: ...

    def create_namespaced_deployment(self, namespace: str, body: dict[str, object]) -> KubernetesDeployment: ...

    def patch_namespaced_deployment(
        self,
        name: str,
        namespace: str,
        body: dict[str, object],
    ) -> KubernetesDeployment: ...

    def delete_namespaced_deployment(self, name: str, namespace: str) -> None: ...

    def list_namespaced_deployment(self, namespace: str, label_selector: str) -> _KubernetesDeploymentList: ...


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


def lifecycle_from_annotations(annotations: dict[str, str], *, now: float) -> WorkerLifecycleState:
    """Project the lifecycle state persisted on a worker Deployment's annotations.

    Lets the Kubernetes backend reuse the shared worker-lifecycle transitions
    instead of mutating status annotations inline.
    """
    last_used_at = parse_annotation_float(annotations, ANNOTATION_LAST_USED_AT, now)
    created_at = parse_annotation_float(annotations, ANNOTATION_CREATED_AT, last_used_at)
    last_started_raw = annotations.get(ANNOTATION_LAST_STARTED_AT)
    return WorkerLifecycleState(
        created_at=created_at,
        last_used_at=last_used_at,
        status=cast("WorkerStatus", annotations.get(ANNOTATION_WORKER_STATUS, "starting")),
        last_started_at=float(last_started_raw) if last_started_raw else None,
        startup_count=parse_annotation_int(annotations, ANNOTATION_STARTUP_COUNT),
        failure_count=parse_annotation_int(annotations, ANNOTATION_FAILURE_COUNT),
        failure_reason=annotations.get(ANNOTATION_FAILURE_REASON) or None,
    )


def apply_lifecycle_annotations(annotations: dict[str, str], state: WorkerLifecycleState) -> None:
    """Write one lifecycle state onto a Deployment's annotations dict in place.

    The failure reason is blanked rather than dropped so the merge-based
    deployment patch clears it (it is read back as ``None``).
    """
    annotations[ANNOTATION_CREATED_AT] = str(state.created_at)
    annotations[ANNOTATION_LAST_USED_AT] = str(state.last_used_at)
    annotations[ANNOTATION_WORKER_STATUS] = state.status
    if state.last_started_at is not None:
        annotations[ANNOTATION_LAST_STARTED_AT] = str(state.last_started_at)
    annotations[ANNOTATION_STARTUP_COUNT] = str(state.startup_count)
    annotations[ANNOTATION_FAILURE_COUNT] = str(state.failure_count)
    annotations[ANNOTATION_FAILURE_REASON] = state.failure_reason or ""


def _template_hash(template: dict[str, object]) -> str:
    """Return a stable hash for one Deployment pod template."""
    payload = json.dumps(template, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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


def _labels(*, extra_labels: dict[str, str], worker_id: str) -> dict[str, str]:
    labels = {
        _LABEL_COMPONENT: _LABEL_COMPONENT_VALUE,
        _LABEL_MANAGED_BY: _LABEL_MANAGED_BY_VALUE,
        _LABEL_NAME: _LABEL_NAME_VALUE,
    }
    labels.update(extra_labels)
    labels[_LABEL_WORKER_ID] = worker_id
    return labels


def _list_selector(*, extra_labels: dict[str, str]) -> str:
    selector = {
        _LABEL_COMPONENT: _LABEL_COMPONENT_VALUE,
        _LABEL_MANAGED_BY: _LABEL_MANAGED_BY_VALUE,
        _LABEL_NAME: _LABEL_NAME_VALUE,
    }
    selector.update(extra_labels)
    return ",".join(f"{key}={value}" for key, value in sorted(selector.items()))


def _resolved_agent_policies_for_runtime_paths(runtime_paths: RuntimePaths) -> dict[str, ResolvedAgentPolicy]:
    try:
        raw_config = runtime_paths.config_path.read_text(encoding="utf-8")
    except OSError:
        return {}

    try:
        config_data = yaml.safe_load(raw_config) or {}
    except yaml.YAMLError as exc:
        msg = f"Failed to parse Kubernetes worker config for scoped storage planning: {exc}"
        raise WorkerBackendError(msg) from exc
    if not isinstance(config_data, dict):
        return {}
    return resolved_agent_policies_from_config_data(cast("dict[str, object]", config_data))


class KubernetesResourceManager:
    """Own Kubernetes API access, manifest construction, and cached cluster metadata."""

    def __init__(
        self,
        *,
        runtime_paths: RuntimePaths,
        config: KubernetesWorkerBackendConfig,
        auth_token: str | None,
        storage_root: Path,
        tool_validation_snapshot: dict[str, dict[str, object]],
        worker_grantable_credentials: frozenset[str],
    ) -> None:
        """Initialize one resource manager for a concrete backend configuration."""
        self.runtime_paths = runtime_paths
        self.config = config
        self.auth_token = auth_token
        self.storage_root = storage_root.expanduser().resolve()
        self.tool_validation_snapshot = tool_validation_snapshot
        self.worker_grantable_credentials = worker_grantable_credentials
        self.resolved_agent_policies = _resolved_agent_policies_for_runtime_paths(runtime_paths)
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

    def list_deployments(self) -> list[KubernetesDeployment]:
        """List managed worker Deployments in this namespace."""
        response = self._apps.list_namespaced_deployment(
            self.config.namespace,
            label_selector=_list_selector(extra_labels=self.config.extra_labels),
        )
        return list(response.items or [])

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
        private_agent_names: frozenset[str] | None = None,
    ) -> DeploymentApplyResult:
        """Create-or-patch one worker Deployment."""
        manifest = self._deployment_manifest(
            worker_key=worker_key,
            worker_id=worker_id,
            state_subpath=state_subpath,
            annotations=annotations,
            replicas=replicas,
            private_agent_names=private_agent_names,
        )
        existing = self.read_deployment(worker_id)
        if existing is not None:
            existing_annotations = existing.metadata.annotations or {}
            desired_metadata = cast("dict[str, object]", manifest.get("metadata", {}))
            desired_annotations = cast("dict[str, str]", desired_metadata.get("annotations", {}))
            if existing_annotations.get(_ANNOTATION_TEMPLATE_HASH) != desired_annotations[_ANNOTATION_TEMPLATE_HASH]:
                self._recreate_deployment(worker_id, manifest, timeout_seconds=self.config.ready_timeout_seconds)
                return DeploymentApplyResult(recreated=True)
        self._apply_object(
            read_fn=self._apps.read_namespaced_deployment,
            create_fn=self._apps.create_namespaced_deployment,
            patch_fn=self._apps.patch_namespaced_deployment,
            resource_name=worker_id,
            manifest=manifest,
        )
        return DeploymentApplyResult(recreated=False)

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

    def delete_deployment(self, deployment_name: str) -> None:
        """Delete one worker Deployment, ignoring 404s."""
        self._delete_object(self._apps.delete_namespaced_deployment, deployment_name)

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
                            _worker_credentials_encryption_key_secret_key(secret_name): None,
                        },
                    },
                )
            except self._api_exception as exc:
                if exc.status != 404:
                    raise
            return
        self._delete_object(self._core.delete_namespaced_secret, secret_name)

    def agent_vault_vault_name(self, worker_key: str) -> str | None:
        """Return the Agent Vault vault name backing one worker, or None when disabled."""
        cfg = self.config.agent_vault
        if cfg is None:
            return None
        return worker_id_for_key(worker_key, prefix=cfg.vault_name_prefix)

    def _agent_vault_init_container(self, *, worker_key: str) -> dict[str, object]:
        cfg: KubernetesAgentVaultConfig | None = self.config.agent_vault
        vault = self.agent_vault_vault_name(worker_key)
        if cfg is None or vault is None:
            msg = "Agent Vault init container requested without Agent Vault config."
            raise WorkerBackendError(msg)
        script = _AGENT_VAULT_MINT_SCRIPT.format(
            bootstrap_path=_AGENT_VAULT_BOOTSTRAP_MOUNT_PATH,
            owner_password_key=_AGENT_VAULT_OWNER_PASSWORD_SECRET_KEY,
            token_path=_AGENT_VAULT_TOKEN_PATH,
        )
        return {
            "name": AGENT_VAULT_MINT_CONTAINER_NAME,
            "image": cfg.cli_image,
            "imagePullPolicy": self.config.image_pull_policy,
            "command": ["sh", "-ec", script],
            "env": [
                {"name": "AGENT_VAULT_API_URL", "value": cfg.api_url},
                {"name": "AGENT_VAULT_OWNER_EMAIL", "value": cfg.owner_email},
                {"name": "AGENT_VAULT_VAULT", "value": vault},
            ],
            "volumeMounts": [
                {"name": AGENT_VAULT_TOKEN_VOLUME_NAME, "mountPath": _AGENT_VAULT_TOKEN_MOUNT_DIR},
                {
                    "name": AGENT_VAULT_BOOTSTRAP_VOLUME_NAME,
                    "mountPath": _AGENT_VAULT_BOOTSTRAP_MOUNT_PATH,
                    "readOnly": True,
                },
            ],
            "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
        }

    def _agent_vault_main_env(self, *, worker_key: str) -> list[dict[str, object]]:
        cfg = self.config.agent_vault
        if cfg is None:
            return []
        vault = self.agent_vault_vault_name(worker_key)
        if vault is None:
            msg = f"Agent Vault main env requested without a worker vault name for worker_key={worker_key!r}."
            raise WorkerBackendError(msg)
        env: list[dict[str, object]] = [
            {"name": _WORKER_EGRESS_PROXY_URL_ENV, "value": cfg.proxy_url},
            {"name": _WORKER_EGRESS_PROXY_TOKEN_FILE_ENV, "value": _AGENT_VAULT_TOKEN_PATH},
            {"name": _WORKER_EGRESS_PROXY_VAULT_ENV, "value": vault},
        ]
        if cfg.worker_ca_configmap_name is not None:
            env.append({"name": _WORKER_EGRESS_PROXY_CA_FILE_ENV, "value": _AGENT_VAULT_WORKER_CA_PATH})
        return env

    def _agent_vault_volumes(self) -> list[dict[str, object]]:
        cfg = self.config.agent_vault
        if cfg is None:
            return []
        return [
            {"name": AGENT_VAULT_TOKEN_VOLUME_NAME, "emptyDir": {}},
            {"name": AGENT_VAULT_BOOTSTRAP_VOLUME_NAME, "secret": {"secretName": cfg.bootstrap_secret_name}},
        ]

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
        """Replace one Deployment when pod-template drift requires a full recreate."""
        self.delete_deployment(deployment_name)
        self._wait_for_deployment_absent(deployment_name, timeout_seconds=timeout_seconds)
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
        on_poll_tick: Callable[[float], None] | None = None,
    ) -> KubernetesDeployment:
        """Poll a worker Deployment until it becomes ready or times out."""
        started_at = time.monotonic()
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
                on_poll_tick(time.monotonic() - started_at)
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
            msg = "The 'kubernetes' package is required for the Kubernetes worker backend."
            raise WorkerBackendError(msg) from exc

        try:
            kubernetes_config.load_incluster_config()
        except Exception:
            kubernetes_config.load_kube_config()

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
        credentials_encryption_key = self._credentials_encryption_key()
        return {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": metadata,
            "type": "Opaque",
            "stringData": self._worker_auth_secret_string_data(
                worker_token=worker_token,
                credentials_encryption_key=credentials_encryption_key,
            ),
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

    def _worker_auth_secret_string_data(
        self,
        *,
        worker_token: str,
        credentials_encryption_key: str | None,
    ) -> dict[str, str]:
        string_data = {SANDBOX_RUNTIME_ENV_BY_KEY["proxy_token"]: worker_token}
        if credentials_encryption_key is not None:
            string_data[CREDENTIALS_ENCRYPTION_KEY_ENV] = credentials_encryption_key
        return string_data

    def _worker_auth_secret_data(self, *, worker_token: str) -> dict[str, str | None]:
        credentials_encryption_key = self._credentials_encryption_key()
        secret_data: dict[str, str | None] = {
            name: _secret_data_value(value)
            for name, value in self._worker_auth_secret_string_data(
                worker_token=worker_token,
                credentials_encryption_key=credentials_encryption_key,
            ).items()
        }
        if credentials_encryption_key is None:
            secret_data[CREDENTIALS_ENCRYPTION_KEY_ENV] = None
        return secret_data

    def _shared_auth_secret_data(self, *, worker_id: str, worker_token: str) -> dict[str, str | None]:
        secret_data: dict[str, str | None] = {worker_id: _secret_data_value(worker_token)}
        credentials_encryption_key = self._credentials_encryption_key()
        encryption_key_secret_key = _worker_credentials_encryption_key_secret_key(worker_id)
        if credentials_encryption_key is not None:
            secret_data[encryption_key_secret_key] = _secret_data_value(credentials_encryption_key)
        else:
            secret_data[encryption_key_secret_key] = None
        return secret_data

    def deployment_template_drifted(
        self,
        deployment: KubernetesDeployment,
        *,
        worker_key: str,
        worker_id: str,
        state_subpath: str,
        private_agent_names: frozenset[str] | None,
    ) -> bool:
        """Return whether one Deployment's recorded pod template differs from current config."""
        startup_manifest_path, startup_manifest_hash = self._startup_manifest_path_and_hash(
            worker_key=worker_key,
            dedicated_root=Path(f"{self.config.storage_mount_path}/{state_subpath}".rstrip("/")),
        )
        template = self._pod_template(
            worker_key=worker_key,
            worker_id=worker_id,
            state_subpath=state_subpath,
            startup_manifest_path=startup_manifest_path,
            startup_manifest_hash=startup_manifest_hash,
            private_agent_names=private_agent_names,
        )
        recorded_hash = dict(deployment.metadata.annotations or {}).get(_ANNOTATION_TEMPLATE_HASH)
        return recorded_hash != _template_hash(template)

    def _pod_template(
        self,
        *,
        worker_key: str,
        worker_id: str,
        state_subpath: str,
        startup_manifest_path: str,
        startup_manifest_hash: str,
        private_agent_names: frozenset[str] | None,
    ) -> dict[str, object]:
        worker_labels = _labels(extra_labels=self.config.extra_labels, worker_id=worker_id)
        template_annotations = dict(self.config.extra_annotations)
        template_annotations[ANNOTATION_WORKER_KEY] = worker_key
        template_annotations[_ANNOTATION_STARTUP_MANIFEST_HASH] = startup_manifest_hash
        token_hash = _worker_auth_token_hash(self.auth_token, worker_key)
        if token_hash is None:
            msg = "A worker auth token is required for Kubernetes workers."
            raise WorkerBackendError(msg)
        template_annotations[_ANNOTATION_RUNNER_TOKEN_HASH] = token_hash
        credentials_key_hash = credentials_encryption_key_hash(self._credentials_encryption_key())
        if credentials_key_hash is not None:
            template_annotations[_ANNOTATION_CREDENTIALS_ENCRYPTION_KEY_HASH] = credentials_key_hash
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
                    "command": ["/app/run-sandbox-runner.sh"],
                    "ports": [{"containerPort": self.config.worker_port, "name": "api"}],
                    "env": self._worker_env(
                        worker_key=worker_key,
                        worker_id=worker_id,
                        state_subpath=state_subpath,
                        startup_manifest_path=startup_manifest_path,
                    ),
                    "volumeMounts": self._volume_mounts(worker_key, state_subpath, private_agent_names),
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
                        "requests": dict(self.config.resource_requests),
                        "limits": dict(self.config.resource_limits),
                    },
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "capabilities": {"drop": ["ALL"]},
                    },
                },
            ],
            "volumes": self._volumes(),
        }
        containers = cast("list[dict[str, object]]", template_spec["containers"])
        containers.extend(dict(container) for container in self.config.extra_containers)
        if self.config.agent_vault is not None:
            template_spec["initContainers"] = [self._agent_vault_init_container(worker_key=worker_key)]
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
        private_agent_names: frozenset[str] | None = None,
    ) -> dict[str, object]:
        worker_labels = _labels(extra_labels=self.config.extra_labels, worker_id=worker_id)
        startup_manifest_path, startup_manifest_hash = self._write_startup_manifest(
            worker_key=worker_key,
            dedicated_root=Path(f"{self.config.storage_mount_path}/{state_subpath}".rstrip("/")),
            local_dedicated_root=(self.storage_root / state_subpath).resolve(),
        )
        template = self._pod_template(
            worker_key=worker_key,
            worker_id=worker_id,
            state_subpath=state_subpath,
            startup_manifest_path=startup_manifest_path,
            startup_manifest_hash=startup_manifest_hash,
            private_agent_names=private_agent_names,
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
        desired_annotations[_ANNOTATION_TEMPLATE_HASH] = _template_hash(template)
        if private_agent_names is not None:
            desired_annotations[_ANNOTATION_PRIVATE_AGENT_NAMES] = json.dumps(
                sorted(private_agent_names),
                separators=(",", ":"),
            )
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
        startup_manifest_path: str,
    ) -> list[dict[str, object]]:
        dedicated_root = f"{self.config.storage_mount_path}/{state_subpath}".rstrip("/")
        venv_path = f"{dedicated_root}/venv"
        env: list[dict[str, object]] = [
            {"name": SANDBOX_RUNTIME_ENV_BY_KEY["runner_mode"], "value": "true"},
            {"name": SANDBOX_RUNTIME_ENV_BY_KEY["runner_execution_mode"], "value": "subprocess"},
            {"name": SANDBOX_RUNTIME_ENV_BY_KEY["runner_port"], "value": str(self.config.worker_port)},
            {
                "name": SANDBOX_STARTUP_MANIFEST_PATH_ENV,
                "value": startup_manifest_path,
            },
            {"name": "MINDROOM_CONFIG_PATH", "value": self.config.config_path},
            {"name": "MINDROOM_STORAGE_PATH", "value": dedicated_root},
            {"name": "VIRTUAL_ENV", "value": venv_path},
            {"name": "PATH", "value": f"{venv_path}/bin:{_DEFAULT_CONTAINER_PATH}"},
            {
                "name": SHARED_CREDENTIALS_PATH_ENV,
                "value": f"{dedicated_root}/.shared_credentials",
            },
            {"name": SANDBOX_RUNTIME_ENV_BY_KEY["dedicated_worker_key"], "value": worker_key},
            {"name": SANDBOX_RUNTIME_ENV_BY_KEY["dedicated_worker_root"], "value": dedicated_root},
            {"name": "HOME", "value": dedicated_root},
            self._worker_token_env(worker_id=worker_id),
        ]
        credentials_encryption_key_env = self._worker_credentials_encryption_key_env(worker_id=worker_id)
        if credentials_encryption_key_env is not None:
            env.append(credentials_encryption_key_env)

        env.extend(self._agent_vault_main_env(worker_key=worker_key))

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

    def _worker_credentials_encryption_key_env(self, *, worker_id: str) -> dict[str, object] | None:
        if self._credentials_encryption_key() is None:
            return None
        return {
            "name": CREDENTIALS_ENCRYPTION_KEY_ENV,
            "valueFrom": {
                "secretKeyRef": {
                    "name": self.config.auth_secret_name or worker_id,
                    "key": (
                        _worker_credentials_encryption_key_secret_key(worker_id)
                        if self.config.auth_secret_name is not None
                        else CREDENTIALS_ENCRYPTION_KEY_ENV
                    ),
                },
            },
        }

    def _credentials_encryption_key(self) -> str | None:
        return credentials_encryption_key_value(self.runtime_paths.env_value(CREDENTIALS_ENCRYPTION_KEY_ENV))

    def _worker_auth_token(self, worker_key: str) -> str:
        worker_token = worker_auth_token(self.auth_token, worker_key)
        if worker_token is None:
            msg = "A worker auth token is required for Kubernetes workers."
            raise WorkerBackendError(msg)
        return worker_token

    def _startup_manifest_path_and_hash(self, *, worker_key: str, dedicated_root: Path) -> tuple[str, str]:
        """Return the worker-visible startup manifest path and content hash without writing."""
        startup_runtime_paths = self._worker_runtime_paths(
            worker_key=worker_key,
            dedicated_root=dedicated_root,
        )
        return (
            str(constants.sandbox_startup_manifest_path(dedicated_root)),
            constants.startup_manifest_sha256(
                startup_runtime_paths,
                tool_validation_snapshot=self.tool_validation_snapshot,
                public_runtime=True,
            ),
        )

    def _write_startup_manifest(
        self,
        *,
        worker_key: str,
        dedicated_root: Path,
        local_dedicated_root: Path,
    ) -> tuple[str, str]:
        constants.write_startup_manifest(
            local_dedicated_root,
            self._worker_runtime_paths(worker_key=worker_key, dedicated_root=dedicated_root),
            tool_validation_snapshot=self.tool_validation_snapshot,
            public_runtime=True,
        )
        return self._startup_manifest_path_and_hash(worker_key=worker_key, dedicated_root=dedicated_root)

    def _worker_runtime_paths(
        self,
        *,
        worker_key: str,
        dedicated_root: Path,
    ) -> RuntimePaths:
        config_path = (
            Path(self.config.config_path)
            if self.config.config_map_name is not None
            else self.runtime_paths.config_path.expanduser().resolve()
        )
        process_env = {
            key: value
            for key, value in self.runtime_paths.process_env.items()
            if not is_kubernetes_worker_backend_config_env_name(key)
        }
        env_file_values = {
            key: value
            for key, value in self.runtime_paths.env_file_values.items()
            if not is_kubernetes_worker_backend_config_env_name(key)
        }
        process_env.update(
            {
                SANDBOX_RUNTIME_ENV_BY_KEY["runner_mode"]: "true",
                SANDBOX_RUNTIME_ENV_BY_KEY["runner_execution_mode"]: "subprocess",
                SANDBOX_RUNTIME_ENV_BY_KEY["runner_port"]: str(self.config.worker_port),
                "MINDROOM_CONFIG_PATH": str(config_path),
                "MINDROOM_STORAGE_PATH": str(dedicated_root),
                _KUBERNETES_STORAGE_SUBPATH_PREFIX_ENV: self.config.storage_subpath_prefix,
                SHARED_CREDENTIALS_PATH_ENV: f"{dedicated_root}/.shared_credentials",
                SANDBOX_RUNTIME_ENV_BY_KEY["dedicated_worker_key"]: worker_key,
                SANDBOX_RUNTIME_ENV_BY_KEY["dedicated_worker_root"]: str(dedicated_root),
            },
        )
        process_env.update(
            worker_extra_env(self.config.extra_env),
        )
        process_env.update(VENDOR_TELEMETRY_ENV_VALUES)
        return constants.isolated_runtime_paths(
            RuntimePaths(
                config_path=config_path,
                config_dir=config_path.parent,
                env_path=config_path.parent / ".env",
                storage_root=dedicated_root.resolve(),
                process_env=MappingProxyType(process_env),
                env_file_values=MappingProxyType(env_file_values),
            ),
        )

    def _volume_mounts(
        self,
        worker_key: str,
        state_subpath: str,
        private_agent_names: frozenset[str] | None,
    ) -> list[dict[str, object]]:
        mounts = self._scoped_storage_mounts(
            worker_key,
            state_subpath,
            private_agent_names=private_agent_names,
        )
        if self.config.config_map_name is None:
            mounts.extend(self._file_config_storage_mounts())
        if self.config.config_map_name is not None:
            mounts.append(
                {
                    "name": WORKER_CONFIG_VOLUME_NAME,
                    "mountPath": self.config.config_path,
                    "subPath": self.config.config_key,
                    "readOnly": True,
                },
            )
        if self.config.agent_vault is not None:
            mounts.append(
                {
                    "name": AGENT_VAULT_TOKEN_VOLUME_NAME,
                    "mountPath": _AGENT_VAULT_TOKEN_MOUNT_DIR,
                    "readOnly": True,
                },
            )
        if self._agent_vault_worker_ca_configmap_name() is not None:
            mounts.append(
                {
                    "name": AGENT_VAULT_CA_VOLUME_NAME,
                    "mountPath": _AGENT_VAULT_WORKER_CA_MOUNT_DIR,
                    "readOnly": True,
                },
            )
        return mounts

    def _agent_vault_worker_ca_configmap_name(self) -> str | None:
        cfg = self.config.agent_vault
        if cfg is None:
            return None
        return cfg.worker_ca_configmap_name

    def _file_config_storage_mounts(self) -> list[dict[str, object]]:
        storage_root = PurePosixPath(posixpath.normpath(self.config.storage_mount_path))
        config_path = PurePosixPath(posixpath.normpath(self.config.config_path))
        try:
            relative_config_path = config_path.relative_to(storage_root)
        except ValueError:
            return []
        if not relative_config_path.parts:
            return []

        visible_subpath = PurePosixPath(relative_config_path.parts[0])
        return [
            {
                "name": WORKER_STORAGE_VOLUME_NAME,
                "mountPath": str(storage_root / visible_subpath),
                "subPath": str(visible_subpath),
                "readOnly": True,
            },
        ]

    def _volumes(self) -> list[dict[str, object]]:
        volumes: list[dict[str, object]] = [
            {
                "name": WORKER_STORAGE_VOLUME_NAME,
                "persistentVolumeClaim": {"claimName": self.config.storage_pvc_name},
            },
        ]
        if self.config.config_map_name is not None:
            volumes.append(
                {
                    "name": WORKER_CONFIG_VOLUME_NAME,
                    "configMap": {"name": self.config.config_map_name},
                },
            )
        volumes.extend(self._agent_vault_volumes())
        ca_configmap_name = self._agent_vault_worker_ca_configmap_name()
        if ca_configmap_name is not None:
            volumes.append(
                {
                    "name": AGENT_VAULT_CA_VOLUME_NAME,
                    "configMap": {
                        "name": ca_configmap_name,
                        "items": [
                            {
                                "key": _AGENT_VAULT_WORKER_CA_FILE,
                                "path": _AGENT_VAULT_WORKER_CA_FILE,
                            },
                        ],
                    },
                },
            )
        volumes.extend(dict(volume) for volume in self.config.extra_volumes)
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
    ) -> list[dict[str, object]]:
        mounted_storage_root = Path(self.config.storage_mount_path)
        mounts: list[dict[str, object]] = [
            {
                "name": WORKER_STORAGE_VOLUME_NAME,
                "mountPath": str(planned_root.worker_visible_path),
                "subPath": str(planned_root.worker_visible_path.relative_to(mounted_storage_root)),
            }
            for planned_root in plan_scoped_visible_state_roots(
                worker_key=worker_key,
                local_shared_storage_root=self.storage_root,
                worker_visible_shared_storage_root=mounted_storage_root,
                private_agent_names=private_agent_names,
                allow_unknown_worker_key=False,
                resolved_agent_policies=self.resolved_agent_policies,
            )
        ]
        mounts.append(
            {
                "name": WORKER_STORAGE_VOLUME_NAME,
                "mountPath": f"{self.config.storage_mount_path}/{state_subpath}",
                "subPath": state_subpath,
            },
        )
        validate_unique_worker_visible_paths(
            (str(mount["mountPath"]) for mount in mounts),
            worker_key=worker_key,
            duplicate_label="Kubernetes mountPath",
        )
        return mounts
