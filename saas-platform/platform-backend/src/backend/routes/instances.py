"""Instance management routes."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from backend.config import PROVISIONER_API_KEY, logger
from backend.deps import ensure_supabase, verify_user
from backend.k8s import check_deployment_exists, run_kubectl
from backend.models import ActionResult, InstancesResponse, ProvisionResponse
from backend.routes.provisioner import (
    provision_instance,
    restart_instance_provisioner,
    start_instance_provisioner,
    stop_instance_provisioner,
)
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

router = APIRouter()

# Track instances being synced to prevent duplicates
_syncing_instances: set[str] = set()


async def _background_sync_instance_status(instance_id: str) -> None:
    """Background task to sync a single instance's Kubernetes status."""
    if instance_id in _syncing_instances:
        return  # Already syncing

    _syncing_instances.add(instance_id)
    start = time.perf_counter()

    try:
        sb = ensure_supabase()

        # Get current status from DB
        result = sb.table("instances").select("status").eq("instance_id", instance_id).single().execute()
        current_status = result.data.get("status") if result.data else None

        # Check if deployment exists
        try:
            k8s_start = time.perf_counter()
            exists = await check_deployment_exists(instance_id)
        except FileNotFoundError:
            logger.warning("kubectl not found, cannot sync instance %s", instance_id)
            return

        if exists:
            # Get actual replicas count
            code, out, _ = await run_kubectl(
                [
                    "get",
                    f"deployment/mindroom-backend-{instance_id}",
                    "-o=jsonpath={.spec.replicas}",
                ],
                namespace="mindroom-instances",
            )
            if code == 0:
                replicas = int(out.strip() or "0")
                actual_status = "running" if replicas > 0 else "stopped"
            else:
                actual_status = "error"
        elif current_status in ["deprovisioned", "provisioning"]:
            # Keep current status if it makes sense
            actual_status = current_status
        else:
            actual_status = "error"

        # Update database
        sb.table("instances").update(
            {
                "status": actual_status,
                "kubernetes_synced_at": datetime.now(UTC).isoformat(),
                "updated_at": datetime.now(UTC).isoformat(),
            },
        ).eq("instance_id", instance_id).execute()

        total_time = (time.perf_counter() - start) * 1000
        k8s_time = (time.perf_counter() - k8s_start) * 1000
        logger.info(
            "Background K8s sync for instance %s: status=%s, K8s calls %.2fms, total %.2fms",
            instance_id,
            actual_status,
            k8s_time,
            total_time,
        )
    finally:
        _syncing_instances.discard(instance_id)


@router.get("/my/instances", response_model=InstancesResponse)
async def list_user_instances(
    user: Annotated[dict, Depends(verify_user)],
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    """List instances for current user with background status refresh."""
    start = time.perf_counter()
    sb = ensure_supabase()
    account_id = user["account_id"]
    result = sb.table("instances").select("*").eq("account_id", account_id).execute()

    instances = result.data or []
    db_time = (time.perf_counter() - start) * 1000

    # Check if any instance needs a background sync (older than 30 seconds)
    stale_threshold = datetime.now(UTC) - timedelta(seconds=30)
    for instance in instances:
        instance_id = instance.get("instance_id")
        if not instance_id:
            continue

        # Skip if already being synced
        if str(instance_id) in _syncing_instances:
            continue

        # Check if kubernetes_synced_at is missing or stale
        synced_at = instance.get("kubernetes_synced_at")
        if not synced_at:
            needs_sync = True
        else:
            # Parse ISO timestamp (handle both Z and +00:00 formats)
            if synced_at.endswith("Z"):
                synced_at = synced_at[:-1] + "+00:00"
            synced_time = datetime.fromisoformat(synced_at)
            needs_sync = synced_time < stale_threshold

        if needs_sync:
            logger.info("Instance %s has stale K8s status, scheduling background sync", instance_id)
            background_tasks.add_task(_background_sync_instance_status, str(instance_id))

    # Log cache effectiveness
    total_time = (time.perf_counter() - start) * 1000
    logger.info("Instances endpoint: DB query %.2fms, total %.2fms (cached K8s status)", db_time, total_time)

    # Return cached data immediately
    return {"instances": instances}


@router.post("/my/instances/provision", response_model=ProvisionResponse)
async def provision_user_instance(user: Annotated[dict, Depends(verify_user)]) -> dict[str, Any]:
    """Provision an instance for the current user."""
    sb = ensure_supabase()

    account_id = user["account_id"]
    sub_result = sb.table("subscriptions").select("*").eq("account_id", account_id).limit(1).execute()
    if not sub_result.data:
        raise HTTPException(status_code=404, detail="No subscription found")
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
