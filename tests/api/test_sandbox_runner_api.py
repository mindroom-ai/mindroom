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

import pytest
from fastapi.testclient import TestClient

import mindroom.api.sandbox_exec as sandbox_exec_module
import mindroom.api.sandbox_protocol as sandbox_protocol_module
import mindroom.api.sandbox_runner as sandbox_runner_module
import mindroom.api.sandbox_worker_prep as sandbox_worker_prep_module
import mindroom.credentials as credentials_module
import mindroom.tool_system.metadata as metadata_module
from mindroom.api.sandbox_runner_app import app as sandbox_runner_app
from mindroom.config.main import Config, ConfigRuntimeValidationError
from mindroom.constants import (
    resolve_primary_runtime_paths,
    resolve_runtime_paths,
    serialize_public_runtime_paths,
    serialize_runtime_paths,
)
from mindroom.credentials import CredentialsManager, get_runtime_credentials_manager, save_scoped_credentials
from mindroom.tool_system.bootstrap import ensure_tool_registry_loaded
from mindroom.tool_system.metadata import (
    TOOL_METADATA,
    ConfigField,
    SetupType,
    ToolCategory,
    ToolMetadata,
    ToolStatus,
    get_tool_by_name,
    resolved_tool_validation_snapshot_for_runtime,
    serialize_tool_validation_snapshot,
)
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    _private_instance_state_root_path,
    agent_workspace_root_path,
    resolve_worker_key,
    worker_dir_name,
)
from mindroom.workers.backends import local as local_workers_module
from mindroom.workers.models import WorkerSpec

if TYPE_CHECKING:
    from collections.abc import Iterator

    from mindroom.constants import RuntimePaths

SANDBOX_TOKEN = "secret-token"  # noqa: S105
SANDBOX_HEADERS = {"x-mindroom-sandbox-token": SANDBOX_TOKEN}
REQUIRES_LINUX_LOCAL_WORKER = pytest.mark.skipif(
    sys.platform != "linux",
    reason="local worker venv bootstrap is validated on Linux",
)


def _fake_local_worker_venv_create(_self: object, venv_dir: Path) -> None:
    """Create the minimal worker venv layout needed for path-validation tests."""
    bin_dir = venv_dir / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "python").symlink_to(Path(sys.executable))


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
def runner_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[TestClient]:
    """Create a test client for the sandbox runner app."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MINDROOM_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("MINDROOM_STORAGE_PATH", str(tmp_path / ".mindroom"))

    _refresh_runner_app_from_env()
    with TestClient(sandbox_runner_app) as client:
        yield client


def _set_sandbox_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the sandbox token through the runner's explicit runtime env boundary."""
    monkeypatch.setenv("MINDROOM_SANDBOX_PROXY_TOKEN", SANDBOX_TOKEN)
    _refresh_runner_app_from_env()


def _set_worker_tool_validation_snapshot(*tool_names: str) -> None:
    """Set the upstream-authored validation snapshot visible to one worker runtime."""
    runtime_paths = resolve_primary_runtime_paths(process_env=dict(os.environ))
    config = Config.validate_with_runtime({}, runtime_paths)
    snapshot = serialize_tool_validation_snapshot(
        resolved_tool_validation_snapshot_for_runtime(runtime_paths, config),
    )
    for tool_name in tool_names:
        snapshot[tool_name] = {
            "config_fields": [],
            "agent_override_fields": [],
            "authored_override_validator": "default",
        }
    _write_startup_manifest(
        runtime_paths=runtime_paths,
        tool_validation_snapshot=snapshot,
    )


def _write_startup_manifest(
    *,
    runtime_paths: RuntimePaths,
    public_runtime: bool = False,
    tool_validation_snapshot: dict[str, object] | None = None,
) -> Path:
    return sandbox_runner_module.constants.write_startup_manifest(
        runtime_paths.storage_root,
        runtime_paths,
        tool_validation_snapshot=tool_validation_snapshot,
        public_runtime=public_runtime,
    )


def _set_startup_manifest(
    monkeypatch: pytest.MonkeyPatch,
    *,
    manifest_path: Path,
) -> None:
    monkeypatch.setenv("MINDROOM_SANDBOX_STARTUP_MANIFEST_PATH", str(manifest_path))


def test_worker_tool_validation_snapshot_reads_from_startup_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dedicated workers should load tool validation snapshots from the startup manifest."""
    runtime_paths = resolve_primary_runtime_paths(process_env=dict(os.environ))
    config = Config.validate_with_runtime({}, runtime_paths)
    snapshot = serialize_tool_validation_snapshot(
        resolved_tool_validation_snapshot_for_runtime(runtime_paths, config),
    )
    snapshot["agentspace_slack_search"] = {
        "config_fields": [],
        "agent_override_fields": [],
        "authored_override_validator": "default",
        "runtime_loadable": True,
    }
    manifest_path = _write_startup_manifest(runtime_paths=runtime_paths, tool_validation_snapshot=snapshot)
    _set_startup_manifest(monkeypatch, manifest_path=manifest_path)

    loaded_snapshot = sandbox_runner_module._upstream_tool_validation_snapshot(runtime_paths)

    assert "agentspace_slack_search" in loaded_snapshot
    assert loaded_snapshot["agentspace_slack_search"].runtime_loadable is True
    assert loaded_snapshot["agentspace_slack_search"].config_fields == ()
    assert loaded_snapshot["agentspace_slack_search"].agent_override_fields == ()


def _refresh_runner_app_from_env() -> tuple[RuntimePaths, Config]:
    runtime_paths = resolve_primary_runtime_paths(process_env=dict(os.environ))
    config = sandbox_runner_module._runtime_config_or_empty(runtime_paths)
    sandbox_runner_module.initialize_sandbox_runner_app(sandbox_runner_app, runtime_paths, config=config)
    return runtime_paths, config


def _initialize_runner_app_from_env() -> RuntimePaths:
    runtime_paths = resolve_primary_runtime_paths(process_env=dict(os.environ))
    sandbox_runner_module.initialize_sandbox_runner_app(
        sandbox_runner_app,
        runtime_paths,
        config=sandbox_runner_module._runtime_config_or_empty(runtime_paths),
    )
    return runtime_paths


def _invalid_plugin_config_path(tmp_path: Path) -> Path:
    """Write one config whose plugin manifest fails runtime validation."""
    plugin_root = tmp_path / "plugins" / "bad-name"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "BadName", "tools_module": None, "skills": []}),
        encoding="utf-8",
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\nplugins:\n  - ./plugins/bad-name\n",
        encoding="utf-8",
    )
    return config_path


def _missing_plugin_path_config_path(tmp_path: Path) -> Path:
    """Write one config that references a plugin unavailable in the worker runtime."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n"
        "  default:\n"
        "    provider: openai\n"
        "    id: gpt-5.4\n"
        "agents:\n"
        "  mind:\n"
        "    display_name: Mind\n"
        "    model: default\n"
        "    include_default_tools: false\n"
        "    tools:\n"
        "      - shell\n"
        "  saguaro:\n"
        "    display_name: Saguaro\n"
        "    model: default\n"
        "    include_default_tools: false\n"
        "    tools:\n"
        "      - agentspace_slack_search\n"
        "router:\n"
        "  model: default\n"
        "plugins:\n"
        "  - ./plugins/agentspace-slack-search\n",
        encoding="utf-8",
    )
    return config_path


def _missing_plugin_path_with_invalid_tool_config_path(tmp_path: Path) -> Path:
    """Write one config that mixes a missing worker plugin with an unknown tool."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n"
        "  default:\n"
        "    provider: openai\n"
        "    id: gpt-5.4\n"
        "agents:\n"
        "  broken:\n"
        "    display_name: Broken\n"
        "    model: default\n"
        "    include_default_tools: false\n"
        "    tools:\n"
        "      - agentspace_slack_search\n"
        "  invalid:\n"
        "    display_name: Invalid\n"
        "    model: default\n"
        "    include_default_tools: false\n"
        "    tools:\n"
        "      - definitely_not_a_tool\n"
        "router:\n"
        "  model: default\n"
        "plugins:\n"
        "  - ./plugins/agentspace-slack-search\n",
        encoding="utf-8",
    )
    return config_path


def _mcp_demo_config_path(tmp_path: Path) -> Path:
    """Write one config that exposes a valid MCP-backed tool assignment."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n"
        "  default:\n"
        "    provider: openai\n"
        "    id: gpt-5.4\n"
        "router:\n"
        "  model: default\n"
        "mcp_servers:\n"
        "  demo:\n"
        "    transport: stdio\n"
        "    command: python\n"
        "    args:\n"
        "      - -c\n"
        "      - print(0)\n"
        "agents:\n"
        "  code:\n"
        "    display_name: Code\n"
        "    role: test\n"
        "    model: default\n"
        "    tools:\n"
        "      - mcp_demo\n",
        encoding="utf-8",
    )
    return config_path


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
    manifest_path = _write_startup_manifest(runtime_paths=payload_runtime)
    _set_startup_manifest(monkeypatch, manifest_path=manifest_path)
    monkeypatch.setenv("MINDROOM_SANDBOX_PROXY_TOKEN", "from-env")

    startup_runtime = sandbox_runner_module._startup_runtime_paths_from_env()
    sandbox_runner_module.initialize_sandbox_runner_app(
        sandbox_runner_app,
        startup_runtime,
        runner_token=sandbox_runner_module._startup_runner_token_from_env(),
    )

    assert startup_runtime.env_value("MINDROOM_SANDBOX_PROXY_TOKEN") is None
    assert sandbox_runner_module._app_runner_token(sandbox_runner_app) == "from-env"


