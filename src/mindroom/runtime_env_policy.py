"""Runtime, worker, sandbox, and tool environment-variable policy."""

from __future__ import annotations

import fnmatch
from collections.abc import Mapping  # noqa: TC003 - public annotations support typing.get_type_hints().
from types import MappingProxyType
from typing import cast

from mindroom.sensitivity import secret_name_suffixes

__all__ = [
    "AGENT_VAULT_ACCESS_ENV_BY_KEY",
    "AWS_BEDROCK_CLAUDE_ENV_BY_KEY",
    "AZURE_OPENAI_ENV_BY_KEY",
    "CREDENTIALS_ENCRYPTION_KEY_ENV",
    "CREDENTIAL_SEEDS_FILE_ENV",
    "CREDENTIAL_SEEDS_JSON_ENV",
    "KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY",
    "KUBERNETES_WORKER_BACKEND_CONFIG_ENV_NAMES",
    "SANDBOX_RUNTIME_ENV_BY_KEY",
    "SANDBOX_STARTUP_MANIFEST_PATH_ENV",
    "SHARED_CREDENTIALS_PATH_ENV",
    "VENDOR_TELEMETRY_ENV_VALUES",
    "VERTEXAI_CLAUDE_ENV_BY_KEY",
    "WORKER_EGRESS_PROXY_ENV_BY_KEY",
    "credentials_encryption_key_from_env",
    "credentials_encryption_key_value",
    "execution_tool_runtime_env",
    "is_isolated_worker_runtime_env_name",
    "is_public_worker_startup_env_name",
    "is_runtime_control_env_name",
    "is_runtime_database_url_env_name",
    "is_shell_passthrough_allowed_env_name",
    "is_trusted_tool_runtime_env_file_name",
    "is_trusted_tool_runtime_process_env_name",
    "is_worker_backend_config_env_name",
    "is_worker_extra_env_name",
    "isolated_worker_runtime_env",
    "public_worker_startup_env",
    "sandbox_runner_runtime_state_env",
    "sandbox_runner_startup_process_env",
    "sandbox_shell_system_env",
    "sandbox_subprocess_system_env",
    "shell_passthrough_env",
    "worker_extra_env",
]

SANDBOX_STARTUP_MANIFEST_PATH_ENV = "MINDROOM_SANDBOX_STARTUP_MANIFEST_PATH"
CREDENTIAL_SEEDS_JSON_ENV = "MINDROOM_CREDENTIAL_SEEDS_JSON"
CREDENTIAL_SEEDS_FILE_ENV = "MINDROOM_CREDENTIAL_SEEDS_FILE"
CREDENTIALS_ENCRYPTION_KEY_ENV = "MINDROOM_CREDENTIALS_ENCRYPTION_KEY"
SHARED_CREDENTIALS_PATH_ENV = "MINDROOM_SHARED_CREDENTIALS_PATH"
AWS_BEDROCK_CLAUDE_ENV_BY_KEY: Mapping[str, str] = MappingProxyType(
    {
        "access_key": "AWS_ACCESS_KEY_ID",
        "secret_key": "AWS_SECRET_ACCESS_KEY",
        "session_token": "AWS_SESSION_TOKEN",
        "region": "AWS_REGION",
        "default_region": "AWS_DEFAULT_REGION",
        "profile": "AWS_PROFILE",
    },
)
VERTEXAI_CLAUDE_ENV_BY_KEY: Mapping[str, str] = MappingProxyType(
    {
        "project_id": "ANTHROPIC_VERTEX_PROJECT_ID",
        "region": "CLOUD_ML_REGION",
    },
)
AZURE_OPENAI_ENV_BY_KEY: Mapping[str, str] = MappingProxyType(
    {
        "api_key": "AZURE_OPENAI_API_KEY",
        "endpoint": "AZURE_OPENAI_ENDPOINT",
        "api_version": "AZURE_OPENAI_API_VERSION",
        "deployment": "AZURE_OPENAI_DEPLOYMENT",
    },
)
AGENT_VAULT_ACCESS_ENV_BY_KEY: Mapping[str, str] = MappingProxyType(
    {
        "api_url": "MINDROOM_AGENT_VAULT_ACCESS_API_URL",
        "admin_token": "MINDROOM_AGENT_VAULT_ACCESS_ADMIN_TOKEN",
        "ui_base_url": "MINDROOM_AGENT_VAULT_ACCESS_UI_BASE_URL",
        "email_domain": "MINDROOM_AGENT_VAULT_ACCESS_EMAIL_DOMAIN",
        "vault_name_prefix": "MINDROOM_AGENT_VAULT_ACCESS_VAULT_NAME_PREFIX",
    },
)

