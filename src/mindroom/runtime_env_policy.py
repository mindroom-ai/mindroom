"""Runtime, worker, sandbox, and tool environment-variable policy."""

from __future__ import annotations

import fnmatch
from types import MappingProxyType
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "CREDENTIAL_SEEDS_FILE_ENV",
    "CREDENTIAL_SEEDS_JSON_ENV",
    "KUBERNETES_WORKER_BACKEND_CONFIG_ENV_NAMES",
    "SANDBOX_STARTUP_MANIFEST_PATH_ENV",
    "VENDOR_TELEMETRY_ENV_VALUES",
    "is_execution_runtime_env_file_name",
    "is_execution_runtime_env_name",
    "is_execution_runtime_process_env_name",
    "is_isolated_worker_runtime_env_name",
    "is_public_worker_startup_env_name",
    "is_runtime_control_env_name",
    "is_runtime_database_url_env_name",
    "is_shell_passthrough_allowed_env_name",
    "is_worker_backend_config_env_name",
    "isolated_worker_runtime_env",
    "public_worker_startup_env",
    "sandbox_execution_runtime_env",
    "sandbox_runner_startup_process_env",
    "sandbox_shell_system_env",
    "shell_passthrough_env",
]

SANDBOX_STARTUP_MANIFEST_PATH_ENV = "MINDROOM_SANDBOX_STARTUP_MANIFEST_PATH"
CREDENTIAL_SEEDS_JSON_ENV = "MINDROOM_CREDENTIAL_SEEDS_JSON"
CREDENTIAL_SEEDS_FILE_ENV = "MINDROOM_CREDENTIAL_SEEDS_FILE"

VENDOR_TELEMETRY_ENV_VALUES: Mapping[str, str] = MappingProxyType(
    {
        "AGNO_TELEMETRY": "false",
        "ANONYMIZED_TELEMETRY": "false",
        "CHROMA_OTEL_COLLECTION_ENDPOINT": "",
        "CHROMA_OTEL_GRANULARITY": "none",
        "COMPOSIO_DISABLE_SENTRY": "true",
        "COMPOSIO_DISABLE_VERSION_CHECK": "true",
        "DISABLE_TELEMETRY": "1",
        "DO_NOT_TRACK": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "LITELLM_LOCAL_ANTHROPIC_BETA_HEADERS": "true",
        "LITELLM_LOCAL_MODEL_COST_MAP": "true",
        "MEM0_TELEMETRY": "false",
        "MEM0_TELEMETRY_SAMPLE_RATE": "0",
        "NEXT_TELEMETRY_DISABLED": "1",
        "OTEL_SDK_DISABLED": "true",
        "TURBO_TELEMETRY_DISABLED": "1",
        "WANDB_MODE": "disabled",
    },
)

KUBERNETES_WORKER_BACKEND_CONFIG_ENV_NAMES = frozenset(
    {
        "MINDROOM_WORKER_BACKEND",
        "MINDROOM_KUBERNETES_WORKER_NAMESPACE",
        "MINDROOM_KUBERNETES_WORKER_IMAGE",
        "MINDROOM_KUBERNETES_WORKER_IMAGE_PULL_POLICY",
        "MINDROOM_KUBERNETES_WORKER_PORT",
        "MINDROOM_KUBERNETES_WORKER_SERVICE_ACCOUNT_NAME",
        "MINDROOM_KUBERNETES_WORKER_STORAGE_PVC_NAME",
        "MINDROOM_KUBERNETES_WORKER_STORAGE_MOUNT_PATH",
        "MINDROOM_KUBERNETES_WORKER_STORAGE_SUBPATH_PREFIX",
        "MINDROOM_KUBERNETES_WORKER_CONFIG_MAP_NAME",
        "MINDROOM_KUBERNETES_WORKER_CONFIG_KEY",
        "MINDROOM_KUBERNETES_WORKER_CONFIG_PATH",
        "MINDROOM_KUBERNETES_WORKER_IDLE_TIMEOUT_SECONDS",
        "MINDROOM_KUBERNETES_WORKER_READY_TIMEOUT_SECONDS",
        "MINDROOM_KUBERNETES_WORKER_NAME_PREFIX",
        "MINDROOM_KUBERNETES_WORKER_NODE_NAME",
        "MINDROOM_KUBERNETES_WORKER_COLOCATE_WITH_CONTROL_PLANE_NODE",
        "MINDROOM_KUBERNETES_WORKER_ENV_JSON",
        "MINDROOM_KUBERNETES_WORKER_LABELS_JSON",
        "MINDROOM_KUBERNETES_WORKER_ANNOTATIONS_JSON",
        "MINDROOM_KUBERNETES_WORKER_OWNER_DEPLOYMENT_NAME",
        "MINDROOM_KUBERNETES_WORKER_MEMORY_REQUEST",
        "MINDROOM_KUBERNETES_WORKER_MEMORY_LIMIT",
        "MINDROOM_KUBERNETES_WORKER_CPU_REQUEST",
        "MINDROOM_KUBERNETES_WORKER_CPU_LIMIT",
        "MINDROOM_KUBERNETES_WORKER_ENABLE_SERVICE_LINKS",
        "MINDROOM_KUBERNETES_WORKER_AUTH_SECRET_NAME",
    },
)

