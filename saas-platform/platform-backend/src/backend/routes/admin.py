"""Admin-only routes for platform management."""

from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from backend.config import PROVISIONER_API_KEY, logger
from pydantic import BaseModel
from backend.deps import ensure_supabase, limiter, verify_admin
from backend.utils.audit import create_audit_log
from backend.models import (
    ActionResult,
    AdminAccountDetailsResponse,
    AdminCreateResponse,
    AdminDashboardMetricsResponse,
    AdminDeleteResponse,
    AdminGetOneResponse,
    AdminListResponse,
    AdminLogoutResponse,
    AdminStatsOut,
    AdminUpdateResponse,
    ProvisionResponse,
    SyncResult,
    UpdateAccountStatusResponse,
)
from backend.routes.provisioner import (
    provision_instance,
    restart_instance_provisioner,
    start_instance_provisioner,
    stop_instance_provisioner,
    sync_instances,
    uninstall_instance,
)
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request

router = APIRouter()
ALLOWED_RESOURCES = {
    "accounts",
    "subscriptions",
    "instances",
    "audit_logs",
    "usage_metrics",
}


def audit_log_entry(
    account_id: str,
    action: str,
    resource_type: str,
    resource_id: str | None = None,
    details: dict | None = None,
) -> None:
    """Log an admin action to the audit_logs table (best effort)."""
    # Use the shared helper for consistency
    create_audit_log(
        action=action,
        resource_type=resource_type,
        account_id=account_id,
        resource_id=resource_id,
        details=details,
        success=True,  # Admin actions that reach this point are successful
    )


@router.get("/admin/stats", response_model=AdminStatsOut)
@limiter.limit("30/minute")
async def get_admin_stats(
    request: Request,
    admin: Annotated[dict, Depends(verify_admin)],
) -> dict[str, Any]:  # noqa: FAST002, B008, ARG001
    """Get platform statistics for admin dashboard."""
    audit_log_entry(
        account_id=admin["user_id"],
        action="view",
        resource_type="stats",
    )
    sb = ensure_supabase()

    try:
        accounts = sb.table("accounts").select("*", count="exact").execute()
        subscriptions = sb.table("subscriptions").select("*", count="exact").eq("status", "active").execute()
        instances = sb.table("instances").select("*", count="exact").eq("status", "running").execute()

        # Get recent activity for dashboard
        recent_logs = (
            sb.table("audit_logs").select("*, accounts(email)").order("created_at", desc=True).limit(5).execute()
        )

        recent_activity = []
        if recent_logs.data:
            recent_activity.extend(
                {
                    "type": log.get("action", "unknown"),
                    "description": f"{log.get('resource_type', '')} {log.get('action', '')} by {log.get('accounts', {}).get('email', 'System')}",
                    "timestamp": log.get("created_at", ""),
                }
                for log in recent_logs.data
            )

        return {
            "accounts": len(accounts.data) if accounts.data else 0,
            "active_subscriptions": len(subscriptions.data) if subscriptions.data else 0,
            "running_instances": len(instances.data) if instances.data else 0,
        }
    except Exception as e:
        logger.exception("Error fetching admin stats")
        raise HTTPException(status_code=500, detail="Failed to fetch statistics") from e


# Generic proxy for instance management actions
async def _proxy_to_provisioner(
    request: Request,
    provisioner_func: Callable,
    instance_id: int,
    admin: Annotated[dict, Depends(verify_admin)],  # noqa: ARG001
) -> dict[str, Any]:
    """Proxy request to provisioner with API key."""
    return await provisioner_func(request, instance_id, f"Bearer {PROVISIONER_API_KEY}")


@router.post("/admin/instances/{instance_id}/start", response_model=ActionResult)
@limiter.limit("10/minute")
async def admin_start_instance(
    request: Request,
    instance_id: int,
    admin: Annotated[dict, Depends(verify_admin)],  # noqa: FAST002, B008
) -> dict[str, Any]:
    """Start an instance (admin proxy)."""
    result = await _proxy_to_provisioner(request, start_instance_provisioner, instance_id, admin)
    audit_log_entry(
        account_id=admin["user_id"],
        action="start",
        resource_type="instance",
        resource_id=str(instance_id),
    )
    return result


