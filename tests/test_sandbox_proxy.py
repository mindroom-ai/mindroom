"""Tests for generic sandbox proxy tool wrapping."""

from __future__ import annotations

import asyncio
from typing import Any, Self

import pytest

import mindroom.tool_system.sandbox_proxy as sandbox_proxy_module
import mindroom.tools  # noqa: F401
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_key, tool_execution_identity
from tests.conftest import FakeCredentialsManager


def test_proxy_wraps_tool_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tool entrypoints should call the sandbox runner API when proxy mode is enabled."""
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
    monkeypatch.setattr(sandbox_proxy_module, "_EXECUTION_MODE", "selective")
    monkeypatch.setattr(sandbox_proxy_module, "_PROXY_TOOLS", {"calculator"})
    monkeypatch.setattr(sandbox_proxy_module, "_SANDBOX_RUNNER_MODE", False)
    monkeypatch.setattr(sandbox_proxy_module, "_CREDENTIAL_POLICY", {})
    monkeypatch.setattr("mindroom.tool_system.sandbox_proxy.httpx.Client", _FakeClient)

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

    class _FakeResponse:
        def __init__(self, payload: dict[str, object]) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return

        def json(self) -> dict[str, object]:
            return self._payload

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

    fake_credentials = FakeCredentialsManager({"openai": {"api_key": "sk-test", "_source": "ui"}})

    monkeypatch.setattr(sandbox_proxy_module, "_PROXY_URL", "http://sandbox-runner:8765")
    monkeypatch.setattr(sandbox_proxy_module, "_PROXY_TOKEN", "test-token")
    monkeypatch.setattr(sandbox_proxy_module, "_EXECUTION_MODE", "all")
    monkeypatch.setattr(sandbox_proxy_module, "_SANDBOX_RUNNER_MODE", False)
    monkeypatch.setattr(sandbox_proxy_module, "_CREDENTIAL_POLICY", {"calculator.add": ("openai",)})
    monkeypatch.setattr("mindroom.tool_system.sandbox_proxy.get_credentials_manager", lambda: fake_credentials)
    monkeypatch.setattr("mindroom.tool_system.sandbox_proxy.httpx.Client", _FakeClient)

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

        class _FakeResponse:
            def raise_for_status(self) -> None:
                return

            def json(self) -> dict[str, object]:
                return {"ok": True, "result": "sandbox-result"}

        class _FakeClient:
            def __init__(self, *, timeout: float) -> None:
                pass

            def __enter__(self) -> Self:
                return self

            def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
                return

            def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> _FakeResponse:  # noqa: ARG002
                captured["url"] = url
                return _FakeResponse()

        monkeypatch.setattr(sandbox_proxy_module, "_PROXY_URL", "http://sandbox:8765")
        monkeypatch.setattr(sandbox_proxy_module, "_PROXY_TOKEN", "test-token")
        monkeypatch.setattr(sandbox_proxy_module, "_EXECUTION_MODE", "off")  # env says off
        monkeypatch.setattr(sandbox_proxy_module, "_SANDBOX_RUNNER_MODE", False)
        monkeypatch.setattr(sandbox_proxy_module, "_CREDENTIAL_POLICY", {})
        monkeypatch.setattr("mindroom.tool_system.sandbox_proxy.httpx.Client", _FakeClient)

        # Override says sandbox calculator
        tool = get_tool_by_name("calculator", worker_tools_override=["calculator"])
        entrypoint = tool.functions["add"].entrypoint
        assert entrypoint is not None
        result = entrypoint(1, 2)
        assert result == "sandbox-result"
        assert captured["url"] == "http://sandbox:8765/api/sandbox-runner/execute"
