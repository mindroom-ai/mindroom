"""Personal egress credentials API for non-admin users."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, ValidationError

from mindroom.api import config_lifecycle, oauth
from mindroom.api.auth import require_connections_user
from mindroom.api.connection_agents import (
    CONNECTIONS_HEADERS,
    build_connection_agent_target,
    require_connections_same_origin,
)
from mindroom.api.egress_status import (
    AuditLogsResponse,
    EgressRuleSummary,
    EgressServiceSource,
    EgressSourceStatus,
    PresetsResponse,
    audit_logs_response,
    effective_services,
    egress_oauth_status,
    egress_service_status,
    oauth_provider_of_service,
    presets_response,
    rule_summaries,
    service_oauth_provider,
    service_source,
    unavailable_egress_oauth_status,
)
from mindroom.authorization import is_sender_allowed_for_agent_credential_management, is_sender_allowed_for_responder
from mindroom.config.egress_broker import EgressService
from mindroom.credentials import get_runtime_credentials_manager
from mindroom.egress_broker.oauth_source import oauth_status, shared_worker_oauth, shared_worker_unavailable_status
from mindroom.egress_broker.secrets import OAuthStatus, delete_secret, save_secret
from mindroom.egress_broker.service import active_audit_log
from mindroom.egress_broker.user_services import (
    MAX_SERVICE_BYTES,
    InactiveReason,
    UserServiceConflictError,
    delete_user_service,
    inactive_user_services,
    save_user_service,
    user_service_hosts_not_allowed,
)
from mindroom.logging_config import get_logger
from mindroom.oauth.registry import load_oauth_providers
from mindroom.requester_identity import resolve_human_requester_alias

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.config.egress_broker import EgressBrokerConfig
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.credentials import CredentialsManager
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget


router = APIRouter(prefix="/api/connections/egress", tags=["egress-credentials"])
logger = get_logger(__name__)

_MAX_PERSONAL_LOG_ROWS = 200


class EgressCredentialService(EgressSourceStatus):
    """One egress service with its management permissions and its key and OAuth sources.

    `source` is `config` for a service the administrator defines and `user` for one defined in the agent's own
    scope through the services routes. `rules` says where the service applies (host, port, path prefix), so a client
    can tell which services overlap; it carries no auth settings.
    """

    name: str
    display_name: str
    description: str
    is_shared: bool
    can_manage: bool
    source: EgressServiceSource
    rules: list[EgressRuleSummary]


class EgressInactiveService(BaseModel):
    """A service entry stored in the agent's scope that the broker ignores, so a user can still remove it.

    `shadowed` means an administrator's service has the same name now; `invalid` means the entry no longer
    validates as a service. Inactive entries count toward the scope's limits, and the delete route removes them.
    """

    name: str
    reason: InactiveReason


class EgressCredentialAgent(BaseModel):
    """One agent with its egress services.

    `shared` is true for a shared or unscoped agent, whose services and keys everyone using it shares, and
    `can_manage` says whether the caller may change them: their own agent, or a shared one they manage. Both are
    known even when the agent has no services yet. `inactive_services` lists the stored entries of the agent's
    scope that are not in `services`.
    """

    agent_name: str
    agent_display_name: str
    shared: bool
    can_manage: bool
    services: list[EgressCredentialService]
    inactive_services: list[EgressInactiveService]


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
    service: EgressService,
    *,
    can_manage: bool,
) -> OAuthStatus | None:
    """Load a service's provider connection state for one agent with the helper behind the portal's status route.

    The token refresh is skipped so a listing never waits on the provider; an unknown provider has no status, and
    any failure to read one provider's state, the worker backend check included, shows that service as not
    connectable instead of failing the listing.
    Requester-scoped connections (GitHub and Atlassian) belong to the requester, so every user of the agent manages
    their own, except where the broker refuses them in a shared sandbox (`shared_worker_oauth`).
    """
    provider = service_oauth_provider(config_lifecycle.bind_current_request_snapshot(request), service)
    if provider is None:
        return None
    try:
        access = shared_worker_oauth(
            provider,
            target,
            opted_in=service.oauth_on_shared_workers,
            runtime_paths=runtime_paths,
        )
        if access == "refused":
            return shared_worker_unavailable_status(provider, runtime_paths)
        result = await oauth.agent_connection_status(
            request,
            provider,
            runtime_paths,
            manager,
            target,
            config=config,
            refresh=False,
        )
        status = await egress_oauth_status(
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
    return replace(status, shared_worker_opt_in=access == "allowed")


async def _build_service_for_agent(
    request: Request,
    name: str,
    service: EgressService,
    source: EgressServiceSource,
    config: Config,
    runtime_paths: RuntimePaths,
    target: ResolvedWorkerTarget,
    manager: CredentialsManager,
    eligibility: tuple[bool, bool],
) -> EgressCredentialService:
    """Build one service row from the eligibility result and the key and OAuth statuses."""
    is_shared, can_manage = eligibility
    oauth_part = (
        await _personal_oauth_status(
            request,
            config,
            runtime_paths,
            manager,
            target,
            name,
            service,
            can_manage=can_manage,
        )
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
        source=source,
        rules=rule_summaries(service),
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
    *,
    eligibility: tuple[bool, bool] | None = None,
) -> list[EgressCredentialService] | None:
    """List the brokered services for one agent, or ``None`` when the requester may not use it.

    The services are the config's, then the ones the requester's scope defines for itself.
    ``requester_id`` must already be the resolved human requester alias. A caller that already ran the
    eligibility check passes its ``(is_shared, can_manage)`` result.
    """
    if eligibility is None:
        eligibility = _check_agent_eligibility(agent_name, requester_id, config, runtime_paths, membership_index)
    if eligibility is None:
        return None
    target = build_connection_agent_target(config, runtime_paths, requester_id, agent_name)
    # Reading the scope's own services touches the credential store, so it stays off the event loop.
    services = await asyncio.to_thread(effective_services, config, manager, target)
    return [
        await _build_service_for_agent(
            request,
            name,
            service,
            service_source(config, name),
            config,
            runtime_paths,
            target,
            manager,
            eligibility,
        )
        for name, service in services.items()
    ]


async def _inactive_services_for_agent(
    agent_name: str,
    requester_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    manager: CredentialsManager,
) -> list[EgressInactiveService]:
    """List the entries stored in the requester's scope for one agent that the broker ignores."""
    target = build_connection_agent_target(config, runtime_paths, requester_id, agent_name)
    # Reading the scope's own services touches the credential store, so it stays off the event loop.
    inactive = await asyncio.to_thread(inactive_user_services, config.egress_broker, manager, target)
    return [EgressInactiveService(name=entry.name, reason=entry.reason) for entry in inactive]


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
        eligibility = _check_agent_eligibility(agent_name, human_requester, config, runtime_paths, memberships)
        if eligibility is None:
            continue
        is_shared, can_manage = eligibility
        services = await egress_services_for_agent(
            request,
            agent_name,
            human_requester,
            config,
            runtime_paths,
            memberships,
            manager,
            eligibility=eligibility,
        )
        if services is None:
            continue
        agents.append(
            EgressCredentialAgent(
                agent_name=agent_name,
                agent_display_name=agent.display_name,
                shared=is_shared,
                can_manage=can_manage,
                services=services,
                inactive_services=await _inactive_services_for_agent(
                    agent_name,
                    human_requester,
                    config,
                    runtime_paths,
                    manager,
                ),
            ),
        )
    return agents


