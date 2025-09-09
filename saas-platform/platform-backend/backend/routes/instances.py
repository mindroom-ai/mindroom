from __future__ import annotations

from typing import Any

from backend.config import PROVISIONER_API_KEY
from backend.deps import ensure_supabase, verify_user
from backend.routes.provisioner import (
    provision_instance,
    restart_instance_provisioner,
    start_instance_provisioner,
    stop_instance_provisioner,
)
from fastapi import APIRouter, Depends, HTTPException

router = APIRouter()


@router.get("/api/v1/instances")
async def list_user_instances(user=Depends(verify_user)) -> dict[str, Any]:  # noqa: B008
    """List instances for current user."""
    sb = ensure_supabase()

    try:
        account_id = user["account_id"]
        result = sb.table("instances").select("*").eq("account_id", account_id).execute()
        return {"instances": result.data or []}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to fetch instances") from e


@router.post("/api/v1/instances/provision")
async def provision_user_instance(user=Depends(verify_user)) -> dict[str, Any]:  # noqa: B008
    """Provision an instance for the current user."""
    sb = ensure_supabase()

    try:
        account_id = user["account_id"]
        sub_result = sb.table("subscriptions").select("*").eq("account_id", account_id).single().execute()
        if not sub_result.data:
            raise HTTPException(status_code=404, detail="No subscription found")

        subscription = sub_result.data
        inst_result = sb.table("instances").select("id").eq("subscription_id", subscription["id"]).execute()
        if inst_result.data:
            raise HTTPException(status_code=400, detail="Instance already exists")

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


@router.post("/api/v1/instances/{instance_id}/start")
async def start_user_instance(instance_id: str, user=Depends(verify_user)) -> dict[str, Any]:  # noqa: B008
    """Start user's instance."""
    sb = ensure_supabase()

    result = (
        sb.table("instances")
        .select("id")
        .eq("instance_id", instance_id)
        .eq("account_id", user["account_id"])
        .single()
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Instance not found or access denied")

    return await start_instance_provisioner(instance_id, f"Bearer {PROVISIONER_API_KEY}")


@router.post("/api/v1/instances/{instance_id}/stop")
async def stop_user_instance(instance_id: str, user=Depends(verify_user)) -> dict[str, Any]:  # noqa: B008
    """Stop user's instance."""
    sb = ensure_supabase()

    result = (
        sb.table("instances")
        .select("id")
        .eq("instance_id", instance_id)
        .eq("account_id", user["account_id"])
        .single()
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Instance not found or access denied")

    return await stop_instance_provisioner(instance_id, f"Bearer {PROVISIONER_API_KEY}")


@router.post("/api/v1/instances/{instance_id}/restart")
async def restart_user_instance(instance_id: str, user=Depends(verify_user)) -> dict[str, Any]:  # noqa: B008
    """Restart user's instance."""
    sb = ensure_supabase()

    result = (
        sb.table("instances")
        .select("id")
        .eq("instance_id", instance_id)
        .eq("account_id", user["account_id"])
        .single()
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Instance not found or access denied")

    return await restart_instance_provisioner(instance_id, f"Bearer {PROVISIONER_API_KEY}")