@router.post("/admin/instances/{instance_id}/stop", response_model=ActionResult)
@limiter.limit("10/minute")
async def admin_stop_instance(
    request: Request,
    instance_id: int,
    admin: Annotated[dict, Depends(verify_admin)],  # noqa: FAST002, B008
) -> dict[str, Any]:
    """Stop an instance (admin proxy)."""
    result = await _proxy_to_provisioner(request, stop_instance_provisioner, instance_id, admin)
    audit_log_entry(
        account_id=admin["user_id"],
        action="stop",
        resource_type="instance",
        resource_id=str(instance_id),
    )
    return result


@router.post("/admin/instances/{instance_id}/restart", response_model=ActionResult)
@limiter.limit("10/minute")
async def admin_restart_instance(
    request: Request,
    instance_id: int,
    admin: Annotated[dict, Depends(verify_admin)],  # noqa: FAST002, B008
) -> dict[str, Any]:
    """Restart an instance (admin proxy)."""
    result = await _proxy_to_provisioner(request, restart_instance_provisioner, instance_id, admin)
    audit_log_entry(
        account_id=admin["user_id"],
        action="restart",
        resource_type="instance",
        resource_id=str(instance_id),
    )
    return result


@router.delete("/admin/instances/{instance_id}/uninstall", response_model=ActionResult)
@limiter.limit("2/minute")
async def admin_uninstall_instance(
    request: Request,
    instance_id: int,
    admin: Annotated[dict, Depends(verify_admin)],  # noqa: FAST002, B008
) -> dict[str, Any]:
    """Uninstall an instance (admin proxy)."""
    result = await _proxy_to_provisioner(request, uninstall_instance, instance_id, admin)
    audit_log_entry(
        account_id=admin["user_id"],
        action="uninstall",
        resource_type="instance",
        resource_id=str(instance_id),
    )
    return result


@router.post("/admin/instances/{instance_id}/provision", response_model=ProvisionResponse)
@limiter.limit("5/minute")
async def admin_provision_instance(
    request: Request,  # noqa: ARG001
    instance_id: int,
    background_tasks: BackgroundTasks,
    admin: Annotated[dict, Depends(verify_admin)],  # noqa: FAST002, B008
) -> dict[str, Any]:
    """Provision a deprovisioned instance."""
    sb = ensure_supabase()

    # Get instance details
    result = sb.table("instances").select("*").eq("instance_id", str(instance_id)).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Instance not found")

    instance = result.data[0]
    if instance.get("status") not in ["deprovisioned", "error"]:
        raise HTTPException(
            status_code=400,
            detail="Instance must be deprovisioned or in error state to provision",
        )

    # Call provisioner with existing instance data
    data = {
        "subscription_id": instance.get("subscription_id"),
        "account_id": instance.get("account_id"),
        "tier": instance.get("tier", "free"),
        "instance_id": instance_id,  # Re-use existing instance ID
    }

    # provision_instance expects: request, data, authorization, background_tasks
    result = await provision_instance(
        request=request,
        data=data,
        authorization=f"Bearer {PROVISIONER_API_KEY}",
        background_tasks=background_tasks,
    )
    audit_log_entry(
        account_id=admin["user_id"],
        action="provision",
        resource_type="instance",
        resource_id=str(instance_id),
        details={
            "account_id": instance.get("account_id"),
            "tier": instance.get("tier"),
        },
    )
    return result


@router.post("/admin/sync-instances", response_model=SyncResult)
@limiter.limit("5/minute")
async def admin_sync_instances(request: Request, admin: Annotated[dict, Depends(verify_admin)]) -> dict[str, Any]:  # noqa: FAST002, B008
    """Sync instance states between database and Kubernetes (admin proxy)."""
    result = await sync_instances(request, f"Bearer {PROVISIONER_API_KEY}")
    audit_log_entry(
        account_id=admin["user_id"],
        action="sync",
        resource_type="instances",
        details={"operation": "sync_k8s_database"},
    )
    return result


