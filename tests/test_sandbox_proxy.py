"""Tests for generic sandbox proxy tool wrapping."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

import pytest

import mindroom.tool_system.sandbox_proxy as sandbox_proxy_module
import mindroom.tools  # noqa: F401
from mindroom.tool_system.metadata import ToolInitOverrideError, get_tool_by_name
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_key, tool_execution_identity
from mindroom.workers import runtime as workers_runtime_module
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.backends.static_runner import StaticSandboxRunnerBackend
from mindroom.workers.models import WorkerSpec
from tests.conftest import FakeCredentialsManager

if TYPE_CHECKING:
    from collections.abc import Callable

_TEST_AUTH_TOKEN = "test-token"  # noqa: S105


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

    monkeypatch.setattr(sandbox_proxy_module, "_PROXY_URL", "http://sandbox-runner:8765")
    monkeypatch.setattr(sandbox_proxy_module, "_PROXY_TOKEN", "test-token")
    monkeypatch.setattr(sandbox_proxy_module, "_EXECUTION_MODE", "selective")
    monkeypatch.setattr(sandbox_proxy_module, "_PROXY_TOOLS", {"calculator"})
    monkeypatch.setattr(sandbox_proxy_module, "_SANDBOX_RUNNER_MODE", False)
    monkeypatch.setattr(sandbox_proxy_module, "_CREDENTIAL_POLICY", {})
    monkeypatch.setattr(
        "mindroom.tool_system.sandbox_proxy.httpx.Client",
        _recording_client_class(captured=captured),
    )

    tool = get_tool_by_name("calculator")
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

    monkeypatch.setattr(sandbox_proxy_module, "_PROXY_URL", "http://sandbox-runner:8765")
    monkeypatch.setattr(sandbox_proxy_module, "_EXECUTION_MODE", "all")
    monkeypatch.setattr(sandbox_proxy_module, "_SANDBOX_RUNNER_MODE", True)
    monkeypatch.setattr("mindroom.tool_system.sandbox_proxy.httpx.Client", _ForbiddenClient)

    tool = get_tool_by_name("calculator")
    entrypoint = tool.functions["add"].entrypoint
    assert entrypoint is not None
    result = entrypoint(1, 2)
    assert '"result": 3' in result


def test_proxy_requests_credential_lease_when_policy_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Proxy should create and consume a lease when credential sharing policy allows it."""
    captured_calls: list[tuple[str, dict[str, Any]]] = []

    fake_credentials = FakeCredentialsManager({"openai": {"api_key": "sk-test", "_source": "ui"}})

    monkeypatch.setattr(sandbox_proxy_module, "_PROXY_URL", "http://sandbox-runner:8765")
    monkeypatch.setattr(sandbox_proxy_module, "_PROXY_TOKEN", "test-token")
    monkeypatch.setattr(sandbox_proxy_module, "_EXECUTION_MODE", "all")
    monkeypatch.setattr(sandbox_proxy_module, "_SANDBOX_RUNNER_MODE", False)
    monkeypatch.setattr(sandbox_proxy_module, "_CREDENTIAL_POLICY", {"calculator.add": ("openai",)})
    monkeypatch.setattr("mindroom.tool_system.sandbox_proxy.get_credentials_manager", lambda: fake_credentials)
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

    tool = get_tool_by_name("calculator")
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
        get_tool_by_name("openai", tool_init_overrides={"api_key": "sk-test"})