_CREDENTIAL_SEED_DECLARATION_ENV_NAMES = frozenset(
    {
        CREDENTIAL_SEEDS_JSON_ENV,
        CREDENTIAL_SEEDS_FILE_ENV,
    },
)
_RUNTIME_STARTUP_ENV_PREFIXES = ("MINDROOM_", "MATRIX_", "BROWSER_")
_VENDOR_TELEMETRY_ENV_NAMES = frozenset(VENDOR_TELEMETRY_ENV_VALUES)
_RUNTIME_STARTUP_ENV_EXTRA_KEYS = frozenset(
    {
        "ACCOUNT_ID",
        "ANTHROPIC_VERTEX_BASE_URL",
        "ANTHROPIC_VERTEX_PROJECT_ID",
        "CLOUD_ML_REGION",
        "CUSTOMER_ID",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_CLOUD_LOCATION",
        "GOOGLE_CLOUD_PROJECT",
        "OLLAMA_HOST",
        "OPENAI_BASE_URL",
        "POD_NAMESPACE",
        *_VENDOR_TELEMETRY_ENV_NAMES,
    },
)
_ISOLATED_RUNTIME_ENV_EXTRA_KEYS = frozenset(
    {
        "ACCOUNT_ID",
        "CUSTOMER_ID",
        "MINDROOM_KUBERNETES_WORKER_STORAGE_SUBPATH_PREFIX",
        "POD_NAMESPACE",
        *_VENDOR_TELEMETRY_ENV_NAMES,
    },
)
_WORKER_RUNTIME_STATE_ENV_NAMES = frozenset(
    {
        "MINDROOM_KUBERNETES_WORKER_STORAGE_SUBPATH_PREFIX",
    },
)
_RUNTIME_STARTUP_EXCLUDED_NAMES = frozenset(
    {
        *_CREDENTIAL_SEED_DECLARATION_ENV_NAMES,
        "MINDROOM_EVENT_CACHE_DATABASE_URL",
        "MINDROOM_LOCAL_CLIENT_ID",
        "MINDROOM_SANDBOX_PROXY_TOKEN",
        SANDBOX_STARTUP_MANIFEST_PATH_ENV,
    },
)
_RUNTIME_STARTUP_SECRET_SUFFIXES = (
    "_API_KEY",
    "_API_KEYS",
    "_PASSWORD",
    "_SECRET",
    "_TOKEN",
)
_RUNTIME_DATABASE_URL_NAMES = frozenset({"DATABASE_URL"})
_RUNTIME_DATABASE_URL_SUFFIXES = ("_DATABASE_URL",)
_EXECUTION_RUNTIME_EXCLUDED_NAMES = frozenset(
    {
        *_RUNTIME_STARTUP_EXCLUDED_NAMES,
        "MINDROOM_API_KEY",
        "MINDROOM_LOCAL_CLIENT_SECRET",
    },
)
_RUNNER_CONTROL_ENV_EXCLUDED_NAMES = frozenset(
    {
        "MINDROOM_API_KEY",
        "MINDROOM_LOCAL_CLIENT_SECRET",
        "MINDROOM_SANDBOX_PROXY_TOKEN",
        SANDBOX_STARTUP_MANIFEST_PATH_ENV,
    },
)
_SANDBOX_SHELL_SYSTEM_ENV_NAMES = frozenset(
    {
        "CURL_CA_BUNDLE",
        "HOME",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LD_LIBRARY_PATH",
        "NIX_LD",
        "NIX_LD_LIBRARY_PATH",
        "NO_PROXY",
        "PATH",
        "PIP_CACHE_DIR",
        "PYTHONPATH",
        "PYTHONPYCACHEPREFIX",
        "REQUESTS_CA_BUNDLE",
        "SHELL",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TERM",
        "TMPDIR",
        "USER",
        "UV_CACHE_DIR",
        "VIRTUAL_ENV",
        "XDG_CACHE_HOME",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    },
)
_KNOWN_WORKER_CREDENTIAL_ENV_NAMES = frozenset(
    {
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GITHUB_TOKEN",
        "ANTHROPIC_VERTEX_PROJECT_ID",
        "CLOUD_ML_REGION",
    },
)


