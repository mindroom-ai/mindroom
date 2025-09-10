"""Instance management routes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from backend.config import PROVISIONER_API_KEY
from backend.deps import ensure_supabase, verify_user
from backend.models import ActionResult, InstancesResponse, ProvisionResponse
from backend.routes.provisioner import (
    provision_instance,
    restart_instance_provisioner,
    start_instance_provisioner,
    stop_instance_provisioner,
)
from fastapi import APIRouter, Depends, HTTPException

router = APIRouter()


@router.get("/my/instances", response_model=InstancesResponse)
async def list_user_instances(user: Annotated[dict, Depends(verify_user)]) -> dict[str, Any]:
    """List instances for current user."""
    sb = ensure_supabase()

    try:
        account_id = user["account_id"]
        result = sb.table("instances").select("*").eq("account_id", account_id).execute()
        return {"instances": result.data or []}  # noqa: TRY300
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to fetch instances") from e


@router.post("/my/instances/provision", response_model=ProvisionResponse)
async def provision_user_instance(user: Annotated[dict, Depends(verify_user)]) -> dict[str, Any]:
    """Provision an instance for the current user."""
    sb = ensure_supabase()

    try:
        account_id = user["account_id"]
        sub_result = sb.table("subscriptions").select("*").eq("account_id", account_id).limit(1).execute()
        if not sub_result.data:
            raise HTTPException(status_code=404, detail="No subscription found")  # noqa: TRY301
        subscription = sub_result.data[0]
        inst_result = (
            sb.table("instances")
            .select("*")
            .eq("subscription_id", subscription["id"])  # one instance per subscription
            .limit(1)
            .execute()
        )
        if inst_result.data:
            # Idempotent: return existing instance metadata
            existing = inst_result.data[0]
            return {
                "success": True,
                "message": "Instance already exists",
                "customer_id": existing.get("instance_id") or existing.get("subdomain") or "",
                "frontend_url": existing.get("frontend_url") or existing.get("instance_url"),
                "api_url": existing.get("backend_url") or existing.get("api_url"),
                "matrix_url": existing.get("matrix_server_url") or existing.get("matrix_url"),
            }

        return await provision_instance(
            data={
                "subscription_id": subscription["id"],
                "account_id": account_id,
                "tier": subscription["tier"],
            },
            authorization=f"Bearer {PROVISIONER_API_KEY}",
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to provision instance") from e


# Helper function for user instance actions
async def _verify_instance_ownership_and_proxy(
    instance_id: int,
    user: dict,
    provisioner_func: Callable,
) -> dict[str, Any]:
    """Verify user owns instance and proxy to provisioner."""
    sb = ensure_supabase()

    result = (
        sb.table("instances")
        .select("id")
        .eq("instance_id", instance_id)
        .eq("account_id", user["account_id"])
        .limit(1)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Instance not found or access denied")

    return await provisioner_func(instance_id, f"Bearer {PROVISIONER_API_KEY}")


@router.post("/my/instances/{instance_id}/start", response_model=ActionResult)
async def start_user_instance(instance_id: int, user: Annotated[dict, Depends(verify_user)]) -> dict[str, Any]:
    """Start user's instance."""
    return await _verify_instance_ownership_and_proxy(instance_id, user, start_instance_provisioner)


@router.post("/my/instances/{instance_id}/stop", response_model=ActionResult)
async def stop_user_instance(instance_id: int, user: Annotated[dict, Depends(verify_user)]) -> dict[str, Any]:
    """Stop user's instance."""
    return await _verify_instance_ownership_and_proxy(instance_id, user, stop_instance_provisioner)


@router.post("/my/instances/{instance_id}/restart", response_model=ActionResult)
async def restart_user_instance(instance_id: int, user: Annotated[dict, Depends(verify_user)]) -> dict[str, Any]:
    """Restart user's instance."""
    return await _verify_instance_ownership_and_proxy(instance_id, user, restart_instance_provisioner)
