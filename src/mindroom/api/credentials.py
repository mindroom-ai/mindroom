"""Unified credentials management API."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from mindroom.credentials import CredentialsManager, get_credentials_manager, validate_service_name
from mindroom.tool_system.worker_routing import (
    SHARED_ONLY_INTEGRATION_NAMES,
    ToolExecutionIdentity,
    WorkerScope,
    requires_shared_only_integration_scope,
    resolve_worker_key,
    unsupported_shared_only_integration_message,
    worker_scope_allows_shared_only_integrations,
)

router = APIRouter(prefix="/api/credentials", tags=["credentials"])

_UNSUPPORTED_DASHBOARD_WORKER_SCOPES = {"room_thread"}


def _filter_internal_keys(credentials: dict[str, Any]) -> dict[str, Any]:
    """Remove internal metadata keys (prefixed with _) from credentials."""
    return {k: v for k, v in credentials.items() if not k.startswith("_")}


def _validated_service(service: str) -> str:
    try:
        return validate_service_name(service)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@dataclass(frozen=True)
class RequestCredentialsTarget:
    """Resolved credential target for one dashboard/API request."""

    base_manager: CredentialsManager
    target_manager: CredentialsManager
    worker_scope: WorkerScope | None
    agent_name: str | None
    execution_identity: ToolExecutionIdentity | None


def _request_auth_user(request: Request) -> dict[str, Any] | None:
    auth_user = getattr(request.state, "auth_user", None)
    return auth_user if isinstance(auth_user, dict) else None


def _build_dashboard_execution_identity(request: Request, agent_name: str) -> ToolExecutionIdentity:
    auth_user = _request_auth_user(request) or {}
    user_id = auth_user.get("user_id")
    requester_id = user_id if isinstance(user_id, str) and user_id else None
    tenant_id = os.getenv("CUSTOMER_ID")
    account_id = os.getenv("ACCOUNT_ID")
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name=agent_name,
        requester_id=requester_id,
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
        tenant_id=tenant_id,
        account_id=account_id,
    )


def _reject_raw_worker_targeting(request: Request) -> None:
    for param_name in ("worker_key", "source_worker_key"):
        if request.query_params.get(param_name):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Query parameter '{param_name}' is not supported on the dashboard credentials API. "
                    "Use agent_name to resolve the scoped worker target."
                ),
            )


def resolve_request_credentials_target(
    request: Request,
    *,
    agent_name: str | None = None,
    credentials_manager: CredentialsManager | None = None,
    service_names: tuple[str, ...] = (),
) -> RequestCredentialsTarget:
    """Resolve the credential storage target for one authenticated dashboard request."""
    _reject_raw_worker_targeting(request)

    base_manager = credentials_manager or get_credentials_manager()

    if not agent_name:
        return RequestCredentialsTarget(
            base_manager=base_manager,
            target_manager=base_manager,
            worker_scope=None,
            agent_name=None,
            execution_identity=None,
        )

    from mindroom.api.main import load_runtime_config  # noqa: PLC0415

    config, _ = load_runtime_config()
    if agent_name not in config.agents:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {agent_name}")

    worker_scope = config.get_agent_worker_scope(agent_name)
    if worker_scope is None:
        return RequestCredentialsTarget(
            base_manager=base_manager,
            target_manager=base_manager,
            worker_scope=None,
            agent_name=agent_name,
            execution_identity=None,
        )

    if worker_scope in _UNSUPPORTED_DASHBOARD_WORKER_SCOPES:
        raise HTTPException(
            status_code=400,
            detail=(
                "Dashboard credential management does not support "
                f"worker_scope={worker_scope} for agent '{agent_name}'."
            ),
        )

    if not worker_scope_allows_shared_only_integrations(worker_scope):
        for service_name in service_names:
            if not requires_shared_only_integration_scope(service_name):
                continue
            raise HTTPException(
                status_code=400,
                detail=unsupported_shared_only_integration_message(
                    service_name,
                    worker_scope,
                    agent_name=agent_name,
                ),
            )

    execution_identity = _build_dashboard_execution_identity(request, agent_name)
    worker_key = resolve_worker_key(worker_scope, execution_identity, agent_name=agent_name)
    if worker_key is None:
        raise HTTPException(
            status_code=400,
            detail=f"Could not resolve worker credentials for agent '{agent_name}'.",
        )

    return RequestCredentialsTarget(
        base_manager=base_manager,
        target_manager=base_manager.for_worker(worker_key),
        worker_scope=worker_scope,
        agent_name=agent_name,
        execution_identity=execution_identity,
    )


def load_credentials_for_target(service: str, target: RequestCredentialsTarget) -> dict[str, Any] | None:
    """Load credentials for the resolved target, including scoped overlays when needed."""
    if target.worker_scope is None or target.agent_name is None or target.execution_identity is None:
        return target.target_manager.load_credentials(service)

    shared_credentials = target.base_manager.load_credentials(service)
    merged_credentials: dict[str, Any] = {}
    if isinstance(shared_credentials, dict) and shared_credentials.get("_source") == "env":
        merged_credentials.update(shared_credentials)

    worker_credentials = target.target_manager.load_credentials(service)
    if isinstance(worker_credentials, dict):
        merged_credentials.update(worker_credentials)
    return merged_credentials or None


class SetApiKeyRequest(BaseModel):
    """Request to set an API key."""

    service: str
    api_key: str
    key_name: str = "api_key"


class CredentialStatus(BaseModel):
    """Status of a service's credentials."""

    service: str
    has_credentials: bool
    key_names: list[str] | None = None


