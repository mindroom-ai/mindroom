"""Personal egress credentials API for non-admin users."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict

from mindroom.api import config_lifecycle
from mindroom.api.auth import require_connections_user
from mindroom.api.connection_agents import (
    CONNECTIONS_HEADERS,
    build_connection_agent_target,
    require_connections_same_origin,
)
from mindroom.authorization import is_sender_allowed_for_agent_credential_management, is_sender_allowed_for_responder
from mindroom.credentials import get_runtime_credentials_manager
from mindroom.egress_broker.secrets import delete_secret, save_secret, secret_status
from mindroom.requester_identity import resolve_human_requester_alias

if TYPE_CHECKING:
    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.credentials import CredentialsManager
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget


router = APIRouter(prefix="/api/connections/egress", tags=["egress-credentials"])


class EgressCredentialService(BaseModel):
    """One egress service with its management permissions and status."""

    name: str
    display_name: str
    description: str
    is_shared: bool
    can_manage: bool
    configured: bool
    updated_at: str | None


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


def _check_agent_eligibility(
    agent_name: str,
    requester_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    membership_index: AgentReplyMembershipIndex,  # type: ignore[name-defined]
) -> tuple[bool, bool] | None:
    """Check if agent is eligible for egress operations.

    Returns (is_shared, can_manage) if eligible, None if not.
    Eligibility requires: (shell or python) AND is_sender_allowed_for_responder.
    """
    entity = config.resolve_entity(agent_name)

    # Must have shell or python
    if not any(tool in entity.available_tools for tool in ("shell", "python")):
        return None

    # Must be allowed to use the agent
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

    return (is_shared, can_manage)


def build_service_for_agent(
    name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
    requester_id: str,
    manager: CredentialsManager,
) -> EgressCredentialService:
    """Build one service status with authorization and configuration info.

    Caller must have already verified eligibility via check_agent_eligibility.
    """
    service_config = config.egress_broker.services[name]

    # Get is_shared and can_manage from eligibility check
    # (We recompute here since build_service_for_agent doesn't take those as params)
    entity = config.resolve_entity(agent_name)
    is_shared = entity.execution_scope in {None, "shared"}
    can_manage = not is_shared or is_sender_allowed_for_agent_credential_management(
        requester_id,
        agent_name,
        config,
        runtime_paths,
    )

    # Always build a target for the agent to get the right scope
    target = build_connection_agent_target(config, runtime_paths, requester_id, agent_name)
    status = secret_status(manager, target, name)
    configured = status.configured
    updated_at = status.updated_at

    return EgressCredentialService(
        name=name,
        display_name=service_config.display_name or name.replace("_", " ").title(),
        description=service_config.description,
        is_shared=is_shared,
        can_manage=can_manage,
        configured=configured,
        updated_at=updated_at,
    )


def _load_egress_agents(request: Request, requester_id: str) -> list[EgressCredentialAgent]:
    """List all eligible agents with their egress services."""
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
        # Check eligibility: must have shell/python AND be allowed
        eligibility = _check_agent_eligibility(
            agent_name,
            human_requester,
            config,
            runtime_paths,
            memberships,
        )
        if eligibility is None:
            continue

        # Build services
        services = [
            build_service_for_agent(
                service_name,
                config,
                runtime_paths,
                agent_name,
                human_requester,
                manager,
            )
            for service_name in config.egress_broker.services
        ]

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
def list_egress_credentials(request: Request, requester_id: _EgressUser) -> EgressCredentialsResponse:
    """List all egress services for agents the user may use or manage."""
    agents = _load_egress_agents(request, requester_id)
    return EgressCredentialsResponse(agents=agents)


def _resolve_agent_and_service(
    request: Request,
    requester_id: str,
    agent_name: str,
    service: str,
    *,
    require_management: bool,
) -> tuple[Config, RuntimePaths, ResolvedWorkerTarget]:
    """Resolve and authorize one agent and service for mutation or query."""
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

    if require_management and not can_manage:
        raise HTTPException(403, "Credential management is required", headers=CONNECTIONS_HEADERS)

    # Always build target for the agent scope
    target = build_connection_agent_target(config, runtime_paths, human_requester, agent_name)

    return config, runtime_paths, target


@router.put("/agents/{agent_name}/{service}", status_code=204)
def put_egress_secret(
    request: Request,
    agent_name: str,
    service: str,
    body: PutSecretRequest,
    requester_id: _EgressUser,
) -> None:
    """Set or replace an egress service secret for one agent."""
    # Check same-origin before authorization lookup
    snapshot = config_lifecycle.bind_current_request_snapshot(request)
    runtime_paths = snapshot.runtime_paths
    require_connections_same_origin(
        request,
        runtime_paths,
        detail="Egress credential changes require a same-origin request",
    )

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
    # Check same-origin before authorization lookup
    snapshot = config_lifecycle.bind_current_request_snapshot(request)
    runtime_paths = snapshot.runtime_paths
    require_connections_same_origin(
        request,
        runtime_paths,
        detail="Egress credential changes require a same-origin request",
    )

    _config, runtime_paths, target = _resolve_agent_and_service(
        request,
        requester_id,
        agent_name,
        service,
        require_management=True,
    )

    manager = get_runtime_credentials_manager(runtime_paths)
    delete_secret(manager, target, service)
