"""Assigned tools and service connections for eligible agents."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, cast
from urllib.parse import urlsplit

import nio
from aiohttp import ClientError
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict

from mindroom.api import config_lifecycle, oauth
from mindroom.api.auth import require_connections_user
from mindroom.api.connection_agents import (
    CONNECTIONS_HEADERS,
    ConnectionUserContext,
    resolve_connection_agent,
    resolve_connection_user,
)
from mindroom.credentials import get_runtime_credentials_manager
from mindroom.matrix.users import create_agent_http_client
from mindroom.oauth.credential_lifecycle import resolve_oauth_credential_context
from mindroom.oauth.registry import load_oauth_providers_for_snapshot
from mindroom.oauth.service import oauth_provider_service_account_configured
from mindroom.tool_system.catalog import resolved_tool_metadata_for_runtime

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths
    from mindroom.oauth import OAuthProvider
    from mindroom.tool_system.catalog import ToolMetadata

router = APIRouter(prefix="/api/connections", tags=["connections"])


class ConnectionService(BaseModel):
    """One available account connection, without credential storage details."""

    provider: str
    is_shared: bool
    can_manage: bool
    display_name: str
    description: str
    icon: str | None
    tools: list[str]


class ConnectionTool(BaseModel):
    """Assigned toolkit metadata, independent of browser authentication support."""

    name: str
    display_name: str
    description: str
    icon: str | None
    provider: str | None
    requires_room_context: bool


class AgentConnections(BaseModel):
    """Allowed services for one personal or managed shared agent."""

    agent_name: str
    agent_display_name: str
    is_shared: bool
    can_use: bool
    services: list[ConnectionService]
    tools: list[ConnectionTool]


class ConnectionsCatalog(BaseModel):
    """Agents the authenticated user may use or manage connections for."""

    agents: list[AgentConnections]


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
class _Connections:
    runtime_paths: RuntimePaths
    user: ConnectionUserContext
    catalog: ConnectionsCatalog
    providers: dict[str, OAuthProvider]


async def _connection_user(request: Request, response: Response) -> ConnectionUserContext:
    response.headers.update(CONNECTIONS_HEADERS)
    snapshot = config_lifecycle.bind_current_request_snapshot(request)
    agent_name = (snapshot.runtime_paths.env_value("MINDROOM_CONNECTIONS_AGENT") or "").strip()
    if not agent_name:
        raise HTTPException(404, "Connections are not enabled", headers=CONNECTIONS_HEADERS)
    auth_user = await require_connections_user(request)
    snapshot = config_lifecycle.bind_current_request_snapshot(request)
    if request.query_params:
        raise HTTPException(400, "Connection target overrides are not accepted", headers=CONNECTIONS_HEADERS)
    requester_id = cast("str", auth_user["matrix_user_id"])
    user = resolve_connection_user(
        snapshot,
        requester_id,
        membership_index=config_lifecycle.app_state(request.app).agent_reply_memberships,
    )
    if not user.visible_agent_names:
        raise HTTPException(403, "No connections are available for this account", headers=CONNECTIONS_HEADERS)
    return user


_ConnectionUserContext = Annotated[ConnectionUserContext, Depends(_connection_user)]


async def _connections(request: Request, user: _ConnectionUserContext) -> _Connections:
    snapshot = config_lifecycle.bind_current_request_snapshot(request)
    providers = load_oauth_providers_for_snapshot(snapshot)
    metadata = resolved_tool_metadata_for_runtime(snapshot.runtime_paths, user.config, tolerate_plugin_load_errors=True)
    agents = [_agent_connections(name, user, providers, metadata) for name in user.visible_agent_names]
    return _Connections(
        runtime_paths=snapshot.runtime_paths,
        user=user,
        catalog=ConnectionsCatalog(agents=agents),
        providers=providers,
    )


def _agent_connections(
    agent_name: str,
    user: ConnectionUserContext,
    providers: dict[str, OAuthProvider],
    metadata: dict[str, ToolMetadata],
) -> AgentConnections:
    """List assigned toolkits and group their browser connections by provider."""
    services: dict[str, ConnectionService] = {}
    tools: list[ConnectionTool] = []
    config = user.config
    entity = config.resolve_entity(agent_name)
    for tool_name in entity.available_tools:
        tool = metadata.get(tool_name)
        if tool is None:
            continue
        provider = providers.get(tool.auth_provider) if tool.auth_provider is not None else None
        tools.append(
            ConnectionTool(
                name=tool_name,
                display_name=tool.display_name,
                description=tool.description,
                icon=tool.icon,
                provider=provider.id if provider is not None else None,
                requires_room_context=tool.requires_room_context,
            ),
        )
        if provider is None:
            continue
        shared = not provider.requester_scoped_credentials and entity.execution_scope in {None, "shared"}
        service = services.setdefault(
            provider.id,
            ConnectionService(
                provider=provider.id,
                is_shared=shared,
                can_manage=agent_name in user.credential_agent_names or not shared,
                display_name=provider.display_name,
                description=tool.description,
                icon=tool.icon,
                tools=[],
            ),
        )
        if tool_name not in service.tools:
            service.tools.append(tool_name)
    agent = config.agents[agent_name]
    return AgentConnections(
        agent_name=agent_name,
        agent_display_name=agent.display_name,
        is_shared=agent.private is None,
        can_use=agent_name in user.agent_names,
        services=list(services.values()),
        tools=tools,
    )


_ConnectionsContext = Annotated[_Connections, Depends(_connections)]


def _service(context: _Connections, agent_name: str, provider_id: str) -> ConnectionService:
    """Resolve the displayed service permissions before account operations."""
    for agent in context.catalog.agents:
        if agent.agent_name == agent_name:
            for service in agent.services:
                if service.provider == provider_id:
                    return service
    raise HTTPException(404, "Connection is not available", headers=CONNECTIONS_HEADERS)


def _require_management(context: _Connections, agent_name: str, provider_id: str) -> OAuthProvider:
    if not _service(context, agent_name, provider_id).can_manage:
        raise HTTPException(403, "Credential management is required", headers=CONNECTIONS_HEADERS)
    return context.providers[provider_id]


def _require_same_origin(request: Request, context: _Connections) -> None:
    public_url = context.runtime_paths.env_value("MINDROOM_PUBLIC_URL") or str(request.base_url)
    parsed = urlsplit(public_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise HTTPException(403, "Connections require an HTTPS public origin", headers=CONNECTIONS_HEADERS)
    expected = f"{parsed.scheme}://{parsed.netloc}"
    if request.headers.get("origin") != expected or request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(
            403,
            "Connection changes require a same-origin request",
            headers=CONNECTIONS_HEADERS,
        )


@router.get("")
async def catalog(context: _ConnectionsContext) -> ConnectionsCatalog:
    """List allowed services without waiting for any upstream account status."""
    return context.catalog


@router.get("/agents/{agent_name}/avatar")
async def avatar(agent_name: str, user: _ConnectionUserContext) -> Response:
    """Serve a visible agent's current Matrix thumbnail without exposing its token."""
    if agent_name not in user.visible_agent_names:
        raise HTTPException(404, "Agent is not available", headers=CONNECTIONS_HEADERS)
    try:
        client = create_agent_http_client(agent_name, user.runtime_paths)
    except ValueError as exc:
        raise HTTPException(404, "Avatar is not available", headers=CONNECTIONS_HEADERS) from exc
    try:
        async with asyncio.timeout(5):
            profile = await client.get_profile(client.user_id)
            if not isinstance(profile, nio.ProfileGetResponse) or not profile.avatar_url:
                raise HTTPException(404, "Avatar is not available", headers=CONNECTIONS_HEADERS)
            uri = urlsplit(profile.avatar_url)
            if (
                uri.scheme != "mxc"
                or not uri.netloc
                or not uri.path.strip("/")
                or uri.path.count("/") != 1
                or uri.query
                or uri.fragment
            ):
                raise HTTPException(404, "Avatar is not available", headers=CONNECTIONS_HEADERS)
            thumbnail = await client.thumbnail(uri.netloc, uri.path[1:], width=96, height=96)
            if (
                not isinstance(thumbnail, nio.ThumbnailResponse)
                or not isinstance(thumbnail.body, bytes)
                or not 0 < len(thumbnail.body) <= 1024 * 1024
                or thumbnail.content_type not in {"image/png", "image/jpeg", "image/gif", "image/webp"}
            ):
                raise HTTPException(404, "Avatar is not available", headers=CONNECTIONS_HEADERS)
            return Response(
                thumbnail.body,
                media_type=thumbnail.content_type,
                headers={**CONNECTIONS_HEADERS, "X-Content-Type-Options": "nosniff"},
            )
    except (ClientError, TimeoutError) as exc:
        raise HTTPException(502, "Avatar is temporarily unavailable", headers=CONNECTIONS_HEADERS) from exc
    except ValueError as exc:
        raise HTTPException(404, "Avatar is not available", headers=CONNECTIONS_HEADERS) from exc
    finally:
        await client.close()


