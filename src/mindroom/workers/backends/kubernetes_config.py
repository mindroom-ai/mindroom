"""Environment-backed configuration for the Kubernetes worker backend."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import UnionType
from typing import TYPE_CHECKING, Literal, TypedDict, Union, cast, get_args, get_origin

import pydantic

from mindroom import yaml_io
from mindroom.constants import runtime_env_values
from mindroom.matrix.identity import try_parse_historical_matrix_user_id
from mindroom.private_storage.private_instance_identity_store import (
    PrivateInstanceIdentityError,
    reconstruct_private_instance_worker_key,
)
from mindroom.runtime_env_policy import (
    CREDENTIALS_ENCRYPTION_KEY_ENV,
    KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY,
    WORKER_BACKEND_ENV,
    WORKER_COMPUTER_ENABLED_ENV,
    credentials_encryption_key_value,
)
from mindroom.tool_system.worker_routing import resolved_worker_key_scope
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.backends._config_helpers import (
    read_bool_env,
    read_env,
    read_float_env,
    read_int_env,
    read_json_mapping_env,
    read_json_object_list_env,
)
from mindroom.workers.backends._dedicated_worker_common import stable_signature_json
from mindroom.workers.backends._lifecycle import DEFAULT_IDLE_TIMEOUT_SECONDS
from mindroom.workers.backends.kubernetes_pod_names import (
    RESERVED_EXTRA_CONTAINER_NAMES,
    RESERVED_EXTRA_VOLUME_NAMES,
)
from mindroom.workers.models import SCRIPT_RESOURCE_PROFILE_NAMES

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic_core import ErrorDetails

    from mindroom.constants import RuntimePaths


class _WorkerSeccompProfile(TypedDict):
    """Validated Kubernetes wire mapping for the main worker's Localhost profile."""

    type: Literal["Localhost"]
    localhostProfile: str


_DEFAULT_READY_TIMEOUT_SECONDS = 60.0
_DEFAULT_WORKER_PORT = 8766
_DEFAULT_IMAGE_PULL_POLICY = "IfNotPresent"
_DEFAULT_STORAGE_SUBPATH_PREFIX = "workers"
_DEFAULT_STORAGE_MOUNT_PATH = "/app/worker"
_DEFAULT_SERVICE_ACCOUNT_NAME = "default"
_DEFAULT_NAME_PREFIX = "mindroom-worker"
_DEFAULT_MEMORY_REQUEST = "256Mi"
_DEFAULT_MEMORY_LIMIT = "1Gi"
_DEFAULT_CPU_REQUEST = "100m"
_DEFAULT_CPU_LIMIT = "500m"
_DEFAULT_SCRIPT_RESOURCE_PROFILE = "small"
_IN_CLUSTER_TOKEN_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
_IN_CLUSTER_CERT_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")

