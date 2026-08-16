"""Resolve and freeze agent-initiated OAuth credential reset targets."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast
from urllib.parse import urlencode

from mindroom.credentials import get_runtime_credentials_manager
from mindroom.oauth.credential_lifecycle import (
    OAuthCredentialContext,
    load_oauth_reset_connection_generation,
    resolve_oauth_credential_context,
)
from mindroom.oauth.registry import load_oauth_providers
from mindroom.oauth.service import oauth_credential_target_payload, oauth_public_base_url
from mindroom.oauth.state import issue_opaque_oauth_state, read_opaque_oauth_state
from mindroom.tool_system.catalog import resolved_tool_metadata_for_runtime
from mindroom.tool_system.worker_routing import build_agent_toolkit_worker_target

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.oauth.providers import OAuthProvider
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget, ToolExecutionIdentity, WorkerScope

_BROWSER_OAUTH_RESET_KIND = "browser_oauth_reset"
_BROWSER_OAUTH_RESET_TTL_SECONDS = 10 * 60


class OAuthResetTargetError(ValueError):
    """One requested provider cannot resolve to a safe agent credential target."""


@dataclass(frozen=True, slots=True)
class BrowserOAuthResetIntent:
    """One requester-bound browser reset action."""

    provider_id: str
    credential_service: str
    agent_name: str
    worker_scope: WorkerScope
    worker_key: str
    requester_id: str
    connection_generation: str
    operation_id: str

    @property
    def execution_scope(self) -> WorkerScope:
        """Return the worker scope carried in the reset URL."""
        return self.worker_scope


@dataclass(frozen=True, slots=True)
class _ResolvedOAuthResetTarget:
    """Exact provider and requester-isolated credential target for one reset."""

    agent_name: str
    credential_context: OAuthCredentialContext

    @property
    def provider(self) -> OAuthProvider:
        """Return the provider bound to this reset."""
        return self.credential_context.provider

    @property
    def worker_target(self) -> ResolvedWorkerTarget:
        """Return the requester-isolated target bound to this reset."""
        worker_target = self.credential_context.worker_target
        assert worker_target is not None
        return worker_target


def resolve_oauth_reset_target(
    provider_id: str,
    *,
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity,
    worker_target: ResolvedWorkerTarget | None = None,
) -> _ResolvedOAuthResetTarget:
    """Resolve one configured provider to the exact credential target it may reset."""
    if agent_name not in config.agents:
        msg = "OAuth reset is available only during an agent request."
        raise OAuthResetTargetError(msg)

    tool_metadata = resolved_tool_metadata_for_runtime(
        runtime_paths,
        config,
        tolerate_plugin_load_errors=True,
    )
    allowed_provider_ids = {
        metadata.auth_provider
        for tool_name in config.resolve_entity(agent_name).available_tools
        if (metadata := tool_metadata.get(tool_name)) is not None and metadata.auth_provider is not None
    }
    if provider_id not in allowed_provider_ids:
        available = ", ".join(sorted(allowed_provider_ids)) or "none"
        msg = f"Provider {provider_id!r} is not available to this agent. Available providers: {available}."
        raise OAuthResetTargetError(msg)

    provider = load_oauth_providers(config, runtime_paths).get(provider_id)
    if provider is None:
        msg = f"OAuth provider {provider_id!r} is not configured."
        raise OAuthResetTargetError(msg)

    resolved_worker_target = worker_target
    if resolved_worker_target is None:
        resolved_worker_target = build_agent_toolkit_worker_target(
            config.resolve_entity(agent_name).execution_scope,
            agent_name,
            is_private=config.get_agent(agent_name).private is not None,
            execution_identity=execution_identity,
            runtime_paths=runtime_paths,
        )
    if resolved_worker_target.routing_agent_name != agent_name:
        msg = "OAuth reset target does not belong to the invoking agent."
        raise OAuthResetTargetError(msg)

    credential_context = resolve_oauth_credential_context(
        provider,
        runtime_paths,
        get_runtime_credentials_manager(runtime_paths),
        resolved_worker_target,
        execution_identity=execution_identity,
        authorization=config.authorization,
    )
    credential_target = credential_context.worker_target
    if (
        credential_target is None
        or credential_target.worker_scope not in {"user", "user_agent"}
        or credential_target.worker_key is None
    ):
        msg = "Agent-initiated OAuth reset requires a requester-isolated user or user_agent scope."
        raise OAuthResetTargetError(msg)
    return _ResolvedOAuthResetTarget(
        agent_name=agent_name,
        credential_context=credential_context,
    )


def _browser_oauth_reset_intent_from_payload(
    provider: OAuthProvider,
    payload: Mapping[str, object],
) -> BrowserOAuthResetIntent:
    """Parse one strict browser reset intent payload."""
    provider_id = payload.get("provider")
    credential_service = payload.get("credential_service")
    agent_name = payload.get("agent_name")
    worker_scope = payload.get("worker_scope")
    worker_key = payload.get("worker_key")
    requester_id = payload.get("requester_id")
    connection_generation = payload.get("connection_generation")
    operation_id = payload.get("operation_id")
    if (
        provider_id != provider.id
        or credential_service != provider.credential_service
        or not isinstance(agent_name, str)
        or not agent_name
        or worker_scope not in {"user", "user_agent"}
        or not isinstance(worker_key, str)
        or not worker_key
        or not isinstance(requester_id, str)
        or not requester_id
        or not isinstance(connection_generation, str)
        or not connection_generation
        or not isinstance(operation_id, str)
        or not operation_id
    ):
        msg = "OAuth reset link target is invalid"
        raise OAuthResetTargetError(msg)
    return BrowserOAuthResetIntent(
        provider_id=provider.id,
        credential_service=provider.credential_service,
        agent_name=agent_name,
        worker_scope=cast("WorkerScope", worker_scope),
        worker_key=worker_key,
        requester_id=requester_id,
        connection_generation=connection_generation,
        operation_id=operation_id,
    )


async def issue_browser_oauth_reset_url(target: _ResolvedOAuthResetTarget) -> str:
    """Issue a time-limited browser URL without mutating the credential."""
    worker_target = target.worker_target
    execution_identity = worker_target.execution_identity
    if execution_identity is None or not execution_identity.requester_id:
        msg = "OAuth reset requires a requester identity"
        raise OAuthResetTargetError(msg)
    connection_generation = await load_oauth_reset_connection_generation(target.credential_context)
    provider = target.provider
    payload: dict[str, str] = {
        **oauth_credential_target_payload(provider, worker_target),
        "requester_id": execution_identity.requester_id,
        "connection_generation": connection_generation,
        "operation_id": f"browser:{secrets.token_hex(32)}",
    }
    reset_token = issue_opaque_oauth_state(
        target.credential_context.runtime_paths,
        kind=_BROWSER_OAUTH_RESET_KIND,
        ttl_seconds=_BROWSER_OAUTH_RESET_TTL_SECONDS,
        data=payload,
    )
    query = urlencode(
        {
            "agent_name": target.agent_name,
            "execution_scope": worker_target.worker_scope,
            "reset_token": reset_token,
        },
    )
    base_url = oauth_public_base_url(target.credential_context.runtime_paths, provider)
    return f"{base_url}/api/oauth/{provider.id}/reset?{query}"


def lookup_browser_oauth_reset_intent(
    provider: OAuthProvider,
    runtime_paths: RuntimePaths,
    token: str,
) -> BrowserOAuthResetIntent:
    """Read one browser reset intent without consuming it."""
    payload = read_opaque_oauth_state(
        runtime_paths,
        kind=_BROWSER_OAUTH_RESET_KIND,
        token=token,
    )
    return _browser_oauth_reset_intent_from_payload(provider, payload)
