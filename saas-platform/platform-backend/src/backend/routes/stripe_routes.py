"""Stripe payment and subscription routes."""

from dataclasses import dataclass
from typing import Annotated, Any

import anyio
from backend.config import PLATFORM_DOMAIN, logger, stripe
from backend.deps import ensure_supabase, limiter, verify_user
from backend.entitlements import ENDED_STRIPE_STATUSES
from backend.models import UrlResponse
from backend.pricing import get_plan_details, get_stripe_price_id, get_trial_days, is_trial_enabled_for_plan
from backend.services import provisioner_service
from backend.services.instance_lifecycle import PENDING_DELETION_BILLING_DETAIL
from backend.services.subscription_projection import PermanentEventError, plan_from_price
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

router = APIRouter()


@dataclass(frozen=True)
class _CustomerSubscriptions:
    """What checkout needs from a Stripe customer's subscription history."""

    unended_subscription: Any | None
    had_trial: bool


def _subscription_history(customer_id: str) -> _CustomerSubscriptions:
    unended_subscription = None
    had_trial = False
    for sub in stripe.Subscription.list(customer=customer_id, status="all", limit=100).auto_paging_iter():
        had_trial = had_trial or sub.trial_start is not None
        # A past_due, unpaid, paused, or incomplete subscription can still be paid or resumed, so checkout
        # would start a second one; only canceled and incomplete_expired subscriptions are over.
        if unended_subscription is None and sub.status not in ENDED_STRIPE_STATUSES:
            unended_subscription = sub
    return _CustomerSubscriptions(unended_subscription, had_trial)


# Identifies the portal configuration created for one plan's prices.
_PLAN_PORTAL_METADATA_KEY = "mindroom_upgrade_prices"


def _plan_portal_configuration(tier: str) -> str:
    """Return the portal configuration that offers only `tier`'s prices, creating it on first use.

    Every plan's prices share one Stripe product, and a portal configuration may list only one price per billing
    interval for each product, so the default configuration cannot offer plan changes and each plan gets its own.
    """
    prices = [price for cycle in ("monthly", "yearly") if (price := get_stripe_price_id(tier, cycle))]
    key = ",".join(prices)
    params = {
        "business_profile": {"headline": "Change your MindRoom plan"},
        "default_return_url": f"https://app.{PLATFORM_DOMAIN}/dashboard/billing",
        "features": {
            "subscription_update": {
                "enabled": True,
                "default_allowed_updates": ["price"],
                "products": [{"product": stripe.Price.retrieve(prices[0]).product, "prices": prices}],
                # The new plan starts a full billing period now, charged at once less the unused time of the old one,
                # because its whole monthly AI budget is available at once; a trial ends.
                "billing_cycle_anchor": "now",
                "proration_behavior": "always_invoice",
                "trial_update_behavior": "end_trial",
            },
            "payment_method_update": {"enabled": True},
            "invoice_history": {"enabled": True},
        },
    }
    for configuration in stripe.billing_portal.Configuration.list(active=True, limit=100).auto_paging_iter():
        if configuration.metadata.get(_PLAN_PORTAL_METADATA_KEY) == key:
            # Rewritten on each use, so a configuration an earlier release created gets these parameters.
            return stripe.billing_portal.Configuration.modify(configuration.id, **params).id
    return stripe.billing_portal.Configuration.create(**params, metadata={_PLAN_PORTAL_METADATA_KEY: key}).id


def _plan_change_portal_options(subscription: Any, tier: str, price_id: str) -> dict[str, Any]:
    """Return portal session options that open the confirmation of an upgrade to `price_id`.

    A subscription that is not active or trialing, or is set to cancel, gets none and opens on the portal home, where
    it can be paid or renewed first; so does one the portal cannot update (a schedule, invoices paid by hand, several
    items). So does a move to a plan that does not cost more: a downgrade would credit the unused time of a plan whose
    AI budget may already be spent.
    """
    if (
        subscription["status"] not in {"active", "trialing"}
        or subscription.get("cancel_at")
        or subscription.get("cancel_at_period_end")
        or subscription.get("schedule")
        or subscription.get("collection_method") == "send_invoice"
        or len(subscription["items"]["data"]) != 1
    ):
        return {}
    item = subscription["items"]["data"][0]
    try:
        # A price no longer in the pricing config still names its plan in its metadata.
        current_tier, _ = plan_from_price(item["price"])
    except PermanentEventError:
        return {}
    current_plan = get_plan_details(current_tier)
    plan = get_plan_details(tier)
    if not (
        current_plan
        and plan
        and isinstance(current_plan.price_monthly, int)
        and isinstance(plan.price_monthly, int)
        and plan.price_monthly > current_plan.price_monthly
    ):
        return {}
    return {
        "configuration": _plan_portal_configuration(tier),
        "flow_data": {
            "type": "subscription_update_confirm",
            "subscription_update_confirm": {
                "subscription": subscription["id"],
                "items": [{"id": item["id"], "price": price_id, "quantity": 1}],
            },
            "after_completion": {
                "type": "redirect",
                "redirect": {"return_url": f"https://app.{PLATFORM_DOMAIN}/dashboard/billing"},
            },
        },
    }