_NAMESPACE_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["namespace"]
_IMAGE_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["image"]
_IMAGE_PULL_POLICY_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["image_pull_policy"]
_IMAGE_PULL_SECRETS_JSON_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["image_pull_secrets_json"]
_PORT_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["port"]
_SERVICE_ACCOUNT_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["service_account"]
_RUNTIME_CLASS_NAME_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["runtime_class_name"]
_STORAGE_PVC_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["storage_pvc"]
_STORAGE_MOUNT_PATH_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["storage_mount_path"]
_STORAGE_SUBPATH_PREFIX_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["storage_subpath_prefix"]
_IDLE_TIMEOUT_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["idle_timeout"]
_READY_TIMEOUT_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["ready_timeout"]
_NAME_PREFIX_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["name_prefix"]
_NODE_NAME_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["node_name"]
_COLOCATE_WITH_CONTROL_PLANE_NODE_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["colocate_with_control_plane_node"]
_RECONCILE_POD_TEMPLATES_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["reconcile_pod_templates"]
_EXTRA_ENV_JSON_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["extra_env_json"]
_EXTRA_LABELS_JSON_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["extra_labels_json"]
_EXTRA_ANNOTATIONS_JSON_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["extra_annotations_json"]
_EXTRA_CONTAINERS_JSON_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["extra_containers_json"]
_EXTRA_VOLUMES_JSON_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["extra_volumes_json"]
_OWNER_DEPLOYMENT_NAME_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["owner_deployment_name"]
_MEMORY_REQUEST_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["memory_request"]
_MEMORY_LIMIT_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["memory_limit"]
_CPU_REQUEST_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["cpu_request"]
_CPU_LIMIT_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["cpu_limit"]
_TMP_SIZE_LIMIT_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["tmp_size_limit"]
_USER_RESOURCES_JSON_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["user_resources_json"]
_SCRIPT_RESOURCE_PROFILES_JSON_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["script_resource_profiles_json"]
_DEFAULT_SCRIPT_RESOURCE_PROFILE_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["default_script_resource_profile"]
_ENABLE_SERVICE_LINKS_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["enable_service_links"]
_AUTH_SECRET_NAME_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["auth_secret_name"]
_SECCOMP_PROFILE_JSON_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["seccomp_profile_json"]
_POD_NAMESPACE_ENV = "POD_NAMESPACE"
_EXTRA_CONTAINER_ALLOWED_KEYS = frozenset(
    {
        "name",
        "image",
        "imagePullPolicy",
        "command",
        "args",
        "env",
        "envFrom",
        "volumeMounts",
        "resources",
        "securityContext",
    },
)
_EXTRA_VOLUME_SOURCE_KEYS = frozenset({"secret", "configMap", "emptyDir", "projected"})
_EXTRA_VOLUME_ALLOWED_KEYS = frozenset({"name", *_EXTRA_VOLUME_SOURCE_KEYS})
# Where the allowed keys can carry a Kubernetes quantity; "*" is every list item or mapping value,
# and "a|b" names the wire and Python spellings the client accepts for one field.
_EXTRA_CONTAINER_QUANTITY_PATHS = (
    ("resources", "requests", "*"),
    ("resources", "limits", "*"),
    ("env", "*", "valueFrom|value_from", "resourceFieldRef|resource_field_ref", "divisor"),
)
_EXTRA_VOLUME_QUANTITY_PATHS = (
    ("emptyDir", "sizeLimit|size_limit"),
    (
        "projected",
        "sources",
        "*",
        "downwardAPI|downward_api",
        "items",
        "*",
        "resourceFieldRef|resource_field_ref",
        "divisor",
    ),
)
_DNS_SUBDOMAIN_RE = re.compile(
    r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?(?:\.[a-z0-9](?:[-a-z0-9]*[a-z0-9])?)*",
)


def _default_script_resource_profiles() -> dict[str, dict[str, dict[str, str]]]:
    return {
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


def _normalized_script_resource_profiles(value: object) -> dict[str, dict[str, dict[str, str]]]:
    if not isinstance(value, dict) or set(value) != SCRIPT_RESOURCE_PROFILE_NAMES:
        msg = f"{_SCRIPT_RESOURCE_PROFILES_JSON_ENV} must define exactly small, standard, and large."
        raise WorkerBackendError(msg)
    value_mapping = cast("dict[str, object]", value)
    normalized: dict[str, dict[str, dict[str, str]]] = {}
    for profile_name in sorted(SCRIPT_RESOURCE_PROFILE_NAMES):
        profile = value_mapping[profile_name]
        if not isinstance(profile, dict) or set(profile) != {"requests", "limits"}:
            msg = f"{_SCRIPT_RESOURCE_PROFILES_JSON_ENV}.{profile_name} must define requests and limits."
            raise WorkerBackendError(msg)
        profile_mapping = cast("dict[str, object]", profile)
        normalized_profile: dict[str, dict[str, str]] = {}
        for resource_kind in ("requests", "limits"):
            quantities = profile_mapping[resource_kind]
            if (
                not isinstance(quantities, dict)
                or set(quantities) != {"cpu", "memory"}
                or any(not isinstance(quantity, str) or not quantity.strip() for quantity in quantities.values())
            ):
                msg = (
                    f"{_SCRIPT_RESOURCE_PROFILES_JSON_ENV}.{profile_name}.{resource_kind} "
                    "must define non-empty cpu and memory quantities."
                )
                raise WorkerBackendError(msg)
            normalized_profile[resource_kind] = dict(cast("dict[str, str]", quantities))
        normalized[profile_name] = normalized_profile
    return normalized


def _read_script_resource_profiles_env(env: Mapping[str, str]) -> dict[str, dict[str, dict[str, str]]]:
    raw = read_env(env, _SCRIPT_RESOURCE_PROFILES_JSON_ENV)
    if not raw:
        return _default_script_resource_profiles()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = f"{_SCRIPT_RESOURCE_PROFILES_JSON_ENV} must contain a JSON object."
        raise WorkerBackendError(msg) from exc
    return _normalized_script_resource_profiles(parsed)


def _normalized_user_resources(value: object) -> dict[str, dict[str, dict[str, str]]]:
    if not isinstance(value, dict):
        msg = f"{_USER_RESOURCES_JSON_ENV} must contain a JSON object."
        raise WorkerBackendError(msg)
    normalized: dict[str, dict[str, dict[str, str]]] = {}
    for user_id, user_resources in cast("dict[str, object]", value).items():
        if try_parse_historical_matrix_user_id(user_id) is None:
            msg = f"{_USER_RESOURCES_JSON_ENV} keys must be Matrix user IDs such as @alice:example.org."
            raise WorkerBackendError(msg)
        if (
            not isinstance(user_resources, dict)
            or not user_resources
            or not set(user_resources) <= {"requests", "limits"}
        ):
            msg = f"{_USER_RESOURCES_JSON_ENV}.{user_id} must define requests, limits, or both."
            raise WorkerBackendError(msg)
        normalized_resources: dict[str, dict[str, str]] = {}
        for resource_kind, quantities in cast("dict[str, object]", user_resources).items():
            if (
                not isinstance(quantities, dict)
                or not quantities
                or not set(quantities) <= {"cpu", "memory"}
                or any(not isinstance(quantity, str) or not quantity.strip() for quantity in quantities.values())
            ):
                msg = (
                    f"{_USER_RESOURCES_JSON_ENV}.{user_id}.{resource_kind} "
                    "must define non-empty cpu or memory quantities."
                )
                raise WorkerBackendError(msg)
            normalized_resources[resource_kind] = dict(cast("dict[str, str]", quantities))
        normalized[user_id] = normalized_resources
    return normalized


def _read_user_resources_env(env: Mapping[str, str]) -> dict[str, dict[str, dict[str, str]]]:
    raw = read_env(env, _USER_RESOURCES_JSON_ENV)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = f"{_USER_RESOURCES_JSON_ENV} must contain a JSON object."
        raise WorkerBackendError(msg) from exc
    return _normalized_user_resources(parsed)


def _normalized_seccomp_profile(value: object) -> _WorkerSeccompProfile | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"type", "localhostProfile"}:
        msg = f"{_SECCOMP_PROFILE_JSON_ENV} must define exactly type and localhostProfile."
        raise WorkerBackendError(msg)
    profile = cast("dict[str, object]", value)
    profile_type = profile["type"]
    profile_path = profile["localhostProfile"]
    if profile_type != "Localhost":
        msg = f"{_SECCOMP_PROFILE_JSON_ENV}.type must be Localhost."
        raise WorkerBackendError(msg)
    if not isinstance(profile_path, str):
        msg = f"{_SECCOMP_PROFILE_JSON_ENV}.localhostProfile must be a relative path."
        raise WorkerBackendError(msg)
    path_parts = profile_path.split("/")
    if (
        not profile_path
        or profile_path.startswith("/")
        or "\\" in profile_path
        or any(part in {"", ".", ".."} for part in path_parts)
    ):
        msg = f"{_SECCOMP_PROFILE_JSON_ENV}.localhostProfile must be a relative path without traversal segments."
        raise WorkerBackendError(msg)
    return {"type": "Localhost", "localhostProfile": profile_path}


