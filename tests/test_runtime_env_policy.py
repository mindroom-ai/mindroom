"""Tests for centralized runtime env classification and projection."""

from __future__ import annotations

import json

from mindroom import runtime_env_policy


def test_public_worker_startup_env_excludes_control_and_secret_values() -> None:
    """Public worker startup serialization keeps only non-secret runtime values."""
    env = {
        "MINDROOM_CONFIG_PATH": "/app/config.yaml",
        "MINDROOM_STORAGE_PATH": "/app/storage",
        "MINDROOM_SANDBOX_PROXY_TOKEN": "proxy-secret",
        "MINDROOM_SANDBOX_STARTUP_MANIFEST_PATH": "/app/.runtime/startup.json",
        "MINDROOM_CREDENTIAL_SEEDS_JSON": "{}",
        "MINDROOM_API_KEY": "runtime-secret",
        "OPENAI_API_KEY": "provider-secret",
        "SERVICE_TOKEN": "service-secret",
        "APP_PASSWORD": "password",
        "DATABASE_URL": "postgres://primary",
        "MINDROOM_EVENT_CACHE_DATABASE_URL": "postgres://cache",
        "MINDROOM_KUBERNETES_WORKER_ENV_JSON": json.dumps({"OPENAI_API_KEY": "nested-secret"}),
        "MINDROOM_KUBERNETES_WORKER_LABELS_JSON": "{}",
        "MINDROOM_KUBERNETES_WORKER_ANNOTATIONS_JSON": "{}",
        "MATRIX_HOMESERVER": "https://matrix.example.invalid",
        "OPENAI_BASE_URL": "https://models.example.invalid/v1",
        "AGNO_TELEMETRY": "false",
        "POD_NAMESPACE": "mindroom",
        "CUSTOMER_ID": "customer-123",
    }

    result = runtime_env_policy.public_worker_startup_env(env)

    assert result == {
        "MINDROOM_CONFIG_PATH": "/app/config.yaml",
        "MINDROOM_STORAGE_PATH": "/app/storage",
        "MATRIX_HOMESERVER": "https://matrix.example.invalid",
        "OPENAI_BASE_URL": "https://models.example.invalid/v1",
        "AGNO_TELEMETRY": "false",
        "POD_NAMESPACE": "mindroom",
        "CUSTOMER_ID": "customer-123",
    }


def test_shell_passthrough_globs_do_not_expose_runtime_control_env() -> None:
    """Explicit broad shell passthrough still denies runtime control material."""
    env = {
        "MINDROOM_CONFIG_PATH": "/app/config.yaml",
        "MINDROOM_STORAGE_PATH": "/app/storage",
        "MINDROOM_SANDBOX_PROXY_TOKEN": "runner-secret",
        "MINDROOM_SANDBOX_RUNNER_MODE": "true",
        "MINDROOM_KUBERNETES_WORKER_IMAGE": "ghcr.io/mindroom-ai/mindroom:latest",
        "MINDROOM_KUBERNETES_WORKER_ENV_JSON": "{}",
        "MINDROOM_USER_SELECTED": "allowed",
        "PUBLIC_TOOL_VALUE": "allowed",
    }

    assert runtime_env_policy.shell_passthrough_env(env, patterns=("MINDROOM_*",)) == {
        "MINDROOM_CONFIG_PATH": "/app/config.yaml",
        "MINDROOM_STORAGE_PATH": "/app/storage",
        "MINDROOM_USER_SELECTED": "allowed",
    }
    assert runtime_env_policy.shell_passthrough_env(env, patterns=("*",)) == {
        "MINDROOM_CONFIG_PATH": "/app/config.yaml",
        "MINDROOM_STORAGE_PATH": "/app/storage",
        "MINDROOM_USER_SELECTED": "allowed",
        "PUBLIC_TOOL_VALUE": "allowed",
    }


def test_execution_runtime_env_keeps_safe_runtime_values_and_drops_runner_control() -> None:
    """Sandbox execution reconstruction uses the centralized control deny policy."""
    env = {
        "MINDROOM_CONFIG_PATH": "/app/config.yaml",
        "MINDROOM_STORAGE_PATH": "/app/storage",
        "MINDROOM_SANDBOX_PROXY_TOKEN": "runner-secret",
        "MINDROOM_SANDBOX_RUNNER_MODE": "true",
        "MINDROOM_SANDBOX_DEDICATED_WORKER_KEY": "worker-key",
        "MINDROOM_SHARED_CREDENTIALS_PATH": "/app/storage/.shared_credentials",
        "MATRIX_HOMESERVER": "https://matrix.example.invalid",
    }

    result = runtime_env_policy.sandbox_execution_runtime_env(env)

    assert "MINDROOM_SANDBOX_PROXY_TOKEN" not in result
    assert result["MINDROOM_SANDBOX_RUNNER_MODE"] == "true"
    assert result["MINDROOM_SANDBOX_DEDICATED_WORKER_KEY"] == "worker-key"
    assert result["MINDROOM_SHARED_CREDENTIALS_PATH"] == "/app/storage/.shared_credentials"
    assert result["MATRIX_HOMESERVER"] == "https://matrix.example.invalid"


def test_worker_backend_config_names_are_classified_and_excluded_from_public_startup() -> None:
    """Primary-side Kubernetes backend config env names are never public startup env."""
    backend_names = {
        "MINDROOM_KUBERNETES_WORKER_IMAGE",
        "MINDROOM_KUBERNETES_WORKER_STORAGE_PVC_NAME",
        "MINDROOM_KUBERNETES_WORKER_ENV_JSON",
        "MINDROOM_KUBERNETES_WORKER_LABELS_JSON",
        "MINDROOM_KUBERNETES_WORKER_ANNOTATIONS_JSON",
        "MINDROOM_KUBERNETES_WORKER_AUTH_SECRET_NAME",
        "MINDROOM_KUBERNETES_WORKER_OWNER_DEPLOYMENT_NAME",
        "MINDROOM_KUBERNETES_WORKER_MEMORY_REQUEST",
        "MINDROOM_KUBERNETES_WORKER_CPU_LIMIT",
    }
    env = dict.fromkeys(backend_names, "value")

    assert all(runtime_env_policy.is_worker_backend_config_env_name(name) for name in backend_names)
    assert runtime_env_policy.public_worker_startup_env(env) == {}
