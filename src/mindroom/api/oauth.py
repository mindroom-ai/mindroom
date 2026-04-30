"""Generic OAuth API routes."""

from __future__ import annotations

import json
from html import escape
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from mindroom.api import config_lifecycle
from mindroom.api.auth import login_redirect_for_request, verify_user
from mindroom.api.credentials import (
    build_dashboard_execution_identity,
    consume_pending_oauth_request,
    issue_pending_oauth_state,
    resolve_request_credentials_target,
)
from mindroom.credentials import delete_scoped_credentials, load_scoped_credentials, save_scoped_credentials
from mindroom.logging_config import get_logger
from mindroom.oauth import (
    OAuthClaimValidationError,
    OAuthProvider,
    OAuthProviderError,
    load_oauth_providers_for_snapshot,
)
from mindroom.oauth.service import (
    OAuthConnectTarget,
    consume_oauth_connect_token,
    lookup_oauth_connect_token,
    oauth_credentials_usable,
    oauth_success_redirect_url,
    sanitized_oauth_token_result,
)
from mindroom.tool_system.worker_routing import ResolvedWorkerTarget, resolve_worker_target

if TYPE_CHECKING:
    from mindroom.api.credentials import RequestCredentialsTarget
    from mindroom.constants import RuntimePaths

router = APIRouter(prefix="/api/oauth", tags=["oauth"])
logger = get_logger(__name__)
_OAUTH_COMPLETE_MESSAGE_TYPE = "mindroom:oauth-complete"
# OAuth callbacks intentionally verify the browser user inline instead of relying on
# standalone-public-path bypasses, because callbacks write scoped credentials.


class OAuthConnectResponse(BaseModel):
    """Authorization URL for an OAuth provider."""

    provider: str
    auth_url: str
    completion_origin: str


class OAuthStatusResponse(BaseModel):
    """Credential status for an OAuth provider."""

    provider: str
    display_name: str
    credential_service: str
    tool_config_service: str | None = None
    connected: bool
    has_client_config: bool
    email: str | None = None
    hosted_domain: str | None = None
    capabilities: list[str] = Field(default_factory=list)


def _load_provider(request: Request, provider_id: str) -> tuple[OAuthProvider, RuntimePaths]:
    snapshot = config_lifecycle.bind_current_request_snapshot(request)
    providers = load_oauth_providers_for_snapshot(snapshot)
    provider = providers.get(provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail=f"Unknown OAuth provider: {provider_id}")
    return provider, snapshot.runtime_paths


async def _require_oauth_api_user(request: Request) -> None:
    await verify_user(request, request.headers.get("authorization"), allow_public_paths=False)


async def _require_oauth_browser_user(request: Request) -> RedirectResponse | None:
    try:
        await _require_oauth_api_user(request)
    except HTTPException as exc:
        if exc.status_code == 401:
            login_redirect = login_redirect_for_request(request)
            if login_redirect is not None:
                return login_redirect
        raise
    return None


def _issue_authorization_url(
    request: Request,
    provider: OAuthProvider,
    runtime_paths: RuntimePaths,
    *,
    agent_name: str | None,
    connect_token: str | None = None,
) -> OAuthConnectResponse:
    if connect_token:
        try:
            connect_target = lookup_oauth_connect_token(provider, runtime_paths, connect_token)
        except OAuthProviderError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _verify_connect_target_authorized(request, connect_target, runtime_paths)
        _verify_connect_target_query(connect_target, agent_name, request.query_params.get("execution_scope"))
        target = resolve_request_credentials_target(
            request,
            agent_name=agent_name,
            service_names=(provider.credential_service,),
            allow_private_scopes=True,
        )
        _verify_connect_target_binding(provider, connect_target, target)
        state = issue_pending_oauth_state(
            request,
            provider.id,
            agent_name,
            payload=_target_binding_payload(provider, target),
        )
        try:
            auth_url = provider.authorization_uri(target.runtime_paths, state=state)
        except OAuthProviderError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        try:
            consume_oauth_connect_token(provider, runtime_paths, connect_token, expected_target=connect_target)
        except OAuthProviderError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return OAuthConnectResponse(
            provider=provider.id,
            auth_url=auth_url,
            completion_origin=_oauth_success_origin(provider, runtime_paths),
        )

    target = resolve_request_credentials_target(
        request,
        agent_name=agent_name,
        service_names=(provider.credential_service,),
        allow_private_scopes=True,
    )
    try:
        state = issue_pending_oauth_state(
            request,
            provider.id,
            agent_name,
            payload=_target_binding_payload(provider, target),
        )
        auth_url = provider.authorization_uri(target.runtime_paths, state=state)
    except OAuthProviderError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return OAuthConnectResponse(
        provider=provider.id,
        auth_url=auth_url,
        completion_origin=_oauth_success_origin(provider, runtime_paths),
    )