def _read_seccomp_profile_env(env: Mapping[str, str]) -> _WorkerSeccompProfile | None:
    raw = read_env(env, _SECCOMP_PROFILE_JSON_ENV)
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = f"{_SECCOMP_PROFILE_JSON_ENV} must contain a JSON object."
        raise WorkerBackendError(msg) from exc
    return _normalized_seccomp_profile(parsed)


def _validate_required_string(item: dict[str, object], env_name: str, index: int, field_name: str) -> None:
    value = item.get(field_name)
    if not isinstance(value, str) or not value.strip():
        msg = f"{env_name}[{index}].{field_name} must be a non-empty string."
        raise WorkerBackendError(msg)


def _validate_unique_names(
    items: tuple[dict[str, object], ...],
    env_name: str,
    reserved_names: frozenset[str],
) -> None:
    seen_names: set[str] = set()
    for index, item in enumerate(items):
        name = cast("str", item["name"]).strip()
        if name in reserved_names:
            msg = f"{env_name}[{index}].name must not use reserved name: {name}."
            raise WorkerBackendError(msg)
        if name in seen_names:
            msg = f"{env_name}[{index}].name duplicates an earlier item: {name}."
            raise WorkerBackendError(msg)
        seen_names.add(name)


def _with_string_quantity(value: object, path: tuple[str, ...], location: str) -> object:
    """Return ``value`` with the quantity at ``path`` as a string: the API server takes a JSON number, the client not."""
    if not path:
        if isinstance(value, bool):
            msg = f"{location}: quantities must be strings or numbers such as 1, 0.5, or 512Mi."
            raise WorkerBackendError(msg)
        return str(value) if isinstance(value, int | float) else value
    key, rest = path[0], path[1:]
    if key == "*" and isinstance(value, list):
        return [_with_string_quantity(item, rest, f"{location}[{index}]") for index, item in enumerate(value)]
    if key == "*" and isinstance(value, dict):
        # The location stops at the map: its keys are user input and may be pasted credentials.
        return {name: _with_string_quantity(item, rest, location) for name, item in value.items()}
    for name in key.split("|"):
        if isinstance(value, dict) and name in value:
            value = {**value, name: _with_string_quantity(value[name], rest, f"{location}.{name}")}
    return value


