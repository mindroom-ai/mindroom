"""Subscription management routes."""

from datetime import UTC, datetime
from typing import Annotated, Any

from backend.config import logger, stripe
from backend.deps import ensure_supabase, limiter, verify_user
from backend.models import (
    SubscriptionCancelResponse,
    SubscriptionOut,
    SubscriptionReactivateResponse,
)
from backend.pricing import get_plan_limits_from_metadata
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

router = APIRouter()


class CancelSubscriptionRequest(BaseModel):
    """Request model for canceling subscription."""

    cancel_at_period_end: bool = True


@router.get("/my/subscription", response_model=SubscriptionOut)
@limiter.limit("30/minute")  # Reading subscription info
async def get_user_subscription(
    request: Request,
    user: Annotated[dict, Depends(verify_user)],
) -> dict[str, Any]:
    """Get current user's subscription."""
    sb = ensure_supabase()

    account_id = user["account_id"]
    result = sb.table("subscriptions").select("*").eq("account_id", account_id).limit(1).execute()
    if not result.data:
        # Auto-create a real free subscription for new users
        logger.info(f"No subscription found for account {account_id}, creating free tier")
        limits = get_plan_limits_from_metadata("free")
        subscription_data = {
            "account_id": account_id,
            "tier": "free",
            "status": "active",
            "max_agents": limits["max_agents"],
            "max_messages_per_day": limits["max_messages_per_day"],
            "created_at": datetime.now(UTC).isoformat(),
            "updated_at": datetime.now(UTC).isoformat(),
        }
        create_result = sb.table("subscriptions").insert(subscription_data).execute()
        if create_result.data:
            subscription = create_result.data[0]
            subscription["max_storage_gb"] = limits["max_storage_gb"]
            return subscription

        logger.error(f"Failed to create subscription for account {account_id}")
        raise HTTPException(status_code=500, detail="Failed to create subscription")

    # Add max_storage_gb from pricing config if not in database
    subscription = result.data[0]
    if "max_storage_gb" not in subscription or subscription["max_storage_gb"] is None:
        tier = subscription.get("tier", "free")
        limits = get_plan_limits_from_metadata(tier)
        subscription["max_storage_gb"] = limits["max_storage_gb"]

    return subscription


@router.post("/my/subscription/cancel", response_model=SubscriptionCancelResponse)
@limiter.limit("5/minute")  # Sensitive operation
async def cancel_subscription(
    req: Request,
    request: CancelSubscriptionRequest,
    user: Annotated[dict, Depends(verify_user)],
) -> dict[str, Any]:
    """Cancel subscription."""
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Stripe not configured")

    sb = ensure_supabase()
    account_id = user["account_id"]

    # Get current subscription
    sub_result = sb.table("subscriptions").select("*").eq("account_id", account_id).limit(1).execute()

    if not sub_result.data:
        raise HTTPException(status_code=404, detail="No subscription found")

    subscription = sub_result.data[0]
    if subscription.get("status") == "cancelled":
        raise HTTPException(status_code=400, detail="Subscription is already cancelled")

    if not subscription.get("stripe_subscription_id"):
        raise HTTPException(status_code=400, detail="No active subscription found")

    stripe_sub_id = subscription["stripe_subscription_id"]

    try:
        if request.cancel_at_period_end:
            # Cancel at end of billing period
            cancelled_sub = stripe.Subscription.modify(
                stripe_sub_id,
                cancel_at_period_end=True,
            )
        else:
            # Cancel immediately
            cancelled_sub = stripe.Subscription.delete(stripe_sub_id)

        # Update local database will happen via webhook
        return {  # noqa: TRY300
            "success": True,
            "message": "Subscription cancelled successfully",
            "cancel_at_period_end": request.cancel_at_period_end,
            "subscription_id": cancelled_sub.id,
        }

    except Exception:
        logger.exception("Error cancelling subscription")
        raise HTTPException(status_code=500, detail="Failed to cancel subscription")


@router.post("/my/subscription/reactivate", response_model=SubscriptionReactivateResponse)
@limiter.limit("5/minute")  # Sensitive operation
async def reactivate_subscription(
    request: Request,
    user: Annotated[dict, Depends(verify_user)],
) -> dict[str, Any]:
    """Reactivate a cancelled subscription (if still in billing period)."""
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Stripe not configured")

    sb = ensure_supabase()
    account_id = user["account_id"]

    # Get current subscription
    sub_result = sb.table("subscriptions").select("*").eq("account_id", account_id).limit(1).execute()

    if not sub_result.data:
        raise HTTPException(status_code=404, detail="No subscription found")

    subscription = sub_result.data[0]
    if subscription.get("status") != "cancelled":
        raise HTTPException(status_code=400, detail="Subscription is not cancelled")

    if not subscription.get("stripe_subscription_id"):
        raise HTTPException(status_code=400, detail="No Stripe subscription found")

    stripe_sub_id = subscription["stripe_subscription_id"]

    try:
        # Reactivate by removing the cancel_at_period_end flag
        reactivated_sub = stripe.Subscription.modify(
            stripe_sub_id,
            cancel_at_period_end=False,
        )

        # Update local database
        sb.table("subscriptions").update(
            {
                "status": "active",
                "cancelled_at": None,
                "updated_at": datetime.now(UTC).isoformat(),
            },
        ).eq("account_id", account_id).execute()

        return {  # noqa: TRY300
            "success": True,
            "message": "Subscription reactivated successfully",
            "subscription_id": reactivated_sub.id,
        }

    except Exception:
        logger.exception("Error reactivating subscription")
        raise HTTPException(status_code=500, detail="Failed to reactivate subscription")
