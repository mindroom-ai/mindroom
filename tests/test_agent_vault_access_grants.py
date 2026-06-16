"""Tests for declarative Agent Vault access grants."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx
import pytest
import yaml

from mindroom.agent_vault_access_grants import (
    AgentVaultAccessGrantError,
    AgentVaultAccessGrantsConfig,
    apply_agent_vault_access_grants,
    resolve_agent_vault_access_grant_targets,
)
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_target, worker_id_for_key

if TYPE_CHECKING:
    from pathlib import Path


class _FakeVaultAPI:
    """Records POSTs and returns scripted responses keyed by path suffix."""

    def __init__(self, responses: dict[str, int]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, object]]] = []
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


def _config_path(tmp_path: Path, data: dict[str, object]) -> Path:
    path = tmp_path / "agent-vault-access-grants.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_resolves_shared_grant_to_runtime_worker_vault(tmp_path: Path) -> None:
    """Shared grants use the same worker key and vault-name derivation as runtime routing."""
    path = _config_path(
        tmp_path,
        {
            "apiUrl": "http://agent-vault:14321",
            "adminToken": "owner-token",
            "vaultNamePrefix": "agent-vault",
            "tenantId": "tenant-42",
            "grants": [
                {
                    "email": "maintainer@example.test",
                    "workerScope": "shared",
                    "agent": "example-agent",
                    "role": "admin",
                },
            ],
        },
    )
    config = AgentVaultAccessGrantsConfig.from_file(path)

    target = resolve_agent_vault_access_grant_targets(config)[0]
    expected_worker_target = resolve_worker_target(
        "shared",
        "example-agent",
        execution_identity=ToolExecutionIdentity(
            channel="matrix",
            agent_name="example-agent",
            requester_id=None,
            room_id=None,
            thread_id=None,
            resolved_thread_id=None,
            session_id=None,
            tenant_id="tenant-42",
        ),
    )

    assert target.worker_key == expected_worker_target.worker_key
    assert target.vault == worker_id_for_key(expected_worker_target.worker_key, prefix="agent-vault")


def test_resolves_user_agent_grant_to_runtime_worker_vault(tmp_path: Path) -> None:
    """User-agent grants include both requester and agent in the worker identity."""
    path = _config_path(
        tmp_path,
        {
            "apiUrl": "http://agent-vault:14321",
            "adminToken": "owner-token",
            "vaultNamePrefix": "agent-vault",
            "grants": [
                {
                    "email": "user@example.test",
                    "workerScope": "user_agent",
                    "requester": "@user:example.test",
                    "agent": "example-agent",
                    "role": "admin",
                },
            ],
        },
    )
    config = AgentVaultAccessGrantsConfig.from_file(path)

    target = resolve_agent_vault_access_grant_targets(config)[0]
    expected_worker_target = resolve_worker_target(
        "user_agent",
        "example-agent",
        execution_identity=ToolExecutionIdentity(
            channel="matrix",
            agent_name="example-agent",
            requester_id="@user:example.test",
            room_id=None,
            thread_id=None,
            resolved_thread_id=None,
            session_id=None,
            tenant_id=None,
        ),
    )

    assert target.worker_key == expected_worker_target.worker_key
    assert target.vault == worker_id_for_key(expected_worker_target.worker_key, prefix="agent-vault")


@pytest.mark.asyncio
async def test_apply_grants_creates_joins_and_grants_admin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Applying a grant creates/joins the vault and adds the configured admin."""
    path = _config_path(
        tmp_path,
        {
            "apiUrl": "http://agent-vault:14321",
            "adminToken": "owner-token",
            "vaultNamePrefix": "agent-vault",
            "grants": [
                {
                    "email": "maintainer@example.test",
                    "workerScope": "shared",
                    "agent": "example-agent",
                    "role": "admin",
                },
            ],
        },
    )
    config = AgentVaultAccessGrantsConfig.from_file(path)
    target = resolve_agent_vault_access_grant_targets(config)[0]
    api = _FakeVaultAPI({"/v1/vaults": 201, "/join": 200, "/users": 201})
    _patch_client(monkeypatch, api)

    result = await apply_agent_vault_access_grants(config)

    assert result.applied == 1
    assert not result.warnings
    assert [path for path, _ in api.calls] == [
        "/v1/vaults",
        f"/v1/vaults/{target.vault}/join",
        f"/v1/vaults/{target.vault}/users",
    ]
    assert api.calls[-1][1] == {"email": "maintainer@example.test", "role": "admin"}
    assert api.auth_headers == ["Bearer owner-token", "Bearer owner-token", "Bearer owner-token"]


@pytest.mark.asyncio
async def test_apply_grants_warns_for_unregistered_account(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unregistered Agent Vault users are reported as warnings, not deploy failures."""
    path = _config_path(
        tmp_path,
        {
            "apiUrl": "http://agent-vault:14321",
            "adminToken": "owner-token",
            "grants": [
                {
                    "email": "missing@example.test",
                    "workerScope": "shared",
                    "agent": "example-agent",
                    "role": "admin",
                },
            ],
        },
    )
    api = _FakeVaultAPI({"/v1/vaults": 409, "/join": 409, "/users": 404})
    _patch_client(monkeypatch, api)

    result = await apply_agent_vault_access_grants(AgentVaultAccessGrantsConfig.from_file(path))

    assert result.applied == 0
    assert len(result.warnings) == 1
    assert "missing@example.test does not have an Agent Vault account" in result.warnings[0]


def test_config_rejects_shared_requester(tmp_path: Path) -> None:
    """Shared worker grants are not requester-specific."""
    path = _config_path(
        tmp_path,
        {
            "apiUrl": "http://agent-vault:14321",
            "adminToken": "owner-token",
            "grants": [
                {
                    "email": "maintainer@example.test",
                    "workerScope": "shared",
                    "requester": "@user:example.test",
                    "agent": "example-agent",
                    "role": "admin",
                },
            ],
        },
    )

    with pytest.raises(AgentVaultAccessGrantError, match="shared grants must not set requester"):
        AgentVaultAccessGrantsConfig.from_file(path)


def test_config_rejects_user_agent_without_requester(tmp_path: Path) -> None:
    """User-agent worker grants need the requester that contributes to the worker key."""
    path = _config_path(
        tmp_path,
        {
            "apiUrl": "http://agent-vault:14321",
            "adminToken": "owner-token",
            "grants": [
                {
                    "email": "user@example.test",
                    "workerScope": "user_agent",
                    "agent": "example-agent",
                    "role": "admin",
                },
            ],
        },
    )

    with pytest.raises(AgentVaultAccessGrantError, match="user_agent grants require requester"):
        AgentVaultAccessGrantsConfig.from_file(path)


def test_config_rejects_unsupported_role(tmp_path: Path) -> None:
    """The first implementation only supports Agent Vault admin grants."""
    path = _config_path(
        tmp_path,
        {
            "apiUrl": "http://agent-vault:14321",
            "adminToken": "owner-token",
            "grants": [
                {
                    "email": "user@example.test",
                    "workerScope": "user",
                    "requester": "@user:example.test",
                    "role": "viewer",
                },
            ],
        },
    )

    with pytest.raises(AgentVaultAccessGrantError, match="role must be admin"):
        AgentVaultAccessGrantsConfig.from_file(path)
