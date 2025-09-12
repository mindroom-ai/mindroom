"""Pricing information routes."""

from __future__ import annotations

from typing import Any

from backend.pricing import PRICING_CONFIG, get_stripe_price_id
from fastapi import APIRouter, HTTPException

router = APIRouter()


@router.get("/pricing/config")
async def get_pricing_config() -> dict[str, Any]:
    """Get the current pricing configuration.

    This returns the pricing plans, features, and limits.
    Stripe price IDs are only included if they are configured.
    """
    if not PRICING_CONFIG:
        raise HTTPException(status_code=500, detail="Pricing configuration not available")

    # Filter out sensitive data if needed
    config = {
        "product": PRICING_CONFIG.get("product", {}),
        "plans": {},
        "trial": PRICING_CONFIG.get("trial", {}),
        "discounts": PRICING_CONFIG.get("discounts", {}),
    }

    # Process each plan
    for plan_key, plan_data in PRICING_CONFIG.get("plans", {}).items():
        # Convert cents to dollar strings for frontend
        price_monthly = plan_data.get("price_monthly", 0)
        price_yearly = plan_data.get("price_yearly", 0)

        config["plans"][plan_key] = {
            "name": plan_data.get("name"),
            "price_monthly": f"${price_monthly / 100:.0f}"
            if isinstance(price_monthly, (int, float))
            else price_monthly,
            "price_yearly": f"${price_yearly / 100:.0f}" if isinstance(price_yearly, (int, float)) else price_yearly,
            "price_model": plan_data.get("price_model", "flat"),
            "description": plan_data.get("description"),
            "features": plan_data.get("features", []),
            "limits": plan_data.get("limits", {}),
            "recommended": plan_data.get("recommended", False),
            # Include Stripe price IDs if available
            "stripe_price_id_monthly": plan_data.get("stripe_price_id_monthly"),
            "stripe_price_id_yearly": plan_data.get("stripe_price_id_yearly"),
        }

    return config


@router.get("/pricing/stripe-price/{plan}/{billing_cycle}")
async def get_stripe_price(plan: str, billing_cycle: str) -> dict[str, Any]:
    """Get the Stripe price ID for a specific plan and billing cycle.

    Args:
        plan: Plan key (e.g., 'starter', 'professional')
        billing_cycle: Either 'monthly' or 'yearly'

    Returns:
        Dict with price_id or error

    """
    if billing_cycle not in ["monthly", "yearly"]:
        raise HTTPException(status_code=400, detail="Invalid billing cycle. Must be 'monthly' or 'yearly'")

    price_id = get_stripe_price_id(plan, billing_cycle)
    if not price_id:
        raise HTTPException(
            status_code=404,
            detail=f"No Stripe price configured for {plan} ({billing_cycle}). Run sync-stripe-prices.py",
        )

    return {"price_id": price_id, "plan": plan, "billing_cycle": billing_cycle}
