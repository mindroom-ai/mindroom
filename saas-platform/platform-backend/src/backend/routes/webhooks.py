"""Webhook handlers for external services."""

from datetime import UTC, datetime
from typing import Annotated, Any

from backend.config import STRIPE_WEBHOOK_SECRET, logger, stripe
from backend.deps import ensure_supabase, limiter
from backend.models import WebhookResponse
from backend.pricing import get_plan_limits_from_metadata
from fastapi import APIRouter, Header, HTTPException, Request

router = APIRouter()


def _timestamp_to_iso(timestamp: float) -> str:
    """Convert Unix timestamp to ISO format string."""
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat()


def _maybe_timestamp_to_iso(timestamp: float | None) -> str | None:
    """Convert Unix timestamp to ISO format string, or None if timestamp is None."""
    return _timestamp_to_iso(timestamp) if timestamp is not None else None


def _get_tier_from_price(price: dict) -> str:
    """Extract tier from price metadata.

    Our sync-stripe-prices.py script sets metadata.plan with the tier name.
    Also supports metadata.tier for backward compatibility.
    """
    if metadata := price.get("metadata", {}):
        # Try 'plan' first (new format), then 'tier' (old format)
        if plan := metadata.get("plan"):
            return plan
        if tier := metadata.get("tier"):
            return tier

    msg = (
        f"Unable to determine tier from price. "
        f"Price metadata: {price.get('metadata')}, "
        f"lookup_key: {price.get('lookup_key')}"
    )
    raise ValueError(msg)


def _get_billing_cycle_from_price(price: dict) -> str:
    """Extract billing cycle from price metadata.

    Our sync-stripe-prices.py script sets metadata.billing_cycle.
    """
    if (metadata := price.get("metadata", {})) and (cycle := metadata.get("billing_cycle")):
        return cycle

    msg = f"Unable to determine billing cycle from price. Price metadata: {price.get('metadata')}"
    raise ValueError(msg)


def handle_subscription_created(subscription: dict) -> tuple[bool, str | None]:
    """Handle Stripe subscription creation events.

    Returns:
        Tuple of (success, account_id) where account_id is used for webhook event tracking

    """
    logger.info("Subscription created: %s", subscription["id"])
    sb = ensure_supabase()

    # Get customer ID and find associated account
    customer_id = subscription["customer"]
    account_result = sb.table("accounts").select("id").eq("stripe_customer_id", customer_id).single().execute()

    if not account_result.data:
        logger.error("No account found for customer %s", customer_id)
        return False, None

    account_id = account_result.data["id"]

    # Extract subscription details
    price_data = subscription["items"]["data"][0]["price"] if subscription.get("items", {}).get("data") else {}
    tier = _get_tier_from_price(price_data)
    _billing_cycle = _get_billing_cycle_from_price(price_data)
    quantity = subscription["items"]["data"][0].get("quantity", 1) if subscription.get("items", {}).get("data") else 1

    # Get plan limits
    limits = get_plan_limits_from_metadata(tier)

    # Handle per-user limits for professional plan
    if tier == "professional" and quantity > 1:
        # Scale limits by user count
        if limits.get("max_agents") and isinstance(limits["max_agents"], int):
            limits["max_agents"] = limits["max_agents"] * quantity
        if limits.get("max_messages_per_day") and isinstance(limits["max_messages_per_day"], int):
            limits["max_messages_per_day"] = limits["max_messages_per_day"] * quantity

    # Prepare subscription data
    subscription_data = {
        "account_id": account_id,
        "stripe_subscription_id": subscription["id"],
        "stripe_price_id": price_data.get("id"),
        "tier": tier,
        "status": subscription["status"],
        "max_agents": limits.get("max_agents", 1),
        "max_messages_per_day": limits.get("max_messages_per_day", 100),
        "trial_ends_at": _maybe_timestamp_to_iso(subscription.get("trial_end")),
        "updated_at": datetime.now(UTC).isoformat(),
    }

    # Add period dates if available
    if start := subscription.get("current_period_start"):
        subscription_data["current_period_start"] = _timestamp_to_iso(start)
    if end := subscription.get("current_period_end"):
        subscription_data["current_period_end"] = _timestamp_to_iso(end)

    # Check if subscription already exists for this account
    existing = sb.table("subscriptions").select("id").eq("account_id", account_id).execute()

    if existing.data:
        # Update existing subscription
        sb.table("subscriptions").update(subscription_data).eq("account_id", account_id).execute()
    else:
        # Create new subscription
        sb.table("subscriptions").insert(subscription_data).execute()

    logger.info(
        "Subscription created for account %s: tier=%s, status=%s",
        account_id,
        tier,
        subscription["status"],
    )
    return True, account_id


