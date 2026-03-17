"""Tests for generic sandbox proxy tool wrapping."""

from __future__ import annotations

import ast
import asyncio
import json
import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

import pytest

import mindroom.tool_system.sandbox_proxy as sandbox_proxy_module
import mindroom.tools  # noqa: F401
from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.credentials import get_runtime_credentials_manager, save_scoped_credentials
from mindroom.tool_system.metadata import ToolInitOverrideError, get_tool_by_name
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_key
from mindroom.workers import runtime as workers_runtime_module
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.backends.static_runner import StaticSandboxRunnerBackend
from mindroom.workers.models import WorkerSpec
from tests.conftest import FakeCredentialsManager

if TYPE_CHECKING:
    from collections.abc import Callable

_TEST_AUTH_TOKEN = "test-token"  # noqa: S105
_TEST_RUNTIME_PATHS = resolve_runtime_paths(config_path=Path("config.yaml"), process_env={})


def _runtime_paths_from_env() -> RuntimePaths:
    return resolve_runtime_paths(config_path=Path("config.yaml"), process_env=dict(os.environ))


def _configure_proxy_runtime(
    monkeypatch: pytest.MonkeyPatch,
    *,
    proxy_url: str | None,
    proxy_token: str | None = _TEST_AUTH_TOKEN,
    execution_mode: str | None = "all",
    runner_mode: bool = False,
    proxy_tools: set[str] | None = None,
    credential_policy: dict[str, tuple[str, ...]] | None = None,
) -> RuntimePaths:
    if proxy_url is None:
        monkeypatch.delenv("MINDROOM_SANDBOX_PROXY_URL", raising=False)
    else:
        monkeypatch.setenv("MINDROOM_SANDBOX_PROXY_URL", proxy_url)
    if proxy_token is None:
        monkeypatch.delenv("MINDROOM_SANDBOX_PROXY_TOKEN", raising=False)
    else:
        monkeypatch.setenv("MINDROOM_SANDBOX_PROXY_TOKEN", proxy_token)
    if execution_mode is None:
        monkeypatch.delenv("MINDROOM_SANDBOX_EXECUTION_MODE", raising=False)
    else:
        monkeypatch.setenv("MINDROOM_SANDBOX_EXECUTION_MODE", execution_mode)
    monkeypatch.setenv("MINDROOM_SANDBOX_RUNNER_MODE", "true" if runner_mode else "false")
    if proxy_tools is None:
        monkeypatch.delenv("MINDROOM_SANDBOX_PROXY_TOOLS", raising=False)
    else:
        monkeypatch.setenv("MINDROOM_SANDBOX_PROXY_TOOLS", ",".join(sorted(proxy_tools)))
    if credential_policy is None:
        monkeypatch.delenv("MINDROOM_SANDBOX_CREDENTIAL_POLICY_JSON", raising=False)
    else:
        monkeypatch.setenv(
            "MINDROOM_SANDBOX_CREDENTIAL_POLICY_JSON",
            json.dumps({key: list(value) for key, value in credential_policy.items()}),
        )
    return _runtime_paths_from_env()


class _FakeResponse:
    def __init__(self, payload: dict[str, object] | None = None) -> None:
        self._payload = payload or {"ok": True, "result": "sandbox-result"}

    def raise_for_status(self) -> None:
        return

    def json(self) -> dict[str, object]:
        return self._payload


def _recording_client_class(
    *,
    captured: dict[str, Any] | None = None,
    captured_calls: list[tuple[str, dict[str, Any]]] | None = None,
    responder: Callable[[str, dict[str, Any]], dict[str, object]] | None = None,
) -> type:
    class _FakeClient:
        def __init__(self, *, timeout: float) -> None:
            if captured is not None:
                captured["timeout"] = timeout

        def __enter__(self) -> Self:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return

        def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> _FakeResponse:
            if captured is not None:
                captured["url"] = url
                captured["json"] = json
                captured["headers"] = headers
            if captured_calls is not None:
                captured_calls.append((url, json))
            payload = responder(url, json) if responder is not None else {"ok": True, "result": "sandbox-result"}
            return _FakeResponse(payload)

    return _FakeClient


def test_proxy_wraps_tool_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tool entrypoints should call the sandbox runner API when proxy mode is enabled."""
    captured: dict[str, Any] = {}

    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url="http://sandbox-runner:8765",
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="selective",
        proxy_tools={"calculator"},
        credential_policy={},
    )
    monkeypatch.setattr(
        "mindroom.tool_system.sandbox_proxy.httpx.Client",
        _recording_client_class(captured=captured),
    )

    tool = get_tool_by_name("calculator", runtime_paths, execution_identity=None)
    entrypoint = tool.functions["add"].entrypoint
    assert entrypoint is not None
    result = entrypoint(1, 2)

    assert result == "sandbox-result"
    assert captured["url"] == "http://sandbox-runner:8765/api/sandbox-runner/execute"
    assert captured["json"] == {
        "tool_name": "calculator",
        "function_name": "add",
        "args": [1, 2],
        "kwargs": {},
    }
    assert captured["headers"] == {"x-mindroom-sandbox-token": "test-token"}


def test_proxy_disabled_in_runner_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Runner mode must execute tools locally to avoid proxy recursion."""

    class _ForbiddenClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            msg = "Proxy client should not be used in runner mode."
            raise AssertionError(msg)

    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url="http://sandbox-runner:8765",
        execution_mode="all",
        runner_mode=True,
    )
    monkeypatch.setattr("mindroom.tool_system.sandbox_proxy.httpx.Client", _ForbiddenClient)

    tool = get_tool_by_name("calculator", runtime_paths, execution_identity=None)
    entrypoint = tool.functions["add"].entrypoint
    assert entrypoint is not None
    result = entrypoint(1, 2)
    assert '"result": 3' in result