class SetCredentialsRequest(BaseModel):
    """Request to set multiple credentials for a service."""

    credentials: dict[str, Any]  # Can be strings, booleans, numbers, etc.


@router.get("/list")
async def list_services(
    request: Request,
    agent_name: str | None = None,
) -> list[str]:
    """List all services with stored credentials."""
    target = resolve_request_credentials_target(request, agent_name=agent_name)
    if target.worker_scope is None:
        return target.target_manager.list_services()
    worker_services = set(target.target_manager.list_services())
    env_services = {
        service
        for service in target.base_manager.list_services()
        if (credentials := target.base_manager.load_credentials(service)) is not None
        and credentials.get("_source") == "env"
    }
    services = worker_services | env_services
    if not worker_scope_allows_shared_only_integrations(target.worker_scope):
        services -= SHARED_ONLY_INTEGRATION_NAMES
    return sorted(services)


@router.get("/{service}/status")
async def get_credential_status(
    service: str,
    request: Request,
    agent_name: str | None = None,
) -> CredentialStatus:
    """Get the status of credentials for a service."""
    service = _validated_service(service)
    target = resolve_request_credentials_target(request, agent_name=agent_name, service_names=(service,))
    credentials = load_credentials_for_target(service, target)

    if credentials:
        filtered = _filter_internal_keys(credentials) if isinstance(credentials, dict) else {}
        return CredentialStatus(
            service=service,
            has_credentials=True,
            key_names=list(filtered.keys()) if filtered else None,
        )

    return CredentialStatus(service=service, has_credentials=False)


@router.post("/{service}")
async def set_credentials(
    service: str,
    http_request: Request,
    payload: SetCredentialsRequest,
    agent_name: str | None = None,
) -> dict[str, str]:
    """Set multiple credentials for a service."""
    service = _validated_service(service)
    target = resolve_request_credentials_target(http_request, agent_name=agent_name, service_names=(service,))

    # Mark as UI-sourced and save
    creds = {**payload.credentials, "_source": "ui"}
    target.target_manager.save_credentials(service, creds)

    return {"status": "success", "message": f"Credentials saved for {service}"}


