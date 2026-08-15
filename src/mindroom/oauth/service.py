"""Shared OAuth service helpers used by API routes and tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlencode, urlparse

from mindroom.oauth.credential_lifecycle import (
    OAuthCredentialContext,
    OAuthCredentialsRefreshResult,
    OAuthCredentialsSnapshot,
    load_oauth_credentials,
    load_oauth_credentials_snapshot,
    load_oauth_credentials_snapshot_sync,
    oauth_connection_generation,
    oauth_credential_generation,
    oauth_credentials_have_required_scopes,
    oauth_credentials_have_scopes,
    oauth_credentials_match_client_id,
    oauth_credentials_satisfy_identity_policy,
    oauth_credentials_usable,
    oauth_credentials_worker_target,
    refresh_oauth_credentials,
    refresh_oauth_credentials_blocking,
    refresh_oauth_credentials_sync,
    refresh_oauth_credentials_with_result,
    reset_oauth_credentials,
    resolve_oauth_credential_context,
    sanitized_oauth_token_result,
)
from mindroom.oauth.providers import (
    OAuthConnectionRequired,
    OAuthProviderError,
    oauth_connect_url_requires_host_browser,
)
from mindroom.oauth.state import consume_opaque_oauth_state, issue_opaque_oauth_state, read_opaque_oauth_state

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths
    from mindroom.oauth.providers import OAuthProvider
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

OAUTH_CONNECT_TOKEN_TTL_MINUTES = 10
OAUTH_REFRESH_REJECTED_REASON = "refresh_rejected"
_OAUTH_CONNECT_TOKEN_TTL_SECONDS = OAUTH_CONNECT_TOKEN_TTL_MINUTES * 60
_OAUTH_CONNECT_TOKEN_KIND = "conversation_oauth_connect"  # noqa: S105
_GOOGLE_SERVICE_ACCOUNT_PROVIDER_IDS = frozenset(
    {
        "google_calendar",
        "google_docs",
        "google_drive",
        "google_gmail",
        "google_sheets",
    },
)
__all__ = [
    "OAUTH_CONNECT_TOKEN_TTL_MINUTES",
    "OAUTH_REFRESH_REJECTED_REASON",
    "OAuthConnectTarget",
    "OAuthCredentialContext",
    "OAuthCredentialsRefreshResult",
    "OAuthCredentialsSnapshot",
    "build_oauth_connect_instruction",
    "build_oauth_reconnect_instruction",
    "consume_oauth_connect_token",
    "load_oauth_credentials",
    "load_oauth_credentials_snapshot",
    "load_oauth_credentials_snapshot_sync",
    "lookup_oauth_connect_token",
    "oauth_connect_url",
    "oauth_connection_generation",
    "oauth_connection_required",
    "oauth_credential_generation",
    "oauth_credential_target_payload",
    "oauth_credentials_have_required_scopes",
    "oauth_credentials_have_scopes",
    "oauth_credentials_match_client_id",
    "oauth_credentials_satisfy_identity_policy",
    "oauth_credentials_usable",
    "oauth_credentials_worker_target",
    "oauth_provider_service_account_configured",
    "oauth_success_redirect_url",
    "refresh_oauth_credentials",
    "refresh_oauth_credentials_blocking",
    "refresh_oauth_credentials_sync",
    "refresh_oauth_credentials_with_result",
    "reset_oauth_credentials",
    "resolve_oauth_credential_context",
    "sanitized_oauth_token_result",
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
    if oauth_connect_url_requires_host_browser(connect_url):
        return (
            f"{provider.display_name} is not connected for this agent. "
            "Open this MindRoom link in a browser on the computer where the MindRoom process is running, "
            "not on a phone or another computer. If needed, open this conversation there or copy the complete "
            f"link into that browser. After connecting, retry the request: {connect_url}"
        )
    return (
        f"{provider.display_name} is not connected for this agent. "
        f"Open this MindRoom link to connect it, then retry the request: {connect_url}"
    )


def build_oauth_reconnect_instruction(
    provider: OAuthProvider,
    connect_url: str,
) -> str:
    """Return a concise instruction for an expired or invalid OAuth session."""
    if oauth_connect_url_requires_host_browser(connect_url):
        return (
            f"{provider.display_name} session for this agent expired or is no longer valid. "
            "Open this MindRoom link in a browser on the computer where the MindRoom process is running, "
            "not on a phone or another computer. If needed, open this conversation there or copy the complete "
            "link into that browser. After reconnecting, retry the request. "
            f"This link is valid for {OAUTH_CONNECT_TOKEN_TTL_MINUTES} minutes; if it expires, "
            f"rerun the original request for a fresh link: {connect_url}"
        )
    return (
        f"{provider.display_name} session for this agent expired or is no longer valid. "
        "Reconnect it with this MindRoom link, then retry the request. "
        f"This link is valid for {OAUTH_CONNECT_TOKEN_TTL_MINUTES} minutes; if it expires, "
        f"rerun the original request for a fresh link: {connect_url}"
    )


def oauth_connection_required(
    context: OAuthCredentialContext,
    *,
    reason: str | None = None,
) -> OAuthConnectionRequired:
    """Build one canonical connect or reconnect error for a credential scope."""
    connect_url = oauth_connect_url(
        context.provider,
        context.runtime_paths,
        worker_target=context.worker_target,
    )
    instruction = (
        build_oauth_reconnect_instruction(context.provider, connect_url)
        if reason == OAUTH_REFRESH_REJECTED_REASON
        else build_oauth_connect_instruction(context.provider, connect_url)
    )
    return OAuthConnectionRequired(
        instruction,
        provider_id=context.provider.id,
        connect_url=connect_url,
        reason=reason,
    )