def test_proxy_requests_credential_lease_when_policy_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Proxy should create and consume a lease when credential sharing policy allows it."""
    captured_calls: list[tuple[str, dict[str, Any]]] = []

    fake_credentials = FakeCredentialsManager({"openai": {"api_key": "sk-test", "_source": "ui"}})

    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url="http://sandbox-runner:8765",
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="all",
        credential_policy={"calculator.add": ("openai",)},
    )
    monkeypatch.setattr(
        "mindroom.tool_system.sandbox_proxy.httpx.Client",
        _recording_client_class(
            captured_calls=captured_calls,
            responder=lambda url, _json: (
                {"lease_id": "lease-123", "expires_at": 123.0, "max_uses": 1}
                if url.endswith("/leases")
                else {"ok": True, "result": "proxied"}
            ),
        ),
    )

    tool = get_tool_by_name("calculator", runtime_paths, credentials_manager=fake_credentials, execution_identity=None)
    entrypoint = tool.functions["add"].entrypoint
    assert entrypoint is not None
    result = entrypoint(1, 2)

    assert result == "proxied"
    assert len(captured_calls) == 2

    lease_url, lease_payload = captured_calls[0]
    assert lease_url.endswith("/api/sandbox-runner/leases")
    assert lease_payload["credential_overrides"] == {"api_key": "sk-test"}

    execute_url, execute_payload = captured_calls[1]
    assert execute_url.endswith("/api/sandbox-runner/execute")
    assert execute_payload["lease_id"] == "lease-123"


def test_get_tool_by_name_rejects_unsafe_tool_init_overrides() -> None:
    """Tool init overrides should allow only the explicit safe whitelist."""
    with pytest.raises(ToolInitOverrideError, match="api_key"):
        get_tool_by_name(
            "openai",
            _TEST_RUNTIME_PATHS,
            tool_init_overrides={"api_key": "sk-test"},
            execution_identity=None,
        )


def test_get_tool_by_name_rejects_invalid_base_dir_override_type() -> None:
    """base_dir overrides should be validated before toolkit construction."""
    with pytest.raises(ToolInitOverrideError, match="base_dir"):
        get_tool_by_name(
            "coding",
            _TEST_RUNTIME_PATHS,
            tool_init_overrides={"base_dir": {"bad": "value"}},
            execution_identity=None,
        )


def test_get_tool_by_name_loads_persisted_non_secret_file_config(tmp_path: Path) -> None:
    """Persisted plain config should still hydrate SetupType.NONE tools during rebuilds."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("models: {}\nagents: {}\n", encoding="utf-8")
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={},
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    save_scoped_credentials(
        "file",
        {
            "base_dir": str(workspace),
            "enable_delete_file": True,
        },
        credentials_manager=credentials_manager,
        execution_identity=None,
    )

    tool = get_tool_by_name("file", runtime_paths, execution_identity=None)

    assert tool.base_dir == workspace.resolve()
    assert "delete_file" in tool.functions


def test_get_tool_by_name_builds_google_bigquery_from_scoped_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Google BigQuery should be configured from persisted tool credentials."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("models: {}\nagents: {}\n", encoding="utf-8")
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={},
    )
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    save_scoped_credentials(
        "google_bigquery",
        {
            "dataset": "demo_dataset",
            "project": "demo-project",
            "location": "us-central1",
        },
        credentials_manager=credentials_manager,
        execution_identity=None,
    )
    captured: dict[str, object] = {}

    class _FakeBigQueryClient:
        def __init__(self, *, project: str, credentials: object | None = None) -> None:
            captured["project"] = project
            captured["credentials"] = credentials

    monkeypatch.setattr("agno.tools.google_bigquery.bigquery.Client", _FakeBigQueryClient)

    tool = get_tool_by_name("google_bigquery", runtime_paths, execution_identity=None)

    assert tool.dataset == "demo_dataset"
    assert tool.project == "demo-project"
    assert tool.location == "us-central1"
    assert captured["project"] == "demo-project"
    assert captured["credentials"] is None


def test_get_tool_by_name_requires_explicit_clickup_config(tmp_path: Path) -> None:
    """Runtime-scoped env values should not configure ClickUp during toolkit construction."""
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.yaml"
    config_path.write_text("models: {}\nagents: {}\n", encoding="utf-8")
    (config_dir / ".env").write_text(
        "CLICKUP_API_KEY=clickup-test\nMASTER_SPACE_ID=space-123\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={},
    )

    with pytest.raises(ValueError, match="CLICKUP_API_KEY not set"):
        get_tool_by_name("clickup", runtime_paths, execution_identity=None)