def _target_binding_payload(provider: OAuthProvider, target: RequestCredentialsTarget) -> dict[str, str]:
    worker_target = resolve_worker_target(
        target.worker_scope,
        target.agent_name,
        execution_identity=target.execution_identity,
    )
    return {
        "provider": provider.id,
        "credential_service": provider.credential_service,
        "agent_name": target.agent_name or "",
        "worker_scope": target.worker_scope or "unscoped",
        "worker_key": worker_target.worker_key or "",
    }


def _resolved_worker_target_for_credentials(target: RequestCredentialsTarget) -> ResolvedWorkerTarget | None:
    if target.worker_scope is None:
        return None
    return resolve_worker_target(
        target.worker_scope,
        target.agent_name,
        execution_identity=target.execution_identity,
    )


def _verify_connect_target_authorized(
    request: Request,
    connect_target: OAuthConnectTarget,
    runtime_paths: RuntimePaths,
) -> None:
    dashboard_identity = build_dashboard_execution_identity(
        request,
        "oauth",
        runtime_paths=runtime_paths,
    )
    if connect_target.requester_id and connect_target.requester_id != dashboard_identity.requester_id:
        raise HTTPException(status_code=403, detail="OAuth connect link does not belong to the current user")


def _verify_connect_target_query(
    connect_target: OAuthConnectTarget,
    agent_name: str | None,
    execution_scope: str | None,
) -> None:
    expected_scope = "" if connect_target.worker_scope == "unscoped" else connect_target.worker_scope
    if (agent_name or "") != (connect_target.agent_name or "") or (execution_scope or "") != expected_scope:
        raise HTTPException(status_code=400, detail="OAuth connect link target does not match this request")


def _verify_connect_target_binding(
    provider: OAuthProvider,
    connect_target: OAuthConnectTarget,
    target: RequestCredentialsTarget,
) -> None:
    expected = _target_binding_payload(provider, target)
    if (
        connect_target.agent_name != (expected["agent_name"] or None)
        or connect_target.worker_scope != expected["worker_scope"]
        or connect_target.worker_key != expected["worker_key"]
    ):
        raise HTTPException(status_code=400, detail="OAuth connect link target does not match this request")


def _verify_pending_target_binding(
    provider: OAuthProvider,
    pending_payload: dict[str, str] | None,
    target: RequestCredentialsTarget,
) -> None:
    if pending_payload != _target_binding_payload(provider, target):
        raise HTTPException(status_code=409, detail="OAuth state no longer matches the requested credential target")


def _claim_str(credentials: dict[str, Any], key: str) -> str | None:
    claims = credentials.get("_oauth_claims")
    if not isinstance(claims, dict):
        return None
    value = claims.get(key)
    return value if isinstance(value, str) and value else None


def _same_external_identity(existing_credentials: dict[str, Any] | None, token_data: dict[str, Any]) -> bool:
    existing_sub = _claim_str(existing_credentials or {}, "sub")
    new_sub = _claim_str(token_data, "sub")
    if existing_sub is not None or new_sub is not None:
        return existing_sub == new_sub

    existing_email = _claim_str(existing_credentials or {}, "email")
    new_email = _claim_str(token_data, "email")
    return existing_email is not None and existing_email == new_email


def _token_data_preserving_refresh_token(
    existing_credentials: dict[str, Any] | None,
    safe_token_data: dict[str, Any],
) -> dict[str, Any]:
    token_data = dict(safe_token_data)
    existing_refresh_token = (existing_credentials or {}).get("refresh_token")
    if (
        "refresh_token" not in token_data
        and isinstance(existing_refresh_token, str)
        and existing_refresh_token
        and _same_external_identity(existing_credentials, token_data)
    ):
        token_data["refresh_token"] = existing_refresh_token
    return token_data


def _script_json(value: object) -> str:
    return json.dumps(value).replace("</", "<\\/")


def _oauth_success_origin(provider: OAuthProvider, runtime_paths: RuntimePaths) -> str:
    success_url = oauth_success_redirect_url(provider, runtime_paths)
    parsed = urlparse(success_url)
    return f"{parsed.scheme}://{parsed.netloc}"


@router.post("/{provider_id}/connect")
async def connect(provider_id: str, request: Request, agent_name: str | None = None) -> OAuthConnectResponse:
    """Start a provider OAuth flow and return the external authorization URL."""
    await _require_oauth_api_user(request)
    provider, runtime_paths = _load_provider(request, provider_id)
    return _issue_authorization_url(request, provider, runtime_paths, agent_name=agent_name)


@router.get("/{provider_id}/authorize")
async def authorize(
    provider_id: str,
    request: Request,
    agent_name: str | None = None,
    connect_token: str | None = None,
) -> RedirectResponse:
    """Start a provider OAuth flow from a browser-openable MindRoom URL."""
    login_redirect = await _require_oauth_browser_user(request)
    if login_redirect is not None:
        return login_redirect
    provider, runtime_paths = _load_provider(request, provider_id)
    response = _issue_authorization_url(
        request,
        provider,
        runtime_paths,
        agent_name=agent_name,
        connect_token=connect_token,
    )
    return RedirectResponse(url=response.auth_url)


