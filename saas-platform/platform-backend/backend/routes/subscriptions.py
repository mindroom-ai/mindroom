from __future__ import annotations

from typing import Any

from backend.deps import ensure_supabase, verify_user
from fastapi import APIRouter, Depends, HTTPException

router = APIRouter()


@router.get("/api/v1/subscription")
async def get_user_subscription(user=Depends(verify_user)) -> dict[str, Any]:  # noqa: B008
    """Get current user's subscription."""
    sb = ensure_supabase()

    try:
        account_id = user["account_id"]
        result = sb.table("subscriptions").select("*").eq("account_id", account_id).single().execute()
        if not result.data:
            raise HTTPException(status_code=404, detail="Subscription not found")
        return result.data
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to fetch subscription") from e