def test_get_tool_by_name_does_not_expose_runtime_env_to_direct_python_execution(tmp_path: Path) -> None:
    """Direct in-process Python execution should not emulate committed runtime env."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={
            "OPENAI_BASE_URL": "http://example.invalid/v1",
            "MINDROOM_NAMESPACE": "alpha1234",
        },
    )

    tool = get_tool_by_name("python", runtime_paths, execution_identity=None)
    entrypoint = tool.functions["run_python_code"].entrypoint
    assert entrypoint is not None

    result = entrypoint(
        'import os\nresult = {"openai_base_url": os.environ.get("OPENAI_BASE_URL"), "namespace": os.environ.get("MINDROOM_NAMESPACE"), "storage": os.environ.get("MINDROOM_STORAGE_PATH")}',
        "result",
    )

    assert ast.literal_eval(result) == {
        "openai_base_url": None,
        "namespace": None,
        "storage": None,
    }


def test_get_tool_by_name_does_not_expose_runtime_env_to_file_backed_python_execution(tmp_path: Path) -> None:
    """Direct file-backed Python execution should also avoid runtime env emulation."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={
            "OPENAI_BASE_URL": "http://example.invalid/v1",
            "MINDROOM_NAMESPACE": "alpha1234",
        },
    )

    tool = get_tool_by_name(
        "python",
        runtime_paths,
        tool_init_overrides={"base_dir": str(tmp_path)},
        execution_identity=None,
    )
    save_entrypoint = tool.functions["save_to_file_and_run"].entrypoint
    run_file_entrypoint = tool.functions["run_python_file_return_variable"].entrypoint
    assert save_entrypoint is not None
    assert run_file_entrypoint is not None

    code = (
        "import os\n"
        'result = {"openai_base_url": os.environ.get("OPENAI_BASE_URL"), '
        '"namespace": os.environ.get("MINDROOM_NAMESPACE"), '
        '"storage": os.environ.get("MINDROOM_STORAGE_PATH")}'
    )
    save_result = save_entrypoint("runtime_values.py", code, "result")
    run_result = run_file_entrypoint("runtime_values.py", "result")
    expected = {
        "openai_base_url": None,
        "namespace": None,
        "storage": None,
    }

    assert ast.literal_eval(save_result) == expected
    assert ast.literal_eval(run_result) == expected


def test_get_tool_by_name_exposes_runtime_env_to_shell_execution(tmp_path: Path) -> None:
    """Direct shell execution should inherit committed runtime env values from the runtime `.env`."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("TEST_EXECUTION_ENV=visible-in-shell\n", encoding="utf-8")
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={},
    )

    tool = get_tool_by_name("shell", runtime_paths, disable_sandbox_proxy=True, execution_identity=None)
    entrypoint = tool.functions["run_shell_command"].entrypoint
    assert entrypoint is not None

    result = entrypoint(["bash", "-lc", "printf '%s' \"$TEST_EXECUTION_ENV\""])

    assert result == "visible-in-shell"


def test_local_shell_does_not_inherit_filtered_process_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Direct shell execution should not leak filtered process env outside the committed runtime."""
    monkeypatch.setenv("MINDROOM_SANDBOX_PROXY_TOKEN", "runner-secret")
    monkeypatch.setenv("CI_JOB_TOKEN", "ci-secret")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("TEST_EXECUTION_ENV=visible-in-shell\n", encoding="utf-8")
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={},
    )

    tool = get_tool_by_name("shell", runtime_paths, disable_sandbox_proxy=True, execution_identity=None)
    entrypoint = tool.functions["run_shell_command"].entrypoint
    assert entrypoint is not None

    result = entrypoint(
        [
            "bash",
            "-lc",
            "printf '%s' \"$MINDROOM_SANDBOX_PROXY_TOKEN|$CI_JOB_TOKEN|$TEST_EXECUTION_ENV\"",
        ],
    )

    assert result == "||visible-in-shell"


def test_proxy_forwards_execution_env_only_for_execution_tools(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Sandbox proxy should forward execution env only for shell/python-style execution tools."""
    captured: dict[str, Any] = {}
    _configure_proxy_runtime(
        monkeypatch,
        proxy_url="http://sandbox-runner:8765",
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="all",
        credential_policy={},
    )
    monkeypatch.setattr(
        "mindroom.tool_system.sandbox_proxy.httpx.Client",
        _recording_client_class(captured=captured),
    )
    monkeypatch.setenv("CI_JOB_TOKEN", "ci-secret")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    config_path.with_name(".env").write_text("TEST_EXECUTION_ENV=visible-in-shell\n", encoding="utf-8")
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=config_path.parent / "storage",
        process_env=dict(os.environ),
    )

    shell_tool = get_tool_by_name("shell", runtime_paths, execution_identity=None)
    shell_entrypoint = shell_tool.functions["run_shell_command"].entrypoint
    assert shell_entrypoint is not None
    result = shell_entrypoint(["bash", "-lc", "printf '%s' \"$TEST_EXECUTION_ENV\""])

    assert result == "sandbox-result"
    assert captured["json"]["execution_env"]["TEST_EXECUTION_ENV"] == "visible-in-shell"
    assert "CI_JOB_TOKEN" not in captured["json"]["execution_env"]
    assert "MINDROOM_SANDBOX_PROXY_TOKEN" not in captured["json"]["execution_env"]

    captured.clear()
    calculator = get_tool_by_name("calculator", runtime_paths, execution_identity=None)
    calculator_entrypoint = calculator.functions["add"].entrypoint
    assert calculator_entrypoint is not None
    calculator_entrypoint(1, 2)

    assert "execution_env" not in captured["json"]


def test_get_worker_manager_falls_back_to_runtime_storage_root_without_tool_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Worker routing should not require ToolRuntimeContext just to recover storage_root."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )
    captured: dict[str, object] = {}

    def _fake_get_primary_worker_manager(
        runtime_paths_arg: RuntimePaths,
        *,
        proxy_url: str | None,
        proxy_token: str | None,
        storage_root: Path | None = None,
    ) -> str:
        captured["runtime_paths"] = runtime_paths_arg
        captured["proxy_url"] = proxy_url
        captured["proxy_token"] = proxy_token
        captured["storage_root"] = storage_root
        return "manager"

    monkeypatch.setattr(sandbox_proxy_module, "get_primary_worker_manager", _fake_get_primary_worker_manager)
    monkeypatch.setattr(sandbox_proxy_module, "get_tool_runtime_context", lambda: None)

    manager = sandbox_proxy_module._get_worker_manager(
        runtime_paths,
        sandbox_proxy_module.SandboxProxyConfig(
            runner_mode=False,
            proxy_url="http://sandbox",
            proxy_token="token",  # noqa: S106
            proxy_timeout_seconds=30.0,
            execution_mode="all",
            credential_lease_ttl_seconds=60,
            proxy_tools=None,
            credential_policy={},
        ),
    )

    assert manager == "manager"
    assert captured["runtime_paths"] == runtime_paths
    assert captured["storage_root"] == (tmp_path / "storage").resolve()


def test_proxy_requires_shared_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Proxy mode should fail closed when no shared token is configured."""

    class _FakeClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return

        def __enter__(self) -> Self:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return

        def post(self, *_args: object, **_kwargs: object) -> None:
            msg = "Proxy client should not make requests without a shared token."
            raise AssertionError(msg)

    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url="http://sandbox-runner:8765",
        proxy_token=None,
        execution_mode="all",
    )
    monkeypatch.setattr("mindroom.tool_system.sandbox_proxy.httpx.Client", _FakeClient)

    tool = get_tool_by_name("calculator", runtime_paths, execution_identity=None)
    entrypoint = tool.functions["add"].entrypoint
    assert entrypoint is not None
    with pytest.raises(RuntimeError, match="MINDROOM_SANDBOX_PROXY_TOKEN"):
        entrypoint(1, 2)