# Single source of truth for the per-worker egress proxy env contract. The
# Kubernetes backend (writer) sets these on the worker pod; the sandbox runner
# (reader, mindroom.constants.worker_proxy_execution_env) consumes them. Both
# import from here so a rename cannot silently desync writer and reader.
WORKER_EGRESS_PROXY_ENV_BY_KEY: Mapping[str, str] = MappingProxyType(
    {
        "proxy_url": "MINDROOM_WORKER_EGRESS_PROXY_URL",
        "token_file": "MINDROOM_WORKER_EGRESS_PROXY_TOKEN_FILE",
        "ca_file": "MINDROOM_WORKER_EGRESS_PROXY_CA_FILE",
    },
)

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

KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY: Mapping[str, str] = MappingProxyType(
    {
        "worker_backend": "MINDROOM_WORKER_BACKEND",
        "namespace": "MINDROOM_KUBERNETES_WORKER_NAMESPACE",
        "image": "MINDROOM_KUBERNETES_WORKER_IMAGE",
        "image_pull_policy": "MINDROOM_KUBERNETES_WORKER_IMAGE_PULL_POLICY",
        "port": "MINDROOM_KUBERNETES_WORKER_PORT",
        "service_account": "MINDROOM_KUBERNETES_WORKER_SERVICE_ACCOUNT_NAME",
        "storage_pvc": "MINDROOM_KUBERNETES_WORKER_STORAGE_PVC_NAME",
        "storage_mount_path": "MINDROOM_KUBERNETES_WORKER_STORAGE_MOUNT_PATH",
        "storage_subpath_prefix": "MINDROOM_KUBERNETES_WORKER_STORAGE_SUBPATH_PREFIX",
        "config_map_name": "MINDROOM_KUBERNETES_WORKER_CONFIG_MAP_NAME",
        "config_key": "MINDROOM_KUBERNETES_WORKER_CONFIG_KEY",
        "config_path": "MINDROOM_KUBERNETES_WORKER_CONFIG_PATH",
        "idle_timeout": "MINDROOM_KUBERNETES_WORKER_IDLE_TIMEOUT_SECONDS",
        "ready_timeout": "MINDROOM_KUBERNETES_WORKER_READY_TIMEOUT_SECONDS",
        "name_prefix": "MINDROOM_KUBERNETES_WORKER_NAME_PREFIX",
        "node_name": "MINDROOM_KUBERNETES_WORKER_NODE_NAME",
        "colocate_with_control_plane_node": "MINDROOM_KUBERNETES_WORKER_COLOCATE_WITH_CONTROL_PLANE_NODE",
        "extra_env_json": "MINDROOM_KUBERNETES_WORKER_ENV_JSON",
        "extra_labels_json": "MINDROOM_KUBERNETES_WORKER_LABELS_JSON",
        "extra_annotations_json": "MINDROOM_KUBERNETES_WORKER_ANNOTATIONS_JSON",
        "owner_deployment_name": "MINDROOM_KUBERNETES_WORKER_OWNER_DEPLOYMENT_NAME",
        "memory_request": "MINDROOM_KUBERNETES_WORKER_MEMORY_REQUEST",
        "memory_limit": "MINDROOM_KUBERNETES_WORKER_MEMORY_LIMIT",
        "cpu_request": "MINDROOM_KUBERNETES_WORKER_CPU_REQUEST",
        "cpu_limit": "MINDROOM_KUBERNETES_WORKER_CPU_LIMIT",
        "enable_service_links": "MINDROOM_KUBERNETES_WORKER_ENABLE_SERVICE_LINKS",
        "auth_secret_name": "MINDROOM_KUBERNETES_WORKER_AUTH_SECRET_NAME",
        "agent_vault_enabled": "MINDROOM_KUBERNETES_AGENT_VAULT_ENABLED",
        "agent_vault_vault_name_prefix": "MINDROOM_KUBERNETES_AGENT_VAULT_VAULT_NAME_PREFIX",
        "agent_vault_cli_image": "MINDROOM_KUBERNETES_AGENT_VAULT_CLI_IMAGE",
        "agent_vault_api_url": "MINDROOM_KUBERNETES_AGENT_VAULT_API_URL",
        "agent_vault_proxy_url": "MINDROOM_KUBERNETES_AGENT_VAULT_PROXY_URL",
        "agent_vault_owner_email": "MINDROOM_KUBERNETES_AGENT_VAULT_OWNER_EMAIL",
        "agent_vault_bootstrap_secret_name": "MINDROOM_KUBERNETES_AGENT_VAULT_BOOTSTRAP_SECRET_NAME",
        "agent_vault_worker_ca_configmap_name": "MINDROOM_KUBERNETES_AGENT_VAULT_WORKER_CA_CONFIGMAP_NAME",
    },
)
KUBERNETES_WORKER_BACKEND_CONFIG_ENV_NAMES = frozenset(KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY.values())

