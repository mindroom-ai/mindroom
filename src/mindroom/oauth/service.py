"""Shared OAuth service helpers used by API routes and tools."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlencode, urlparse

from mindroom.credentials import load_scoped_credentials, save_scoped_credentials, scoped_credentials_path
from mindroom.file_locks import async_exclusive_file_lock
from mindroom.oauth.providers import OAuthClaimValidationError, OAuthProviderError, OAuthTokenResult
from mindroom.oauth.state import consume_opaque_oauth_state, issue_opaque_oauth_state, read_opaque_oauth_state

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.credentials import CredentialsManager
    from mindroom.oauth.providers import OAuthClientConfig, OAuthProvider
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

_OAUTH_CONNECT_TOKEN_TTL_SECONDS = 600
_OAUTH_CONNECT_TOKEN_KIND = "conversation_oauth_connect"  # noqa: S105
_OAUTH_ACCESS_TOKEN_EXPIRY_SKEW_SECONDS = 60
_RECOVERABLE_REFRESH_ERROR_CODES = frozenset({"invalid_grant", "invalid_refresh_token"})
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

__all__ = [
    "OAuthConnectTarget",
    "OAuthCredentialsRefreshResult",
    "build_oauth_connect_instruction",
    "build_oauth_reconnect_instruction",
    "consume_oauth_connect_token",
    "lookup_oauth_connect_token",
    "oauth_connect_url",
    "oauth_credential_target_payload",
    "oauth_credentials_have_required_scopes",
    "oauth_credentials_match_client_id",
    "oauth_credentials_satisfy_identity_policy",
    "oauth_credentials_usable",
    "oauth_provider_service_account_configured",
    "oauth_success_redirect_url",
    "refresh_scoped_oauth_credentials",
    "refresh_scoped_oauth_credentials_with_result",
    "sanitized_oauth_token_result",
    "scoped_oauth_credentials_refresh_lock_path",
]


@dataclass(frozen=True)
class OAuthConnectTarget:
    """Server-side credential target for a conversation-issued OAuth link."""

    provider_id: str
    credential_service: str
    agent_name: str | None
    worker_scope: str
    worker_key: str
    requester_id: str | None


@dataclass(frozen=True)
class OAuthCredentialsRefreshResult:
    """Result of one locked scoped OAuth credential refresh attempt."""

    credentials: dict[str, Any] | None
    refreshed: bool


def scoped_oauth_credentials_refresh_lock_path(
    service: str,
    *,
    credentials_manager: CredentialsManager,
    worker_target: ResolvedWorkerTarget | None,
) -> Path:
    """Return the per-scope lock file for one OAuth credential refresh."""
    credentials_path = scoped_credentials_path(
        service,
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )
    return credentials_path.with_name(f"{credentials_path.name}.oauth-refresh.lock")


async def refresh_scoped_oauth_credentials(
    provider: OAuthProvider,
    runtime_paths: RuntimePaths,
    *,
    credentials_manager: CredentialsManager,
    worker_target: ResolvedWorkerTarget | None,
    allowed_shared_services: frozenset[str] | None = None,
) -> dict[str, Any] | None:
    """Refresh one scoped OAuth credential under a per-scope advisory file lock."""
    return (
        await refresh_scoped_oauth_credentials_with_result(
            provider,
            runtime_paths,
            credentials_manager=credentials_manager,
            worker_target=worker_target,
            allowed_shared_services=allowed_shared_services,
        )
    ).credentials


async def refresh_scoped_oauth_credentials_with_result(
    provider: OAuthProvider,
    runtime_paths: RuntimePaths,
    *,
    credentials_manager: CredentialsManager,
    worker_target: ResolvedWorkerTarget | None,
    allowed_shared_services: frozenset[str] | None = None,
) -> OAuthCredentialsRefreshResult:
    """Refresh one scoped OAuth credential and report whether this call saved new credentials."""
    lock_path = scoped_oauth_credentials_refresh_lock_path(
        provider.credential_service,
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )
    async with async_exclusive_file_lock(lock_path):
        credentials = load_scoped_credentials(
            provider.credential_service,
            credentials_manager=credentials_manager,
            worker_target=worker_target,
            allowed_shared_services=allowed_shared_services,
        )
        if credentials is None:
            return OAuthCredentialsRefreshResult(credentials=None, refreshed=False)
        if not oauth_credentials_usable(provider, runtime_paths, credentials):
            return OAuthCredentialsRefreshResult(credentials=credentials, refreshed=False)
        return await _refresh_scoped_oauth_credentials_locked(
            provider,
            runtime_paths,
            credentials=credentials,
            credentials_manager=credentials_manager,
            worker_target=worker_target,
            allowed_shared_services=allowed_shared_services,
        )


async def _refresh_scoped_oauth_credentials_locked(
    provider: OAuthProvider,
    runtime_paths: RuntimePaths,
    *,
    credentials: dict[str, Any],
    credentials_manager: CredentialsManager,
    worker_target: ResolvedWorkerTarget | None,
    allowed_shared_services: frozenset[str] | None,
) -> OAuthCredentialsRefreshResult:
    attempted_refresh_token = _refresh_token_value(credentials)
    try:
        refreshed_credentials = await provider.refresh_token_data(credentials, runtime_paths)
    except OAuthProviderError as exc:
        if attempted_refresh_token is None or not _is_recoverable_stale_refresh_rejection(exc):
            raise
        latest_credentials = load_scoped_credentials(
            provider.credential_service,
            credentials_manager=credentials_manager,
            worker_target=worker_target,
            allowed_shared_services=allowed_shared_services,
        )
        latest_refresh_token = _refresh_token_value(latest_credentials)
        if (
            latest_credentials is None
            or latest_refresh_token is None
            or latest_refresh_token == attempted_refresh_token
            or not oauth_credentials_usable(provider, runtime_paths, latest_credentials)
        ):
            raise
        refreshed_credentials = await provider.refresh_token_data(latest_credentials, runtime_paths)
        if refreshed_credentials is None:
            return OAuthCredentialsRefreshResult(credentials=latest_credentials, refreshed=False)
        save_scoped_credentials(
            provider.credential_service,
            refreshed_credentials,
            credentials_manager=credentials_manager,
            worker_target=worker_target,
        )
        return OAuthCredentialsRefreshResult(credentials=refreshed_credentials, refreshed=True)

    if refreshed_credentials is None:
        return OAuthCredentialsRefreshResult(credentials=credentials, refreshed=False)
    save_scoped_credentials(
        provider.credential_service,
        refreshed_credentials,
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )
    return OAuthCredentialsRefreshResult(credentials=refreshed_credentials, refreshed=True)


def _refresh_token_value(credentials: Mapping[str, Any] | None) -> str | None:
    if credentials is None:
        return None
    refresh_token = credentials.get("refresh_token")
    return refresh_token if isinstance(refresh_token, str) and refresh_token else None


def _is_recoverable_stale_refresh_rejection(exc: OAuthProviderError) -> bool:
    error_code = _normalized_oauth_error_code(exc.oauth_error)
    return error_code in _RECOVERABLE_REFRESH_ERROR_CODES


def _normalized_oauth_error_code(value: object) -> str | None:
    return value.strip().lower() if isinstance(value, str) and value.strip() else None


def oauth_credential_target_payload(
    provider: OAuthProvider,
    worker_target: ResolvedWorkerTarget | None,
) -> dict[str, str]:
    """Return serializable OAuth state payload for one credential target."""
    agent_name = worker_target.routing_agent_name if worker_target is not None else None
    worker_scope = worker_target.worker_scope if worker_target is not None else None
    worker_key = worker_target.worker_key if worker_target is not None else None
    return {
        "provider": provider.id,
        "credential_service": provider.credential_service,
        "agent_name": agent_name or "",
        "worker_scope": worker_scope or "unscoped",
        "worker_key": worker_key or "",
    }


def _issue_oauth_connect_token(
    provider: OAuthProvider,
    runtime_paths: RuntimePaths,
    worker_target: ResolvedWorkerTarget | None,
) -> str | None:
    """Create an opaque token that binds an OAuth link to one requester and target."""
    if worker_target is None or worker_target.execution_identity is None or not worker_target.worker_key:
        return None
    requester_id = worker_target.execution_identity.requester_id

    payload = oauth_credential_target_payload(provider, worker_target)
    payload["requester_id"] = requester_id or ""
    return issue_opaque_oauth_state(
        runtime_paths,
        kind=_OAUTH_CONNECT_TOKEN_KIND,
        ttl_seconds=_OAUTH_CONNECT_TOKEN_TTL_SECONDS,
        data=payload,
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


def _build_oauth_authorize_url(
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
    worker_target: ResolvedWorkerTarget | None = None,
) -> str:
    """Return a browser-openable MindRoom OAuth link for one credential scope."""
    agent_name = worker_target.routing_agent_name if worker_target is not None else None
    execution_scope = worker_target.worker_scope if worker_target is not None else None
    connect_token = _issue_oauth_connect_token(provider, runtime_paths, worker_target)
    return _build_oauth_authorize_url(
        provider,
        runtime_paths,
        agent_name=agent_name,
        execution_scope=execution_scope,
        connect_token=connect_token,
    )


def build_oauth_connect_instruction(
    provider: OAuthProvider,
    connect_url: str,
) -> str:
    """Return a concise user-facing connection instruction for a tool result."""
    return (
        f"{provider.display_name} is not connected for this agent. "
        f"Open this MindRoom link to connect it, then retry the request: {connect_url}"
    )


def build_oauth_reconnect_instruction(
    provider: OAuthProvider,
    connect_url: str,
) -> str:
    """Return a concise instruction for an expired or invalid OAuth session."""
    return (
        f"{provider.display_name} session for this agent expired or is no longer valid. "
        f"Reconnect it with this MindRoom link, then retry the request: {connect_url}"
    )


def sanitized_oauth_token_result(provider: OAuthProvider, result: OAuthTokenResult) -> OAuthTokenResult:
    """Return a token result with only safe claim metadata persisted."""
    return provider.token_result_with_safe_claims(result)