@router.get("/agents/{agent_name}/{provider_id}/status")
async def status(agent_name: str, provider_id: str, request: Request, context: _ConnectionsContext) -> ConnectionStatus:
    """Load status for one authorized agent and provider."""
    service = _service(context, agent_name, provider_id)
    provider = context.providers[provider_id]
    try:
        if service.can_manage:
            result = await oauth.status(provider_id, request, agent_name=agent_name)
        else:
            agent = resolve_connection_agent(context.user, agent_name)
            credential_context = resolve_oauth_credential_context(
                provider,
                context.runtime_paths,
                get_runtime_credentials_manager(context.runtime_paths),
                agent.worker_target,
                execution_identity=agent.execution_identity,
                config=agent.config,
            )
            result = await oauth.connection_status(request, credential_context)
    except HTTPException as exc:
        raise HTTPException(
            exc.status_code,
            "Connection status is unavailable",
            headers=CONNECTIONS_HEADERS,
        ) from exc
    # Shared service accounts are runtime configuration, never a personal account.
    personal = not result.has_service_account_config
    return ConnectionStatus(
        provider=provider_id,
        connected=result.connected and (personal or not service.can_manage),
        can_connect=result.has_client_config and personal and service.can_manage,
        reset_required=result.reset_required,
        account_label=result.email if personal and service.can_manage else None,
    )