SANDBOX_RUNTIME_ENV_BY_KEY: Mapping[str, str] = MappingProxyType(
    {
        "credential_lease_ttl_seconds": "MINDROOM_SANDBOX_CREDENTIAL_LEASE_TTL_SECONDS",
        "credential_policy_json": "MINDROOM_SANDBOX_CREDENTIAL_POLICY_JSON",
        "dedicated_worker_key": "MINDROOM_SANDBOX_DEDICATED_WORKER_KEY",
        "dedicated_worker_root": "MINDROOM_SANDBOX_DEDICATED_WORKER_ROOT",
        "execution_mode": "MINDROOM_SANDBOX_EXECUTION_MODE",
        "proxy_timeout_seconds": "MINDROOM_SANDBOX_PROXY_TIMEOUT_SECONDS",
        "proxy_token": "MINDROOM_SANDBOX_PROXY_TOKEN",
        "proxy_tools": "MINDROOM_SANDBOX_PROXY_TOOLS",
        "proxy_url": "MINDROOM_SANDBOX_PROXY_URL",
        "runner_execution_mode": "MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE",
        "runner_mode": "MINDROOM_SANDBOX_RUNNER_MODE",
        "runner_port": "MINDROOM_SANDBOX_RUNNER_PORT",
        "runner_subprocess_timeout_seconds": "MINDROOM_SANDBOX_RUNNER_SUBPROCESS_TIMEOUT_SECONDS",
        "shared_storage_root": "MINDROOM_SANDBOX_SHARED_STORAGE_ROOT",
        "unsafe_allow_local_execution_tools": "MINDROOM_UNSAFE_ALLOW_LOCAL_EXECUTION_TOOLS",
        "worker_endpoint": "MINDROOM_SANDBOX_WORKER_ENDPOINT",
        "worker_idle_timeout_seconds": "MINDROOM_SANDBOX_WORKER_IDLE_TIMEOUT_SECONDS",
    },
)
_SANDBOX_RUNTIME_ENV_PREFIX = "MINDROOM_SANDBOX_"