@router.get("/admin/accounts/{account_id}", response_model=AdminAccountDetailsResponse)
async def get_account_details(
    account_id: str,
    admin: Annotated[dict, Depends(verify_admin)],  # noqa: ARG001, FAST002, B008
) -> dict[str, Any]:
    """Get detailed account information including subscription and instances."""
    sb = ensure_supabase()

    try:
        # Get account details
        account_result = sb.table("accounts").select("*").eq("id", account_id).single().execute()
        if not account_result.data:
            raise HTTPException(status_code=404, detail="Account not found")  # noqa: TRY301

        account = account_result.data

        # Get subscription if exists
        subscription_result = (
            sb.table("subscriptions")
            .select("*")
            .eq("account_id", account_id)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )

        # Get instances if exist
        instances_result = (
            sb.table("instances").select("*").eq("account_id", account_id).order("created_at", desc=True).execute()
        )

        # Build response
        return {
            "account": account,
            "subscription": subscription_result.data[0] if subscription_result.data else None,
            "instances": instances_result.data if instances_result.data else [],
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error fetching account details")
        raise HTTPException(status_code=500, detail="Failed to fetch account details") from e


class UpdateAccountStatusRequest(BaseModel):
    """Request model for updating account status."""

    status: str
    reason: str | None = None


@router.put("/admin/accounts/{account_id}/status", response_model=UpdateAccountStatusResponse)
async def update_account_status(
    account_id: str,
    request: UpdateAccountStatusRequest,
    admin: Annotated[dict, Depends(verify_admin)],  # noqa: FAST002, B008
) -> dict[str, Any]:
    """Update account status (active, suspended, etc)."""
    sb = ensure_supabase()

    valid_statuses = ["active", "suspended", "deleted", "pending_verification"]
    if request.status not in valid_statuses:
        raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of: {valid_statuses}")

    try:
        result = (
            sb.table("accounts")
            .update(
                {
                    "status": request.status,
                    "updated_at": datetime.now(UTC).isoformat(),
                },
            )
            .eq("id", account_id)
            .execute()
        )

        if not result.data:
            raise HTTPException(status_code=404, detail="Account not found")  # noqa: TRY301

        audit_log_entry(
            account_id=admin["user_id"],
            action="update",
            resource_type="account",
            resource_id=account_id,
            details={"status": request.status, "reason": request.reason},
        )

        return {
            "status": "success",
            "account_id": account_id,
            "new_status": request.status,
        }  # noqa: TRY300
    except Exception as e:
        logger.exception("Error updating account status")
        raise HTTPException(status_code=500, detail="Failed to update account status") from e


@router.post("/admin/auth/logout", response_model=AdminLogoutResponse)
async def admin_logout() -> dict[str, bool]:
    """Admin logout placeholder."""
    return {"success": True}


@router.get("/admin/metrics/dashboard", response_model=AdminDashboardMetricsResponse)
@limiter.limit("30/minute")
async def get_dashboard_metrics(
    request: Request,  # noqa: ARG001
    admin: Annotated[dict, Depends(verify_admin)],  # noqa: FAST002, B008
) -> dict[str, Any]:
    """Get dashboard metrics for admin panel."""
    audit_log_entry(
        account_id=admin["user_id"],
        action="view",
        resource_type="dashboard_metrics",
    )
    sb = ensure_supabase()

    try:
        accounts = sb.table("accounts").select("*", count="exact", head=True).execute()
        active_subs = sb.table("subscriptions").select("*", count="exact", head=True).eq("status", "active").execute()
        _ = sb.table("instances").select("*", count="exact", head=True).eq("status", "running").execute()

        subs_data = sb.table("subscriptions").select("tier").eq("status", "active").execute()
        tier_prices = {"starter": 49, "professional": 199, "enterprise": 999, "free": 0}
        mrr = sum(tier_prices.get(sub.get("tier", "free"), 0) for sub in (subs_data.data or []))

        seven_days_ago = (datetime.now(UTC) - timedelta(days=7)).isoformat()
        messages = (
            sb.table("usage_metrics")
            .select("metric_date, messages_sent")
            .gte("metric_date", seven_days_ago)
            .order("metric_date")
            .execute()
        )

        if messages.data:
            by_date = defaultdict(int)
            for m in messages.data:
                date = m["metric_date"][:10]
                by_date[date] += m.get("messages_sent", 0)
            _ = [  # noqa: F841
                {"date": date, "messages_sent": count} for date, count in sorted(by_date.items())
            ]

        all_instances = sb.table("instances").select("status").execute()
        status_counts: dict[str, int] = {}
        if all_instances.data:
            for inst in all_instances.data:
                status = inst.get("status", "unknown")
                status_counts[status] = status_counts.get(status, 0) + 1

        audit_logs = (
            sb.table("audit_logs")
            .select("created_at, action, account_id")
            .order("created_at", desc=True)
            .limit(10)
            .execute()
        )
        recent_activity = audit_logs.data if audit_logs.data else []
    except Exception:
        logger.exception("Error fetching metrics")
        return {
            "total_accounts": 0,
            "active_subscriptions": 0,
            "total_instances": 0,
            "instances_by_status": {},
            "subscription_revenue": 0.0,
            "subscriptions_by_tier": {},
            "recent_signups": [],
            "recent_instances": [],
        }
    else:
        # Build subscriptions by tier
        subscriptions_by_tier = {}
        if subs_data.data:
            for sub in subs_data.data:
                tier = sub.get("tier", "free")
                subscriptions_by_tier[tier] = subscriptions_by_tier.get(tier, 0) + 1

        # Build instances by status dict
        instances_by_status = {status: count for status, count in status_counts.items()}

        return {
            "total_accounts": accounts.count or 0,
            "active_subscriptions": active_subs.count or 0,
            "total_instances": len(all_instances.data) if all_instances.data else 0,
            "instances_by_status": instances_by_status,
            "subscription_revenue": float(mrr),
            "subscriptions_by_tier": subscriptions_by_tier,
            "recent_signups": [],  # Not implemented yet
            "recent_instances": recent_activity if recent_activity else [],
        }


# === React Admin Data Provider ===
@router.get("/admin/{resource}", response_model=AdminListResponse)
@limiter.limit("60/minute")
async def admin_get_list(  # noqa: C901
    request: Request,  # noqa: ARG001
    resource: str,
    admin: Annotated[dict, Depends(verify_admin)],  # noqa: FAST002, B008
    _sort: Annotated[str | None, Query()] = None,
    _order: Annotated[str | None, Query()] = None,
    _start: int = Query(0),  # noqa: FAST002
    _end: int = Query(10),  # noqa: FAST002
    q: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """Generic list endpoint for React Admin."""
    if resource not in ALLOWED_RESOURCES:
        raise HTTPException(status_code=400, detail="Invalid resource")
    sb = ensure_supabase()

    audit_log_entry(
        account_id=admin["user_id"],
        action="list",
        resource_type=resource,
        details={"query": q, "start": _start, "end": _end},
    )

    try:
        # Add joins for better data display in admin panel
        if resource == "instances":
            query = sb.table("instances").select("*, accounts(email, full_name)", count="exact")
        elif resource == "subscriptions":
            query = sb.table("subscriptions").select("*, accounts(email, full_name)", count="exact")
        elif resource == "audit_logs":
            query = sb.table("audit_logs").select("*, accounts(email)", count="exact")
        elif resource == "usage_metrics":
            query = sb.table("usage_metrics").select("*, accounts(email, full_name)", count="exact")
        else:
            query = sb.table(resource).select("*", count="exact")

        if q:
            search_fields = {
                "accounts": ["email", "full_name", "company_name"],
                "instances": ["name", "subdomain"],
                "audit_logs": ["action", "details"],
                "subscriptions": ["tier", "status"],
            }
            if resource in search_fields:
                or_conditions = [f"{field}.ilike.%{q}%" for field in search_fields[resource]]
                query = query.or_(",".join(or_conditions))

        if _sort:
            order_column = f"{_sort}.{_order.lower() if _order else 'asc'}"
            query = query.order(order_column)

        query = query.range(_start, _end - 1)
        result = query.execute()
    except Exception:
        logger.exception("Error in get_list")
        return {"data": [], "total": 0}
    else:
        return {"data": result.data, "total": result.count}


@router.get("/admin/{resource}/{resource_id}", response_model=AdminGetOneResponse)
@limiter.limit("60/minute")
async def admin_get_one(
    request: Request,  # noqa: ARG001
    resource: str,
    resource_id: str,
    admin: Annotated[dict, Depends(verify_admin)],  # noqa: FAST002, B008
) -> dict[str, Any]:
    """Get single record for React Admin."""
    if resource not in ALLOWED_RESOURCES:
        raise HTTPException(status_code=400, detail="Invalid resource")
    sb = ensure_supabase()

    audit_log_entry(
        account_id=admin["user_id"],
        action="read",
        resource_type=resource,
        resource_id=resource_id,
    )

    try:
        result = sb.table(resource).select("*").eq("id", resource_id).single().execute()
    except Exception:
        logger.exception("Error fetching single resource")
        raise HTTPException(status_code=404, detail="Not found") from None
    else:
        return {"data": result.data}


@router.post("/admin/{resource}", response_model=AdminCreateResponse)
@limiter.limit("15/minute")
async def admin_create(
    request: Request,  # noqa: ARG001
    resource: str,
    data: dict,
    admin: Annotated[dict, Depends(verify_admin)],  # noqa: FAST002, B008
) -> dict[str, Any]:
    """Create record for React Admin."""
    if resource not in ALLOWED_RESOURCES:
        raise HTTPException(status_code=400, detail="Invalid resource")
    sb = ensure_supabase()

    try:
        result = sb.table(resource).insert(data).execute()

        # Log admin creation
        if result.data:
            new_id = result.data[0].get("id") if result.data[0] else None
            audit_log_entry(
                account_id=admin["user_id"],
                action="create",
                resource_type=resource,
                resource_id=str(new_id) if new_id else None,
                details={"data": data},
            )

        return {"data": result.data[0] if result.data else None}
    except Exception:
        logger.exception("Error creating resource")
        raise HTTPException(status_code=400, detail="Invalid request") from None


@router.put("/admin/{resource}/{resource_id}", response_model=AdminUpdateResponse)
@limiter.limit("15/minute")
async def admin_update(
    request: Request,  # noqa: ARG001
    resource: str,
    resource_id: str,
    data: dict,
    admin: Annotated[dict, Depends(verify_admin)],  # noqa: FAST002, B008
) -> dict[str, Any]:
    """Update record for React Admin."""
    if resource not in ALLOWED_RESOURCES:
        raise HTTPException(status_code=400, detail="Invalid resource")
    sb = ensure_supabase()

    try:
        data.pop("id", None)
        result = sb.table(resource).update(data).eq("id", resource_id).execute()

        # Log admin update
        audit_log_entry(
            account_id=admin["user_id"],
            action="update",
            resource_type=resource,
            resource_id=resource_id,
            details={"data": data},
        )

        return {"data": result.data[0] if result.data else None}
    except Exception:
        logger.exception("Error updating resource")
        raise HTTPException(status_code=400, detail="Invalid request") from None


@router.delete("/admin/accounts/{account_id}/complete", response_model=AdminDeleteResponse)
@limiter.limit("2/minute")
async def admin_delete_account_complete(
    request: Request,
    account_id: str,
    admin: Annotated[dict, Depends(verify_admin)],  # noqa: FAST002, B008
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    """Completely delete an account with all associated resources."""
    sb = ensure_supabase()

    # Get account details
    account_result = sb.table("accounts").select("*").eq("id", account_id).execute()
    if not account_result.data:
        raise HTTPException(status_code=404, detail="Account not found")

    account = account_result.data[0]
    logger.info(
        f"Admin {admin['user_id']} initiating complete deletion of account {account_id} ({account.get('email')})"
    )

    # 1. First, get all instances for this account
    instances_result = sb.table("instances").select("*").eq("account_id", account_id).execute()
    instances = instances_result.data or []

    # 2. Deprovision all instances
    for instance in instances:
        instance_id = instance.get("instance_id")
        if instance.get("status") not in ["deprovisioned", "terminated"]:
            logger.info(f"Deprovisioning instance {instance_id} for account {account_id}")
            try:
                # Call the uninstall endpoint via provisioner
                await uninstall_instance(
                    instance_id=instance_id,
                    api_key=PROVISIONER_API_KEY,
                )
            except Exception as e:
                logger.error(f"Failed to deprovision instance {instance_id}: {e}")
                # Continue with other instances even if one fails

    # 3. Cancel any active Stripe subscriptions
    if account.get("stripe_customer_id"):
        try:
            import stripe
            from backend.config import STRIPE_API_KEY

            stripe.api_key = STRIPE_API_KEY

            # List and cancel all subscriptions for this customer
            subscriptions = stripe.Subscription.list(customer=account["stripe_customer_id"], status="active")
            for subscription in subscriptions.data:
                logger.info(f"Canceling Stripe subscription {subscription.id}")
                stripe.Subscription.cancel(subscription.id)

            # Delete the Stripe customer (optional - you may want to keep for records)
            # stripe.Customer.delete(account["stripe_customer_id"])

        except Exception as e:
            logger.error(f"Failed to cancel Stripe subscriptions: {e}")
            # Continue with deletion even if Stripe fails

    # 4. Delete the account (cascade deletion will handle related records)
    try:
        sb.table("accounts").delete().eq("id", account_id).execute()

        # Log the complete deletion
        audit_log_entry(
            account_id=admin["user_id"],
            action="delete_complete",
            resource_type="accounts",
            resource_id=account_id,
            details={
                "deleted_email": account.get("email"),
                "instances_deprovisioned": len(instances),
                "had_stripe_customer": bool(account.get("stripe_customer_id")),
            },
        )

        logger.info(f"Successfully deleted account {account_id} and all associated resources")

    except Exception as e:
        logger.exception(f"Error deleting account {account_id}")
        raise HTTPException(status_code=500, detail=f"Failed to delete account: {str(e)}") from None

    return {"data": {"id": account_id}}


@router.delete("/admin/{resource}/{resource_id}", response_model=AdminDeleteResponse)
@limiter.limit("10/minute")
async def admin_delete(
    request: Request,  # noqa: ARG001
    resource: str,
    resource_id: str,
    admin: Annotated[dict, Depends(verify_admin)],  # noqa: FAST002, B008
) -> dict[str, Any]:
    """Delete record for React Admin (generic endpoint - use with caution for accounts)."""
    if resource not in ALLOWED_RESOURCES:
        raise HTTPException(status_code=400, detail="Invalid resource")

    # For accounts, redirect to the complete deletion endpoint
    if resource == "accounts":
        logger.warning(
            f"Generic delete called for account {resource_id}, use /admin/accounts/{resource_id}/complete instead"
        )
        raise HTTPException(
            status_code=400, detail="Use DELETE /admin/accounts/{account_id}/complete for proper account deletion"
        )

    sb = ensure_supabase()

    try:
        sb.table(resource).delete().eq("id", resource_id).execute()

        # Log admin deletion
        audit_log_entry(
            account_id=admin["user_id"],
            action="delete",
            resource_type=resource,
            resource_id=resource_id,
        )
    except Exception:
        logger.exception("Error deleting resource")
        raise HTTPException(status_code=400, detail="Invalid request") from None
    else:
        return {"data": {"id": resource_id}}
