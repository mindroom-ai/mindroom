"""Personal egress credentials API for non-admin users."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict

from mindroom.api import config_lifecycle, oauth
from mindroom.api.auth import require_connections_user
from mindroom.api.connection_agents import (
    CONNECTIONS_HEADERS,
    build_connection_agent_target,
    require_connections_same_origin,
)
from mindroom.api.egress_status import (
    EgressSourceStatus,
    egress_oauth_provider,
    egress_oauth_status,
    egress_service_status,
    service_oauth_provider,
    unavailable_egress_oauth_status,
)
from mindroom.authorization import is_sender_allowed_for_agent_credential_management, is_sender_allowed_for_responder
from mindroom.credentials import get_runtime_credentials_manager
from mindroom.egress_broker.oauth_source import oauth_status
from mindroom.egress_broker.secrets import OAuthStatus, delete_secret, save_secret
from mindroom.logging_config import get_logger
from mindroom.requester_identity import resolve_human_requester_alias

if TYPE_CHECKING:
    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.credentials import CredentialsManager
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget


router = APIRouter(prefix="/api/connections/egress", tags=["egress-credentials"])
logger = get_logger(__name__)


class EgressCredentialService(EgressSourceStatus):
    """One egress service with its management permissions and its key and OAuth sources."""

    name: str
    display_name: str
    description: str
    is_shared: bool
    can_manage: bool


class EgressCredentialAgent(BaseModel):
    """One agent with its egress services."""

    agent_name: str
    agent_display_name: str
    services: list[EgressCredentialService]


class EgressCredentialsResponse(BaseModel):
    """List of agents with their egress services."""

    agents: list[EgressCredentialAgent]


class PutSecretRequest(BaseModel):
    """Request body for setting a secret (write-only)."""

    model_config = ConfigDict(extra="forbid")

    secret: str


class _EmptyMutation(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _check_agent_eligibility(
    agent_name: str,
    requester_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    membership_index: AgentReplyMembershipIndex,
) -> tuple[bool, bool] | None:
    """Return ``(is_shared, can_manage)`` for an eligible agent, else ``None``.

    Eligibility requires a ``shell`` or ``python`` tool and permission to use the agent.
    Listing, portal catalog, and mutations all decide through this one rule.
    """
    entity = config.resolve_entity(agent_name)
    if not any(tool in entity.available_tools for tool in ("shell", "python")):
        return None
    if not is_sender_allowed_for_responder(
        requester_id,
        agent_name,
        None,
        config,
        runtime_paths,
        membership_index,
    ):
        return None
    is_shared = entity.execution_scope in {None, "shared"}
    can_manage = not is_shared or is_sender_allowed_for_agent_credential_management(
        requester_id,
        agent_name,
        config,
        runtime_paths,
    )
    return is_shared, can_manage


async def _personal_oauth_status(
    request: Request,
    config: Config,
    runtime_paths: RuntimePaths,
    manager: CredentialsManager,
    target: ResolvedWorkerTarget,
    service_name: str,
    *,
    can_manage: bool,
) -> OAuthStatus | None:
    """Load a service's provider connection state for one agent with the helper behind the portal's status route.

    The token refresh is skipped so a listing never waits on the provider; an unknown provider has no status, and
    any failure to read one provider's state shows that service as not connectable instead of failing the listing.
    Requester-scoped connections (GitHub) belong to the requester, so every user of the agent manages their own.
    """
    provider = service_oauth_provider(config_lifecycle.bind_current_request_snapshot(request), service_name)
    if provider is None:
        return None
    try:
        result = await oauth.agent_connection_status(
            request,
            provider,
            runtime_paths,
            manager,
            target,
            config=config,
            refresh=False,
        )
        return await egress_oauth_status(
            result,
            can_manage=can_manage or provider.requester_scoped_credentials,
            stored_connection=partial(
                oauth_status,
                provider.id,
                target,
                service=service_name,
                config=config,
                runtime_paths=runtime_paths,
                credentials_manager=manager,
            ),
        )
    except Exception as exc:
        logger.warning(
            "egress_oauth_status_unavailable",
            service=service_name,
            provider_id=provider.id,
            error_type=type(exc).__name__,
        )
        return unavailable_egress_oauth_status(provider)


async def _build_service_for_agent(
    request: Request,
    name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    target: ResolvedWorkerTarget,
    manager: CredentialsManager,
    eligibility: tuple[bool, bool],
) -> EgressCredentialService:
    """Build one service row from the eligibility result and the key and OAuth statuses."""
    is_shared, can_manage = eligibility
    service = config.egress_broker.services[name]
    oauth_part = (
        await _personal_oauth_status(request, config, runtime_paths, manager, target, name, can_manage=can_manage)
        if service.oauth_provider is not None
        else None
    )
    sources = await egress_service_status(manager, target, service, name, oauth_part)
    return EgressCredentialService(
        name=name,
        display_name=service.display_name or name.replace("_", " ").title(),
        description=service.description,
        is_shared=is_shared,
        can_manage=can_manage,
        **sources.model_dump(),
    )


async def egress_services_for_agent(
    request: Request,
    agent_name: str,
    requester_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    membership_index: AgentReplyMembershipIndex,
    manager: CredentialsManager,
) -> list[EgressCredentialService] | None:
    """List the brokered services for one agent, or ``None`` when the requester may not use it.

    ``requester_id`` must already be the resolved human requester alias.
    """
    eligibility = _check_agent_eligibility(agent_name, requester_id, config, runtime_paths, membership_index)
    if eligibility is None:
        return None
    target = build_connection_agent_target(config, runtime_paths, requester_id, agent_name)
    return [
        await _build_service_for_agent(request, name, config, runtime_paths, target, manager, eligibility)
        for name in config.egress_broker.services
    ]


async def _load_egress_agents(request: Request, requester_id: str) -> list[EgressCredentialAgent]:
    """List every agent the requester may use, with its egress services."""
    snapshot = config_lifecycle.bind_current_request_snapshot(request)
    config = snapshot.runtime_config
    if config is None:
        return []

    runtime_paths = snapshot.runtime_paths
    human_requester = resolve_human_requester_alias(requester_id, config, runtime_paths)
    memberships = config_lifecycle.app_state(request.app).agent_reply_memberships
    manager = get_runtime_credentials_manager(runtime_paths)

    agents: list[EgressCredentialAgent] = []
    for agent_name, agent in config.agents.items():
        services = await egress_services_for_agent(
            request,
            agent_name,
            human_requester,
            config,
            runtime_paths,
            memberships,
            manager,
        )
        if services is None:
            continue
        agents.append(
            EgressCredentialAgent(
                agent_name=agent_name,
                agent_display_name=agent.display_name,
                services=services,
            ),
        )
    return agents


async def _egress_user(request: Request, response: Response) -> str:
    """Authenticate and return the requester ID without requiring MINDROOM_CONNECTIONS_AGENT."""
    response.headers.update(CONNECTIONS_HEADERS)
    auth_user = await require_connections_user(request)
    if request.query_params:
        raise HTTPException(400, "Query parameters are not accepted", headers=CONNECTIONS_HEADERS)
    return str(auth_user["matrix_user_id"])


_EgressUser = Annotated[str, Depends(_egress_user)]


@router.get("", response_model=EgressCredentialsResponse)
async def list_egress_credentials(request: Request, requester_id: _EgressUser) -> EgressCredentialsResponse:
    """List egress services for every agent the user may use."""
    agents = await _load_egress_agents(request, requester_id)
    return EgressCredentialsResponse(agents=agents)


def _resolve_agent_and_service(
    request: Request,
    requester_id: str,
    agent_name: str,
    service: str,
    *,
    require_management: bool,
    requester_owned_oauth: bool = False,
) -> tuple[Config, RuntimePaths, ResolvedWorkerTarget]:
    """Resolve and authorize one agent and service for mutation or query.

    `requester_owned_oauth` is for connecting or disconnecting the service's OAuth account: a requester-scoped
    provider's connection belongs to the requester, so any user of the agent may manage it.
    """
    snapshot = config_lifecycle.bind_current_request_snapshot(request)
    config = snapshot.runtime_config
    if config is None or agent_name not in config.agents:
        raise HTTPException(404, "Agent is not available", headers=CONNECTIONS_HEADERS)
    if service not in config.egress_broker.services:
        raise HTTPException(404, "Service is not configured", headers=CONNECTIONS_HEADERS)

    runtime_paths = snapshot.runtime_paths
    human_requester = resolve_human_requester_alias(requester_id, config, runtime_paths)
    memberships = config_lifecycle.app_state(request.app).agent_reply_memberships

    # Check eligibility: must have shell/python AND be allowed
    eligibility = _check_agent_eligibility(
        agent_name,
        human_requester,
        config,
        runtime_paths,
        memberships,
    )
    if eligibility is None:
        raise HTTPException(404, "Agent is not available", headers=CONNECTIONS_HEADERS)

    _is_shared, can_manage = eligibility

    requester_owned = False
    if requester_owned_oauth:
        provider = service_oauth_provider(snapshot, service)
        requester_owned = provider is not None and provider.requester_scoped_credentials
    if require_management and not can_manage and not requester_owned:
        raise HTTPException(403, "Credential management is required", headers=CONNECTIONS_HEADERS)

    # Always build target for the agent scope
    target = build_connection_agent_target(config, runtime_paths, human_requester, agent_name)

    return config, runtime_paths, target


def _require_same_origin(request: Request) -> None:
    """Reject a mutation from another origin before any authorization lookup."""
    require_connections_same_origin(
        request,
        config_lifecycle.bind_current_request_snapshot(request).runtime_paths,
        detail="Egress credential changes require a same-origin request",
    )


@router.put("/agents/{agent_name}/{service}", status_code=204)
def put_egress_secret(
    request: Request,
    agent_name: str,
    service: str,
    body: PutSecretRequest,
    requester_id: _EgressUser,
) -> None:
    """Set or replace an egress service secret for one agent."""
    _require_same_origin(request)

    _config, runtime_paths, target = _resolve_agent_and_service(
        request,
        requester_id,
        agent_name,
        service,
        require_management=True,
    )

    manager = get_runtime_credentials_manager(runtime_paths)
    try:
        save_secret(manager, target, service, body.secret)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc), headers=CONNECTIONS_HEADERS) from exc


@router.delete("/agents/{agent_name}/{service}", status_code=204)
def delete_egress_secret(
    request: Request,
    agent_name: str,
    service: str,
    requester_id: _EgressUser,
) -> None:
    """Delete an egress service secret for one agent."""
    _require_same_origin(request)

    _config, runtime_paths, target = _resolve_agent_and_service(
        request,
        requester_id,
        agent_name,
        service,
        require_management=True,
    )

    manager = get_runtime_credentials_manager(runtime_paths)
    delete_secret(manager, target, service)


@router.post("/agents/{agent_name}/{service}/connect")
async def connect_egress_account(
    request: Request,
    agent_name: str,
    service: str,
    requester_id: _EgressUser,
    _body: _EmptyMutation,
) -> oauth.OAuthConnectResponse:
    """Start the OAuth flow of a service's provider for one agent's credential scope."""
    _require_same_origin(request)
    _resolve_agent_and_service(
        request,
        requester_id,
        agent_name,
        service,
        require_management=True,
        requester_owned_oauth=True,
    )
    provider = egress_oauth_provider(request, service, connecting=True, headers=CONNECTIONS_HEADERS)
    try:
        return await oauth.connect(provider.id, request, agent_name=agent_name)
    except HTTPException as exc:
        raise HTTPException(
            exc.status_code,
            "Could not start account connection",
            headers=CONNECTIONS_HEADERS,
        ) from exc


@router.post("/agents/{agent_name}/{service}/disconnect")
async def disconnect_egress_account(
    request: Request,
    agent_name: str,
    service: str,
    requester_id: _EgressUser,
    _body: _EmptyMutation,
) -> dict[str, str]:
    """Reset the OAuth connection of a service's provider for one agent's credential scope."""
    _require_same_origin(request)
    _resolve_agent_and_service(
        request,
        requester_id,
        agent_name,
        service,
        require_management=True,
        requester_owned_oauth=True,
    )
    provider = egress_oauth_provider(request, service, connecting=False, headers=CONNECTIONS_HEADERS)
    try:
        return await oauth.disconnect(provider.id, request, agent_name=agent_name)
    except HTTPException as exc:
        raise HTTPException(
            exc.status_code,
            "Could not disconnect account",
            headers=CONNECTIONS_HEADERS,
        ) from exc
