"""Instance management routes."""

import json
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from backend.config import PROVISIONER_API_KEY, logger
from backend.deps import ensure_supabase, limiter, verify_user
from backend.k8s import check_deployment_exists, run_kubectl
from backend.models import ActionResult, InstancesResponse, ProvisionResponse
from backend.routes.provisioner import (
    provision_instance,
    restart_instance_provisioner,
    start_instance_provisioner,
    stop_instance_provisioner,
)
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request

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
        k8s_start = time.perf_counter()
        exists = await check_deployment_exists(instance_id)

        if exists:
            # Inspect deployment status to understand readiness
            code, out, err = await run_kubectl(
                [
                    "get",
                    f"deployment/mindroom-backend-{instance_id}",
                    "-o=json",
                ],
                namespace="mindroom-instances",
            )
            if code == 0 and out:
                try:
                    deployment = json.loads(out)
                except json.JSONDecodeError:
                    logger.warning(
                        "Failed to parse deployment JSON for instance %s: %s",
                        instance_id,
                        out[:120],
                    )
                    deployment = {}

                if not deployment:
                    actual_status = "provisioning"
                else:
                    spec = deployment.get("spec", {}) or {}
                    status = deployment.get("status", {}) or {}
                    desired_replicas = int(spec.get("replicas") or 0)
                    ready_replicas = int(status.get("readyReplicas") or 0)
                    available_replicas = int(status.get("availableReplicas") or 0)
                    updated_replicas = int(status.get("updatedReplicas") or 0)

                    if desired_replicas == 0:
                        actual_status = "stopped"
                    elif min(ready_replicas, available_replicas, updated_replicas) >= desired_replicas:
                        actual_status = "running"
                    else:
                        actual_status = "provisioning"
            else:
                logger.warning(
                    "kubectl get deployment failed for instance %s: %s",
                    instance_id,
                    err.strip() if err else out.strip(),
                )
                # Fall back to assuming deployment exists but is still provisioning
                actual_status = "provisioning"
        elif current_status in ["deprovisioned", "provisioning"]:
            # Keep current status if it makes sense
            actual_status = current_status
        else:
            actual_status = "error"

        # Update database
        now = datetime.now(UTC).isoformat()
        sb.table("instances").update(
            {
                "status": actual_status,
                "kubernetes_synced_at": now,
                "updated_at": now,
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
@limiter.limit("30/minute")  # Reading is less sensitive
async def list_user_instances(
    request: Request,
    user: Annotated[dict, Depends(verify_user)],
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    """List instances for current user with background status refresh."""
    start = time.perf_counter()
    sb = ensure_supabase()
    account_id = user["account_id"]

    db_start = time.perf_counter()
    result = sb.table("instances").select("*").eq("account_id", account_id).execute()
    db_time = (time.perf_counter() - db_start) * 1000

    instances = result.data or []

    enhanced_instances: list[dict[str, Any]] = []
    for instance in instances:
        status = instance.get("status")
        hint: str | None = None

        if status == "provisioning":
            hint = (
                "Provisioning in progress. First boot can take several minutes while "
                "containers pull images and TLS certificates issue."
            )
        elif status == "restarting":
            hint = "Restarting backend pods; the workspace will be available again shortly."
        elif status == "running" and not instance.get("kubernetes_synced_at"):
            hint = "Running (awaiting initial Kubernetes health check)."

        enhanced_instances.append({**instance, "status_hint": hint})

    # Check if any instance needs a background sync (older than 30 seconds)
    stale_threshold = datetime.now(UTC) - timedelta(seconds=30)
    for instance in enhanced_instances:
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
            logger.info(
                "Instance %s has stale K8s status, scheduling background sync",
                instance_id,
            )
            background_tasks.add_task(_background_sync_instance_status, str(instance_id))

    # Log cache effectiveness
    total_time = (time.perf_counter() - start) * 1000
    logger.info(
        "Instances endpoint: DB query %.2fms, total %.2fms (cached K8s status)",
        db_time,
        total_time,
    )

    # Return cached data immediately
    return {"instances": enhanced_instances}


@router.post("/my/instances/provision", response_model=ProvisionResponse)
@limiter.limit("5/minute")  # Creating instances is expensive
async def provision_user_instance(
    request: Request,
    user: Annotated[dict, Depends(verify_user)],
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    """Provision an instance for the current user."""
    sb = ensure_supabase()

    account_id = user["account_id"]
    sub_result = sb.table("subscriptions").select("*").eq("account_id", account_id).execute()
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
        existing = inst_result.data[0]

        # If instance is deprovisioned, reprovision it
        if existing.get("status") == "deprovisioned":
            logger.info(
                "Reprovisioning %s instance %s for user %s",
                existing.get("status"),
                existing["instance_id"],
                account_id,
            )
            return await provision_instance(
                request=request,
                data={
                    "subscription_id": subscription["id"],
                    "account_id": account_id,
                    "tier": subscription["tier"],
                    "instance_id": existing["instance_id"],  # Reuse the same instance ID
                },
                authorization=f"Bearer {PROVISIONER_API_KEY}",
                background_tasks=background_tasks,
            )

        # Otherwise return existing instance metadata
        status = existing.get("status", "unknown")
        message = "Instance is already provisioning" if status == "provisioning" else "Instance already exists"
        logger.info(
            "Instance already exists for user %s with status %s, returning existing metadata",
            account_id,
            status,
        )
        return {
            "success": True,
            "message": message,
            "customer_id": existing.get("instance_id") or existing.get("subdomain") or "",
            "frontend_url": existing.get("frontend_url") or existing.get("instance_url"),
            "api_url": existing.get("backend_url") or existing.get("api_url"),
            "matrix_url": existing.get("matrix_server_url") or existing.get("matrix_url"),
        }

    return await provision_instance(
        request=request,
        data={
            "subscription_id": subscription["id"],
            "account_id": account_id,
            "tier": subscription["tier"],
        },
        authorization=f"Bearer {PROVISIONER_API_KEY}",
        background_tasks=background_tasks,
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
@limiter.limit("10/minute")  # Control actions moderate rate
async def start_user_instance(
    request: Request, instance_id: int, user: Annotated[dict, Depends(verify_user)]
) -> dict[str, Any]:
    """Start user's instance."""
    return await _verify_instance_ownership_and_proxy(instance_id, user, start_instance_provisioner)


@router.post("/my/instances/{instance_id}/stop", response_model=ActionResult)
@limiter.limit("10/minute")  # Control actions moderate rate
async def stop_user_instance(
    request: Request, instance_id: int, user: Annotated[dict, Depends(verify_user)]
) -> dict[str, Any]:
    """Stop user's instance."""
    return await _verify_instance_ownership_and_proxy(instance_id, user, stop_instance_provisioner)


@router.post("/my/instances/{instance_id}/restart", response_model=ActionResult)
@limiter.limit("10/minute")  # Control actions moderate rate
async def restart_user_instance(
    request: Request, instance_id: int, user: Annotated[dict, Depends(verify_user)]
) -> dict[str, Any]:
    """Restart user's instance."""
    return await _verify_instance_ownership_and_proxy(instance_id, user, restart_instance_provisioner)
