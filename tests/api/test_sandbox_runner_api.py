"""Tests for sandbox runner API endpoints."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import mindroom.api.sandbox_runner as sandbox_runner_module
import mindroom.tool_system.sandbox_proxy as sandbox_proxy_module
from mindroom.api.sandbox_runner_app import app as sandbox_runner_app
from mindroom.tool_system.metadata import ensure_tool_registry_loaded
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    agent_workspace_root_path,
    resolve_worker_key,
    worker_dir_name,
)
from mindroom.workers.backends import local as local_workers_module
from mindroom.workers.models import WorkerSpec

if TYPE_CHECKING:
    from pathlib import Path

SANDBOX_TOKEN = "secret-token"  # noqa: S105
SANDBOX_HEADERS = {"x-mindroom-sandbox-token": SANDBOX_TOKEN}
REQUIRES_LINUX_LOCAL_WORKER = pytest.mark.skipif(
    sys.platform != "linux",
    reason="local worker venv bootstrap is validated on Linux",
)


@pytest.fixture(autouse=True)
def _load_tools() -> None:
    ensure_tool_registry_loaded()


@pytest.fixture(autouse=True)
def _reset_worker_manager(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(local_workers_module, "_local_worker_manager", None)
    monkeypatch.setattr(local_workers_module, "_local_worker_manager_config", None)
    monkeypatch.setenv("MINDROOM_SANDBOX_WORKER_ROOT", str(tmp_path / "workers"))


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


def test_sandbox_runner_rejects_scoped_worker_base_dir_outside_visible_agent_root(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Scoped workers should reject base_dir overrides outside their visible agent roots."""
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
    assert "allowed agent roots" in response.json()["detail"]


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


