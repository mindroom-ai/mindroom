"""Generic OAuth API routes."""

from __future__ import annotations

import json
from dataclasses import replace
from html import escape
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from mindroom.api import config_lifecycle
from mindroom.api.auth import login_redirect_for_request, verify_user
from mindroom.api.credentials_oauth_flows import consume_pending_oauth_request, issue_pending_oauth_state
from mindroom.api.credentials_target import (
    resolve_request_credentials_target,
    resolve_requester_credentials_target,
    worker_target_for_credentials_target,
)
from mindroom.api.dashboard_credential_scope import (
    build_dashboard_execution_identity,
    require_agent_credential_management_authorized,
)
from mindroom.background_tasks import run_coroutine_until_complete
from mindroom.logging_config import get_logger
from mindroom.oauth import (
    OAuthClaimValidationError,
    OAuthClientConfigResolution,
    OAuthProvider,
    OAuthProviderError,
    is_oauth_loopback_hostname,
)
from mindroom.oauth.credential_binding import (
    OAuthCredentialBinding,
    oauth_credential_binding,
    oauth_credential_binding_payload,
)
from mindroom.oauth.credential_lifecycle import (
    OAuthCredentialConflictError,
    OAuthCredentialContext,
    OAuthCredentialsStatus,
    exchange_and_store_oauth_credentials,
    load_oauth_credentials_snapshot,
    load_oauth_credentials_status,
    oauth_credentials_usable,
    oauth_verified_claim,
    refresh_oauth_credentials,
    resolve_oauth_credential_context,
)
from mindroom.oauth.registry import load_oauth_providers_for_snapshot
from mindroom.oauth.reset import (
    BrowserOAuthResetIntent,
    OAuthResetTargetError,
    lookup_browser_oauth_reset_intent,
    resolve_oauth_reset_target,
)
from mindroom.oauth.reset_execution import OAuthResetPreparationError, retire_and_reset_oauth_credentials
from mindroom.oauth.service import (
    consume_oauth_connect_token,
    lookup_oauth_connect_token,
    oauth_provider_service_account_configured,
    oauth_success_redirect_url,
)

if TYPE_CHECKING:
    from mindroom.api.credentials_target import RequestCredentialsTarget
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget, WorkerScope

router = APIRouter(prefix="/api/oauth", tags=["oauth"])
logger = get_logger(__name__)
_OAUTH_COMPLETE_MESSAGE_TYPE = "mindroom:oauth-complete"
_OAUTH_STALE_CONNECTION_MESSAGE = (
    "This OAuth connection changed before the request completed. Start the connection again from the dashboard."
)
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
    client_config_service: str | None = None
    client_config_redirect_uri_supported: bool = False
    connected: bool
    has_client_config: bool
    has_custom_client_config: bool = False
    has_service_account_config: bool = False
    reset_required: bool = False
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


def _dynamic_client_matches_hosted_callback(
    resolution: OAuthClientConfigResolution,
    provider: OAuthProvider,
    runtime_paths: RuntimePaths,
) -> bool:
    """Return whether a dynamically registered client has its exact configured HTTPS callback."""
    if not resolution.dynamically_registered:
        return False
    expected_redirect_uri = provider.default_redirect_uri(runtime_paths)
    expected_callback = urlparse(expected_redirect_uri)
    return (
        expected_callback.scheme.casefold() == "https"
        and expected_callback.hostname is not None
        and not is_oauth_loopback_hostname(expected_callback.hostname)
        and resolution.config.redirect_uri == expected_redirect_uri
        and resolution.registered_redirect_uri == expected_redirect_uri
    )


async def _client_config_resolution_for_request(
    request: Request,
    provider: OAuthProvider,
    runtime_paths: RuntimePaths,
    *,
    reject_remote_provisioned: bool,
) -> OAuthClientConfigResolution | None:
    """Resolve an OAuth client that can return to the requesting browser host."""
    try:
        resolution = await provider.client_config_resolution_async(runtime_paths)
    except OAuthProviderError as exc:
        logger.warning(
            "oauth_client_config_resolution_failed",
            provider_id=provider.id,
            error_type=type(exc).__name__,
        )
        raise HTTPException(status_code=503, detail="OAuth client configuration could not be resolved") from exc
    if (
        resolution is None
        or resolution.custom
        or is_oauth_loopback_hostname(request.url.hostname)
        or _dynamic_client_matches_hosted_callback(resolution, provider, runtime_paths)
    ):
        return resolution
    if reject_remote_provisioned:
        detail = (
            "The provisioned OAuth client is available only when MindRoom is opened on localhost. "
            "Set MINDROOM_PUBLIC_URL (or MINDROOM_BASE_URL) and configure a custom OAuth client for remote access."
        )
        raise HTTPException(status_code=503, detail=detail)
    return None


