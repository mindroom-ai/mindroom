"""Instance management routes."""

import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from backend.config import logger
from backend.deps import ensure_supabase, limiter, verify_user
from backend.entitlements import assert_instance_entitlement, parse_timestamp
from backend.models import ActionResult, InstancesResponse, ProvisionResponse
from backend.services import instance_lifecycle, instances_data, provisioner_service
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request

router = APIRouter()

# Track instances being synced to prevent duplicates
_syncing_instances: set[str] = set()
InstanceAction = Callable[[int], Awaitable[dict[str, Any]]]


async def _background_sync_instance_status(instance_id: str) -> None:
    """Background task: record one instance's Kubernetes status unless a sync of it is already running."""
    if instance_id in _syncing_instances:
        return

    _syncing_instances.add(instance_id)
    try:
        sb = ensure_supabase()
        instance_row = instances_data.get_instance(sb, instance_id, columns="status")
        if instance_row is None:
            logger.warning("Background sync: instance %s not found in database, skipping sync", instance_id)
            return
        await provisioner_service.sync_instance_status(sb, instance_id, instance_row["status"])
    except Exception:
        logger.exception("Background K8s sync failed for instance %s", instance_id)
    finally:
        _syncing_instances.discard(instance_id)


@router.get("/my/instances", response_model=InstancesResponse)
@limiter.limit("30/minute")  # Reading is less sensitive
async def list_user_instances(
    request: Request, user: Annotated[dict, Depends(verify_user)], background_tasks: BackgroundTasks
) -> dict[str, Any]:
    """List instances for current user with background status refresh."""
    start = time.perf_counter()
    sb = ensure_supabase()
    account_id = user["account_id"]

    db_start = time.perf_counter()
    instances = instances_data.get_instances_for_account(sb, account_id)
    db_time = (time.perf_counter() - db_start) * 1000

    # A row whose Kubernetes status is missing or older than 30 seconds is refreshed in the background.
    stale_threshold = datetime.now(UTC) - timedelta(seconds=30)
    enhanced_instances: list[dict[str, Any]] = []
    for instance in instances:
        status = instance.get("status")
        synced_at = instance.get("kubernetes_synced_at")
        hint: str | None = None
        if status == "provisioning":
            hint = (
                "Provisioning in progress. First boot can take several minutes while "
                "containers pull images and TLS certificates issue."
            )
        elif status == "running" and not synced_at:
            hint = "Running (awaiting initial Kubernetes health check)."
        enhanced_instances.append({**instance, "status_hint": hint})

        instance_id = str(instance["instance_id"])
        synced_time = parse_timestamp(synced_at)
        stale = synced_time is None or synced_time < stale_threshold
        if stale and instance_id not in _syncing_instances:
            logger.info("Instance %s has stale K8s status, scheduling background sync", instance_id)
            background_tasks.add_task(_background_sync_instance_status, instance_id)

    # Log cache effectiveness
    total_time = (time.perf_counter() - start) * 1000
    logger.info("Instances endpoint: DB query %.2fms, total %.2fms (cached K8s status)", db_time, total_time)

    # Return cached data immediately
    return {"instances": enhanced_instances}


_PENDING_DELETION_DETAIL = "This account is pending deletion and cannot run instances"


def _existing_instance_response(existing: dict[str, Any], message: str, *, success: bool = True) -> dict[str, Any]:
    """Return provision-route metadata for an instance that already exists."""
    return {
        "success": success,
        "message": message,
        "customer_id": existing.get("instance_id") or existing.get("subdomain") or "",
        "frontend_url": existing.get("frontend_url") or existing.get("instance_url"),
        "api_url": existing.get("backend_url") or existing.get("api_url"),
        "matrix_url": existing.get("matrix_server_url") or existing.get("matrix_url"),
    }