@router.post("/{service}/api-key")
async def set_api_key(
    service: str,
    http_request: Request,
    request: SetApiKeyRequest,
    agent_name: str | None = None,
) -> dict[str, str]:
    """Set an API key for a service."""
    service = _validated_service(service)
    request_service = _validated_service(request.service)
    if request_service != service:
        raise HTTPException(status_code=400, detail="Service mismatch in request")

    target = resolve_request_credentials_target(http_request, agent_name=agent_name, service_names=(service,))
    credentials = load_credentials_for_target(service, target) or {}
    credentials[request.key_name] = request.api_key
    credentials["_source"] = "ui"
    target.target_manager.save_credentials(service, credentials)

    return {"status": "success", "message": f"API key set for {service}"}


@router.get("/{service}/api-key")
async def get_api_key(
    service: str,
    request: Request,
    key_name: str = "api_key",
    include_value: bool = False,
    agent_name: str | None = None,
) -> dict[str, Any]:
    """Get API key metadata for a service, and optionally the full key value."""
    service = _validated_service(service)
    target = resolve_request_credentials_target(request, agent_name=agent_name, service_names=(service,))
    credentials = load_credentials_for_target(service, target) or {}
    api_key = credentials.get(key_name)

    if api_key:
        source = credentials.get("_source")
        response = {
            "service": service,
            "has_key": True,
            "key_name": key_name,
            # Return masked version
            "masked_key": f"{api_key[:4]}...{api_key[-4:]}" if len(api_key) > 8 else "****",
            "source": source,
        }
        if include_value:
            response["api_key"] = api_key
        return response

    return {"service": service, "has_key": False, "key_name": key_name}


@router.get("/{service}")
async def get_credentials(
    service: str,
    request: Request,
    agent_name: str | None = None,
) -> dict[str, Any]:
    """Get credentials for a service (for editing)."""
    service = _validated_service(service)
    target = resolve_request_credentials_target(request, agent_name=agent_name, service_names=(service,))
    credentials = load_credentials_for_target(service, target)

    if not credentials:
        return {"service": service, "credentials": {}}

    return {"service": service, "credentials": _filter_internal_keys(credentials)}


@router.delete("/{service}")
async def delete_credentials(
    service: str,
    request: Request,
    agent_name: str | None = None,
) -> dict[str, str]:
    """Delete all credentials for a service."""
    service = _validated_service(service)
    target = resolve_request_credentials_target(request, agent_name=agent_name, service_names=(service,))
    target.target_manager.delete_credentials(service)

    return {"status": "success", "message": f"Credentials deleted for {service}"}


@router.post("/{service}/copy-from/{source_service}")
async def copy_credentials(
    service: str,
    source_service: str,
    request: Request,
    agent_name: str | None = None,
) -> dict[str, str]:
    """Copy credentials from one service to another."""
    service = _validated_service(service)
    source_service = _validated_service(source_service)
    target = resolve_request_credentials_target(
        request,
        agent_name=agent_name,
        service_names=(service, source_service),
    )
    source_creds = load_credentials_for_target(source_service, target)

    if not source_creds:
        raise HTTPException(status_code=404, detail=f"No credentials found for {source_service}")

    # Copy credentials, marking as UI-sourced
    target_creds = {k: v for k, v in source_creds.items() if not k.startswith("_")}
    target_creds["_source"] = "ui"
    target.target_manager.save_credentials(service, target_creds)

    return {"status": "success", "message": f"Credentials copied from {source_service} to {service}"}


@router.post("/{service}/test")
async def validate_credentials(
    service: str,
    request: Request,
    agent_name: str | None = None,
) -> dict[str, Any]:
    """Test if credentials are valid for a service."""
    service = _validated_service(service)
    # This is a placeholder - actual testing would depend on the service
    target = resolve_request_credentials_target(request, agent_name=agent_name, service_names=(service,))
    credentials = load_credentials_for_target(service, target)

    if not credentials:
        raise HTTPException(status_code=404, detail=f"No credentials found for {service}")

    # For now, just check if credentials exist
    # In the future, we could implement actual validation per service
    return {
        "service": service,
        "status": "success",
        "message": "Credentials exist (validation not implemented)",
    }
