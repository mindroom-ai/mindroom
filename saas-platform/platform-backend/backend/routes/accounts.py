from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from backend.deps import ensure_supabase, verify_user
from backend.models import AdminStatusOut
from fastapi import APIRouter, Depends, HTTPException

router = APIRouter()


@router.get("/my/account")
async def get_current_account(user=Depends(verify_user)) -> dict[str, Any]:  # noqa: B008
    """Get current user's account with subscription and instances."""
    sb = ensure_supabase()

    try:
        account_id = user["account_id"]

        account_result = (
            sb.table("accounts")
            .select(
                "*, subscriptions(*, instances(*))",
            )
            .eq("id", account_id)
            .single()
            .execute()
        )

        if not account_result.data:
            raise HTTPException(status_code=404, detail="Account not found")

        return account_result.data
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to fetch account") from e


@router.get("/my/account/admin-status", response_model=AdminStatusOut)
async def check_admin_status(user=Depends(verify_user)) -> dict[str, bool]:  # noqa: B008
    """Check if current user is an admin."""
    sb = ensure_supabase()

    try:
        account_id = user["account_id"]
        account_result = sb.table("accounts").select("is_admin").eq("id", account_id).single().execute()
        if not account_result.data:
            return AdminStatusOut(is_admin=False).model_dump()
        return AdminStatusOut(is_admin=bool(account_result.data.get("is_admin", False))).model_dump()
    except Exception:
        return AdminStatusOut(is_admin=False).model_dump()


@router.post("/my/account/setup")
async def setup_account(user=Depends(verify_user)) -> dict[str, Any]:  # noqa: B008
    """Setup free tier account for new user."""
    sb = ensure_supabase()

    try:
        account_id = user["account_id"]

        sub_result = sb.table("subscriptions").select("id").eq("account_id", account_id).execute()
        if sub_result.data:
            return {"message": "Account already setup", "account_id": account_id}

        subscription_data = {
            "account_id": account_id,
            "tier": "free",
            "status": "active",
            "max_agents": 1,
            "max_messages_per_day": 100,
            "created_at": datetime.now(UTC).isoformat(),
        }

        sub_result = sb.table("subscriptions").insert(subscription_data).execute()

        return {
            "message": "Free tier account created",
            "account_id": account_id,
            "subscription": sub_result.data[0] if sub_result.data else None,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to setup account") from e