_CREDENTIAL_SEED_DECLARATION_ENV_NAMES = frozenset(
    {
        CREDENTIAL_SEEDS_JSON_ENV,
        CREDENTIAL_SEEDS_FILE_ENV,
    },
)
_RUNTIME_STARTUP_ENV_PREFIXES = ("MINDROOM_", "MATRIX_", "BROWSER_")
_VENDOR_TELEMETRY_ENV_NAMES = frozenset(VENDOR_TELEMETRY_ENV_VALUES)
_AWS_BEDROCK_CLAUDE_PUBLIC_STARTUP_ENV_NAMES = frozenset(
    {
        AWS_BEDROCK_CLAUDE_ENV_BY_KEY["default_region"],
        AWS_BEDROCK_CLAUDE_ENV_BY_KEY["profile"],
        AWS_BEDROCK_CLAUDE_ENV_BY_KEY["region"],
    },
)
_RUNTIME_STARTUP_ENV_EXTRA_KEYS = frozenset(
    {
        "ACCOUNT_ID",
        "ANTHROPIC_VERTEX_BASE_URL",
        *_AWS_BEDROCK_CLAUDE_PUBLIC_STARTUP_ENV_NAMES,
        "CUSTOMER_ID",
        "AZURE_OPENAI_API_VERSION",
        "AZURE_OPENAI_DEPLOYMENT",
        "AZURE_OPENAI_ENDPOINT",
        "GOOGLE_CLOUD_LOCATION",
        "GOOGLE_CLOUD_PROJECT",
        "OLLAMA_HOST",
        "OPENAI_BASE_URL",
        "POD_NAMESPACE",
        *VERTEXAI_CLAUDE_ENV_BY_KEY.values(),
        *_VENDOR_TELEMETRY_ENV_NAMES,
    },
)
_ISOLATED_RUNTIME_ENV_EXTRA_KEYS = frozenset(
    {
        "ACCOUNT_ID",
        "CUSTOMER_ID",
        "POD_NAMESPACE",
        *_VENDOR_TELEMETRY_ENV_NAMES,
    },
)
_PUBLIC_WORKER_SANDBOX_STARTUP_ENV_NAMES = frozenset(
    {
        SANDBOX_RUNTIME_ENV_BY_KEY["dedicated_worker_key"],
        SANDBOX_RUNTIME_ENV_BY_KEY["dedicated_worker_root"],
        SANDBOX_RUNTIME_ENV_BY_KEY["runner_execution_mode"],
        SANDBOX_RUNTIME_ENV_BY_KEY["runner_mode"],
        SANDBOX_RUNTIME_ENV_BY_KEY["runner_port"],
        SANDBOX_RUNTIME_ENV_BY_KEY["runner_subprocess_timeout_seconds"],
    },
)
_WORKER_RUNTIME_STATE_ENV_NAMES = _PUBLIC_WORKER_SANDBOX_STARTUP_ENV_NAMES | frozenset(
    {
        KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["storage_subpath_prefix"],
    },
)
_WORKER_EXTRA_ENV_SANDBOX_ENV_NAMES = frozenset(
    {
        SANDBOX_RUNTIME_ENV_BY_KEY["runner_subprocess_timeout_seconds"],
    },
)
_SANDBOX_RUNNER_STARTUP_ENV_NAMES = frozenset(
    {
        SANDBOX_RUNTIME_ENV_BY_KEY["runner_execution_mode"],
        SANDBOX_RUNTIME_ENV_BY_KEY["runner_mode"],
        SANDBOX_RUNTIME_ENV_BY_KEY["runner_port"],
        SANDBOX_RUNTIME_ENV_BY_KEY["runner_subprocess_timeout_seconds"],
        SANDBOX_RUNTIME_ENV_BY_KEY["shared_storage_root"],
        SANDBOX_RUNTIME_ENV_BY_KEY["worker_endpoint"],
        SANDBOX_RUNTIME_ENV_BY_KEY["worker_idle_timeout_seconds"],
    },
)
_SANDBOX_RUNNER_RUNTIME_STATE_ENV_NAMES = _SANDBOX_RUNNER_STARTUP_ENV_NAMES | _WORKER_RUNTIME_STATE_ENV_NAMES
_WORKER_EXTRA_ENV_GENERATED_NAMES = frozenset(
    {
        "HOME",
        "MINDROOM_CONFIG_PATH",
        SHARED_CREDENTIALS_PATH_ENV,
        "MINDROOM_STORAGE_PATH",
        "PATH",
        "VIRTUAL_ENV",
    },
)
_RUNTIME_STARTUP_EXCLUDED_NAMES = frozenset(
    {
        *_CREDENTIAL_SEED_DECLARATION_ENV_NAMES,
        CREDENTIALS_ENCRYPTION_KEY_ENV,
        "MINDROOM_EVENT_CACHE_DATABASE_URL",
        "MINDROOM_LOCAL_CLIENT_ID",
        SANDBOX_RUNTIME_ENV_BY_KEY["proxy_token"],
        SANDBOX_STARTUP_MANIFEST_PATH_ENV,
    },
)
# Shared secret stems (api_key/password/secret/token) plus the env-only `_API_KEYS`.
_RUNTIME_STARTUP_SECRET_SUFFIXES = (*secret_name_suffixes(upper=True), "_API_KEYS")
_RUNTIME_DATABASE_URL_NAMES = frozenset({"DATABASE_URL"})
_RUNTIME_DATABASE_URL_SUFFIXES = ("_DATABASE_URL",)
_EXECUTION_RUNTIME_EXCLUDED_NAMES = frozenset(
    {
        *_RUNTIME_STARTUP_EXCLUDED_NAMES,
        "MINDROOM_API_KEY",
        "MINDROOM_LOCAL_CLIENT_SECRET",
    },
)
_NON_SANDBOX_RUNTIME_CONTROL_ENV_NAMES = frozenset(
    {
        CREDENTIALS_ENCRYPTION_KEY_ENV,
        "MINDROOM_API_KEY",
        "MINDROOM_LOCAL_CLIENT_SECRET",
    },
)
_SANDBOX_SUBPROCESS_SYSTEM_ENV_NAMES = frozenset(
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
        "PYTHONPATH",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TMPDIR",
        "VIRTUAL_ENV",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    },
)
_SANDBOX_SHELL_SYSTEM_ENV_NAMES = _SANDBOX_SUBPROCESS_SYSTEM_ENV_NAMES | frozenset(
    {
        "PIP_CACHE_DIR",
        "PYTHONPYCACHEPREFIX",
        "SHELL",
        "TERM",
        "USER",
        "UV_CACHE_DIR",
        "XDG_CACHE_HOME",
    },
)
_KNOWN_WORKER_CREDENTIAL_ENV_NAMES = frozenset(
    {
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_DELEGATED_USER",
        "GOOGLE_SERVICE_ACCOUNT_FILE",
        "GITHUB_TOKEN",
        *VERTEXAI_CLAUDE_ENV_BY_KEY.values(),
    },
)


