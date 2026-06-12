"""Instance provisioning and management routes.

Thin HTTP shell over the provisioner service: validate the provisioner API key,
parse the request, and delegate to `backend.services.provisioner_service`.
"""

import hmac
from typing import Annotated, Any

from backend.config import PROVISIONER_API_KEY, logger
from backend.deps import _extract_bearer_token, ensure_supabase, limiter
from backend.models import ActionResult, ProvisionResponse, SyncResult
from backend.services import provisioner_service
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request

router = APIRouter()


def _require_provisioner_auth(authorization: str | None) -> None:
    """Validate provisioner API key using constant-time comparison."""
    try:
        token = _extract_bearer_token(authorization)
    except HTTPException:
        raise HTTPException(status_code=401, detail="Unauthorized") from None
    if not PROVISIONER_API_KEY:
        logger.error("PROVISIONER_API_KEY is not configured")
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not hmac.compare_digest(token, PROVISIONER_API_KEY):
        raise HTTPException(status_code=401, detail="Unauthorized")


@router.post("/system/provision", response_model=ProvisionResponse)
@limiter.limit("5/minute")
async def provision_instance(
    request: Request,  # noqa: ARG001
    data: dict,
    authorization: Annotated[str | None, Header()] = None,
    background_tasks: BackgroundTasks = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Provision a new instance (compatible with customer portal)."""
    _require_provisioner_auth(authorization)
    return await provisioner_service.provision_instance(ensure_supabase(), data=data, background_tasks=background_tasks)


@router.post("/system/instances/{instance_id}/start", response_model=ActionResult)
@limiter.limit("10/minute")
async def start_instance_provisioner(
    request: Request,  # noqa: ARG001
    instance_id: int,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Start an instance (provisioner API compatible)."""
    _require_provisioner_auth(authorization)
    return await provisioner_service.start_instance(instance_id)


@router.post("/system/instances/{instance_id}/stop", response_model=ActionResult)
@limiter.limit("10/minute")
async def stop_instance_provisioner(
    request: Request,  # noqa: ARG001
    instance_id: int,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Stop an instance (provisioner API compatible)."""
    _require_provisioner_auth(authorization)
    return await provisioner_service.stop_instance(instance_id)


@router.post("/system/instances/{instance_id}/restart", response_model=ActionResult)
@limiter.limit("10/minute")
async def restart_instance_provisioner(
    request: Request,  # noqa: ARG001
    instance_id: int,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Restart an instance (provisioner API compatible)."""
    _require_provisioner_auth(authorization)
    return await provisioner_service.restart_instance(instance_id)


@router.delete("/system/instances/{instance_id}/uninstall", response_model=ActionResult)
@limiter.limit("2/minute")
async def uninstall_instance(
    request: Request,  # noqa: ARG001
    instance_id: int,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Completely uninstall/deprovision an instance."""
    _require_provisioner_auth(authorization)
    return await provisioner_service.uninstall_instance(instance_id)


@router.post("/system/sync-instances", response_model=SyncResult)
@limiter.limit("5/minute")
async def sync_instances(
    request: Request,  # noqa: ARG001
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Sync instance states between database and Kubernetes cluster."""
    _require_provisioner_auth(authorization)
    return await provisioner_service.sync_instances(ensure_supabase())
