"""Stripe payment and subscription routes."""

from __future__ import annotations

import os
from typing import Annotated, Any

from backend.config import logger, stripe
from backend.deps import ensure_supabase, verify_user, verify_user_optional
from backend.models import UrlResponse
from backend.pricing import get_stripe_price_id, get_trial_days, is_trial_enabled_for_plan
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

router = APIRouter()


class CheckoutRequest(BaseModel):
    """Request model for creating Stripe checkout sessions."""

    tier: str
    billing_cycle: str = "monthly"  # monthly or yearly
    quantity: int = 1  # For per-user pricing (professional plan)


@router.post("/stripe/checkout", response_model=UrlResponse)
async def create_checkout_session(
    request: CheckoutRequest,
    user: Annotated[dict | None, Depends(verify_user_optional)],
) -> dict[str, Any]:
    """Create Stripe checkout session for subscription."""
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Stripe not configured")

    # Get price ID from config
    price_id = get_stripe_price_id(request.tier, request.billing_cycle)
    if not price_id:
        raise HTTPException(status_code=400, detail=f"No price found for {request.tier} ({request.billing_cycle})")

    sb = ensure_supabase()

    customer_id: str | None = None

    if user:
        result = sb.table("accounts").select("stripe_customer_id").eq("id", user["account_id"]).single().execute()
        if result.data and result.data.get("stripe_customer_id"):
            customer_id = result.data["stripe_customer_id"]
        else:
            customer = stripe.Customer.create(
                email=user["email"],
                metadata={"supabase_user_id": user["account_id"]},
            )
            customer_id = customer.id
            sb.table("accounts").update({"stripe_customer_id": customer_id}).eq(
                "id",
                user["account_id"],
            ).execute()

    # Check if customer already has an active subscription
    if customer_id:
        # Check for existing active subscriptions
        subscriptions = stripe.Subscription.list(customer=customer_id, status="all", limit=10)
        for sub in subscriptions.data:
            if sub.status in ["active", "trialing"]:
                # Customer already has a subscription - they should use the portal to manage it
                logger.warning(
                    "Customer %s already has an active subscription %s, redirecting to portal",
                    customer_id,
                    sub.id,
                )
                # Create a portal session instead
                portal_session = stripe.billing_portal.Session.create(
                    customer=customer_id,
                    return_url=f"{os.getenv('APP_URL', 'https://app.staging.mindroom.chat')}/dashboard/billing",
                )
                return {"url": portal_session.url}

    # Use quantity for professional plan (per-user pricing)
    quantity = request.quantity if request.tier == "professional" else 1

    checkout_params = {
        "line_items": [{"price": price_id, "quantity": quantity}],
        "mode": "subscription",
        "success_url": f"{os.getenv('APP_URL', 'https://app.staging.mindroom.chat')}/dashboard?success=true&session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{os.getenv('APP_URL', 'https://app.staging.mindroom.chat')}/dashboard/billing/upgrade?cancelled=true",
        "allow_promotion_codes": True,
        "billing_address_collection": "required",
        "subscription_data": {
            "metadata": {
                "tier": request.tier,
                "billing_cycle": request.billing_cycle,
                "quantity": str(quantity),
                "supabase_user_id": user["account_id"] if user else "",
            },
        },
        "metadata": {
            "tier": request.tier,
            "billing_cycle": request.billing_cycle,
            "quantity": str(quantity),
            "supabase_user_id": user["account_id"] if user else "",
        },
    }

    # Add trial period if enabled for this plan
    if is_trial_enabled_for_plan(request.tier):
        checkout_params["subscription_data"]["trial_period_days"] = get_trial_days()

    if customer_id:
        checkout_params["customer"] = customer_id

    session = stripe.checkout.Session.create(**checkout_params)
    return {"url": session.url}


@router.post("/stripe/portal", response_model=UrlResponse)
async def create_portal_session(user: Annotated[dict, Depends(verify_user)]) -> dict[str, Any]:
    """Create Stripe customer portal session for subscription management."""
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Stripe not configured")
    sb = ensure_supabase()

    # Stripe customer ID is stored on the accounts table
    result = sb.table("accounts").select("stripe_customer_id").eq("id", user["account_id"]).single().execute()
    if not result.data or not result.data.get("stripe_customer_id"):
        raise HTTPException(status_code=404, detail="No Stripe customer found")

    session = stripe.billing_portal.Session.create(
        customer=result.data["stripe_customer_id"],
        return_url=f"{os.getenv('APP_URL', 'https://app.staging.mindroom.chat')}/dashboard/billing?return=true",
    )

    return {"url": session.url}
