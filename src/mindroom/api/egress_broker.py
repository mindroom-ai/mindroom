"""Egress broker admin API routes."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from mindroom.api import config_lifecycle, oauth
from mindroom.api.credentials_target import (
    resolve_request_credentials_target,
    worker_target_for_credentials_target,
)
from mindroom.api.egress_status import (
    RESTRICT_ONLY_KEY_REFUSAL,
    AuditLogsResponse,
    EgressRuleSummary,
    EgressSourceStatus,
    PresetsResponse,
    PutSecretRequest,
    audit_logs_response,
    effective_services,
    egress_oauth_status,
    egress_service_status,
    oauth_provider_of_service,
    presets_response,
    require_egress_broker_enabled,
    rule_summaries,
)
from mindroom.egress_broker.env import egress_agent_scope, personal_page_served
from mindroom.egress_broker.secrets import PERSONAL_WORKER_SCOPES, delete_secret, save_secret
from mindroom.egress_broker.service import active_audit_log, active_ca_pem
from mindroom.egress_broker.user_services import ServiceSource, service_source
from mindroom.oauth.registry import load_oauth_providers_for_snapshot

if TYPE_CHECKING:
    from mindroom.api.credentials_target import RequestCredentialsTarget
    from mindroom.config.egress_broker import EgressService
    from mindroom.oauth import OAuthProvider
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

router = APIRouter(
    prefix="/api/egress-broker",
    tags=["egress-broker"],
    dependencies=[Depends(require_egress_broker_enabled)],
)


class _ServiceStatus(EgressSourceStatus):
    """Status of one egress service.

    `source` is `config` for the administrator's services and `user` for services the selected scope defines for
    itself, which the panel shows read-only: they are edited on the personal egress page. `restrict_only` marks a
    service that only limits the paths of the other services on its hosts and never has a key or account.
    """

    name: str
    display_name: str | None
    description: str
    source: ServiceSource
    rules: list[EgressRuleSummary]
    restrict_only: bool


class ServicesResponse(BaseModel):
    """List of egress services with their status, and the agents whose keys the panel sets.

    `shared_agents` names the shared agents that reach the broker, in config order, each with keys of its own.
    `has_unscoped_agents` says whether an agent without a worker scope reaches it; all of those share the global keys.
    Agents whose keys belong to each requester (`user`, `user_agent`, and private agents) are never named: their
    users set keys on the personal egress page. `has_personal_agents` says whether one of them reaches the broker,
    and `personal_page_served` whether this deployment serves that page. All four fields are the same whichever scope
    is selected.
    """

    services: list[_ServiceStatus]
    shared_agents: list[str]
    has_unscoped_agents: bool
    has_personal_agents: bool
    personal_page_served: bool


async def _load_service_statuses_for_target(
    request: Request,
    target: RequestCredentialsTarget,
) -> list[_ServiceStatus]:
    """Build service status list for one target."""
    config = config_lifecycle.bind_current_request_snapshot(request).runtime_config
    if config is None:
        return []

    worker_target = worker_target_for_credentials_target(target)

    services: list[_ServiceStatus] = []
    scope_services = await asyncio.to_thread(effective_services, config, target.base_manager, worker_target)
    for name, service_config in scope_services.items():
        oauth_part = await egress_oauth_status(
            request,
            name,
            service_config,
            worker_target,
            config=config,
            runtime_paths=target.runtime_paths,
            manager=target.base_manager,
            can_manage=True,
            # The router's dependency has authenticated the request, so the dashboard's status helper can read it.
            connection_status=lambda provider: oauth.authenticated_connection_status(
                provider.id,
                request,
                agent_name=target.agent_name,
                refresh=False,
            ),
        )
        sources = await egress_service_status(target.base_manager, worker_target, service_config, name, oauth_part)
        services.append(
            _ServiceStatus(
                name=name,
                display_name=service_config.display_name,
                description=service_config.description,
                source=service_source(config.egress_broker, name),
                rules=rule_summaries(service_config),
                restrict_only=service_config.restrict_only,
                **asdict(sources),
            ),
        )

    return services


def _resolve_scope_and_service(
    request: Request,
    name: str,
    agent_name: str | None,
) -> tuple[RequestCredentialsTarget, ResolvedWorkerTarget | None, EgressService]:
    """Resolve the selected scope and the service, which must be one it uses: a config service or its own."""
    config = config_lifecycle.bind_current_request_snapshot(request).runtime_config
    if config is None:
        raise HTTPException(status_code=404, detail=f"Service '{name}' is not configured")
    target = resolve_request_credentials_target(request, agent_name=agent_name)
    worker_target = worker_target_for_credentials_target(target)
    service = effective_services(config, target.base_manager, worker_target).get(name)
    if service is None:
        raise HTTPException(status_code=404, detail=f"Service '{name}' is not configured")
    return target, worker_target, service


def _config_service_provider(request: Request, name: str, agent_name: str | None, *, connecting: bool) -> OAuthProvider:
    """Return the OAuth provider of a config service for the connect and disconnect routes of the selected scope.

    A service the config does not define is a 404; `oauth_provider_of_service` judges the rest for the scope's worker
    target, the one the status listing uses for the same service.
    """
    target = resolve_request_credentials_target(request, agent_name=agent_name)
    worker_target = worker_target_for_credentials_target(target)
    config = config_lifecycle.bind_current_request_snapshot(request).runtime_config
    service = config.egress_broker.services.get(name) if config is not None else None
    if service is None:
        raise HTTPException(404, "Service is not configured")
    return oauth_provider_of_service(request, service, worker_target, connecting=connecting)


@router.get("/services", response_model=ServicesResponse)
async def get_services(
    request: Request,
    agent_name: Annotated[str | None, Query()] = None,
) -> ServicesResponse:
    """List egress services with their configured status.

    Returns configured status and last-updated time, never secret values.
    """
    target = resolve_request_credentials_target(request, agent_name=agent_name)

    services = await _load_service_statuses_for_target(request, target)
    config = config_lifecycle.bind_current_request_snapshot(request).runtime_config
    scopes = {name: egress_agent_scope(config, name) for name in config.agents} if config is not None else {}
    return ServicesResponse(
        services=services,
        shared_agents=[name for name, scope in scopes.items() if scope == "shared"],
        has_unscoped_agents="unscoped" in scopes.values(),
        has_personal_agents=any(scope in PERSONAL_WORKER_SCOPES for scope in scopes.values()),
        personal_page_served=personal_page_served(target.runtime_paths),
    )


@router.get("/presets", response_model=PresetsResponse)
def get_presets(request: Request) -> PresetsResponse:
    """List the built-in service presets with the rules, login, and placeholders each one sets, and the providers."""
    return presets_response(load_oauth_providers_for_snapshot(config_lifecycle.bind_current_request_snapshot(request)))


@router.put("/services/{name}/secret", status_code=204)
def put_service_secret(
    request: Request,
    name: str,
    body: PutSecretRequest,
    agent_name: Annotated[str | None, Query()] = None,
) -> None:
    """Set an egress service secret.

    Raises 404 if the service is neither configured nor one of the selected scope's own services, 422 if the
    secret is invalid or the service is `restrict_only`, which takes no key.
    """
    target, worker_target, service = _resolve_scope_and_service(request, name, agent_name)
    if service.restrict_only:
        raise HTTPException(status_code=422, detail=RESTRICT_ONLY_KEY_REFUSAL)

    try:
        save_secret(target.base_manager, worker_target, name, body.secret)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/services/{name}/secret", status_code=204)
def delete_service_secret(
    request: Request,
    name: str,
    agent_name: Annotated[str | None, Query()] = None,
) -> None:
    """Delete an egress service secret."""
    target, worker_target, _service = _resolve_scope_and_service(request, name, agent_name)
    delete_secret(target.base_manager, worker_target, name)


@router.post("/services/{name}/connect")
async def connect_service_account(
    request: Request,
    name: str,
    agent_name: Annotated[str | None, Query()] = None,
) -> oauth.OAuthConnectResponse:
    """Start the OAuth flow of a service's provider for the selected scope.

    Raises 404 if the service is not configured or has no usable OAuth provider, 409 if a service account
    replaces personal accounts for that provider or the scope's sandbox is shared by several requesters, where the
    broker never uses a requester's own account unless the service opts in.
    """
    provider = _config_service_provider(request, name, agent_name, connecting=True)
    return await oauth.connect(provider.id, request, agent_name=agent_name)


@router.post("/services/{name}/disconnect")
async def disconnect_service_account(
    request: Request,
    name: str,
    agent_name: Annotated[str | None, Query()] = None,
) -> dict[str, str]:
    """Reset the OAuth connection of a service's provider for the selected scope.

    Raises 404 like the connect route, and 409 where the scope's sandbox is shared by several requesters.
    """
    provider = _config_service_provider(request, name, agent_name, connecting=False)
    return await oauth.disconnect(provider.id, request, agent_name=agent_name)


@router.get("/logs", response_model=AuditLogsResponse)
def get_logs(
    agent_name: Annotated[str | None, Query()] = None,
    requester: Annotated[str | None, Query()] = None,
    host: Annotated[str | None, Query()] = None,
    service: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> AuditLogsResponse:
    """Query egress broker request audit logs.

    Every row carries `requester_id`, the requester the worker token named, and `requester` matches it exactly.
    Returns 409 if the broker is not running.
    """
    audit = active_audit_log()
    if audit is None:
        raise HTTPException(status_code=409, detail="Egress broker is not running")

    records = audit.query(
        agent_name=agent_name,
        requester_id=requester,
        host=host,
        service=service,
        limit=limit,
    )

    return audit_logs_response(records)


@router.get("/ca.pem", response_class=PlainTextResponse)
def get_ca_pem() -> str:
    """Download the broker's root CA certificate in PEM format.

    Returns 409 if the broker is not running.
    """
    ca_pem = active_ca_pem()
    if ca_pem is None:
        raise HTTPException(status_code=409, detail="Egress broker is not running")
    return ca_pem