def test_proxy_prefers_worker_scoped_credentials_for_worker_routed_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Worker-routed credential leases should prefer credentials stored in the resolved worker scope."""
    captured_calls: list[tuple[str, dict[str, Any]]] = []

    class _FakeResponse:
        def __init__(self, data: dict[str, object]) -> None:
            self._data = data

        def raise_for_status(self) -> None:
            return

        def json(self) -> dict[str, object]:
            return self._data

    class _FakeClient:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        def __enter__(self) -> Self:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return

        def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> _FakeResponse:
            _ = headers
            captured_calls.append((url, json))
            if url.endswith("/leases"):
                return _FakeResponse({"lease_id": "lease-123", "expires_at": 123.0, "max_uses": 1})
            return _FakeResponse({"ok": True, "result": "proxied"})

    execution_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-1",
    )
    worker_key = resolve_worker_key("user", execution_identity, agent_name="code")
    assert worker_key is not None
    fake_credentials = FakeCredentialsManager(
        {"openai": {"api_key": "shared-key", "_source": "ui"}},
        worker_managers={
            worker_key: FakeCredentialsManager({"openai": {"api_key": "worker-key", "_source": "ui"}}),
        },
    )

    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url="http://sandbox-runner:8765",
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="off",
        credential_policy={"calculator.add": ("openai",)},
    )
    monkeypatch.setattr("mindroom.tool_system.sandbox_proxy.httpx.Client", _FakeClient)

    tool = get_tool_by_name(
        "calculator",
        runtime_paths,
        credentials_manager=fake_credentials,
        execution_identity=execution_identity,
        worker_tools_override=["calculator"],
        worker_scope="user",
        routing_agent_name="code",
    )
    entrypoint = tool.functions["add"].entrypoint
    assert entrypoint is not None
    result = entrypoint(1, 2)

    assert result == "proxied"
    lease_url, lease_payload = captured_calls[0]
    assert lease_url.endswith("/api/sandbox-runner/leases")
    assert lease_payload["credential_overrides"] == {"api_key": "worker-key"}


def test_proxy_includes_worker_routing_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Worker-routed tool calls should include scope, key, and execution identity."""
    captured: dict[str, Any] = {}

    class _FakeResponse:
        def raise_for_status(self) -> None:
            return

        def json(self) -> dict[str, object]:
            return {"ok": True, "result": "sandbox-result"}

    class _FakeClient:
        def __init__(self, *, timeout: float) -> None:
            captured["timeout"] = timeout

        def __enter__(self) -> Self:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return

        def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> _FakeResponse:
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return _FakeResponse()

    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url="http://sandbox-runner:8765",
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="off",
        credential_policy={},
    )
    monkeypatch.setattr("mindroom.tool_system.sandbox_proxy.httpx.Client", _FakeClient)

    execution_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="shared_agent",
        requester_id="alice",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-1",
    )

    tool = get_tool_by_name(
        "calculator",
        runtime_paths,
        execution_identity=execution_identity,
        worker_tools_override=["calculator"],
        worker_scope="user_agent",
        routing_agent_name="code",
    )
    entrypoint = tool.functions["add"].entrypoint
    assert entrypoint is not None
    expected_worker_key = resolve_worker_key("user_agent", execution_identity, agent_name="code")
    assert expected_worker_key is not None

    runtime_context = ToolRuntimeContext(
        agent_name="code",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        requester_id="alice",
        client=object(),
        config=Config(
            agents={
                "code": AgentConfig(
                    display_name="Code",
                    private=AgentPrivateConfig(per="user_agent"),
                ),
            },
            models={},
        ),
        runtime_paths=runtime_paths,
    )

    with tool_runtime_context(runtime_context):
        result = entrypoint(1, 2)

    assert result == "sandbox-result"
    assert captured["headers"] == {"x-mindroom-sandbox-token": "test-token"}
    assert captured["json"]["worker_scope"] == "user_agent"
    assert captured["json"]["worker_key"] == expected_worker_key
    assert captured["json"]["execution_identity"] == {
        "channel": "matrix",
        "agent_name": "shared_agent",
        "requester_id": "alice",
        "room_id": "!room:example.org",
        "thread_id": "$thread",
        "resolved_thread_id": "$thread",
        "session_id": "session-1",
        "tenant_id": None,
        "account_id": None,
    }
    assert captured["json"]["private_agent_names"] == ["code"]


