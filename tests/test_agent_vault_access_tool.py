"""Tests for the Agent Vault self-service access tool."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx
import pytest

from mindroom.constants import resolve_runtime_paths
from mindroom.custom_tools.agent_vault_access import AgentVaultAccessTools, _AgentVaultAccessError
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    resolve_worker_target,
    worker_id_for_key,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths

_ENV = {
    "MINDROOM_AGENT_VAULT_ACCESS_API_URL": "http://agent-vault:14321",
    "MINDROOM_AGENT_VAULT_ACCESS_ADMIN_TOKEN": "owner-token",
    "MINDROOM_AGENT_VAULT_ACCESS_UI_BASE_URL": "https://example.test/agent-vault",
    "MINDROOM_AGENT_VAULT_ACCESS_EMAIL_DOMAIN": "example.test",
}


def _runtime_paths(tmp_path: Path, *, env: dict[str, str] | None = None) -> RuntimePaths:
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env=dict(env if env is not None else _ENV),
    )


def _worker_target(*, requester: str | None = "@bas.nijholt:example.test") -> object:
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="mind",
        requester_id=requester,
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
        tenant_id=None,
    )
    return resolve_worker_target(
        "user_agent",
        "mind",
        execution_identity=identity,
        private_agent_names=frozenset({"mind"}),
    )


class _FakeVaultAPI:
    """Records POSTs and returns scripted responses keyed by path suffix."""

    def __init__(self, responses: dict[str, int]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []
        self.auth_headers: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content.decode()) if request.content else {}
        self.calls.append((path, body))
        self.auth_headers.append(request.headers.get("authorization", ""))
        for suffix, status in self.responses.items():
            if path.endswith(suffix):
                payload = {"name": body.get("name", "")} if status < 300 else {"error": "scripted"}
                return httpx.Response(status, json=payload)
        return httpx.Response(500, json={"error": "unexpected path"})


def _patch_client(monkeypatch: pytest.MonkeyPatch, api: _FakeVaultAPI) -> None:
    transport = httpx.MockTransport(api.handler)
    real_async_client = httpx.AsyncClient

    def factory(**kwargs: object) -> httpx.AsyncClient:
        kwargs.pop("timeout", None)
        return real_async_client(transport=transport)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def test_tool_requires_configuration(tmp_path: Path) -> None:
    """Missing required config must fail tool construction loudly."""
    with pytest.raises(_AgentVaultAccessError, match="API_URL"):
        AgentVaultAccessTools(
            runtime_paths=_runtime_paths(tmp_path, env={}),
            worker_target=_worker_target(),
        )


def test_tool_registers_and_builds_via_metadata(tmp_path: Path) -> None:
    """The tool builds through the registry with worker-target injection."""
    tool = get_tool_by_name(
        "agent_vault_access",
        _runtime_paths(tmp_path),
        worker_target=_worker_target(),
    )
    assert isinstance(tool, AgentVaultAccessTools)
    assert [t.__name__ for t in tool.tools] == ["request_vault_access"]


@pytest.mark.asyncio
async def test_request_vault_access_grants_and_returns_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A first request resolves the vault, grants membership, and returns the link."""
    target = _worker_target()
    expected_vault = worker_id_for_key(target.worker_key, prefix="agent-vault")
    api = _FakeVaultAPI({"/v1/vaults": 201, "/join": 409, "/users": 201})
    _patch_client(monkeypatch, api)

    tool = AgentVaultAccessTools(runtime_paths=_runtime_paths(tmp_path), worker_target=target)
    payload = json.loads(await tool.request_vault_access())

    assert payload["status"] == "ok"
    assert payload["vault"] == expected_vault
    assert payload["email"] == "bas.nijholt@example.test"
    assert payload["access"] == "granted"
    assert payload["url"] == f"https://example.test/agent-vault/vaults/{expected_vault}"
    # The grant must target the resolved vault and the derived email.
    grant_calls = [body for path, body in api.calls if path.endswith("/users")]
    assert grant_calls == [{"email": "bas.nijholt@example.test", "role": "member"}]


