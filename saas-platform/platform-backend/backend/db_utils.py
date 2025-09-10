"""Common database utilities and query helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from backend.deps import ensure_supabase
from fastapi import HTTPException


def get_instance_by_id(instance_id: int | str) -> dict[str, Any] | None:
    """Get instance by ID."""
    sb = ensure_supabase()
    result = sb.table("instances").select("*").eq("instance_id", str(instance_id)).execute()
    return result.data[0] if result.data else None


def verify_instance_ownership(instance_id: int, account_id: str) -> bool:
    """Verify that an account owns an instance."""
    sb = ensure_supabase()
    result = (
        sb.table("instances")
        .select("id")
        .eq("instance_id", instance_id)
        .eq("account_id", account_id)
        .limit(1)
        .execute()
    )
    return bool(result.data)


def update_instance_status(instance_id: int | str, status: str) -> bool:
    """Update instance status in database."""
    try:
        sb = ensure_supabase()
        sb.table("instances").update(
            {
                "status": status,
                "updated_at": datetime.now(UTC).isoformat(),
            },
        ).eq("instance_id", str(instance_id)).execute()
    except Exception:
        return False
    else:
        return True


def get_user_subscription(account_id: str) -> dict[str, Any] | None:
    """Get active subscription for an account."""
    sb = ensure_supabase()
    result = (
        sb.table("subscriptions").select("*").eq("account_id", account_id).eq("status", "active").limit(1).execute()
    )
    return result.data[0] if result.data else None


def handle_db_error(operation: str) -> None:
    """Raise a standardized HTTP 500 error for database operations."""
    raise HTTPException(status_code=500, detail=f"Failed to {operation}")