def test_proxy_user_agent_shared_agent_sends_explicit_empty_private_visibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shared user-agent workers should still send explicit empty private visibility."""
    captured: dict[str, Any] = {}

    class _FakeResponse:
        def raise_for_status(self) -> None:
            return

        def json(self) -> dict[str, object]:
            return {"ok": True, "result": "sandbox-result"}

    class _FakeClient:
        def __init__(self, *, timeout: float) -> None:
            captured["timeout"] = timeout

        def __enter__(self) -> Self:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return

        def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> _FakeResponse:
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return _FakeResponse()

    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url="http://sandbox-runner:8765",
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="off",
        credential_policy={},
    )
    monkeypatch.setattr("mindroom.tool_system.sandbox_proxy.httpx.Client", _FakeClient)

    execution_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="shared_agent",
        requester_id="alice",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-1",
    )

    tool = get_tool_by_name(
        "calculator",
        runtime_paths,
        execution_identity=execution_identity,
        worker_tools_override=["calculator"],
        worker_scope="user_agent",
        routing_agent_name="code",
    )
    entrypoint = tool.functions["add"].entrypoint
    assert entrypoint is not None
    expected_worker_key = resolve_worker_key("user_agent", execution_identity, agent_name="code")
    assert expected_worker_key is not None

    runtime_context = ToolRuntimeContext(
        agent_name="code",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        requester_id="alice",
        client=object(),
        config=Config(
            agents={
                "code": AgentConfig(
                    display_name="Code",
                    worker_scope="user_agent",
                ),
            },
            models={},
        ),
        runtime_paths=runtime_paths,
    )

    with tool_runtime_context(runtime_context):
        result = entrypoint(1, 2)

    assert result == "sandbox-result"
    assert captured["json"]["worker_key"] == expected_worker_key
    assert captured["json"]["private_agent_names"] == []


def test_static_sandbox_runner_backend_reuses_worker_handle_identity() -> None:
    """The current shared sandbox-runner provider should return stable handle identity per worker key."""
    backend = StaticSandboxRunnerBackend(
        api_root="http://sandbox-runner:8765",
        auth_token=_TEST_AUTH_TOKEN,
        idle_timeout_seconds=60.0,
    )

    first = backend.ensure_worker(WorkerSpec("worker-a"), now=10.0)
    second = backend.ensure_worker(WorkerSpec("worker-a"), now=20.0)

    assert first.worker_id == second.worker_id
    assert second.worker_key == "worker-a"
    assert second.endpoint == "http://sandbox-runner:8765/api/sandbox-runner/execute"
    assert second.auth_token == _TEST_AUTH_TOKEN
    assert second.backend_name == "static_sandbox_runner"
    assert second.startup_count == 1
    assert second.last_used_at == 20.0


def test_static_sandbox_runner_backend_marks_idle_workers() -> None:
    """Idle cleanup on the static provider should preserve worker identity while changing lifecycle state."""
    backend = StaticSandboxRunnerBackend(
        api_root="http://sandbox-runner:8765",
        auth_token=_TEST_AUTH_TOKEN,
        idle_timeout_seconds=5.0,
    )
    backend.ensure_worker(WorkerSpec("worker-a"), now=0.0)

    cleaned_workers = backend.cleanup_idle_workers(now=10.0)

    assert len(cleaned_workers) == 1
    assert cleaned_workers[0].worker_key == "worker-a"
    assert cleaned_workers[0].status == "idle"


def test_get_worker_manager_singleton_creation_is_thread_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Concurrent proxy requests should not build multiple static worker managers for one config."""
    workers_runtime_module._reset_primary_worker_manager()
    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url="http://sandbox-runner:8765",
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode=None,
    )
    proxy_config = sandbox_proxy_module.sandbox_proxy_config(runtime_paths)
    monkeypatch.delenv("MINDROOM_WORKER_BACKEND", raising=False)

    first_init_started = threading.Event()
    allow_first_init_to_finish = threading.Event()
    init_count_lock = threading.Lock()
    init_count = 0
    managers: list[object] = []
    exceptions: list[Exception] = []

    class FakeBackend:
        backend_name = "fake_static_backend"
        idle_timeout_seconds = 60.0

        def __init__(self, *, api_root: str, auth_token: str | None) -> None:
            del api_root, auth_token
            nonlocal init_count
            with init_count_lock:
                init_count += 1
                call_number = init_count
            if call_number == 1:
                first_init_started.set()
                assert allow_first_init_to_finish.wait(timeout=1.0)

    def load_manager() -> None:
        try:
            managers.append(sandbox_proxy_module._get_worker_manager(runtime_paths, proxy_config))
        except Exception as exc:  # pragma: no cover - surfaced by assertion below
            exceptions.append(exc)

    monkeypatch.setattr(workers_runtime_module, "StaticSandboxRunnerBackend", FakeBackend)

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
    workers_runtime_module._reset_primary_worker_manager()