def test_get_tool_by_name_rejects_invalid_base_dir_override_type() -> None:
    """base_dir overrides should be validated before toolkit construction."""
    with pytest.raises(ToolInitOverrideError, match="base_dir"):
        get_tool_by_name("coding", tool_init_overrides={"base_dir": {"bad": "value"}})


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

    monkeypatch.setattr(sandbox_proxy_module, "_PROXY_URL", "http://sandbox-runner:8765")
    monkeypatch.setattr(sandbox_proxy_module, "_PROXY_TOKEN", None)
    monkeypatch.setattr(sandbox_proxy_module, "_EXECUTION_MODE", "all")
    monkeypatch.setattr(sandbox_proxy_module, "_SANDBOX_RUNNER_MODE", False)
    monkeypatch.setattr("mindroom.tool_system.sandbox_proxy.httpx.Client", _FakeClient)

    tool = get_tool_by_name("calculator")
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

    monkeypatch.setattr(sandbox_proxy_module, "_PROXY_URL", "http://sandbox-runner:8765")
    monkeypatch.setattr(sandbox_proxy_module, "_PROXY_TOKEN", "test-token")
    monkeypatch.setattr(sandbox_proxy_module, "_EXECUTION_MODE", "off")
    monkeypatch.setattr(sandbox_proxy_module, "_SANDBOX_RUNNER_MODE", False)
    monkeypatch.setattr(sandbox_proxy_module, "_CREDENTIAL_POLICY", {"calculator.add": ("openai",)})
    monkeypatch.setattr("mindroom.tool_system.sandbox_proxy.get_credentials_manager", lambda: fake_credentials)
    monkeypatch.setattr("mindroom.tool_system.sandbox_proxy.httpx.Client", _FakeClient)

    tool = get_tool_by_name(
        "calculator",
        worker_tools_override=["calculator"],
        worker_scope="user",
        routing_agent_name="code",
    )
    entrypoint = tool.functions["add"].entrypoint
    assert entrypoint is not None
    with tool_execution_identity(execution_identity):
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

    monkeypatch.setattr(sandbox_proxy_module, "_PROXY_URL", "http://sandbox-runner:8765")
    monkeypatch.setattr(sandbox_proxy_module, "_PROXY_TOKEN", "test-token")
    monkeypatch.setattr(sandbox_proxy_module, "_EXECUTION_MODE", "off")
    monkeypatch.setattr(sandbox_proxy_module, "_SANDBOX_RUNNER_MODE", False)
    monkeypatch.setattr(sandbox_proxy_module, "_CREDENTIAL_POLICY", {})
    monkeypatch.setattr("mindroom.tool_system.sandbox_proxy.httpx.Client", _FakeClient)

    tool = get_tool_by_name(
        "calculator",
        worker_tools_override=["calculator"],
        worker_scope="user_agent",
        routing_agent_name="code",
    )
    entrypoint = tool.functions["add"].entrypoint
    assert entrypoint is not None

    execution_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="shared_agent",
        requester_id="alice",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-1",
    )
    expected_worker_key = resolve_worker_key("user_agent", execution_identity, agent_name="code")
    assert expected_worker_key is not None

    with tool_execution_identity(execution_identity):
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
    monkeypatch.setattr(sandbox_proxy_module, "_PROXY_URL", "http://sandbox-runner:8765")
    monkeypatch.setattr(sandbox_proxy_module, "_PROXY_TOKEN", "test-token")
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
            managers.append(sandbox_proxy_module._get_worker_manager())
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
    monkeypatch.setattr(sandbox_proxy_module, "_SANDBOX_RUNNER_MODE", False)
    monkeypatch.setattr(sandbox_proxy_module, "_PROXY_URL", None)
    monkeypatch.setattr(sandbox_proxy_module, "_PROXY_TOKEN", "test-token")
    monkeypatch.setattr(sandbox_proxy_module, "_EXECUTION_MODE", "off")
    monkeypatch.setenv("MINDROOM_WORKER_BACKEND", "kubernetes")
    monkeypatch.setenv("MINDROOM_KUBERNETES_WORKER_IMAGE", "ghcr.io/mindroom-ai/mindroom:latest")
    monkeypatch.setenv("MINDROOM_KUBERNETES_WORKER_STORAGE_PVC_NAME", "mindroom-storage")

    assert (
        sandbox_proxy_module._sandbox_proxy_enabled_for_tool(
            "shell",
            worker_tools_override=["shell"],
            worker_scope="shared",
        )
        is True
    )
    assert sandbox_proxy_module._sandbox_proxy_enabled_for_tool("shell", worker_tools_override=None) is False


