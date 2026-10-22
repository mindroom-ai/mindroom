"""Personal egress credentials API for non-admin users, mounted under `/api/connections/egress`.

It lists every agent the user may use with its services and the preset catalog, sets or deletes a service's key,
reads, saves, and deletes the services a scope defines for itself, connects or disconnects a service's OAuth account
and allows or stops Agent Keys using it, and shows the caller's own brokered requests, or everyone's on a shared or
unscoped agent the caller manages.
"""

from __future__ import annotations

import asyncio
import functools
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, ValidationError

from mindroom.api import config_lifecycle, oauth
from mindroom.api.auth import require_connections_user
from mindroom.api.connection_agents import (
    CONNECTIONS_HEADERS,
    build_connection_agent_target,
    masked_oauth_call,
    require_connections_same_origin,
)
from mindroom.api.egress_status import (
    RESTRICT_ONLY_KEY_REFUSAL,
    AuditLogsResponse,
    EgressRuleSummary,
    EgressSourceStatus,
    OAuthReuseRequest,
    PresetsResponse,
    PutSecretRequest,
    audit_logs_response,
    effective_services,
    egress_oauth_status,
    egress_service_status,
    load_concurrently,
    oauth_provider_of_service,
    presets_response,
    require_egress_broker_enabled,
    rule_summaries,
    service_oauth_provider,
)
from mindroom.authorization import is_sender_allowed_for_agent_credential_management, is_sender_allowed_for_responder
from mindroom.config.egress_broker import EgressService
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.egress_broker.env import EgressAgentScope, egress_agent_scope
from mindroom.egress_broker.oauth_reuse import set_oauth_reuse
from mindroom.egress_broker.oauth_source import broker_credential_context
from mindroom.egress_broker.secrets import delete_secret, save_secret
from mindroom.egress_broker.service import active_audit_log
from mindroom.egress_broker.user_services import (
    MAX_SERVICE_BYTES,
    InactiveUserService,
    ServiceSource,
    UserServiceConflictError,
    delete_user_service,
    inactive_user_services,
    load_user_services,
    save_user_service,
    service_source,
    user_service_hosts_not_allowed,
)
from mindroom.oauth.registry import load_oauth_providers, load_oauth_providers_for_snapshot
from mindroom.requester_identity import resolve_human_requester_alias

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.config.agent import AgentConfig
    from mindroom.config.egress_broker import EgressBrokerConfig
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.credentials.manager import CredentialsManager
    from mindroom.oauth import OAuthProvider
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

router = APIRouter(
    prefix="/api/connections/egress",
    tags=["egress-credentials"],
    dependencies=[Depends(require_egress_broker_enabled)],
)


# Request gates and agent resolution