def test_worker_tools_override_can_use_kubernetes_backend_without_proxy_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Worker-routed tools should stay proxy-enabled when the Kubernetes backend provides worker handles directly."""
    monkeypatch.setenv("MINDROOM_WORKER_BACKEND", "kubernetes")
    monkeypatch.setenv("MINDROOM_KUBERNETES_WORKER_IMAGE", "ghcr.io/mindroom-ai/mindroom:latest")
    monkeypatch.setenv("MINDROOM_KUBERNETES_WORKER_STORAGE_PVC_NAME", "mindroom-storage")
    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url=None,
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="off",
    )

    assert (
        sandbox_proxy_module._sandbox_proxy_enabled_for_tool(
            "shell",
            runtime_paths=runtime_paths,
            worker_tools_override=["shell"],
            worker_scope="shared",
        )
        is True
    )
    assert (
        sandbox_proxy_module._sandbox_proxy_enabled_for_tool(
            "shell",
            runtime_paths=runtime_paths,
            worker_tools_override=None,
        )
        is False
    )


def test_kubernetes_backend_keeps_unscoped_env_routing_enabled_without_proxy_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unscoped agents should still route through dedicated workers on the Kubernetes backend."""
    monkeypatch.setenv("MINDROOM_WORKER_BACKEND", "kubernetes")
    monkeypatch.setenv("MINDROOM_KUBERNETES_WORKER_IMAGE", "ghcr.io/mindroom-ai/mindroom:latest")
    monkeypatch.setenv("MINDROOM_KUBERNETES_WORKER_STORAGE_PVC_NAME", "mindroom-storage")
    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url=None,
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="selective",
        proxy_tools={"shell"},
    )

    assert (
        sandbox_proxy_module._sandbox_proxy_enabled_for_tool("shell", runtime_paths=runtime_paths, worker_scope=None)
        is True
    )
    assert (
        sandbox_proxy_module._sandbox_proxy_enabled_for_tool(
            "calculator",
            runtime_paths=runtime_paths,
            worker_scope=None,
        )
        is False
    )


def test_kubernetes_backend_uses_env_routing_for_worker_scoped_agents_without_proxy_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worker-scoped agents should still honor env-based routing on the Kubernetes backend."""
    monkeypatch.setenv("MINDROOM_WORKER_BACKEND", "kubernetes")
    monkeypatch.setenv("MINDROOM_KUBERNETES_WORKER_IMAGE", "ghcr.io/mindroom-ai/mindroom:latest")
    monkeypatch.setenv("MINDROOM_KUBERNETES_WORKER_STORAGE_PVC_NAME", "mindroom-storage")
    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url=None,
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="selective",
        proxy_tools={"shell"},
    )

    assert (
        sandbox_proxy_module._sandbox_proxy_enabled_for_tool("shell", runtime_paths=runtime_paths, worker_scope="user")
        is True
    )
    assert (
        sandbox_proxy_module._sandbox_proxy_enabled_for_tool(
            "calculator",
            runtime_paths=runtime_paths,
            worker_scope="user",
        )
        is False
    )


def test_kubernetes_backend_keeps_wrapping_when_required_config_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kubernetes routing should stay enabled so misconfiguration fails closed at call time."""
    monkeypatch.setenv("MINDROOM_WORKER_BACKEND", "kubernetes")
    monkeypatch.delenv("MINDROOM_KUBERNETES_WORKER_IMAGE", raising=False)
    monkeypatch.delenv("MINDROOM_KUBERNETES_WORKER_STORAGE_PVC_NAME", raising=False)
    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url=None,
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode=None,
    )

    assert (
        sandbox_proxy_module._sandbox_proxy_enabled_for_tool(
            "shell",
            runtime_paths=runtime_paths,
            worker_tools_override=["shell"],
        )
        is True
    )


def test_kubernetes_backend_keeps_wrapping_when_proxy_token_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kubernetes routing should stay enabled so missing auth fails closed at call time."""
    monkeypatch.setenv("MINDROOM_WORKER_BACKEND", "kubernetes")
    monkeypatch.setenv("MINDROOM_KUBERNETES_WORKER_IMAGE", "ghcr.io/mindroom-ai/mindroom:latest")
    monkeypatch.setenv("MINDROOM_KUBERNETES_WORKER_STORAGE_PVC_NAME", "mindroom-storage")
    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url=None,
        proxy_token=None,
        execution_mode=None,
    )

    assert (
        sandbox_proxy_module._sandbox_proxy_enabled_for_tool(
            "shell",
            runtime_paths=runtime_paths,
            worker_tools_override=["shell"],
        )
        is True
    )


def test_kubernetes_backend_misconfiguration_raises_instead_of_running_locally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Misconfigured Kubernetes worker routing should raise rather than executing in the primary runtime."""
    monkeypatch.setenv("MINDROOM_WORKER_BACKEND", "kubernetes")
    monkeypatch.delenv("MINDROOM_KUBERNETES_WORKER_IMAGE", raising=False)
    monkeypatch.delenv("MINDROOM_KUBERNETES_WORKER_STORAGE_PVC_NAME", raising=False)
    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url=None,
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="off",
    )

    tool = get_tool_by_name(
        "shell",
        runtime_paths,
        execution_identity=None,
        worker_tools_override=["shell"],
        worker_scope=None,
        routing_agent_name="code",
    )
    entrypoint = tool.functions["run_shell_command"].entrypoint
    assert entrypoint is not None

    with pytest.raises(WorkerBackendError, match="MINDROOM_KUBERNETES_WORKER_IMAGE"):
        entrypoint("pwd")