def _resolve_oauth_credentials_target(
    request: Request,
    provider: OAuthProvider,
    *,
    agent_name: str | None,
    execution_scope_override_provided: bool | None = None,
    execution_scope_override: WorkerScope | None = None,
) -> RequestCredentialsTarget:
    if provider.requester_scoped_credentials:
        # Requester-scoped providers always bind to the user's store, so agent scope overrides cannot change it.
        target = resolve_requester_credentials_target(
            request,
            agent_name=agent_name,
            service_names=(provider.credential_service,),
        )
    else:
        target = resolve_request_credentials_target(
            request,
            agent_name=agent_name,
            service_names=(provider.credential_service,),
            execution_scope_override_provided=execution_scope_override_provided,
            execution_scope_override=execution_scope_override,
            allow_private_scopes=True,
        )
    snapshot = config_lifecycle.bind_current_request_snapshot(request)
    context = resolve_oauth_credential_context(
        provider,
        target.runtime_paths,
        target.base_manager,
        worker_target_for_credentials_target(target),
        execution_identity=target.execution_identity,
        authorization=snapshot.runtime_config.authorization if snapshot.runtime_config is not None else None,
    )
    worker_target = context.worker_target
    if worker_target is None or worker_target.worker_key is None:
        return target
    return replace(
        target,
        target_manager=target.base_manager.for_worker(worker_target.worker_key),
        worker_scope=worker_target.worker_scope,
        agent_name=worker_target.routing_agent_name,
        execution_identity=worker_target.execution_identity,
    )


async def _issue_authorization_url(
    request: Request,
    provider: OAuthProvider,
    runtime_paths: RuntimePaths,
    *,
    agent_name: str | None,
    connect_token: str | None = None,
) -> OAuthConnectResponse:
    await _client_config_resolution_for_request(request, provider, runtime_paths, reject_remote_provisioned=True)
    connect_target = None
    if connect_token:
        try:
            connect_target = lookup_oauth_connect_token(provider, runtime_paths, connect_token)
        except OAuthProviderError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _verify_connect_target_authorized(request, connect_target.requester_id, runtime_paths)
        _verify_connect_target_query(connect_target.binding, agent_name, request.query_params.get("execution_scope"))
    target = _resolve_oauth_credentials_target(request, provider, agent_name=agent_name)
    if connect_target is not None:
        _verify_connect_target_binding(connect_target.binding, provider, worker_target_for_credentials_target(target))
    try:
        code_verifier = provider.issue_pkce_code_verifier()
        state = issue_pending_oauth_state(
            request,
            provider.id,
            agent_name,
            payload=await _target_binding_payload(provider, target),
            code_verifier=code_verifier,
        )
        auth_url = await provider.authorization_uri_async(
            target.runtime_paths,
            state=state,
            code_verifier=code_verifier,
        )
    except OAuthProviderError as exc:
        logger.warning(
            "oauth_authorization_start_failed",
            provider_id=provider.id,
            error_type=type(exc).__name__,
        )
        raise HTTPException(status_code=503, detail="OAuth authorization could not be started") from exc
    if connect_token and connect_target is not None:
        try:
            consume_oauth_connect_token(provider, runtime_paths, connect_token, expected_target=connect_target)
        except OAuthProviderError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    completion_origin = _oauth_success_origin(provider, runtime_paths)
    return OAuthConnectResponse(provider=provider.id, auth_url=auth_url, completion_origin=completion_origin)


async def _target_binding_payload(provider: OAuthProvider, target: RequestCredentialsTarget) -> dict[str, str]:
    snapshot = await load_oauth_credentials_snapshot(_credential_context(provider, target.runtime_paths, target))
    binding = oauth_credential_binding(provider, worker_target_for_credentials_target(target))
    payload = oauth_credential_binding_payload(binding)
    payload["connection_generation"] = snapshot.connection_generation
    return payload


def _verify_connect_target_authorized(request: Request, requester_id: str | None, runtime_paths: RuntimePaths) -> None:
    dashboard_identity = build_dashboard_execution_identity(request, "oauth", runtime_paths=runtime_paths)
    snapshot = config_lifecycle.bind_current_request_snapshot(request)
    if dashboard_identity.requester_id and snapshot.runtime_config is not None:
        dashboard_identity = replace(
            dashboard_identity,
            requester_id=snapshot.runtime_config.authorization.resolve_alias(dashboard_identity.requester_id),
        )
    if requester_id and requester_id != dashboard_identity.requester_id:
        raise HTTPException(status_code=403, detail="OAuth link does not belong to the current user")


