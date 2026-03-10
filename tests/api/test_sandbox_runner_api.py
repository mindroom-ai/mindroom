"""Tests for sandbox runner API endpoints."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

import mindroom.tool_system.sandbox_proxy as sandbox_proxy_module
from mindroom.api.sandbox_runner_app import app as sandbox_runner_app
from mindroom.tool_system.metadata import ensure_tool_registry_loaded
from mindroom.tool_system.worker_routing import worker_dir_name

if TYPE_CHECKING:
    from pathlib import Path

SANDBOX_TOKEN = "secret-token"  # noqa: S105
SANDBOX_HEADERS = {"x-mindroom-sandbox-token": SANDBOX_TOKEN}


@pytest.fixture(autouse=True)
def _load_tools() -> None:
    ensure_tool_registry_loaded()


@pytest.fixture
def runner_client() -> TestClient:
    """Create a test client for the sandbox runner app."""
    return TestClient(sandbox_runner_app)


def _set_sandbox_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the sandbox token in both the module cache and env for subprocess workers."""
    monkeypatch.setattr(sandbox_proxy_module, "_PROXY_TOKEN", SANDBOX_TOKEN)
    monkeypatch.setenv("MINDROOM_SANDBOX_PROXY_TOKEN", SANDBOX_TOKEN)


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
    monkeypatch.setattr(sandbox_proxy_module, "_PROXY_TOKEN", None)
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


def test_sandbox_runner_worker_file_state_persists_and_is_isolated(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Worker-routed file tools should persist state by worker key and isolate different workers."""
    _set_sandbox_token(monkeypatch)
    worker_root = tmp_path / "workers"
    monkeypatch.setenv("MINDROOM_SANDBOX_WORKER_ROOT", str(worker_root))

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


def test_sandbox_runner_worker_python_uses_persistent_virtualenv(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Worker-routed python tools should execute inside the worker-specific virtualenv."""
    _set_sandbox_token(monkeypatch)
    worker_root = tmp_path / "workers"
    monkeypatch.setenv("MINDROOM_SANDBOX_WORKER_ROOT", str(worker_root))

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