def _schema_error(model_class: type, error: ErrorDetails) -> str:
    """Describe one validation error by schema field names and list indices only, never by a user-supplied key."""
    path: list[str] = []
    annotation: object = model_class
    for segment in error["loc"]:
        while get_origin(annotation) in {Union, UnionType}:
            annotation = next(arg for arg in get_args(annotation) if arg is not type(None))
        fields = _field_annotations(annotation)
        if isinstance(segment, int) and get_origin(annotation) is list:
            annotation = get_args(annotation)[0]
        elif segment in fields:
            annotation = fields[segment]
        else:
            # An unknown key or a free-form map key is user input and can be a pasted credential.
            return f"{'.'.join(path) or 'top level'}: invalid value ({error['type']})"
        path.append(str(segment))
    return f"{'.'.join(path)}: {error['msg']}"


def _field_annotations(annotation: object) -> dict[object, object]:
    """Map every name a model accepts for one of its fields, wire or Python, to that field's type."""
    if not (isinstance(annotation, type) and issubclass(annotation, pydantic.BaseModel)):
        return {}
    return {
        name: field.annotation
        for python_name, field in annotation.model_fields.items()
        for name in (python_name, field.alias, *getattr(field.validation_alias, "choices", [field.validation_alias]))
    }


def validated_kubernetes_object(model_name: str, value: dict[str, object], *, what: str) -> dict[str, object]:
    """Return ``value`` in the wire form the installed client sends, naming each field it rejects but no values."""
    try:
        kubernetes_client = importlib.import_module("kubernetes.client")
    except ModuleNotFoundError as exc:
        msg = "The Kubernetes worker backend needs the 'kubernetes' package: install mindroom[kubernetes]."
        raise WorkerBackendError(msg) from exc
    model_class = getattr(kubernetes_client, model_name)
    try:
        model = model_class.model_validate(value)
    except pydantic.ValidationError as exc:
        errors = exc.errors(include_input=False, include_url=False)
    else:
        # Unlike the client's own serialization, this keeps explicit nulls, which clear fields in a patch.
        return model.model_dump(mode="json", by_alias=True, exclude_unset=True)
    # Raised outside the handler, so no chained exception carries the rejected values into logs.
    fields = "; ".join(_schema_error(model_class, error) for error in errors)
    msg = f"{what} is not a valid Kubernetes {model_name.removeprefix('V1')}: {fields}"
    raise WorkerBackendError(msg)


def _canonical_manifests(
    items: tuple[dict[str, object], ...],
    quantity_paths: tuple[tuple[str, ...], ...],
    model_name: str,
    env_name: str,
) -> tuple[dict[str, object], ...]:
    """Return ``items`` as the client's wire form, so typed creates and untyped patches send the same manifest."""
    canonical: list[dict[str, object]] = []
    for index, item in enumerate(items):
        location = f"{env_name}[{index}]"
        for path in quantity_paths:
            item = cast("dict[str, object]", _with_string_quantity(item, path, location))  # noqa: PLW2901
        canonical.append(validated_kubernetes_object(model_name, item, what=location))
    return tuple(canonical)


def _read_extra_containers_env(env: Mapping[str, str]) -> tuple[dict[str, object], ...]:
    containers = read_json_object_list_env(env, _EXTRA_CONTAINERS_JSON_ENV)
    for index, container in enumerate(containers):
        if not set(container) <= _EXTRA_CONTAINER_ALLOWED_KEYS:
            allowed = ", ".join(sorted(_EXTRA_CONTAINER_ALLOWED_KEYS))
            msg = f"{_EXTRA_CONTAINERS_JSON_ENV}[{index}]: unknown field; allowed: {allowed}."
            raise WorkerBackendError(msg)
        _validate_required_string(container, _EXTRA_CONTAINERS_JSON_ENV, index, "name")
        _validate_required_string(container, _EXTRA_CONTAINERS_JSON_ENV, index, "image")
    _validate_unique_names(containers, _EXTRA_CONTAINERS_JSON_ENV, RESERVED_EXTRA_CONTAINER_NAMES)
    return _canonical_manifests(containers, _EXTRA_CONTAINER_QUANTITY_PATHS, "V1Container", _EXTRA_CONTAINERS_JSON_ENV)


