"""Egress broker admin API routes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from mindroom.api.credentials_target import (
    resolve_request_credentials_target,
    worker_target_for_credentials_target,
)
from mindroom.egress_broker.secrets import delete_secret, save_secret, secret_status
from mindroom.egress_broker.service import active_audit_log, active_ca_pem

if TYPE_CHECKING:
    from mindroom.api.credentials_target import RequestCredentialsTarget

router = APIRouter(prefix="/api/egress-broker", tags=["egress-broker"])


class ServiceStatus(BaseModel):
    """Status of one egress service."""

    name: str
    display_name: str | None
    description: str
    configured: bool
    updated_at: str | None


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


def _load_service_statuses_for_target(
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
        status = secret_status(target.base_manager, worker_target, name)
        services.append(
            ServiceStatus(
                name=name,
                display_name=service_config.display_name,
                description=service_config.description,
                configured=status.configured,
                updated_at=status.updated_at,
            ),
        )

    return services


@router.get("/services", response_model=ServicesResponse)
def get_services(
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

    services = _load_service_statuses_for_target(request, target)
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
