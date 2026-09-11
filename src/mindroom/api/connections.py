"""OAuth connections for a personal agent and authorized shared agents."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, cast
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict

from mindroom.api import config_lifecycle, oauth
from mindroom.api.auth import require_personal_connections_user
from mindroom.api.personal_agent import resolve_personal_agent
from mindroom.authorization import is_sender_allowed_for_agent_credential_management
from mindroom.oauth.registry import load_oauth_providers_for_snapshot
from mindroom.oauth.service import oauth_provider_service_account_configured
from mindroom.tool_system.catalog import resolved_tool_metadata_for_runtime

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.oauth import OAuthProvider
    from mindroom.tool_system.catalog import ToolMetadata

router = APIRouter(prefix="/api/connections", tags=["connections"])
_PRIVATE_HEADERS = {"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer"}


class ConnectionService(BaseModel):
    """One available account connection, without credential storage details."""

    provider: str
    display_name: str
    description: str
    tools: list[str]


class AgentConnections(BaseModel):
    """Allowed services for one personal or managed shared agent."""

    agent_name: str
    agent_display_name: str
    is_shared: bool
    services: list[ConnectionService]


class ConnectionsCatalog(BaseModel):
    """Agents whose connections the authenticated user may manage."""

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
    catalog: ConnectionsCatalog
    providers: dict[str, OAuthProvider]


async def _connections(request: Request, response: Response) -> _Connections:
    response.headers.update(_PRIVATE_HEADERS)
    snapshot = config_lifecycle.bind_current_request_snapshot(request)
    agent_name = (snapshot.runtime_paths.env_value("MINDROOM_CONNECTIONS_AGENT") or "").strip()
    if not agent_name:
        raise HTTPException(404, "Personal connections are not enabled", headers=_PRIVATE_HEADERS)
    auth_user = await require_personal_connections_user(request)
    snapshot = config_lifecycle.bind_current_request_snapshot(request)
    agent_name = (snapshot.runtime_paths.env_value("MINDROOM_CONNECTIONS_AGENT") or "").strip()
    if not agent_name:
        raise HTTPException(404, "Personal connections are not enabled", headers=_PRIVATE_HEADERS)
    if request.query_params:
        raise HTTPException(400, "Connection target overrides are not accepted", headers=_PRIVATE_HEADERS)
    config = snapshot.runtime_config
    if config is None:
        raise HTTPException(503, "Connections are unavailable", headers=_PRIVATE_HEADERS)
    requester_id = cast("str", auth_user["matrix_user_id"])
    agent_names: list[str] = []
    try:
        personal = resolve_personal_agent(snapshot, requester_id, channel="matrix")
    except HTTPException as exc:
        if exc.status_code != 403:
            raise
    else:
        agent_names.append(personal.agent_name)
    agent_names.extend(
        name
        for name, agent in config.agents.items()
        if agent.private is None
        and is_sender_allowed_for_agent_credential_management(requester_id, name, config, snapshot.runtime_paths)
    )
    if not agent_names:
        raise HTTPException(403, "No connections are available for this account", headers=_PRIVATE_HEADERS)
    providers = load_oauth_providers_for_snapshot(snapshot)
    metadata = resolved_tool_metadata_for_runtime(snapshot.runtime_paths, config, tolerate_plugin_load_errors=True)
    agents = [_agent_connections(name, config, providers, metadata) for name in agent_names]
    return _Connections(
        runtime_paths=snapshot.runtime_paths,
        catalog=ConnectionsCatalog(agents=agents),
        providers=providers,
    )


def _agent_connections(
    agent_name: str,
    config: Config,
    providers: dict[str, OAuthProvider],
    metadata: dict[str, ToolMetadata],
) -> AgentConnections:
    """Group an authorized agent's available OAuth tools by provider."""
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
    agent = config.agents[agent_name]
    return AgentConnections(
        agent_name=agent_name,
        agent_display_name=agent.display_name,
        is_shared=agent.private is None,
        services=list(services.values()),
    )


_ConnectionsContext = Annotated[_Connections, Depends(_connections)]


def _require_provider(context: _Connections, agent_name: str, provider_id: str) -> OAuthProvider:
    for agent in context.catalog.agents:
        if agent.agent_name == agent_name and any(service.provider == provider_id for service in agent.services):
            return context.providers[provider_id]
    raise HTTPException(404, "Connection is not available", headers=_PRIVATE_HEADERS)


def _require_same_origin(request: Request, context: _Connections) -> None:
    public_url = context.runtime_paths.env_value("MINDROOM_PUBLIC_URL") or str(request.base_url)
    parsed = urlsplit(public_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise HTTPException(403, "Personal connections require an HTTPS public origin", headers=_PRIVATE_HEADERS)
    expected = f"{parsed.scheme}://{parsed.netloc}"
    if request.headers.get("origin") != expected or request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "Connection changes require a same-origin request", headers=_PRIVATE_HEADERS)


@router.get("")
async def catalog(context: _ConnectionsContext) -> ConnectionsCatalog:
    """List allowed services without waiting for any upstream account status."""
    return context.catalog


@router.get("/agents/{agent_name}/{provider_id}/status")
async def status(agent_name: str, provider_id: str, request: Request, context: _ConnectionsContext) -> ConnectionStatus:
    """Load status for one authorized agent and provider."""
    _require_provider(context, agent_name, provider_id)
    try:
        result = await oauth.status(provider_id, request, agent_name=agent_name)
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
    provider = _require_provider(context, agent_name, provider_id)
    if oauth_provider_service_account_configured(provider, context.runtime_paths):
        raise HTTPException(409, "Personal account linking is unavailable for this service", headers=_PRIVATE_HEADERS)
    try:
        return await oauth.connect(provider_id, request, agent_name=agent_name)
    except HTTPException as exc:
        raise HTTPException(exc.status_code, "Could not start account connection", headers=_PRIVATE_HEADERS) from exc


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
    _require_provider(context, agent_name, provider_id)
    try:
        return await oauth.disconnect(provider_id, request, agent_name=agent_name)
    except HTTPException as exc:
        raise HTTPException(exc.status_code, "Could not disconnect account", headers=_PRIVATE_HEADERS) from exc
