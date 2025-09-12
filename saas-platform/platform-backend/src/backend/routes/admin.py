"""Admin-only routes for platform management."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from backend.config import PROVISIONER_API_KEY, logger
from backend.deps import ensure_supabase, limiter, verify_admin
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
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query

router = APIRouter()
ALLOWED_RESOURCES = {"accounts", "subscriptions", "instances", "audit_logs", "usage_metrics"}


def audit_log_entry(
    account_id: str,
    action: str,
    resource_type: str,
    resource_id: str | None = None,
    details: dict | None = None,
) -> None:
    """Log an admin action to the audit_logs table (best effort)."""
    try:
        sb = ensure_supabase()
        sb.table("audit_logs").insert(
            {
                "account_id": account_id,
                "action": action,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "details": details,
                "created_at": datetime.now(UTC).isoformat(),
            },
        ).execute()
    except Exception:
        logger.warning(
            f"Failed to log audit: {action} on {resource_type}{f'/{resource_id}' if resource_id else ''}",
        )


@router.get("/admin/stats", response_model=AdminStatsOut)
@limiter.limit("30/minute")
async def get_admin_stats(admin: Annotated[dict, Depends(verify_admin)]) -> dict[str, Any]:
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
    provisioner_func: Callable,
    instance_id: int,
    admin: Annotated[dict, Depends(verify_admin)],  # noqa: ARG001
) -> dict[str, Any]:
    """Proxy request to provisioner with API key."""
    return await provisioner_func(instance_id, f"Bearer {PROVISIONER_API_KEY}")


@router.post("/admin/instances/{instance_id}/start", response_model=ActionResult)
@limiter.limit("10/minute")
async def admin_start_instance(instance_id: int, admin: Annotated[dict, Depends(verify_admin)]) -> dict[str, Any]:
    """Start an instance (admin proxy)."""
    result = await _proxy_to_provisioner(start_instance_provisioner, instance_id, admin)
    audit_log_entry(
        account_id=admin["user_id"],
        action="start",
        resource_type="instance",
        resource_id=str(instance_id),
    )
    return result


@router.post("/admin/instances/{instance_id}/stop", response_model=ActionResult)
async def admin_stop_instance(instance_id: int, admin: Annotated[dict, Depends(verify_admin)]) -> dict[str, Any]:
    """Stop an instance (admin proxy)."""
    result = await _proxy_to_provisioner(stop_instance_provisioner, instance_id, admin)
    audit_log_entry(
        account_id=admin["user_id"],
        action="stop",
        resource_type="instance",
        resource_id=str(instance_id),
    )
    return result


@router.post("/admin/instances/{instance_id}/restart", response_model=ActionResult)
@limiter.limit("10/minute")
async def admin_restart_instance(instance_id: int, admin: Annotated[dict, Depends(verify_admin)]) -> dict[str, Any]:
    """Restart an instance (admin proxy)."""
    result = await _proxy_to_provisioner(restart_instance_provisioner, instance_id, admin)
    audit_log_entry(
        account_id=admin["user_id"],
        action="restart",
        resource_type="instance",
        resource_id=str(instance_id),
    )
    return result


@router.delete("/admin/instances/{instance_id}/uninstall", response_model=ActionResult)
@limiter.limit("2/minute")
async def admin_uninstall_instance(instance_id: int, admin: Annotated[dict, Depends(verify_admin)]) -> dict[str, Any]:
    """Uninstall an instance (admin proxy)."""
    result = await _proxy_to_provisioner(uninstall_instance, instance_id, admin)
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
    instance_id: int,
    background_tasks: BackgroundTasks,
    admin: Annotated[dict, Depends(verify_admin)],
) -> dict[str, Any]:
    """Provision a deprovisioned instance."""
    sb = ensure_supabase()

    # Get instance details
    result = sb.table("instances").select("*").eq("instance_id", str(instance_id)).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Instance not found")

    instance = result.data[0]
    if instance.get("status") not in ["deprovisioned", "error"]:
        raise HTTPException(status_code=400, detail="Instance must be deprovisioned or in error state to provision")

    # Call provisioner with existing instance data
    data = {
        "subscription_id": instance.get("subscription_id"),
        "account_id": instance.get("account_id"),
        "tier": instance.get("tier", "free"),
        "instance_id": instance_id,  # Re-use existing instance ID
    }

    result = await provision_instance(data, f"Bearer {PROVISIONER_API_KEY}", background_tasks)
    audit_log_entry(
        account_id=admin["user_id"],
        action="provision",
        resource_type="instance",
        resource_id=str(instance_id),
        details={"account_id": instance.get("account_id"), "tier": instance.get("tier")},
    )
    return result


@router.post("/admin/sync-instances", response_model=SyncResult)
@limiter.limit("5/minute")
async def admin_sync_instances(admin: Annotated[dict, Depends(verify_admin)]) -> dict[str, Any]:
    """Sync instance states between database and Kubernetes (admin proxy)."""
    result = await sync_instances(f"Bearer {PROVISIONER_API_KEY}")
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
    admin: Annotated[dict, Depends(verify_admin)],  # noqa: ARG001
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
            **account,
            "subscription": subscription_result.data[0] if subscription_result.data else None,
            "instances": instances_result.data if instances_result.data else [],
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error fetching account details")
        raise HTTPException(status_code=500, detail="Failed to fetch account details") from e


@router.put("/admin/accounts/{account_id}/status", response_model=UpdateAccountStatusResponse)
async def update_account_status(
    account_id: str,
    status: str,
    admin: Annotated[dict, Depends(verify_admin)],
) -> dict[str, Any]:
    """Update account status (active, suspended, etc)."""
    sb = ensure_supabase()

    valid_statuses = ["active", "suspended", "deleted", "pending_verification"]
    if status not in valid_statuses:
        raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of: {valid_statuses}")

    try:
        result = (
            sb.table("accounts")
            .update(
                {
                    "status": status,
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
            details={"status": status},
        )

        return {"status": "success", "account_id": account_id, "new_status": status}  # noqa: TRY300
    except Exception as e:
        logger.exception("Error updating account status")
        raise HTTPException(status_code=500, detail="Failed to update account status") from e


@router.post("/admin/auth/logout", response_model=AdminLogoutResponse)
async def admin_logout() -> dict[str, bool]:
    """Admin logout placeholder."""
    return {"success": True}


# === React Admin Data Provider ===
@router.get("/admin/{resource}", response_model=AdminListResponse)
@limiter.limit("60/minute")
async def admin_get_list(  # noqa: C901
    resource: str,
    admin: Annotated[dict, Depends(verify_admin)],
    _sort: Annotated[str | None, Query()] = None,
    _order: Annotated[str | None, Query()] = None,
    _start: Annotated[int, Query()] = 0,
    _end: Annotated[int, Query()] = 10,
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
    resource: str,
    resource_id: str,
    admin: Annotated[dict, Depends(verify_admin)],
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
    resource: str,
    data: dict,
    admin: Annotated[dict, Depends(verify_admin)],
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
    resource: str,
    resource_id: str,
    data: dict,
    admin: Annotated[dict, Depends(verify_admin)],
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


@router.delete("/admin/{resource}/{resource_id}", response_model=AdminDeleteResponse)
@limiter.limit("10/minute")
async def admin_delete(
    resource: str,
    resource_id: str,
    admin: Annotated[dict, Depends(verify_admin)],
) -> dict[str, Any]:
    """Delete record for React Admin."""
    if resource not in ALLOWED_RESOURCES:
        raise HTTPException(status_code=400, detail="Invalid resource")
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


@router.get("/admin/metrics/dashboard", response_model=AdminDashboardMetricsResponse)
@limiter.limit("30/minute")
async def get_dashboard_metrics(
    admin: Annotated[dict, Depends(verify_admin)],
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
        running_instances = (
            sb.table("instances").select("*", count="exact", head=True).eq("status", "running").execute()
        )

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

        daily_messages = []
        if messages.data:
            by_date = defaultdict(int)
            for m in messages.data:
                date = m["metric_date"][:10]
                by_date[date] += m.get("messages_sent", 0)
            daily_messages = [{"date": date, "messages_sent": count} for date, count in sorted(by_date.items())]

        all_instances = sb.table("instances").select("status").execute()
        status_counts: dict[str, int] = {}
        if all_instances.data:
            for inst in all_instances.data:
                status = inst.get("status", "unknown")
                status_counts[status] = status_counts.get(status, 0) + 1
        instance_statuses = [{"status": status, "count": count} for status, count in status_counts.items()]

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
            "totalAccounts": 0,
            "activeSubscriptions": 0,
            "runningInstances": 0,
            "mrr": 0,
            "dailyMessages": [],
            "instanceStatuses": [],
            "recentActivity": [],
        }
    else:
        return {
            "totalAccounts": accounts.count or 0,
            "activeSubscriptions": active_subs.count or 0,
            "runningInstances": running_instances.count or 0,
            "mrr": mrr,
            "dailyMessages": daily_messages,
            "instanceStatuses": instance_statuses,
            "recentActivity": recent_activity,
        }
