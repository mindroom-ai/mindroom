"""Tests for sandbox runner API endpoints."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

import mindroom.api.sandbox_exec as sandbox_exec_module
import mindroom.api.sandbox_protocol as sandbox_protocol_module
import mindroom.api.sandbox_runner as sandbox_runner_module
import mindroom.api.sandbox_worker_prep as sandbox_worker_prep_module
import mindroom.credentials as credentials_module
import mindroom.tool_system.metadata as metadata_module
from mindroom.api.sandbox_runner_app import app as sandbox_runner_app
from mindroom.config.main import runtime_private_agent_names
from mindroom.constants import (
    resolve_primary_runtime_paths,
    resolve_runtime_paths,
    serialize_public_runtime_paths,
    serialize_runtime_paths,
)
from mindroom.credentials import CredentialsManager, get_runtime_credentials_manager, save_scoped_credentials
from mindroom.tool_system.metadata import (
    TOOL_METADATA,
    ConfigField,
    SetupType,
    ToolCategory,
    ToolMetadata,
    ToolStatus,
    ensure_tool_registry_loaded,
    get_tool_by_name,
)
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    agent_workspace_root_path,
    private_instance_scope_root_path,
    resolve_worker_key,
    worker_dir_name,
)
from mindroom.workers.backends import local as local_workers_module
from mindroom.workers.models import WorkerSpec

if TYPE_CHECKING:
    from collections.abc import Iterator

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

SANDBOX_TOKEN = "secret-token"  # noqa: S105
SANDBOX_HEADERS = {"x-mindroom-sandbox-token": SANDBOX_TOKEN}
REQUIRES_LINUX_LOCAL_WORKER = pytest.mark.skipif(
    sys.platform != "linux",
    reason="local worker venv bootstrap is validated on Linux",
)


@pytest.fixture(autouse=True)
def _load_tools() -> None:
    ensure_tool_registry_loaded(resolve_runtime_paths(config_path=Path("config.yaml")))


@pytest.fixture(autouse=True)
def _reset_worker_manager(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(local_workers_module, "_local_worker_manager", None)
    monkeypatch.setattr(local_workers_module, "_local_worker_manager_config", None)
    monkeypatch.delenv("MINDROOM_SANDBOX_WORKER_ROOT", raising=False)
    monkeypatch.setenv("MINDROOM_STORAGE_PATH", str(tmp_path / ".mindroom"))


@pytest.fixture
def runner_client() -> Iterator[TestClient]:
    """Create a test client for the sandbox runner app."""

    class _RuntimeRefreshingTestClient(TestClient):
        def request(
            self,
            method: str,
            url: httpx._types.URLTypes,
            *,
            content: httpx._types.RequestContent | None = None,
            data: httpx._types.RequestData | None = None,
            files: httpx._types.RequestFiles | None = None,
            json: object | None = None,
            params: httpx._types.QueryParamTypes | None = None,
            headers: httpx._types.HeaderTypes | None = None,
            cookies: httpx._types.CookieTypes | None = None,
            auth: httpx._types.AuthTypes | httpx._client.UseClientDefault = httpx._client.USE_CLIENT_DEFAULT,
            follow_redirects: bool | httpx._client.UseClientDefault = httpx._client.USE_CLIENT_DEFAULT,
            timeout: httpx._types.TimeoutTypes | httpx._client.UseClientDefault = httpx._client.USE_CLIENT_DEFAULT,
            extensions: dict[str, object] | None = None,
        ) -> httpx.Response:
            _refresh_runner_app_from_env()
            return super().request(
                method,
                url,
                content=content,
                data=data,
                files=files,
                json=json,
                params=params,
                headers=headers,
                cookies=cookies,
                auth=auth,
                follow_redirects=follow_redirects,
                timeout=timeout,
                extensions=extensions,
            )

    _refresh_runner_app_from_env()
    with _RuntimeRefreshingTestClient(sandbox_runner_app) as client:
        yield client


def _set_sandbox_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the sandbox token through the runner's explicit runtime env boundary."""
    monkeypatch.setenv("MINDROOM_SANDBOX_PROXY_TOKEN", SANDBOX_TOKEN)


def _refresh_runner_app_from_env() -> tuple[RuntimePaths, Config]:
    runtime_paths = resolve_primary_runtime_paths(process_env=dict(os.environ))
    config = sandbox_runner_module._runtime_config_or_empty(runtime_paths)
    sandbox_runner_module.initialize_sandbox_runner_app(sandbox_runner_app, runtime_paths)
    sandbox_runner_module.ensure_registry_loaded_with_config(runtime_paths, config)
    return runtime_paths, config


def test_startup_runtime_keeps_runner_token_outside_runtime_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Startup auth token should stay separate from the committed runtime payload."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    payload_runtime = resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={"MINDROOM_NAMESPACE": "alpha1234"},
    )
    monkeypatch.setenv("MINDROOM_RUNTIME_PATHS_JSON", json.dumps(serialize_runtime_paths(payload_runtime)))
    monkeypatch.setenv("MINDROOM_SANDBOX_PROXY_TOKEN", "from-env")

    startup_runtime = sandbox_runner_module._startup_runtime_paths_from_env()
    sandbox_runner_module.initialize_sandbox_runner_app(
        sandbox_runner_app,
        startup_runtime,
        runner_token=sandbox_runner_module._startup_runner_token_from_env(),
    )

    assert startup_runtime.env_value("MINDROOM_SANDBOX_PROXY_TOKEN") is None
    assert sandbox_runner_module._app_runner_token(sandbox_runner_app) == "from-env"


def test_startup_runtime_rehydrates_runtime_env_from_process_env_and_dotenv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Startup runtime should recover trusted env from real process env while keeping runner auth separate."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("OPENAI_API_KEY=dotenv-secret\n", encoding="utf-8")
    payload_runtime = resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={"MINDROOM_NAMESPACE": "alpha1234"},
    )
    monkeypatch.setenv("MINDROOM_RUNTIME_PATHS_JSON", json.dumps(serialize_public_runtime_paths(payload_runtime)))
    monkeypatch.setenv("MINDROOM_SANDBOX_PROXY_TOKEN", "from-env")
    monkeypatch.setenv("TEST_EXECUTION_ENV", "worker-visible")

    startup_runtime = sandbox_runner_module._startup_runtime_paths_from_env()

    assert startup_runtime.env_value("MINDROOM_NAMESPACE") == "alpha1234"
    assert startup_runtime.env_value("OPENAI_API_KEY") == "dotenv-secret"
    assert startup_runtime.env_value("TEST_EXECUTION_ENV") == "worker-visible"
    assert startup_runtime.env_value("MINDROOM_SANDBOX_PROXY_TOKEN") is None


def test_public_startup_runtime_payload_excludes_runner_token(tmp_path: Path) -> None:
    """Public startup runtime payloads should not serialize the runner auth token."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("models: {}\nagents: {}\n", encoding="utf-8")
    runtime_paths = resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={
            "MINDROOM_SANDBOX_PROXY_TOKEN": "secret-token",
            "MINDROOM_NAMESPACE": "alpha1234",
        },
    )

    payload = serialize_public_runtime_paths(runtime_paths)

    assert payload["process_env"] == {
        "MINDROOM_CONFIG_PATH": str(config_path.resolve()),
        "MINDROOM_NAMESPACE": "alpha1234",
        "MINDROOM_STORAGE_PATH": str((tmp_path / "storage").resolve()),
    }


def test_public_startup_runtime_still_allows_python_execution_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Public startup payloads may stay secret-free while Python execution receives explicit env per request."""
    _set_sandbox_token(monkeypatch)
    child_runtime = sandbox_runner_module.constants.deserialize_runtime_paths(
        serialize_public_runtime_paths(
            resolve_primary_runtime_paths(
                config_path=tmp_path / "config.yaml",
                storage_path=tmp_path / "storage",
                process_env={"MINDROOM_NAMESPACE": "alpha1234"},
            ),
        ),
    )
    child_runtime.config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )

    response = sandbox_runner_module._execute_request_subprocess_sync(
        sandbox_runner_module.SandboxRunnerExecuteRequest(
            tool_name="python",
            function_name="run_python_code",
            args=[
                'import os\nresult = {"api_key": os.environ.get("OPENAI_API_KEY"), "test": os.environ.get("TEST_EXECUTION_ENV")}',
                "result",
            ],
            execution_env={"OPENAI_API_KEY": "sk-secret", "TEST_EXECUTION_ENV": "visible"},
        ),
        child_runtime,
        sandbox_runner_module._runtime_config_or_empty(child_runtime),
        runner_token=SANDBOX_TOKEN,
    )

    assert response.ok is True
    assert ast.literal_eval(str(response.result)) == {
        "api_key": "sk-secret",
        "test": "visible",
    }