def _verify_connect_target_query(
    binding: OAuthCredentialBinding,
    agent_name: str | None,
    execution_scope: str | None,
) -> None:
    expected_scope = "" if binding.worker_scope == "unscoped" else binding.worker_scope
    if (agent_name or "") != (binding.requested_agent_name or "") or (execution_scope or "") != expected_scope:
        raise HTTPException(status_code=400, detail="OAuth link target does not match this request")


def _verify_connect_target_binding(
    binding: OAuthCredentialBinding,
    provider: OAuthProvider,
    worker_target: ResolvedWorkerTarget | None,
) -> None:
    if binding != oauth_credential_binding(provider, worker_target):
        raise HTTPException(status_code=400, detail="OAuth link target does not match this request")


def _verify_browser_reset_intent(
    request: Request,
    provider: OAuthProvider,
    intent: BrowserOAuthResetIntent,
    runtime_paths: RuntimePaths,
    *,
    agent_name: str | None,
    execution_scope: str | None,
) -> OAuthCredentialContext:
    """Reauthorize and resolve the exact browser reset target."""
    _verify_connect_target_authorized(request, intent.requester_id, runtime_paths)
    _verify_connect_target_query(intent.binding, agent_name, execution_scope)
    snapshot = config_lifecycle.bind_current_request_snapshot(request)
    config = snapshot.runtime_config
    if config is None:
        raise HTTPException(status_code=503, detail="OAuth reset requires an active configuration")
    if not agent_name:
        raise HTTPException(status_code=403, detail="The current requester cannot manage this agent's credentials")
    identity = require_agent_credential_management_authorized(
        request,
        config=config,
        runtime_paths=runtime_paths,
        agent_name=agent_name,
    )
    try:
        target = resolve_oauth_reset_target(
            provider.id,
            agent_name=agent_name,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=identity,
        )
    except OAuthResetTargetError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _verify_connect_target_binding(intent.binding, provider, target.worker_target)
    return target.credential_context


async def _verify_pending_target_binding(
    provider: OAuthProvider,
    pending_payload: dict[str, str] | None,
    target: RequestCredentialsTarget,
) -> None:
    if pending_payload != await _target_binding_payload(provider, target):
        raise HTTPException(status_code=409, detail=_OAUTH_STALE_CONNECTION_MESSAGE)


def _pending_connection_generation(pending_payload: dict[str, str] | None) -> str:
    generation = pending_payload.get("connection_generation") if pending_payload is not None else None
    if not isinstance(generation, str) or not generation:
        raise HTTPException(status_code=409, detail=_OAUTH_STALE_CONNECTION_MESSAGE)
    return generation


def _credential_context(
    provider: OAuthProvider,
    runtime_paths: RuntimePaths,
    target: RequestCredentialsTarget,
) -> OAuthCredentialContext:
    return resolve_oauth_credential_context(
        provider,
        runtime_paths,
        target.base_manager,
        worker_target_for_credentials_target(target),
    )


def _script_json(value: object) -> str:
    return json.dumps(value).replace("</", "<\\/")


def _oauth_browser_error_response(message: str, *, status_code: int) -> HTMLResponse:
    """Render a browser-readable OAuth error without exposing a raw API payload."""
    title = "OAuth connection changed" if status_code == 409 else "OAuth reset unavailable"
    escaped_message = escape(message)
    return HTMLResponse(
        f"""<!doctype html>
<html lang="en">
  <head><meta charset="utf-8"><title>{title}</title></head>
  <body>
    <h1>{title}</h1>
    <p>{escaped_message}</p>
  </body>
</html>""",
        status_code=status_code,
    )


def _oauth_success_origin(provider: OAuthProvider, runtime_paths: RuntimePaths) -> str:
    success_url = oauth_success_redirect_url(provider, runtime_paths)
    parsed = urlparse(success_url)
    return f"{parsed.scheme}://{parsed.netloc}"


@router.post("/{provider_id}/connect")
async def connect(provider_id: str, request: Request, agent_name: str | None = None) -> OAuthConnectResponse:
    """Start a provider OAuth flow and return the external authorization URL."""
    await _require_oauth_api_user(request)
    provider, runtime_paths = _load_provider(request, provider_id)
    return await _issue_authorization_url(request, provider, runtime_paths, agent_name=agent_name)


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
    response = await _issue_authorization_url(
        request,
        provider,
        runtime_paths,
        agent_name=agent_name,
        connect_token=connect_token,
    )
    return RedirectResponse(url=response.auth_url)


