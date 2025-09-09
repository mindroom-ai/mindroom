from __future__ import annotations

import os
from typing import Annotated, Any

from backend.config import stripe
from backend.deps import ensure_supabase, verify_user, verify_user_optional
from backend.models import UrlResponse
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

router = APIRouter()


class CheckoutRequest(BaseModel):
    price_id: str
    tier: str


@router.post("/stripe/checkout", response_model=UrlResponse)
async def create_checkout_session(
    request: CheckoutRequest,
    user: Annotated[dict | None, Depends(verify_user_optional)],
) -> dict[str, Any]:
    """Create Stripe checkout session for subscription."""
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Stripe not configured")
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

    checkout_params = {
        "line_items": [{"price": request.price_id, "quantity": 1}],
        "mode": "subscription",
        "success_url": f"{os.getenv('APP_URL', 'https://app.staging.mindroom.chat')}/dashboard?success=true&session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{os.getenv('APP_URL', 'https://app.staging.mindroom.chat')}/pricing?cancelled=true",
        "allow_promotion_codes": True,
        "billing_address_collection": "required",
        "payment_method_collection": "if_required",
        "subscription_data": {
            "trial_period_days": 14,
            "metadata": {"tier": request.tier, "supabase_user_id": user["account_id"] if user else ""},
        },
        "metadata": {"tier": request.tier, "supabase_user_id": user["account_id"] if user else ""},
    }

    if customer_id:
        checkout_params["customer"] = customer_id

    session = stripe.checkout.Session.create(**checkout_params)
    return {"url": session.url}


@router.post("/stripe/portal", response_model=UrlResponse)
async def create_portal_session(user=Depends(verify_user)) -> dict[str, Any]:  # noqa: B008
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
        return_url=f"{os.getenv('APP_URL', 'https://app.staging.mindroom.chat')}/dashboard/billing",
    )

    return {"url": session.url}
