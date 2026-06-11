"""Agent Vault self-service access tool for MindRoom agents.

Lets a user ask their own agent for a link to manage that agent's Agent
Vault secrets. MindRoom resolves the caller's worker target to the vault that
backs that worker (the deterministic per-worker vault name), grants the
caller's Agent Vault account membership of that vault, and returns the gated
UI link.

This grants *UI management access* only. The runtime secret boundary is still
the per-worker vault scope plus the in-pod proxy-role token: that token can
exercise a credential but cannot read it, and membership here never changes
which worker reaches which vault.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote, urljoin

import httpx
from agno.tools import Toolkit

from mindroom.runtime_env_policy import AGENT_VAULT_ACCESS_ENV_BY_KEY
from mindroom.tool_system.worker_routing import worker_id_for_key

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

_DEFAULT_VAULT_NAME_PREFIX = "agent-vault"
_HTTP_TIMEOUT_SECONDS = 15.0


class _AgentVaultAccessError(RuntimeError):
    """Raised when the tool cannot be constructed from the runtime configuration."""


class AgentVaultAccessTools(Toolkit):
    """Tool that grants the caller UI access to their agent's vault."""

    def __init__(
        self,
        *,
        runtime_paths: RuntimePaths,
        worker_target: ResolvedWorkerTarget | None = None,
    ) -> None:
        env = AGENT_VAULT_ACCESS_ENV_BY_KEY
        self._api_url = (runtime_paths.env_value(env["api_url"]) or "").strip()
        self._admin_token = (runtime_paths.env_value(env["admin_token"]) or "").strip()
        self._admin_token_file = (runtime_paths.env_value(env["admin_token_file"]) or "").strip()
        self._ui_base_url = (runtime_paths.env_value(env["ui_base_url"]) or "").strip()
        self._email_domain = (runtime_paths.env_value(env["email_domain"]) or "").strip().lstrip("@")
        self._vault_name_prefix = (
            runtime_paths.env_value(env["vault_name_prefix"]) or _DEFAULT_VAULT_NAME_PREFIX
        ).strip()
        missing = [
            name
            for name, value in (
                (env["api_url"], self._api_url),
                (env["ui_base_url"], self._ui_base_url),
                (env["email_domain"], self._email_domain),
            )
            if not value
        ]
        if not self._admin_token and not self._admin_token_file:
            missing.append(f"{env['admin_token']} or {env['admin_token_file']}")
        if missing:
            msg = f"AgentVaultAccessTools requires these environment values: {', '.join(sorted(missing))}"
            raise _AgentVaultAccessError(msg)
        self._worker_target = worker_target
        super().__init__(name="agent_vault_access", tools=[self.request_vault_access])

    async def request_vault_access(self) -> str:
        """Grant yourself access to manage this agent's Agent Vault secrets and return a link.

        Resolves the vault that backs your worker identity for this agent,
        grants your Agent Vault account membership, and returns the UI link
        where you can add or update the secrets this agent's tools will use.
        """
        target = self._worker_target
        if target is None or not target.worker_key:
            return self._error(
                "no worker identity is available for this agent, so it has no dedicated vault. "
                "Agent Vault access requires a worker-scoped agent.",
            )
        identity = target.execution_identity
        requester_id = identity.requester_id if identity is not None else None
        if not requester_id:
            return self._error("could not determine who is asking; Agent Vault access needs a known requester.")

        email = self._requester_email(requester_id)
        if email is None:
            return self._error(
                f"could not derive an email for requester {requester_id!r}; "
                "expected a Matrix ID whose localpart maps to the configured email domain.",
            )

        vault = worker_id_for_key(target.worker_key, prefix=self._vault_name_prefix)
        try:
            # One token read per request: both API calls must use the same
            # token, or a rotation between them could 401 the second call.
            token = self._resolve_admin_token()
            await self._ensure_vault(vault, token)
            await self._ensure_vault_admin(vault, token)
            granted = await self._grant_member(vault, email, token)
        except _AgentVaultAccessError as exc:
            return self._error(str(exc))
        except httpx.HTTPError as exc:
            return self._error(f"Agent Vault API request failed: {exc}")

        link = urljoin(self._ui_base_url.rstrip("/") + "/", f"vaults/{quote(vault, safe='')}")
        status = "granted" if granted else "already had access"
        return json.dumps(
            {
                "tool": "agent_vault_access",
                "status": "ok",
                "vault": vault,
                "email": email,
                "access": status,
                "url": link,
                "note": (
                    "Open the link, log in through the usual SSO gate, and manage this agent's secrets there. "
                    "Anyone you grant can read those secrets, so only add what this agent needs."
                ),
            },
            sort_keys=True,
        )

    def _requester_email(self, requester_id: str) -> str | None:
        # Matrix IDs look like @localpart:server; map localpart to the configured domain.
        localpart = requester_id[1:].split(":", 1)[0] if requester_id.startswith("@") else requester_id
        localpart = localpart.strip()
        if not localpart:
            return None
        if "@" in localpart:
            # Already an email-like value; trust it as-is.
            return localpart
        return f"{localpart}@{self._email_domain}"

    def _resolve_admin_token(self) -> str:
        # Re-read the token file on every call so a rotated Secret (refreshed
        # in place by the kubelet) takes effect without a process restart.
        if self._admin_token_file:
            try:
                token = Path(self._admin_token_file).read_text(encoding="utf-8").strip()
            except OSError as exc:
                msg = f"could not read the Agent Vault admin token file {self._admin_token_file!r}: {exc}"
                raise _AgentVaultAccessError(msg) from exc
            if not token:
                msg = f"the Agent Vault admin token file {self._admin_token_file!r} is empty."
                raise _AgentVaultAccessError(msg)
            return token
        return self._admin_token

    def _headers(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    async def _ensure_vault(self, vault: str, token: str) -> None:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            response = await client.post(
                urljoin(self._api_url.rstrip("/") + "/", "v1/vaults"),
                headers=self._headers(token),
                json={"name": vault},
            )
        # 409/422 mean the vault already exists, which is fine for an idempotent grant.
        if response.status_code in {200, 201, 409, 422}:
            return
        response.raise_for_status()

    async def _ensure_vault_admin(self, vault: str, token: str) -> None:
        # Granting membership requires vault-admin on that specific vault, and
        # instance owners are not vault admins implicitly. Vaults created by
        # the worker token-mint flow do not include this admin actor, so join
        # (owner-only, grants vault-admin) before granting.
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            response = await client.post(
                urljoin(self._api_url.rstrip("/") + "/", f"v1/vaults/{quote(vault, safe='')}/join"),
                headers=self._headers(token),
            )
        # 409 means this actor is already a vault member, fine for an idempotent grant.
        if response.status_code in {200, 201, 409}:
            return
        if response.status_code == 403:
            msg = (
                "the configured Agent Vault admin token cannot join this vault: "
                "/join is owner-only, so the token must belong to an instance-owner "
                "agent or session, not a plain admin/member one."
            )
            raise _AgentVaultAccessError(msg)
        response.raise_for_status()

    async def _grant_member(self, vault: str, email: str, token: str) -> bool:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            response = await client.post(
                urljoin(self._api_url.rstrip("/") + "/", f"v1/vaults/{quote(vault, safe='')}/users"),
                headers=self._headers(token),
                json={"email": email, "role": "member"},
            )
        if response.status_code in {200, 201}:
            return True
        if response.status_code in {409, 422}:
            # Already a member: idempotent success.
            return False
        if response.status_code == 404:
            msg = (
                f"{email} does not have an Agent Vault account yet. "
                "Register and verify at the vault UI first, then ask again."
            )
            raise _AgentVaultAccessError(msg)
        response.raise_for_status()
        return False

    def _error(self, detail: str) -> str:
        return json.dumps(
            {"tool": "agent_vault_access", "status": "error", "error": detail},
            sort_keys=True,
        )
