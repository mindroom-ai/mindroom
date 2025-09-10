"""Admin-only routes for platform management."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from backend.config import PROVISIONER_API_KEY, logger
from backend.deps import ensure_supabase, verify_admin
from backend.models import ActionResult, AdminStatsOut, UpdateAccountStatusResponse
from backend.routes.provisioner import (
    provision_instance,
    restart_instance_provisioner,
    start_instance_provisioner,
    stop_instance_provisioner,
    uninstall_instance,
)
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query

router = APIRouter()


@router.get("/admin/stats", response_model=AdminStatsOut)
async def get_admin_stats(admin: Annotated[dict, Depends(verify_admin)]) -> dict[str, Any]:  # noqa: ARG001
    """Get platform statistics for admin dashboard."""
    sb = ensure_supabase()

    try:
        accounts = sb.table("accounts").select("*", count="exact").execute()
        subscriptions = sb.table("subscriptions").select("*", count="exact").eq("status", "active").execute()
        instances = sb.table("instances").select("*", count="exact").eq("status", "active").execute()

        return {
            "accounts": len(accounts.data) if accounts.data else 0,
            "active_subscriptions": len(subscriptions.data) if subscriptions.data else 0,
            "running_instances": len(instances.data) if instances.data else 0,
        }
    except Exception as e:
        logger.exception("Error fetching admin stats")
        raise HTTPException(status_code=500, detail="Failed to fetch statistics") from e


@router.post("/admin/instances/{instance_id}/start", response_model=ActionResult)
async def admin_start_instance(instance_id: int, admin: Annotated[dict, Depends(verify_admin)]) -> dict[str, Any]:  # noqa: ARG001
    """Proxy start to provisioner (no key exposed to browser)."""
    return await start_instance_provisioner(instance_id, f"Bearer {PROVISIONER_API_KEY}")


@router.post("/admin/instances/{instance_id}/stop", response_model=ActionResult)
async def admin_stop_instance(instance_id: int, admin: Annotated[dict, Depends(verify_admin)]) -> dict[str, Any]:  # noqa: ARG001
    """Proxy stop to provisioner (no key exposed to browser)."""
    return await stop_instance_provisioner(instance_id, f"Bearer {PROVISIONER_API_KEY}")


@router.post("/admin/instances/{instance_id}/restart", response_model=ActionResult)
async def admin_restart_instance(instance_id: int, admin: Annotated[dict, Depends(verify_admin)]) -> dict[str, Any]:  # noqa: ARG001
    """Proxy restart to provisioner (no key exposed to browser)."""
    return await restart_instance_provisioner(instance_id, f"Bearer {PROVISIONER_API_KEY}")


@router.delete("/admin/instances/{instance_id}/uninstall", response_model=ActionResult)
async def admin_uninstall_instance(instance_id: int, admin: Annotated[dict, Depends(verify_admin)]) -> dict[str, Any]:  # noqa: ARG001
    """Proxy uninstall to provisioner (no key exposed to browser)."""
    return await uninstall_instance(instance_id, f"Bearer {PROVISIONER_API_KEY}")


@router.post("/admin/instances/{instance_id}/provision")
async def admin_provision_instance(
    instance_id: int,
    background_tasks: BackgroundTasks,
    admin: Annotated[dict, Depends(verify_admin)],  # noqa: ARG001
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

    return await provision_instance(data, f"Bearer {PROVISIONER_API_KEY}", background_tasks)


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

        sb.table("audit_logs").insert(
            {
                "account_id": admin["user_id"],
                "action": "update",
                "resource_type": "account",
                "resource_id": account_id,
                "details": {"status": status},
                "created_at": datetime.now(UTC).isoformat(),
            },
        ).execute()

        return {"status": "success", "account_id": account_id, "new_status": status}  # noqa: TRY300
    except Exception as e:
        logger.exception("Error updating account status")
        raise HTTPException(status_code=500, detail="Failed to update account status") from e


@router.post("/admin/auth/logout")
async def admin_logout() -> dict[str, bool]:
    """Admin logout placeholder."""
    return {"success": True}


# === React Admin Data Provider ===
@router.get("/admin/{resource}")
async def admin_get_list(
    resource: str,
    _sort: Annotated[str | None, Query()] = None,
    _order: Annotated[str | None, Query()] = None,
    _start: Annotated[int, Query()] = 0,
    _end: Annotated[int, Query()] = 10,
    q: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """Generic list endpoint for React Admin."""
    sb = ensure_supabase()

    try:
        # Special-case: instances should include account info
        if resource == "instances":
            query = sb.table("instances").select("*, accounts(email, full_name)", count="exact")
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


@router.get("/admin/{resource}/{resource_id}")
async def admin_get_one(resource: str, resource_id: str) -> dict[str, Any]:
    """Get single record for React Admin."""
    sb = ensure_supabase()

    try:
        result = sb.table(resource).select("*").eq("id", resource_id).single().execute()
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    else:
        return {"data": result.data}


@router.post("/admin/{resource}")
async def admin_create(resource: str, data: dict) -> dict[str, Any]:
    """Create record for React Admin."""
    sb = ensure_supabase()

    try:
        result = sb.table(resource).insert(data).execute()
        return {"data": result.data[0] if result.data else None}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.put("/admin/{resource}/{resource_id}")
async def admin_update(resource: str, resource_id: str, data: dict) -> dict[str, Any]:
    """Update record for React Admin."""
    sb = ensure_supabase()

    try:
        data.pop("id", None)
        result = sb.table(resource).update(data).eq("id", resource_id).execute()
        return {"data": result.data[0] if result.data else None}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.delete("/admin/{resource}/{resource_id}")
async def admin_delete(resource: str, resource_id: str) -> dict[str, Any]:
    """Delete record for React Admin."""
    sb = ensure_supabase()

    try:
        sb.table(resource).delete().eq("id", resource_id).execute()
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    else:
        return {"data": {"id": resource_id}}


@router.get("/admin/metrics/dashboard")
async def get_dashboard_metrics() -> dict[str, Any]:
    """Get dashboard metrics for admin panel."""
    try:
        sb = ensure_supabase()
    except HTTPException:
        return {
            "totalAccounts": 0,
            "activeSubscriptions": 0,
            "runningInstances": 0,
            "mrr": 0,
            "dailyMessages": [],
            "instanceStatuses": [],
            "recentActivity": [],
        }

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