def _read_image_pull_secrets_env(env: Mapping[str, str]) -> tuple[str, ...]:
    """Return Secret names from the chart's ``imagePullSecrets`` list of ``{"name": ...}`` objects."""
    names: list[str] = []
    for index, item in enumerate(read_json_object_list_env(env, _IMAGE_PULL_SECRETS_JSON_ENV)):
        if set(item) != {"name"}:
            msg = f"{_IMAGE_PULL_SECRETS_JSON_ENV}[{index}] must contain exactly one key, `name`."
            raise WorkerBackendError(msg)
        _validate_required_string(item, _IMAGE_PULL_SECRETS_JSON_ENV, index, "name")
        names.append(cast("str", item["name"]).strip())
    return tuple(names)


def _read_extra_volumes_env(env: Mapping[str, str]) -> tuple[dict[str, object], ...]:
    volumes = read_json_object_list_env(env, _EXTRA_VOLUMES_JSON_ENV)
    for index, volume in enumerate(volumes):
        if not set(volume) <= _EXTRA_VOLUME_ALLOWED_KEYS:
            allowed = ", ".join(sorted(_EXTRA_VOLUME_ALLOWED_KEYS))
            msg = f"{_EXTRA_VOLUMES_JSON_ENV}[{index}]: unknown field; allowed: {allowed}."
            raise WorkerBackendError(msg)
        _validate_required_string(volume, _EXTRA_VOLUMES_JSON_ENV, index, "name")
        source_keys = [key for key in _EXTRA_VOLUME_SOURCE_KEYS if key in volume]
        if len(source_keys) != 1:
            msg = f"{_EXTRA_VOLUMES_JSON_ENV}[{index}] must set exactly one of secret, configMap, emptyDir, projected."
            raise WorkerBackendError(msg)
    _validate_unique_names(volumes, _EXTRA_VOLUMES_JSON_ENV, RESERVED_EXTRA_VOLUME_NAMES)
    return _canonical_manifests(volumes, _EXTRA_VOLUME_QUANTITY_PATHS, "V1Volume", _EXTRA_VOLUMES_JSON_ENV)


