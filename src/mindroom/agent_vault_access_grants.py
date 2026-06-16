"""Declarative Agent Vault UI/admin access grants.

This module is intentionally deployment-neutral.
Kubernetes charts and other operators can feed it structured grant config,
while worker identity and vault-name derivation stay delegated to the same
worker-routing helpers used by runtime tool execution.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NoReturn, cast
from urllib.parse import quote, urljoin

import httpx
import yaml

from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    WorkerScope,
    resolve_worker_target,
    worker_id_for_key,
)

_DEFAULT_VAULT_NAME_PREFIX = "agent-vault"
_HTTP_TIMEOUT_SECONDS = 15.0
_SUPPORTED_WORKER_SCOPES = frozenset({"shared", "user", "user_agent"})
_SUPPORTED_ROLES = frozenset({"admin"})
_CONFIG_PATH = "/etc/mindroom-agent-vault-access-grants/access-grants.yaml"

__all__ = [
    "AgentVaultAccessGrant",
    "AgentVaultAccessGrantApplyResult",
    "AgentVaultAccessGrantError",
    "AgentVaultAccessGrantsConfig",
    "ResolvedAgentVaultAccessGrantTarget",
    "apply_agent_vault_access_grants",
    "main",
    "resolve_agent_vault_access_grant_targets",
    "wait_for_agent_vault_ready",
]

_GrantRole = Literal["admin"]
_GrantOutcome = Literal["granted", "already_admin", "missing_account"]

if TYPE_CHECKING:
    from collections.abc import Sequence


class AgentVaultAccessGrantError(RuntimeError):
    """Raised when access-grant configuration or application fails."""


@dataclass(frozen=True)
class AgentVaultAccessGrant:
    """One declarative Agent Vault access grant."""

    email: str
    worker_scope: WorkerScope
    role: _GrantRole
    requester: str | None = None
    agent: str | None = None


@dataclass(frozen=True)
class AgentVaultAccessGrantsConfig:
    """Validated Agent Vault access-grant config."""

    api_url: str
    grants: tuple[AgentVaultAccessGrant, ...]
    admin_token: str | None = None
    admin_token_file: str | None = None
    vault_name_prefix: str = _DEFAULT_VAULT_NAME_PREFIX
    tenant_id: str | None = None
    account_id: str | None = None

    @classmethod
    def from_file(cls, path: Path) -> AgentVaultAccessGrantsConfig:
        """Load and validate access grants from a YAML file."""
        try:
            raw_config = path.read_text(encoding="utf-8")
        except OSError as exc:
            msg = f"could not read Agent Vault access grants config {str(path)!r}: {exc}"
            raise AgentVaultAccessGrantError(msg) from exc
        try:
            payload = yaml.safe_load(raw_config)
        except yaml.YAMLError as exc:
            msg = f"could not parse Agent Vault access grants config {str(path)!r}: {exc}"
            raise AgentVaultAccessGrantError(msg) from exc
        return cls.from_payload(payload, source=str(path))

    @classmethod
    def from_payload(
        cls,
        payload: object,
        *,
        source: str = "Agent Vault access grants config",
    ) -> AgentVaultAccessGrantsConfig:
        """Validate one decoded access-grants payload."""
        if not isinstance(payload, dict):
            msg = f"{source} must be a YAML object"
            raise AgentVaultAccessGrantError(msg)

        raw_payload = cast("dict[str, object]", payload)
        api_url = _required_string(raw_payload, "apiUrl", "api_url", label="apiUrl")
        raw_grants = raw_payload.get("grants", [])
        if raw_grants is None:
            raw_grants = []
        if not isinstance(raw_grants, list):
            msg = "grants must be a list"
            raise AgentVaultAccessGrantError(msg)

        grants = tuple(_parse_grant(item, index) for index, item in enumerate(raw_grants))
        vault_name_prefix = _optional_string(
            raw_payload,
            "vaultNamePrefix",
            "vault_name_prefix",
            label="vaultNamePrefix",
        )
        return cls(
            api_url=api_url,
            admin_token=_optional_string(raw_payload, "adminToken", "admin_token", label="adminToken"),
            admin_token_file=_optional_string(
                raw_payload,
                "adminTokenFile",
                "admin_token_file",
                label="adminTokenFile",
            ),
            vault_name_prefix=vault_name_prefix or _DEFAULT_VAULT_NAME_PREFIX,
            tenant_id=_optional_string(raw_payload, "tenantId", "tenant_id", label="tenantId"),
            account_id=_optional_string(raw_payload, "accountId", "account_id", label="accountId"),
            grants=grants,
        )


@dataclass(frozen=True)
class ResolvedAgentVaultAccessGrantTarget:
    """One grant resolved to the worker key and vault name Agent Vault uses."""

    grant: AgentVaultAccessGrant
    worker_key: str
    vault: str


@dataclass(frozen=True)
class AgentVaultAccessGrantApplyResult:
    """Summary of one access-grant application run."""

    applied: int
    already_admin: int
    warnings: tuple[str, ...]


def resolve_agent_vault_access_grant_targets(
    config: AgentVaultAccessGrantsConfig,
) -> tuple[ResolvedAgentVaultAccessGrantTarget, ...]:
    """Resolve configured grants to worker keys and Agent Vault vault names."""
    return tuple(_resolve_grant_target(config, grant) for grant in config.grants)


async def apply_agent_vault_access_grants(
    config: AgentVaultAccessGrantsConfig,
    *,
    admin_token: str | None = None,
    admin_token_file: str | None = None,
) -> AgentVaultAccessGrantApplyResult:
    """Apply declarative Agent Vault access grants idempotently."""
    resolved_token = _resolve_admin_token(config, admin_token=admin_token, admin_token_file=admin_token_file)
    client = _AgentVaultClient(config.api_url, resolved_token)
    applied = 0
    already_admin = 0
    warnings: list[str] = []

    for target in resolve_agent_vault_access_grant_targets(config):
        await client.ensure_vault(target.vault)
        await client.ensure_vault_admin(target.vault)
        outcome = await client.grant_admin(target.vault, target.grant.email)
        if outcome == "granted":
            applied += 1
        elif outcome == "already_admin":
            already_admin += 1
        else:
            warnings.append(
                f"{target.grant.email} does not have an Agent Vault account yet; "
                "ask them to register and verify before rerunning this grant.",
            )

    return AgentVaultAccessGrantApplyResult(
        applied=applied,
        already_admin=already_admin,
        warnings=tuple(warnings),
    )


async def wait_for_agent_vault_ready(api_url: str, *, timeout_seconds: int) -> None:
    """Wait until the Agent Vault health endpoint is ready."""
    if timeout_seconds <= 0:
        return

    deadline = time.monotonic() + timeout_seconds
    last_error: str | None = None
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
        while time.monotonic() < deadline:
            try:
                response = await client.get(urljoin(api_url.rstrip("/") + "/", "health"))
            except httpx.HTTPError as exc:
                last_error = str(exc)
            else:
                if response.status_code < 500:
                    return
                last_error = f"HTTP {response.status_code}"
            await asyncio.sleep(2)

    detail = f": {last_error}" if last_error else ""
    msg = f"Agent Vault did not become ready within {timeout_seconds}s{detail}"
    raise AgentVaultAccessGrantError(msg)


class _AgentVaultClient:
    """Small Agent Vault admin API client for idempotent grants."""

    def __init__(self, api_url: str, token: str) -> None:
        self._api_url = api_url
        self._token = token

    async def ensure_vault(self, vault: str) -> None:
        response = await self._post("v1/vaults", {"name": vault})
        if response.status_code in {200, 201, 409, 422}:
            return
        response.raise_for_status()

    async def ensure_vault_admin(self, vault: str) -> None:
        response = await self._post(f"v1/vaults/{quote(vault, safe='')}/join")
        if response.status_code in {200, 201, 409}:
            return
        if response.status_code == 403:
            msg = (
                "the Agent Vault admin token cannot join this vault: /join is owner-only, "
                "so the token must belong to an instance-owner agent or session."
            )
            raise AgentVaultAccessGrantError(msg)
        response.raise_for_status()

    async def grant_admin(self, vault: str, email: str) -> _GrantOutcome:
        response = await self._post(
            f"v1/vaults/{quote(vault, safe='')}/users",
            {"email": email, "role": "admin"},
        )
        if response.status_code in {200, 201}:
            return "granted"
        if response.status_code in {409, 422}:
            await self._set_admin_role(vault, email)
            return "already_admin"
        if response.status_code == 404:
            return "missing_account"
        return self._raise_api_error("granting vault admin access", response)

    async def _set_admin_role(self, vault: str, email: str) -> None:
        response = await self._post(
            f"v1/vaults/{quote(vault, safe='')}/users/{quote(email, safe='')}/role",
            {"role": "admin"},
        )
        if response.status_code in {200, 201, 204}:
            return
        self._raise_api_error("updating vault user role to admin", response)

    async def _post(self, path: str, payload: dict[str, object] | None = None) -> httpx.Response:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            return await client.post(
                urljoin(self._api_url.rstrip("/") + "/", path),
                headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"},
                json=payload,
            )

    def _raise_api_error(self, action: str, response: httpx.Response) -> NoReturn:
        detail = f"Agent Vault API returned {response.status_code} while {action}"
        body = response.text.strip()
        if body:
            detail = f"{detail}: {body}"
        raise AgentVaultAccessGrantError(detail)


def _resolve_grant_target(
    config: AgentVaultAccessGrantsConfig,
    grant: AgentVaultAccessGrant,
) -> ResolvedAgentVaultAccessGrantTarget:
    agent_name = grant.agent or "default"
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name=agent_name,
        requester_id=grant.requester,
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
        tenant_id=config.tenant_id,
        account_id=config.account_id,
    )
    target = resolve_worker_target(
        grant.worker_scope,
        grant.agent,
        execution_identity=identity,
        tenant_id=config.tenant_id,
        account_id=config.account_id,
    )
    if target.worker_key is None:
        msg = f"could not resolve worker key for {grant.worker_scope} grant to {grant.email}"
        raise AgentVaultAccessGrantError(msg)
    return ResolvedAgentVaultAccessGrantTarget(
        grant=grant,
        worker_key=target.worker_key,
        vault=worker_id_for_key(target.worker_key, prefix=config.vault_name_prefix),
    )


def _parse_grant(payload: object, index: int) -> AgentVaultAccessGrant:
    label = f"grants[{index}]"
    if not isinstance(payload, dict):
        msg = f"{label} must be an object"
        raise AgentVaultAccessGrantError(msg)
    raw_payload = cast("dict[str, object]", payload)
    email = _required_string(raw_payload, "email", label=f"{label}.email")
    worker_scope = _parse_worker_scope(
        _required_string(raw_payload, "workerScope", "worker_scope", label=f"{label}.workerScope"),
        label=f"{label}.workerScope",
    )
    role = _parse_role(
        _optional_string(raw_payload, "role", label=f"{label}.role") or "admin",
        label=f"{label}.role",
    )
    requester = _optional_string(raw_payload, "requester", label=f"{label}.requester")
    agent = _optional_string(raw_payload, "agent", label=f"{label}.agent")

    if worker_scope == "shared":
        if requester is not None:
            msg = "shared grants must not set requester"
            raise AgentVaultAccessGrantError(msg)
        if agent is None:
            msg = "shared grants require agent"
            raise AgentVaultAccessGrantError(msg)
    elif worker_scope == "user":
        if requester is None:
            msg = "user grants require requester"
            raise AgentVaultAccessGrantError(msg)
    elif worker_scope == "user_agent":
        if requester is None:
            msg = "user_agent grants require requester"
            raise AgentVaultAccessGrantError(msg)
        if agent is None:
            msg = "user_agent grants require agent"
            raise AgentVaultAccessGrantError(msg)

    return AgentVaultAccessGrant(
        email=email,
        worker_scope=worker_scope,
        role=role,
        requester=requester,
        agent=agent,
    )


def _parse_worker_scope(value: str, *, label: str) -> WorkerScope:
    if value not in _SUPPORTED_WORKER_SCOPES:
        msg = f"{label} must be one of: shared, user, user_agent"
        raise AgentVaultAccessGrantError(msg)
    return cast("WorkerScope", value)


def _parse_role(value: str, *, label: str) -> _GrantRole:
    if value not in _SUPPORTED_ROLES:
        msg = f"{label} must be admin"
        raise AgentVaultAccessGrantError(msg)
    return cast("_GrantRole", value)


def _required_string(payload: dict[str, object], *keys: str, label: str) -> str:
    value = _optional_string(payload, *keys, label=label)
    if value is None:
        msg = f"{label} is required"
        raise AgentVaultAccessGrantError(msg)
    return value


def _optional_string(payload: dict[str, object], *keys: str, label: str) -> str | None:
    for key in keys:
        if key not in payload:
            continue
        value = payload[key]
        if value is None:
            return None
        if not isinstance(value, str):
            msg = f"{label} must be a string"
            raise AgentVaultAccessGrantError(msg)
        stripped = value.strip()
        return stripped or None
    return None


def _resolve_admin_token(
    config: AgentVaultAccessGrantsConfig,
    *,
    admin_token: str | None,
    admin_token_file: str | None,
) -> str:
    token_file = admin_token_file or config.admin_token_file
    if token_file:
        try:
            token = Path(token_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            msg = f"could not read the Agent Vault admin token file {token_file!r}: {exc}"
            raise AgentVaultAccessGrantError(msg) from exc
        if not token:
            msg = f"the Agent Vault admin token file {token_file!r} is empty"
            raise AgentVaultAccessGrantError(msg)
        return token

    token = (admin_token or config.admin_token or "").strip()
    if not token:
        msg = "Agent Vault access grants require adminToken or adminTokenFile"
        raise AgentVaultAccessGrantError(msg)
    return token


async def _apply_from_args(args: argparse.Namespace) -> AgentVaultAccessGrantApplyResult:
    config = AgentVaultAccessGrantsConfig.from_file(args.config)
    await wait_for_agent_vault_ready(config.api_url, timeout_seconds=args.wait_ready_seconds)
    return await apply_agent_vault_access_grants(
        config,
        admin_token=args.admin_token,
        admin_token_file=args.admin_token_file,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m mindroom.agent_vault_access_grants")
    subparsers = parser.add_subparsers(dest="command", required=True)

    apply_parser = subparsers.add_parser("apply", help="Apply Agent Vault access grants")
    apply_parser.add_argument(
        "--config",
        type=Path,
        default=Path(_CONFIG_PATH),
        help="YAML access-grants config path",
    )
    apply_parser.add_argument(
        "--admin-token-file",
        default=None,
        help="File containing an Agent Vault owner/admin token",
    )
    apply_parser.add_argument(
        "--admin-token",
        default=None,
        help="Agent Vault owner/admin token. Prefer --admin-token-file in deployments.",
    )
    apply_parser.add_argument(
        "--wait-ready-seconds",
        type=int,
        default=0,
        help="Wait for Agent Vault /health before applying grants",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for Agent Vault access grants."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command != "apply":
        parser.error("unknown command")

    try:
        result = asyncio.run(_apply_from_args(args))
    except AgentVaultAccessGrantError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except httpx.HTTPError as exc:
        print(f"error: Agent Vault API request failed: {exc}", file=sys.stderr)
        return 1

    for warning in result.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    print(
        "Agent Vault access grants applied: "
        f"granted={result.applied} already_admin={result.already_admin} warnings={len(result.warnings)}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