def _egress_user_accepting(*query_names: str) -> Callable[[Request, Response], Awaitable[str]]:
    """Build the dependency that authenticates the requester and rejects every query parameter but `query_names`.

    It needs no MINDROOM_CONNECTIONS_AGENT and returns the requester ID.
    """

    async def dependency(request: Request, response: Response) -> str:
        response.headers.update(CONNECTIONS_HEADERS)
        auth_user = await require_connections_user(request)
        if set(request.query_params) - set(query_names):
            detail = (
                f"Only the query parameters {', '.join(query_names)} are accepted"
                if query_names
                else "Query parameters are not accepted"
            )
            raise HTTPException(400, detail, headers=CONNECTIONS_HEADERS)
        return str(auth_user["matrix_user_id"])

    return dependency


_EgressUser = Annotated[str, Depends(_egress_user_accepting())]
_EgressLogUser = Annotated[str, Depends(_egress_user_accepting("agent_name", "limit"))]


@router.get("", response_model=EgressCredentialsResponse)
async def list_egress_credentials(request: Request, requester_id: _EgressUser) -> EgressCredentialsResponse:
    """List egress services for every agent the user may use."""
    agents = await _load_egress_agents(request, requester_id)
    return EgressCredentialsResponse(agents=agents)


@router.get("/presets", response_model=PresetsResponse)
async def get_presets(_requester_id: _EgressUser) -> PresetsResponse:
    """List the built-in service presets with the rules, login, and placeholders each one sets."""
    return presets_response()


