"""Shared OAuth service helpers used by API routes and tools."""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlencode, urlparse

from mindroom.oauth.providers import OAuthProviderError

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths
    from mindroom.oauth.providers import OAuthProvider, OAuthTokenResult
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

_OAUTH_CONNECT_TOKEN_TTL_SECONDS = 600
_oauth_connect_token_lock = threading.Lock()


@dataclass(frozen=True)
class OAuthConnectTarget:
    """Server-side credential target for a conversation-issued OAuth link."""

    provider_id: str
    credential_service: str
    agent_name: str | None
    worker_scope: str
    worker_key: str
    requester_id: str | None
    tenant_id: str | None
    account_id: str | None
    created_at: float


_oauth_connect_targets: dict[str, OAuthConnectTarget] = {}


def _prune_expired_connect_targets(now: float) -> None:
    expired_keys = [
        token
        for token, target in _oauth_connect_targets.items()
        if now - target.created_at > _OAUTH_CONNECT_TOKEN_TTL_SECONDS
    ]
    for token in expired_keys:
        _oauth_connect_targets.pop(token, None)


def issue_oauth_connect_token(provider: OAuthProvider, worker_target: ResolvedWorkerTarget) -> str | None:
    """Create a short-lived token that binds an OAuth link to one worker target."""
    if not worker_target.worker_key:
        return None

    token = secrets.token_urlsafe(24)
    now = time.time()
    connect_target = OAuthConnectTarget(
        provider_id=provider.id,
        credential_service=provider.credential_service,
        agent_name=worker_target.routing_agent_name,
        worker_scope=worker_target.worker_scope or "unscoped",
        worker_key=worker_target.worker_key,
        requester_id=(
            worker_target.execution_identity.requester_id if worker_target.execution_identity is not None else None
        ),
        tenant_id=worker_target.tenant_id,
        account_id=worker_target.account_id,
        created_at=now,
    )
    with _oauth_connect_token_lock:
        _prune_expired_connect_targets(now)
        _oauth_connect_targets[token] = connect_target
    return token


def consume_oauth_connect_token(provider: OAuthProvider, token: str) -> OAuthConnectTarget:
    """Consume one conversation-issued OAuth target token for a provider authorize request."""
    now = time.time()
    with _oauth_connect_token_lock:
        _prune_expired_connect_targets(now)
        connect_target = _oauth_connect_targets.pop(token, None)

    if connect_target is None:
        msg = "OAuth connect link is invalid or expired"
        raise OAuthProviderError(msg)
    if connect_target.provider_id != provider.id:
        msg = "OAuth connect link does not match this provider"
        raise OAuthProviderError(msg)
    return connect_target


def oauth_connect_target_payload(connect_target: OAuthConnectTarget) -> dict[str, str]:
    """Return serializable OAuth state payload for one connect target."""
    return {
        "target_mode": "worker_key",
        "provider": connect_target.provider_id,
        "credential_service": connect_target.credential_service,
        "agent_name": connect_target.agent_name or "",
        "worker_scope": connect_target.worker_scope,
        "worker_key": connect_target.worker_key,
        "requester_id": connect_target.requester_id or "",
        "tenant_id": connect_target.tenant_id or "",
        "account_id": connect_target.account_id or "",
    }


def mindroom_public_base_url(runtime_paths: RuntimePaths, provider: OAuthProvider | None = None) -> str:
    """Return the public MindRoom origin used for user-facing OAuth links."""
    configured = runtime_paths.env_value("MINDROOM_PUBLIC_URL") or runtime_paths.env_value("MINDROOM_BASE_URL")
    if configured:
        return configured.rstrip("/")

    if provider is not None:
        client_config = provider.client_config(runtime_paths)
        if client_config is not None:
            parsed = urlparse(client_config.redirect_uri)
            if parsed.scheme and parsed.netloc:
                return f"{parsed.scheme}://{parsed.netloc}"

    port = runtime_paths.env_value("MINDROOM_PORT", default="8765") or "8765"
    return f"http://localhost:{port}"


def oauth_success_redirect_url(provider: OAuthProvider, runtime_paths: RuntimePaths) -> str:
    """Return the post-callback browser destination for one provider."""
    base_url = mindroom_public_base_url(runtime_paths, provider)
    return f"{base_url}/?oauth_provider={provider.id}&oauth=connected"


def build_oauth_authorize_url(
    provider: OAuthProvider,
    runtime_paths: RuntimePaths,
    *,
    agent_name: str | None = None,
    execution_scope: str | None = None,
    connect_token: str | None = None,
) -> str:
    """Build an authenticated MindRoom URL that starts a provider OAuth flow."""
    base_url = mindroom_public_base_url(runtime_paths, provider)
    params: dict[str, str] = {}
    if connect_token:
        params["connect_token"] = connect_token
    elif agent_name:
        params["agent_name"] = agent_name
    if execution_scope and not connect_token:
        params["execution_scope"] = execution_scope
    query = f"?{urlencode(params)}" if params else ""
    return f"{base_url}/api/oauth/{provider.id}/authorize{query}"


def build_oauth_connect_instruction(
    provider: OAuthProvider,
    runtime_paths: RuntimePaths,
    *,
    worker_target: ResolvedWorkerTarget | None,
) -> str:
    """Return a concise user-facing connection instruction for a tool result."""
    agent_name = worker_target.routing_agent_name if worker_target is not None else None
    execution_scope = worker_target.worker_scope if worker_target is not None else None
    connect_token = issue_oauth_connect_token(provider, worker_target) if worker_target is not None else None
    connect_url = build_oauth_authorize_url(
        provider,
        runtime_paths,
        agent_name=agent_name,
        execution_scope=execution_scope,
        connect_token=connect_token,
    )
    return (
        f"{provider.display_name} is not connected for this agent. "
        f"Open this MindRoom link to connect it, then retry the request: {connect_url}"
    )


def sanitized_oauth_token_result(provider: OAuthProvider, result: OAuthTokenResult) -> OAuthTokenResult:
    """Return a token result with only safe claim metadata persisted."""
    return provider.token_result_with_safe_claims(result)
