"""Assigned tools and service connections for eligible agents."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Annotated, cast

import nio
from aiohttp import ClientError
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict

from mindroom.api import config_lifecycle, oauth
from mindroom.api.auth import is_administrator, require_connections_user
from mindroom.api.connection_agents import (
    CONNECTIONS_HEADERS,
    ConnectionUserContext,
    masked_oauth_call,
    require_connections_same_origin,
    resolve_connection_agent,
    resolve_connection_user,
)
from mindroom.api.egress_credentials import EgressCredentialService, egress_services_for_agent
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.egress_broker.env import egress_broker_enabled
from mindroom.logging_config import get_logger
from mindroom.matrix.media import MatrixMediaUpstreamError, fetch_matrix_thumbnail, matrix_profile_avatar_uri
from mindroom.matrix.users import create_agent_http_client
from mindroom.oauth.credential_lifecycle import resolve_oauth_credential_context
from mindroom.oauth.registry import load_oauth_providers_for_snapshot
from mindroom.oauth.service import oauth_provider_service_account_configured
from mindroom.tool_system.catalog import listable_tool_metadata

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths
    from mindroom.oauth import OAuthProvider
    from mindroom.tool_system.catalog import ToolMetadata

router = APIRouter(prefix="/api/connections", tags=["connections"])
logger = get_logger(__name__)


class ConnectionService(BaseModel):
    """One available account connection, without credential storage details.

    `connected` is only filled in by the catalog route: it is what the status route would report to this viewer,
    read without refreshing the token, so a page can count connected accounts before it loads each status.
    """

    provider: str
    is_shared: bool
    can_manage: bool
    connected: bool = False
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
    mcp_available: bool


class AgentConnections(BaseModel):
    """Allowed services for one personal or managed shared agent.

    `egress_services` is a list for an agent whose egress credentials the user may use, empty when it has no
    services, so the portal can link every such agent to the services page. It is null for any other agent, for
    every agent while the egress broker is off, and for an agent whose egress state cannot be read.
    `managers` is a shared or unscoped agent's credential managers as Matrix IDs, as the egress listing reports them;
    it is empty for any other agent, and for an agent with none only administrators manage its credentials. While the
    egress broker is off, it is filled only for a user with a connection on the agent that they cannot change, whose
    row names who can.
    """

    agent_name: str
    agent_display_name: str
    is_shared: bool
    can_use: bool
    managers: list[str] = []
    mcp_available: bool
    services: list[ConnectionService]
    tools: list[ConnectionTool]
    egress_services: list[EgressCredentialService] | None = None


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


class ConnectionsIdentity(BaseModel):
    """Who the Connections pages are open for, and whether the administrator dashboard is open to them too.

    `egress_enabled` says whether the egress broker is on, so the pages link the Agent Keys page only then.
    `display_name` is the user's Matrix display name, or their localpart when the homeserver has none to give.
    """

    matrix_user_id: str
    display_name: str
    email: str | None
    is_administrator: bool
    egress_enabled: bool


class _EmptyMutation(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True)
class _Connections:
    runtime_paths: RuntimePaths
    user: ConnectionUserContext
    catalog: ConnectionsCatalog
    providers: dict[str, OAuthProvider]
    egress_enabled: bool


async def _connection_user(request: Request, response: Response) -> ConnectionUserContext:
    response.headers.update(CONNECTIONS_HEADERS)
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
    metadata = listable_tool_metadata(snapshot.runtime_paths, user.config)
    egress_enabled = egress_broker_enabled(snapshot.runtime_paths)
    agents = [
        _agent_connections(name, user, providers, metadata, egress_enabled=egress_enabled)
        for name in user.visible_agent_names
    ]
    return _Connections(
        runtime_paths=snapshot.runtime_paths,
        user=user,
        catalog=ConnectionsCatalog(agents=agents),
        providers=providers,
        egress_enabled=egress_enabled,
    )


def _agent_connections(
    agent_name: str,
    user: ConnectionUserContext,
    providers: dict[str, OAuthProvider],
    metadata: dict[str, ToolMetadata],
    *,
    egress_enabled: bool,
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
                mcp_available=tool.mcp_available,
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
    # The Agent Keys section names the managers of every shared agent; without it, only a row the user cannot change.
    names_managers = egress_enabled or any(not service.can_manage for service in services.values())
    return AgentConnections(
        agent_name=agent_name,
        agent_display_name=agent.display_name,
        is_shared=agent.private is None,
        can_use=agent_name in user.agent_names,
        managers=(
            list(agent.credential_managers) if names_managers and entity.execution_scope in {None, "shared"} else []
        ),
        mcp_available=agent_name in user.gateway_agent_names,
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


async def _agent_scope_status(
    request: Request,
    context: _Connections,
    agent_name: str,
    provider_id: str,
    *,
    listing: bool = False,
) -> oauth.OAuthStatusResponse:
    """Read a non-manager's connection for the status route and the catalog; `listing` skips refresh and bootstrap."""
    agent = resolve_connection_agent(context.user, agent_name)
    credential_context = resolve_oauth_credential_context(
        context.providers[provider_id],
        context.runtime_paths,
        get_runtime_credentials_manager(context.runtime_paths),
        agent.worker_target,
        execution_identity=agent.execution_identity,
        config=agent.config,
    )
    return await oauth.connection_status(request, credential_context, refresh=not listing, bootstrap_client=not listing)


