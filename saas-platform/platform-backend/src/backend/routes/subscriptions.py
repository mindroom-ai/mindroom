"""Subscription management routes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from backend.config import logger, stripe
from backend.deps import ensure_supabase, verify_user
from backend.models import SubscriptionOut
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

router = APIRouter()


class SubscriptionUpdateRequest(BaseModel):
    """Request model for updating subscription."""

    tier: str | None = None
    billing_cycle: str | None = None
    quantity: int | None = None


class CancelSubscriptionRequest(BaseModel):
    """Request model for canceling subscription."""

    cancel_at_period_end: bool = True


@router.get("/my/subscription", response_model=SubscriptionOut)
async def get_user_subscription(user: Annotated[dict, Depends(verify_user)]) -> dict[str, Any]:
    """Get current user's subscription."""
    sb = ensure_supabase()

    account_id = user["account_id"]
    result = sb.table("subscriptions").select("*").eq("account_id", account_id).limit(1).execute()
    if not result.data:
        # Return a default free subscription if none exists
        return {
            "id": "00000000-0000-0000-0000-000000000000",
            "account_id": account_id,
            "tier": "free",
            "status": "active",
            "max_agents": 1,
            "max_messages_per_day": 100,
            "created_at": datetime.now(UTC).isoformat(),
            "updated_at": datetime.now(UTC).isoformat(),
        }
    return result.data[0]


@router.post("/my/subscription/upgrade")
async def upgrade_subscription(
    request: SubscriptionUpdateRequest,
    user: Annotated[dict, Depends(verify_user)],
) -> dict[str, Any]:
    """Upgrade or change subscription plan."""
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Stripe not configured")

    sb = ensure_supabase()
    account_id = user["account_id"]

    # Get current subscription
    sub_result = sb.table("subscriptions").select("*").eq("account_id", account_id).single().execute()

    if not sub_result.data or not sub_result.data.get("stripe_subscription_id"):
        raise HTTPException(
            status_code=400,
            detail="No active subscription found. Please use checkout to create a new subscription.",
        )

    stripe_sub_id = sub_result.data["stripe_subscription_id"]

    try:
        # Get the Stripe subscription
        stripe_sub = stripe.Subscription.retrieve(stripe_sub_id)

        # Update the subscription based on request
        update_params = {}

        if request.tier or request.billing_cycle:
            # Change plan - need to update the subscription items
            from backend.pricing import get_stripe_price_id  # noqa: PLC0415

            new_tier = request.tier or sub_result.data.get("tier", "starter")
            new_cycle = request.billing_cycle or "monthly"  # Default or detect from current

            price_id = get_stripe_price_id(new_tier, new_cycle)
            if not price_id:
                raise HTTPException(status_code=400, detail=f"Invalid plan: {new_tier} ({new_cycle})")

            # Update subscription items
            update_params["items"] = [
                {
                    "id": stripe_sub["items"]["data"][0]["id"],
                    "price": price_id,
                    "quantity": request.quantity or stripe_sub["items"]["data"][0]["quantity"],
                },
            ]
        elif request.quantity:
            # Just update quantity for per-user pricing
            update_params["items"] = [
                {
                    "id": stripe_sub["items"]["data"][0]["id"],
                    "quantity": request.quantity,
                },
            ]

        if update_params:
            updated_sub = stripe.Subscription.modify(stripe_sub_id, **update_params)

            # Update local database will happen via webhook
            return {
                "success": True,
                "message": "Subscription updated successfully",
                "subscription_id": updated_sub.id,
            }
        return {"success": False, "message": "No changes requested"}  # noqa: TRY300

    except stripe.error.StripeError as e:
        logger.exception("Stripe error updating subscription")
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/my/subscription/cancel")
async def cancel_subscription(
    request: CancelSubscriptionRequest,
    user: Annotated[dict, Depends(verify_user)],
) -> dict[str, Any]:
    """Cancel subscription."""
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Stripe not configured")

    sb = ensure_supabase()
    account_id = user["account_id"]

    # Get current subscription
    sub_result = sb.table("subscriptions").select("*").eq("account_id", account_id).single().execute()

    if not sub_result.data or not sub_result.data.get("stripe_subscription_id"):
        raise HTTPException(status_code=400, detail="No active subscription found")

    stripe_sub_id = sub_result.data["stripe_subscription_id"]

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

    except stripe.error.StripeError as e:
        logger.exception("Stripe error cancelling subscription")
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/my/subscription/reactivate")
async def reactivate_subscription(user: Annotated[dict, Depends(verify_user)]) -> dict[str, Any]:
    """Reactivate a cancelled subscription (if still in billing period)."""
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Stripe not configured")

    sb = ensure_supabase()
    account_id = user["account_id"]

    # Get current subscription
    sub_result = sb.table("subscriptions").select("*").eq("account_id", account_id).single().execute()

    if not sub_result.data or not sub_result.data.get("stripe_subscription_id"):
        raise HTTPException(status_code=400, detail="No subscription found")

    stripe_sub_id = sub_result.data["stripe_subscription_id"]

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

    except stripe.error.StripeError as e:
        logger.exception("Stripe error reactivating subscription")
        raise HTTPException(status_code=400, detail=str(e)) from e