def test_lifespan_reuses_initialized_runner_context_without_reloading_disk_config(tmp_path: Path) -> None:
    """Existing sandbox-runner state should survive lifespan startup without reparsing config.yaml."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={"MINDROOM_NAMESPACE": "alpha1234"},
    )
    config = sandbox_runner_module._runtime_config_or_empty(runtime_paths)
    preserved_runner_token = SANDBOX_TOKEN
    sandbox_runner_module.initialize_sandbox_runner_app(
        sandbox_runner_app,
        runtime_paths,
        config=config,
        runner_token=preserved_runner_token,
    )
    config_path.write_text("agents:\n  broken: [\n", encoding="utf-8")

    with TestClient(sandbox_runner_app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert sandbox_runner_module._app_runner_token(sandbox_runner_app) == preserved_runner_token
    assert sandbox_runner_module._app_runtime_config(sandbox_runner_app) == config


def test_startup_runtime_rehydrates_runtime_env_from_process_env_and_dotenv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Startup runtime should recover trusted env from real process env while keeping runner auth separate."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "OPENAI_API_KEY=dotenv-secret\nOPENAI_BASE_URL=http://example.invalid/v1\nCUSTOM_API_TOKEN=custom-secret\n",
        encoding="utf-8",
    )
    payload_runtime = resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={"MINDROOM_NAMESPACE": "alpha1234"},
    )
    manifest_path = _write_startup_manifest(runtime_paths=payload_runtime, public_runtime=True)
    _set_startup_manifest(monkeypatch, manifest_path=manifest_path)
    monkeypatch.setenv("MINDROOM_SANDBOX_PROXY_TOKEN", "from-env")
    monkeypatch.setenv("TEST_EXECUTION_ENV", "worker-visible")

    startup_runtime = sandbox_runner_module._startup_runtime_paths_from_env()

    assert startup_runtime.env_value("MINDROOM_NAMESPACE") == "alpha1234"
    assert startup_runtime.env_value("OPENAI_API_KEY") == "dotenv-secret"
    assert startup_runtime.env_value("TEST_EXECUTION_ENV") == "worker-visible"
    assert startup_runtime.env_value("MINDROOM_SANDBOX_PROXY_TOKEN") is None


def test_dedicated_worker_startup_runtime_does_not_rehydrate_dotenv_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Dedicated workers should trust the committed startup payload instead of ambient runner env."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "OPENAI_API_KEY=dotenv-secret\nOPENAI_BASE_URL=http://example.invalid/v1\nCUSTOM_API_TOKEN=custom-secret\n",
        encoding="utf-8",
    )
    payload_runtime = resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={
            "MINDROOM_NAMESPACE": "alpha1234",
            "MINDROOM_SANDBOX_DEDICATED_WORKER_KEY": "worker-1",
        },
    )
    payload = serialize_runtime_paths(payload_runtime)
    payload["env_file_values"] = {"MINDROOM_NAMESPACE": "alpha1234"}
    manifest_path = _write_startup_manifest(
        runtime_paths=sandbox_runner_module.constants.deserialize_runtime_paths(payload),
    )
    _set_startup_manifest(monkeypatch, manifest_path=manifest_path)
    monkeypatch.setenv("MINDROOM_SANDBOX_PROXY_TOKEN", "from-env")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://runner-env.example/v1")
    monkeypatch.setenv("TEST_EXECUTION_ENV", "worker-visible")

    startup_runtime = sandbox_runner_module._startup_runtime_paths_from_env()
    execution_env = sandbox_exec_module.request_execution_env("shell", None, startup_runtime)
    effective_runtime = sandbox_exec_module.runtime_paths_with_execution_env(startup_runtime, execution_env)

    assert startup_runtime.env_value("MINDROOM_NAMESPACE") == "alpha1234"
    assert startup_runtime.env_value("OPENAI_API_KEY") is None
    assert startup_runtime.env_value("OPENAI_BASE_URL") is None
    assert startup_runtime.env_value("CUSTOM_API_TOKEN") is None
    assert startup_runtime.env_value("TEST_EXECUTION_ENV") is None
    assert "OPENAI_API_KEY" not in execution_env
    assert "OPENAI_BASE_URL" not in execution_env
    assert "CUSTOM_API_TOKEN" not in execution_env
    assert "TEST_EXECUTION_ENV" not in execution_env
    assert effective_runtime.env_value("OPENAI_BASE_URL") is None
    assert effective_runtime.env_value("TEST_EXECUTION_ENV") is None


@pytest.mark.asyncio
async def test_dedicated_worker_inprocess_shell_does_not_see_runner_local_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Dedicated-worker in-process shell should not observe ambient runner env."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    payload_runtime = resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={
            "MINDROOM_NAMESPACE": "alpha1234",
            "MINDROOM_SANDBOX_DEDICATED_WORKER_KEY": "worker-1",
        },
    )
    payload = serialize_runtime_paths(payload_runtime)
    payload["env_file_values"] = {"MINDROOM_NAMESPACE": "alpha1234"}
    manifest_path = _write_startup_manifest(
        runtime_paths=sandbox_runner_module.constants.deserialize_runtime_paths(payload),
    )
    _set_startup_manifest(monkeypatch, manifest_path=manifest_path)
    monkeypatch.setenv("MINDROOM_SANDBOX_PROXY_TOKEN", SANDBOX_TOKEN)
    monkeypatch.setenv("OPENAI_BASE_URL", "http://runner-env.example/v1")
    monkeypatch.setenv("TEST_EXECUTION_ENV", "worker-visible")

    startup_runtime = sandbox_runner_module._startup_runtime_paths_from_env()
    response = await sandbox_runner_module._execute_request_inprocess(
        sandbox_runner_module.SandboxRunnerExecuteRequest(
            tool_name="shell",
            function_name="run_shell_command",
            args=[
                [
                    "bash",
                    "-lc",
                    "printf '%s' \"$OPENAI_BASE_URL|$TEST_EXECUTION_ENV|$MINDROOM_SANDBOX_PROXY_TOKEN|$MINDROOM_NAMESPACE\"",
                ],
            ],
            kwargs={},
        ),
        startup_runtime,
        sandbox_runner_module._runtime_config_or_empty(startup_runtime),
        runner_token=SANDBOX_TOKEN,
    )

    assert response.ok is True
    assert response.result == "|||alpha1234"


def test_public_startup_runtime_payload_excludes_runner_token(tmp_path: Path) -> None:
    """Public startup runtime payloads should not serialize the runner auth token."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("models: {}\nagents: {}\n", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "MINDROOM_EVENT_CACHE_DATABASE_URL=postgresql://cache:file-secret@db/mindroom\n"
        "MINDROOM_CACHE_DATABASE_URL=postgresql://cache:custom-file-secret@db/mindroom\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={
            "MINDROOM_EVENT_CACHE_DATABASE_URL": "postgresql://cache:process-secret@db/mindroom",
            "MINDROOM_CACHE_DATABASE_URL": "postgresql://cache:custom-process-secret@db/mindroom",
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
    assert "MINDROOM_EVENT_CACHE_DATABASE_URL" not in payload["env_file_values"]
    assert "MINDROOM_CACHE_DATABASE_URL" not in payload["env_file_values"]


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


@pytest.mark.asyncio
async def test_execute_request_inprocess_marks_tool_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Ordinary tool exceptions should be labeled as tool failures."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={},
    )

    class _FakeToolkit:
        requires_connect = False

    def _raising_entrypoint(*_args: object, **_kwargs: object) -> object:
        missing_path = "missing.txt"
        raise FileNotFoundError(missing_path)

    monkeypatch.setattr(
        sandbox_runner_module,
        "_resolve_entrypoint",
        lambda **_kwargs: (_FakeToolkit(), _raising_entrypoint),
    )

    response = await sandbox_runner_module._execute_request_inprocess(
        sandbox_runner_module.SandboxRunnerExecuteRequest(
            tool_name="file",
            function_name="read_file",
            args=["missing.txt"],
            kwargs={},
        ),
        runtime_paths,
        sandbox_runner_module._runtime_config_or_empty(runtime_paths),
    )

    assert response.ok is False
    assert response.error == "Sandbox tool execution failed: FileNotFoundError: missing.txt"
    assert response.failure_kind == "tool"


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_output_path", [None, "", "   \t\n"])
async def test_execute_request_inprocess_ignores_empty_tool_output_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    raw_output_path: object,
) -> None:
    """Null or blank output paths should behave like omitted output paths."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={},
    )

    class _FakeToolkit:
        requires_connect = False

    def _fake_entrypoint() -> dict[str, object]:
        return {"called": True}

    def _unexpected_output_root_resolution(**_kwargs: object) -> Path | None:
        pytest.fail("empty mindroom_output_path must not resolve a tool output workspace root")

    def _fake_resolve_entrypoint(**kwargs: object) -> tuple[_FakeToolkit, object]:
        assert kwargs["tool_output_workspace_root"] is None
        return _FakeToolkit(), _fake_entrypoint

    monkeypatch.setattr(
        sandbox_runner_module,
        "_runner_tool_output_workspace_root",
        _unexpected_output_root_resolution,
    )
    monkeypatch.setattr(sandbox_runner_module, "_resolve_entrypoint", _fake_resolve_entrypoint)

    response = await sandbox_runner_module._execute_request_inprocess(
        sandbox_runner_module.SandboxRunnerExecuteRequest(
            tool_name="file",
            function_name="list_files",
            args=[],
            kwargs={sandbox_runner_module.OUTPUT_PATH_ARGUMENT: raw_output_path},
        ),
        runtime_paths,
        sandbox_runner_module._runtime_config_or_empty(runtime_paths),
    )

    assert response.ok is True
    assert response.result == {"called": True}


@pytest.mark.asyncio
async def test_execute_request_inprocess_preserves_normal_null_arguments(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Only the reserved output-path argument treats null as omission."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={},
    )

    class _FakeToolkit:
        requires_connect = False

    def _fake_entrypoint(limit: int | None = 10) -> dict[str, object]:
        return {"limit": limit}

    def _fake_resolve_entrypoint(**kwargs: object) -> tuple[_FakeToolkit, object]:
        assert kwargs["tool_output_workspace_root"] is None
        return _FakeToolkit(), _fake_entrypoint

    monkeypatch.setattr(sandbox_runner_module, "_resolve_entrypoint", _fake_resolve_entrypoint)

    response = await sandbox_runner_module._execute_request_inprocess(
        sandbox_runner_module.SandboxRunnerExecuteRequest(
            tool_name="fake",
            function_name="with_optional_limit",
            args=[],
            kwargs={"limit": None},
        ),
        runtime_paths,
        sandbox_runner_module._runtime_config_or_empty(runtime_paths),
    )

    assert response.ok is True
    assert response.result == {"limit": None}


def test_execute_request_subprocess_sync_marks_subprocess_timeouts_as_worker_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Subprocess failures should be labeled as worker failures."""
    _set_sandbox_token(monkeypatch)
    monkeypatch.setenv("MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE", "subprocess")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={},
    )

    def _timeout(*_args: object, **_kwargs: object) -> object:
        raise subprocess.TimeoutExpired(cmd=["python"], timeout=5.0)

    monkeypatch.setattr(sandbox_runner_module.subprocess, "run", _timeout)

    response = sandbox_runner_module._execute_request_subprocess_sync(
        sandbox_runner_module.SandboxRunnerExecuteRequest(
            tool_name="calculator",
            function_name="add",
            args=[1, 2],
            kwargs={},
        ),
        runtime_paths,
        sandbox_runner_module._runtime_config_or_empty(runtime_paths),
        runner_token=SANDBOX_TOKEN,
    )

    assert response.ok is False
    assert response.error == "Sandbox subprocess timed out."
    assert response.failure_kind == "worker"


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
        worker_target=None,
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