@router.get("/{provider_id}/success", response_class=HTMLResponse)
async def success(provider_id: str, request: Request) -> HTMLResponse:
    """Signal OAuth completion to the dashboard popup opener."""
    await _require_oauth_api_user(request)
    provider, _runtime_paths = _load_provider(request, provider_id)
    message = {
        "type": _OAUTH_COMPLETE_MESSAGE_TYPE,
        "provider": provider.id,
        "status": "connected",
    }
    escaped_display_name = escape(provider.display_name)
    html = f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <title>{escaped_display_name} connected</title>
  </head>
  <body>
    <p>{escaped_display_name} is connected. You can close this window.</p>
    <script>
      const message = {_script_json(message)};
      if (window.opener && !window.opener.closed) {{
        window.opener.postMessage(message, "*");
      }}
      window.close();
    </script>
  </body>
</html>"""
    return HTMLResponse(html)


@router.get("/{provider_id}/callback")
async def callback(provider_id: str, request: Request) -> RedirectResponse:
    """Handle a provider OAuth callback and store scoped credentials."""
    error = request.query_params.get("error")
    if error:
        raise HTTPException(status_code=400, detail=f"OAuth provider returned an error: {error}")

    code = request.query_params.get("code")
    if not code:
        raise HTTPException(status_code=400, detail="No authorization code received")

    state = request.query_params.get("state")
    if not state:
        raise HTTPException(status_code=400, detail="No OAuth state received")

    await _require_oauth_api_user(request)
    provider, runtime_paths = _load_provider(request, provider_id)
    pending = consume_pending_oauth_request(request, provider.id, state)
    worker_target: ResolvedWorkerTarget | None = None
    target = resolve_request_credentials_target(
        request,
        agent_name=pending.agent_name,
        service_names=(provider.credential_service,),
        execution_scope_override_provided=pending.execution_scope_override_provided,
        execution_scope_override=pending.execution_scope_override,
        allow_private_scopes=True,
    )
    _verify_pending_target_binding(provider, pending.payload, target)

    try:
        token_result = await provider.exchange_code(code, runtime_paths)
        provider.validate_claims(token_result, runtime_paths)
        safe_result = sanitized_oauth_token_result(provider, token_result)
        worker_target = _resolved_worker_target_for_credentials(target)
        credentials_manager = target.base_manager
        existing_credentials = load_scoped_credentials(
            provider.credential_service,
            credentials_manager=credentials_manager,
            worker_target=worker_target,
            allowed_shared_services=target.allowed_shared_services,
        )
        token_data = _token_data_preserving_refresh_token(existing_credentials, safe_result.token_data)
        save_scoped_credentials(
            provider.credential_service,
            token_data,
            credentials_manager=credentials_manager,
            worker_target=worker_target,
        )
    except OAuthClaimValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OAuthProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception(
            "oauth_callback_failed",
            provider_id=provider.id,
            error_type=type(exc).__name__,
        )
        raise HTTPException(status_code=500, detail="OAuth callback failed") from exc

    return RedirectResponse(url=oauth_success_redirect_url(provider, runtime_paths))


@router.get("/{provider_id}/status")
async def status(provider_id: str, request: Request, agent_name: str | None = None) -> OAuthStatusResponse:
    """Return scoped connection status for one provider."""
    await _require_oauth_api_user(request)
    provider, runtime_paths = _load_provider(request, provider_id)
    target = resolve_request_credentials_target(
        request,
        agent_name=agent_name,
        service_names=(provider.credential_service,),
        allow_private_scopes=True,
    )
    worker_target = _resolved_worker_target_for_credentials(target)
    credentials = (
        load_scoped_credentials(
            provider.credential_service,
            credentials_manager=target.base_manager,
            worker_target=worker_target,
            allowed_shared_services=target.allowed_shared_services,
        )
        or {}
    )
    has_client_config = provider.client_config(runtime_paths) is not None
    connected = oauth_credentials_usable(provider, runtime_paths, credentials)
    return OAuthStatusResponse(
        provider=provider.id,
        display_name=provider.display_name,
        credential_service=provider.credential_service,
        tool_config_service=provider.tool_config_service,
        connected=connected,
        has_client_config=has_client_config,
        email=_claim_str(credentials, "email"),
        hosted_domain=_claim_str(credentials, "hd"),
        capabilities=list(provider.status_capabilities),
    )


@router.post("/{provider_id}/disconnect")
async def disconnect(provider_id: str, request: Request, agent_name: str | None = None) -> dict[str, str]:
    """Remove scoped OAuth credentials for one provider while preserving tool settings."""
    await _require_oauth_api_user(request)
    provider, _runtime_paths = _load_provider(request, provider_id)
    target = resolve_request_credentials_target(
        request,
        agent_name=agent_name,
        service_names=(provider.credential_service,),
        allow_private_scopes=True,
    )
    worker_target = _resolved_worker_target_for_credentials(target)
    delete_scoped_credentials(
        provider.credential_service,
        credentials_manager=target.base_manager,
        worker_target=worker_target,
    )
    return {"status": "disconnected", "provider": provider.id}