@dataclass(frozen=True, slots=True)
class KubernetesWorkerBackendConfig:
    """Resolved environment-backed configuration for the Kubernetes provider."""

    namespace: str
    image: str
    image_pull_policy: str
    worker_port: int
    service_account_name: str
    storage_pvc_name: str
    storage_mount_path: str
    storage_subpath_prefix: str
    idle_timeout_seconds: float
    ready_timeout_seconds: float
    name_prefix: str
    node_name: str | None
    colocate_with_control_plane_node: bool
    extra_env: dict[str, str]
    extra_labels: dict[str, str]
    extra_annotations: dict[str, str]
    owner_deployment_name: str | None
    resource_requests: dict[str, str]
    resource_limits: dict[str, str]
    enable_service_links: bool
    auth_secret_name: str | None
    seccomp_profile: _WorkerSeccompProfile | None = None
    script_resource_profiles: dict[str, dict[str, dict[str, str]]] = field(
        default_factory=_default_script_resource_profiles,
    )
    default_script_resource_profile: str = _DEFAULT_SCRIPT_RESOURCE_PROFILE
    reconcile_pod_templates: bool = True
    extra_containers: tuple[dict[str, object], ...] = ()
    extra_volumes: tuple[dict[str, object], ...] = ()
    runtime_class_name: str | None = None
    tmp_size_limit: str | None = None
    user_resources: dict[str, dict[str, dict[str, str]]] = field(default_factory=dict)
    image_pull_secrets: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Reject storage prefixes that are not strict relative descendants."""
        parts = self.storage_subpath_prefix.split("/")
        if (
            not self.storage_subpath_prefix
            or self.storage_subpath_prefix.startswith("/")
            or any(part in {"", ".", ".."} for part in parts)
        ):
            msg = f"{_STORAGE_SUBPATH_PREFIX_ENV} must be a relative path without traversal segments."
            raise WorkerBackendError(msg)
        normalized_profiles = _normalized_script_resource_profiles(self.script_resource_profiles)
        if self.default_script_resource_profile not in normalized_profiles:
            msg = f"{_DEFAULT_SCRIPT_RESOURCE_PROFILE_ENV} must be one of: small, standard, large."
            raise WorkerBackendError(msg)
        object.__setattr__(self, "script_resource_profiles", normalized_profiles)
        object.__setattr__(self, "user_resources", _normalized_user_resources(self.user_resources))
        object.__setattr__(self, "seccomp_profile", _normalized_seccomp_profile(self.seccomp_profile))
        runtime_class_name = self.runtime_class_name.strip() if self.runtime_class_name is not None else None
        if runtime_class_name and (
            len(runtime_class_name) > 253 or _DNS_SUBDOMAIN_RE.fullmatch(runtime_class_name) is None
        ):
            msg = f"{_RUNTIME_CLASS_NAME_ENV} must be a valid Kubernetes DNS subdomain."
            raise WorkerBackendError(msg)
        object.__setattr__(self, "runtime_class_name", runtime_class_name or None)

    def resources_for_profile(
        self,
        profile_name: str | None,
        *,
        worker_key: str | None = None,
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Return main-worker resources with the key's requester override, or one bounded script profile."""
        if profile_name is None:
            user_resources = self._user_resources_for_worker_key(worker_key) if worker_key is not None else {}
            return (
                {**self.resource_requests, **user_resources.get("requests", {})},
                {**self.resource_limits, **user_resources.get("limits", {})},
            )
        profile = self.script_resource_profiles.get(profile_name)
        if profile is None:
            msg = "Worker resource profile must be one of: small, standard, large."
            raise WorkerBackendError(msg)
        return dict(profile["requests"]), dict(profile["limits"])

    def _user_resources_for_worker_key(self, worker_key: str) -> dict[str, dict[str, str]]:
        """Return the override whose requester owns one user or user-agent worker key."""
        if not self.user_resources or resolved_worker_key_scope(worker_key) not in {"user", "user_agent"}:
            return {}
        for user_id, user_resources in self.user_resources.items():
            try:
                if reconstruct_private_instance_worker_key(worker_key, user_id) == worker_key:
                    return user_resources
            except PrivateInstanceIdentityError:
                return {}
        return {}

    @classmethod
    def from_runtime(cls, runtime_paths: RuntimePaths) -> KubernetesWorkerBackendConfig:
        """Build Kubernetes worker configuration from one explicit runtime context."""
        env = runtime_env_values(runtime_paths)
        namespace = read_env(env, _NAMESPACE_ENV) or read_env(env, _POD_NAMESPACE_ENV) or "default"
        image = read_env(env, _IMAGE_ENV)
        if not image:
            msg = f"{_IMAGE_ENV} must be set when {WORKER_BACKEND_ENV}=kubernetes."
            raise WorkerBackendError(msg)

        storage_pvc_name = read_env(env, _STORAGE_PVC_ENV)
        if not storage_pvc_name:
            msg = f"{_STORAGE_PVC_ENV} must be set when {WORKER_BACKEND_ENV}=kubernetes."
            raise WorkerBackendError(msg)

        resource_requests = {
            "memory": read_env(env, _MEMORY_REQUEST_ENV, _DEFAULT_MEMORY_REQUEST) or _DEFAULT_MEMORY_REQUEST,
            "cpu": read_env(env, _CPU_REQUEST_ENV, _DEFAULT_CPU_REQUEST) or _DEFAULT_CPU_REQUEST,
        }
        resource_limits = {
            "memory": read_env(env, _MEMORY_LIMIT_ENV, _DEFAULT_MEMORY_LIMIT) or _DEFAULT_MEMORY_LIMIT,
            "cpu": read_env(env, _CPU_LIMIT_ENV, _DEFAULT_CPU_LIMIT) or _DEFAULT_CPU_LIMIT,
        }
        return cls(
            namespace=namespace,
            image=image,
            image_pull_policy=read_env(env, _IMAGE_PULL_POLICY_ENV, _DEFAULT_IMAGE_PULL_POLICY)
            or _DEFAULT_IMAGE_PULL_POLICY,
            image_pull_secrets=_read_image_pull_secrets_env(env),
            worker_port=read_int_env(env, _PORT_ENV, _DEFAULT_WORKER_PORT),
            service_account_name=read_env(env, _SERVICE_ACCOUNT_ENV, _DEFAULT_SERVICE_ACCOUNT_NAME)
            or _DEFAULT_SERVICE_ACCOUNT_NAME,
            runtime_class_name=read_env(env, _RUNTIME_CLASS_NAME_ENV) or None,
            storage_pvc_name=storage_pvc_name,
            storage_mount_path=read_env(env, _STORAGE_MOUNT_PATH_ENV, _DEFAULT_STORAGE_MOUNT_PATH)
            or _DEFAULT_STORAGE_MOUNT_PATH,
            storage_subpath_prefix=read_env(env, _STORAGE_SUBPATH_PREFIX_ENV, _DEFAULT_STORAGE_SUBPATH_PREFIX)
            or _DEFAULT_STORAGE_SUBPATH_PREFIX,
            idle_timeout_seconds=read_float_env(env, _IDLE_TIMEOUT_ENV, DEFAULT_IDLE_TIMEOUT_SECONDS),
            ready_timeout_seconds=read_float_env(env, _READY_TIMEOUT_ENV, _DEFAULT_READY_TIMEOUT_SECONDS),
            name_prefix=read_env(env, _NAME_PREFIX_ENV, _DEFAULT_NAME_PREFIX) or _DEFAULT_NAME_PREFIX,
            node_name=read_env(env, _NODE_NAME_ENV) or None,
            colocate_with_control_plane_node=read_bool_env(env, _COLOCATE_WITH_CONTROL_PLANE_NODE_ENV, default=False),
            extra_env=read_json_mapping_env(env, _EXTRA_ENV_JSON_ENV),
            extra_labels=read_json_mapping_env(env, _EXTRA_LABELS_JSON_ENV),
            extra_annotations=read_json_mapping_env(env, _EXTRA_ANNOTATIONS_JSON_ENV),
            extra_containers=_read_extra_containers_env(env),
            extra_volumes=_read_extra_volumes_env(env),
            owner_deployment_name=read_env(env, _OWNER_DEPLOYMENT_NAME_ENV) or None,
            resource_requests=resource_requests,
            resource_limits=resource_limits,
            enable_service_links=read_bool_env(env, _ENABLE_SERVICE_LINKS_ENV, default=False),
            auth_secret_name=read_env(env, _AUTH_SECRET_NAME_ENV) or None,
            seccomp_profile=_read_seccomp_profile_env(env),
            script_resource_profiles=_read_script_resource_profiles_env(env),
            default_script_resource_profile=(
                read_env(
                    env,
                    _DEFAULT_SCRIPT_RESOURCE_PROFILE_ENV,
                    _DEFAULT_SCRIPT_RESOURCE_PROFILE,
                )
                or _DEFAULT_SCRIPT_RESOURCE_PROFILE
            ),
            reconcile_pod_templates=read_bool_env(env, _RECONCILE_POD_TEMPLATES_ENV, default=True),
            tmp_size_limit=read_env(env, _TMP_SIZE_LIMIT_ENV) or None,
            user_resources=_read_user_resources_env(env),
        )