def test_kubernetes_backend_keeps_unscoped_env_routing_enabled_without_proxy_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unscoped agents should still route through dedicated workers on the Kubernetes backend."""
    monkeypatch.setattr(sandbox_proxy_module, "_SANDBOX_RUNNER_MODE", False)
    monkeypatch.setattr(sandbox_proxy_module, "_PROXY_URL", None)
    monkeypatch.setattr(sandbox_proxy_module, "_PROXY_TOKEN", "test-token")
    monkeypatch.setattr(sandbox_proxy_module, "_EXECUTION_MODE", "selective")
    monkeypatch.setattr(sandbox_proxy_module, "_PROXY_TOOLS", {"shell"})
    monkeypatch.setenv("MINDROOM_WORKER_BACKEND", "kubernetes")
    monkeypatch.setenv("MINDROOM_KUBERNETES_WORKER_IMAGE", "ghcr.io/mindroom-ai/mindroom:latest")
    monkeypatch.setenv("MINDROOM_KUBERNETES_WORKER_STORAGE_PVC_NAME", "mindroom-storage")

    assert sandbox_proxy_module._sandbox_proxy_enabled_for_tool("shell", worker_scope=None) is True
    assert sandbox_proxy_module._sandbox_proxy_enabled_for_tool("calculator", worker_scope=None) is False


def test_kubernetes_backend_uses_env_routing_for_worker_scoped_agents_without_proxy_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worker-scoped agents should still honor env-based routing on the Kubernetes backend."""
    monkeypatch.setattr(sandbox_proxy_module, "_SANDBOX_RUNNER_MODE", False)
    monkeypatch.setattr(sandbox_proxy_module, "_PROXY_URL", None)
    monkeypatch.setattr(sandbox_proxy_module, "_PROXY_TOKEN", "test-token")
    monkeypatch.setattr(sandbox_proxy_module, "_EXECUTION_MODE", "selective")
    monkeypatch.setattr(sandbox_proxy_module, "_PROXY_TOOLS", {"shell"})
    monkeypatch.setenv("MINDROOM_WORKER_BACKEND", "kubernetes")
    monkeypatch.setenv("MINDROOM_KUBERNETES_WORKER_IMAGE", "ghcr.io/mindroom-ai/mindroom:latest")
    monkeypatch.setenv("MINDROOM_KUBERNETES_WORKER_STORAGE_PVC_NAME", "mindroom-storage")

    assert sandbox_proxy_module._sandbox_proxy_enabled_for_tool("shell", worker_scope="user") is True
    assert sandbox_proxy_module._sandbox_proxy_enabled_for_tool("calculator", worker_scope="user") is False


def test_kubernetes_backend_keeps_wrapping_when_required_config_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kubernetes routing should stay enabled so misconfiguration fails closed at call time."""
    monkeypatch.setattr(sandbox_proxy_module, "_SANDBOX_RUNNER_MODE", False)
    monkeypatch.setattr(sandbox_proxy_module, "_PROXY_URL", None)
    monkeypatch.setattr(sandbox_proxy_module, "_PROXY_TOKEN", "test-token")
    monkeypatch.setenv("MINDROOM_WORKER_BACKEND", "kubernetes")
    monkeypatch.delenv("MINDROOM_KUBERNETES_WORKER_IMAGE", raising=False)
    monkeypatch.delenv("MINDROOM_KUBERNETES_WORKER_STORAGE_PVC_NAME", raising=False)

    assert sandbox_proxy_module._sandbox_proxy_enabled_for_tool("shell", worker_tools_override=["shell"]) is True


def test_kubernetes_backend_keeps_wrapping_when_proxy_token_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kubernetes routing should stay enabled so missing auth fails closed at call time."""
    monkeypatch.setattr(sandbox_proxy_module, "_SANDBOX_RUNNER_MODE", False)
    monkeypatch.setattr(sandbox_proxy_module, "_PROXY_URL", None)
    monkeypatch.setattr(sandbox_proxy_module, "_PROXY_TOKEN", None)
    monkeypatch.setenv("MINDROOM_WORKER_BACKEND", "kubernetes")
    monkeypatch.setenv("MINDROOM_KUBERNETES_WORKER_IMAGE", "ghcr.io/mindroom-ai/mindroom:latest")
    monkeypatch.setenv("MINDROOM_KUBERNETES_WORKER_STORAGE_PVC_NAME", "mindroom-storage")

    assert sandbox_proxy_module._sandbox_proxy_enabled_for_tool("shell", worker_tools_override=["shell"]) is True


