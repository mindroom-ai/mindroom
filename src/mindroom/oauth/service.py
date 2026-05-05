"""Shared OAuth service helpers used by API routes and tools."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlencode, urlparse

from mindroom.oauth.providers import OAuthClaimValidationError, OAuthProviderError, OAuthTokenResult
from mindroom.oauth.state import consume_opaque_oauth_state, issue_opaque_oauth_state, read_opaque_oauth_state

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths
    from mindroom.oauth.providers import OAuthClientConfig, OAuthProvider
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

_OAUTH_CONNECT_TOKEN_TTL_SECONDS = 600
_OAUTH_CONNECT_TOKEN_KIND = "conversation_oauth_connect"  # noqa: S105
_OAUTH_ACCESS_TOKEN_EXPIRY_SKEW_SECONDS = 60
_GOOGLE_SERVICE_ACCOUNT_PROVIDER_IDS = frozenset(
    {
        "google_calendar",
        "google_drive",
        "google_gmail",
        "google_sheets",
    },
)
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


def _issue_oauth_connect_token(
    provider: OAuthProvider,
    runtime_paths: RuntimePaths,
    worker_target: ResolvedWorkerTarget | None,
) -> str | None:
    """Create an opaque token that binds an OAuth link to one requester and target."""
    if worker_target is None or worker_target.execution_identity is None or not worker_target.worker_key:
        return None
    requester_id = worker_target.execution_identity.requester_id

    connect_target = OAuthConnectTarget(
        provider_id=provider.id,
        credential_service=provider.credential_service,
        agent_name=worker_target.routing_agent_name,
        worker_scope=worker_target.worker_scope or "unscoped",
        worker_key=worker_target.worker_key,
        requester_id=requester_id,
    )
    return issue_opaque_oauth_state(
        runtime_paths,
        kind=_OAUTH_CONNECT_TOKEN_KIND,
        ttl_seconds=_OAUTH_CONNECT_TOKEN_TTL_SECONDS,
        data=_oauth_connect_target_payload(connect_target),
    )


def _connect_target_from_payload(provider: OAuthProvider, payload: dict[str, object]) -> OAuthConnectTarget:
    if payload.get("provider") != provider.id:
        msg = "OAuth connect link does not match this provider"
        raise OAuthProviderError(msg)
    if payload.get("credential_service") != provider.credential_service:
        msg = "OAuth connect link does not match this provider"
        raise OAuthProviderError(msg)
    worker_scope = str(payload.get("worker_scope") or "")
    worker_key = str(payload.get("worker_key") or "")
    if worker_scope not in {"shared", "user", "user_agent", "unscoped"} or not worker_key:
        msg = "OAuth connect link target is invalid"
        raise OAuthProviderError(msg)
    return OAuthConnectTarget(
        provider_id=provider.id,
        credential_service=provider.credential_service,
        agent_name=str(payload.get("agent_name") or "") or None,
        worker_scope=worker_scope,
        worker_key=worker_key,
        requester_id=str(payload.get("requester_id") or "") or None,
    )


def lookup_oauth_connect_token(provider: OAuthProvider, runtime_paths: RuntimePaths, token: str) -> OAuthConnectTarget:
    """Return one conversation-issued OAuth target token without consuming it."""
    data = read_opaque_oauth_state(
        runtime_paths,
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
        runtime_paths,
        kind=_OAUTH_CONNECT_TOKEN_KIND,
        token=token,
    )
    connect_target = _connect_target_from_payload(provider, data)
    if expected_target is not None and connect_target != expected_target:
        msg = "OAuth connect link target changed"
        raise OAuthProviderError(msg)
    return connect_target


def _oauth_connect_target_payload(connect_target: OAuthConnectTarget) -> dict[str, str]:
    """Return serializable OAuth state payload for one connect target."""
    return {
        "provider": connect_target.provider_id,
        "credential_service": connect_target.credential_service,
        "agent_name": connect_target.agent_name or "",
        "worker_scope": connect_target.worker_scope,
        "worker_key": connect_target.worker_key,
        "requester_id": connect_target.requester_id or "",
    }


def _mindroom_public_base_url(runtime_paths: RuntimePaths, provider: OAuthProvider | None = None) -> str:
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
    base_url = _mindroom_public_base_url(runtime_paths, provider)
    return f"{base_url}/api/oauth/{provider.id}/success"


def oauth_provider_service_account_configured(provider: OAuthProvider, runtime_paths: RuntimePaths) -> bool:
    """Return whether one provider can authenticate through a Google service account."""
    return provider.id in _GOOGLE_SERVICE_ACCOUNT_PROVIDER_IDS and bool(
        runtime_paths.env_value("GOOGLE_SERVICE_ACCOUNT_FILE"),
    )


def oauth_credentials_usable(  # noqa: PLR0911
    provider: OAuthProvider,
    runtime_paths: RuntimePaths,
    credentials: dict[str, object] | None,
    *,
    now: float | None = None,
) -> bool:
    """Return whether stored OAuth credentials can currently authenticate provider calls."""
    client_config = provider.client_config(runtime_paths)
    if not credentials or client_config is None:
        return False
    if not oauth_credentials_match_client_id(client_config, credentials):
        return False
    if not oauth_credentials_have_required_scopes(provider, credentials):
        return False
    if not oauth_credentials_satisfy_identity_policy(provider, runtime_paths, credentials):
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


def oauth_credentials_match_client_id(
    client_config: OAuthClientConfig,
    credentials: dict[str, object],
) -> bool:
    """Return whether token credentials belong to the active OAuth app client."""
    stored_client_id = credentials.get("client_id")
    return isinstance(stored_client_id, str) and stored_client_id.strip() == client_config.client_id


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


def oauth_credentials_satisfy_identity_policy(
    provider: OAuthProvider,
    runtime_paths: RuntimePaths,
    credentials: dict[str, object],
) -> bool:
    """Return whether stored credentials still satisfy configured identity policy."""
    has_identity_policy = (
        bool(provider.resolved_allowed_email_domains(runtime_paths))
        or bool(provider.resolved_allowed_hosted_domains(runtime_paths))
        or provider.claim_validator is not None
    )
    if not has_identity_policy:
        return True

    raw_claims = credentials.get("_oauth_claims")
    if not isinstance(raw_claims, dict) or not raw_claims:
        return False
    if credentials.get("_oauth_claims_verified") is not True:
        return False
    claims = cast("dict[str, Any]", raw_claims)
    try:
        provider.validate_claims(
            OAuthTokenResult(
                token_data=dict(credentials),
                claims=claims,
                claims_verified=True,
            ),
            runtime_paths,
        )
    except OAuthClaimValidationError:
        return False
    return True


def build_oauth_authorize_url(
    provider: OAuthProvider,
    runtime_paths: RuntimePaths,
    *,
    agent_name: str | None = None,
    execution_scope: str | None = None,
    connect_token: str | None = None,
) -> str:
    """Build an authenticated MindRoom URL that starts a provider OAuth flow."""
    base_url = _mindroom_public_base_url(runtime_paths, provider)
    params: dict[str, str] = {}
    if connect_token:
        params["connect_token"] = connect_token
    if agent_name:
        params["agent_name"] = agent_name
    if execution_scope:
        params["execution_scope"] = execution_scope
    query = f"?{urlencode(params)}" if params else ""
    return f"{base_url}/api/oauth/{provider.id}/authorize{query}"


def oauth_connect_url(
    provider: OAuthProvider,
    runtime_paths: RuntimePaths,
    *,
    worker_target: ResolvedWorkerTarget | None,
) -> str:
    """Return a browser-openable MindRoom OAuth link for one credential scope."""
    agent_name = worker_target.routing_agent_name if worker_target is not None else None
    execution_scope = worker_target.worker_scope if worker_target is not None else None
    connect_token = _issue_oauth_connect_token(provider, runtime_paths, worker_target)
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