def kubernetes_backend_config_signature(
    config: KubernetesWorkerBackendConfig,
    runtime_paths: RuntimePaths,
    *,
    auth_token: str | None,
    storage_root: Path,
) -> tuple[str, ...]:
    """Return a cache signature for one concrete Kubernetes backend config."""
    credentials_encryption_key = runtime_paths.env_value(CREDENTIALS_ENCRYPTION_KEY_ENV)
    credentials_encryption_key_marker = _credentials_encryption_key_hash(credentials_encryption_key) or ""
    extra_env_json = stable_signature_json(config.extra_env)
    extra_labels_json = stable_signature_json(config.extra_labels)
    extra_annotations_json = stable_signature_json(config.extra_annotations)
    extra_containers_json = stable_signature_json(config.extra_containers)
    extra_volumes_json = stable_signature_json(config.extra_volumes)
    resource_requests_json = stable_signature_json(config.resource_requests)
    resource_limits_json = stable_signature_json(config.resource_limits)
    script_resource_profiles_json = stable_signature_json(config.script_resource_profiles)
    client_identity = _kubernetes_client_identity(runtime_paths)
    signature = (
        "kubernetes",
        runtime_paths.env_value(WORKER_COMPUTER_ENABLED_ENV, default=""),
        stable_signature_json(config.seccomp_profile),
        config.namespace,
        config.image,
        config.image_pull_policy,
        str(config.worker_port),
        config.service_account_name,
        config.storage_pvc_name,
        config.storage_mount_path,
        config.storage_subpath_prefix,
        str(config.idle_timeout_seconds),
        str(config.ready_timeout_seconds),
        config.name_prefix,
        client_identity,
        config.node_name or "",
        str(config.colocate_with_control_plane_node),
        extra_env_json,
        extra_labels_json,
        extra_annotations_json,
        extra_containers_json,
        extra_volumes_json,
        config.owner_deployment_name or "",
        resource_requests_json,
        resource_limits_json,
        script_resource_profiles_json,
        config.default_script_resource_profile,
        str(config.enable_service_links),
        config.auth_secret_name or "",
        str(config.reconcile_pod_templates),
        credentials_encryption_key_marker,
        auth_token or "",
        str(storage_root),
    )
    if config.runtime_class_name is not None:
        signature = (*signature, f"runtime-class:{config.runtime_class_name}")
    if config.tmp_size_limit is not None:
        signature = (*signature, f"tmp-size-limit:{config.tmp_size_limit}")
    if config.user_resources:
        signature = (*signature, f"user-resources:{stable_signature_json(config.user_resources)}")
    if config.image_pull_secrets:
        signature = (*signature, f"image-pull-secrets:{stable_signature_json(config.image_pull_secrets)}")
    return signature