@router.get("/logs", response_model=AuditLogsResponse)
def get_personal_logs(
    request: Request,
    requester_id: _EgressLogUser,
    agent_name: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query()] = _MAX_PERSONAL_LOG_ROWS,
) -> AuditLogsResponse:
    """Return the caller's own recent brokered requests, newest first, optionally for one agent.

    The log is filtered by the canonical requester, the identity the worker tokens carry, whatever else the query
    says, so no filter can show another user's rows, not even on an agent several users share. Returns 409 if the
    broker is not running.
    """
    audit = active_audit_log()
    if audit is None:
        raise HTTPException(409, "Egress broker is not running", headers=CONNECTIONS_HEADERS)
    snapshot = config_lifecycle.bind_current_request_snapshot(request)
    if snapshot.runtime_config is None:
        raise HTTPException(503, "Egress logs are unavailable", headers=CONNECTIONS_HEADERS)
    records = audit.query(
        requester_id=resolve_human_requester_alias(requester_id, snapshot.runtime_config, snapshot.runtime_paths),
        agent_name=agent_name or None,
        limit=max(1, min(limit, _MAX_PERSONAL_LOG_ROWS)),
    )
    return audit_logs_response(records)


@dataclass(frozen=True)
class _ResolvedAgent:
    """An agent the requester may use, with the worker target their credentials and services live under."""

    config: Config
    runtime_paths: RuntimePaths
    target: ResolvedWorkerTarget
    can_manage: bool


def _resolve_agent(request: Request, requester_id: str, agent_name: str) -> _ResolvedAgent:
    """Resolve one agent for the requester, or 404 when it is unknown or the requester may not use it."""
    snapshot = config_lifecycle.bind_current_request_snapshot(request)
    config = snapshot.runtime_config
    if config is None or agent_name not in config.agents:
        raise HTTPException(404, "Agent is not available", headers=CONNECTIONS_HEADERS)

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

    # Always build target for the agent scope
    target = build_connection_agent_target(config, runtime_paths, human_requester, agent_name)
    return _ResolvedAgent(config, runtime_paths, target, can_manage=eligibility[1])


