"""Shared OAuth service helpers used by API routes and tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlencode, urlparse

from mindroom.oauth.credential_binding import (
    OAuthCredentialBinding,
    OAuthCredentialBindingParseError,
    oauth_credential_binding,
    oauth_credential_binding_payload,
    parse_oauth_credential_binding_payload,
)
from mindroom.oauth.providers import (
    OAuthConnectionRequired,
    OAuthProviderError,
    oauth_connect_url_requires_host_browser,
)
from mindroom.oauth.state import consume_opaque_oauth_state, issue_opaque_oauth_state, read_opaque_oauth_state

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths
    from mindroom.oauth.credential_lifecycle import OAuthCredentialContext
    from mindroom.oauth.providers import OAuthProvider
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

OAUTH_CONNECT_TOKEN_TTL_MINUTES = 10
OAUTH_ACCESS_REJECTED_REASON = "access_rejected"
OAUTH_REFRESH_REJECTED_REASON = "refresh_rejected"
OAUTH_MISSING_WRITE_SCOPE_REASON = "missing_write_scope"
OAUTH_RESET_REQUIRED_REASON = "reset_required"
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
    "OAUTH_ACCESS_REJECTED_REASON",
    "OAUTH_CONNECT_TOKEN_TTL_MINUTES",
    "OAUTH_MISSING_WRITE_SCOPE_REASON",
    "OAUTH_REFRESH_REJECTED_REASON",
    "OAUTH_RESET_REQUIRED_REASON",
    "OAuthConnectTarget",
    "build_oauth_connect_instruction",
    "build_oauth_reconnect_instruction",
    "consume_oauth_connect_token",
    "lookup_oauth_connect_token",
    "oauth_connect_url",
    "oauth_connection_required",
    "oauth_provider_service_account_configured",
    "oauth_public_base_url",
    "oauth_success_redirect_url",
]


@dataclass(frozen=True, slots=True)
class OAuthConnectTarget:
    """Server-side credential target for a conversation-issued OAuth link."""

    binding: OAuthCredentialBinding
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

    binding = oauth_credential_binding(provider, worker_target)
    payload = oauth_credential_binding_payload(binding)
    payload["requester_id"] = requester_id or ""
    return issue_opaque_oauth_state(
        runtime_paths,
        kind=_OAUTH_CONNECT_TOKEN_KIND,
        ttl_seconds=_OAUTH_CONNECT_TOKEN_TTL_SECONDS,
        data=payload,
    )


def _connect_target_from_payload(provider: OAuthProvider, payload: dict[str, object]) -> OAuthConnectTarget:
    try:
        binding = parse_oauth_credential_binding_payload(
            provider,
            payload,
            allowed_worker_scopes=frozenset({"shared", "user", "user_agent", "unscoped"}),
            require_agent_name=False,
            require_worker_key=True,
        )
    except OAuthCredentialBindingParseError as exc:
        if exc.reason == "provider_mismatch":
            msg = "OAuth connect link does not match this provider"
        else:
            msg = "OAuth connect link target is invalid"
        raise OAuthProviderError(msg) from exc
    return OAuthConnectTarget(binding=binding, requester_id=str(payload.get("requester_id") or "") or None)


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


def oauth_public_base_url(runtime_paths: RuntimePaths, provider: OAuthProvider | None = None) -> str:
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
    base_url = oauth_public_base_url(runtime_paths, provider)
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
    base_url = oauth_public_base_url(runtime_paths, provider)
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
    *,
    retry_safe: bool = True,
) -> str:
    """Return a concise instruction for an expired or invalid OAuth session."""
    retry_guidance = "After reconnecting, retry the request."
    expiry_guidance = "rerun the original request for a fresh link"
    if not retry_safe:
        retry_guidance = (
            "The original operation may have partially succeeded; do not automatically retry it after reconnecting."
        )
        expiry_guidance = "request a fresh reconnect link without repeating the original operation"
    if oauth_connect_url_requires_host_browser(connect_url):
        return (
            f"{provider.display_name} session for this agent expired or is no longer valid. "
            "Open this MindRoom link in a browser on the computer where the MindRoom process is running, "
            "not on a phone or another computer. If needed, open this conversation there or copy the complete "
            f"link into that browser. {retry_guidance} "
            f"This link is valid for {OAUTH_CONNECT_TOKEN_TTL_MINUTES} minutes; if it expires, "
            f"{expiry_guidance}: {connect_url}"
        )
    return (
        f"{provider.display_name} session for this agent expired or is no longer valid. "
        f"Reconnect it with this MindRoom link. {retry_guidance} "
        f"This link is valid for {OAUTH_CONNECT_TOKEN_TTL_MINUTES} minutes; if it expires, "
        f"{expiry_guidance}: {connect_url}"
    )


def oauth_connection_required(
    context: OAuthCredentialContext,
    *,
    reason: str | None = None,
    retry_safe: bool = True,
) -> OAuthConnectionRequired:
    """Build one canonical connect or reconnect error for a credential scope."""
    if reason == OAUTH_RESET_REQUIRED_REASON:
        instruction = (
            f"{context.provider.display_name} credentials for this requester cannot be read. "
            "Use the authenticated MindRoom dashboard's Integrations page to reset this provider connection, "
            "then reconnect and retry the request."
        )
        return OAuthConnectionRequired(
            instruction,
            provider_id=context.provider.id,
            reason=reason,
            reset_required=True,
        )
    connect_url = oauth_connect_url(
        context.provider,
        context.runtime_paths,
        worker_target=context.worker_target,
    )
    if reason in {OAUTH_ACCESS_REJECTED_REASON, OAUTH_REFRESH_REJECTED_REASON}:
        instruction = build_oauth_reconnect_instruction(context.provider, connect_url, retry_safe=retry_safe)
    elif reason == OAUTH_MISSING_WRITE_SCOPE_REASON:
        instruction = (
            f"{context.provider.display_name} reconnect required to grant write access. "
            f"Reconnect with this MindRoom link, then retry the write: {connect_url}"
        )
    else:
        instruction = build_oauth_connect_instruction(context.provider, connect_url)
    return OAuthConnectionRequired(
        instruction,
        provider_id=context.provider.id,
        connect_url=connect_url,
        reason=reason,
    )