def handle_subscription_updated(subscription: dict) -> tuple[bool, str | None]:
    """Handle Stripe subscription update events.

    Returns:
        Tuple of (success, account_id) where account_id is used for webhook event tracking

    """
    logger.info("Subscription updated: %s", subscription["id"])
    sb = ensure_supabase()

    # Get customer ID and find associated account
    customer_id = subscription["customer"]
    account_result = sb.table("accounts").select("id").eq("stripe_customer_id", customer_id).single().execute()

    if not account_result.data:
        logger.error("No account found for customer %s", customer_id)
        return False, None

    account_id = account_result.data["id"]

    # Extract subscription details
    price_data = subscription["items"]["data"][0]["price"] if subscription.get("items", {}).get("data") else {}
    tier = _get_tier_from_price(price_data)
    _billing_cycle = _get_billing_cycle_from_price(price_data)
    quantity = subscription["items"]["data"][0].get("quantity", 1) if subscription.get("items", {}).get("data") else 1

    # Get plan limits
    limits = get_plan_limits_from_metadata(tier)

    # Handle per-user limits for professional plan
    if tier == "professional" and quantity > 1:
        # Scale limits by user count
        if limits.get("max_agents") and isinstance(limits["max_agents"], int):
            limits["max_agents"] = limits["max_agents"] * quantity
        if limits.get("max_messages_per_day") and isinstance(limits["max_messages_per_day"], int):
            limits["max_messages_per_day"] = limits["max_messages_per_day"] * quantity

    # Prepare subscription data
    subscription_data = {
        "stripe_subscription_id": subscription["id"],
        "stripe_price_id": price_data.get("id"),
        "tier": tier,
        "status": subscription["status"],
        "max_agents": limits.get("max_agents", 1),
        "max_messages_per_day": limits.get("max_messages_per_day", 100),
        "trial_ends_at": _maybe_timestamp_to_iso(subscription.get("trial_end")),
        "cancelled_at": _maybe_timestamp_to_iso(subscription.get("canceled_at")),
        "updated_at": datetime.now(UTC).isoformat(),
    }

    # Add period dates if available
    if start := subscription.get("current_period_start"):
        subscription_data["current_period_start"] = _timestamp_to_iso(start)
    if end := subscription.get("current_period_end"):
        subscription_data["current_period_end"] = _timestamp_to_iso(end)

    # Update subscription with tenant validation
    sb.table("subscriptions").update(subscription_data).eq("account_id", account_id).execute()

    logger.info(
        "Subscription updated for account %s: tier=%s, status=%s",
        account_id,
        tier,
        subscription["status"],
    )
    return True, account_id


def handle_subscription_deleted(subscription: dict) -> tuple[bool, str | None]:
    """Handle Stripe subscription deletion events.

    Returns:
        Tuple of (success, account_id) where account_id is used for webhook event tracking

    """
    logger.info("Subscription deleted: %s", subscription["id"])
    sb = ensure_supabase()

    # First verify the subscription exists and get account_id for audit trail
    sub_result = (
        sb.table("subscriptions")
        .select("account_id")
        .eq("stripe_subscription_id", subscription["id"])
        .single()
        .execute()
    )

    if not sub_result.data:
        logger.warning(f"Webhook received for unknown subscription: {subscription['id']}")
        return False, None

    account_id = sub_result.data["account_id"]

    # Update subscription status to cancelled with tenant validation
    sb.table("subscriptions").update(
        {
            "status": "cancelled",
            "cancelled_at": datetime.now(UTC).isoformat(),
            "updated_at": datetime.now(UTC).isoformat(),
        },
    ).eq("stripe_subscription_id", subscription["id"]).eq(
        "account_id",
        account_id,  # Double-check account ownership
    ).execute()

    return True, account_id


def handle_payment_succeeded(invoice: dict) -> tuple[bool, str | None]:
    """Handle successful Stripe payment events.

    Returns:
        Tuple of (success, account_id) where account_id is used for webhook event tracking

    """
    logger.info("Payment succeeded: %s", invoice["id"])

    # Skip if no subscription (one-time payments)
    if not invoice.get("subscription"):
        return False, None

    sb = ensure_supabase()

    # Get account from customer
    customer_id = invoice["customer"]
    account_result = sb.table("accounts").select("id").eq("stripe_customer_id", customer_id).single().execute()

    if not account_result.data:
        logger.warning(f"No account found for customer_id: {customer_id} in payment")
        # Try to get account_id from subscription if available
        if invoice.get("subscription"):
            sub_result = (
                sb.table("subscriptions")
                .select("account_id")
                .eq("stripe_subscription_id", invoice["subscription"])
                .single()
                .execute()
            )
            if sub_result.data:
                account_id = sub_result.data["account_id"]
            else:
                return False, None
        else:
            return False, None
    else:
        account_id = account_result.data["id"]

    # Record the payment in both tables for compatibility
    # First, record in payments table with tenant isolation
    sb.table("payments").insert(
        {
            "invoice_id": invoice["id"],
            "subscription_id": invoice["subscription"],
            "customer_id": customer_id,
            "account_id": account_id,  # Add account_id for tenant isolation
            "amount": invoice["amount_paid"] / 100,
            "currency": invoice["currency"],
            "status": "succeeded",
        },
    ).execute()

    # Also record in usage table for metrics
    sb.table("usage").insert(
        {
            "account_id": account_id,
            "metric_type": "payment",
            "metric_value": invoice["amount_paid"] / 100,  # Convert from cents
            "metadata": {
                "invoice_id": invoice["id"],
                "subscription_id": invoice["subscription"],
                "currency": invoice["currency"],
                "billing_reason": invoice.get("billing_reason", "subscription_cycle"),
            },
            "timestamp": _timestamp_to_iso(invoice["created"]),
        },
    ).execute()

    return True, account_id


