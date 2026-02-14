"""Tests for sandbox runner API endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING

import mindroom.sandbox_proxy as sandbox_proxy_module

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

SANDBOX_TOKEN = "secret-token"  # noqa: S105
SANDBOX_HEADERS = {"x-mindroom-sandbox-token": SANDBOX_TOKEN}


def _set_sandbox_token(monkeypatch: object) -> None:
    """Set the sandbox token in both the module cache and env for subprocess workers."""
    monkeypatch.setattr(sandbox_proxy_module, "_PROXY_TOKEN", SANDBOX_TOKEN)
    monkeypatch.setenv("MINDROOM_SANDBOX_PROXY_TOKEN", SANDBOX_TOKEN)


def test_sandbox_runner_executes_tool_call(test_client: TestClient, monkeypatch: object) -> None:
    """Sandbox runner should execute tool calls and return their result."""
    _set_sandbox_token(monkeypatch)
    response = test_client.post(
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


def test_sandbox_runner_executes_tool_call_in_subprocess_mode(test_client: TestClient, monkeypatch: object) -> None:
    """Sandbox runner should optionally execute tool calls in a subprocess."""
    _set_sandbox_token(monkeypatch)
    monkeypatch.setenv("MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE", "subprocess")
    response = test_client.post(
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


def test_sandbox_runner_rejects_missing_token(test_client: TestClient, monkeypatch: object) -> None:
    """Sandbox runner should require the shared token when configured."""
    _set_sandbox_token(monkeypatch)
    response = test_client.post(
        "/api/sandbox-runner/execute",
        json={
            "tool_name": "calculator",
            "function_name": "add",
            "args": [1, 2],
            "kwargs": {},
        },
    )
    assert response.status_code == 401

    authed_response = test_client.post(
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


def test_sandbox_runner_rejects_when_token_not_configured(test_client: TestClient, monkeypatch: object) -> None:
    """Sandbox runner should fail closed when no token is configured."""
    monkeypatch.setattr(sandbox_proxy_module, "_PROXY_TOKEN", None)
    response = test_client.post(
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


def test_sandbox_runner_lease_is_one_time_use(test_client: TestClient, monkeypatch: object) -> None:
    """Credential leases should be consumed after one execution by default."""
    _set_sandbox_token(monkeypatch)

    lease_response = test_client.post(
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

    first_execute = test_client.post(
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

    second_execute = test_client.post(
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


def test_sandbox_runner_subprocess_consumes_lease(test_client: TestClient, monkeypatch: object) -> None:
    """Lease-based credential overrides should work in subprocess mode."""
    _set_sandbox_token(monkeypatch)
    monkeypatch.setenv("MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE", "subprocess")

    lease_response = test_client.post(
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

    execute_response = test_client.post(
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


def test_sandbox_runner_unknown_tool_returns_404(test_client: TestClient, monkeypatch: object) -> None:
    """Unknown tools should return 404 instead of an unhandled server error."""
    _set_sandbox_token(monkeypatch)
    response = test_client.post(
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