def _resolve_agent_and_service(
    request: Request,
    requester_id: str,
    agent_name: str,
    service_name: str,
    *,
    require_management: bool,
    requester_owned_oauth: bool = False,
) -> tuple[_ResolvedAgent, EgressService]:
    """Resolve and authorize one agent and one of its services for mutation or query.

    The service is one of the config's or one the requester's scope defines for itself, so a name that exists only
    in another requester's scope is a 404.
    `requester_owned_oauth` is for connecting or disconnecting the service's OAuth account: a requester-scoped
    provider's connection belongs to the requester, so any user of the agent may manage it.
    """
    agent = _resolve_agent(request, requester_id, agent_name)
    manager = get_runtime_credentials_manager(agent.runtime_paths)
    service = effective_services(agent.config, manager, agent.target).get(service_name)
    if service is None:
        raise HTTPException(404, "Service is not configured", headers=CONNECTIONS_HEADERS)

    requester_owned = False
    if requester_owned_oauth:
        provider = service_oauth_provider(config_lifecycle.bind_current_request_snapshot(request), service)
        requester_owned = provider is not None and provider.requester_scoped_credentials
    if require_management and not agent.can_manage and not requester_owned:
        raise HTTPException(403, "Credential management is required", headers=CONNECTIONS_HEADERS)

    return agent, service


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

    agent, _service = _resolve_agent_and_service(
        request,
        requester_id,
        agent_name,
        service,
        require_management=True,
    )

    manager = get_runtime_credentials_manager(agent.runtime_paths)
    try:
        save_secret(manager, agent.target, service, body.secret)
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

    agent, _service = _resolve_agent_and_service(
        request,
        requester_id,
        agent_name,
        service,
        require_management=True,
    )

    manager = get_runtime_credentials_manager(agent.runtime_paths)
    delete_secret(manager, agent.target, service)


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(422, detail, headers=CONNECTIONS_HEADERS)


async def _limit_service_body(request: Request) -> None:
    """Refuse a service body over 16 KiB with 413 before its fields are validated, so a huge rules list never is.

    FastAPI has already read the body, so this reads Starlette's cached bytes.
    """
    if len(await request.body()) > MAX_SERVICE_BYTES:
        msg = f"A service can take at most {MAX_SERVICE_BYTES // 1024} KiB"
        raise HTTPException(413, msg, headers=CONNECTIONS_HEADERS)


def _parse_user_service(body: object) -> EgressService:
    """Validate a service body like a config service; the operator-only `oauth_on_shared_workers` is not accepted.

    Every refusal is a 422 with a string detail, a body that is not a JSON object included.
    """
    if not isinstance(body, dict):
        msg = "A service must be a JSON object"
        raise _unprocessable(msg)
    if "oauth_on_shared_workers" in body:
        msg = "oauth_on_shared_workers can only be set by an administrator in config.yaml"
        raise _unprocessable(msg)
    try:
        return EgressService.model_validate(body)
    except ValidationError as exc:
        messages = []
        for error in exc.errors(include_url=False, include_context=False, include_input=False):
            where = ".".join(str(part) for part in error["loc"])
            message = error["msg"].removeprefix("Value error, ")
            messages.append(f"{where}: {message}" if where else message)
        raise _unprocessable("; ".join(messages)) from exc


def _require_hosts_within_operator_policy(operator: EgressBrokerConfig, service: EgressService) -> None:
    """Refuse a service whose rules would apply on hosts that `unmatched_hosts: deny` keeps closed."""
    hosts = user_service_hosts_not_allowed(operator, service)
    if hosts:
        msg = (
            "Your administrator only allows requests to hosts that one of its own services covers, "
            f"so these hosts cannot be used: {', '.join(hosts)}"
        )
        raise _unprocessable(msg)


def _resolve_service_editor(request: Request, requester_id: str, agent_name: str) -> _ResolvedAgent:
    """Resolve an agent whose own services the requester may change: their private agent, or a shared one they manage."""
    agent = _resolve_agent(request, requester_id, agent_name)
    if not agent.can_manage:
        raise HTTPException(403, "Credential management is required", headers=CONNECTIONS_HEADERS)
    return agent


@router.get("/agents/{agent_name}/services/{name}")
def get_user_service(
    request: Request,
    agent_name: str,
    name: str,
    requester_id: _EgressUser,
) -> dict[str, Any]:
    """Return one of the scope's own services as authored, so a preset stays a preset."""
    agent = _resolve_agent(request, requester_id, agent_name)
    operator = agent.config.egress_broker
    manager = get_runtime_credentials_manager(agent.runtime_paths)
    service = None if name in operator.services else effective_services(agent.config, manager, agent.target).get(name)
    if service is None:
        raise HTTPException(404, "Service is not a service of your own", headers=CONNECTIONS_HEADERS)
    return service.authored_model_dump()