class CheckoutRequest(BaseModel):
    """Request model for creating Stripe checkout sessions."""

    tier: str
    billing_cycle: str = "monthly"  # monthly or yearly


@router.post("/stripe/checkout", response_model=UrlResponse)
@limiter.limit("5/minute")
async def create_checkout_session(
    request: Request,  # noqa: ARG001
    payload: CheckoutRequest,
    user: Annotated[dict, Depends(verify_user)],
) -> dict[str, Any]:
    """Create Stripe checkout session for subscription."""
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Stripe not configured")

    # Get price ID from config
    price_id = get_stripe_price_id(payload.tier, payload.billing_cycle)
    if not price_id:
        raise HTTPException(status_code=400, detail=f"No price found for {payload.tier} ({payload.billing_cycle})")

    sb = ensure_supabase()
    provisioner_service.refuse_pending_deletion(sb, user["account_id"], PENDING_DELETION_BILLING_DETAIL)
    result = sb.table("accounts").select("stripe_customer_id").eq("id", user["account_id"]).single().execute()
    if result.data and result.data.get("stripe_customer_id"):
        customer_id = result.data["stripe_customer_id"]
    else:
        customer = stripe.Customer.create(email=user["email"], metadata={"supabase_user_id": user["account_id"]})
        customer_id = customer.id
        sb.table("accounts").update({"stripe_customer_id": customer_id}).eq("id", user["account_id"]).execute()

    # Check if customer already has a subscription Stripe has not ended, and whether any past one had a trial
    history = await anyio.to_thread.run_sync(_subscription_history, customer_id)
    if history.unended_subscription is not None:
        # A second checkout would start a second subscription, so the portal changes the existing one instead.
        logger.info(
            "Customer %s already has an unended subscription %s, redirecting to portal",
            customer_id,
            history.unended_subscription.id,
        )
        options = await anyio.to_thread.run_sync(
            _plan_change_portal_options, history.unended_subscription, payload.tier, price_id
        )
        portal_session = stripe.billing_portal.Session.create(
            customer=customer_id, return_url=f"https://app.{PLATFORM_DOMAIN}/dashboard/billing", **options
        )
        return {"url": portal_session.url}

    checkout_params = {
        "line_items": [{"price": price_id, "quantity": 1}],
        "mode": "subscription",
        "success_url": f"https://app.{PLATFORM_DOMAIN}/dashboard?success=true&session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"https://app.{PLATFORM_DOMAIN}/dashboard/billing/upgrade?cancelled=true",
        "allow_promotion_codes": True,
        "billing_address_collection": "required",
        "subscription_data": {
            "metadata": {
                "tier": payload.tier,
                "billing_cycle": payload.billing_cycle,
                "supabase_user_id": user["account_id"],
            }
        },
    }

    # Add trial period if enabled for this plan; each customer gets one trial, since cancelling keeps it in Stripe
    if is_trial_enabled_for_plan(payload.tier) and not history.had_trial:
        checkout_params["subscription_data"]["trial_period_days"] = get_trial_days()

    checkout_params["customer"] = customer_id

    try:
        session = stripe.checkout.Session.create(**checkout_params)
    except Exception as e:
        logger.exception("Error creating checkout session")
        raise HTTPException(status_code=500, detail="Failed to create checkout session") from e

    return {"url": session.url}


@router.post("/stripe/portal", response_model=UrlResponse)
@limiter.limit("10/minute")
async def create_portal_session(request: Request, user: Annotated[dict, Depends(verify_user)]) -> dict[str, Any]:  # noqa: ARG001
    """Create Stripe customer portal session for subscription management."""
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Stripe not configured")
    sb = ensure_supabase()
    provisioner_service.refuse_pending_deletion(sb, user["account_id"], PENDING_DELETION_BILLING_DETAIL)

    # Stripe customer ID is stored on the accounts table
    result = sb.table("accounts").select("stripe_customer_id").eq("id", user["account_id"]).single().execute()
    if not result.data or not result.data.get("stripe_customer_id"):
        raise HTTPException(status_code=404, detail="No Stripe customer found")

    try:
        session = stripe.billing_portal.Session.create(
            customer=result.data["stripe_customer_id"],
            return_url=f"https://app.{PLATFORM_DOMAIN}/dashboard/billing?return=true",
        )
    except Exception as e:
        logger.exception("Error creating portal session")
        raise HTTPException(status_code=500, detail="Failed to create portal session") from e

    return {"url": session.url}