def test_resolve_entrypoint_applies_tool_config_overrides_over_persisted_config(tmp_path: Path) -> None:
    """Sandbox-side rebuilds should let authored overrides beat persisted non-secret config."""
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
        worker_target=None,
    )

    toolkit, entrypoint = sandbox_runner_module._resolve_entrypoint(
        runtime_paths=runtime_paths,
        config=sandbox_runner_module._runtime_config_or_empty(runtime_paths),
        tool_name="clickup",
        function_name="list_spaces",
        tool_config_overrides={"master_space_id": "space-override"},
    )

    assert toolkit.api_key == "clickup-test"
    assert toolkit.master_space_id == "space-override"
    assert entrypoint is not None


def test_resolve_entrypoint_inherit_sentinel_falls_back_to_persisted_config(tmp_path: Path) -> None:
    """Sentinel overrides should remove a higher-priority authored value and reuse persisted config."""
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
        worker_target=None,
    )

    toolkit, entrypoint = sandbox_runner_module._resolve_entrypoint(
        runtime_paths=runtime_paths,
        config=sandbox_runner_module._runtime_config_or_empty(runtime_paths),
        tool_name="clickup",
        function_name="list_spaces",
        tool_config_overrides={"master_space_id": metadata_module.AUTHORED_OVERRIDE_INHERIT},
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
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("MINDROOM_API_KEY", "dashboard-secret")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "OPENAI_API_KEY=dotenv-secret\n"
        "TEST_EXECUTION_ENV=visible-in-shell\n"
        "MINDROOM_EVENT_CACHE_DATABASE_URL=postgresql://cache:dotenv-secret@db/mindroom\n"
        "MINDROOM_CACHE_DATABASE_URL=postgresql://cache:custom-dotenv-secret@db/mindroom\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={
            **dict(os.environ),
            "MINDROOM_EVENT_CACHE_DATABASE_URL": "postgresql://cache:process-secret@db/mindroom",
            "MINDROOM_CACHE_DATABASE_URL": "postgresql://cache:custom-process-secret@db/mindroom",
        },
    )

    execution_env = sandbox_exec_module.request_execution_env(
        "shell",
        None,
        runtime_paths,
    )

    assert execution_env["OPENAI_API_KEY"] == "dotenv-secret"
    assert execution_env["TEST_EXECUTION_ENV"] == "visible-in-shell"
    assert execution_env["MINDROOM_STORAGE_PATH"] == str((tmp_path / "storage").resolve())
    assert "MINDROOM_EVENT_CACHE_DATABASE_URL" not in execution_env
    assert "MINDROOM_CACHE_DATABASE_URL" not in execution_env
    assert "MINDROOM_SANDBOX_PROXY_TOKEN" not in execution_env
    assert "MINDROOM_API_KEY" not in execution_env


@pytest.mark.asyncio
async def test_execute_request_inprocess_reuses_passed_config_without_execution_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """In-process runner should not reparse config when runtime paths are unchanged."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )
    config = sandbox_runner_module._runtime_config_or_empty(runtime_paths)
    request = sandbox_runner_module.SandboxRunnerExecuteRequest(
        tool_name="calculator",
        function_name="add",
        args=[1, 2],
        kwargs={},
    )

    monkeypatch.setattr(sandbox_runner_module.sandbox_exec, "request_execution_env", lambda *_args: {})
    monkeypatch.setattr(
        sandbox_runner_module,
        "_runtime_config_or_empty",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("config should not be reloaded")),
    )

    class _Toolkit:
        requires_connect = False

    async def _entrypoint(*_args: object, **_kwargs: object) -> int:
        return 3

    def _fake_resolve_entrypoint(**_kwargs: object) -> tuple[_Toolkit, object]:
        return _Toolkit(), _entrypoint

    monkeypatch.setattr(
        sandbox_runner_module,
        "_resolve_entrypoint",
        _fake_resolve_entrypoint,
    )

    response = await sandbox_runner_module._execute_request_inprocess(
        request,
        runtime_paths,
        config,
    )

    assert response.ok is True
    assert response.result == 3


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


def test_sandbox_runner_execute_returns_422_for_invalid_runtime_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Explicit runner refresh should reject invalid runtime config before committing it."""
    _set_sandbox_token(monkeypatch)
    monkeypatch.setenv("MINDROOM_CONFIG_PATH", str(_invalid_plugin_config_path(tmp_path)))
    monkeypatch.setenv("MINDROOM_STORAGE_PATH", str(tmp_path / ".mindroom"))
    with pytest.raises(ConfigRuntimeValidationError) as exc_info:
        _refresh_runner_app_from_env()

    assert str(exc_info.value) == (
        "Invalid plugin name: 'BadName'. Plugin names must use lowercase ASCII letters, digits, "
        "hyphens, or underscores. (" + str((tmp_path / "plugins" / "bad-name" / "mindroom.plugin.json").resolve()) + ")"
    )