@router.put("/agents/{agent_name}/services/{name}", status_code=204)
def put_user_service(
    request: Request,
    agent_name: str,
    name: str,
    requester_id: _EgressUser,
    _size: Annotated[None, Depends(_limit_service_body)],
    body: Annotated[object, Body()] = None,
) -> None:
    """Create or replace one of the scope's own services.

    The body has the fields of a config service except `oauth_on_shared_workers`. Returns 413 for a body over 16 KiB,
    409 for the name of a config service, and 422, always with a string detail, for a body that is not a JSON object,
    an invalid service, a limit (count or size), a placeholder user services may not set, an unknown OAuth provider
    or one on a shared or unscoped agent, or rules on hosts that `unmatched_hosts: deny` keeps closed.
    """
    _require_same_origin(request)
    agent = _resolve_service_editor(request, requester_id, agent_name)
    operator = agent.config.egress_broker
    if name in operator.services:
        raise HTTPException(409, str(UserServiceConflictError(name)), headers=CONNECTIONS_HEADERS)
    service = _parse_user_service(body)
    _require_hosts_within_operator_policy(operator, service)
    manager = get_runtime_credentials_manager(agent.runtime_paths)
    try:
        save_user_service(
            manager,
            agent.target,
            name,
            service,
            config_services=operator.services,
            oauth_providers=load_oauth_providers(agent.config, agent.runtime_paths).keys(),
        )
    except ValueError as exc:
        raise _unprocessable(str(exc)) from exc


@router.delete("/agents/{agent_name}/services/{name}", status_code=204)
def delete_user_service_route(
    request: Request,
    agent_name: str,
    name: str,
    requester_id: _EgressUser,
) -> None:
    """Delete one of the scope's own services together with its stored key.

    Returns 404 when the scope has no such service. A config service cannot be deleted, so its name is a 409. An
    inactive entry (see `EgressInactiveService`) can still be removed: one stored under a name that a config service
    has since taken keeps the key, which the config service uses, and one that no longer validates takes its key
    with it.
    """
    _require_same_origin(request)
    agent = _resolve_service_editor(request, requester_id, agent_name)
    operator = agent.config.egress_broker
    manager = get_runtime_credentials_manager(agent.runtime_paths)
    if delete_user_service(manager, agent.target, name, config_services=operator.services):
        return
    if name in operator.services:
        detail = f"Service '{name}' is configured by your administrator and cannot be deleted here"
        raise HTTPException(409, detail, headers=CONNECTIONS_HEADERS)
    raise HTTPException(404, "Service is not a service of your own", headers=CONNECTIONS_HEADERS)


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
    agent, egress_service = await asyncio.to_thread(
        _resolve_agent_and_service,
        request,
        requester_id,
        agent_name,
        service,
        require_management=True,
        requester_owned_oauth=True,
    )
    provider = oauth_provider_of_service(
        request,
        egress_service,
        agent.target,
        connecting=True,
        headers=CONNECTIONS_HEADERS,
    )
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
    agent, egress_service = await asyncio.to_thread(
        _resolve_agent_and_service,
        request,
        requester_id,
        agent_name,
        service,
        require_management=True,
        requester_owned_oauth=True,
    )
    provider = oauth_provider_of_service(
        request,
        egress_service,
        agent.target,
        connecting=False,
        headers=CONNECTIONS_HEADERS,
    )
    try:
        return await oauth.disconnect(provider.id, request, agent_name=agent_name)
    except HTTPException as exc:
        raise HTTPException(
            exc.status_code,
            "Could not disconnect account",
            headers=CONNECTIONS_HEADERS,
        ) from exc
