"""Personal egress credentials API for non-admin users."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from functools import partial
from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict

from mindroom.api import config_lifecycle, oauth
from mindroom.api.auth import require_connections_user
from mindroom.api.connection_agents import (
    CONNECTIONS_HEADERS,
    build_connection_agent_target,
    require_connections_same_origin,
)
from mindroom.authorization import is_sender_allowed_for_agent_credential_management, is_sender_allowed_for_responder
from mindroom.credentials import get_runtime_credentials_manager
from mindroom.egress_broker.oauth_source import oauth_status
from mindroom.egress_broker.secrets import (
    EgressServiceStatus,
    OAuthStatus,
    delete_secret,
    save_secret,
    service_status,
)
from mindroom.oauth.registry import load_oauth_providers_for_snapshot
from mindroom.oauth.service import oauth_provider_service_account_configured
from mindroom.requester_identity import resolve_human_requester_alias

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.api.config_lifecycle import ApiSnapshot
    from mindroom.config.egress_broker import EgressService
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.credentials import CredentialsManager
    from mindroom.oauth import OAuthProvider
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget


router = APIRouter(prefix="/api/connections/egress", tags=["egress-credentials"])


class EgressOAuthStatus(BaseModel):
    """Connection state of the OAuth account a service can use instead of an API key.

    `connected` means the broker would inject a personal access token. `service_account` marks a provider that a
    shared service account serves instead: the broker cannot inject that, and personal accounts are not connectable.
    """

    provider: str
    display_name: str
    connected: bool
    account_label: str | None
    can_connect: bool
    reset_required: bool
    service_account: bool


class EgressCredentialService(BaseModel):
    """One egress service with its management permissions and status.

    `configured` is true when either secret source is available and `updated_at` is the API key's timestamp, both
    kept for older clients. `active_source` says which source the broker uses: an explicit key wins over OAuth.
    """

    name: str
    display_name: str
    description: str
    is_shared: bool
    can_manage: bool
    configured: bool
    updated_at: str | None
    active_source: Literal["key", "oauth"] | None
    key_configured: bool
    key_updated_at: str | None
    oauth: EgressOAuthStatus | None


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


async def egress_oauth_status(
    result: oauth.OAuthStatusResponse,
    *,
    can_manage: bool,
    stored_connection: Callable[[], OAuthStatus | None],
) -> OAuthStatus:
    """Return a service's OAuth status as the broker sees it.

    Without a service account this is what the Connections portal shows the same viewer. A service account is not
    a token the broker can inject, so with one `connected` only reflects a stored personal connection that the
    broker would still use (`stored_connection` reads it like the broker does), and nothing is connectable.
    """
    if not result.has_service_account_config:
        view = oauth.personal_connection_view(result, can_manage=can_manage)
        return OAuthStatus(
            provider=result.provider,
            display_name=result.display_name,
            connected=view.connected,
            account_label=view.account_label,
            can_connect=view.can_connect,
            reset_required=view.reset_required,
        )
    stored = await asyncio.to_thread(stored_connection)
    return OAuthStatus(
        provider=result.provider,
        display_name=result.display_name,
        connected=stored is not None and stored.connected,
        account_label=None,
        can_connect=False,
        reset_required=result.reset_required,
        service_account=True,
    )


def unavailable_egress_oauth_status(provider: OAuthProvider) -> OAuthStatus:
    """Return the status shown while a provider's connection state cannot be loaded, so one failure spares the page."""
    return OAuthStatus(
        provider=provider.id,
        display_name=provider.display_name,
        connected=False,
        account_label=None,
        can_connect=False,
        reset_required=False,
    )


def egress_service_status(
    manager: CredentialsManager,
    target: ResolvedWorkerTarget | None,
    service: EgressService,
    name: str,
    oauth_part: OAuthStatus | None,
) -> EgressServiceStatus:
    """Combine a service's key status with its already loaded OAuth status; an explicit key wins."""
    return service_status(manager, target, service, name, oauth_status=lambda _provider_id, _target: oauth_part)


def source_status_fields(status: EgressServiceStatus) -> dict[str, Any]:
    """Return the response fields that describe a service's key and OAuth sources."""
    return {
        "configured": status.configured,
        "updated_at": status.key_updated_at,
        "active_source": status.active_source,
        "key_configured": status.key_configured,
        "key_updated_at": status.key_updated_at,
        "oauth": EgressOAuthStatus(**asdict(status.oauth)) if status.oauth is not None else None,
    }


def _service_oauth_provider(snapshot: ApiSnapshot, service_name: str) -> OAuthProvider | None:
    """Return the registry's provider for a configured egress service, or None without a known one."""
    config = snapshot.runtime_config
    service = config.egress_broker.services.get(service_name) if config is not None else None
    if service is None or service.oauth_provider is None:
        return None
    return load_oauth_providers_for_snapshot(snapshot).get(service.oauth_provider)


def egress_oauth_provider(
    request: Request,
    service_name: str,
    *,
    headers: Mapping[str, str] | None = None,
) -> OAuthProvider:
    """Return the OAuth provider of a configured egress service for the connect and disconnect routes.

    A service without a provider, or whose provider the registry does not know, is a 404. While a shared service
    account is configured for the provider, personal accounts are not managed here at all, which is a 409.
    """
    snapshot = config_lifecycle.bind_current_request_snapshot(request)
    config = snapshot.runtime_config
    if config is None or service_name not in config.egress_broker.services:
        raise HTTPException(404, "Service is not configured", headers=headers)
    provider = _service_oauth_provider(snapshot, service_name)
    if provider is None:
        raise HTTPException(404, "Service has no account connection", headers=headers)
    if oauth_provider_service_account_configured(provider, snapshot.runtime_paths):
        raise HTTPException(409, "Personal account linking is unavailable for this service", headers=headers)
    return provider


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

    The token refresh is skipped so a listing never waits on the provider; an unknown provider has no status.
    Requester-scoped connections (GitHub) belong to the requester, so every user of the agent manages their own.
    """
    provider = _service_oauth_provider(config_lifecycle.bind_current_request_snapshot(request), service_name)
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
    except HTTPException:
        return unavailable_egress_oauth_status(provider)
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
        await _personal_oauth_status(
            request,
            config,
            runtime_paths,
            manager,
            target,
            name,
            can_manage=can_manage,
        )
        if service.oauth_provider is not None
        else None
    )
    status = egress_service_status(manager, target, service, name, oauth_part)
    return EgressCredentialService(
        name=name,
        display_name=service.display_name or name.replace("_", " ").title(),
        description=service.description,
        is_shared=is_shared,
        can_manage=can_manage,
        **source_status_fields(status),
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
        provider = _service_oauth_provider(snapshot, service)
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
    provider = egress_oauth_provider(request, service, headers=CONNECTIONS_HEADERS)
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
    provider = egress_oauth_provider(request, service, headers=CONNECTIONS_HEADERS)
    try:
        return await oauth.disconnect(provider.id, request, agent_name=agent_name)
    except HTTPException as exc:
        raise HTTPException(
            exc.status_code,
            "Could not disconnect account",
            headers=CONNECTIONS_HEADERS,
        ) from exc
