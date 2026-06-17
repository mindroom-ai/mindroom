"""Tests for declarative Agent Vault access grants."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx
import pytest
import yaml

from mindroom.agent_vault_access_grants import (
    AgentVaultAccessGrantApplyResult,
    AgentVaultAccessGrantError,
    AgentVaultAccessGrantsConfig,
    apply_agent_vault_access_grants,
    resolve_agent_vault_access_grant_targets,
    wait_for_agent_vault_ready,
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
        self.content_type_headers: list[str | None] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content.decode()) if request.content else {}
        self.calls.append((path, body))
        self.auth_headers.append(request.headers.get("authorization", ""))
        self.content_type_headers.append(request.headers.get("content-type"))
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

    assert result == AgentVaultAccessGrantApplyResult(applied=1, warnings=())
    assert [path for path, _ in api.calls] == [
        "/v1/vaults",
        f"/v1/vaults/{target.vault}/join",
        f"/v1/vaults/{target.vault}/users",
    ]
    assert api.calls[-1][1] == {"email": "maintainer@example.test", "role": "admin"}
    assert api.auth_headers == ["Bearer owner-token", "Bearer owner-token", "Bearer owner-token"]
    assert api.content_type_headers == ["application/json", None, "application/json"]


@pytest.mark.asyncio
async def test_apply_grants_counts_existing_member_role_updates_as_applied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Existing members are still counted as applied when their role is ensured as admin."""
    path = _config_path(
        tmp_path,
        {
            "apiUrl": "http://agent-vault:14321",
            "adminToken": "owner-token",
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
    api = _FakeVaultAPI({"/v1/vaults": 409, "/join": 409, "/users": 409, "/role": 200})
    _patch_client(monkeypatch, api)

    result = await apply_agent_vault_access_grants(config)

    assert result.applied == 1
    assert [path for path, _ in api.calls] == [
        "/v1/vaults",
        f"/v1/vaults/{target.vault}/join",
        f"/v1/vaults/{target.vault}/users",
        f"/v1/vaults/{target.vault}/users/maintainer@example.test/role",
    ]


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


@pytest.mark.asyncio
async def test_apply_grants_uses_config_admin_token_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AdminTokenFile from config is preferred over inline config tokens."""
    token_file = tmp_path / "admin-token"
    token_file.write_text("file-token\n", encoding="utf-8")
    path = _config_path(
        tmp_path,
        {
            "apiUrl": "http://agent-vault:14321",
            "adminToken": "ignored-token",
            "adminTokenFile": str(token_file),
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
    api = _FakeVaultAPI({"/v1/vaults": 201, "/join": 200, "/users": 201})
    _patch_client(monkeypatch, api)

    await apply_agent_vault_access_grants(AgentVaultAccessGrantsConfig.from_file(path))

    assert api.auth_headers == ["Bearer file-token", "Bearer file-token", "Bearer file-token"]


@pytest.mark.asyncio
async def test_apply_grants_explicit_admin_token_file_overrides_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI-provided admin token file wins over the config token file."""
    config_token_file = tmp_path / "config-admin-token"
    config_token_file.write_text("config-token\n", encoding="utf-8")
    override_token_file = tmp_path / "override-admin-token"
    override_token_file.write_text("override-token\n", encoding="utf-8")
    path = _config_path(
        tmp_path,
        {
            "apiUrl": "http://agent-vault:14321",
            "adminTokenFile": str(config_token_file),
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
    api = _FakeVaultAPI({"/v1/vaults": 201, "/join": 200, "/users": 201})
    _patch_client(monkeypatch, api)

    await apply_agent_vault_access_grants(
        AgentVaultAccessGrantsConfig.from_file(path),
        admin_token_file=str(override_token_file),
    )

    assert api.auth_headers == ["Bearer override-token", "Bearer override-token", "Bearer override-token"]


@pytest.mark.asyncio
async def test_apply_grants_rejects_missing_admin_token_file(tmp_path: Path) -> None:
    """Missing adminTokenFile fails before any Agent Vault API call."""
    config = AgentVaultAccessGrantsConfig.from_payload(
        {
            "apiUrl": "http://agent-vault:14321",
            "adminTokenFile": str(tmp_path / "missing-token"),
            "grants": [],
        },
    )

    with pytest.raises(AgentVaultAccessGrantError, match="could not read adminTokenFile"):
        await apply_agent_vault_access_grants(config)


@pytest.mark.asyncio
async def test_apply_grants_rejects_empty_admin_token_file(tmp_path: Path) -> None:
    """Empty adminTokenFile fails before any Agent Vault API call."""
    token_file = tmp_path / "empty-token"
    token_file.write_text("", encoding="utf-8")
    config = AgentVaultAccessGrantsConfig.from_payload(
        {
            "apiUrl": "http://agent-vault:14321",
            "adminTokenFile": str(token_file),
            "grants": [],
        },
    )

    with pytest.raises(AgentVaultAccessGrantError, match=r"adminTokenFile .* is empty"):
        await apply_agent_vault_access_grants(config)


@pytest.mark.asyncio
async def test_apply_grants_requires_admin_token_or_file() -> None:
    """Access grants require some owner/admin token source."""
    config = AgentVaultAccessGrantsConfig.from_payload(
        {
            "apiUrl": "http://agent-vault:14321",
            "grants": [],
        },
    )

    with pytest.raises(AgentVaultAccessGrantError, match="adminToken or adminTokenFile"):
        await apply_agent_vault_access_grants(config)


@pytest.mark.asyncio
async def test_wait_for_agent_vault_ready_accepts_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 2xx health response marks Agent Vault as ready."""
    api = _FakeVaultAPI({"/health": 204})
    _patch_client(monkeypatch, api)

    await wait_for_agent_vault_ready("http://agent-vault:14321", timeout_seconds=1)

    assert [path for path, _ in api.calls] == ["/health"]


@pytest.mark.asyncio
async def test_wait_for_agent_vault_ready_rejects_client_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 4xx health response should not be treated as readiness."""
    api = _FakeVaultAPI({"/health": 404})
    _patch_client(monkeypatch, api)

    async def sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("mindroom.agent_vault_access_grants.asyncio.sleep", sleep)

    with pytest.raises(AgentVaultAccessGrantError, match="HTTP 404"):
        await wait_for_agent_vault_ready("http://agent-vault:14321", timeout_seconds=0.001)


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


def test_config_rejects_user_grant_with_agent(tmp_path: Path) -> None:
    """User worker grants must not accept an agent that is ignored by worker-key resolution."""
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
                    "agent": "example-agent",
                    "role": "admin",
                },
            ],
        },
    )

    with pytest.raises(AgentVaultAccessGrantError, match="user grants must not set agent"):
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
