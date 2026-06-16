"""Agent Vault self-service access tool for MindRoom agents.

Lets a user ask their own agent for a link to manage that agent's Agent
Vault secrets. MindRoom resolves the caller's worker target to the vault that
backs that worker (the deterministic per-worker vault name), grants the
caller's Agent Vault account admin access to that vault, and returns the gated
UI link.

This grants *UI management access* only. The runtime secret boundary is still
the per-worker vault scope plus the in-pod proxy-role token: that token can
exercise a credential but cannot read it, and UI admin access here never changes
which worker reaches which vault.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn
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
        self._owner_email = (runtime_paths.env_value(env["owner_email"]) or "").strip()
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
        grants your Agent Vault account admin access, and returns the UI link
        where you can add or update the secrets this agent's tools will use.
        """
        target = self._worker_target
        if target_error := self._target_error(target):
            return self._error(target_error)
        assert target is not None  # Narrowed by _target_error.
        worker_key = target.worker_key
        assert worker_key is not None  # Narrowed by _target_error.
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

        vault = worker_id_for_key(worker_key, prefix=self._vault_name_prefix)
        try:
            # One token read per request: both API calls must use the same
            # token, or a rotation between them could 401 the second call.
            token = self._resolve_admin_token()
            await self._ensure_vault(vault, token)
            await self._ensure_vault_admin(vault, token)
            # Worker init logs in as the configured owner account to mint the
            # proxy token, so keep that account admin on self-service vaults too.
            if self._owner_email and self._owner_email.casefold() != email.casefold():
                await self._grant_admin(
                    vault,
                    self._owner_email,
                    token,
                    missing_account_message=(
                        "Agent Vault access is not ready: the configured worker token-mint owner account "
                        "could not be kept as vault admin. Ask an operator to verify "
                        f"{AGENT_VAULT_ACCESS_ENV_BY_KEY['owner_email']} is registered in Agent Vault."
                    ),
                )
            granted = await self._grant_admin(vault, email, token)
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

    def _target_error(self, target: ResolvedWorkerTarget | None) -> str | None:
        if target is None or not target.worker_key:
            return (
                "no worker identity is available for this agent, so it has no dedicated vault. "
                "Agent Vault access requires a worker-scoped agent."
            )
        if target.worker_scope not in {"user", "user_agent"}:
            return (
                "Agent Vault self-service admin access requires a requester-isolated worker vault "
                "(worker_scope=user or worker_scope=user_agent). This agent uses a shared worker scope, "
                "so ask an operator to configure shared credentials or give the agent a private worker scope."
            )
        return None

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

    async def _post(self, path: str, token: str, payload: dict | None = None) -> httpx.Response:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            return await client.post(
                urljoin(self._api_url.rstrip("/") + "/", path),
                headers=self._headers(token),
                json=payload,
            )

    async def _ensure_vault(self, vault: str, token: str) -> None:
        response = await self._post("v1/vaults", token, {"name": vault})
        # 409/422 mean the vault already exists, which is fine for an idempotent grant.
        if response.status_code in {200, 201, 409, 422}:
            return
        response.raise_for_status()

    async def _ensure_vault_admin(self, vault: str, token: str) -> None:
        # Granting membership requires vault-admin on that specific vault, and
        # instance owners are not vault admins implicitly. Vaults created by
        # the worker token-mint flow do not include this admin actor, so join
        # (owner-only, grants vault-admin) before granting.
        response = await self._post(f"v1/vaults/{quote(vault, safe='')}/join", token)
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

    async def _grant_admin(
        self,
        vault: str,
        email: str,
        token: str,
        *,
        missing_account_message: str | None = None,
    ) -> bool:
        response = await self._post(
            f"v1/vaults/{quote(vault, safe='')}/users",
            token,
            {"email": email, "role": "admin"},
        )
        if response.status_code in {200, 201}:
            return True
        if response.status_code in {409, 422}:
            # Already has vault access: ensure it is the admin role this tool promises.
            await self._set_admin_role(vault, email, token)
            return False
        if response.status_code == 404:
            msg = (
                missing_account_message
                or f"{email} does not have an Agent Vault account yet. "
                "Register and verify at the vault UI first, then ask again."
            )
            raise _AgentVaultAccessError(msg)
        return self._raise_api_error("granting vault admin access", response)

    async def _set_admin_role(self, vault: str, email: str, token: str) -> None:
        response = await self._post(
            f"v1/vaults/{quote(vault, safe='')}/users/{quote(email, safe='')}/role",
            token,
            {"role": "admin"},
        )
        if response.status_code in {200, 201, 204}:
            return
        self._raise_api_error("updating vault user role to admin", response)

    def _raise_api_error(self, action: str, response: httpx.Response) -> NoReturn:
        detail = f"Agent Vault API returned {response.status_code} while {action}"
        body = response.text.strip()
        if body:
            detail = f"{detail}: {body}"
        raise _AgentVaultAccessError(detail)

    def _error(self, detail: str) -> str:
        return json.dumps(
            {"tool": "agent_vault_access", "status": "error", "error": detail},
            sort_keys=True,
        )
