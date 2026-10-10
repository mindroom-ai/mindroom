"""Egress broker admin API routes."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from mindroom.api import oauth
from mindroom.api.credentials_target import (
    resolve_request_credentials_target,
    worker_target_for_credentials_target,
)
from mindroom.api.egress_credentials import (
    EgressOAuthStatus,
    egress_oauth_provider,
    egress_oauth_status,
    egress_service_status,
    source_status_fields,
    unavailable_egress_oauth_status,
)
from mindroom.egress_broker.oauth_source import oauth_status
from mindroom.egress_broker.secrets import delete_secret, save_secret
from mindroom.egress_broker.service import active_audit_log, active_ca_pem
from mindroom.oauth.registry import load_oauth_providers_for_snapshot

if TYPE_CHECKING:
    from mindroom.api.credentials_target import RequestCredentialsTarget
    from mindroom.config.main import Config
    from mindroom.egress_broker.secrets import OAuthStatus

router = APIRouter(prefix="/api/egress-broker", tags=["egress-broker"])


class ServiceStatus(BaseModel):
    """Status of one egress service.

    `configured` is true when either secret source is available and `updated_at` is the API key's timestamp, both
    kept for older clients. `active_source` says which source the broker uses: an explicit key wins over OAuth.
    """

    name: str
    display_name: str | None
    description: str
    configured: bool
    updated_at: str | None
    active_source: Literal["key", "oauth"] | None
    key_configured: bool
    key_updated_at: str | None
    oauth: EgressOAuthStatus | None


class ServicesResponse(BaseModel):
    """List of egress services with their status."""

    services: list[ServiceStatus]


class PutSecretRequest(BaseModel):
    """Request body for setting a secret."""

    secret: str


class AuditRecordResponse(BaseModel):
    """One audit log record for API responses."""

    at: str
    kind: str
    scope: str
    agent_name: str | None
    requester_id: str | None
    method: str
    host: str
    path: str
    service: str | None
    status: int
    bytes_up: int
    bytes_down: int
    duration_ms: int


class AuditLogsResponse(BaseModel):
    """Audit log query response."""

    records: list[AuditRecordResponse]


async def _admin_oauth_status(
    request: Request,
    config: Config,
    target: RequestCredentialsTarget,
    name: str,
    provider_id: str,
) -> OAuthStatus | None:
    """Load a provider's connection state for the selected scope with the dashboard's own OAuth status route."""
    from mindroom.api import config_lifecycle  # noqa: PLC0415

    provider = load_oauth_providers_for_snapshot(config_lifecycle.bind_current_request_snapshot(request)).get(
        provider_id,
    )
    if provider is None:
        return None
    try:
        result = await oauth.status(provider_id, request, agent_name=target.agent_name)
    except HTTPException:
        return unavailable_egress_oauth_status(provider)
    return await egress_oauth_status(
        result,
        can_manage=True,
        stored_connection=partial(
            oauth_status,
            provider_id,
            worker_target_for_credentials_target(target),
            service=name,
            config=config,
            runtime_paths=target.runtime_paths,
            credentials_manager=target.base_manager,
        ),
    )


async def _load_service_statuses_for_target(
    request: Request,
    target: RequestCredentialsTarget,
) -> list[ServiceStatus]:
    """Build service status list for one target."""
    from mindroom.api import config_lifecycle  # noqa: PLC0415

    config = config_lifecycle.bind_current_request_snapshot(request).runtime_config
    if config is None:
        return []

    worker_target = worker_target_for_credentials_target(target)

    services: list[ServiceStatus] = []
    for name, service_config in config.egress_broker.services.items():
        oauth_part = (
            await _admin_oauth_status(request, config, target, name, service_config.oauth_provider)
            if service_config.oauth_provider is not None
            else None
        )
        status = egress_service_status(target.base_manager, worker_target, service_config, name, oauth_part)
        services.append(
            ServiceStatus(
                name=name,
                display_name=service_config.display_name,
                description=service_config.description,
                **source_status_fields(status),
            ),
        )

    return services


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


@router.put("/services/{name}/secret", status_code=204)
def put_service_secret(
    request: Request,
    name: str,
    body: PutSecretRequest,
    agent_name: Annotated[str | None, Query()] = None,
) -> None:
    """Set an egress service secret.

    Raises 404 if the service is not configured, 422 if the secret is invalid.
    """
    from mindroom.api import config_lifecycle  # noqa: PLC0415

    config = config_lifecycle.bind_current_request_snapshot(request).runtime_config
    if config is None or name not in config.egress_broker.services:
        raise HTTPException(status_code=404, detail=f"Service '{name}' is not configured")

    target = resolve_request_credentials_target(
        request,
        agent_name=agent_name,
        service_names=(),
    )

    worker_target = worker_target_for_credentials_target(target)

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
    from mindroom.api import config_lifecycle  # noqa: PLC0415

    config = config_lifecycle.bind_current_request_snapshot(request).runtime_config
    if config is None or name not in config.egress_broker.services:
        raise HTTPException(status_code=404, detail=f"Service '{name}' is not configured")

    target = resolve_request_credentials_target(
        request,
        agent_name=agent_name,
        service_names=(),
    )

    worker_target = worker_target_for_credentials_target(target)
    delete_secret(target.base_manager, worker_target, name)


@router.post("/services/{name}/connect")
async def connect_service_account(
    request: Request,
    name: str,
    agent_name: Annotated[str | None, Query()] = None,
) -> oauth.OAuthConnectResponse:
    """Start the OAuth flow of a service's provider for the selected scope.

    Raises 404 if the service is not configured or has no usable OAuth provider, 409 if a service account
    replaces personal accounts for that provider.
    """
    provider = egress_oauth_provider(request, name)
    return await oauth.connect(provider.id, request, agent_name=agent_name)


@router.post("/services/{name}/disconnect")
async def disconnect_service_account(
    request: Request,
    name: str,
    agent_name: Annotated[str | None, Query()] = None,
) -> dict[str, str]:
    """Reset the OAuth connection of a service's provider for the selected scope."""
    provider = egress_oauth_provider(request, name)
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

    return AuditLogsResponse(
        records=[
            AuditRecordResponse(
                at=rec.at.isoformat(),
                kind=rec.kind,
                scope=rec.scope,
                agent_name=rec.agent_name,
                requester_id=rec.requester_id,
                method=rec.method,
                host=rec.host,
                path=rec.path,
                service=rec.service,
                status=rec.status,
                bytes_up=rec.bytes_up,
                bytes_down=rec.bytes_down,
                duration_ms=rec.duration_ms,
            )
            for rec in records
        ],
    )


@router.get("/ca.pem", response_class=PlainTextResponse)
def get_ca_pem() -> str:
    """Download the broker's root CA certificate in PEM format.

    Returns 409 if the broker is not running.
    """
    ca_pem = active_ca_pem()
    if ca_pem is None:
        raise HTTPException(status_code=409, detail="Egress broker is not running")
    return ca_pem