def test_kubernetes_backend_misconfiguration_raises_instead_of_running_locally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Misconfigured Kubernetes worker routing should raise rather than executing in the primary runtime."""
    monkeypatch.setattr(sandbox_proxy_module, "_SANDBOX_RUNNER_MODE", False)
    monkeypatch.setattr(sandbox_proxy_module, "_PROXY_URL", None)
    monkeypatch.setattr(sandbox_proxy_module, "_PROXY_TOKEN", "test-token")
    monkeypatch.setattr(sandbox_proxy_module, "_EXECUTION_MODE", "off")
    monkeypatch.setenv("MINDROOM_WORKER_BACKEND", "kubernetes")
    monkeypatch.delenv("MINDROOM_KUBERNETES_WORKER_IMAGE", raising=False)
    monkeypatch.delenv("MINDROOM_KUBERNETES_WORKER_STORAGE_PVC_NAME", raising=False)

    tool = get_tool_by_name("shell", worker_tools_override=["shell"], worker_scope=None, routing_agent_name="code")
    entrypoint = tool.functions["run_shell_command"].entrypoint
    assert entrypoint is not None

    with pytest.raises(WorkerBackendError, match="MINDROOM_KUBERNETES_WORKER_IMAGE"):
        entrypoint("pwd")


class TestWorkerToolsOverride:
    """Tests for per-agent worker_tools_override parameter."""

    def test_override_none_defers_to_env_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """None override should defer to the standard env var logic."""
        monkeypatch.setattr(sandbox_proxy_module, "_SANDBOX_RUNNER_MODE", False)
        monkeypatch.setattr(sandbox_proxy_module, "_PROXY_URL", "http://sandbox:8765")
        monkeypatch.setattr(sandbox_proxy_module, "_EXECUTION_MODE", "selective")
        monkeypatch.setattr(sandbox_proxy_module, "_PROXY_TOOLS", {"shell"})

        # None override → falls through to env var logic
        assert sandbox_proxy_module._sandbox_proxy_enabled_for_tool("shell", worker_tools_override=None) is True
        assert sandbox_proxy_module._sandbox_proxy_enabled_for_tool("calculator", worker_tools_override=None) is False

    def test_override_empty_list_disables_sandboxing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty list override should disable sandboxing even when env vars enable it."""
        monkeypatch.setattr(sandbox_proxy_module, "_SANDBOX_RUNNER_MODE", False)
        monkeypatch.setattr(sandbox_proxy_module, "_PROXY_URL", "http://sandbox:8765")
        monkeypatch.setattr(sandbox_proxy_module, "_EXECUTION_MODE", "all")

        assert sandbox_proxy_module._sandbox_proxy_enabled_for_tool("shell", worker_tools_override=[]) is False
        assert sandbox_proxy_module._sandbox_proxy_enabled_for_tool("file", worker_tools_override=[]) is False

    def test_override_explicit_list_selects_tools(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Explicit list override should sandbox only the listed tools."""
        monkeypatch.setattr(sandbox_proxy_module, "_SANDBOX_RUNNER_MODE", False)
        monkeypatch.setattr(sandbox_proxy_module, "_PROXY_URL", "http://sandbox:8765")
        # Even with env var saying "off", override wins
        monkeypatch.setattr(sandbox_proxy_module, "_EXECUTION_MODE", "off")

        assert (
            sandbox_proxy_module._sandbox_proxy_enabled_for_tool("shell", worker_tools_override=["shell", "file"])
            is True
        )
        assert (
            sandbox_proxy_module._sandbox_proxy_enabled_for_tool("file", worker_tools_override=["shell", "file"])
            is True
        )
        assert (
            sandbox_proxy_module._sandbox_proxy_enabled_for_tool("calculator", worker_tools_override=["shell", "file"])
            is False
        )

    def test_override_still_respects_runner_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Runner mode should always disable proxying, even with override."""
        monkeypatch.setattr(sandbox_proxy_module, "_SANDBOX_RUNNER_MODE", True)
        monkeypatch.setattr(sandbox_proxy_module, "_PROXY_URL", "http://sandbox:8765")

        assert sandbox_proxy_module._sandbox_proxy_enabled_for_tool("shell", worker_tools_override=["shell"]) is False

    def test_override_still_requires_proxy_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No proxy URL should always disable proxying, even with override."""
        monkeypatch.setattr(sandbox_proxy_module, "_SANDBOX_RUNNER_MODE", False)
        monkeypatch.setattr(sandbox_proxy_module, "_PROXY_URL", None)

        assert sandbox_proxy_module._sandbox_proxy_enabled_for_tool("shell", worker_tools_override=["shell"]) is False

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
        monkeypatch.setattr(sandbox_proxy_module, "_SANDBOX_RUNNER_MODE", False)
        monkeypatch.setattr(sandbox_proxy_module, "_PROXY_URL", "http://sandbox:8765")
        monkeypatch.setattr(sandbox_proxy_module, "_EXECUTION_MODE", "all")

        assert sandbox_proxy_module._sandbox_proxy_enabled_for_tool(tool_name, worker_tools_override=None) is False
        assert (
            sandbox_proxy_module._sandbox_proxy_enabled_for_tool(tool_name, worker_tools_override=[tool_name]) is False
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

        monkeypatch.setattr(sandbox_proxy_module, "_SANDBOX_RUNNER_MODE", False)
        monkeypatch.setattr(sandbox_proxy_module, "_PROXY_URL", "http://sandbox:8765")
        monkeypatch.setattr(sandbox_proxy_module, "_PROXY_TOKEN", "test-token")
        monkeypatch.setattr(sandbox_proxy_module, "_EXECUTION_MODE", "all")
        monkeypatch.setattr(sandbox_proxy_module, "_CREDENTIAL_POLICY", {})
        monkeypatch.setattr("mindroom.tool_system.sandbox_proxy.httpx.Client", _ForbiddenClient)

        fake_credentials = FakeCredentialsManager({})
        monkeypatch.setattr("mindroom.custom_tools.homeassistant.get_credentials_manager", lambda: fake_credentials)

        tool = get_tool_by_name(
            "homeassistant",
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

        monkeypatch.setattr(sandbox_proxy_module, "_PROXY_URL", "http://sandbox:8765")
        monkeypatch.setattr(sandbox_proxy_module, "_PROXY_TOKEN", "test-token")
        monkeypatch.setattr(sandbox_proxy_module, "_EXECUTION_MODE", "off")  # env says off
        monkeypatch.setattr(sandbox_proxy_module, "_SANDBOX_RUNNER_MODE", False)
        monkeypatch.setattr(sandbox_proxy_module, "_CREDENTIAL_POLICY", {})
        monkeypatch.setattr(
            "mindroom.tool_system.sandbox_proxy.httpx.Client",
            _recording_client_class(captured=captured),
        )

        # Override says sandbox calculator
        tool = get_tool_by_name("calculator", worker_tools_override=["calculator"])
        entrypoint = tool.functions["add"].entrypoint
        assert entrypoint is not None
        result = entrypoint(1, 2)
        assert result == "sandbox-result"
        assert captured["url"] == "http://sandbox:8765/api/sandbox-runner/execute"

    def test_get_tool_by_name_passes_tool_init_overrides_to_proxy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Proxy execution should preserve non-secret tool init overrides like base_dir."""
        captured: dict[str, Any] = {}

        monkeypatch.setattr(sandbox_proxy_module, "_PROXY_URL", "http://sandbox:8765")
        monkeypatch.setattr(sandbox_proxy_module, "_PROXY_TOKEN", "test-token")
        monkeypatch.setattr(sandbox_proxy_module, "_EXECUTION_MODE", "off")
        monkeypatch.setattr(sandbox_proxy_module, "_SANDBOX_RUNNER_MODE", False)
        monkeypatch.setattr(sandbox_proxy_module, "_CREDENTIAL_POLICY", {})
        monkeypatch.setattr(
            "mindroom.tool_system.sandbox_proxy.httpx.Client",
            _recording_client_class(captured=captured),
        )

        tool = get_tool_by_name(
            "coding",
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
        monkeypatch.setattr(sandbox_proxy_module, "_PROXY_URL", "http://sandbox:8765")
        monkeypatch.setattr(sandbox_proxy_module, "_PROXY_TOKEN", "test-token")
        monkeypatch.setattr(sandbox_proxy_module, "_EXECUTION_MODE", "off")
        monkeypatch.setattr(sandbox_proxy_module, "_SANDBOX_RUNNER_MODE", False)
        monkeypatch.setattr(sandbox_proxy_module, "_CREDENTIAL_POLICY", {})
        monkeypatch.setattr(
            "mindroom.tool_system.sandbox_proxy.httpx.Client",
            _recording_client_class(captured=captured),
        )
        monkeypatch.setattr(
            sandbox_proxy_module,
            "_current_shared_storage_root",
            lambda: Path("/srv/mindroom"),
        )

        tool = get_tool_by_name(
            "coding",
            tool_init_overrides={"base_dir": "/srv/mindroom/agents/general/workspace/mind_data"},
            worker_tools_override=["coding"],
            worker_scope="shared",
            routing_agent_name="general",
        )
        entrypoint = tool.functions["ls"].entrypoint
        assert entrypoint is not None

        execution_identity = ToolExecutionIdentity(
            channel="matrix",
            agent_name="general",
            requester_id="@alice:example.org",
            room_id="!room:example.org",
            thread_id="$thread",
            resolved_thread_id="$thread",
            session_id="session-1",
        )
        with tool_execution_identity(execution_identity):
            result = entrypoint(path=".")

        assert result == "sandbox-result"
        assert captured["url"].endswith("/api/sandbox-runner/execute")
        assert captured["json"]["tool_init_overrides"] == {"base_dir": "agents/general/workspace/mind_data"}

    def test_proxy_preserves_storage_root_absolute_base_dir_without_worker_key(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Unscoped proxied calls must keep absolute canonical paths unchanged."""
        captured: dict[str, Any] = {}

        monkeypatch.setenv("MINDROOM_STORAGE_PATH", "/mindroom_data")
        monkeypatch.setattr(sandbox_proxy_module, "_PROXY_URL", "http://sandbox:8765")
        monkeypatch.setattr(sandbox_proxy_module, "_PROXY_TOKEN", "test-token")
        monkeypatch.setattr(sandbox_proxy_module, "_EXECUTION_MODE", "off")
        monkeypatch.setattr(sandbox_proxy_module, "_SANDBOX_RUNNER_MODE", False)
        monkeypatch.setattr(sandbox_proxy_module, "_CREDENTIAL_POLICY", {})
        monkeypatch.setattr(
            "mindroom.tool_system.sandbox_proxy.httpx.Client",
            _recording_client_class(captured=captured),
        )

        tool = get_tool_by_name(
            "coding",
            tool_init_overrides={"base_dir": "/mindroom_data/agents/general/workspace/mind_data"},
            worker_tools_override=["coding"],
        )
        entrypoint = tool.functions["ls"].entrypoint
        assert entrypoint is not None

        result = entrypoint(path=".")

        assert result == "sandbox-result"
        assert captured["url"] == "http://sandbox:8765/api/sandbox-runner/execute"
        assert captured["json"]["tool_init_overrides"] == {
            "base_dir": "/mindroom_data/agents/general/workspace/mind_data",
        }

    def test_proxy_preserves_unrelated_absolute_base_dir_for_worker_key(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Worker-keyed requests must not retarget unrelated absolute paths that merely contain `agents`."""
        captured: dict[str, Any] = {}
        unrelated_base_dir = tmp_path / "demo" / "agents" / "general" / "workspace"

        monkeypatch.setattr(sandbox_proxy_module, "_PROXY_URL", "http://sandbox:8765")
        monkeypatch.setattr(sandbox_proxy_module, "_PROXY_TOKEN", "test-token")
        monkeypatch.setattr(sandbox_proxy_module, "_EXECUTION_MODE", "off")
        monkeypatch.setattr(sandbox_proxy_module, "_SANDBOX_RUNNER_MODE", False)
        monkeypatch.setattr(sandbox_proxy_module, "_CREDENTIAL_POLICY", {})
        monkeypatch.setattr(
            "mindroom.tool_system.sandbox_proxy.httpx.Client",
            _recording_client_class(captured=captured),
        )

        tool = get_tool_by_name(
            "coding",
            tool_init_overrides={"base_dir": str(unrelated_base_dir)},
            worker_tools_override=["coding"],
            worker_scope="shared",
            routing_agent_name="general",
        )
        entrypoint = tool.functions["ls"].entrypoint
        assert entrypoint is not None

        execution_identity = ToolExecutionIdentity(
            channel="matrix",
            agent_name="general",
            requester_id="@alice:example.org",
            room_id="!room:example.org",
            thread_id="$thread",
            resolved_thread_id="$thread",
            session_id="session-1",
        )
        with tool_execution_identity(execution_identity):
            result = entrypoint(path=".")

        assert result == "sandbox-result"
        assert captured["json"]["tool_init_overrides"] == {
            "base_dir": str(unrelated_base_dir),
        }