def test_sandbox_runner_skips_unavailable_plugins_for_worker_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Worker startup should not fail on plugin paths missing from the worker filesystem."""
    config_path = _missing_plugin_path_config_path(tmp_path)
    monkeypatch.setenv("MINDROOM_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("MINDROOM_STORAGE_PATH", str(tmp_path / ".mindroom"))
    monkeypatch.setenv("MINDROOM_SANDBOX_DEDICATED_WORKER_KEY", "worker-a")
    _set_worker_tool_validation_snapshot("agentspace_slack_search")
    _set_sandbox_token(monkeypatch)

    runtime_paths, config = _refresh_runner_app_from_env()

    assert runtime_paths.config_path.exists()
    assert config.plugins == []

    with (
        patch("mindroom.workers.backends.local.venv.EnvBuilder.create", new=_fake_local_worker_venv_create),
        TestClient(sandbox_runner_app) as client,
    ):
        response = client.post(
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
    assert response.json()["ok"] is True
    assert '"result": 3' in response.json()["result"]


def test_sandbox_runner_shared_startup_still_rejects_missing_plugins(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Shared runner startup must keep canonical plugin validation semantics."""
    config_path = _missing_plugin_path_config_path(tmp_path)
    monkeypatch.setenv("MINDROOM_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("MINDROOM_STORAGE_PATH", str(tmp_path / ".mindroom"))
    _set_worker_tool_validation_snapshot("agentspace_slack_search")
    monkeypatch.setenv("MINDROOM_SANDBOX_PROXY_TOKEN", SANDBOX_TOKEN)

    with pytest.raises(ConfigRuntimeValidationError, match="Configured plugin path does not exist"):
        _refresh_runner_app_from_env()


def test_sandbox_runner_defers_unavailable_authored_tools_for_worker_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Dedicated workers should trust primary-runtime config validation and reject only executed tools."""
    config_path = _missing_plugin_path_with_invalid_tool_config_path(tmp_path)
    monkeypatch.setenv("MINDROOM_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("MINDROOM_STORAGE_PATH", str(tmp_path / ".mindroom"))
    monkeypatch.setenv("MINDROOM_SANDBOX_DEDICATED_WORKER_KEY", "worker-a")
    _set_worker_tool_validation_snapshot("agentspace_slack_search")
    _set_sandbox_token(monkeypatch)

    runtime_paths, config = _refresh_runner_app_from_env()

    assert runtime_paths.config_path.exists()
    assert config.plugins == []

    with (
        patch("mindroom.workers.backends.local.venv.EnvBuilder.create", new=_fake_local_worker_venv_create),
        TestClient(sandbox_runner_app) as client,
    ):
        response = client.post(
            "/api/sandbox-runner/execute",
            headers=SANDBOX_HEADERS,
            json={
                "tool_name": "definitely_not_a_tool",
                "function_name": "run",
                "args": [],
                "kwargs": {},
            },
        )

    assert response.status_code == 200
    response_payload = response.json()
    assert response_payload["ok"] is False
    assert "Unknown tool: definitely_not_a_tool" in response_payload["error"]


def test_sandbox_runner_execute_rejects_invalid_mcp_tool_overrides(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Execute requests should use the same MCP-specific override validation as config loading."""
    config_path = _mcp_demo_config_path(tmp_path)
    monkeypatch.setenv("MINDROOM_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("MINDROOM_STORAGE_PATH", str(tmp_path / ".mindroom"))
    _set_sandbox_token(monkeypatch)
    _refresh_runner_app_from_env()

    with TestClient(sandbox_runner_app) as client:
        response = client.post(
            "/api/sandbox-runner/execute",
            headers=SANDBOX_HEADERS,
            json={
                "tool_name": "mcp_demo",
                "function_name": "demo",
                "args": [],
                "kwargs": {},
                "tool_config_overrides": {
                    "include_tools": ["echo"],
                    "exclude_tools": ["echo"],
                },
            },
        )

    assert response.status_code == 400
    assert "include_tools and exclude_tools overlap" in response.json()["detail"]


def test_sandbox_runner_execute_uses_committed_startup_config_until_explicit_refresh(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Execute requests should keep using the runner's committed startup config after later disk drift."""
    _set_sandbox_token(monkeypatch)
    runtime_paths = sandbox_runner_module._app_runtime_paths(sandbox_runner_app)
    runtime_paths.config_path.write_text("models: [\n", encoding="utf-8")

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


def test_sandbox_runner_applies_shell_path_prepend_override(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sandbox runner should allow shell_path_prepend for shell execution."""
    _set_sandbox_token(monkeypatch)
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin:/bin")
    _refresh_runner_app_from_env()

    response = runner_client.post(
        "/api/sandbox-runner/execute",
        headers=SANDBOX_HEADERS,
        json={
            "tool_name": "shell",
            "function_name": "run_shell_command",
            "args": [
                [
                    sys.executable,
                    "-c",
                    'import os, sys; sys.stdout.write(os.environ.get("PATH", ""))',
                ],
            ],
            "kwargs": {},
            "tool_init_overrides": {"shell_path_prepend": "/opt/custom/bin, /opt/worker/bin"},
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["result"].startswith("/opt/custom/bin:/opt/worker/bin:")
    assert data["result"].endswith("/usr/local/bin:/usr/bin:/bin")


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
    original_builtin_registry = metadata_module._BUILTIN_TOOL_REGISTRY.copy()
    original_builtin_metadata = metadata_module._BUILTIN_TOOL_METADATA.copy()
    original_manager = credentials_module._credentials_manager
    original_signature = credentials_module._credentials_manager_signature
    shared_storage = tmp_path / "shared-storage"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MINDROOM_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("MINDROOM_STORAGE_PATH", str(shared_storage))
    metadata_module.register_builtin_tool_metadata(
        ToolMetadata(
            name=tool_name,
            display_name="Dummy",
            description="Dummy",
            category=ToolCategory.DEVELOPMENT,
            status=ToolStatus.REQUIRES_CONFIG,
            setup_type=SetupType.API_KEY,
            config_fields=[ConfigField(name="token", label="Token", type="password", required=False)],
            factory=lambda: DummyTool,
        ),
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
        metadata_module._BUILTIN_TOOL_REGISTRY.clear()
        metadata_module._BUILTIN_TOOL_REGISTRY.update(original_builtin_registry)
        TOOL_METADATA.clear()
        TOOL_METADATA.update(original_metadata)
        metadata_module._BUILTIN_TOOL_METADATA.clear()
        metadata_module._BUILTIN_TOOL_METADATA.update(original_builtin_metadata)
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
    original_builtin_registry = metadata_module._BUILTIN_TOOL_REGISTRY.copy()
    original_builtin_metadata = metadata_module._BUILTIN_TOOL_METADATA.copy()
    original_manager = credentials_module._credentials_manager
    original_signature = credentials_module._credentials_manager_signature
    storage_root = tmp_path / "runtime-storage"
    monkeypatch.setenv("MINDROOM_STORAGE_PATH", str(storage_root))
    metadata_module.register_builtin_tool_metadata(
        ToolMetadata(
            name=tool_name,
            display_name="Dummy",
            description="Dummy",
            category=ToolCategory.DEVELOPMENT,
            status=ToolStatus.REQUIRES_CONFIG,
            setup_type=SetupType.API_KEY,
            config_fields=[ConfigField(name="token", label="Token", type="password", required=False)],
            factory=lambda: DummyTool,
        ),
    )

    try:
        CredentialsManager(base_path=storage_root / "credentials").save_credentials(
            tool_name,
            {"token": stored_value},
        )
        credentials_module._credentials_manager = None
        credentials_module._credentials_manager_signature = None

        toolkit = get_tool_by_name(
            tool_name,
            resolve_runtime_paths(config_path=Path("config.yaml")),
            worker_target=None,
        )

        assert toolkit.token == stored_value
    finally:
        metadata_module._TOOL_REGISTRY.clear()
        metadata_module._TOOL_REGISTRY.update(original_registry)
        metadata_module._BUILTIN_TOOL_REGISTRY.clear()
        metadata_module._BUILTIN_TOOL_REGISTRY.update(original_builtin_registry)
        TOOL_METADATA.clear()
        TOOL_METADATA.update(original_metadata)
        metadata_module._BUILTIN_TOOL_METADATA.clear()
        metadata_module._BUILTIN_TOOL_METADATA.update(original_builtin_metadata)
        credentials_module._credentials_manager = original_manager
        credentials_module._credentials_manager_signature = original_signature


def test_resolve_worker_base_dir_does_not_create_directories_during_validation(tmp_path: Path) -> None:
    """Worker base-dir validation should not leave empty directories behind."""
    storage_root = tmp_path / "mindroom_data"
    worker_root = tmp_path / "workers" / "worker-state"
    requested_base_dir = "agents/general/workspace"

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
    _refresh_runner_app_from_env()
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


def test_sandbox_runner_shell_handles_survive_requests_in_subprocess_mode(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Shell background handles should keep working across requests in subprocess runner mode."""
    _set_sandbox_token(monkeypatch)
    monkeypatch.setenv("MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE", "subprocess")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MINDROOM_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("MINDROOM_STORAGE_PATH", str(tmp_path / "storage"))
    _refresh_runner_app_from_env()

    run_response = runner_client.post(
        "/api/sandbox-runner/execute",
        headers=SANDBOX_HEADERS,
        json={
            "tool_name": "shell",
            "function_name": "run_shell_command",
            "args": [["sleep", "3"]],
            "kwargs": {"timeout": 0},
        },
    )
    assert run_response.status_code == 200
    run_data = run_response.json()
    assert run_data["ok"] is True
    assert "Handle: " in run_data["result"]
    handle = run_data["result"].split("Handle: ")[1].split("\n")[0]

    check_response = runner_client.post(
        "/api/sandbox-runner/execute",
        headers=SANDBOX_HEADERS,
        json={
            "tool_name": "shell",
            "function_name": "check_shell_command",
            "args": [handle],
            "kwargs": {},
        },
    )
    assert check_response.status_code == 200
    check_data = check_response.json()
    assert check_data["ok"] is True
    assert "Unknown handle" not in check_data["result"]

    kill_response = runner_client.post(
        "/api/sandbox-runner/execute",
        headers=SANDBOX_HEADERS,
        json={
            "tool_name": "shell",
            "function_name": "kill_shell_command",
            "args": [handle],
            "kwargs": {"force": True},
        },
    )
    assert kill_response.status_code == 200
    kill_data = kill_response.json()
    assert kill_data["ok"] is True
    assert "Unknown handle" not in kill_data["result"]


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


def test_sandbox_runner_rejects_disallowed_authored_base_dir_override(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Authored config should not be allowed to set runtime-only base_dir fields."""
    _set_sandbox_token(monkeypatch)

    response = runner_client.post(
        "/api/sandbox-runner/execute",
        headers=SANDBOX_HEADERS,
        json={
            "tool_name": "coding",
            "function_name": "ls",
            "args": [],
            "kwargs": {"path": "."},
            "tool_config_overrides": {"base_dir": "/workspace"},
        },
    )

    assert response.status_code == 400
    assert "request.tool_config_overrides.coding.base_dir" in response.json()["detail"]


def test_sandbox_runner_rejects_password_authored_override(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Password-backed config fields should stay credential-only in runner requests."""
    _set_sandbox_token(monkeypatch)

    response = runner_client.post(
        "/api/sandbox-runner/execute",
        headers=SANDBOX_HEADERS,
        json={
            "tool_name": "clickup",
            "function_name": "list_spaces",
            "args": [],
            "kwargs": {},
            "tool_config_overrides": {"api_key": "test-key"},
        },
    )

    assert response.status_code == 400
    assert "request.tool_config_overrides.clickup.api_key" in response.json()["detail"]


def test_sandbox_runner_rejects_unknown_authored_override_field(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown authored override fields should be rejected with path context."""
    _set_sandbox_token(monkeypatch)

    response = runner_client.post(
        "/api/sandbox-runner/execute",
        headers=SANDBOX_HEADERS,
        json={
            "tool_name": "shell",
            "function_name": "run_shell_command",
            "args": [["echo", "hello"]],
            "kwargs": {},
            "tool_config_overrides": {"missing_field": True},
        },
    )

    assert response.status_code == 400
    assert "request.tool_config_overrides.shell.missing_field" in response.json()["detail"]


def test_sandbox_runner_execute_refreshes_plugin_metadata_before_override_validation(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Execute prevalidation should use the runner's current committed plugin metadata."""
    _set_sandbox_token(monkeypatch)
    plugin_root = tmp_path / "plugins" / "demo"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "demo_plugin", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    (plugin_root / "tools.py").write_text(
        "from agno.tools import Toolkit\n"
        "from mindroom.tool_system.metadata import ConfigField, ToolCategory, register_tool_with_metadata\n"
        "\n"
        "class DemoPluginTool(Toolkit):\n"
        "    def __init__(self, label: str | None = None) -> None:\n"
        "        super().__init__(name='demo_plugin', tools=[])\n"
        "        self.label = label\n"
        "\n"
        "@register_tool_with_metadata(\n"
        "    name='demo_plugin',\n"
        "    display_name='Demo Plugin',\n"
        "    description='Demo plugin tool',\n"
        "    category=ToolCategory.DEVELOPMENT,\n"
        "    config_fields=[ConfigField(name='label', label='Label', type='text', required=False)],\n"
        ")\n"
        "def demo_plugin_tools():\n"
        "    return DemoPluginTool\n",
        encoding="utf-8",
    )
    config_path = Path(os.environ["MINDROOM_CONFIG_PATH"])
    config_path.write_text(
        "models:\n"
        "  default:\n"
        "    provider: openai\n"
        "    id: gpt-5.4\n"
        "agents: {}\n"
        "router:\n"
        "  model: default\n"
        "plugins:\n"
        "  - ./plugins/demo\n",
        encoding="utf-8",
    )
    _refresh_runner_app_from_env()

    response = runner_client.post(
        "/api/sandbox-runner/execute",
        headers=SANDBOX_HEADERS,
        json={
            "tool_name": "demo_plugin",
            "function_name": "missing",
            "args": [],
            "kwargs": {},
            "tool_config_overrides": {"label": "hello"},
        },
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Tool 'demo_plugin' does not expose 'missing'."


def test_sandbox_runner_execute_refreshes_plugin_metadata_before_tool_init_override_validation(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Tool init override prevalidation should also use the runner's current committed plugin metadata."""
    _set_sandbox_token(monkeypatch)
    plugin_root = tmp_path / "plugins" / "demo-init"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "demo_plugin_init", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    (plugin_root / "tools.py").write_text(
        "from agno.tools import Toolkit\n"
        "from mindroom.tool_system.metadata import ConfigField, ToolCategory, register_tool_with_metadata\n"
        "\n"
        "class DemoPluginInitTool(Toolkit):\n"
        "    def __init__(self, base_dir: str | None = None) -> None:\n"
        "        super().__init__(name='demo_plugin_init', tools=[])\n"
        "        self.base_dir = base_dir\n"
        "\n"
        "@register_tool_with_metadata(\n"
        "    name='demo_plugin_init',\n"
        "    display_name='Demo Plugin Init',\n"
        "    description='Demo plugin tool with init overrides',\n"
        "    category=ToolCategory.DEVELOPMENT,\n"
        "    config_fields=[ConfigField(name='base_dir', label='Base dir', type='path', required=False)],\n"
        ")\n"
        "def demo_plugin_init_tools():\n"
        "    return DemoPluginInitTool\n",
        encoding="utf-8",
    )
    config_path = Path(os.environ["MINDROOM_CONFIG_PATH"])
    config_path.write_text(
        "models:\n"
        "  default:\n"
        "    provider: openai\n"
        "    id: gpt-5.4\n"
        "agents: {}\n"
        "router:\n"
        "  model: default\n"
        "plugins:\n"
        "  - ./plugins/demo-init\n",
        encoding="utf-8",
    )
    _refresh_runner_app_from_env()

    response = runner_client.post(
        "/api/sandbox-runner/execute",
        headers=SANDBOX_HEADERS,
        json={
            "tool_name": "demo_plugin_init",
            "function_name": "missing",
            "args": [],
            "kwargs": {},
            "tool_init_overrides": {"base_dir": "agents/general/workspace"},
        },
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Tool 'demo_plugin_init' does not expose 'missing'."


def test_sandbox_runner_subprocess_rejects_unsafe_tool_init_overrides(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unsafe tool init overrides should be rejected before subprocess execution starts."""
    _set_sandbox_token(monkeypatch)
    monkeypatch.setenv("MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE", "subprocess")
    _refresh_runner_app_from_env()

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
    _refresh_runner_app_from_env()

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

    with patch("mindroom.workers.backends.local.venv.EnvBuilder.create", new=_fake_local_worker_venv_create):
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
    _refresh_runner_app_from_env()

    with patch("mindroom.workers.backends.local.venv.EnvBuilder.create", new=_fake_local_worker_venv_create):
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
    _refresh_runner_app_from_env()

    with patch("mindroom.workers.backends.local.venv.EnvBuilder.create", new=_fake_local_worker_venv_create):
        response = runner_client.post(
            "/api/sandbox-runner/execute",
            headers=SANDBOX_HEADERS,
            json={
                "tool_name": "file",
                "function_name": "save_file",
                "args": ["hello", "note.txt"],
                "kwargs": {},
                "worker_key": worker_key,
                "tool_init_overrides": {"base_dir": "agents/general/workspace"},
            },
        )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    saved_file = shared_root / "agents" / "general" / "workspace" / "note.txt"
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
    _refresh_runner_app_from_env()

    def fake_create(_self: object, venv_dir: Path) -> None:
        (venv_dir / "bin").mkdir(parents=True, exist_ok=True)
        (venv_dir / "bin" / "python").symlink_to(Path(sys.executable))

    with patch("mindroom.workers.backends.local.venv.EnvBuilder.create", new=fake_create):
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
    _refresh_runner_app_from_env()

    with patch("mindroom.workers.backends.local.venv.EnvBuilder.create", new=_fake_local_worker_venv_create):
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

    with patch("mindroom.workers.backends.local.venv.EnvBuilder.create", new=_fake_local_worker_venv_create):
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
    _refresh_runner_app_from_env()

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
            "tool_init_overrides": {"base_dir": "agents/general/workspace"},
        },
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "result": "ok", "error": None, "failure_kind": None}
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
    _refresh_runner_app_from_env()

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
    worker_target = captured_kwargs["worker_target"]
    assert worker_target.worker_scope == "shared"
    assert worker_target.routing_agent_name == "general"


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
    _refresh_runner_app_from_env()

    def fake_create(_self: object, venv_dir: Path) -> None:
        (venv_dir / "bin").mkdir(parents=True, exist_ok=True)
        (venv_dir / "bin" / "python").symlink_to(Path(sys.executable))

    with patch("mindroom.workers.backends.local.venv.EnvBuilder.create", new=fake_create):
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
    _refresh_runner_app_from_env()

    with patch("mindroom.workers.backends.local.venv.EnvBuilder.create", new=_fake_local_worker_venv_create):
        response = runner_client.post(
            "/api/sandbox-runner/execute",
            headers=SANDBOX_HEADERS,
            json={
                "tool_name": "file",
                "function_name": "save_file",
                "args": ["hello from canonical workspace", "note.txt"],
                "kwargs": {},
                "worker_key": worker_key,
                "tool_init_overrides": {"base_dir": "agents/general/workspace"},
            },
        )

    assert response.status_code == 200
    assert response.json()["ok"] is True

    canonical_file = agent_workspace_root_path(storage_root, "general") / "note.txt"
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
    _refresh_runner_app_from_env()

    canonical_base_dir = agent_workspace_root_path(storage_root, "general") / "mind_data"
    with patch("mindroom.workers.backends.local.venv.EnvBuilder.create", new=_fake_local_worker_venv_create):
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


def test_prepare_worker_request_shared_worker_does_not_read_private_agent_names(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Shared workers should not consult private-agent config visibility."""
    worker_key = "v1:tenant-123:shared:general"
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")
    worker_handle = SimpleNamespace()
    worker_paths = local_workers_module._local_worker_state_paths_for_root(tmp_path / "workers" / "general")

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
    )

    assert prepared.handle is worker_handle


def test_prepare_worker_request_user_agent_private_visibility_comes_from_explicit_names(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """User-agent workers should derive private visibility from the provided names."""
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

    monkeypatch.setattr(sandbox_worker_prep_module, "prepare_worker", lambda *_args, **_kwargs: worker_handle)
    monkeypatch.setattr(
        sandbox_worker_prep_module,
        "local_worker_state_paths_from_handle",
        lambda _handle: worker_paths,
    )

    prepared = sandbox_worker_prep_module.prepare_worker_request(
        worker_key=worker_key,
        tool_init_overrides={
            "base_dir": str(
                _private_instance_state_root_path(
                    runtime_paths.storage_root,
                    worker_key=worker_key,
                    agent_name="mind",
                ),
            ),
        },
        runtime_paths=runtime_paths,
        private_agent_names=frozenset({"mind"}),
    )

    assert prepared.handle is worker_handle


def test_prepare_worker_request_rejects_sibling_private_agent_root_for_user_agent_workers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """User-agent workers must not accept sibling private agent roots."""
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

    monkeypatch.setattr(sandbox_worker_prep_module, "prepare_worker", lambda *_args, **_kwargs: worker_handle)
    monkeypatch.setattr(
        sandbox_worker_prep_module,
        "local_worker_state_paths_from_handle",
        lambda _handle: worker_paths,
    )

    with pytest.raises(
        sandbox_worker_prep_module.WorkerRequestPreparationError,
        match="base_dir must stay inside the allowed state roots or worker root",
    ):
        sandbox_worker_prep_module.prepare_worker_request(
            worker_key=worker_key,
            tool_init_overrides={
                "base_dir": str(
                    _private_instance_state_root_path(
                        runtime_paths.storage_root,
                        worker_key=worker_key,
                        agent_name="other_agent",
                    ),
                ),
            },
            runtime_paths=runtime_paths,
            private_agent_names=frozenset({"mind"}),
        )


def test_prepare_worker_request_requires_explicit_private_visibility_for_user_agent_workers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """User-agent workers should fail closed without explicit private visibility."""
    worker_key = "v1:tenant-123:user_agent:mind:@alice:example.org"
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")
    worker_handle = SimpleNamespace()
    worker_paths = local_workers_module._local_worker_state_paths_for_root(tmp_path / "workers" / "mind")

    def _prepare_worker(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return worker_handle

    def _local_worker_state_paths_from_handle(_handle: object) -> local_workers_module.LocalWorkerStatePaths:
        return worker_paths

    monkeypatch.setattr(sandbox_worker_prep_module, "prepare_worker", _prepare_worker)
    monkeypatch.setattr(
        sandbox_worker_prep_module,
        "local_worker_state_paths_from_handle",
        _local_worker_state_paths_from_handle,
    )
    with pytest.raises(
        sandbox_worker_prep_module.WorkerRequestPreparationError,
        match="user_agent workers require explicit private-agent visibility",
    ):
        sandbox_worker_prep_module.prepare_worker_request(
            worker_key=worker_key,
            tool_init_overrides={"base_dir": "private_instances/example/mind"},
            runtime_paths=runtime_paths,
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
    _refresh_runner_app_from_env()

    expected_base_dir = agent_workspace_root_path(shared_root, "general")

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
        assert runner_token == SANDBOX_TOKEN
        assert prepared_worker is not None
        assert prepared_worker.paths.root == worker_root
        assert prepared_worker.runtime_overrides["base_dir"] == expected_base_dir
        note_path = expected_base_dir / str(request.args[1])
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text(str(request.args[0]), encoding="utf-8")
        return sandbox_runner_module.SandboxRunnerExecuteResponse(ok=True, result="saved")

    monkeypatch.setattr(sandbox_runner_module, "_execute_request_subprocess", _fake_execute_request_subprocess)

    with patch("mindroom.workers.backends.local.venv.EnvBuilder.create", new=_fake_local_worker_venv_create):
        response = runner_client.post(
            "/api/sandbox-runner/execute",
            headers=SANDBOX_HEADERS,
            json={
                "tool_name": "file",
                "function_name": "save_file",
                "args": ["hello from dedicated worker canonical workspace", "note.txt"],
                "kwargs": {},
                "worker_key": worker_key,
                "tool_init_overrides": {"base_dir": "agents/general/workspace"},
            },
        )

    assert response.status_code == 200
    assert response.json()["ok"] is True

    canonical_file = expected_base_dir / "note.txt"
    assert canonical_file.read_text(encoding="utf-8") == "hello from dedicated worker canonical workspace"
    assert not (worker_root / "workspace" / "note.txt").exists()


def test_dedicated_worker_mode_allows_private_template_dir_missing_from_worker_filesystem(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Dedicated workers should not fail startup on control-plane-only private templates."""
    _set_sandbox_token(monkeypatch)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        (
            "models:\n"
            "  default:\n"
            "    provider: openai\n"
            "    id: gpt-5.4\n"
            "agents:\n"
            "  mind:\n"
            "    display_name: Mind\n"
            "    private:\n"
            "      per: user_agent\n"
            "      template_dir: ./missing-template\n"
            "router:\n"
            "  model: default\n"
        ),
        encoding="utf-8",
    )
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
    assert worker_key is not None
    shared_root = tmp_path / "shared-storage"
    worker_root = shared_root / "workers" / worker_dir_name(worker_key)
    private_base_dir = _private_instance_state_root_path(shared_root, worker_key=worker_key, agent_name="mind") / (
        "mind_data"
    )
    monkeypatch.setenv("MINDROOM_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("MINDROOM_SANDBOX_RUNNER_MODE", "true")
    monkeypatch.setenv("MINDROOM_SANDBOX_DEDICATED_WORKER_KEY", worker_key)
    monkeypatch.setenv("MINDROOM_SANDBOX_DEDICATED_WORKER_ROOT", str(worker_root))
    monkeypatch.setenv("MINDROOM_SANDBOX_SHARED_STORAGE_ROOT", str(shared_root))
    monkeypatch.setenv("MINDROOM_STORAGE_PATH", str(worker_root))
    _refresh_runner_app_from_env()

    async def _fake_execute_request_subprocess(
        request: sandbox_runner_module.SandboxRunnerExecuteRequest,
        runtime_paths: object,
        config: object,
        prepared_worker: object | None = None,
        *,
        runner_token: str | None = None,
    ) -> sandbox_runner_module.SandboxRunnerExecuteResponse:
        assert request.worker_key == worker_key
        assert request.private_agent_names == ["mind"]
        assert runtime_paths is not None
        assert config is not None
        assert runner_token == SANDBOX_TOKEN
        assert prepared_worker is not None
        assert prepared_worker.paths.root == worker_root
        assert prepared_worker.runtime_overrides["base_dir"] == private_base_dir
        note_path = private_base_dir / str(request.args[1])
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text(str(request.args[0]), encoding="utf-8")
        return sandbox_runner_module.SandboxRunnerExecuteResponse(ok=True, result="saved")

    monkeypatch.setattr(sandbox_runner_module, "_execute_request_subprocess", _fake_execute_request_subprocess)

    with patch("mindroom.workers.backends.local.venv.EnvBuilder.create", new=_fake_local_worker_venv_create):
        response = runner_client.post(
            "/api/sandbox-runner/execute",
            headers=SANDBOX_HEADERS,
            json={
                "tool_name": "file",
                "function_name": "save_file",
                "args": ["hello from private worker", "note.txt"],
                "kwargs": {},
                "worker_key": worker_key,
                "routing_agent_name": "mind",
                "private_agent_names": ["mind"],
                "tool_init_overrides": {"base_dir": str(private_base_dir)},
            },
        )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert (private_base_dir / "note.txt").read_text(encoding="utf-8") == "hello from private worker"


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
    _refresh_runner_app_from_env()

    response = runner_client.post(
        "/api/sandbox-runner/execute",
        headers=SANDBOX_HEADERS,
        json={
            "tool_name": "file",
            "function_name": "save_file",
            "args": ["hello from nested worker prefix", "note.txt"],
            "kwargs": {},
            "worker_key": worker_key,
            "tool_init_overrides": {"base_dir": "agents/general/workspace"},
        },
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True

    canonical_file = agent_workspace_root_path(shared_root, "general") / "note.txt"
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
    _refresh_runner_app_from_env()

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
    _refresh_runner_app_from_env()
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
def test_sandbox_runner_worker_shell_uses_worker_home_and_venv(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Worker-routed shell execution should inherit the worker runtime env."""
    _set_sandbox_token(monkeypatch)
    storage_root = tmp_path / "storage"
    monkeypatch.setenv("MINDROOM_STORAGE_PATH", str(storage_root))
    _refresh_runner_app_from_env()

    response = runner_client.post(
        "/api/sandbox-runner/execute",
        headers=SANDBOX_HEADERS,
        json={
            "tool_name": "shell",
            "function_name": "run_shell_command",
            "args": [["bash", "-lc", 'printf \'%s|%s|%s\' "$HOME" "$VIRTUAL_ENV" "$PWD"']],
            "kwargs": {},
            "worker_key": "worker-a",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True

    worker_root = storage_root / "workers" / worker_dir_name("worker-a")
    expected_result = f"{worker_root}|{worker_root / 'venv'}|{worker_root / 'workspace'}"
    assert data["result"] == expected_result


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
    _refresh_runner_app_from_env()

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
    _refresh_runner_app_from_env()

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
    _refresh_runner_app_from_env()

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
    _refresh_runner_app_from_env()

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
    _refresh_runner_app_from_env()

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

    with patch("mindroom.workers.backends.local.venv.EnvBuilder.create", new=_fake_local_worker_venv_create):
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

    assert execute_response.status_code == 200
    payload = execute_response.json()
    assert payload["ok"] is False
    assert payload["failure_kind"] == "worker"
    assert "Failed to initialize worker 'worker-fail'" in payload["error"]
    assert "boom" in payload["error"]

    workers_response = runner_client.get("/api/sandbox-runner/workers", headers=SANDBOX_HEADERS)
    assert workers_response.status_code == 200

    worker = next(worker for worker in workers_response.json()["workers"] if worker["worker_key"] == "worker-fail")
    assert worker["status"] == "failed"
    assert "boom" in worker["failure_reason"]
    assert worker["failure_count"] == 1


# ----------------------------------------------------------------------------
# Workspace env hook (.mindroom/worker-env.sh) integration tests
# ----------------------------------------------------------------------------


def _write_workspace_env_hook(workspace: Path, body: str) -> Path:
    hook_dir = workspace / ".mindroom"
    hook_dir.mkdir(parents=True, exist_ok=True)
    hook_path = hook_dir / "worker-env.sh"
    hook_path.write_text(body, encoding="utf-8")
    return hook_path


def test_workspace_env_hook_uses_routed_agent_workspace_without_base_dir(tmp_path: Path) -> None:
    """Agent-routed requests should not depend on a tool base_dir to source the hook."""
    config_path = tmp_path / "config.yaml"
    storage_root = tmp_path / "storage"
    runtime_paths = resolve_runtime_paths(config_path=config_path, storage_path=storage_root, process_env={})
    config = Config.validate_with_runtime(
        {
            "models": {"default": {"provider": "openai", "id": "gpt-5.4"}},
            "agents": {
                "general": {
                    "display_name": "General",
                    "memory_backend": "file",
                },
            },
            "router": {"model": "default"},
        },
        runtime_paths,
    )
    workspace = agent_workspace_root_path(storage_root, "general")
    workspace.mkdir(parents=True, exist_ok=True)
    _write_workspace_env_hook(
        workspace,
        'export WORKSPACE_HOOK_TOKEN=from-agent-workspace\nexport PATH="$PWD/.local/bin:$PATH"\n',
    )

    request = sandbox_runner_module.SandboxRunnerExecuteRequest(
        tool_name="shell",
        function_name="run_shell_command",
        routing_agent_name="general",
    )
    overlay, failure = sandbox_runner_module._workspace_env_overlay_for_request(
        request,
        prepared=None,
        execution_env={"PATH": "/usr/bin:/bin"},
        runtime_paths=runtime_paths,
        config=config,
        apply=True,
    )

    assert failure is None
    assert overlay["WORKSPACE_HOOK_TOKEN"] == "from-agent-workspace"  # noqa: S105
    assert overlay["PATH"].startswith(f"{workspace.resolve()}/.local/bin:")


def test_workspace_env_hook_subprocess_serializes_overlay_execution_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The subprocess child should receive the post-hook env, not the stale original request env."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_runtime_paths(config_path=config_path, storage_path=tmp_path / "storage", process_env={})
    config = Config.validate_with_runtime({}, runtime_paths)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_workspace_env_hook(workspace, "export PATH=/hook/bin:$PATH\n")
    captured_envelope: dict[str, object] = {}

    def fake_subprocess_run(
        _command: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        input_payload = kwargs["input"]
        assert isinstance(input_payload, str)
        captured_envelope["payload"] = sandbox_protocol_module.parse_subprocess_envelope(input_payload)
        env = kwargs["env"]
        assert env is None or isinstance(env, dict)
        captured_envelope["env"] = env
        cwd = kwargs["cwd"]
        assert cwd is None or isinstance(cwd, str)
        captured_envelope["cwd"] = cwd
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        timeout = kwargs["timeout"]
        assert isinstance(timeout, int | float)
        assert timeout >= 1.0
        assert kwargs["check"] is False
        response = sandbox_runner_module.SandboxRunnerExecuteResponse(ok=True, result="ok")
        return subprocess.CompletedProcess(
            args=_command,
            returncode=0,
            stdout="",
            stderr=sandbox_protocol_module.response_marker_payload(response.model_dump_json()),
        )

    monkeypatch.setattr(sandbox_runner_module.subprocess, "run", fake_subprocess_run)

    request = sandbox_runner_module.SandboxRunnerExecuteRequest(
        tool_name="shell",
        function_name="run_shell_command",
        args=[["bash", "-lc", "echo ok"]],
        kwargs={},
        execution_env={"PATH": "/usr/bin:/bin"},
        tool_init_overrides={"base_dir": str(workspace)},
    )
    response = sandbox_runner_module._execute_request_subprocess_sync(request, runtime_paths, config)

    assert response.ok is True
    envelope = captured_envelope["payload"]
    assert isinstance(envelope, sandbox_protocol_module.SandboxSubprocessEnvelope)
    assert envelope.request["execution_env"]["PATH"] == "/hook/bin:/usr/bin:/bin"
    env = captured_envelope["env"]
    assert isinstance(env, dict)
    assert env["PATH"] == "/hook/bin:/usr/bin:/bin"


def test_workspace_env_hook_shell_side_effects_do_not_reach_command(tmp_path: Path) -> None:
    """Hook shell state such as `cd` should not leak into the command process."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_runtime_paths(config_path=config_path, storage_path=tmp_path / "storage", process_env={})
    config = Config.validate_with_runtime({}, runtime_paths)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_workspace_env_hook(workspace, "export WORKSPACE_HOOK_TOKEN=hooked\ncd /\n")

    request = sandbox_runner_module.SandboxRunnerExecuteRequest(
        tool_name="shell",
        function_name="run_shell_command",
        args=[["bash", "-c", 'printf "%s|%s" "$WORKSPACE_HOOK_TOKEN" "$PWD"']],
        kwargs={},
        execution_env={"PATH": os.environ["PATH"]},
        tool_init_overrides={"base_dir": str(workspace)},
    )
    response = sandbox_runner_module._execute_request_subprocess_sync(request, runtime_paths, config)

    assert response.ok is True, response
    token, pwd = str(response.result).split("|", 1)
    assert token == "hooked"  # noqa: S105
    assert pwd == str(workspace)


def test_workspace_env_hook_skips_non_execution_tools_for_routed_agent(tmp_path: Path) -> None:
    """Routed non-execution tools should not be blocked by a shell env hook."""
    config_path = tmp_path / "config.yaml"
    storage_root = tmp_path / "storage"
    runtime_paths = resolve_runtime_paths(config_path=config_path, storage_path=storage_root, process_env={})
    config = Config.validate_with_runtime(
        {
            "models": {"default": {"provider": "openai", "id": "gpt-5.4"}},
            "agents": {
                "general": {
                    "display_name": "General",
                    "memory_backend": "file",
                },
            },
            "router": {"model": "default"},
        },
        runtime_paths,
    )
    workspace = agent_workspace_root_path(storage_root, "general")
    workspace.mkdir(parents=True, exist_ok=True)
    _write_workspace_env_hook(workspace, 'echo "bad hook" >&2\nexit 5\n')

    request = sandbox_runner_module.SandboxRunnerExecuteRequest(
        tool_name="file",
        function_name="read_file",
        routing_agent_name="general",
    )
    overlay, failure = sandbox_runner_module._workspace_env_overlay_for_request(
        request,
        prepared=None,
        execution_env={"PATH": "/usr/bin:/bin"},
        runtime_paths=runtime_paths,
        config=config,
        apply=True,
    )

    assert overlay == {}
    assert failure is None


@REQUIRES_LINUX_LOCAL_WORKER
def test_workspace_env_hook_overlays_shell_execution(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """`.mindroom/worker-env.sh` exports should be visible to worker-routed shell calls."""
    _set_sandbox_token(monkeypatch)
    storage_root = tmp_path / "storage"
    workspace = storage_root / "agents" / "general" / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    _write_workspace_env_hook(
        workspace,
        'export WORKSPACE_HOOK_TOKEN=hello-from-hook\nexport PATH="$PWD/.local/bin:$PATH"\n',
    )
    monkeypatch.setenv("MINDROOM_STORAGE_PATH", str(storage_root))
    _refresh_runner_app_from_env()

    with patch("mindroom.workers.backends.local.venv.EnvBuilder.create", new=_fake_local_worker_venv_create):
        response = runner_client.post(
            "/api/sandbox-runner/execute",
            headers=SANDBOX_HEADERS,
            json={
                "tool_name": "shell",
                "function_name": "run_shell_command",
                "args": [["bash", "-c", 'printf "%s|%s" "$WORKSPACE_HOOK_TOKEN" "$PATH"']],
                "kwargs": {},
                "worker_key": "v1:tenant-123:shared:general",
                "tool_init_overrides": {"base_dir": "agents/general/workspace"},
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True, payload
    token, path_value = payload["result"].split("|", 1)
    assert token == "hello-from-hook"  # noqa: S105
    assert path_value.startswith(f"{workspace.resolve()}/.local/bin:")


@REQUIRES_LINUX_LOCAL_WORKER
def test_workspace_env_hook_edits_take_effect_on_next_call(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Editing `.mindroom/worker-env.sh` should be reflected in the next shell call."""
    _set_sandbox_token(monkeypatch)
    storage_root = tmp_path / "storage"
    workspace = storage_root / "agents" / "general" / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    _write_workspace_env_hook(workspace, "export WORKSPACE_HOOK_TOKEN=first\n")
    monkeypatch.setenv("MINDROOM_STORAGE_PATH", str(storage_root))
    _refresh_runner_app_from_env()

    with patch("mindroom.workers.backends.local.venv.EnvBuilder.create", new=_fake_local_worker_venv_create):
        first = runner_client.post(
            "/api/sandbox-runner/execute",
            headers=SANDBOX_HEADERS,
            json={
                "tool_name": "shell",
                "function_name": "run_shell_command",
                "args": [["bash", "-c", 'printf "%s" "$WORKSPACE_HOOK_TOKEN"']],
                "kwargs": {},
                "worker_key": "v1:tenant-123:shared:general",
                "tool_init_overrides": {"base_dir": "agents/general/workspace"},
            },
        )

        assert first.status_code == 200
        assert first.json()["ok"] is True
        assert first.json()["result"] == "first"

        _write_workspace_env_hook(workspace, "export WORKSPACE_HOOK_TOKEN=second\n")

        second = runner_client.post(
            "/api/sandbox-runner/execute",
            headers=SANDBOX_HEADERS,
            json={
                "tool_name": "shell",
                "function_name": "run_shell_command",
                "args": [["bash", "-c", 'printf "%s" "$WORKSPACE_HOOK_TOKEN"']],
                "kwargs": {},
                "worker_key": "v1:tenant-123:shared:general",
                "tool_init_overrides": {"base_dir": "agents/general/workspace"},
            },
        )

    assert second.status_code == 200
    assert second.json()["ok"] is True
    assert second.json()["result"] == "second"


@REQUIRES_LINUX_LOCAL_WORKER
def test_workspace_env_hook_keeps_user_credentials_and_filters_runner_control(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Explicit hook exports pass except for runner control-plane names."""
    _set_sandbox_token(monkeypatch)
    storage_root = tmp_path / "storage"
    workspace = storage_root / "agents" / "general" / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    _write_workspace_env_hook(
        workspace,
        "export OPENAI_API_KEY=from-hook\n"
        "export STRIPE_SECRET=from-hook\n"
        "export GITEA_TOKEN=from-hook\n"
        "export MINDROOM_SANDBOX_PROXY_TOKEN=leaked\n"
        "export MINDROOM_SANDBOX_FOO=leaked\n",
    )
    monkeypatch.setenv("MINDROOM_STORAGE_PATH", str(storage_root))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _refresh_runner_app_from_env()

    with patch("mindroom.workers.backends.local.venv.EnvBuilder.create", new=_fake_local_worker_venv_create):
        response = runner_client.post(
            "/api/sandbox-runner/execute",
            headers=SANDBOX_HEADERS,
            json={
                "tool_name": "shell",
                "function_name": "run_shell_command",
                "args": [
                    [
                        "bash",
                        "-c",
                        (
                            'printf "%s|%s|%s|%s|%s" '
                            '"${OPENAI_API_KEY:-}" '
                            '"${STRIPE_SECRET:-}" '
                            '"${GITEA_TOKEN:-}" '
                            '"${MINDROOM_SANDBOX_PROXY_TOKEN:-}" '
                            '"${MINDROOM_SANDBOX_FOO:-}"'
                        ),
                    ],
                ],
                "kwargs": {},
                "worker_key": "v1:tenant-123:shared:general",
                "tool_init_overrides": {"base_dir": "agents/general/workspace"},
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True, payload
    api_key, stripe_secret, token, proxy_token, sandbox_value = payload["result"].split("|", 4)
    assert api_key == "from-hook"
    assert stripe_secret == "from-hook"  # noqa: S105
    assert token == "from-hook"  # noqa: S105
    assert proxy_token == ""
    assert sandbox_value == ""


@REQUIRES_LINUX_LOCAL_WORKER
def test_workspace_env_hook_failure_returns_tool_failure(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A non-zero hook exit should surface as a tool failure mentioning the hook."""
    _set_sandbox_token(monkeypatch)
    storage_root = tmp_path / "storage"
    workspace = storage_root / "agents" / "general" / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    _write_workspace_env_hook(workspace, 'echo "bad hook" >&2\nexit 5\n')
    monkeypatch.setenv("MINDROOM_STORAGE_PATH", str(storage_root))
    _refresh_runner_app_from_env()

    with patch("mindroom.workers.backends.local.venv.EnvBuilder.create", new=_fake_local_worker_venv_create):
        response = runner_client.post(
            "/api/sandbox-runner/execute",
            headers=SANDBOX_HEADERS,
            json={
                "tool_name": "shell",
                "function_name": "run_shell_command",
                "args": [["bash", "-c", "echo should-not-run"]],
                "kwargs": {},
                "worker_key": "v1:tenant-123:shared:general",
                "tool_init_overrides": {"base_dir": "agents/general/workspace"},
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False, payload
    assert payload["failure_kind"] == "tool"
    assert ".mindroom/worker-env.sh" in payload["error"]
    assert "exited with code 5" in payload["error"]


@REQUIRES_LINUX_LOCAL_WORKER
def test_workspace_env_hook_overlays_worker_routed_python_default_mode(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Worker-keyed python gets the overlay even in the default inprocess runner mode.

    Worker-keyed non-shell tools take the `tool_name != "shell" and
    worker_key is not None` branch, which always dispatches through
    `_execute_request_subprocess`.
    """
    _set_sandbox_token(monkeypatch)
    storage_root = tmp_path / "storage"
    workspace = storage_root / "agents" / "general" / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    _write_workspace_env_hook(workspace, "export WORKSPACE_TOOLCHAIN_TOKEN=visible-to-python\n")
    monkeypatch.setenv("MINDROOM_STORAGE_PATH", str(storage_root))
    _refresh_runner_app_from_env()

    def _venv_with_real_python(_self: object, venv_dir: Path) -> None:
        bin_dir = venv_dir / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / "python").symlink_to(Path(sys.executable))

    with patch("mindroom.workers.backends.local.venv.EnvBuilder.create", new=_venv_with_real_python):
        response = runner_client.post(
            "/api/sandbox-runner/execute",
            headers=SANDBOX_HEADERS,
            json={
                "tool_name": "python",
                "function_name": "run_python_code",
                "args": [
                    'import os\nresult = os.environ.get("WORKSPACE_TOOLCHAIN_TOKEN")',
                    "result",
                ],
                "kwargs": {},
                "worker_key": "v1:tenant-123:shared:general",
                "tool_init_overrides": {"base_dir": "agents/general/workspace"},
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True, payload
    assert "visible-to-python" in str(payload["result"])


@REQUIRES_LINUX_LOCAL_WORKER
def test_workspace_env_hook_skips_worker_routed_coding_default_mode(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Worker-keyed coding should not source the workspace env hook."""
    _set_sandbox_token(monkeypatch)
    storage_root = tmp_path / "storage"
    workspace = storage_root / "agents" / "general" / "workspace"
    bin_dir = workspace / ".local" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (workspace / "note.txt").write_text("needle\n", encoding="utf-8")
    fake_rg = bin_dir / "rg"
    fake_rg.write_text('#!/usr/bin/env bash\necho "hook-rg" >&2\nexit 2\n', encoding="utf-8")
    fake_rg.chmod(0o755)
    _write_workspace_env_hook(workspace, 'export PATH="$PWD/.local/bin:$PATH"\n')
    monkeypatch.setenv("MINDROOM_STORAGE_PATH", str(storage_root))
    _refresh_runner_app_from_env()

    with patch("mindroom.workers.backends.local.venv.EnvBuilder.create", new=_fake_local_worker_venv_create):
        response = runner_client.post(
            "/api/sandbox-runner/execute",
            headers=SANDBOX_HEADERS,
            json={
                "tool_name": "coding",
                "function_name": "grep",
                "args": ["needle"],
                "kwargs": {"path": "."},
                "worker_key": "v1:tenant-123:shared:general",
                "tool_init_overrides": {"base_dir": "agents/general/workspace"},
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True, payload
    assert payload["result"] == "note.txt:1:needle"


@REQUIRES_LINUX_LOCAL_WORKER
def test_workspace_env_hook_failure_does_not_block_worker_routed_file_default_mode(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Worker-keyed file requests should not source the workspace env hook."""
    _set_sandbox_token(monkeypatch)
    storage_root = tmp_path / "storage"
    workspace = storage_root / "agents" / "general" / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    _write_workspace_env_hook(workspace, 'echo "file hook failed" >&2\nexit 6\n')
    monkeypatch.setenv("MINDROOM_STORAGE_PATH", str(storage_root))
    _refresh_runner_app_from_env()

    with patch("mindroom.workers.backends.local.venv.EnvBuilder.create", new=_fake_local_worker_venv_create):
        response = runner_client.post(
            "/api/sandbox-runner/execute",
            headers=SANDBOX_HEADERS,
            json={
                "tool_name": "file",
                "function_name": "save_file",
                "args": ["should not be written", "note.txt"],
                "kwargs": {},
                "worker_key": "v1:tenant-123:shared:general",
                "tool_init_overrides": {"base_dir": "agents/general/workspace"},
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True, payload
    assert payload["result"] == "note.txt"
    assert (workspace / "note.txt").read_text(encoding="utf-8") == "should not be written"


@REQUIRES_LINUX_LOCAL_WORKER
def test_workspace_env_hook_overlays_python_subprocess(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Worker-routed `python` subprocess should observe overlay env via os.environ."""
    _set_sandbox_token(monkeypatch)
    monkeypatch.setenv("MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE", "subprocess")
    storage_root = tmp_path / "storage"
    workspace = storage_root / "agents" / "general" / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    _write_workspace_env_hook(workspace, "export WORKSPACE_PIP_INDEX=https://wheels.example/simple\n")
    monkeypatch.setenv("MINDROOM_STORAGE_PATH", str(storage_root))
    _refresh_runner_app_from_env()

    def _venv_with_real_python(_self: object, venv_dir: Path) -> None:
        bin_dir = venv_dir / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / "python").symlink_to(Path(sys.executable))

    with patch("mindroom.workers.backends.local.venv.EnvBuilder.create", new=_venv_with_real_python):
        response = runner_client.post(
            "/api/sandbox-runner/execute",
            headers=SANDBOX_HEADERS,
            json={
                "tool_name": "python",
                "function_name": "run_python_code",
                "args": [
                    'import os\nresult = os.environ.get("WORKSPACE_PIP_INDEX")',
                    "result",
                ],
                "kwargs": {},
                "worker_key": "v1:tenant-123:shared:general",
                "tool_init_overrides": {"base_dir": "agents/general/workspace"},
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True, payload
    assert "https://wheels.example/simple" in str(payload["result"])


@REQUIRES_LINUX_LOCAL_WORKER
def test_workspace_env_hook_unkeyed_proxy_uses_init_override_base_dir(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Unkeyed proxy requests should still pick up the workspace hook from the requested base_dir."""
    _set_sandbox_token(monkeypatch)
    workspace = tmp_path / "static-workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    _write_workspace_env_hook(workspace, "export STATIC_HOOK_TOKEN=visible\n")

    response = runner_client.post(
        "/api/sandbox-runner/execute",
        headers=SANDBOX_HEADERS,
        json={
            "tool_name": "shell",
            "function_name": "run_shell_command",
            "args": [["bash", "-c", 'printf "%s" "$STATIC_HOOK_TOKEN"']],
            "kwargs": {},
            "tool_init_overrides": {"base_dir": str(workspace)},
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True, payload
    assert payload["result"] == "visible"


@REQUIRES_LINUX_LOCAL_WORKER
def test_workspace_env_hook_rejects_symlink_escape(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A `.mindroom/worker-env.sh` symlink that escapes the workspace must fail closed."""
    _set_sandbox_token(monkeypatch)
    storage_root = tmp_path / "storage"
    workspace = storage_root / "agents" / "general" / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "evil.sh"
    target.write_text("export OPENAI_API_KEY=leaked\n", encoding="utf-8")
    hook_dir = workspace / ".mindroom"
    hook_dir.mkdir()
    (hook_dir / "worker-env.sh").symlink_to(target)
    monkeypatch.setenv("MINDROOM_STORAGE_PATH", str(storage_root))
    _refresh_runner_app_from_env()

    with patch("mindroom.workers.backends.local.venv.EnvBuilder.create", new=_fake_local_worker_venv_create):
        response = runner_client.post(
            "/api/sandbox-runner/execute",
            headers=SANDBOX_HEADERS,
            json={
                "tool_name": "shell",
                "function_name": "run_shell_command",
                "args": [["bash", "-c", "echo should-not-run"]],
                "kwargs": {},
                "worker_key": "v1:tenant-123:shared:general",
                "tool_init_overrides": {"base_dir": "agents/general/workspace"},
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["failure_kind"] == "tool"
    assert "resolves outside" in payload["error"]