async def _service_connected(
    request: Request,
    context: _Connections,
    agent_name: str,
    service: ConnectionService,
) -> bool:
    """Say whether the status route would report the service connected, without refreshing its token.

    It reads state the way that route does, so a viewer sees the same answer in the catalog and in the expanded row.
    It never bootstraps an OAuth client over the network, which every service of the catalog would otherwise attempt:
    a provider with no stored client reads as not connected. Any failure also reads as not connected: one
    unreadable service must not fail the catalog.
    """
    try:
        if service.can_manage:
            result = await oauth.authenticated_connection_status(
                service.provider,
                request,
                agent_name=agent_name,
                refresh=False,
                bootstrap_client=False,
            )
        else:
            result = await _agent_scope_status(request, context, agent_name, service.provider, listing=True)
    except Exception as exc:
        logger.warning(
            "connection_catalog_status_unavailable",
            provider_id=service.provider,
            error_type=type(exc).__name__,
        )
        return False
    return oauth.personal_connection_view(result, can_manage=service.can_manage).connected


async def _egress_services(
    request: Request,
    agent_name: str,
    requester_id: str,
) -> list[EgressCredentialService] | None:
    """List an agent's egress rows for the catalog; one agent whose egress state cannot be read must not fail it."""
    try:
        return await egress_services_for_agent(request, agent_name, requester_id)
    except Exception as exc:
        logger.warning("connection_catalog_egress_unavailable", agent_name=agent_name, error_type=type(exc).__name__)
        return None


async def _matrix_display_name(user_id: str, runtime_paths: RuntimePaths) -> str:
    """Read the user's display name as the router, falling back to the localpart when it cannot be read.

    The lookup does not retry, so a homeserver that is down or rate limiting cannot hold up the rest of the identity.
    """
    fallback = user_id.removeprefix("@").split(":", 1)[0] or user_id
    try:
        client = create_agent_http_client(ROUTER_AGENT_NAME, runtime_paths)
    except ValueError:
        return fallback
    client.config = replace(client.config, max_timeouts=0, max_limit_exceeded=0)
    try:
        async with asyncio.timeout(2):
            response = await client.get_displayname(user_id)
    except (ClientError, TimeoutError):
        return fallback
    finally:
        await client.close()
    return (isinstance(response, nio.ProfileGetDisplayNameResponse) and response.displayname) or fallback


@router.get("/me")
async def me(request: Request, response: Response) -> ConnectionsIdentity:
    """Return the signed-in identity for the Connections pages' header.

    Any signed user gets an answer, with or without agents, so both pages can show
    "Signed in as", link the dashboard to administrators, and link the Agent Keys page while the broker is on.
    """
    response.headers.update(CONNECTIONS_HEADERS)
    auth_user = await require_connections_user(request)
    if request.query_params:
        raise HTTPException(400, "Query parameters are not accepted", headers=CONNECTIONS_HEADERS)
    snapshot = config_lifecycle.bind_current_request_snapshot(request)
    matrix_user_id = cast("str", auth_user["matrix_user_id"])
    email = auth_user.get("email")
    return ConnectionsIdentity(
        matrix_user_id=matrix_user_id,
        display_name=await _matrix_display_name(matrix_user_id, snapshot.runtime_paths),
        email=email if isinstance(email, str) and email else None,
        is_administrator=is_administrator(matrix_user_id, snapshot),
        egress_enabled=egress_broker_enabled(snapshot.runtime_paths),
    )


