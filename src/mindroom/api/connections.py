"""Personal OAuth connections for an operator-selected private agent."""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict

from mindroom.access_policy import resolve_responder_access
from mindroom.api import config_lifecycle, oauth
from mindroom.api.auth import require_personal_connections_user
from mindroom.api.dashboard_credential_scope import require_agent_oauth_connection_authorized
from mindroom.oauth.registry import load_oauth_providers_for_snapshot
from mindroom.oauth.service import oauth_provider_service_account_configured
from mindroom.tool_system.catalog import resolved_tool_metadata_for_runtime

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths
    from mindroom.oauth import OAuthProvider

router = APIRouter(prefix="/api/connections", tags=["connections"])
_PRIVATE_HEADERS = {"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer"}


class ConnectionService(BaseModel):
    """One available account connection, without credential storage details."""

    provider: str
    display_name: str
    description: str
    tools: list[str]


class ConnectionsCatalog(BaseModel):
    """Services available to the authenticated user's private agent."""

    agent_display_name: str
    services: list[ConnectionService]


class ConnectionStatus(BaseModel):
    """User-facing account status for one independent service card."""

    provider: str
    connected: bool
    can_connect: bool
    reset_required: bool
    account_label: str | None


class _EmptyMutation(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True)
class _PersonalConnections:
    agent_name: str
    runtime_paths: RuntimePaths
    catalog: ConnectionsCatalog
    providers: dict[str, OAuthProvider]


async def _personal_connections(request: Request, response: Response) -> _PersonalConnections:
    response.headers.update(_PRIVATE_HEADERS)
    snapshot = config_lifecycle.bind_current_request_snapshot(request)
    agent_name = (snapshot.runtime_paths.env_value("MINDROOM_CONNECTIONS_AGENT") or "").strip()
    if not agent_name:
        raise HTTPException(404, "Personal connections are not enabled", headers=_PRIVATE_HEADERS)
    await require_personal_connections_user(request)
    snapshot = config_lifecycle.bind_current_request_snapshot(request)
    agent_name = (snapshot.runtime_paths.env_value("MINDROOM_CONNECTIONS_AGENT") or "").strip()
    if not agent_name:
        raise HTTPException(404, "Personal connections are not enabled", headers=_PRIVATE_HEADERS)
    if request.query_params:
        raise HTTPException(400, "Connection target overrides are not accepted", headers=_PRIVATE_HEADERS)
    config = snapshot.runtime_config
    if config is None:
        raise HTTPException(503, "Personal connections are unavailable", headers=_PRIVATE_HEADERS)
    agent = config.agents.get(agent_name)
    if agent is None or agent.private is None or agent.private.per not in {"user", "user_agent"}:
        raise HTTPException(403, "Personal connections require a private agent", headers=_PRIVATE_HEADERS)
    identity = require_agent_oauth_connection_authorized(
        request,
        config=config,
        runtime_paths=snapshot.runtime_paths,
        agent_name=agent_name,
    )
    # A browser has no conversation membership context. Use explicit user grants.
    requester = identity.requester_id
    access = resolve_responder_access(config, agent_name)
    if requester is None or (
        requester not in config.administrators and not any(fnmatchcase(requester, pattern) for pattern in access.users)
    ):
        raise HTTPException(403, "Personal agent access is required", headers=_PRIVATE_HEADERS)
    providers = load_oauth_providers_for_snapshot(snapshot)
    metadata = resolved_tool_metadata_for_runtime(snapshot.runtime_paths, config, tolerate_plugin_load_errors=True)
    services: dict[str, ConnectionService] = {}
    for tool_name in config.resolve_entity(agent_name).available_tools:
        tool = metadata.get(tool_name)
        if tool is None or tool.auth_provider is None or tool.auth_provider not in providers:
            continue
        provider = providers[tool.auth_provider]
        service = services.setdefault(
            provider.id,
            ConnectionService(
                provider=provider.id,
                display_name=provider.display_name,
                description=tool.description,
                tools=[],
            ),
        )
        if tool_name not in service.tools:
            service.tools.append(tool_name)
    return _PersonalConnections(
        agent_name=agent_name,
        runtime_paths=snapshot.runtime_paths,
        catalog=ConnectionsCatalog(agent_display_name=agent.display_name, services=list(services.values())),
        providers={provider_id: providers[provider_id] for provider_id in services},
    )


_PersonalContext = Annotated[_PersonalConnections, Depends(_personal_connections)]


def _require_provider(context: _PersonalConnections, provider_id: str) -> OAuthProvider:
    provider = context.providers.get(provider_id)
    if provider is None:
        raise HTTPException(404, "Connection is not available", headers=_PRIVATE_HEADERS)
    return provider


def _require_same_origin(request: Request, context: _PersonalConnections) -> None:
    public_url = context.runtime_paths.env_value("MINDROOM_PUBLIC_URL") or str(request.base_url)
    parsed = urlsplit(public_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise HTTPException(403, "Personal connections require an HTTPS public origin", headers=_PRIVATE_HEADERS)
    expected = f"{parsed.scheme}://{parsed.netloc}"
    if request.headers.get("origin") != expected or request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "Connection changes require a same-origin request", headers=_PRIVATE_HEADERS)


@router.get("")
async def catalog(context: _PersonalContext) -> ConnectionsCatalog:
    """List allowed services without waiting for any upstream account status."""
    return context.catalog


@router.get("/{provider_id}/status")
async def status(provider_id: str, request: Request, context: _PersonalContext) -> ConnectionStatus:
    """Load only this user's status for one allowed provider."""
    _require_provider(context, provider_id)
    try:
        result = await oauth.status(provider_id, request, agent_name=context.agent_name)
    except HTTPException as exc:
        raise HTTPException(exc.status_code, "Connection status is unavailable", headers=_PRIVATE_HEADERS) from exc
    # Shared service accounts are runtime configuration, never a personal account.
    personal = not result.has_service_account_config
    return ConnectionStatus(
        provider=provider_id,
        connected=result.connected and personal,
        can_connect=result.has_client_config and personal,
        reset_required=result.reset_required,
        account_label=result.email if personal else None,
    )


@router.post("/{provider_id}/connect")
async def connect(
    provider_id: str,
    request: Request,
    context: _PersonalContext,
    _body: _EmptyMutation,
) -> oauth.OAuthConnectResponse:
    """Start existing OAuth state handling with a server-derived private target."""
    _require_same_origin(request, context)
    provider = _require_provider(context, provider_id)
    if oauth_provider_service_account_configured(provider, context.runtime_paths):
        raise HTTPException(409, "Personal account linking is unavailable for this service", headers=_PRIVATE_HEADERS)
    try:
        return await oauth.connect(provider_id, request, agent_name=context.agent_name)
    except HTTPException as exc:
        raise HTTPException(exc.status_code, "Could not start account connection", headers=_PRIVATE_HEADERS) from exc


@router.post("/{provider_id}/disconnect")
async def disconnect(
    provider_id: str,
    request: Request,
    context: _PersonalContext,
    _body: _EmptyMutation,
) -> dict[str, str]:
    """Reset only the authenticated user's scoped provider credentials."""
    _require_same_origin(request, context)
    _require_provider(context, provider_id)
    try:
        return await oauth.disconnect(provider_id, request, agent_name=context.agent_name)
    except HTTPException as exc:
        raise HTTPException(exc.status_code, "Could not disconnect account", headers=_PRIVATE_HEADERS) from exc