def _browser_reset_intent(
    provider: OAuthProvider,
    runtime_paths: RuntimePaths,
    reset_token: str,
) -> BrowserOAuthResetIntent:
    try:
        return lookup_browser_oauth_reset_intent(provider, runtime_paths, reset_token)
    except (OAuthProviderError, OAuthResetTargetError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{provider_id}/reset", response_class=HTMLResponse)
async def confirm_reset(
    provider_id: str,
    request: Request,
    reset_token: str,
    agent_name: str | None = None,
    execution_scope: str | None = None,
) -> Response:
    """Show the authenticated human confirmation for one scoped reset."""
    login_redirect = await _require_oauth_browser_user(request)
    if login_redirect is not None:
        return login_redirect
    try:
        provider, runtime_paths = _load_provider(request, provider_id)
        intent = _browser_reset_intent(provider, runtime_paths, reset_token)
        _verify_browser_reset_intent(
            request,
            provider,
            intent,
            runtime_paths,
            agent_name=agent_name,
            execution_scope=execution_scope,
        )
    except HTTPException as exc:
        return _oauth_browser_error_response(str(exc.detail), status_code=exc.status_code)
    display_name = escape(provider.display_name)
    target_agent = escape(intent.binding.requested_agent_name or "unknown")
    target_scope = escape(intent.binding.worker_scope)
    return HTMLResponse(
        f"""<!doctype html>
<html lang="en">
  <head><meta charset="utf-8"><title>Reset {display_name}</title></head>
  <body>
    <h1>Reset and reconnect {display_name}</h1>
    <p>This removes the current scoped credential, then opens the provider authorization page.</p>
    <p>Target agent: <strong>{target_agent}</strong>.</p>
    <p>Credential scope: <strong>{target_scope} scope</strong>.</p>
    <form method="post"><button type="submit">Reset and reconnect</button></form>
  </body>
</html>""",
    )


@router.post("/{provider_id}/reset")
async def reset_and_authorize(
    provider_id: str,
    request: Request,
    reset_token: str,
    agent_name: str | None = None,
    execution_scope: str | None = None,
) -> Response:
    """Commit one browser-confirmed reset and continue into provider authorization."""
    await _require_oauth_api_user(request)
    provider, runtime_paths = _load_provider(request, provider_id)
    intent = _browser_reset_intent(provider, runtime_paths, reset_token)
    try:
        context = _verify_browser_reset_intent(
            request,
            provider,
            intent,
            runtime_paths,
            agent_name=agent_name,
            execution_scope=execution_scope,
        )
    except HTTPException as exc:
        if exc.status_code == 409:
            return _oauth_browser_error_response(str(exc.detail), status_code=409)
        raise
    snapshot = config_lifecycle.bind_current_request_snapshot(request)
    config = snapshot.runtime_config
    if config is None:
        raise HTTPException(status_code=503, detail="OAuth reset requires an active configuration")
    try:
        deleted = await retire_and_reset_oauth_credentials(
            context,
            mcp_servers=config.mcp_servers,
            operation_id=intent.operation_id,
            expected_connection_generation=intent.connection_generation,
        )
        authorization = await _issue_authorization_url(
            request,
            provider,
            runtime_paths,
            agent_name=agent_name,
        )
    except OAuthCredentialConflictError:
        return _oauth_browser_error_response(_OAUTH_STALE_CONNECTION_MESSAGE, status_code=409)
    except OAuthResetPreparationError as exc:
        logger.warning(
            "oauth_connection_reset_preparation_failed",
            provider_id=provider.id,
            error_type=type(exc).__name__,
        )
        raise HTTPException(status_code=503, detail="OAuth reset could not start safely") from exc
    logger.info(
        "oauth_connection_reset",
        provider_id=provider.id,
        agent_name=agent_name,
        credential_existed=deleted,
    )
    return RedirectResponse(url=authorization.auth_url, status_code=303)


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


async def _store_callback_credentials(
    request: Request,
    provider: OAuthProvider,
    runtime_paths: RuntimePaths,
    *,
    code: str,
    state: str,
) -> None:
    """Resolve and store one callback's exact credential target."""
    pending = consume_pending_oauth_request(request, provider.id, state)
    target = _resolve_oauth_credentials_target(
        request,
        provider,
        agent_name=pending.agent_name,
        execution_scope_override_provided=pending.execution_scope_override_provided,
        execution_scope_override=pending.execution_scope_override,
    )

    async def verify_and_store() -> None:
        await _verify_pending_target_binding(provider, pending.payload, target)
        await exchange_and_store_oauth_credentials(
            _credential_context(provider, runtime_paths, target),
            code,
            pending.code_verifier,
            expected_connection_generation=_pending_connection_generation(pending.payload),
        )

    await run_coroutine_until_complete(verify_and_store())


@router.get("/{provider_id}/callback")
async def callback(provider_id: str, request: Request) -> Response:
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
    try:
        await _store_callback_credentials(
            request,
            provider,
            runtime_paths,
            code=code,
            state=state,
        )
    except HTTPException as exc:
        if exc.status_code == 409:
            return _oauth_browser_error_response(str(exc.detail), status_code=409)
        raise
    except OAuthCredentialConflictError:
        return _oauth_browser_error_response(_OAUTH_STALE_CONNECTION_MESSAGE, status_code=409)
    except OAuthClaimValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OAuthProviderError as exc:
        logger.warning(
            "oauth_callback_provider_failed",
            provider_id=provider.id,
            error_type=type(exc).__name__,
        )
        raise HTTPException(status_code=400, detail="OAuth callback could not be completed") from exc
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
    target = _resolve_oauth_credentials_target(
        request,
        provider,
        agent_name=agent_name,
    )
    context = _credential_context(provider, runtime_paths, target)
    credential_status: OAuthCredentialsStatus = await load_oauth_credentials_status(context)
    credentials = credential_status.credentials or {}
    has_service_account_config = oauth_provider_service_account_configured(provider, runtime_paths)
    client_config_resolution = (
        provider.client_config_resolution(runtime_paths)
        if has_service_account_config
        else await _client_config_resolution_for_request(
            request,
            provider,
            runtime_paths,
            reject_remote_provisioned=False,
        )
    )
    has_client_config = client_config_resolution is not None
    credentials_usable = oauth_credentials_usable(provider, runtime_paths, credentials)
    if credentials_usable and has_client_config and not has_service_account_config:
        try:
            refreshed_credentials = await refresh_oauth_credentials(context)
        except OAuthProviderError as exc:
            logger.warning(
                "oauth_token_refresh_failed",
                provider_id=provider.id,
                error_type=type(exc).__name__,
            )
            credentials = (await load_oauth_credentials_snapshot(context)).credentials or {}
            credentials_usable = oauth_credentials_usable(provider, runtime_paths, credentials)
        else:
            credentials = refreshed_credentials or {}
            credentials_usable = oauth_credentials_usable(provider, runtime_paths, credentials)
    connected = has_service_account_config or credentials_usable
    if client_config_resolution is not None:
        client_config_service = client_config_resolution.service
    elif provider.all_client_config_services:
        client_config_service = provider.all_client_config_services[0]
    else:
        client_config_service = None
    client_config_redirect_uri_supported = (
        client_config_service is not None and client_config_service in provider.client_config_services
    )
    return OAuthStatusResponse(
        provider=provider.id,
        display_name=provider.display_name,
        credential_service=provider.credential_service,
        tool_config_service=provider.tool_config_service,
        client_config_service=client_config_service,
        client_config_redirect_uri_supported=client_config_redirect_uri_supported,
        connected=connected,
        has_client_config=has_client_config,
        has_custom_client_config=(client_config_resolution is not None and client_config_resolution.custom),
        has_service_account_config=has_service_account_config,
        reset_required=credential_status.reset_required,
        email=oauth_verified_claim(credentials, "email"),
        hosted_domain=oauth_verified_claim(credentials, "hd"),
        capabilities=list(provider.status_capabilities),
    )


@router.post("/{provider_id}/disconnect")
async def disconnect(provider_id: str, request: Request, agent_name: str | None = None) -> dict[str, str]:
    """Remove scoped OAuth credentials for one provider while preserving tool settings."""
    await _require_oauth_api_user(request)
    provider, runtime_paths = _load_provider(request, provider_id)
    target = _resolve_oauth_credentials_target(
        request,
        provider,
        agent_name=agent_name,
    )
    context = _credential_context(provider, runtime_paths, target)
    snapshot = config_lifecycle.bind_current_request_snapshot(request)
    config = snapshot.runtime_config

    try:
        await retire_and_reset_oauth_credentials(
            context,
            mcp_servers=config.mcp_servers if config is not None else {},
            operation_id=None,
        )
    except OAuthResetPreparationError as exc:
        logger.warning(
            "oauth_disconnect_preparation_failed",
            provider_id=provider.id,
            error_type=type(exc).__name__,
        )
        raise HTTPException(status_code=503, detail="OAuth disconnect could not start safely") from exc
    return {"status": "disconnected", "provider": provider.id}
