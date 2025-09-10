"""Webhook handlers for external services."""

from __future__ import annotations

from typing import Annotated, Any

from backend.config import STRIPE_WEBHOOK_SECRET, logger, stripe
from backend.deps import ensure_supabase
from fastapi import APIRouter, Header, HTTPException, Request

router = APIRouter()


def handle_subscription_created(subscription: dict) -> None:
    """Handle Stripe subscription creation events."""
    logger.info("Subscription created: %s", subscription["id"])
    sb = ensure_supabase()
    sb.table("subscriptions").upsert(
        {
            "subscription_id": subscription["id"],
            "customer_id": subscription["customer"],
            "status": subscription["status"],
            "tier": subscription["items"]["data"][0]["price"].get("lookup_key", "free")
            if subscription.get("items", {}).get("data")
            else "free",
        },
    ).execute()


def handle_subscription_deleted(subscription: dict) -> None:
    """Handle Stripe subscription deletion events."""
    logger.info("Subscription deleted: %s", subscription["id"])
    sb = ensure_supabase()
    sb.table("subscriptions").update({"status": "cancelled"}).eq(
        "subscription_id",
        subscription["id"],
    ).execute()


def handle_payment_succeeded(invoice: dict) -> None:
    """Handle successful Stripe payment events."""
    logger.info("Payment succeeded: %s", invoice["id"])
    sb = ensure_supabase()
    sb.table("payments").insert(
        {
            "invoice_id": invoice["id"],
            "subscription_id": invoice["subscription"],
            "customer_id": invoice["customer"],
            "amount": invoice["amount_paid"] / 100,
            "currency": invoice["currency"],
            "status": "succeeded",
        },
    ).execute()


@router.post("/webhooks/stripe")
async def stripe_webhook(
    request: Request,
    stripe_signature: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Handle incoming Stripe webhook events."""
    if not stripe_signature:
        raise HTTPException(status_code=400, detail="Missing signature")

    body = await request.body()

    try:
        event = stripe.Webhook.construct_event(body, stripe_signature, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        logger.exception("Webhook error")
        raise HTTPException(status_code=400, detail="Invalid signature") from e

    try:
        if event.type == "customer.subscription.created":
            handle_subscription_created(event.data.object)
        elif event.type == "customer.subscription.deleted":
            handle_subscription_deleted(event.data.object)
        elif event.type == "invoice.payment_succeeded":
            handle_payment_succeeded(event.data.object)
        else:
            logger.info("Unhandled event type: %s", event.type)
    except Exception as e:
        logger.exception("Error processing webhook")
        return {"received": True, "error": str(e)}
    else:
        return {"received": True}