@router.post("/agents/{agent_name}/{provider_id}/connect")
async def connect(
    agent_name: str,
    provider_id: str,
    request: Request,
    context: _ConnectionsContext,
    _body: _EmptyMutation,
) -> oauth.OAuthConnectResponse:
    """Start existing OAuth state handling with an authorized agent target."""
    _require_same_origin(request, context)
    provider = _require_management(context, agent_name, provider_id)
    if oauth_provider_service_account_configured(provider, context.runtime_paths):
        raise HTTPException(
            409,
            "Personal account linking is unavailable for this service",
            headers=CONNECTIONS_HEADERS,
        )
    try:
        return await oauth.connect(provider_id, request, agent_name=agent_name)
    except HTTPException as exc:
        raise HTTPException(
            exc.status_code,
            "Could not start account connection",
            headers=CONNECTIONS_HEADERS,
        ) from exc


@router.post("/agents/{agent_name}/{provider_id}/disconnect")
async def disconnect(
    agent_name: str,
    provider_id: str,
    request: Request,
    context: _ConnectionsContext,
    _body: _EmptyMutation,
) -> dict[str, str]:
    """Reset the authorized agent's scoped provider credentials."""
    _require_same_origin(request, context)
    _require_management(context, agent_name, provider_id)
    try:
        return await oauth.disconnect(provider_id, request, agent_name=agent_name)
    except HTTPException as exc:
        raise HTTPException(
            exc.status_code,
            "Could not disconnect account",
            headers=CONNECTIONS_HEADERS,
        ) from exc
