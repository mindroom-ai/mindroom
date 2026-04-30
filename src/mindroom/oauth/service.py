"""Shared OAuth service helpers used by API routes and tools."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlencode, urlparse

from mindroom.oauth.providers import OAuthProviderError
from mindroom.oauth.state import consume_opaque_oauth_state, issue_opaque_oauth_state, read_opaque_oauth_state

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths
    from mindroom.oauth.providers import OAuthProvider, OAuthTokenResult
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

_OAUTH_CONNECT_TOKEN_TTL_SECONDS = 600
_OAUTH_CONNECT_TOKEN_KIND = "conversation_oauth_connect"  # noqa: S105
_SANDBOX_SHARED_STORAGE_ROOT_ENV = "MINDROOM_SANDBOX_SHARED_STORAGE_ROOT"
OAUTH_CREDENTIAL_FIELDS = frozenset(
    {
        "_id_token",
        "_oauth_claims",
        "_oauth_provider",
        "_source",
        "access_token",
        "client_id",
        "client_secret",
        "expires_at",
        "expires_in",
        "id_token",
        "refresh_token",
        "scope",
        "scopes",
        "token",
        "token_type",
        "token_uri",
    },
)
_OAUTH_ACCESS_TOKEN_EXPIRY_SKEW_SECONDS = 60
_SCOPE_IMPLICATIONS = {
    "https://www.googleapis.com/auth/calendar": frozenset(
        {"https://www.googleapis.com/auth/calendar.readonly"},
    ),
    "https://www.googleapis.com/auth/drive": frozenset(
        {
            "https://www.googleapis.com/auth/drive.file",
            "https://www.googleapis.com/auth/drive.readonly",
        },
    ),
    "https://www.googleapis.com/auth/gmail.modify": frozenset(
        {"https://www.googleapis.com/auth/gmail.readonly"},
    ),
    "https://www.googleapis.com/auth/spreadsheets": frozenset(
        {"https://www.googleapis.com/auth/spreadsheets.readonly"},
    ),
}


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


def _connect_token_runtime_paths(runtime_paths: RuntimePaths) -> RuntimePaths:
    shared_storage_root = runtime_paths.env_value(_SANDBOX_SHARED_STORAGE_ROOT_ENV)
    if not shared_storage_root:
        return runtime_paths
    return replace(runtime_paths, storage_root=Path(shared_storage_root).expanduser().resolve())


def issue_oauth_connect_token(
    provider: OAuthProvider,
    runtime_paths: RuntimePaths,
    worker_target: ResolvedWorkerTarget,
) -> str | None:
    """Create an opaque token that binds an OAuth link to one worker target."""
    if not worker_target.worker_key:
        return None

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
        created_at=0,
    )
    return issue_opaque_oauth_state(
        _connect_token_runtime_paths(runtime_paths),
        kind=_OAUTH_CONNECT_TOKEN_KIND,
        ttl_seconds=_OAUTH_CONNECT_TOKEN_TTL_SECONDS,
        data=oauth_connect_target_payload(connect_target),
    )


def _connect_target_from_payload(provider: OAuthProvider, payload: dict[str, object]) -> OAuthConnectTarget:
    if payload.get("provider") != provider.id:
        msg = "OAuth connect link does not match this provider"
        raise OAuthProviderError(msg)
    if payload.get("credential_service") != provider.credential_service:
        msg = "OAuth connect link does not match this provider"
        raise OAuthProviderError(msg)
    return OAuthConnectTarget(
        provider_id=provider.id,
        credential_service=provider.credential_service,
        agent_name=str(payload.get("agent_name") or "") or None,
        worker_scope=str(payload.get("worker_scope") or "unscoped"),
        worker_key=str(payload.get("worker_key") or ""),
        requester_id=str(payload.get("requester_id") or "") or None,
        tenant_id=str(payload.get("tenant_id") or "") or None,
        account_id=str(payload.get("account_id") or "") or None,
        created_at=0,
    )


def lookup_oauth_connect_token(provider: OAuthProvider, runtime_paths: RuntimePaths, token: str) -> OAuthConnectTarget:
    """Return one conversation-issued OAuth target token without consuming it."""
    data = read_opaque_oauth_state(
        _connect_token_runtime_paths(runtime_paths),
        kind=_OAUTH_CONNECT_TOKEN_KIND,
        token=token,
    )
    return _connect_target_from_payload(provider, data)


def consume_oauth_connect_token(
    provider: OAuthProvider,
    runtime_paths: RuntimePaths,
    token: str,
    *,
    expected_target: OAuthConnectTarget | None = None,
) -> OAuthConnectTarget:
    """Consume one conversation-issued OAuth target token for a provider authorize request."""
    data = consume_opaque_oauth_state(
        _connect_token_runtime_paths(runtime_paths),
        kind=_OAUTH_CONNECT_TOKEN_KIND,
        token=token,
    )
    connect_target = _connect_target_from_payload(provider, data)
    if expected_target is not None and connect_target != expected_target:
        msg = "OAuth connect link target changed"
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
    return f"{base_url}/api/oauth/{provider.id}/success"


def oauth_credentials_usable(
    provider: OAuthProvider,
    runtime_paths: RuntimePaths,
    credentials: dict[str, object] | None,
    *,
    now: float | None = None,
) -> bool:
    """Return whether stored OAuth credentials can currently authenticate provider calls."""
    if not credentials or provider.client_config(runtime_paths) is None:
        return False
    if not oauth_credentials_have_required_scopes(provider, credentials):
        return False

    token = credentials.get("token") or credentials.get("access_token")
    refresh_token = credentials.get("refresh_token")
    has_refresh_token = isinstance(refresh_token, str) and bool(refresh_token)
    if isinstance(token, str) and token:
        expires_at = credentials.get("expires_at")
        if isinstance(expires_at, bool) or not isinstance(expires_at, int | float) or not math.isfinite(expires_at):
            return True
        return (
            float(expires_at) > (now if now is not None else time.time()) + _OAUTH_ACCESS_TOKEN_EXPIRY_SKEW_SECONDS
            or has_refresh_token
        )

    expires_at = credentials.get("expires_at")
    if isinstance(expires_at, bool) or not isinstance(expires_at, int | float) or not math.isfinite(expires_at):
        return False
    return has_refresh_token


def oauth_credentials_have_required_scopes(provider: OAuthProvider, credentials: dict[str, object]) -> bool:
    """Return whether stored credentials include every provider-required scope."""
    granted_scopes: set[str] = set()
    raw_scopes = credentials.get("scopes")
    if isinstance(raw_scopes, list):
        granted_scopes.update(scope for scope in raw_scopes if isinstance(scope, str) and scope)
    raw_scope = credentials.get("scope")
    if isinstance(raw_scope, str):
        granted_scopes.update(scope for scope in raw_scope.split() if scope)
    expanded_granted_scopes = set(granted_scopes)
    for scope in granted_scopes:
        expanded_granted_scopes.update(_SCOPE_IMPLICATIONS.get(scope, ()))
    return set(provider.scopes).issubset(expanded_granted_scopes)


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


def oauth_connect_url(
    provider: OAuthProvider,
    runtime_paths: RuntimePaths,
    *,
    worker_target: ResolvedWorkerTarget | None,
) -> str:
    """Return a browser-openable MindRoom OAuth link for one worker target."""
    agent_name = worker_target.routing_agent_name if worker_target is not None else None
    execution_scope = worker_target.worker_scope if worker_target is not None else None
    connect_token = (
        issue_oauth_connect_token(provider, runtime_paths, worker_target) if worker_target is not None else None
    )
    return build_oauth_authorize_url(
        provider,
        runtime_paths,
        agent_name=agent_name,
        execution_scope=execution_scope,
        connect_token=connect_token,
    )


def build_oauth_connect_instruction(
    provider: OAuthProvider,
    runtime_paths: RuntimePaths,
    *,
    worker_target: ResolvedWorkerTarget | None,
) -> str:
    """Return a concise user-facing connection instruction for a tool result."""
    connect_url = oauth_connect_url(
        provider,
        runtime_paths,
        worker_target=worker_target,
    )
    return (
        f"{provider.display_name} is not connected for this agent. "
        f"Open this MindRoom link to connect it, then retry the request: {connect_url}"
    )


def sanitized_oauth_token_result(provider: OAuthProvider, result: OAuthTokenResult) -> OAuthTokenResult:
    """Return a token result with only safe claim metadata persisted."""
    return provider.token_result_with_safe_claims(result)