def handle_payment_failed(invoice: dict) -> tuple[bool, str | None]:
    """Handle failed Stripe payment events.

    Returns:
        Tuple of (success, account_id) where account_id is used for webhook event tracking

    """
    logger.info("Payment failed: %s", invoice["id"])

    # Skip if no subscription
    if not invoice.get("subscription"):
        return False, None

    sb = ensure_supabase()

    # Get account_id for tenant association
    sub_result = (
        sb.table("subscriptions")
        .select("account_id")
        .eq("stripe_subscription_id", invoice["subscription"])
        .single()
        .execute()
    )

    if not sub_result.data:
        logger.warning(f"No subscription found for payment failure: {invoice['subscription']}")
        return False, None

    account_id = sub_result.data["account_id"]

    # Update subscription status to past_due
    sb.table("subscriptions").update(
        {
            "status": "past_due",
            "updated_at": datetime.now(UTC).isoformat(),
        },
    ).eq("stripe_subscription_id", invoice["subscription"]).eq(
        "account_id",
        account_id,  # Tenant validation
    ).execute()

    return True, account_id


@router.post("/webhooks/stripe", response_model=WebhookResponse)
@limiter.limit("20/minute")
async def stripe_webhook(  # noqa: C901, PLR0912, PLR0915
    request: Request,
    stripe_signature: Annotated[str | None, Header(alias="Stripe-Signature")] = None,
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

    # Store the webhook event with tenant association
    sb = ensure_supabase()
    account_id = None
    error_msg = None

    try:
        # Subscription lifecycle events
        if event.type == "customer.subscription.created":
            success, account_id = handle_subscription_created(event.data.object)
            if not success:
                error_msg = "Failed to process subscription creation"
        elif event.type == "customer.subscription.updated":
            success, account_id = handle_subscription_updated(event.data.object)
            if not success:
                error_msg = "Failed to process subscription update"
        elif event.type == "customer.subscription.deleted":
            success, account_id = handle_subscription_deleted(event.data.object)
            if not success:
                error_msg = "Failed to process subscription deletion"

        # Payment events
        elif event.type == "invoice.payment_succeeded":
            success, account_id = handle_payment_succeeded(event.data.object)
            if not success:
                error_msg = "Failed to process payment"
        elif event.type == "invoice.payment_failed":
            success, account_id = handle_payment_failed(event.data.object)
            if not success:
                error_msg = "Failed to process payment failure"

        # Trial events
        elif event.type == "customer.subscription.trial_will_end":
            # Log for now, could send email notifications later
            logger.info("Trial ending soon for subscription: %s", event.data.object["id"])
            # Try to get account_id for audit
            if hasattr(event.data.object, "customer"):
                customer_id = event.data.object.customer
                acc_result = sb.table("accounts").select("id").eq("stripe_customer_id", customer_id).single().execute()
                if acc_result.data:
                    account_id = acc_result.data["id"]

        else:
            logger.info("Unhandled event type: %s", event.type)
            # For unhandled events, try to extract account_id from common fields
            if hasattr(event.data.object, "customer"):
                customer_id = event.data.object.customer
                acc_result = sb.table("accounts").select("id").eq("stripe_customer_id", customer_id).single().execute()
                if acc_result.data:
                    account_id = acc_result.data["id"]
    except Exception as e:
        logger.exception("Error processing webhook")
        error_msg = str(e)

    # Record the webhook event with tenant association
    try:
        webhook_record = {
            "stripe_event_id": event.id,
            "event_type": event.type,
            "payload": event.data.object,
            "processed_at": datetime.now(UTC).isoformat(),
        }

        # Add account_id if we could determine it
        if account_id:
            webhook_record["account_id"] = account_id

        # Add error if there was one
        if error_msg:
            webhook_record["error"] = error_msg

        sb.table("webhook_events").insert(webhook_record).execute()
    except Exception:
        logger.exception("Failed to record webhook event")

    if error_msg:
        return {"received": True, "error": error_msg}
    return {"received": True}