def test_subprocess_runtime_payload_preserves_parent_env_file_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Subprocess execution should receive the exact runtime context the parent resolved."""
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    (config_dir / ".env").write_text(
        "MINDROOM_NAMESPACE=alpha1234\nMATRIX_HOMESERVER=http://dotenv-hs\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_primary_runtime_paths(config_path=config_path)
    captured_payload: dict[str, object] = {}

    def fake_run(
        cmd: list[str],
        **run_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        del cmd
        envelope = json.loads(str(run_kwargs["input"]))
        captured_payload.update(envelope["runtime_paths"])
        response = sandbox_runner_module.SandboxRunnerExecuteResponse(ok=True, result="ok")
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="",
            stderr=sandbox_protocol_module.RESPONSE_MARKER + response.model_dump_json(),
        )

    monkeypatch.setattr(sandbox_runner_module.subprocess, "run", fake_run)

    response = sandbox_runner_module._execute_request_subprocess_sync(
        sandbox_runner_module.SandboxRunnerExecuteRequest(
            tool_name="calculator",
            function_name="add",
            args=[1, 2],
            kwargs={},
        ),
        runtime_paths,
        sandbox_runner_module._runtime_config_or_empty(runtime_paths),
    )
    child_runtime = sandbox_runner_module.constants.deserialize_runtime_paths(captured_payload)

    assert response.ok is True
    assert child_runtime.env_file_values["MINDROOM_NAMESPACE"] == "alpha1234"
    assert child_runtime.env_value("MINDROOM_NAMESPACE") == "alpha1234"
    assert child_runtime.env_value("MATRIX_HOMESERVER") == "http://dotenv-hs"


def test_resolve_entrypoint_builds_clickup_from_scoped_credentials(tmp_path: Path) -> None:
    """Sandbox-side tool rebuilds should use persisted tool credentials."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("models: {}\nagents: {}\n", encoding="utf-8")
    runtime_paths = resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={},
    )
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    save_scoped_credentials(
        "clickup",
        {"api_key": "clickup-test", "master_space_id": "space-123"},
        credentials_manager=credentials_manager,
    )

    toolkit, entrypoint = sandbox_runner_module._resolve_entrypoint(
        runtime_paths=runtime_paths,
        config=sandbox_runner_module._runtime_config_or_empty(runtime_paths),
        tool_name="clickup",
        function_name="list_spaces",
    )

    assert toolkit.api_key == "clickup-test"
    assert toolkit.master_space_id == "space-123"
    assert entrypoint is not None


def test_sandbox_runner_subprocess_python_sees_runtime_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Sandbox subprocess execution should expose runtime-scoped env values to the child tool."""
    _set_sandbox_token(monkeypatch)
    monkeypatch.setenv("MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE", "subprocess")
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    (config_dir / ".env").write_text("MINDROOM_NAMESPACE=alpha1234\n", encoding="utf-8")
    runtime_paths = resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={"OPENAI_BASE_URL": "http://example.invalid/v1"},
    )

    response = sandbox_runner_module._execute_request_subprocess_sync(
        sandbox_runner_module.SandboxRunnerExecuteRequest(
            tool_name="python",
            function_name="run_python_code",
            args=[
                'import os\nresult = {"openai_base_url": os.environ.get("OPENAI_BASE_URL"), "namespace": os.environ.get("MINDROOM_NAMESPACE"), "storage": os.environ.get("MINDROOM_STORAGE_PATH")}',
                "result",
            ],
            kwargs={},
        ),
        runtime_paths,
        sandbox_runner_module._runtime_config_or_empty(runtime_paths),
        runner_token=SANDBOX_TOKEN,
    )

    assert response.ok is True
    assert ast.literal_eval(str(response.result)) == {
        "openai_base_url": "http://example.invalid/v1",
        "namespace": "alpha1234",
        "storage": str((tmp_path / "storage").resolve()),
    }


def test_sandbox_runner_subprocess_shell_sees_runtime_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Sandbox subprocess shell execution should inherit committed runtime env values without tool-env fallback logic."""
    _set_sandbox_token(monkeypatch)
    monkeypatch.setenv("MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE", "subprocess")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("TEST_EXECUTION_ENV=visible-in-shell\n", encoding="utf-8")
    runtime_paths = resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={},
    )

    response = sandbox_runner_module._execute_request_subprocess_sync(
        sandbox_runner_module.SandboxRunnerExecuteRequest(
            tool_name="shell",
            function_name="run_shell_command",
            args=[["bash", "-lc", "printf '%s' \"$TEST_EXECUTION_ENV\""]],
            kwargs={},
        ),
        runtime_paths,
        sandbox_runner_module._runtime_config_or_empty(runtime_paths),
        runner_token=SANDBOX_TOKEN,
    )

    assert response.ok is True
    assert response.result == "visible-in-shell"


def test_sandbox_runner_execution_env_excludes_runner_token_and_unrelated_host_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Execution env should carry committed runtime values without leaking control secrets or arbitrary host env."""
    _set_sandbox_token(monkeypatch)
    monkeypatch.setenv("CI_JOB_TOKEN", "ci-secret")
    monkeypatch.setenv("MINDROOM_API_KEY", "dashboard-secret")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "OPENAI_API_KEY=dotenv-secret\nTEST_EXECUTION_ENV=visible-in-shell\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env=dict(os.environ),
    )

    execution_env = sandbox_exec_module.request_execution_env(
        "shell",
        None,
        runtime_paths,
    )

    assert execution_env["OPENAI_API_KEY"] == "dotenv-secret"
    assert execution_env["TEST_EXECUTION_ENV"] == "visible-in-shell"
    assert execution_env["MINDROOM_STORAGE_PATH"] == str((tmp_path / "storage").resolve())
    assert "MINDROOM_SANDBOX_PROXY_TOKEN" not in execution_env
    assert "MINDROOM_API_KEY" not in execution_env
    assert "CI_JOB_TOKEN" not in execution_env


def test_worker_subprocess_env_preserves_parent_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Worker subprocesses should keep PATH without re-exporting tool config env vars."""
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin:/bin")
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.yaml"
    credentials_path = tmp_path / "google-credentials.json"
    config_path.write_text("models: {}\nagents: {}\n", encoding="utf-8")
    (config_dir / ".env").write_text(
        "GOOGLE_CLOUD_PROJECT=demo-project\n"
        "GOOGLE_CLOUD_LOCATION=us-central1\n"
        f"GOOGLE_APPLICATION_CREDENTIALS={credentials_path}\n",
        encoding="utf-8",
    )
    paths = local_workers_module.local_worker_state_paths_for_root(tmp_path / "worker")

    env = sandbox_exec_module.worker_subprocess_env(paths)

    assert env["PATH"].startswith(f"{paths.venv_dir}/bin:")
    assert env["PATH"].endswith("/usr/local/bin:/usr/bin:/bin")
    assert "GOOGLE_CLOUD_PROJECT" not in env
    assert "GOOGLE_CLOUD_LOCATION" not in env
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in env


