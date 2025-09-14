"""Pricing information routes."""

from __future__ import annotations

from typing import Any

from backend.models import StripePriceResponse
from backend.pricing import PRICING_CONFIG_MODEL, get_stripe_price_id
from fastapi import APIRouter, HTTPException

router = APIRouter()


@router.get("/pricing/config")
async def get_pricing_config() -> dict[str, Any]:
    """Get the current pricing configuration.

    This returns the pricing plans, features, and limits.
    Stripe price IDs are only included if they are configured.
    """
    config_model = PRICING_CONFIG_MODEL

    # Build response using the Pydantic model
    config = {
        "product": {
            "name": config_model.product.name,
            "description": config_model.product.description,
            "metadata": {"platform": config_model.product.metadata.platform},
        },
        "plans": {},
        "trial": {
            "enabled": config_model.trial.enabled,
            "days": config_model.trial.days,
            "applicable_plans": config_model.trial.applicable_plans,
        },
        "discounts": {
            "annual_percentage": config_model.discounts.annual_percentage,
        },
    }

    # Process each plan
    for plan_key, plan_data in config_model.plans.items():
        # Convert cents to dollar strings for frontend
        price_monthly = plan_data.price_monthly
        price_yearly = plan_data.price_yearly

        config["plans"][plan_key] = {
            "name": plan_data.name,
            "price_monthly": f"${price_monthly / 100:.0f}"
            if isinstance(price_monthly, (int, float))
            else price_monthly,
            "price_yearly": f"${price_yearly / 100:.0f}" if isinstance(price_yearly, (int, float)) else price_yearly,
            "price_model": plan_data.price_model or "flat",
            "description": plan_data.description,
            "features": plan_data.features,
            "limits": plan_data.limits.model_dump(),
            "recommended": plan_data.recommended,
            # Include Stripe price IDs if available
            "stripe_price_id_monthly": plan_data.stripe_price_id_monthly,
            "stripe_price_id_yearly": plan_data.stripe_price_id_yearly,
        }

    return config


@router.get("/pricing/stripe-price/{plan}/{billing_cycle}", response_model=StripePriceResponse)
async def get_stripe_price(plan: str, billing_cycle: str) -> dict[str, Any]:
    """Get the Stripe price ID for a specific plan and billing cycle.

    Args:
        plan: Plan key (e.g., 'starter', 'professional')
        billing_cycle: Either 'monthly' or 'yearly'

    Returns:
        Dict with price_id or error

    """
    if billing_cycle not in ["monthly", "yearly"]:
        raise HTTPException(
            status_code=400,
            detail="Invalid billing cycle. Must be 'monthly' or 'yearly'",
        )

    price_id = get_stripe_price_id(plan, billing_cycle)
    if not price_id:
        raise HTTPException(
            status_code=404,
            detail=f"No Stripe price configured for {plan} ({billing_cycle}). Run sync-stripe-prices.py",
        )

    return {"price_id": price_id, "plan": plan, "billing_cycle": billing_cycle}