@router.post("/my/instances/provision", response_model=ProvisionResponse)
@limiter.limit("5/minute")  # Creating instances is expensive
async def provision_user_instance(
    request: Request,  # noqa: ARG001
    user: Annotated[dict, Depends(verify_user)],
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    """Provision an instance for the current user."""
    sb = ensure_supabase()

    account_id = user["account_id"]
    sub_result = sb.table("subscriptions").select("*").eq("account_id", account_id).execute()
    if not sub_result.data:
        raise HTTPException(status_code=404, detail="No subscription found")
    provisioner_service.refuse_pending_deletion(sb, account_id, _PENDING_DELETION_DETAIL)
    # Provisioning mints a platform-paid OpenRouter key, so a Stripe-billed status is confirmed with Stripe first.
    subscription = await instance_lifecycle.verified_subscription(sb, sub_result.data[0]["id"])
    if subscription is None:
        raise HTTPException(status_code=404, detail="No subscription found")
    assert_instance_entitlement(subscription, "provision")

    inst_result = (
        sb.table("instances")
        .select("*")
        .eq("subscription_id", subscription["id"])  # one instance per subscription
        .limit(1)
        .execute()
    )
    if inst_result.data:
        existing = inst_result.data[0]

        # An instance stopped or torn down because the subscription lapsed returns through the lifecycle owner.
        if existing.get("lifecycle_stopped_at"):
            await instance_lifecycle.reconcile_subscription_instances(subscription["id"])
            existing = instances_data.get_instance(sb, existing["instance_id"]) or existing
            if existing.get("lifecycle_stopped_at"):
                return _existing_instance_response(
                    existing, "Instance restart failed and will be retried automatically", success=False
                )
            return _existing_instance_response(existing, "Instance restarted")

        # If instance is deprovisioned, reprovision it
        if existing.get("status") == "deprovisioned":
            logger.info(
                "Reprovisioning %s instance %s for user %s", existing.get("status"), existing["instance_id"], account_id
            )
            return await provisioner_service.provision_instance(
                sb,
                data={
                    "subscription_id": subscription["id"],
                    "account_id": account_id,
                    "tier": subscription["tier"],
                    "instance_id": existing["instance_id"],  # Reuse the same instance ID
                },
                background_tasks=background_tasks,
                expected_status="deprovisioned",
            )

        # Otherwise return existing instance metadata
        status = existing.get("status", "unknown")
        message = "Instance is already provisioning" if status == "provisioning" else "Instance already exists"
        logger.info(
            "Instance already exists for user %s with status %s, returning existing metadata", account_id, status
        )
        return _existing_instance_response(existing, message)

    return await provisioner_service.provision_instance(
        sb,
        data={"subscription_id": subscription["id"], "account_id": account_id, "tier": subscription["tier"]},
        background_tasks=background_tasks,
    )


# Helper function for user instance actions
async def _verify_instance_ownership_and_run(
    instance_id: int,
    user: dict,
    instance_action: InstanceAction,
    *,
    require_active_subscription: bool,
) -> dict[str, Any]:
    """Verify user owns instance and run the provisioner service action."""
    sb = ensure_supabase()

    instance = instances_data.get_owned_instance(sb, instance_id, user["account_id"])
    if instance is None:
        raise HTTPException(status_code=404, detail="Instance not found or access denied")

    if require_active_subscription:
        sub_result = sb.table("subscriptions").select("*").eq("id", instance["subscription_id"]).limit(1).execute()
        if not sub_result.data:
            raise HTTPException(status_code=404, detail="Subscription not found")
        assert_instance_entitlement(sub_result.data[0], "run")
        provisioner_service.refuse_pending_deletion(sb, user["account_id"], _PENDING_DELETION_DETAIL)
        if instance.get("lifecycle_stopped_at"):
            # Resuming also re-enables the OpenRouter key and clears the teardown schedule.
            summary = await instance_lifecycle.reconcile_subscription_instances(instance["subscription_id"])
            if summary.errors:
                raise HTTPException(status_code=500, detail=f"Failed to resume instance: {summary.errors[0]}")
            # Reconciliation may have found the stored active status stale and kept the hold.
            sub_result = sb.table("subscriptions").select("*").eq("id", instance["subscription_id"]).limit(1).execute()
            assert_instance_entitlement(sub_result.data[0], "run")
            refreshed = instances_data.get_owned_instance(sb, instance_id, user["account_id"])
            if refreshed is None or refreshed.get("lifecycle_stopped_at"):
                raise HTTPException(status_code=409, detail="Instance is stopped for an inactive subscription")

    return await instance_action(instance_id)


@router.post("/my/instances/{instance_id}/start", response_model=ActionResult)
@limiter.limit("10/minute")  # Control actions moderate rate
async def start_user_instance(
    request: Request,  # noqa: ARG001
    instance_id: int,
    user: Annotated[dict, Depends(verify_user)],
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    """Start user's instance."""
    result = await _verify_instance_ownership_and_run(
        instance_id, user, provisioner_service.start_instance, require_active_subscription=True
    )
    # A stopped instance keeps its old deployment and loses only a key its tier does not include, so once it runs
    # the lifecycle redeploys it with what its tier pays for.
    background_tasks.add_task(instance_lifecycle.reconcile_account_instances, user["account_id"])
    return result


@router.post("/my/instances/{instance_id}/stop", response_model=ActionResult)
@limiter.limit("10/minute")  # Control actions moderate rate
async def stop_user_instance(
    request: Request,  # noqa: ARG001
    instance_id: int,
    user: Annotated[dict, Depends(verify_user)],
) -> dict[str, Any]:
    """Stop user's instance."""
    return await _verify_instance_ownership_and_run(
        instance_id, user, provisioner_service.stop_instance, require_active_subscription=False
    )


@router.post("/my/instances/{instance_id}/restart", response_model=ActionResult)
@limiter.limit("10/minute")  # Control actions moderate rate
async def restart_user_instance(
    request: Request,  # noqa: ARG001
    instance_id: int,
    user: Annotated[dict, Depends(verify_user)],
) -> dict[str, Any]:
    """Restart user's instance."""
    return await _verify_instance_ownership_and_run(
        instance_id, user, provisioner_service.restart_instance, require_active_subscription=True
    )