def is_runtime_control_env_name(name: str) -> bool:
    """Return whether an env var is internal runtime/control-plane material."""
    return (
        name in _RUNNER_CONTROL_ENV_EXCLUDED_NAMES
        or name in _CREDENTIAL_SEED_DECLARATION_ENV_NAMES
        or name.startswith("MINDROOM_SANDBOX_")
        or is_worker_backend_config_env_name(name)
    )


def is_worker_backend_config_env_name(name: str) -> bool:
    """Return whether an env var configures a primary-side worker backend."""
    return name in KUBERNETES_WORKER_BACKEND_CONFIG_ENV_NAMES


def is_runtime_database_url_env_name(name: str) -> bool:
    """Return whether an env name conventionally carries a database connection URL."""
    return name in _RUNTIME_DATABASE_URL_NAMES or name.endswith(_RUNTIME_DATABASE_URL_SUFFIXES)


def is_public_worker_startup_env_name(name: str) -> bool:
    """Return whether an env var may be serialized into public worker startup manifests."""
    if name in _RUNTIME_STARTUP_EXCLUDED_NAMES or is_worker_backend_config_env_name(name):
        return False
    if is_runtime_database_url_env_name(name):
        return False
    if not (name.startswith(_RUNTIME_STARTUP_ENV_PREFIXES) or name in _RUNTIME_STARTUP_ENV_EXTRA_KEYS):
        return False
    return not name.endswith(_RUNTIME_STARTUP_SECRET_SUFFIXES)


def is_isolated_worker_runtime_env_name(name: str) -> bool:
    """Return whether inherited env may remain visible inside isolated workers."""
    if name in _EXECUTION_RUNTIME_EXCLUDED_NAMES:
        return False
    if is_worker_backend_config_env_name(name) and name not in _WORKER_RUNTIME_STATE_ENV_NAMES:
        return False
    if is_runtime_database_url_env_name(name):
        return False
    if not (name.startswith(_RUNTIME_STARTUP_ENV_PREFIXES) or name in _ISOLATED_RUNTIME_ENV_EXTRA_KEYS):
        return False
    return not name.endswith(_RUNTIME_STARTUP_SECRET_SUFFIXES)


def is_execution_runtime_env_name(name: str) -> bool:
    """Return whether an env var may be visible to sandbox execution runtime construction."""
    return is_isolated_worker_runtime_env_name(name)


def is_execution_runtime_env_file_name(name: str) -> bool:
    """Return whether a config-adjacent env value may be visible to local execution tools."""
    return name not in _EXECUTION_RUNTIME_EXCLUDED_NAMES and not is_runtime_database_url_env_name(name)


def is_execution_runtime_process_env_name(name: str) -> bool:
    """Return whether a process env value may be visible to local execution tools."""
    return name not in _EXECUTION_RUNTIME_EXCLUDED_NAMES and (
        is_public_worker_startup_env_name(name) or name in _KNOWN_WORKER_CREDENTIAL_ENV_NAMES
    )


def is_shell_passthrough_allowed_env_name(name: str) -> bool:
    """Return whether explicit shell passthrough may expose this env var."""
    return not is_runtime_control_env_name(name)


def public_worker_startup_env(env: Mapping[str, str]) -> dict[str, str]:
    """Return the env safe to serialize into public worker startup manifests."""
    return {key: value for key, value in env.items() if is_public_worker_startup_env_name(key)}


def isolated_worker_runtime_env(env: Mapping[str, str]) -> dict[str, str]:
    """Return inherited env safe for isolated worker RuntimePaths."""
    return {key: value for key, value in env.items() if is_isolated_worker_runtime_env_name(key)}


def sandbox_execution_runtime_env(env: Mapping[str, str]) -> dict[str, str]:
    """Return env safe for sandboxed python/tool runtime reconstruction."""
    return {key: value for key, value in env.items() if is_execution_runtime_env_name(key)}


def sandbox_runner_startup_process_env(env: Mapping[str, str]) -> dict[str, str]:
    """Return ambient process env safe for non-dedicated sandbox runner startup rehydration."""
    return {key: value for key, value in env.items() if not is_runtime_control_env_name(key)}


def shell_passthrough_env(
    env: Mapping[str, str],
    *,
    patterns: tuple[str, ...],
) -> dict[str, str]:
    """Return explicit shell passthrough values after control-env denial."""
    if not patterns:
        return {}
    return {
        key: value
        for key, value in env.items()
        if is_shell_passthrough_allowed_env_name(key) and any(fnmatch.fnmatchcase(key, pattern) for pattern in patterns)
    }


def sandbox_shell_system_env(env: Mapping[str, str]) -> Mapping[str, str]:
    """Return the non-secret system env shell commands may receive by default."""
    return cast(
        "Mapping[str, str]",
        MappingProxyType({key: value for key, value in env.items() if key in _SANDBOX_SHELL_SYSTEM_ENV_NAMES}),
    )