@REQUIRES_LINUX_LOCAL_WORKER
def test_sandbox_runner_worker_request_does_not_inject_base_dir_into_unrelated_tools(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Worker requests should still run tools that do not declare a base_dir init field."""
    _set_sandbox_token(monkeypatch)
    worker_root = tmp_path / "workers"
    monkeypatch.setenv("MINDROOM_SANDBOX_WORKER_ROOT", str(worker_root))

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
    worker_root = tmp_path / "workers"
    storage_root = tmp_path / "storage"
    monkeypatch.setenv("MINDROOM_SANDBOX_WORKER_ROOT", str(worker_root))
    monkeypatch.setenv("MINDROOM_STORAGE_PATH", str(storage_root))

    prepare_calls = 0
    original_prepare = sandbox_runner_module._prepare_worker

    def _counting_prepare(worker_key: str) -> object:
        nonlocal prepare_calls
        prepare_calls += 1
        return original_prepare(worker_key)

    async def _fake_execute_request_subprocess(
        request: sandbox_runner_module.SandboxRunnerExecuteRequest,
        prepared_worker: object | None = None,
    ) -> sandbox_runner_module.SandboxRunnerExecuteResponse:
        assert request.worker_key == "worker-a"
        assert prepared_worker is not None
        return sandbox_runner_module.SandboxRunnerExecuteResponse(ok=True, result="ok")

    monkeypatch.setattr(sandbox_runner_module, "_prepare_worker", _counting_prepare)
    monkeypatch.setattr(sandbox_runner_module, "_execute_request_subprocess", _fake_execute_request_subprocess)

    response = runner_client.post(
        "/api/sandbox-runner/execute",
        headers=SANDBOX_HEADERS,
        json={
            "tool_name": "file",
            "function_name": "save_file",
            "args": ["hello", "note.txt"],
            "kwargs": {},
            "worker_key": "worker-a",
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


@REQUIRES_LINUX_LOCAL_WORKER
def test_sandbox_runner_worker_request_preserves_forwarded_base_dir(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Worker requests should honor a forwarded canonical base_dir inside shared agent storage."""
    _set_sandbox_token(monkeypatch)
    worker_root = tmp_path / "workers"
    storage_root = tmp_path / "storage"
    monkeypatch.setenv("MINDROOM_SANDBOX_WORKER_ROOT", str(worker_root))
    monkeypatch.setenv("MINDROOM_STORAGE_PATH", str(storage_root))

    response = runner_client.post(
        "/api/sandbox-runner/execute",
        headers=SANDBOX_HEADERS,
        json={
            "tool_name": "file",
            "function_name": "save_file",
            "args": ["hello from canonical workspace", "note.txt"],
            "kwargs": {},
            "worker_key": "worker-a",
            "tool_init_overrides": {"base_dir": "agents/general/workspace/mind_data"},
        },
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True

    canonical_file = agent_workspace_root_path(storage_root, "general") / "mind_data" / "note.txt"
    assert canonical_file.read_text(encoding="utf-8") == "hello from canonical workspace"
    assert not (worker_root / worker_dir_name("worker-a") / "workspace" / "note.txt").exists()


@REQUIRES_LINUX_LOCAL_WORKER
def test_sandbox_runner_worker_request_uses_default_storage_root_when_env_is_unset(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Worker requests should validate canonical agent roots against the default storage root."""
    _set_sandbox_token(monkeypatch)
    storage_root = tmp_path / "default-storage"
    monkeypatch.delenv("MINDROOM_STORAGE_PATH", raising=False)
    monkeypatch.delenv("MINDROOM_SANDBOX_DEDICATED_WORKER_KEY", raising=False)
    monkeypatch.delenv("MINDROOM_SANDBOX_DEDICATED_WORKER_ROOT", raising=False)
    monkeypatch.setattr(sandbox_runner_module, "STORAGE_PATH_OBJ", storage_root)

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


@REQUIRES_LINUX_LOCAL_WORKER
def test_sandbox_runner_worker_python_supports_matrix_scoped_worker_keys(
    runner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Scoped worker keys should be sanitized before they reach the venv path."""
    _set_sandbox_token(monkeypatch)
    worker_root = tmp_path / "workers"
    monkeypatch.setenv("MINDROOM_SANDBOX_WORKER_ROOT", str(worker_root))
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
    assert worker["debug_metadata"]["state_root"] == str((tmp_path / "workers" / worker_dir_name("worker-a")).resolve())


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

    metadata_path = tmp_path / "workers" / worker_dir_name("worker-a") / "metadata" / "worker.json"
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

    worker_file = tmp_path / "workers" / worker_dir_name("worker-a") / "workspace" / "note.txt"
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
        assert env["MINDROOM_SANDBOX_DEDICATED_WORKER_KEY"] == "worker-a"
        assert env["MINDROOM_SANDBOX_DEDICATED_WORKER_ROOT"] == str(worker_root)
        request_payload = json.loads(request_input)
        assert request_payload["worker_key"] == "worker-a"
        note_path = worker_root / "workspace" / request_payload["args"][1]
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text(request_payload["args"][0], encoding="utf-8")
        response = sandbox_runner_module.SandboxRunnerExecuteResponse(ok=True, result="saved")
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout="",
            stderr=sandbox_runner_module._RESPONSE_MARKER + response.model_dump_json(),
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
        request_payload = json.loads(str(run_kwargs["input"]))
        assert request_payload["worker_key"] == "worker-a"

        note_path = worker_root / "workspace" / request_payload["args"][1]
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text(request_payload["args"][0], encoding="utf-8")

        response = sandbox_runner_module.SandboxRunnerExecuteResponse(ok=True, result="saved")
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout="",
            stderr=sandbox_runner_module._RESPONSE_MARKER + response.model_dump_json(),
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
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is False
    assert "Dedicated sandbox worker is pinned" in data["error"]


def test_worker_subprocess_env_preserves_parent_worker_root_without_explicit_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Subprocess workers should resolve back to the parent worker root even when only storage path is set."""
    monkeypatch.delenv("MINDROOM_SANDBOX_WORKER_ROOT", raising=False)
    monkeypatch.setenv("MINDROOM_STORAGE_PATH", str(tmp_path / ".mindroom"))

    worker_root = local_workers_module._default_worker_root()
    paths = local_workers_module._local_worker_state_paths("worker-a", worker_root=worker_root)
    subprocess_env = sandbox_runner_module._worker_subprocess_env(paths)

    with patch.dict("os.environ", subprocess_env, clear=True):
        child_worker_root = local_workers_module._default_worker_root()

    child_paths = local_workers_module._local_worker_state_paths("worker-a", worker_root=child_worker_root)
    assert child_worker_root == worker_root
    assert child_paths.root == paths.root
    assert subprocess_env["MINDROOM_SANDBOX_WORKER_ROOT"] == str(worker_root)


def test_get_local_worker_manager_singleton_creation_is_thread_safe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Concurrent access should build the local worker manager only once per config."""
    monkeypatch.setattr(local_workers_module, "_local_worker_manager", None)
    monkeypatch.setattr(local_workers_module, "_local_worker_manager_config", None)
    monkeypatch.setenv("MINDROOM_SANDBOX_WORKER_ROOT", str(tmp_path / "workers"))

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
            managers.append(local_workers_module.get_local_worker_manager())
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
    data = execute_response.json()
    assert data["ok"] is False
    assert "Failed to initialize worker 'worker-fail'" in data["error"]
    assert "boom" in data["error"]

    workers_response = runner_client.get("/api/sandbox-runner/workers", headers=SANDBOX_HEADERS)
    assert workers_response.status_code == 200

    worker = next(worker for worker in workers_response.json()["workers"] if worker["worker_key"] == "worker-fail")
    assert worker["status"] == "failed"
    assert "boom" in worker["failure_reason"]
    assert worker["failure_count"] == 1
