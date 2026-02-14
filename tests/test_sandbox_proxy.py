"""Tests for generic sandbox proxy tool wrapping."""

from __future__ import annotations

from typing import Any, Self

import pytest

import mindroom.tools  # noqa: F401
from mindroom.tools_metadata import get_tool_by_name
from tests.conftest import FakeCredentialsManager


def test_proxy_wraps_tool_calls(monkeypatch: object) -> None:
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

    monkeypatch.setenv("MINDROOM_SANDBOX_PROXY_URL", "http://sandbox-runner:8765")
    monkeypatch.setenv("MINDROOM_SANDBOX_EXECUTION_MODE", "selective")
    monkeypatch.setenv("MINDROOM_SANDBOX_PROXY_TOOLS", "calculator")
    monkeypatch.setenv("MINDROOM_SANDBOX_PROXY_TOKEN", "test-token")
    monkeypatch.delenv("MINDROOM_SANDBOX_CREDENTIAL_POLICY_JSON", raising=False)
    monkeypatch.delenv("MINDROOM_SANDBOX_RUNNER_MODE", raising=False)
    monkeypatch.setattr("mindroom.sandbox_proxy.httpx.Client", _FakeClient)

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


def test_proxy_disabled_in_runner_mode(monkeypatch: object) -> None:
    """Runner mode must execute tools locally to avoid proxy recursion."""

    class _ForbiddenClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            msg = "Proxy client should not be used in runner mode."
            raise AssertionError(msg)

    monkeypatch.setenv("MINDROOM_SANDBOX_PROXY_URL", "http://sandbox-runner:8765")
    monkeypatch.setenv("MINDROOM_SANDBOX_EXECUTION_MODE", "all")
    monkeypatch.setenv("MINDROOM_SANDBOX_PROXY_TOOLS", "calculator")
    monkeypatch.setenv("MINDROOM_SANDBOX_RUNNER_MODE", "true")
    monkeypatch.setattr("mindroom.sandbox_proxy.httpx.Client", _ForbiddenClient)

    tool = get_tool_by_name("calculator")
    entrypoint = tool.functions["add"].entrypoint
    assert entrypoint is not None
    result = entrypoint(1, 2)
    assert '"result": 3' in result


def test_proxy_requests_credential_lease_when_policy_matches(monkeypatch: object) -> None:
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

    monkeypatch.setenv("MINDROOM_SANDBOX_PROXY_URL", "http://sandbox-runner:8765")
    monkeypatch.setenv("MINDROOM_SANDBOX_EXECUTION_MODE", "all")
    monkeypatch.setenv("MINDROOM_SANDBOX_PROXY_TOKEN", "test-token")
    monkeypatch.setenv("MINDROOM_SANDBOX_CREDENTIAL_POLICY_JSON", '{"calculator.add":["openai"]}')
    monkeypatch.setattr("mindroom.sandbox_proxy.get_credentials_manager", lambda: fake_credentials)
    monkeypatch.setattr("mindroom.sandbox_proxy.httpx.Client", _FakeClient)

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


def test_proxy_requires_shared_token(monkeypatch: object) -> None:
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

    monkeypatch.setenv("MINDROOM_SANDBOX_PROXY_URL", "http://sandbox-runner:8765")
    monkeypatch.setenv("MINDROOM_SANDBOX_EXECUTION_MODE", "all")
    monkeypatch.delenv("MINDROOM_SANDBOX_PROXY_TOKEN", raising=False)
    monkeypatch.setattr("mindroom.sandbox_proxy.httpx.Client", _FakeClient)

    tool = get_tool_by_name("calculator")
    entrypoint = tool.functions["add"].entrypoint
    assert entrypoint is not None
    with pytest.raises(RuntimeError, match="MINDROOM_SANDBOX_PROXY_TOKEN"):
        entrypoint(1, 2)
