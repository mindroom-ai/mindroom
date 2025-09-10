"""Subscription management routes."""

from __future__ import annotations

from typing import Annotated, Any

from backend.deps import ensure_supabase, verify_user
from backend.models import SubscriptionOut
from fastapi import APIRouter, Depends, HTTPException

router = APIRouter()


@router.get("/my/subscription", response_model=SubscriptionOut)
async def get_user_subscription(user: Annotated[dict, Depends(verify_user)]) -> dict[str, Any]:
    """Get current user's subscription."""
    sb = ensure_supabase()

    account_id = user["account_id"]
    result = sb.table("subscriptions").select("*").eq("account_id", account_id).limit(1).execute()
    if not result.data:
        msg = "Subscription not found"
        raise HTTPException(status_code=404, detail=msg)
    return result.data[0]