@router.get("")
async def catalog(request: Request, context: _ConnectionsContext) -> ConnectionsCatalog:
    """List allowed services with whether each is connected; the full status of an account is loaded per card.

    `connected` on a service is read like the status route reads it, without refreshing the token or bootstrapping an
    OAuth client, so the portal can count connected accounts before a card is opened; a service whose state cannot be
    read counts as not connected.

    Egress rows exist only while the egress broker is on. They follow the personal egress API's eligibility rule,
    only agents the user may use, and carry the stored OAuth state of their services. That state is read only here,
    so status, connect and disconnect of other providers never depend on it, and one service whose state cannot be
    read is shown as not connectable. Reading it skips the token refresh; resolving the OAuth client of an egress
    service's provider can still bootstrap it over the network once when none is stored.
    """
    requester_id = context.user.owner.requester_id
    agents = [
        agent.model_copy(
            update={
                "services": [
                    service.model_copy(
                        update={"connected": await _service_connected(request, context, agent.agent_name, service)},
                    )
                    for service in agent.services
                ],
                "egress_services": (
                    await _egress_services(request, agent.agent_name, requester_id) if context.egress_enabled else None
                ),
            },
        )
        for agent in context.catalog.agents
    ]
    return ConnectionsCatalog(agents=agents)


def _no_avatar() -> Response:
    """Answer 204 for an agent without a usable avatar, so the browser logs no failed image request."""
    return Response(status_code=204, headers=CONNECTIONS_HEADERS)


@router.get("/agents/{agent_name}/avatar")
async def avatar(agent_name: str, user: _ConnectionUserContext) -> Response:
    """Serve a visible agent's current Matrix thumbnail without exposing its token.

    An agent with no avatar to show gets 204: it has no Matrix account yet, no profile picture, or one that is
    not a usable raster image. Matrix failing to answer is a 502.
    """
    if agent_name not in user.visible_agent_names:
        raise HTTPException(404, "Agent is not available", headers=CONNECTIONS_HEADERS)
    try:
        client = create_agent_http_client(agent_name, user.runtime_paths)
    except ValueError:
        return _no_avatar()
    try:
        async with asyncio.timeout(5):
            profile = await client.get_profile(client.user_id)
            avatar_url = matrix_profile_avatar_uri(profile)
            if not avatar_url:
                return _no_avatar()
            thumbnail = await fetch_matrix_thumbnail(client, avatar_url)
            if thumbnail is None:
                return _no_avatar()
            body, content_type = thumbnail
            return Response(
                body,
                media_type=content_type,
                headers={**CONNECTIONS_HEADERS, "X-Content-Type-Options": "nosniff"},
            )
    except (ClientError, MatrixMediaUpstreamError, TimeoutError) as exc:
        raise HTTPException(502, "Avatar is temporarily unavailable", headers=CONNECTIONS_HEADERS) from exc
    except ValueError:
        return _no_avatar()
    finally:
        await client.close()


@router.get("/agents/{agent_name}/{provider_id}/status")
async def status(agent_name: str, provider_id: str, request: Request, context: _ConnectionsContext) -> ConnectionStatus:
    """Load status for one authorized agent and provider."""
    service = _service(context, agent_name, provider_id)
    call = (
        oauth.status(provider_id, request, agent_name=agent_name)
        if service.can_manage
        else _agent_scope_status(request, context, agent_name, provider_id)
    )
    result = await masked_oauth_call(call, "Connection status is unavailable")
    view = oauth.personal_connection_view(result, can_manage=service.can_manage)
    return ConnectionStatus(
        provider=provider_id,
        connected=view.connected,
        can_connect=view.can_connect,
        reset_required=view.reset_required,
        account_label=view.account_label,
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
    require_connections_same_origin(request, context.runtime_paths)
    provider = _require_management(context, agent_name, provider_id)
    if oauth_provider_service_account_configured(provider, context.runtime_paths):
        raise HTTPException(
            409,
            "Personal account linking is unavailable for this service",
            headers=CONNECTIONS_HEADERS,
        )
    return await masked_oauth_call(
        oauth.connect(provider_id, request, agent_name=agent_name),
        "Could not start account connection",
    )


@router.post("/agents/{agent_name}/{provider_id}/disconnect")
async def disconnect(
    agent_name: str,
    provider_id: str,
    request: Request,
    context: _ConnectionsContext,
    _body: _EmptyMutation,
) -> dict[str, str]:
    """Reset the authorized agent's scoped provider credentials."""
    require_connections_same_origin(request, context.runtime_paths)
    _require_management(context, agent_name, provider_id)
    return await masked_oauth_call(
        oauth.disconnect(provider_id, request, agent_name=agent_name),
        "Could not disconnect account",
    )