def is_runtime_control_env_name(name: str) -> bool:
    """Return whether an env var is internal runtime/control-plane material."""
    return (
        name in _NON_SANDBOX_RUNTIME_CONTROL_ENV_NAMES
        or name in _CREDENTIAL_SEED_DECLARATION_ENV_NAMES
        or name.startswith(_SANDBOX_RUNTIME_ENV_PREFIX)
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
    if name in _RUNTIME_STARTUP_EXCLUDED_NAMES:
        return False
    if is_worker_backend_config_env_name(name) and name not in _WORKER_RUNTIME_STATE_ENV_NAMES:
        return False
    if is_runtime_database_url_env_name(name) or name.endswith("_FILE"):
        return False
    if name.startswith(_SANDBOX_RUNTIME_ENV_PREFIX) and name not in _PUBLIC_WORKER_SANDBOX_STARTUP_ENV_NAMES:
        return False
    if not (name.startswith(_RUNTIME_STARTUP_ENV_PREFIXES) or name in _RUNTIME_STARTUP_ENV_EXTRA_KEYS):
        return False
    return not name.endswith(_RUNTIME_STARTUP_SECRET_SUFFIXES)


def is_isolated_worker_runtime_env_name(name: str) -> bool:
    """Return whether inherited env may remain visible inside isolated workers."""
    if name in _EXECUTION_RUNTIME_EXCLUDED_NAMES and name != CREDENTIALS_ENCRYPTION_KEY_ENV:
        return False
    if is_worker_backend_config_env_name(name) and name not in _WORKER_RUNTIME_STATE_ENV_NAMES:
        return False
    if name.startswith(_SANDBOX_RUNTIME_ENV_PREFIX) and name not in _WORKER_RUNTIME_STATE_ENV_NAMES:
        return False
    if is_runtime_database_url_env_name(name):
        return False
    if not (name.startswith(_RUNTIME_STARTUP_ENV_PREFIXES) or name in _ISOLATED_RUNTIME_ENV_EXTRA_KEYS):
        return False
    return not name.endswith(_RUNTIME_STARTUP_SECRET_SUFFIXES)


def is_trusted_tool_runtime_env_file_name(name: str) -> bool:
    """Return whether a config-adjacent env value may be visible to trusted tool construction."""
    return (
        name not in _EXECUTION_RUNTIME_EXCLUDED_NAMES
        and not is_runtime_database_url_env_name(name)
        and not is_worker_backend_config_env_name(name)
    )


def is_trusted_tool_runtime_process_env_name(name: str) -> bool:
    """Return whether a process env value may be visible to trusted tool construction."""
    return name not in _EXECUTION_RUNTIME_EXCLUDED_NAMES and (
        is_public_worker_startup_env_name(name) or name in _KNOWN_WORKER_CREDENTIAL_ENV_NAMES
    )


def is_shell_passthrough_allowed_env_name(name: str) -> bool:
    """Return whether explicit shell passthrough may expose this env var."""
    return not is_runtime_control_env_name(name)


def is_worker_extra_env_name(name: str) -> bool:
    """Return whether backend extra env may be added to worker pods and startup manifests."""
    if name in _WORKER_EXTRA_ENV_GENERATED_NAMES:
        return False
    if name in _WORKER_EXTRA_ENV_SANDBOX_ENV_NAMES:
        return True
    return name not in _VENDOR_TELEMETRY_ENV_NAMES and not is_runtime_control_env_name(name)


def public_worker_startup_env(env: Mapping[str, str]) -> dict[str, str]:
    """Return the env safe to serialize into public worker startup manifests."""
    return {key: value for key, value in env.items() if is_public_worker_startup_env_name(key)}


def isolated_worker_runtime_env(env: Mapping[str, str]) -> dict[str, str]:
    """Return inherited env safe for isolated worker RuntimePaths."""
    return {key: value for key, value in env.items() if is_isolated_worker_runtime_env_name(key)}


def execution_tool_runtime_env(env: Mapping[str, str]) -> dict[str, str]:
    """Return env safe for sandboxed tool execution snapshots."""
    return {
        key: value
        for key, value in env.items()
        if is_isolated_worker_runtime_env_name(key) and key != CREDENTIALS_ENCRYPTION_KEY_ENV
    }


def sandbox_runner_startup_process_env(env: Mapping[str, str]) -> dict[str, str]:
    """Return ambient process env safe for non-dedicated sandbox runner startup rehydration."""
    return {
        key: value
        for key, value in env.items()
        if key in _SANDBOX_RUNNER_STARTUP_ENV_NAMES or not is_runtime_control_env_name(key)
    }


def sandbox_runner_runtime_state_env(env: Mapping[str, str]) -> dict[str, str]:
    """Return runner-owned runtime state that must survive request env reconstruction."""
    return {key: value for key, value in env.items() if key in _SANDBOX_RUNNER_RUNTIME_STATE_ENV_NAMES}


def worker_extra_env(env: Mapping[str, str]) -> dict[str, str]:
    """Return user-provided worker env after dropping protected runtime and backend controls."""
    return {key: value for key, value in env.items() if is_worker_extra_env_name(key)}


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


def credentials_encryption_key_value(value: str | None) -> str | None:
    """Return the configured credential encryption key, treating blank values as unset."""
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def credentials_encryption_key_from_env(env: Mapping[str, str]) -> str | None:
    """Return the credential encryption key from an env mapping."""
    return credentials_encryption_key_value(env.get(CREDENTIALS_ENCRYPTION_KEY_ENV))


def sandbox_shell_system_env(env: Mapping[str, str]) -> Mapping[str, str]:
    """Return the non-secret system env shell commands may receive by default."""
    return cast(
        "Mapping[str, str]",
        MappingProxyType({key: value for key, value in env.items() if key in _SANDBOX_SHELL_SYSTEM_ENV_NAMES}),
    )


def sandbox_subprocess_system_env(env: Mapping[str, str]) -> Mapping[str, str]:
    """Return the non-secret system env sandbox subprocesses may receive by default."""
    return cast(
        "Mapping[str, str]",
        MappingProxyType({key: value for key, value in env.items() if key in _SANDBOX_SUBPROCESS_SYSTEM_ENV_NAMES}),
    )
