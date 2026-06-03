"""Tests for centralized runtime env classification and projection."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path

from mindroom import constants, runtime_env_policy
from mindroom.api import sandbox_exec

_POLICY_OWNED_ENV_PREFIXES = (
    "MINDROOM_CREDENTIAL_SEEDS_",
    "MINDROOM_KUBERNETES_WORKER_",
    "MINDROOM_SANDBOX_",
    "MINDROOM_SHARED_CREDENTIALS_",
    "MINDROOM_WORKER_BACKEND",
)
_POLICY_OWNED_ENV_EXTRA_NAMES = frozenset(runtime_env_policy.VERTEXAI_CLAUDE_ENV_BY_KEY.values())
_PYTHON_STRING_LITERAL_RE = re.compile(
    r"""(?P<prefix>[rubfRUBF]*)?(?P<quote>["'])(?P<value>[A-Z][A-Z0-9_]+)(?P=quote)""",
)
_PROJECTION_MATRIX_ENV_NAMES = (
    runtime_env_policy.CREDENTIALS_ENCRYPTION_KEY_ENV,
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_SERVICE_ACCOUNT_FILE",
    "GOOGLE_DELEGATED_USER",
    runtime_env_policy.KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["extra_env_json"],
    runtime_env_policy.KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["storage_subpath_prefix"],
    runtime_env_policy.SANDBOX_RUNTIME_ENV_BY_KEY["proxy_token"],
    runtime_env_policy.SHARED_CREDENTIALS_PATH_ENV,
    "OPENAI_API_KEY",
)
_PROJECTION_MATRIX_EXPECTATIONS = {
    runtime_env_policy.CREDENTIALS_ENCRYPTION_KEY_ENV: {
        "public_worker_startup_env": False,
        "isolated_worker_runtime_env": True,
        "trusted_tool_runtime_paths": True,
        "execution_tool_runtime_paths": False,
        "shell_passthrough_env": False,
    },
    "GOOGLE_APPLICATION_CREDENTIALS": {
        "public_worker_startup_env": True,
        "isolated_worker_runtime_env": False,
        "trusted_tool_runtime_paths": True,
        "execution_tool_runtime_paths": False,
        "shell_passthrough_env": True,
    },
    "GOOGLE_SERVICE_ACCOUNT_FILE": {
        "public_worker_startup_env": False,
        "isolated_worker_runtime_env": False,
        "trusted_tool_runtime_paths": True,
        "execution_tool_runtime_paths": False,
        "shell_passthrough_env": True,
    },
    "GOOGLE_DELEGATED_USER": {
        "public_worker_startup_env": False,
        "isolated_worker_runtime_env": False,
        "trusted_tool_runtime_paths": True,
        "execution_tool_runtime_paths": False,
        "shell_passthrough_env": True,
    },
    runtime_env_policy.KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["extra_env_json"]: {
        "public_worker_startup_env": False,
        "isolated_worker_runtime_env": False,
        "trusted_tool_runtime_paths": False,
        "execution_tool_runtime_paths": False,
        "shell_passthrough_env": False,
    },
    runtime_env_policy.KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["storage_subpath_prefix"]: {
        "public_worker_startup_env": True,
        "isolated_worker_runtime_env": True,
        "trusted_tool_runtime_paths": True,
        "execution_tool_runtime_paths": True,
        "shell_passthrough_env": False,
    },
    runtime_env_policy.SANDBOX_RUNTIME_ENV_BY_KEY["proxy_token"]: {
        "public_worker_startup_env": False,
        "isolated_worker_runtime_env": False,
        "trusted_tool_runtime_paths": False,
        "execution_tool_runtime_paths": False,
        "shell_passthrough_env": False,
    },
    runtime_env_policy.SHARED_CREDENTIALS_PATH_ENV: {
        "public_worker_startup_env": True,
        "isolated_worker_runtime_env": True,
        "trusted_tool_runtime_paths": True,
        "execution_tool_runtime_paths": False,
        "shell_passthrough_env": True,
    },
    "OPENAI_API_KEY": {
        "public_worker_startup_env": False,
        "isolated_worker_runtime_env": False,
        "trusted_tool_runtime_paths": True,
        "execution_tool_runtime_paths": False,
        "shell_passthrough_env": True,
    },
}


def _policy_owned_env_values(value: object) -> set[str]:
    if isinstance(value, str):
        if value.startswith(_POLICY_OWNED_ENV_PREFIXES) or value in _POLICY_OWNED_ENV_EXTRA_NAMES:
            return {value}
        return set()
    if isinstance(value, Mapping):
        values: set[str] = set()
        for item in value.values():
            values.update(_policy_owned_env_values(item))
        return values
    if isinstance(value, frozenset | tuple):
        values = set()
        for item in value:
            values.update(_policy_owned_env_values(item))
        return values
    return set()


def _runtime_policy_owned_env_names() -> set[str]:
    names: set[str] = set()
    for export_name in runtime_env_policy.__all__:
        names.update(_policy_owned_env_values(getattr(runtime_env_policy, export_name)))
    return names


def test_public_worker_startup_env_excludes_control_and_secret_values() -> None:
    """Public worker startup serialization keeps only non-secret runtime values."""
    env = {
        "MINDROOM_CONFIG_PATH": "/app/config.yaml",
        "MINDROOM_STORAGE_PATH": "/app/storage",
        "MINDROOM_SANDBOX_PROXY_TOKEN": "proxy-secret",
        "MINDROOM_SANDBOX_PROXY_URL": "http://runner.example.invalid",
        "MINDROOM_SANDBOX_PROXY_TOOLS": "*",
        "MINDROOM_SANDBOX_CREDENTIAL_POLICY_JSON": '{"shell":["github"]}',
        "MINDROOM_SANDBOX_SHARED_STORAGE_ROOT": "/shared/storage",
        "MINDROOM_SANDBOX_FUTURE_CONTROL": "future-control",
        "MINDROOM_SANDBOX_STARTUP_MANIFEST_PATH": "/app/.runtime/startup.json",
        "MINDROOM_SANDBOX_RUNNER_MODE": "true",
        "MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE": "subprocess",
        "MINDROOM_SANDBOX_RUNNER_PORT": "8766",
        "MINDROOM_SANDBOX_RUNNER_SUBPROCESS_TIMEOUT_SECONDS": "45",
        "MINDROOM_SANDBOX_DEDICATED_WORKER_KEY": "worker-key",
        "MINDROOM_SANDBOX_DEDICATED_WORKER_ROOT": "/app/worker/workers/worker-key",
        "MINDROOM_KUBERNETES_WORKER_STORAGE_SUBPATH_PREFIX": "workers",
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
        "MINDROOM_SANDBOX_RUNNER_MODE": "true",
        "MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE": "subprocess",
        "MINDROOM_SANDBOX_RUNNER_PORT": "8766",
        "MINDROOM_SANDBOX_RUNNER_SUBPROCESS_TIMEOUT_SECONDS": "45",
        "MINDROOM_SANDBOX_DEDICATED_WORKER_KEY": "worker-key",
        "MINDROOM_SANDBOX_DEDICATED_WORKER_ROOT": "/app/worker/workers/worker-key",
        "MINDROOM_KUBERNETES_WORKER_STORAGE_SUBPATH_PREFIX": "workers",
    }


def test_public_runtime_paths_keep_only_worker_local_file_secret_paths(tmp_path: Path) -> None:
    """Copied worker-local file-secret paths are public only inside the worker storage root."""
    storage_root = tmp_path / "worker"
    projected_secret = storage_root / ".runtime" / "file-secrets" / "OPENAI_API_KEY_FILE" / "openai.key"
    stray_secret = tmp_path / "other" / ".runtime" / "file-secrets" / "GITHUB_TOKEN_FILE" / "token"
    runtime_paths = constants.resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=storage_root,
        process_env={
            "OPENAI_API_KEY_FILE": str(projected_secret),
            "GITHUB_TOKEN_FILE": str(stray_secret),
            "MATRIX_HOMESERVER": "https://matrix.example.invalid",
        },
    )

    assert runtime_env_policy.public_worker_startup_env(runtime_paths.process_env) == {
        "MATRIX_HOMESERVER": "https://matrix.example.invalid",
    }

    public_runtime = constants.serialize_public_runtime_paths(runtime_paths)

    assert public_runtime["process_env"] == {
        "MATRIX_HOMESERVER": "https://matrix.example.invalid",
        "OPENAI_API_KEY_FILE": str(projected_secret),
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
        "MINDROOM_KUBERNETES_WORKER_STORAGE_SUBPATH_PREFIX": "workers",
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


def test_env_projection_matrix_documents_sensitive_runtime_boundaries(tmp_path: Path) -> None:
    """Sensitive and credential-adjacent env names should have one documented projection policy."""
    process_env = {name: f"process:{name}" for name in _PROJECTION_MATRIX_ENV_NAMES}
    env_file_values = {name: f"envfile:{name}" for name in _PROJECTION_MATRIX_ENV_NAMES}
    config_path = tmp_path / "config.yaml"
    runtime_paths = constants.RuntimePaths(
        config_path=config_path,
        config_dir=tmp_path,
        env_path=tmp_path / ".env",
        storage_root=tmp_path / "storage",
        process_env=process_env,
        env_file_values=env_file_values,
    )
    trusted_tool_runtime_paths = sandbox_exec.tool_runtime_paths_with_request_env(
        runtime_paths,
        {},
        include_base_execution_env=True,
        include_credentials_encryption_key=True,
    )
    execution_tool_runtime_paths = sandbox_exec.tool_runtime_paths_with_request_env(
        runtime_paths,
        {},
        include_base_execution_env=False,
        include_credentials_encryption_key=False,
    )
    public_worker_startup_env = runtime_env_policy.public_worker_startup_env(process_env)
    isolated_worker_runtime_env = runtime_env_policy.isolated_worker_runtime_env(process_env)
    shell_passthrough_env = runtime_env_policy.shell_passthrough_env(process_env, patterns=("*",))

    actual = {
        name: {
            "public_worker_startup_env": name in public_worker_startup_env,
            "isolated_worker_runtime_env": name in isolated_worker_runtime_env,
            "trusted_tool_runtime_paths": trusted_tool_runtime_paths.env_value(name) is not None,
            "execution_tool_runtime_paths": execution_tool_runtime_paths.env_value(name) is not None,
            "shell_passthrough_env": name in shell_passthrough_env,
        }
        for name in _PROJECTION_MATRIX_ENV_NAMES
    }

    assert actual == _PROJECTION_MATRIX_EXPECTATIONS


def test_execution_runtime_env_keeps_safe_runtime_values_and_drops_runner_control() -> None:
    """Sandbox execution reconstruction uses the centralized control deny policy."""
    env = {
        "MINDROOM_CONFIG_PATH": "/app/config.yaml",
        "MINDROOM_STORAGE_PATH": "/app/storage",
        "MINDROOM_SANDBOX_PROXY_TOKEN": "runner-secret",
        "MINDROOM_SANDBOX_RUNNER_MODE": "true",
        "MINDROOM_SANDBOX_RUNNER_SUBPROCESS_TIMEOUT_SECONDS": "45",
        "MINDROOM_SANDBOX_DEDICATED_WORKER_KEY": "worker-key",
        "MINDROOM_SANDBOX_CREDENTIAL_POLICY_JSON": '{"shell":["github"]}',
        "MINDROOM_SANDBOX_FUTURE_CONTROL": "future-control",
        "MINDROOM_SHARED_CREDENTIALS_PATH": "/app/storage/.shared_credentials",
        "MATRIX_HOMESERVER": "https://matrix.example.invalid",
    }

    result = runtime_env_policy.isolated_worker_runtime_env(env)

    assert "MINDROOM_SANDBOX_PROXY_TOKEN" not in result
    assert "MINDROOM_SANDBOX_CREDENTIAL_POLICY_JSON" not in result
    assert "MINDROOM_SANDBOX_FUTURE_CONTROL" not in result
    assert result["MINDROOM_SANDBOX_RUNNER_MODE"] == "true"
    assert result["MINDROOM_SANDBOX_RUNNER_SUBPROCESS_TIMEOUT_SECONDS"] == "45"
    assert result["MINDROOM_SANDBOX_DEDICATED_WORKER_KEY"] == "worker-key"
    assert result["MINDROOM_SHARED_CREDENTIALS_PATH"] == "/app/storage/.shared_credentials"
    assert result["MATRIX_HOMESERVER"] == "https://matrix.example.invalid"


def test_sandbox_runner_startup_process_env_keeps_ambient_values_and_drops_control() -> None:
    """Non-dedicated runner startup rehydration preserves ambient env without control material."""
    env = {
        "TEST_EXECUTION_ENV": "worker-visible",
        "MINDROOM_NAMESPACE": "alpha1234",
        "MINDROOM_SANDBOX_PROXY_TOKEN": "runner-secret",
        "MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE": "subprocess",
        "MINDROOM_SANDBOX_RUNNER_MODE": "true",
        "MINDROOM_SANDBOX_RUNNER_PORT": "8766",
        "MINDROOM_SANDBOX_RUNNER_SUBPROCESS_TIMEOUT_SECONDS": "9",
        "MINDROOM_SANDBOX_SHARED_STORAGE_ROOT": "/shared/storage",
        "MINDROOM_SANDBOX_WORKER_ENDPOINT": "/api/sandbox-runner",
        "MINDROOM_SANDBOX_WORKER_IDLE_TIMEOUT_SECONDS": "60",
        "MINDROOM_SANDBOX_STARTUP_MANIFEST_PATH": "/app/.runtime/startup.json",
        "MINDROOM_KUBERNETES_WORKER_ENV_JSON": json.dumps({"OPENAI_API_KEY": "nested-secret"}),
        "MINDROOM_KUBERNETES_WORKER_STORAGE_SUBPATH_PREFIX": "workers",
    }

    result = runtime_env_policy.sandbox_runner_startup_process_env(env)

    assert result == {
        "TEST_EXECUTION_ENV": "worker-visible",
        "MINDROOM_NAMESPACE": "alpha1234",
        "MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE": "subprocess",
        "MINDROOM_SANDBOX_RUNNER_MODE": "true",
        "MINDROOM_SANDBOX_RUNNER_PORT": "8766",
        "MINDROOM_SANDBOX_RUNNER_SUBPROCESS_TIMEOUT_SECONDS": "9",
        "MINDROOM_SANDBOX_SHARED_STORAGE_ROOT": "/shared/storage",
        "MINDROOM_SANDBOX_WORKER_ENDPOINT": "/api/sandbox-runner",
        "MINDROOM_SANDBOX_WORKER_IDLE_TIMEOUT_SECONDS": "60",
    }


def test_sandbox_runner_runtime_state_keeps_dedicated_storage_subpath_prefix() -> None:
    """Dedicated runner subprocesses keep the worker storage prefix needed to resolve shared roots."""
    env = {
        "MINDROOM_SANDBOX_DEDICATED_WORKER_KEY": "v1:tenant-123:shared:general",
        "MINDROOM_SANDBOX_DEDICATED_WORKER_ROOT": "/shared/nested/workers/worker-dir",
        "MINDROOM_KUBERNETES_WORKER_STORAGE_SUBPATH_PREFIX": "nested/workers",
        "MINDROOM_KUBERNETES_WORKER_ENV_JSON": json.dumps({"OPENAI_API_KEY": "nested-secret"}),
        "MINDROOM_SANDBOX_PROXY_TOKEN": "runner-secret",
    }

    assert runtime_env_policy.sandbox_runner_runtime_state_env(env) == {
        "MINDROOM_SANDBOX_DEDICATED_WORKER_KEY": "v1:tenant-123:shared:general",
        "MINDROOM_SANDBOX_DEDICATED_WORKER_ROOT": "/shared/nested/workers/worker-dir",
        "MINDROOM_KUBERNETES_WORKER_STORAGE_SUBPATH_PREFIX": "nested/workers",
    }


def test_worker_backend_config_names_are_classified_and_filter_public_startup() -> None:
    """Only worker runtime state survives public startup filtering for backend config env names."""
    backend_names = runtime_env_policy.KUBERNETES_WORKER_BACKEND_CONFIG_ENV_NAMES
    env = dict.fromkeys(backend_names, "value")

    assert all(runtime_env_policy.is_worker_backend_config_env_name(name) for name in backend_names)
    assert runtime_env_policy.public_worker_startup_env(env) == {
        "MINDROOM_KUBERNETES_WORKER_STORAGE_SUBPATH_PREFIX": "value",
    }
    assert runtime_env_policy.shell_passthrough_env(env, patterns=("*",)) == {}
    assert not any(runtime_env_policy.is_trusted_tool_runtime_env_file_name(name) for name in backend_names)


def test_sandbox_subprocess_system_env_uses_policy_allowlist() -> None:
    """Subprocess host env passthrough is centralized with the runtime env policy."""
    env = {
        "PATH": "/usr/bin",
        "PYTHONPATH": "/app/src",
        "HTTP_PROXY": "http://proxy.example.invalid",
        "TERM": "xterm-256color",
        "OPENAI_API_KEY": "provider-secret",
        "MINDROOM_SANDBOX_PROXY_TOKEN": "runner-secret",
        "MINDROOM_KUBERNETES_WORKER_ENV_JSON": json.dumps({"OPENAI_API_KEY": "nested-secret"}),
    }

    assert runtime_env_policy.sandbox_subprocess_system_env(env) == {
        "PATH": "/usr/bin",
        "PYTHONPATH": "/app/src",
        "HTTP_PROXY": "http://proxy.example.invalid",
    }


def test_worker_runtime_state_can_reintroduce_storage_subpath_after_backend_filtering() -> None:
    """Storage subpath is backend config when inherited, but explicit worker runtime state when re-added."""
    env = {
        "MINDROOM_KUBERNETES_WORKER_STORAGE_SUBPATH_PREFIX": "workers",
        "MINDROOM_KUBERNETES_WORKER_IMAGE": "ghcr.io/mindroom-ai/mindroom:latest",
    }

    assert runtime_env_policy.is_worker_backend_config_env_name("MINDROOM_KUBERNETES_WORKER_STORAGE_SUBPATH_PREFIX")
    assert runtime_env_policy.is_isolated_worker_runtime_env_name(
        "MINDROOM_KUBERNETES_WORKER_STORAGE_SUBPATH_PREFIX",
    )
    assert runtime_env_policy.isolated_worker_runtime_env(env) == {
        "MINDROOM_KUBERNETES_WORKER_STORAGE_SUBPATH_PREFIX": "workers",
    }


def test_worker_extra_env_drops_protected_controls_but_keeps_runner_timeout() -> None:
    """Kubernetes extra env may tune runner timeout without overriding generated worker controls."""
    env = {
        "HOME": "/unsafe/home",
        "MINDROOM_API_KEY": "runtime-api-key",
        "MINDROOM_CONFIG_PATH": "/unsafe/config.yaml",
        "MINDROOM_LOCAL_CLIENT_SECRET": "runtime-client-secret",
        "MINDROOM_SHARED_CREDENTIALS_PATH": "/unsafe/shared-credentials",
        "MINDROOM_STORAGE_PATH": "/unsafe/storage",
        "MINDROOM_SANDBOX_RUNNER_SUBPROCESS_TIMEOUT_SECONDS": "45",
        "MINDROOM_SANDBOX_DEDICATED_WORKER_ROOT": "/unsafe/root",
        "MINDROOM_SANDBOX_PROXY_TOKEN": "unsafe-token",
        "MINDROOM_SANDBOX_STARTUP_MANIFEST_PATH": "/unsafe/startup.json",
        "MINDROOM_KUBERNETES_WORKER_ENV_JSON": json.dumps({"MINDROOM_SANDBOX_PROXY_TOKEN": "nested-token"}),
        "MINDROOM_KUBERNETES_WORKER_AUTH_SECRET_NAME": "primary-auth-secret",
        "AGNO_TELEMETRY": "true",
        "PATH": "/unsafe/bin",
        "VIRTUAL_ENV": "/unsafe/venv",
        "MINDROOM_WORKER_TOOL_VALUE": "visible",
    }

    assert runtime_env_policy.worker_extra_env(env) == {
        "MINDROOM_SANDBOX_RUNNER_SUBPROCESS_TIMEOUT_SECONDS": "45",
        "MINDROOM_WORKER_TOOL_VALUE": "visible",
    }


def test_runtime_control_env_literals_stay_in_policy_module() -> None:
    """Python callers should import centralized runtime env names from the policy module."""
    source_root = Path(__file__).resolve().parents[1] / "src" / "mindroom"
    allowed_path = source_root / "runtime_env_policy.py"
    policy_owned_env_names = _runtime_policy_owned_env_names()

    violations: dict[str, list[str]] = {}
    for path in source_root.rglob("*.py"):
        if path == allowed_path:
            continue
        text = path.read_text(encoding="utf-8")
        literal_names = {match.group("value") for match in _PYTHON_STRING_LITERAL_RE.finditer(text)}
        leaked_names = sorted(policy_owned_env_names & literal_names)
        if leaked_names:
            violations[str(path.relative_to(source_root))] = leaked_names

    assert violations == {}