class TestWorkerToolsOverride:
    """Tests for per-agent worker_tools_override parameter."""

    def test_override_none_defers_to_env_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """None override should defer to the standard sandbox-proxy env controls."""
        runtime_paths = _configure_proxy_runtime(
            monkeypatch,
            proxy_url="http://sandbox:8765",
            execution_mode="selective",
            proxy_tools={"shell"},
        )

        # None override -> falls through to sandbox-proxy env controls
        assert (
            sandbox_proxy_module._sandbox_proxy_enabled_for_tool(
                "shell",
                runtime_paths=runtime_paths,
                worker_tools_override=None,
            )
            is True
        )
        assert (
            sandbox_proxy_module._sandbox_proxy_enabled_for_tool(
                "calculator",
                runtime_paths=runtime_paths,
                worker_tools_override=None,
            )
            is False
        )

    def test_override_empty_list_disables_sandboxing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty list override should disable sandboxing even when sandbox env controls enable it."""
        runtime_paths = _configure_proxy_runtime(
            monkeypatch,
            proxy_url="http://sandbox:8765",
            execution_mode="all",
        )

        assert (
            sandbox_proxy_module._sandbox_proxy_enabled_for_tool(
                "shell",
                runtime_paths=runtime_paths,
                worker_tools_override=[],
            )
            is False
        )
        assert (
            sandbox_proxy_module._sandbox_proxy_enabled_for_tool(
                "file",
                runtime_paths=runtime_paths,
                worker_tools_override=[],
            )
            is False
        )

    def test_override_explicit_list_selects_tools(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Explicit list override should sandbox only the listed tools."""
        runtime_paths = _configure_proxy_runtime(
            monkeypatch,
            proxy_url="http://sandbox:8765",
            execution_mode="off",
        )

        assert (
            sandbox_proxy_module._sandbox_proxy_enabled_for_tool(
                "shell",
                runtime_paths=runtime_paths,
                worker_tools_override=["shell", "file"],
            )
            is True
        )
        assert (
            sandbox_proxy_module._sandbox_proxy_enabled_for_tool(
                "file",
                runtime_paths=runtime_paths,
                worker_tools_override=["shell", "file"],
            )
            is True
        )
        assert (
            sandbox_proxy_module._sandbox_proxy_enabled_for_tool(
                "calculator",
                runtime_paths=runtime_paths,
                worker_tools_override=["shell", "file"],
            )
            is False
        )

    def test_override_still_respects_runner_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Runner mode should always disable proxying, even with override."""
        runtime_paths = _configure_proxy_runtime(
            monkeypatch,
            proxy_url="http://sandbox:8765",
            execution_mode=None,
            runner_mode=True,
        )

        assert (
            sandbox_proxy_module._sandbox_proxy_enabled_for_tool(
                "shell",
                runtime_paths=runtime_paths,
                worker_tools_override=["shell"],
            )
            is False
        )

    def test_override_still_requires_proxy_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No proxy URL should always disable proxying, even with override."""
        runtime_paths = _configure_proxy_runtime(
            monkeypatch,
            proxy_url=None,
            execution_mode=None,
        )

        assert (
            sandbox_proxy_module._sandbox_proxy_enabled_for_tool(
                "shell",
                runtime_paths=runtime_paths,
                worker_tools_override=["shell"],
            )
            is False
        )

    @pytest.mark.parametrize(
        "tool_name",
        ["gmail", "google_calendar", "google_sheets", "homeassistant"],
    )
    def test_local_only_tools_never_proxy(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tool_name: str,
    ) -> None:
        """Credential-backed custom tools should stay in the primary runtime."""
        runtime_paths = _configure_proxy_runtime(
            monkeypatch,
            proxy_url="http://sandbox:8765",
            execution_mode="all",
        )

        assert (
            sandbox_proxy_module._sandbox_proxy_enabled_for_tool(
                tool_name,
                runtime_paths=runtime_paths,
                worker_tools_override=None,
            )
            is False
        )
        assert (
            sandbox_proxy_module._sandbox_proxy_enabled_for_tool(
                tool_name,
                runtime_paths=runtime_paths,
                worker_tools_override=[tool_name],
            )
            is False
        )

    def test_get_tool_by_name_keeps_homeassistant_local_even_when_listed(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Home Assistant should execute locally even if it appears in worker_tools."""

        class _ForbiddenClient:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                msg = "Sandbox proxy should not be used for local-only tools."
                raise AssertionError(msg)

        runtime_paths = _configure_proxy_runtime(
            monkeypatch,
            proxy_url="http://sandbox:8765",
            proxy_token=_TEST_AUTH_TOKEN,
            execution_mode="all",
            credential_policy={},
        )
        monkeypatch.setattr("mindroom.tool_system.sandbox_proxy.httpx.Client", _ForbiddenClient)

        fake_credentials = FakeCredentialsManager({})

        tool = get_tool_by_name(
            "homeassistant",
            runtime_paths,
            execution_identity=None,
            credentials_manager=fake_credentials,
            worker_tools_override=["homeassistant"],
            worker_scope="shared",
            routing_agent_name="general",
        )
        entrypoint = tool.async_functions["list_entities"].entrypoint
        assert entrypoint is not None

        result = asyncio.run(entrypoint())
        assert "Home Assistant is not configured" in result

    def test_get_tool_by_name_passes_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """get_tool_by_name should pass worker_tools_override through to the proxy wrapper."""
        captured: dict[str, Any] = {}

        runtime_paths = _configure_proxy_runtime(
            monkeypatch,
            proxy_url="http://sandbox:8765",
            proxy_token=_TEST_AUTH_TOKEN,
            execution_mode="off",
            credential_policy={},
        )
        monkeypatch.setattr(
            "mindroom.tool_system.sandbox_proxy.httpx.Client",
            _recording_client_class(captured=captured),
        )

        # Override says sandbox calculator
        tool = get_tool_by_name(
            "calculator",
            runtime_paths,
            worker_tools_override=["calculator"],
            execution_identity=None,
        )
        entrypoint = tool.functions["add"].entrypoint
        assert entrypoint is not None
        result = entrypoint(1, 2)
        assert result == "sandbox-result"
        assert captured["url"] == "http://sandbox:8765/api/sandbox-runner/execute"

    def test_get_tool_by_name_passes_tool_init_overrides_to_proxy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Proxy execution should preserve non-secret tool init overrides like base_dir."""
        captured: dict[str, Any] = {}

        runtime_paths = _configure_proxy_runtime(
            monkeypatch,
            proxy_url="http://sandbox:8765",
            proxy_token=_TEST_AUTH_TOKEN,
            execution_mode="off",
            credential_policy={},
        )
        monkeypatch.setattr(
            "mindroom.tool_system.sandbox_proxy.httpx.Client",
            _recording_client_class(captured=captured),
        )

        tool = get_tool_by_name(
            "coding",
            runtime_paths,
            execution_identity=None,
            tool_init_overrides={"base_dir": "/workspace/demo"},
            worker_tools_override=["coding"],
        )
        entrypoint = tool.functions["ls"].entrypoint
        assert entrypoint is not None

        result = entrypoint(path=".")

        assert result == "sandbox-result"
        assert captured["url"] == "http://sandbox:8765/api/sandbox-runner/execute"
        assert captured["json"]["tool_init_overrides"] == {"base_dir": "/workspace/demo"}

    def test_proxy_rewrites_storage_root_base_dir_to_shared_relative_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Worker-keyed proxy execution should rewrite canonical agent base_dir paths portably."""
        captured: dict[str, Any] = {}

        monkeypatch.delenv("MINDROOM_STORAGE_PATH", raising=False)
        runtime_paths = _configure_proxy_runtime(
            monkeypatch,
            proxy_url="http://sandbox:8765",
            proxy_token=_TEST_AUTH_TOKEN,
            execution_mode="off",
            credential_policy={},
        )
        execution_identity = ToolExecutionIdentity(
            channel="matrix",
            agent_name="general",
            requester_id="@alice:example.org",
            room_id="!room:example.org",
            thread_id="$thread",
            resolved_thread_id="$thread",
            session_id="session-1",
        )
        monkeypatch.setattr(
            "mindroom.tool_system.sandbox_proxy.httpx.Client",
            _recording_client_class(captured=captured),
        )
        tool = get_tool_by_name(
            "coding",
            runtime_paths,
            execution_identity=execution_identity,
            tool_init_overrides={"base_dir": "/srv/mindroom/agents/general/workspace/mind_data"},
            shared_storage_root_path=Path("/srv/mindroom"),
            worker_tools_override=["coding"],
            worker_scope="shared",
            routing_agent_name="general",
        )
        entrypoint = tool.functions["ls"].entrypoint
        assert entrypoint is not None
        result = entrypoint(path=".")

        assert result == "sandbox-result"
        assert captured["url"].endswith("/api/sandbox-runner/execute")
        assert captured["json"]["tool_init_overrides"] == {"base_dir": "agents/general/workspace/mind_data"}

    def test_proxy_preserves_storage_root_absolute_base_dir_without_worker_key(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Unscoped proxied calls must keep absolute canonical paths unchanged."""
        captured: dict[str, Any] = {}
        storage_root = tmp_path / "mindroom_data"
        base_dir = storage_root / "agents" / "general" / "workspace" / "mind_data"

        monkeypatch.setenv("MINDROOM_STORAGE_PATH", str(storage_root))
        runtime_paths = _configure_proxy_runtime(
            monkeypatch,
            proxy_url="http://sandbox:8765",
            proxy_token=_TEST_AUTH_TOKEN,
            execution_mode="off",
            credential_policy={},
        )
        monkeypatch.setattr(
            "mindroom.tool_system.sandbox_proxy.httpx.Client",
            _recording_client_class(captured=captured),
        )

        tool = get_tool_by_name(
            "coding",
            runtime_paths,
            execution_identity=None,
            tool_init_overrides={"base_dir": str(base_dir)},
            worker_tools_override=["coding"],
        )
        entrypoint = tool.functions["ls"].entrypoint
        assert entrypoint is not None

        result = entrypoint(path=".")

        assert result == "sandbox-result"
        assert captured["url"] == "http://sandbox:8765/api/sandbox-runner/execute"
        assert captured["json"]["tool_init_overrides"] == {
            "base_dir": str(base_dir),
        }

    def test_proxy_preserves_unrelated_absolute_base_dir_for_worker_key(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Worker-keyed requests must not retarget unrelated absolute paths that merely contain `agents`."""
        captured: dict[str, Any] = {}
        unrelated_base_dir = tmp_path / "demo" / "agents" / "general" / "workspace"

        runtime_paths = _configure_proxy_runtime(
            monkeypatch,
            proxy_url="http://sandbox:8765",
            proxy_token=_TEST_AUTH_TOKEN,
            execution_mode="off",
            credential_policy={},
        )
        execution_identity = ToolExecutionIdentity(
            channel="matrix",
            agent_name="general",
            requester_id="@alice:example.org",
            room_id="!room:example.org",
            thread_id="$thread",
            resolved_thread_id="$thread",
            session_id="session-1",
        )
        monkeypatch.setattr(
            "mindroom.tool_system.sandbox_proxy.httpx.Client",
            _recording_client_class(captured=captured),
        )

        tool = get_tool_by_name(
            "coding",
            runtime_paths,
            execution_identity=execution_identity,
            tool_init_overrides={"base_dir": str(unrelated_base_dir)},
            worker_tools_override=["coding"],
            worker_scope="shared",
            routing_agent_name="general",
        )
        entrypoint = tool.functions["ls"].entrypoint
        assert entrypoint is not None
        result = entrypoint(path=".")

        assert result == "sandbox-result"
        assert captured["json"]["tool_init_overrides"] == {
            "base_dir": str(unrelated_base_dir),
        }