@pytest.mark.asyncio
async def test_request_vault_access_is_idempotent_when_already_member(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-requesting when already a member reports success without error."""
    api = _FakeVaultAPI({"/v1/vaults": 409, "/join": 200, "/users": 409})
    _patch_client(monkeypatch, api)

    tool = AgentVaultAccessTools(runtime_paths=_runtime_paths(tmp_path), worker_target=_worker_target())
    payload = json.loads(await tool.request_vault_access())

    assert payload["status"] == "ok"
    assert payload["access"] == "already had access"


@pytest.mark.asyncio
async def test_request_vault_access_joins_worker_created_vault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vaults created by the worker mint flow need an owner join before the grant.

    Granting membership requires vault-admin on that vault and instance owners
    are not vault admins implicitly, so the tool must POST /join (which grants
    the owner actor vault-admin) before POSTing the member grant.
    """
    target = _worker_target()
    expected_vault = worker_id_for_key(target.worker_key, prefix="agent-vault")
    # Pre-existing vault (409 on create), not yet joined (200 on join).
    api = _FakeVaultAPI({"/v1/vaults": 409, "/join": 200, "/users": 201})
    _patch_client(monkeypatch, api)

    tool = AgentVaultAccessTools(runtime_paths=_runtime_paths(tmp_path), worker_target=target)
    payload = json.loads(await tool.request_vault_access())

    assert payload["status"] == "ok"
    assert payload["access"] == "granted"
    paths = [path for path, _ in api.calls]
    join_path = f"/v1/vaults/{expected_vault}/join"
    grant_path = f"/v1/vaults/{expected_vault}/users"
    assert join_path in paths
    assert paths.index(join_path) < paths.index(grant_path)


@pytest.mark.asyncio
async def test_request_vault_access_reports_non_owner_token_on_join(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-owner token cannot use the owner-only /join; say so instead of a bare 403."""
    api = _FakeVaultAPI({"/v1/vaults": 409, "/join": 403, "/users": 201})
    _patch_client(monkeypatch, api)

    tool = AgentVaultAccessTools(runtime_paths=_runtime_paths(tmp_path), worker_target=_worker_target())
    payload = json.loads(await tool.request_vault_access())

    assert payload["status"] == "error"
    assert "owner-only" in payload["error"]
    # The grant must not be attempted after the failed join.
    assert not [path for path, _ in api.calls if path.endswith("/users")]


@pytest.mark.asyncio
async def test_request_vault_access_reports_unregistered_account(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unregistered account yields a clean error, not a traceback."""
    api = _FakeVaultAPI({"/v1/vaults": 201, "/join": 409, "/users": 404})
    _patch_client(monkeypatch, api)

    tool = AgentVaultAccessTools(runtime_paths=_runtime_paths(tmp_path), worker_target=_worker_target())
    payload = json.loads(await tool.request_vault_access())

    assert payload["status"] == "error"
    assert "does not have an Agent Vault account" in payload["error"]


@pytest.mark.asyncio
async def test_request_vault_access_without_worker_identity(tmp_path: Path) -> None:
    """Agents without a worker identity have no vault to grant."""
    tool = AgentVaultAccessTools(runtime_paths=_runtime_paths(tmp_path), worker_target=None)
    payload = json.loads(await tool.request_vault_access())
    assert payload["status"] == "error"
    assert "no dedicated vault" in payload["error"]


@pytest.mark.asyncio
async def test_request_vault_access_without_requester(tmp_path: Path) -> None:
    """A missing requester cannot be mapped to an account."""
    tool = AgentVaultAccessTools(
        runtime_paths=_runtime_paths(tmp_path),
        worker_target=_worker_target(requester=None),
    )
    payload = json.loads(await tool.request_vault_access())
    assert payload["status"] == "error"


@pytest.mark.asyncio
async def test_admin_token_file_is_reread_per_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rotated token file takes effect on the next call without a restart."""
    token_file = tmp_path / "token"
    token_file.write_text("first-token\n", encoding="utf-8")
    env = {k: v for k, v in _ENV.items() if k != "MINDROOM_AGENT_VAULT_ACCESS_ADMIN_TOKEN"}
    env["MINDROOM_AGENT_VAULT_ACCESS_ADMIN_TOKEN_FILE"] = str(token_file)
    api = _FakeVaultAPI({"/v1/vaults": 201, "/join": 409, "/users": 201})
    _patch_client(monkeypatch, api)

    tool = AgentVaultAccessTools(runtime_paths=_runtime_paths(tmp_path, env=env), worker_target=_worker_target())
    assert json.loads(await tool.request_vault_access())["status"] == "ok"
    token_file.write_text("second-token\n", encoding="utf-8")
    assert json.loads(await tool.request_vault_access())["status"] == "ok"

    assert "Bearer first-token" in api.auth_headers
    assert "Bearer second-token" in api.auth_headers


@pytest.mark.asyncio
async def test_admin_token_file_missing_is_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreadable token file fails the call with a clear error, not a crash."""
    env = {k: v for k, v in _ENV.items() if k != "MINDROOM_AGENT_VAULT_ACCESS_ADMIN_TOKEN"}
    env["MINDROOM_AGENT_VAULT_ACCESS_ADMIN_TOKEN_FILE"] = str(tmp_path / "missing-token")
    _patch_client(monkeypatch, _FakeVaultAPI({"/v1/vaults": 201, "/join": 409, "/users": 201}))

    tool = AgentVaultAccessTools(runtime_paths=_runtime_paths(tmp_path, env=env), worker_target=_worker_target())
    payload = json.loads(await tool.request_vault_access())

    assert payload["status"] == "error"
    assert "admin token file" in payload["error"]


def test_tool_requires_some_admin_token(tmp_path: Path) -> None:
    """Construction fails when neither the inline token nor the token file is set."""
    env = {k: v for k, v in _ENV.items() if k != "MINDROOM_AGENT_VAULT_ACCESS_ADMIN_TOKEN"}
    with pytest.raises(_AgentVaultAccessError, match="ADMIN_TOKEN"):
        AgentVaultAccessTools(runtime_paths=_runtime_paths(tmp_path, env=env), worker_target=_worker_target())
