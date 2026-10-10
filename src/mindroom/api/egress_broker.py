"""Egress broker admin API routes."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from mindroom.api import oauth
from mindroom.api.credentials_target import (
    resolve_request_credentials_target,
    worker_target_for_credentials_target,
)
from mindroom.api.egress_status import (
    AuditLogsResponse,
    EgressRuleSummary,
    EgressServiceSource,
    EgressSourceStatus,
    PresetsResponse,
    audit_logs_response,
    effective_services,
    egress_oauth_provider,
    egress_oauth_status,
    egress_service_status,
    presets_response,
    rule_summaries,
    service_source,
    unavailable_egress_oauth_status,
)
from mindroom.egress_broker.oauth_source import oauth_status, shared_worker_oauth, shared_worker_unavailable_status
from mindroom.egress_broker.secrets import delete_secret, save_secret
from mindroom.egress_broker.service import active_audit_log, active_ca_pem
from mindroom.logging_config import get_logger
from mindroom.oauth.registry import load_oauth_providers_for_snapshot

if TYPE_CHECKING:
    from mindroom.api.credentials_target import RequestCredentialsTarget
    from mindroom.config.egress_broker import EgressService
    from mindroom.config.main import Config
    from mindroom.egress_broker.secrets import OAuthStatus
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

router = APIRouter(prefix="/api/egress-broker", tags=["egress-broker"])
logger = get_logger(__name__)


class _ServiceStatus(EgressSourceStatus):
    """Status of one egress service.

    `source` is `config` for the administrator's services and `user` for services the selected scope defines for
    itself, which the panel shows read-only: they are edited on the personal egress page.
    """

    name: str
    display_name: str | None
    description: str
    source: EgressServiceSource
    rules: list[EgressRuleSummary]


class ServicesResponse(BaseModel):
    """List of egress services with their status."""

    services: list[_ServiceStatus]


class PutSecretRequest(BaseModel):
    """Request body for setting a secret."""

    secret: str


async def _admin_oauth_status(
    request: Request,
    config: Config,
    target: RequestCredentialsTarget,
    name: str,
    service: EgressService,
) -> OAuthStatus | None:
    """Load a provider's connection state for the selected scope with the dashboard's OAuth status helper.

    The router's dependency has authenticated the request. The token refresh is skipped, as on the personal page,
    so a stalled provider never stalls the panel. Any failure to read one provider's state, the worker backend check
    included, shows that service as not connectable instead of failing the panel.
    """
    from mindroom.api import config_lifecycle  # noqa: PLC0415

    provider_id = service.oauth_provider
    if provider_id is None:
        return None
    provider = load_oauth_providers_for_snapshot(config_lifecycle.bind_current_request_snapshot(request)).get(
        provider_id,
    )
    if provider is None:
        return None
    worker_target = worker_target_for_credentials_target(target)
    try:
        access = shared_worker_oauth(
            provider,
            worker_target,
            opted_in=service.oauth_on_shared_workers,
            runtime_paths=target.runtime_paths,
        )
        if access == "refused":
            return shared_worker_unavailable_status(provider, target.runtime_paths)
        result = await oauth.authenticated_connection_status(
            provider_id,
            request,
            agent_name=target.agent_name,
            refresh=False,
        )
        status = await egress_oauth_status(
            result,
            can_manage=True,
            stored_connection=partial(
                oauth_status,
                provider_id,
                worker_target,
                service=name,
                config=config,
                runtime_paths=target.runtime_paths,
                credentials_manager=target.base_manager,
            ),
        )
    except Exception as exc:
        logger.warning(
            "egress_oauth_status_unavailable",
            service=name,
            provider_id=provider_id,
            error_type=type(exc).__name__,
        )
        return unavailable_egress_oauth_status(provider)
    return replace(status, shared_worker_opt_in=access == "allowed")


async def _load_service_statuses_for_target(
    request: Request,
    target: RequestCredentialsTarget,
) -> list[_ServiceStatus]:
    """Build service status list for one target."""
    from mindroom.api import config_lifecycle  # noqa: PLC0415

    config = config_lifecycle.bind_current_request_snapshot(request).runtime_config
    if config is None:
        return []

    worker_target = worker_target_for_credentials_target(target)

    services: list[_ServiceStatus] = []
    # Reading the scope's own services touches the credential store, so it stays off the event loop.
    scope_services = await asyncio.to_thread(effective_services, config, target.base_manager, worker_target)
    for name, service_config in scope_services.items():
        oauth_part = await _admin_oauth_status(request, config, target, name, service_config)
        sources = await egress_service_status(target.base_manager, worker_target, service_config, name, oauth_part)
        services.append(
            _ServiceStatus(
                name=name,
                display_name=service_config.display_name,
                description=service_config.description,
                source=service_source(config, name),
                rules=rule_summaries(service_config),
                **sources.model_dump(),
            ),
        )

    return services


def _resolve_scope_and_service(
    request: Request,
    name: str,
    agent_name: str | None,
) -> tuple[RequestCredentialsTarget, ResolvedWorkerTarget | None]:
    """Resolve the selected scope and check that the service is one it uses, a config service or its own."""
    from mindroom.api import config_lifecycle  # noqa: PLC0415

    config = config_lifecycle.bind_current_request_snapshot(request).runtime_config
    if config is None:
        raise HTTPException(status_code=404, detail=f"Service '{name}' is not configured")
    target = resolve_request_credentials_target(
        request,
        agent_name=agent_name,
        service_names=(),
    )
    worker_target = worker_target_for_credentials_target(target)
    if name not in effective_services(config, target.base_manager, worker_target):
        raise HTTPException(status_code=404, detail=f"Service '{name}' is not configured")
    return target, worker_target


def _scope_worker_target(request: Request, agent_name: str | None) -> ResolvedWorkerTarget | None:
    """Return the worker target of the selected scope, the one the status listing uses for the same service."""
    target = resolve_request_credentials_target(request, agent_name=agent_name, service_names=())
    return worker_target_for_credentials_target(target)


@router.get("/services", response_model=ServicesResponse)
async def get_services(
    request: Request,
    agent_name: Annotated[str | None, Query()] = None,
) -> ServicesResponse:
    """List egress services with their configured status.

    Returns configured status and last-updated time, never secret values.
    """
    target = resolve_request_credentials_target(
        request,
        agent_name=agent_name,
        service_names=(),  # egress services are not in the normal service list
    )

    services = await _load_service_statuses_for_target(request, target)
    return ServicesResponse(services=services)


@router.get("/presets", response_model=PresetsResponse)
def get_presets() -> PresetsResponse:
    """List the built-in service presets with the rules, login, and placeholders each one sets."""
    return presets_response()


@router.put("/services/{name}/secret", status_code=204)
def put_service_secret(
    request: Request,
    name: str,
    body: PutSecretRequest,
    agent_name: Annotated[str | None, Query()] = None,
) -> None:
    """Set an egress service secret.

    Raises 404 if the service is neither configured nor one of the selected scope's own services, 422 if the
    secret is invalid.
    """
    target, worker_target = _resolve_scope_and_service(request, name, agent_name)

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
    target, worker_target = _resolve_scope_and_service(request, name, agent_name)
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
    provider = egress_oauth_provider(request, name, _scope_worker_target(request, agent_name), connecting=True)
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
    provider = egress_oauth_provider(request, name, _scope_worker_target(request, agent_name), connecting=False)
    return await oauth.disconnect(provider.id, request, agent_name=agent_name)


@router.get("/logs", response_model=AuditLogsResponse)
def get_logs(
    agent_name: Annotated[str | None, Query()] = None,
    host: Annotated[str | None, Query()] = None,
    service: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> AuditLogsResponse:
    """Query egress broker request audit logs.

    Returns 409 if the broker is not running.
    """
    audit = active_audit_log()
    if audit is None:
        raise HTTPException(status_code=409, detail="Egress broker is not running")

    records = audit.query(
        agent_name=agent_name,
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