def _egress_user_accepting(*query_names: str) -> Callable[[Request, Response], Awaitable[str]]:
    """Build the dependency that authenticates the requester and rejects every query parameter but `query_names`.

    It returns the requester ID.
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


def _unprocessable(detail: str) -> HTTPException:
    """Build the 422 every refused service or key body ends in."""
    return HTTPException(422, detail, headers=CONNECTIONS_HEADERS)


def _require_egress_same_origin(request: Request) -> None:
    """Reject a mutation from another origin before any authorization lookup."""
    require_connections_same_origin(
        request,
        config_lifecycle.bind_current_request_snapshot(request).runtime_paths,
        detail="Egress credential changes require a same-origin request",
    )


def _check_agent_eligibility(
    request: Request,
    agent_name: str,
    requester_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> tuple[bool, bool] | None:
    """Return ``(is_shared, can_manage)`` for an eligible agent, else ``None``.

    Eligibility requires an agent that reaches the broker (`egress_agent_scope`) and permission to use it.
    Listing, portal catalog, mutations, and the request log's shared-agent rows all decide through this one rule.
    """
    scope = egress_agent_scope(config, agent_name)
    if scope is None:
        return None
    if not is_sender_allowed_for_responder(
        requester_id,
        agent_name,
        None,
        config,
        runtime_paths,
        config_lifecycle.app_state(request.app).agent_reply_memberships,
    ):
        return None
    is_shared = scope in {"shared", "unscoped"}
    can_manage = not is_shared or is_sender_allowed_for_agent_credential_management(
        requester_id,
        agent_name,
        config,
        runtime_paths,
    )
    return is_shared, can_manage


@dataclass(frozen=True)
class _ResolvedAgent:
    """An agent the requester may use, with the worker target their credentials and services live under."""

    config: Config
    runtime_paths: RuntimePaths
    target: ResolvedWorkerTarget
    manager: CredentialsManager
    can_manage: bool


def _resolve_agent(request: Request, requester_id: str, agent_name: str) -> _ResolvedAgent:
    """Resolve one agent for the requester, or 404 when it is unknown or the requester may not use it."""
    snapshot = config_lifecycle.bind_current_request_snapshot(request)
    config = snapshot.runtime_config
    if config is None or agent_name not in config.agents:
        raise HTTPException(404, "Agent is not available", headers=CONNECTIONS_HEADERS)

    runtime_paths = snapshot.runtime_paths
    human_requester = resolve_human_requester_alias(requester_id, config, runtime_paths)
    eligibility = _check_agent_eligibility(request, agent_name, human_requester, config, runtime_paths)
    if eligibility is None:
        raise HTTPException(404, "Agent is not available", headers=CONNECTIONS_HEADERS)

    target = build_connection_agent_target(config, runtime_paths, human_requester, agent_name)
    manager = get_runtime_credentials_manager(runtime_paths)
    return _ResolvedAgent(config, runtime_paths, target, manager, can_manage=eligibility[1])


def _resolve_agent_and_service(
    request: Request,
    requester_id: str,
    agent_name: str,
    service_name: str,
    *,
    requester_owned_oauth: bool = False,
) -> tuple[_ResolvedAgent, EgressService]:
    """Resolve one agent and one of its services for a mutation, which needs credential management (403).

    The service is one of the config's or one the requester's scope defines for itself, so a name that exists only
    in another requester's scope is a 404.
    `requester_owned_oauth` is for connecting or disconnecting the service's OAuth account: a requester-scoped
    provider's connection belongs to the requester, so any user of the agent may manage it.
    """
    agent = _resolve_agent(request, requester_id, agent_name)
    service = effective_services(agent.config, agent.manager, agent.target).get(service_name)
    if service is None:
        raise HTTPException(404, "Service is not configured", headers=CONNECTIONS_HEADERS)

    requester_owned = False
    if requester_owned_oauth:
        provider = service_oauth_provider(config_lifecycle.bind_current_request_snapshot(request), service)
        requester_owned = provider is not None and provider.requester_scoped_credentials
    if not agent.can_manage and not requester_owned:
        raise HTTPException(403, "Credential management is required", headers=CONNECTIONS_HEADERS)

    return agent, service


def _resolve_service_editor(request: Request, requester_id: str, agent_name: str) -> _ResolvedAgent:
    """Resolve an agent whose own services the requester may change: their private agent, or a shared one they manage."""
    agent = _resolve_agent(request, requester_id, agent_name)
    if not agent.can_manage:
        raise HTTPException(403, "Credential management is required", headers=CONNECTIONS_HEADERS)
    return agent


# Listing and presets


class EgressCredentialService(EgressSourceStatus):
    """One egress service with its management permissions and its key and OAuth sources.

    `source` is `config` for a service the administrator defines and `user` for one defined in the agent's own
    scope through the services routes. `rules` says where the service applies (host, port, path prefix), so a client
    can tell which services overlap, and how each rule sends the key (`auth_type` and `auth_name`, never the key).
    `placeholder_names` lists the variables the service sets in the sandbox (a preset's included), names only, so the
    editor can warn when a service of one's own sets the same one. `restrict_only` marks a service that only limits
    the paths of the other services on its hosts and never has a key or account of its own.
    """

    name: str
    display_name: str
    description: str
    is_shared: bool
    can_manage: bool
    source: ServiceSource
    rules: list[EgressRuleSummary]
    placeholder_names: list[str]
    restrict_only: bool


class EgressCredentialAgent(BaseModel):
    """One agent with its egress services.

    `shared` is true for a shared or unscoped agent, whose services and keys everyone using it shares, and
    `can_manage` says whether the caller may change them: their own agent, or a shared one they manage. Both are
    known even when the agent has no services yet. `inactive_services` lists the stored entries of the agent's
    scope that the broker ignores, so a user can still remove them: `shadowed` means an administrator's service has
    the same name now, `invalid` that the entry no longer validates as a service. They count toward the scope's
    limits.

    `scope` says whose store the services and keys live in: one `user_agent` agent's own, every `user` agent's
    of the caller, everyone's on a `shared` agent, or the global store of an `unscoped` one. `scope_agents` names
    the caller's agents that share a `user` store, this one included, and is empty for any other scope.
    `managers` is a shared or unscoped agent's credential managers, as Matrix IDs; it is empty for the rest, and
    for an agent with none only administrators manage it.
    """

    agent_name: str
    agent_display_name: str
    shared: bool
    can_manage: bool
    scope: EgressAgentScope
    scope_agents: list[str]
    managers: list[str]
    services: list[EgressCredentialService]
    inactive_services: list[InactiveUserService]


class EgressCredentialsResponse(BaseModel):
    """List of agents with their egress services."""

    agents: list[EgressCredentialAgent]


async def egress_services_for_agent(
    request: Request,
    agent_name: str,
    requester_id: str,
    *,
    eligibility: tuple[bool, bool] | None = None,
) -> list[EgressCredentialService] | None:
    """List the brokered services for one agent, or ``None`` when the requester may not use it.

    The services are the config's, then the ones the requester's scope defines for itself.
    ``requester_id`` must already be the resolved human requester alias. A caller that already ran the
    eligibility check passes its ``(is_shared, can_manage)`` result.
    """
    snapshot = config_lifecycle.bind_current_request_snapshot(request)
    config, runtime_paths = snapshot.runtime_config, snapshot.runtime_paths
    if config is None:
        return None
    if eligibility is None:
        eligibility = _check_agent_eligibility(request, agent_name, requester_id, config, runtime_paths)
    if eligibility is None:
        return None
    is_shared, can_manage = eligibility
    manager = get_runtime_credentials_manager(runtime_paths)
    target = build_connection_agent_target(config, runtime_paths, requester_id, agent_name)
    services = await asyncio.to_thread(effective_services, config, manager, target)

    async def row(name: str, service: EgressService) -> EgressCredentialService:
        oauth_part = await egress_oauth_status(
            request,
            name,
            service,
            target,
            config=config,
            runtime_paths=runtime_paths,
            manager=manager,
            can_manage=can_manage,
            connection_status=lambda provider: oauth.agent_connection_status(
                request,
                provider,
                runtime_paths,
                manager,
                target,
                config=config,
                refresh=False,
            ),
            reuse_target=lambda _provider: target,
        )
        sources = await egress_service_status(manager, target, service, name, oauth_part)
        return EgressCredentialService(
            name=name,
            display_name=service.display_name or name.replace("_", " ").title(),
            description=service.description,
            is_shared=is_shared,
            can_manage=can_manage,
            source=service_source(config.egress_broker, name),
            rules=rule_summaries(service),
            placeholder_names=list(service.placeholder_env),
            restrict_only=service.restrict_only,
            **asdict(sources),
        )

    return await load_concurrently(
        (functools.partial(row, name, service) for name, service in services.items()),
        limited=True,
    )


async def _load_egress_agents(request: Request, requester_id: str) -> list[EgressCredentialAgent]:
    """List every agent the requester may use, with its egress services, in config declaration order."""
    snapshot = config_lifecycle.bind_current_request_snapshot(request)
    config = snapshot.runtime_config
    if config is None:
        return []

    runtime_paths = snapshot.runtime_paths
    human_requester = resolve_human_requester_alias(requester_id, config, runtime_paths)
    manager = get_runtime_credentials_manager(runtime_paths)

    eligible: dict[str, tuple[bool, bool]] = {}
    for agent_name in config.agents:
        eligibility = _check_agent_eligibility(request, agent_name, human_requester, config, runtime_paths)
        if eligibility is not None:
            eligible[agent_name] = eligibility
    # Every `user` agent of the requester reads and writes one store, so each of them names all of them.
    user_store_agents = [
        agent.display_name
        for agent_name, agent in config.agents.items()
        if agent_name in eligible and egress_agent_scope(config, agent_name) == "user"
    ]

    async def listed_agent(agent_name: str, agent: AgentConfig) -> EgressCredentialAgent | None:
        eligibility = eligible.get(agent_name)
        scope = egress_agent_scope(config, agent_name)
        if eligibility is None or scope is None:
            return None
        is_shared, can_manage = eligibility
        target = build_connection_agent_target(config, runtime_paths, human_requester, agent_name)
        async with asyncio.TaskGroup() as group:
            inactive = group.create_task(
                asyncio.to_thread(inactive_user_services, config.egress_broker, manager, target),
            )
            services = await egress_services_for_agent(request, agent_name, human_requester, eligibility=eligibility)
        if services is None:
            return None
        return EgressCredentialAgent(
            agent_name=agent_name,
            agent_display_name=agent.display_name,
            shared=is_shared,
            can_manage=can_manage,
            scope=scope,
            scope_agents=list(user_store_agents) if scope == "user" else [],
            managers=list(agent.credential_managers) if is_shared else [],
            services=services,
            inactive_services=inactive.result(),
        )

    # Agents load concurrently, like their rows, so the page waits for the slowest agent instead of all of them.
    # Only rows take the shared limit: an agent waits on its rows, so an agent holding a slot could starve them.
    listed = await load_concurrently(
        functools.partial(listed_agent, name, agent) for name, agent in config.agents.items()
    )
    return [agent for agent in listed if agent is not None]


@router.get("", response_model=EgressCredentialsResponse)
async def list_egress_credentials(request: Request, requester_id: _EgressUser) -> EgressCredentialsResponse:
    """List egress services for every agent the user may use."""
    agents = await _load_egress_agents(request, requester_id)
    return EgressCredentialsResponse(agents=agents)


@router.get("/presets", response_model=PresetsResponse)
async def get_presets(request: Request, _requester_id: _EgressUser) -> PresetsResponse:
    """List the built-in service presets with the rules, login, and placeholders each one sets, and the providers."""
    snapshot = config_lifecycle.bind_current_request_snapshot(request)
    return presets_response(load_oauth_providers_for_snapshot(snapshot))


# Keys


@router.put("/agents/{agent_name}/{service}", status_code=204)
def put_egress_secret(
    request: Request,
    agent_name: str,
    service: str,
    body: PutSecretRequest,
    requester_id: _EgressUser,
) -> None:
    """Set or replace an egress service secret for one agent; a `restrict_only` service takes no key (422)."""
    _require_egress_same_origin(request)

    agent, egress_service = _resolve_agent_and_service(request, requester_id, agent_name, service)
    if egress_service.restrict_only:
        raise _unprocessable(RESTRICT_ONLY_KEY_REFUSAL)

    try:
        save_secret(agent.manager, agent.target, service, body.secret)
    except ValueError as exc:
        raise _unprocessable(str(exc)) from exc


@router.delete("/agents/{agent_name}/{service}", status_code=204)
def delete_egress_secret(
    request: Request,
    agent_name: str,
    service: str,
    requester_id: _EgressUser,
) -> None:
    """Delete an egress service secret for one agent."""
    _require_egress_same_origin(request)

    agent, _service = _resolve_agent_and_service(request, requester_id, agent_name, service)
    delete_secret(agent.manager, agent.target, service)


# User services


async def _limit_service_body(request: Request) -> None:
    """Refuse a body over `MAX_SERVICE_BYTES` with 413 before its fields are validated, so a huge rules list never is.

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
    service = (
        None if name in operator.services else effective_services(agent.config, agent.manager, agent.target).get(name)
    )
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
    an invalid service, a limit (count or size), a placeholder user services may not set, an unknown OAuth provider,
    one on a shared or unscoped agent, or one whose rules leave its provider's preset hosts, or rules on hosts that
    `unmatched_hosts: deny` keeps closed.
    """
    _require_egress_same_origin(request)
    agent = _resolve_service_editor(request, requester_id, agent_name)
    operator = agent.config.egress_broker
    if name in operator.services:
        raise HTTPException(409, str(UserServiceConflictError(name)), headers=CONNECTIONS_HEADERS)
    service = _parse_user_service(body)
    _require_hosts_within_operator_policy(operator, service)
    try:
        save_user_service(
            agent.manager,
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
    """Delete one of the scope's own services together with its stored key and its account's opt-in.

    Returns 404 when the scope has no such service. A config service cannot be deleted, so its name is a 409. An
    inactive entry (see `InactiveUserService`) can still be removed: one stored under a name that a config service
    has since taken keeps the key, which the config service uses, and one that no longer validates takes its key
    with it.
    """
    _require_egress_same_origin(request)
    agent = _resolve_service_editor(request, requester_id, agent_name)
    operator = agent.config.egress_broker
    own = None if name in operator.services else load_user_services(agent.manager, agent.target).get(name)
    if delete_user_service(agent.manager, agent.target, name, config_services=operator.services):
        provider = service_oauth_provider(config_lifecycle.bind_current_request_snapshot(request), own)
        if provider is not None:
            _set_account_reuse(agent, provider, name, enabled=False, by=requester_id)
        return
    if name in operator.services:
        detail = f"Service '{name}' is configured by your administrator and cannot be deleted here"
        raise HTTPException(409, detail, headers=CONNECTIONS_HEADERS)
    raise HTTPException(404, "Service is not a service of your own", headers=CONNECTIONS_HEADERS)


# Accounts


class _EmptyMutation(BaseModel):
    model_config = ConfigDict(extra="forbid")


async def _account_provider(
    request: Request,
    requester_id: str,
    agent_name: str,
    service: str,
    *,
    connecting: bool,
) -> tuple[_ResolvedAgent, OAuthProvider]:
    """Run the account gates in order: same origin, agent and service access, then the provider."""
    _require_egress_same_origin(request)
    agent, egress_service = await asyncio.to_thread(
        _resolve_agent_and_service,
        request,
        requester_id,
        agent_name,
        service,
        requester_owned_oauth=True,
    )
    provider = oauth_provider_of_service(
        request,
        egress_service,
        agent.target,
        connecting=connecting,
        headers=CONNECTIONS_HEADERS,
    )
    return agent, provider


def _set_account_reuse(agent: _ResolvedAgent, provider: OAuthProvider, service: str, *, enabled: bool, by: str) -> None:
    """Allow or stop Agent Keys using the service's account, in the store the broker reads it from for this agent."""
    context = broker_credential_context(provider, agent.config, agent.runtime_paths, agent.manager, agent.target)
    set_oauth_reuse(context, service, provider.id, enabled=enabled, by=by)


@router.post("/agents/{agent_name}/{service}/connect")
async def connect_egress_account(
    request: Request,
    agent_name: str,
    service: str,
    requester_id: _EgressUser,
    _body: _EmptyMutation,
) -> oauth.OAuthConnectResponse:
    """Start the OAuth flow of a service's provider for one agent's credential scope."""
    _agent, provider = await _account_provider(request, requester_id, agent_name, service, connecting=True)
    return await masked_oauth_call(
        oauth.connect(provider.id, request, agent_name=agent_name),
        "Could not start account connection",
    )


@router.post("/agents/{agent_name}/{service}/disconnect")
async def disconnect_egress_account(
    request: Request,
    agent_name: str,
    service: str,
    requester_id: _EgressUser,
    _body: _EmptyMutation,
) -> dict[str, str]:
    """Reset the OAuth connection of a service's provider for one agent's credential scope; its opt-in stays."""
    _agent, provider = await _account_provider(request, requester_id, agent_name, service, connecting=False)
    return await masked_oauth_call(
        oauth.disconnect(provider.id, request, agent_name=agent_name),
        "Could not disconnect account",
    )


@router.put("/agents/{agent_name}/{service}/oauth-reuse", status_code=204)
async def allow_egress_account_reuse(
    request: Request,
    agent_name: str,
    service: str,
    requester_id: _EgressUser,
    _body: OAuthReuseRequest,
) -> None:
    """Let Agent Keys use the service's connected account for this agent; the body must confirm the approval bypass.

    The account's own connection, connected now or later, is then injected for the agent's shell and python. Who may
    do this, and the 403, 404, and 409 answers, follow the connect route; a body without the confirmation is a 422.
    """
    agent, provider = await _account_provider(request, requester_id, agent_name, service, connecting=False)
    await asyncio.to_thread(_set_account_reuse, agent, provider, service, enabled=True, by=requester_id)


@router.delete("/agents/{agent_name}/{service}/oauth-reuse", status_code=204)
async def stop_egress_account_reuse(
    request: Request,
    agent_name: str,
    service: str,
    requester_id: _EgressUser,
) -> None:
    """Stop Agent Keys using the service's connected account for this agent; the connection stays for its tools."""
    agent, provider = await _account_provider(request, requester_id, agent_name, service, connecting=False)
    await asyncio.to_thread(_set_account_reuse, agent, provider, service, enabled=False, by=requester_id)


# Request log


_MAX_PERSONAL_LOG_ROWS = 200

_EgressLogUser = Annotated[str, Depends(_egress_user_accepting("agent_name", "limit"))]


@router.get("/logs", response_model=AuditLogsResponse)
def get_personal_logs(
    request: Request,
    requester_id: _EgressLogUser,
    agent_name: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query()] = _MAX_PERSONAL_LOG_ROWS,
) -> AuditLogsResponse:
    """Return recent brokered requests, newest first, optionally for one agent.

    The rows are filtered by the canonical requester, the identity the worker tokens carry, whatever else the query
    says, so no filter can show another user's rows, with one exception: `agent_name` naming a shared or unscoped
    agent the caller may manage, by the rule that gates its keys and services, returns every requester's rows that
    agent logged in its current scope. A personal agent, an unknown one, or one the caller may not use or manage
    answers exactly as without the exception. Returns 409 if the broker is not running.
    """
    audit = active_audit_log()
    if audit is None:
        raise HTTPException(409, "Egress broker is not running", headers=CONNECTIONS_HEADERS)
    snapshot = config_lifecycle.bind_current_request_snapshot(request)
    config = snapshot.runtime_config
    if config is None:
        raise HTTPException(503, "Egress logs are unavailable", headers=CONNECTIONS_HEADERS)
    requester = resolve_human_requester_alias(requester_id, config, snapshot.runtime_paths)
    log_requester: str | None = requester
    log_scope = None
    if agent_name and agent_name in config.agents:
        eligibility = _check_agent_eligibility(request, agent_name, requester, config, snapshot.runtime_paths)
        is_shared, can_manage = eligibility or (False, False)
        if is_shared and can_manage:
            # Rows the agent logged before it was shared were personal, so they stay with their requesters.
            log_requester, log_scope = None, egress_agent_scope(config, agent_name)
    records = audit.query(
        requester_id=log_requester,
        agent_name=agent_name or None,
        scope=log_scope,
        limit=max(1, min(limit, _MAX_PERSONAL_LOG_ROWS)),
    )
    return audit_logs_response(records)