def kubernetes_backend_cleanup_signature(
    config: KubernetesWorkerBackendConfig,
    runtime_paths: RuntimePaths,
    *,
    storage_root: Path,
) -> tuple[str, ...]:
    """Return the stable fields needed to find and retire an existing Kubernetes worker."""
    return (
        "kubernetes",
        config.namespace,
        config.name_prefix,
        config.storage_pvc_name,
        config.storage_subpath_prefix,
        config.auth_secret_name or "",
        _kubernetes_cleanup_client_identity(runtime_paths),
        str(storage_root),
    )


def _kubernetes_cleanup_client_identity(runtime_paths: RuntimePaths) -> str:
    """Return a Kubernetes cleanup identity that ignores mutable user credentials."""
    in_cluster_identity = _in_cluster_client_identity()
    if in_cluster_identity is not None:
        return in_cluster_identity

    contexts: dict[str, dict[str, object]] = {}
    clusters: dict[str, dict[str, object]] = {}
    current_context: str | None = None
    for kubeconfig_path in resolve_kubeconfig_paths(runtime_paths):
        try:
            document = yaml_io.safe_load(kubeconfig_path.read_bytes())
        except OSError:
            continue
        if not isinstance(document, dict):
            continue
        configured_context = document.get("current-context")
        if isinstance(configured_context, str):
            current_context = configured_context
        _merge_named_kubeconfig_entries(contexts, document.get("contexts"), "context")
        _merge_named_kubeconfig_entries(clusters, document.get("clusters"), "cluster")

    context = contexts.get(current_context or "")
    cluster_name = context.get("cluster") if context is not None else None
    cluster = clusters.get(cluster_name) if isinstance(cluster_name, str) else None
    cluster_server = cluster.get("server") if cluster is not None else None
    if not isinstance(cluster_server, str) or not cluster_server:
        return _kubernetes_client_identity(runtime_paths)
    return "kubeconfig-cluster:" + stable_signature_json({"server": cluster_server})


def _merge_named_kubeconfig_entries(
    destination: dict[str, dict[str, object]],
    entries: object,
    value_key: str,
) -> None:
    """Merge named kubeconfig entries with the client's first-name-wins semantics."""
    if not isinstance(entries, list):
        return
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        entry_mapping = cast("dict[str, object]", entry)
        name = entry_mapping.get("name")
        value = entry_mapping.get(value_key)
        if isinstance(name, str) and isinstance(value, dict) and name not in destination:
            destination[name] = cast("dict[str, object]", value)


def _kubernetes_client_identity(runtime_paths: RuntimePaths) -> str:
    """Return a non-secret cache fingerprint for the selected Kubernetes control plane."""
    in_cluster_identity = _in_cluster_client_identity()
    if in_cluster_identity is not None:
        return in_cluster_identity
    identities: list[str] = []
    for kubeconfig_path in resolve_kubeconfig_paths(runtime_paths):
        try:
            kubeconfig_digest = hashlib.sha256(kubeconfig_path.read_bytes()).hexdigest()
        except OSError:
            kubeconfig_digest = "unavailable"
        identities.append(f"{kubeconfig_path}:{kubeconfig_digest}")
    return "kubeconfig:" + os.pathsep.join(identities)


def _in_cluster_client_identity() -> str | None:
    """Return the in-cluster identity only when its required credentials are available."""
    service_host = os.environ.get("KUBERNETES_SERVICE_HOST")
    service_port = os.environ.get("KUBERNETES_SERVICE_PORT")
    if service_host and service_port and _in_cluster_credentials_available():
        return f"in-cluster:{service_host}:{service_port}"
    return None


def _in_cluster_credentials_available() -> bool:
    """Return whether the Kubernetes client can read its required service-account files."""
    try:
        for path in (_IN_CLUSTER_TOKEN_PATH, _IN_CLUSTER_CERT_PATH):
            with path.open("rb") as credential_file:
                if not credential_file.read(1):
                    return False
    except OSError:
        return False
    return True


def resolve_kubeconfig_paths(runtime_paths: RuntimePaths) -> tuple[Path, ...]:
    """Resolve the ordered files selected by the Kubernetes client."""
    raw_paths = runtime_env_values(runtime_paths).get("KUBECONFIG")
    configured_paths = raw_paths.split(os.pathsep) if raw_paths else [str(Path.home() / ".kube" / "config")]
    return tuple(Path(path).expanduser().resolve() for path in configured_paths if path)


def _credentials_encryption_key_hash(encryption_key: str | None) -> str | None:
    """Return a stable non-secret marker for the credential encryption key."""
    normalized_key = credentials_encryption_key_value(encryption_key)
    if normalized_key is None:
        return None
    return hashlib.sha256(normalized_key.encode("utf-8")).hexdigest()