def test_sandbox_runner_executes_tool_call(runner_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sandbox runner should execute tool calls and return their result."""
    _set_sandbox_token(monkeypatch)
    response = runner_client.post(
        "/api/sandbox-runner/execute",
        headers=SANDBOX_HEADERS,
        json={
            "tool_name": "calculator",
            "function_name": "add",
            "args": [1, 2],
            "kwargs": {},
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert '"result": 3' in data["result"]


def test_sandbox_runner_applies_tool_init_overrides(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Sandbox runner should instantiate tools with forwarded non-secret init overrides."""
    _set_sandbox_token(monkeypatch)
    workspace = tmp_path / "mind_data"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "USER.md").write_text("Bas\n", encoding="utf-8")

    response = runner_client.post(
        "/api/sandbox-runner/execute",
        headers=SANDBOX_HEADERS,
        json={
            "tool_name": "coding",
            "function_name": "ls",
            "args": [],
            "kwargs": {"path": "."},
            "tool_init_overrides": {"base_dir": str(workspace)},
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert "USER.md" in data["result"]


def test_resolve_entrypoint_loads_persisted_tool_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Sandbox toolkit rebuilds should load persisted credentials from runner storage."""

    class DummyTool:
        def __init__(self, token: str | None = None) -> None:
            self.name = "dummy"
            self.token = token
            self.functions = {"run": type("F", (), {"entrypoint": lambda _unused: None})()}
            self.async_functions = {}
            self.requires_connect = False

    tool_name = "dummy_cred_tool"
    stored_value = "value123"
    original_registry = metadata_module._TOOL_REGISTRY.copy()
    original_metadata = TOOL_METADATA.copy()
    original_manager = credentials_module._credentials_manager
    original_signature = credentials_module._credentials_manager_signature
    shared_storage = tmp_path / "shared-storage"
    monkeypatch.setenv("MINDROOM_STORAGE_PATH", str(shared_storage))
    metadata_module._TOOL_REGISTRY[tool_name] = lambda: DummyTool
    TOOL_METADATA[tool_name] = ToolMetadata(
        name=tool_name,
        display_name="Dummy",
        description="Dummy",
        category=ToolCategory.DEVELOPMENT,
        status=ToolStatus.REQUIRES_CONFIG,
        setup_type=SetupType.API_KEY,
        config_fields=[ConfigField(name="token", label="Token", type="password", required=False)],
    )

    try:
        CredentialsManager(base_path=shared_storage / "credentials").save_credentials(
            tool_name,
            {"token": stored_value},
        )
        credentials_module._credentials_manager = None
        credentials_module._credentials_manager_signature = None

        runtime_paths, config = _refresh_runner_app_from_env()
        toolkit, _ = sandbox_runner_module._resolve_entrypoint(
            runtime_paths=runtime_paths,
            config=config,
            tool_name=tool_name,
            function_name="run",
        )

        assert toolkit.token == stored_value
    finally:
        metadata_module._TOOL_REGISTRY.clear()
        metadata_module._TOOL_REGISTRY.update(original_registry)
        TOOL_METADATA.clear()
        TOOL_METADATA.update(original_metadata)
        credentials_module._credentials_manager = original_manager
        credentials_module._credentials_manager_signature = original_signature


def test_get_tool_by_name_loads_persisted_tool_credentials_without_explicit_manager(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Config-backed tool rebuilds should use the runtime credential store by default."""

    class DummyTool:
        def __init__(self, token: str | None = None) -> None:
            self.name = "dummy"
            self.token = token
            self.functions = {"run": type("F", (), {"entrypoint": lambda _unused: None})()}
            self.async_functions = {}
            self.requires_connect = False

    tool_name = "dummy_runtime_cred_tool"
    stored_value = "value123"
    original_registry = metadata_module._TOOL_REGISTRY.copy()
    original_metadata = TOOL_METADATA.copy()
    original_manager = credentials_module._credentials_manager
    original_signature = credentials_module._credentials_manager_signature
    storage_root = tmp_path / "runtime-storage"
    monkeypatch.setenv("MINDROOM_STORAGE_PATH", str(storage_root))
    metadata_module._TOOL_REGISTRY[tool_name] = lambda: DummyTool
    TOOL_METADATA[tool_name] = ToolMetadata(
        name=tool_name,
        display_name="Dummy",
        description="Dummy",
        category=ToolCategory.DEVELOPMENT,
        status=ToolStatus.REQUIRES_CONFIG,
        setup_type=SetupType.API_KEY,
        config_fields=[ConfigField(name="token", label="Token", type="password", required=False)],
    )

    try:
        CredentialsManager(base_path=storage_root / "credentials").save_credentials(
            tool_name,
            {"token": stored_value},
        )
        credentials_module._credentials_manager = None
        credentials_module._credentials_manager_signature = None

        toolkit = get_tool_by_name(tool_name, resolve_runtime_paths(config_path=Path("config.yaml")))

        assert toolkit.token == stored_value
    finally:
        metadata_module._TOOL_REGISTRY.clear()
        metadata_module._TOOL_REGISTRY.update(original_registry)
        TOOL_METADATA.clear()
        TOOL_METADATA.update(original_metadata)
        credentials_module._credentials_manager = original_manager
        credentials_module._credentials_manager_signature = original_signature


def test_resolve_worker_base_dir_does_not_create_directories_during_validation(tmp_path: Path) -> None:
    """Worker base-dir validation should not leave empty directories behind."""
    storage_root = tmp_path / "mindroom_data"
    worker_root = tmp_path / "workers" / "worker-state"
    requested_base_dir = "agents/general/workspace/mind_data"

    resolved = sandbox_worker_prep_module.resolve_worker_base_dir(
        SimpleNamespace(root=worker_root, workspace=worker_root / "workspace"),
        storage_root,
        "v1:default:shared:general",
        requested_base_dir,
    )

    assert resolved == (storage_root / requested_base_dir).resolve()
    assert not resolved.exists()


def test_sandbox_runner_healthz(runner_client: TestClient) -> None:
    """Sandbox runner should expose a minimal unauthenticated health endpoint."""
    response = runner_client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_sandbox_runner_executes_tool_call_in_subprocess_mode(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sandbox runner should optionally execute tool calls in a subprocess."""
    _set_sandbox_token(monkeypatch)
    monkeypatch.setenv("MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE", "subprocess")
    response = runner_client.post(
        "/api/sandbox-runner/execute",
        headers=SANDBOX_HEADERS,
        json={
            "tool_name": "calculator",
            "function_name": "add",
            "args": [1, 2],
            "kwargs": {},
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert '"result": 3' in data["result"]


def test_sandbox_runner_rejects_missing_token(runner_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sandbox runner should require the shared token when configured."""
    _set_sandbox_token(monkeypatch)
    response = runner_client.post(
        "/api/sandbox-runner/execute",
        json={
            "tool_name": "calculator",
            "function_name": "add",
            "args": [1, 2],
            "kwargs": {},
        },
    )
    assert response.status_code == 401

    authed_response = runner_client.post(
        "/api/sandbox-runner/execute",
        headers=SANDBOX_HEADERS,
        json={
            "tool_name": "calculator",
            "function_name": "add",
            "args": [1, 2],
            "kwargs": {},
        },
    )
    assert authed_response.status_code == 200
    authed_data = authed_response.json()
    assert authed_data["ok"] is True


def test_sandbox_runner_rejects_when_token_not_configured(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sandbox runner should fail closed when no token is configured."""
    monkeypatch.delenv("MINDROOM_SANDBOX_PROXY_TOKEN", raising=False)
    response = runner_client.post(
        "/api/sandbox-runner/execute",
        headers=SANDBOX_HEADERS,
        json={
            "tool_name": "calculator",
            "function_name": "add",
            "args": [1, 2],
            "kwargs": {},
        },
    )
    assert response.status_code == 503
    assert "not configured" in response.json()["detail"]


def test_sandbox_runner_rejects_direct_credential_overrides(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Credential overrides must come from a lease, not the execute request payload."""
    _set_sandbox_token(monkeypatch)

    response = runner_client.post(
        "/api/sandbox-runner/execute",
        headers=SANDBOX_HEADERS,
        json={
            "tool_name": "calculator",
            "function_name": "add",
            "args": [2, 3],
            "kwargs": {},
            "credential_overrides": {"OPENAI_API_KEY": "test-key"},
        },
    )

    assert response.status_code == 400
    assert "lease_id" in response.json()["detail"]


def test_sandbox_runner_rejects_execution_env_for_non_execution_tools(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit execution env is only supported for shell/python execution tools."""
    _set_sandbox_token(monkeypatch)

    response = runner_client.post(
        "/api/sandbox-runner/execute",
        headers=SANDBOX_HEADERS,
        json={
            "tool_name": "calculator",
            "function_name": "add",
            "args": [2, 3],
            "kwargs": {},
            "execution_env": {"TEST_EXECUTION_ENV": "visible"},
        },
    )

    assert response.status_code == 400
    assert "execution tools" in response.json()["detail"]


def test_sandbox_runner_rejects_unsafe_tool_init_overrides(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tool init overrides should reject non-whitelisted config fields."""
    _set_sandbox_token(monkeypatch)

    response = runner_client.post(
        "/api/sandbox-runner/execute",
        headers=SANDBOX_HEADERS,
        json={
            "tool_name": "openai",
            "function_name": "list_models",
            "args": [],
            "kwargs": {},
            "tool_init_overrides": {"api_key": "test-key"},
        },
    )

    assert response.status_code == 400
    assert "api_key" in response.json()["detail"]


def test_sandbox_runner_rejects_invalid_base_dir_override_type(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed base_dir overrides should be rejected before toolkit construction."""
    _set_sandbox_token(monkeypatch)

    response = runner_client.post(
        "/api/sandbox-runner/execute",
        headers=SANDBOX_HEADERS,
        json={
            "tool_name": "coding",
            "function_name": "ls",
            "args": [],
            "kwargs": {"path": "."},
            "tool_init_overrides": {"base_dir": {"bad": "value"}},
        },
    )

    assert response.status_code == 400
    assert "base_dir" in response.json()["detail"]


def test_sandbox_runner_subprocess_rejects_unsafe_tool_init_overrides(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unsafe tool init overrides should be rejected before subprocess execution starts."""
    _set_sandbox_token(monkeypatch)
    monkeypatch.setenv("MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE", "subprocess")

    response = runner_client.post(
        "/api/sandbox-runner/execute",
        headers=SANDBOX_HEADERS,
        json={
            "tool_name": "openai",
            "function_name": "list_models",
            "args": [],
            "kwargs": {},
            "tool_init_overrides": {"api_key": "test-key"},
        },
    )

    assert response.status_code == 400
    assert "api_key" in response.json()["detail"]


def test_sandbox_runner_subprocess_rejects_invalid_base_dir_override_type(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed base_dir overrides should be rejected before subprocess dispatch."""
    _set_sandbox_token(monkeypatch)
    monkeypatch.setenv("MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE", "subprocess")

    response = runner_client.post(
        "/api/sandbox-runner/execute",
        headers=SANDBOX_HEADERS,
        json={
            "tool_name": "coding",
            "function_name": "ls",
            "args": [],
            "kwargs": {"path": "."},
            "tool_init_overrides": {"base_dir": {"bad": "value"}},
        },
    )

    assert response.status_code == 400
    assert "base_dir" in response.json()["detail"]


def test_sandbox_runner_rejects_worker_base_dir_outside_worker_root(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Worker requests should reject base_dir overrides that escape the worker root."""
    _set_sandbox_token(monkeypatch)

    response = runner_client.post(
        "/api/sandbox-runner/execute",
        headers=SANDBOX_HEADERS,
        json={
            "tool_name": "coding",
            "function_name": "ls",
            "args": [],
            "kwargs": {"path": "."},
            "worker_key": "worker-a",
            "tool_init_overrides": {"base_dir": str(tmp_path / "outside-worker-root")},
        },
    )

    assert response.status_code == 400
    assert "worker root" in response.json()["detail"]


def test_sandbox_runner_rejects_scoped_worker_base_dir_outside_visible_state_root(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Scoped workers should reject base_dir overrides outside their visible state roots."""
    _set_sandbox_token(monkeypatch)
    monkeypatch.setenv("MINDROOM_STORAGE_PATH", str(tmp_path / "storage"))

    response = runner_client.post(
        "/api/sandbox-runner/execute",
        headers=SANDBOX_HEADERS,
        json={
            "tool_name": "coding",
            "function_name": "ls",
            "args": [],
            "kwargs": {"path": "."},
            "worker_key": "v1:tenant-123:shared:general",
            "tool_init_overrides": {"base_dir": "agents/other/workspace"},
        },
    )

    assert response.status_code == 400
    assert "allowed state roots" in response.json()["detail"]


def test_sandbox_runner_dedicated_worker_uses_shared_storage_root_env_for_agent_paths(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Dedicated workers should resolve relative agent roots against the shared storage env."""
    _set_sandbox_token(monkeypatch)
    worker_key = "v1:tenant-123:shared:general"
    shared_root = tmp_path / "shared"
    worker_root = shared_root / "sandbox-workers" / worker_dir_name(worker_key)
    monkeypatch.setenv("MINDROOM_SANDBOX_DEDICATED_WORKER_KEY", worker_key)
    monkeypatch.setenv("MINDROOM_SANDBOX_DEDICATED_WORKER_ROOT", str(worker_root))
    monkeypatch.setenv("MINDROOM_STORAGE_PATH", str(worker_root))
    monkeypatch.setenv("MINDROOM_SANDBOX_SHARED_STORAGE_ROOT", str(shared_root))

    response = runner_client.post(
        "/api/sandbox-runner/execute",
        headers=SANDBOX_HEADERS,
        json={
            "tool_name": "file",
            "function_name": "save_file",
            "args": ["hello", "note.txt"],
            "kwargs": {},
            "worker_key": worker_key,
            "tool_init_overrides": {"base_dir": "agents/general/workspace/mind_data"},
        },
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    saved_file = shared_root / "agents" / "general" / "workspace" / "mind_data" / "note.txt"
    assert saved_file.read_text(encoding="utf-8") == "hello"


def test_sandbox_runner_user_scope_allows_broad_agents_tree_base_dir(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """User-scoped workers intentionally allow base_dir anywhere under the shared agents tree."""
    _set_sandbox_token(monkeypatch)
    storage_root = tmp_path / "storage"
    monkeypatch.setenv("MINDROOM_STORAGE_PATH", str(storage_root))

    response = runner_client.post(
        "/api/sandbox-runner/execute",
        headers=SANDBOX_HEADERS,
        json={
            "tool_name": "file",
            "function_name": "save_file",
            "args": ["hello", "note.txt"],
            "kwargs": {},
            "worker_key": "v1:tenant-123:user:@alice:example.org",
            "tool_init_overrides": {"base_dir": "agents/other/workspace"},
        },
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert (storage_root / "agents" / "other" / "workspace" / "note.txt").read_text(encoding="utf-8") == "hello"


def test_sandbox_runner_rejects_unknown_worker_key_base_dir(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Malformed worker keys must not gain shared-storage base_dir access."""
    _set_sandbox_token(monkeypatch)
    monkeypatch.setenv("MINDROOM_STORAGE_PATH", str(tmp_path / "storage"))

    response = runner_client.post(
        "/api/sandbox-runner/execute",
        headers=SANDBOX_HEADERS,
        json={
            "tool_name": "coding",
            "function_name": "ls",
            "args": [],
            "kwargs": {"path": "."},
            "worker_key": "legacy-worker",
            "tool_init_overrides": {"base_dir": "agents/other/workspace"},
        },
    )

    assert response.status_code == 400
    assert "visible state roots" in response.json()["detail"]


@REQUIRES_LINUX_LOCAL_WORKER
def test_sandbox_runner_worker_request_does_not_inject_base_dir_into_unrelated_tools(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Worker requests should still run tools that do not declare a base_dir init field."""
    _set_sandbox_token(monkeypatch)
    monkeypatch.setenv("MINDROOM_STORAGE_PATH", str(tmp_path))

    response = runner_client.post(
        "/api/sandbox-runner/execute",
        headers=SANDBOX_HEADERS,
        json={
            "tool_name": "calculator",
            "function_name": "add",
            "args": [1, 2],
            "kwargs": {},
            "worker_key": "worker-a",
        },
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert '"result": 3' in response.json()["result"]


def test_sandbox_runner_worker_request_rejects_invalid_base_dir_type_for_unknown_tool(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worker base_dir validation should run before unknown-tool resolution."""
    _set_sandbox_token(monkeypatch)

    response = runner_client.post(
        "/api/sandbox-runner/execute",
        headers=SANDBOX_HEADERS,
        json={
            "tool_name": "does_not_exist",
            "function_name": "add",
            "args": [],
            "kwargs": {},
            "worker_key": "worker-a",
            "tool_init_overrides": {"base_dir": {"bad": "value"}},
        },
    )

    assert response.status_code == 400
    assert "base_dir" in response.json()["detail"]


@REQUIRES_LINUX_LOCAL_WORKER
def test_sandbox_runner_prepares_worker_once_before_subprocess_dispatch(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Worker request validation should reuse the prepared worker for parent dispatch."""
    _set_sandbox_token(monkeypatch)
    storage_root = tmp_path / "storage"
    worker_key = "v1:tenant-123:shared:general"
    monkeypatch.setenv("MINDROOM_STORAGE_PATH", str(storage_root))

    prepare_calls = 0
    original_prepare = sandbox_worker_prep_module.prepare_worker

    def _counting_prepare(
        worker_key: str,
        runtime_paths: object,
        *,
        runner_token: str | None = None,
    ) -> object:
        nonlocal prepare_calls
        prepare_calls += 1
        return original_prepare(worker_key, runtime_paths, runner_token=runner_token)

    async def _fake_execute_request_subprocess(
        request: sandbox_runner_module.SandboxRunnerExecuteRequest,
        runtime_paths: object,
        config: object,
        prepared_worker: object | None = None,
        *,
        runner_token: str | None = None,
    ) -> sandbox_runner_module.SandboxRunnerExecuteResponse:
        assert request.worker_key == worker_key
        assert runtime_paths is not None
        assert config is not None
        assert prepared_worker is not None
        assert runner_token == SANDBOX_TOKEN
        return sandbox_runner_module.SandboxRunnerExecuteResponse(ok=True, result="ok")

    monkeypatch.setattr(sandbox_worker_prep_module, "prepare_worker", _counting_prepare)
    monkeypatch.setattr(sandbox_runner_module, "_execute_request_subprocess", _fake_execute_request_subprocess)

    response = runner_client.post(
        "/api/sandbox-runner/execute",
        headers=SANDBOX_HEADERS,
        json={
            "tool_name": "file",
            "function_name": "save_file",
            "args": ["hello", "note.txt"],
            "kwargs": {},
            "worker_key": worker_key,
            "tool_init_overrides": {"base_dir": "agents/general/workspace/mind_data"},
        },
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "result": "ok", "error": None}
    assert prepare_calls == 1


def test_sandbox_runner_lease_is_one_time_use(runner_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Credential leases should be consumed after one execution by default."""
    _set_sandbox_token(monkeypatch)

    lease_response = runner_client.post(
        "/api/sandbox-runner/leases",
        headers=SANDBOX_HEADERS,
        json={
            "tool_name": "calculator",
            "function_name": "add",
            "credential_overrides": {"OPENAI_API_KEY": "test-key"},
            "ttl_seconds": 60,
            "max_uses": 1,
        },
    )
    assert lease_response.status_code == 200
    lease_data = lease_response.json()
    lease_id = lease_data["lease_id"]

    first_execute = runner_client.post(
        "/api/sandbox-runner/execute",
        headers=SANDBOX_HEADERS,
        json={
            "tool_name": "calculator",
            "function_name": "add",
            "args": [2, 3],
            "kwargs": {},
            "lease_id": lease_id,
        },
    )
    assert first_execute.status_code == 200
    assert first_execute.json()["ok"] is True

    second_execute = runner_client.post(
        "/api/sandbox-runner/execute",
        headers=SANDBOX_HEADERS,
        json={
            "tool_name": "calculator",
            "function_name": "add",
            "args": [2, 3],
            "kwargs": {},
            "lease_id": lease_id,
        },
    )
    assert second_execute.status_code == 400
    assert "invalid or expired" in second_execute.json()["detail"]


def test_sandbox_runner_subprocess_consumes_lease(runner_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Lease-based credential overrides should work in subprocess mode."""
    _set_sandbox_token(monkeypatch)
    monkeypatch.setenv("MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE", "subprocess")

    lease_response = runner_client.post(
        "/api/sandbox-runner/leases",
        headers=SANDBOX_HEADERS,
        json={
            "tool_name": "calculator",
            "function_name": "add",
            "credential_overrides": {"OPENAI_API_KEY": "test-key"},
            "ttl_seconds": 60,
            "max_uses": 1,
        },
    )
    assert lease_response.status_code == 200
    lease_id = lease_response.json()["lease_id"]

    execute_response = runner_client.post(
        "/api/sandbox-runner/execute",
        headers=SANDBOX_HEADERS,
        json={
            "tool_name": "calculator",
            "function_name": "add",
            "args": [2, 3],
            "kwargs": {},
            "lease_id": lease_id,
        },
    )
    assert execute_response.status_code == 200
    assert execute_response.json()["ok"] is True


def test_sandbox_runner_unknown_tool_returns_404(runner_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unknown tools should return 404 instead of an unhandled server error."""
    _set_sandbox_token(monkeypatch)
    response = runner_client.post(
        "/api/sandbox-runner/execute",
        headers=SANDBOX_HEADERS,
        json={
            "tool_name": "does_not_exist",
            "function_name": "add",
            "args": [1, 2],
            "kwargs": {},
        },
    )
    assert response.status_code == 404
    assert "Unknown tool" in response.json()["detail"]


def test_sandbox_runner_forwards_worker_context_to_tool_rebuild(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sandbox runner should rebuild tools with worker scope and routing agent context."""
    _set_sandbox_token(monkeypatch)
    captured_kwargs: dict[str, object] = {}
    toolkit = SimpleNamespace(
        requires_connect=False,
        functions={"ping": SimpleNamespace(entrypoint=lambda: {"ok": True})},
        async_functions={},
    )

    def fake_get_tool_by_name(tool_name: str, **kwargs: object) -> SimpleNamespace:
        assert tool_name == "homeassistant"
        captured_kwargs.update(kwargs)
        return toolkit

    monkeypatch.setattr("mindroom.api.sandbox_runner.get_tool_by_name", fake_get_tool_by_name)
    response = runner_client.post(
        "/api/sandbox-runner/execute",
        headers=SANDBOX_HEADERS,
        json={
            "tool_name": "homeassistant",
            "function_name": "ping",
            "worker_scope": "shared",
            "routing_agent_name": "general",
        },
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert captured_kwargs["worker_scope"] == "shared"
    assert captured_kwargs["routing_agent_name"] == "general"


@REQUIRES_LINUX_LOCAL_WORKER
def test_sandbox_runner_worker_file_state_persists_and_is_isolated(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Worker-routed file tools should persist state by worker key and isolate different workers."""
    _set_sandbox_token(monkeypatch)
    worker_root = tmp_path / "workers"
    monkeypatch.setenv("MINDROOM_STORAGE_PATH", str(tmp_path))

    save_response = runner_client.post(
        "/api/sandbox-runner/execute",
        headers=SANDBOX_HEADERS,
        json={
            "tool_name": "file",
            "function_name": "save_file",
            "args": ["hello from worker A", "note.txt"],
            "kwargs": {},
            "worker_key": "worker-a",
        },
    )
    assert save_response.status_code == 200
    assert save_response.json()["ok"] is True

    read_same_worker = runner_client.post(
        "/api/sandbox-runner/execute",
        headers=SANDBOX_HEADERS,
        json={
            "tool_name": "file",
            "function_name": "read_file",
            "args": ["note.txt"],
            "kwargs": {},
            "worker_key": "worker-a",
        },
    )
    assert read_same_worker.status_code == 200
    assert read_same_worker.json()["ok"] is True
    assert "hello from worker A" in read_same_worker.json()["result"]

    read_other_worker = runner_client.post(
        "/api/sandbox-runner/execute",
        headers=SANDBOX_HEADERS,
        json={
            "tool_name": "file",
            "function_name": "read_file",
            "args": ["note.txt"],
            "kwargs": {},
            "worker_key": "worker-b",
        },
    )
    assert read_other_worker.status_code == 200
    assert read_other_worker.json()["ok"] is True
    assert "hello from worker A" not in read_other_worker.json()["result"]
    assert "No such file or directory" in read_other_worker.json()["result"]

    worker_file = worker_root / worker_dir_name("worker-a") / "workspace" / "note.txt"
    assert worker_file.read_text(encoding="utf-8") == "hello from worker A"


@REQUIRES_LINUX_LOCAL_WORKER
def test_sandbox_runner_worker_request_preserves_forwarded_base_dir(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Worker requests should honor a forwarded canonical base_dir inside shared agent storage."""
    _set_sandbox_token(monkeypatch)
    storage_root = tmp_path / "storage"
    worker_key = "v1:tenant-123:shared:general"
    monkeypatch.setenv("MINDROOM_STORAGE_PATH", str(storage_root))

    response = runner_client.post(
        "/api/sandbox-runner/execute",
        headers=SANDBOX_HEADERS,
        json={
            "tool_name": "file",
            "function_name": "save_file",
            "args": ["hello from canonical workspace", "note.txt"],
            "kwargs": {},
            "worker_key": worker_key,
            "tool_init_overrides": {"base_dir": "agents/general/workspace/mind_data"},
        },
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True

    canonical_file = agent_workspace_root_path(storage_root, "general") / "mind_data" / "note.txt"
    worker_root = storage_root / "workers"
    assert canonical_file.read_text(encoding="utf-8") == "hello from canonical workspace"
    assert not (worker_root / worker_dir_name(worker_key) / "workspace" / "note.txt").exists()


@REQUIRES_LINUX_LOCAL_WORKER
def test_sandbox_runner_worker_request_uses_default_storage_root_when_env_is_unset(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Worker requests should validate canonical agent roots against the default storage root."""
    _set_sandbox_token(monkeypatch)
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\nmodels: {}\n", encoding="utf-8")
    storage_root = tmp_path / "mindroom_data"
    monkeypatch.setenv("MINDROOM_CONFIG_PATH", str(config_path))
    monkeypatch.delenv("MINDROOM_STORAGE_PATH", raising=False)
    monkeypatch.delenv("MINDROOM_SANDBOX_DEDICATED_WORKER_KEY", raising=False)
    monkeypatch.delenv("MINDROOM_SANDBOX_DEDICATED_WORKER_ROOT", raising=False)

    canonical_base_dir = agent_workspace_root_path(storage_root, "general") / "mind_data"
    response = runner_client.post(
        "/api/sandbox-runner/execute",
        headers=SANDBOX_HEADERS,
        json={
            "tool_name": "file",
            "function_name": "save_file",
            "args": ["hello from default storage root fallback", "note.txt"],
            "kwargs": {},
            "worker_key": "v1:tenant-123:shared:general",
            "tool_init_overrides": {"base_dir": str(canonical_base_dir)},
        },
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert (canonical_base_dir / "note.txt").read_text(encoding="utf-8") == "hello from default storage root fallback"


def test_runtime_private_agent_names_skips_config_load_for_non_user_agent_worker() -> None:
    """Non-user-agent workers should not consult private-agent config visibility."""
    config = SimpleNamespace(
        get_private_agent_names=lambda: (_ for _ in ()).throw(AssertionError("should not be called")),
    )

    assert runtime_private_agent_names(config, worker_key="v1:tenant-123:shared:general") == frozenset()


def test_runtime_private_agent_names_returns_private_names_for_user_agent_worker() -> None:
    """User-agent workers should read private visibility from the provided config."""
    worker_key = resolve_worker_key(
        "user_agent",
        ToolExecutionIdentity(
            channel="matrix",
            agent_name="mind",
            requester_id="@alice:example.org",
            room_id="!room:example.org",
            thread_id=None,
            resolved_thread_id=None,
            session_id=None,
        ),
        agent_name="mind",
    )
    config = SimpleNamespace(get_private_agent_names=lambda: frozenset({"mind"}))

    assert runtime_private_agent_names(config, worker_key=worker_key) == frozenset({"mind"})


def test_prepare_worker_request_shared_worker_does_not_read_private_agent_names(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Shared workers should not consult private-agent config visibility."""
    worker_key = "v1:tenant-123:shared:general"
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")
    worker_handle = SimpleNamespace()
    worker_paths = local_workers_module._local_worker_state_paths_for_root(tmp_path / "workers" / "general")
    config = SimpleNamespace(
        get_private_agent_names=lambda: (_ for _ in ()).throw(AssertionError("should not be called")),
    )

    monkeypatch.setattr(sandbox_worker_prep_module, "prepare_worker", lambda *_args, **_kwargs: worker_handle)
    monkeypatch.setattr(
        sandbox_worker_prep_module,
        "local_worker_state_paths_from_handle",
        lambda _handle: worker_paths,
    )

    prepared = sandbox_worker_prep_module.prepare_worker_request(
        worker_key=worker_key,
        tool_init_overrides={"base_dir": "agents/general/workspace"},
        runtime_paths=runtime_paths,
        config=config,
    )

    assert prepared.handle is worker_handle


def test_prepare_worker_request_user_agent_private_visibility_comes_from_explicit_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """User-agent workers should derive private visibility from the provided config object."""
    worker_key = resolve_worker_key(
        "user_agent",
        ToolExecutionIdentity(
            channel="matrix",
            agent_name="mind",
            requester_id="@alice:example.org",
            room_id="!room:example.org",
            thread_id=None,
            resolved_thread_id=None,
            session_id=None,
            tenant_id="tenant-123",
        ),
        agent_name="mind",
    )
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")
    worker_handle = SimpleNamespace()
    worker_paths = local_workers_module._local_worker_state_paths_for_root(tmp_path / "workers" / "mind")
    config = SimpleNamespace(get_private_agent_names=lambda: frozenset({"mind"}))

    monkeypatch.setattr(sandbox_worker_prep_module, "prepare_worker", lambda *_args, **_kwargs: worker_handle)
    monkeypatch.setattr(
        sandbox_worker_prep_module,
        "local_worker_state_paths_from_handle",
        lambda _handle: worker_paths,
    )

    prepared = sandbox_worker_prep_module.prepare_worker_request(
        worker_key=worker_key,
        tool_init_overrides={
            "base_dir": str(private_instance_scope_root_path(runtime_paths.storage_root, worker_key)),
        },
        runtime_paths=runtime_paths,
        config=config,
    )

    assert prepared.handle is worker_handle


def test_prepare_worker_request_wraps_private_visibility_config_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Config visibility failures should surface as worker-preparation errors."""
    worker_key = "v1:tenant-123:user_agent:mind:@alice:example.org"
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")
    worker_handle = SimpleNamespace()
    worker_paths = local_workers_module._local_worker_state_paths_for_root(tmp_path / "workers" / "mind")

    def _prepare_worker(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return worker_handle

    def _local_worker_state_paths_from_handle(_handle: object) -> local_workers_module.LocalWorkerStatePaths:
        return worker_paths

    config = SimpleNamespace(
        get_private_agent_names=lambda: (_ for _ in ()).throw(ValueError("invalid config")),
    )

    monkeypatch.setattr(sandbox_worker_prep_module, "prepare_worker", _prepare_worker)
    monkeypatch.setattr(
        sandbox_worker_prep_module,
        "local_worker_state_paths_from_handle",
        _local_worker_state_paths_from_handle,
    )
    with pytest.raises(sandbox_worker_prep_module.WorkerRequestPreparationError, match="invalid config"):
        sandbox_worker_prep_module.prepare_worker_request(
            worker_key=worker_key,
            tool_init_overrides={"base_dir": "private_instances/example/mind"},
            runtime_paths=runtime_paths,
            config=config,
        )


@REQUIRES_LINUX_LOCAL_WORKER
def test_dedicated_worker_mode_resolves_relative_agent_base_dir_from_shared_storage(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Dedicated workers should still resolve relative agent paths from shared storage roots."""
    _set_sandbox_token(monkeypatch)
    worker_key = "v1:tenant-123:shared:general"
    shared_root = tmp_path / "shared-storage"
    worker_root = shared_root / "workers" / worker_dir_name(worker_key)
    monkeypatch.setenv("MINDROOM_SANDBOX_DEDICATED_WORKER_KEY", worker_key)
    monkeypatch.setenv("MINDROOM_SANDBOX_DEDICATED_WORKER_ROOT", str(worker_root))
    monkeypatch.setenv("MINDROOM_STORAGE_PATH", str(worker_root))

    response = runner_client.post(
        "/api/sandbox-runner/execute",
        headers=SANDBOX_HEADERS,
        json={
            "tool_name": "file",
            "function_name": "save_file",
            "args": ["hello from dedicated worker canonical workspace", "note.txt"],
            "kwargs": {},
            "worker_key": worker_key,
            "tool_init_overrides": {"base_dir": "agents/general/workspace/mind_data"},
        },
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True

    canonical_file = agent_workspace_root_path(shared_root, "general") / "mind_data" / "note.txt"
    assert canonical_file.read_text(encoding="utf-8") == "hello from dedicated worker canonical workspace"
    assert not (worker_root / "workspace" / "note.txt").exists()


@REQUIRES_LINUX_LOCAL_WORKER
def test_dedicated_worker_mode_resolves_relative_agent_base_dir_from_nested_worker_prefix(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Dedicated workers should recover the shared root even with nested worker prefixes."""
    _set_sandbox_token(monkeypatch)
    worker_key = "v1:tenant-123:shared:general"
    shared_root = tmp_path / "shared-storage"
    worker_root = shared_root / "nested" / "workers" / worker_dir_name(worker_key)
    monkeypatch.setenv("MINDROOM_SANDBOX_DEDICATED_WORKER_KEY", worker_key)
    monkeypatch.setenv("MINDROOM_SANDBOX_DEDICATED_WORKER_ROOT", str(worker_root))
    monkeypatch.setenv("MINDROOM_STORAGE_PATH", str(worker_root))
    monkeypatch.delenv("MINDROOM_SANDBOX_SHARED_STORAGE_ROOT", raising=False)
    monkeypatch.setenv("MINDROOM_KUBERNETES_WORKER_STORAGE_SUBPATH_PREFIX", "nested/workers")

    response = runner_client.post(
        "/api/sandbox-runner/execute",
        headers=SANDBOX_HEADERS,
        json={
            "tool_name": "file",
            "function_name": "save_file",
            "args": ["hello from nested worker prefix", "note.txt"],
            "kwargs": {},
            "worker_key": worker_key,
            "tool_init_overrides": {"base_dir": "agents/general/workspace/mind_data"},
        },
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True

    canonical_file = agent_workspace_root_path(shared_root, "general") / "mind_data" / "note.txt"
    assert canonical_file.read_text(encoding="utf-8") == "hello from nested worker prefix"
    assert not (worker_root / "workspace" / "note.txt").exists()


@REQUIRES_LINUX_LOCAL_WORKER
def test_sandbox_runner_worker_python_uses_persistent_virtualenv(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Worker-routed python tools should execute inside the worker-specific virtualenv."""
    _set_sandbox_token(monkeypatch)
    worker_root = tmp_path / "workers"
    monkeypatch.setenv("MINDROOM_STORAGE_PATH", str(tmp_path))

    response = runner_client.post(
        "/api/sandbox-runner/execute",
        headers=SANDBOX_HEADERS,
        json={
            "tool_name": "python",
            "function_name": "run_python_code",
            "args": ["import sys\nresult = sys.prefix", "result"],
            "kwargs": {},
            "worker_key": "worker-a",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True

    expected_prefix = worker_root / worker_dir_name("worker-a") / "venv"
    assert str(expected_prefix) in data["result"]


@REQUIRES_LINUX_LOCAL_WORKER
def test_sandbox_runner_worker_python_supports_matrix_scoped_worker_keys(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Scoped worker keys should be sanitized before they reach the venv path."""
    _set_sandbox_token(monkeypatch)
    worker_root = tmp_path / "workers"
    monkeypatch.setenv("MINDROOM_STORAGE_PATH", str(tmp_path))
    worker_key = resolve_worker_key(
        "user",
        ToolExecutionIdentity(
            channel="matrix",
            agent_name="persistent_worker_lab",
            requester_id="@smoketest_a:chat-internal.ionq.co",
            room_id="!persistent-workers:chat-internal.ionq.co",
            thread_id="$thread",
            resolved_thread_id="$thread",
            session_id="session-1",
            tenant_id="default",
        ),
        agent_name="persistent_worker_lab",
    )
    assert worker_key is not None

    response = runner_client.post(
        "/api/sandbox-runner/execute",
        headers=SANDBOX_HEADERS,
        json={
            "tool_name": "python",
            "function_name": "run_python_code",
            "args": ["import sys\nresult = sys.prefix", "result"],
            "kwargs": {},
            "worker_key": worker_key,
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True

    worker_dir = worker_dir_name(worker_key)
    assert ":" not in worker_dir
    expected_prefix = worker_root / worker_dir / "venv"
    assert str(expected_prefix) in data["result"]


@REQUIRES_LINUX_LOCAL_WORKER
def test_sandbox_runner_lists_known_workers(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Sandbox runner should expose worker metadata for debugging and observability."""
    _set_sandbox_token(monkeypatch)

    execute_response = runner_client.post(
        "/api/sandbox-runner/execute",
        headers=SANDBOX_HEADERS,
        json={
            "tool_name": "file",
            "function_name": "save_file",
            "args": ["hello from worker A", "note.txt"],
            "kwargs": {},
            "worker_key": "worker-a",
        },
    )
    assert execute_response.status_code == 200
    assert execute_response.json()["ok"] is True

    workers_response = runner_client.get("/api/sandbox-runner/workers", headers=SANDBOX_HEADERS)
    assert workers_response.status_code == 200

    worker = next(worker for worker in workers_response.json()["workers"] if worker["worker_key"] == "worker-a")
    assert worker["status"] == "ready"
    assert worker["backend_name"] == "local_sandbox_runner"
    assert worker["startup_count"] == 1
    worker_root = tmp_path / ".mindroom" / "workers"
    assert worker["debug_metadata"]["state_root"] == str((worker_root / worker_dir_name("worker-a")).resolve())


@REQUIRES_LINUX_LOCAL_WORKER
def test_sandbox_runner_cleanup_marks_idle_workers_without_deleting_state(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Idle cleanup should evict the live worker handle but keep its persisted state."""
    _set_sandbox_token(monkeypatch)
    monkeypatch.setenv("MINDROOM_SANDBOX_WORKER_IDLE_TIMEOUT_SECONDS", "60")

    save_response = runner_client.post(
        "/api/sandbox-runner/execute",
        headers=SANDBOX_HEADERS,
        json={
            "tool_name": "file",
            "function_name": "save_file",
            "args": ["hello from worker A", "note.txt"],
            "kwargs": {},
            "worker_key": "worker-a",
        },
    )
    assert save_response.status_code == 200
    assert save_response.json()["ok"] is True

    worker_root = tmp_path / ".mindroom" / "workers"
    metadata_path = worker_root / worker_dir_name("worker-a") / "metadata" / "worker.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["last_used_at"] = 0.0
    metadata["status"] = "ready"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    cleanup_response = runner_client.post("/api/sandbox-runner/workers/cleanup", headers=SANDBOX_HEADERS)
    workers_response = runner_client.get("/api/sandbox-runner/workers", headers=SANDBOX_HEADERS)

    assert cleanup_response.status_code == 200
    cleaned_worker = cleanup_response.json()["cleaned_workers"][0]
    assert cleaned_worker["worker_key"] == "worker-a"
    assert cleaned_worker["status"] == "idle"

    listed_worker = next(worker for worker in workers_response.json()["workers"] if worker["worker_key"] == "worker-a")
    assert listed_worker["status"] == "idle"

    worker_file = worker_root / worker_dir_name("worker-a") / "workspace" / "note.txt"
    assert worker_file.read_text(encoding="utf-8") == "hello from worker A"


def test_dedicated_worker_mode_uses_mounted_root(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Dedicated worker mode should execute against the mounted worker root directly."""
    _set_sandbox_token(monkeypatch)
    worker_root = tmp_path / "dedicated-worker"
    monkeypatch.setenv("MINDROOM_SANDBOX_DEDICATED_WORKER_KEY", "worker-a")
    monkeypatch.setenv("MINDROOM_SANDBOX_DEDICATED_WORKER_ROOT", str(worker_root))

    def fake_create(_self: object, venv_dir: Path) -> None:
        (venv_dir / "bin").mkdir(parents=True, exist_ok=True)
        (venv_dir / "bin" / "python").write_text("", encoding="utf-8")

    def fake_run(
        cmd: list[str],
        **run_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        assert run_kwargs["capture_output"] is True
        assert run_kwargs["text"] is True
        assert isinstance(run_kwargs["timeout"], float)
        assert run_kwargs["check"] is False
        request_input = str(run_kwargs["input"])
        env = run_kwargs["env"]
        cwd = run_kwargs["cwd"]
        assert env is not None
        assert isinstance(env, dict)
        assert cmd[0] == str(worker_root / "venv" / "bin" / "python")
        assert isinstance(cwd, str)
        assert cwd == str(worker_root / "workspace")
        assert "MINDROOM_STORAGE_PATH" not in env
        assert "MINDROOM_SANDBOX_WORKER_ROOT" not in env
        request_envelope = json.loads(request_input)
        request_payload = request_envelope["request"]
        runtime_payload = request_envelope["runtime_paths"]
        assert request_payload["worker_key"] == "worker-a"
        assert runtime_payload["process_env"]["MINDROOM_SANDBOX_DEDICATED_WORKER_KEY"] == "worker-a"
        assert runtime_payload["process_env"]["MINDROOM_SANDBOX_DEDICATED_WORKER_ROOT"] == str(worker_root)
        note_path = worker_root / "workspace" / request_payload["args"][1]
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text(request_payload["args"][0], encoding="utf-8")
        response = sandbox_runner_module.SandboxRunnerExecuteResponse(ok=True, result="saved")
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout="",
            stderr=sandbox_protocol_module.RESPONSE_MARKER + response.model_dump_json(),
        )

    with (
        patch("mindroom.workers.backends.local.venv.EnvBuilder.create", new=fake_create),
        patch("mindroom.api.sandbox_runner.subprocess.run", new=fake_run),
    ):
        save_response = runner_client.post(
            "/api/sandbox-runner/execute",
            headers=SANDBOX_HEADERS,
            json={
                "tool_name": "file",
                "function_name": "save_file",
                "args": ["hello from dedicated worker", "note.txt"],
                "kwargs": {},
                "worker_key": "worker-a",
            },
        )
    assert save_response.status_code == 200
    assert save_response.json()["ok"] is True

    worker_file = worker_root / "workspace" / "note.txt"
    assert worker_file.read_text(encoding="utf-8") == "hello from dedicated worker"


def test_dedicated_worker_mode_defaults_missing_worker_key_to_pinned_worker(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Dedicated worker mode should infer the pinned worker key when callers omit it."""
    _set_sandbox_token(monkeypatch)
    worker_root = tmp_path / "dedicated-worker"
    monkeypatch.setenv("MINDROOM_SANDBOX_DEDICATED_WORKER_KEY", "worker-a")
    monkeypatch.setenv("MINDROOM_SANDBOX_DEDICATED_WORKER_ROOT", str(worker_root))

    def fake_create(_self: object, venv_dir: Path) -> None:
        (venv_dir / "bin").mkdir(parents=True, exist_ok=True)
        (venv_dir / "bin" / "python").write_text("", encoding="utf-8")

    def fake_run(
        cmd: list[str],
        **run_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        request_envelope = json.loads(str(run_kwargs["input"]))
        request_payload = request_envelope["request"]
        assert request_payload["worker_key"] == "worker-a"

        note_path = worker_root / "workspace" / request_payload["args"][1]
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text(request_payload["args"][0], encoding="utf-8")

        response = sandbox_runner_module.SandboxRunnerExecuteResponse(ok=True, result="saved")
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout="",
            stderr=sandbox_protocol_module.RESPONSE_MARKER + response.model_dump_json(),
        )

    with (
        patch("mindroom.workers.backends.local.venv.EnvBuilder.create", new=fake_create),
        patch("mindroom.api.sandbox_runner.subprocess.run", new=fake_run),
    ):
        save_response = runner_client.post(
            "/api/sandbox-runner/execute",
            headers=SANDBOX_HEADERS,
            json={
                "tool_name": "file",
                "function_name": "save_file",
                "args": ["hello from inferred worker", "note.txt"],
                "kwargs": {},
            },
        )

    assert save_response.status_code == 200
    assert save_response.json()["ok"] is True

    worker_file = worker_root / "workspace" / "note.txt"
    assert worker_file.read_text(encoding="utf-8") == "hello from inferred worker"


def test_dedicated_worker_mode_does_not_treat_empty_worker_key_as_missing(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Dedicated worker mode should not rewrite explicit empty worker keys."""
    _set_sandbox_token(monkeypatch)
    monkeypatch.setenv("MINDROOM_SANDBOX_DEDICATED_WORKER_KEY", "worker-a")
    monkeypatch.setenv("MINDROOM_SANDBOX_DEDICATED_WORKER_ROOT", str(tmp_path / "dedicated-worker"))

    response = runner_client.post(
        "/api/sandbox-runner/execute",
        headers=SANDBOX_HEADERS,
        json={
            "tool_name": "file",
            "function_name": "read_file",
            "args": ["note.txt"],
            "kwargs": {},
            "worker_key": "",
        },
    )

    assert response.status_code == 400
    assert "Dedicated sandbox worker is pinned" in response.json()["detail"]


def test_dedicated_worker_mode_rejects_mismatched_worker_key(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Dedicated worker mode should reject requests for other worker keys."""
    _set_sandbox_token(monkeypatch)
    monkeypatch.setenv("MINDROOM_SANDBOX_DEDICATED_WORKER_KEY", "worker-a")
    monkeypatch.setenv("MINDROOM_SANDBOX_DEDICATED_WORKER_ROOT", str(tmp_path / "dedicated-worker"))

    response = runner_client.post(
        "/api/sandbox-runner/execute",
        headers=SANDBOX_HEADERS,
        json={
            "tool_name": "file",
            "function_name": "read_file",
            "args": ["note.txt"],
            "kwargs": {},
            "worker_key": "worker-b",
        },
    )
    assert response.status_code == 400
    assert "Dedicated sandbox worker is pinned" in response.json()["detail"]


def test_prepare_worker_uses_explicit_runtime_storage_root_for_local_workers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Local worker state roots should come from the committed runtime storage root."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\nmodels: {}\n", encoding="utf-8")
    monkeypatch.setenv("MINDROOM_SANDBOX_WORKER_ROOT", str(tmp_path / "ambient-workers"))
    runtime_paths = resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "explicit-storage",
        process_env=dict(os.environ),
    )

    worker = sandbox_worker_prep_module.prepare_worker("worker-a", runtime_paths)

    assert worker.debug_metadata["state_root"] == str(
        tmp_path / "explicit-storage" / "workers" / worker_dir_name("worker-a"),
    )


def test_get_local_worker_manager_singleton_creation_is_thread_safe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Concurrent access should build the local worker manager only once per config."""
    monkeypatch.setattr(local_workers_module, "_local_worker_manager", None)
    monkeypatch.setattr(local_workers_module, "_local_worker_manager_config", None)
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / ".mindroom",
    )

    first_init_started = threading.Event()
    allow_first_init_to_finish = threading.Event()
    init_count_lock = threading.Lock()
    init_count = 0
    managers: list[object] = []
    exceptions: list[Exception] = []

    class FakeBackend:
        backend_name = "fake_local_backend"
        idle_timeout_seconds = 60.0

        def __init__(self, *, worker_root: Path, api_root: str, idle_timeout_seconds: float) -> None:
            del worker_root, api_root, idle_timeout_seconds
            nonlocal init_count
            with init_count_lock:
                init_count += 1
                call_number = init_count
            if call_number == 1:
                first_init_started.set()
                assert allow_first_init_to_finish.wait(timeout=1.0)

    def load_manager() -> None:
        try:
            managers.append(local_workers_module.get_local_worker_manager(runtime_paths))
        except Exception as exc:  # pragma: no cover - surfaced by assertion below
            exceptions.append(exc)

    monkeypatch.setattr(local_workers_module, "_LocalWorkerBackend", FakeBackend)

    first_thread = threading.Thread(target=load_manager)
    second_thread = threading.Thread(target=load_manager)

    first_thread.start()
    assert first_init_started.wait(timeout=1.0)
    second_thread.start()
    assert init_count == 1
    allow_first_init_to_finish.set()
    first_thread.join(timeout=1.0)
    second_thread.join(timeout=1.0)

    assert exceptions == []
    assert init_count == 1
    assert len(managers) == 2
    assert managers[0] is managers[1]


def test_local_worker_backend_serializes_same_worker_initialization(tmp_path: Path) -> None:
    """Concurrent requests for one worker key should not initialize the venv twice."""
    backend = local_workers_module._LocalWorkerBackend(
        worker_root=tmp_path / "workers",
        api_root="/api/sandbox-runner",
        idle_timeout_seconds=60.0,
    )
    first_create_started = threading.Event()
    allow_first_create_to_finish = threading.Event()
    second_create_started = threading.Event()
    call_count_lock = threading.Lock()
    create_call_count = 0
    exceptions: list[Exception] = []

    def fake_create(_self: object, venv_dir: Path) -> None:
        nonlocal create_call_count
        with call_count_lock:
            create_call_count += 1
            call_number = create_call_count
        if call_number == 1:
            first_create_started.set()
            assert allow_first_create_to_finish.wait(timeout=1.0)
            (venv_dir / "bin").mkdir(parents=True, exist_ok=True)
            (venv_dir / "bin" / "python").write_text("", encoding="utf-8")
            return

        second_create_started.set()
        (venv_dir / "bin").mkdir(parents=True, exist_ok=True)
        (venv_dir / "bin" / "python").write_text("", encoding="utf-8")

    def ensure_worker() -> None:
        try:
            backend.ensure_worker(WorkerSpec("worker-race"))
        except Exception as exc:  # pragma: no cover - surfaced by test assertion below
            exceptions.append(exc)

    with patch("mindroom.workers.backends.local.venv.EnvBuilder.create", new=fake_create):
        thread_one = threading.Thread(target=ensure_worker)
        thread_two = threading.Thread(target=ensure_worker)

        thread_one.start()
        assert first_create_started.wait(timeout=1.0)
        thread_two.start()
        assert not second_create_started.wait(timeout=0.2)
        allow_first_create_to_finish.set()
        thread_one.join(timeout=1.0)
        thread_two.join(timeout=1.0)

    assert exceptions == []
    assert create_call_count == 1
    worker = backend.get_worker("worker-race")
    assert worker is not None
    assert worker.startup_count == 1
    assert worker.status == "ready"


def test_sandbox_runner_records_worker_initialization_failures(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worker bootstrap failures should be returned to callers and exposed in worker metadata."""
    _set_sandbox_token(monkeypatch)

    with patch("mindroom.workers.backends.local.venv.EnvBuilder.create", side_effect=OSError("boom")):
        execute_response = runner_client.post(
            "/api/sandbox-runner/execute",
            headers=SANDBOX_HEADERS,
            json={
                "tool_name": "file",
                "function_name": "read_file",
                "args": ["note.txt"],
                "kwargs": {},
                "worker_key": "worker-fail",
            },
        )

    assert execute_response.status_code == 400
    detail = execute_response.json()["detail"]
    assert "Failed to initialize worker 'worker-fail'" in detail
    assert "boom" in detail

    workers_response = runner_client.get("/api/sandbox-runner/workers", headers=SANDBOX_HEADERS)
    assert workers_response.status_code == 200

    worker = next(worker for worker in workers_response.json()["workers"] if worker["worker_key"] == "worker-fail")
    assert worker["status"] == "failed"
    assert "boom" in worker["failure_reason"]
    assert worker["failure_count"] == 1
