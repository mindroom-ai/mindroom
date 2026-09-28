"""Webhook handlers for external services."""

from datetime import UTC, datetime
from typing import Annotated, Any, NotRequired, TypedDict

from backend.config import STRIPE_WEBHOOK_SECRET, logger, stripe
from backend.deps import ensure_supabase, limiter
from backend.entitlements import db_subscription_status
from backend.models import WebhookResponse
from backend.pricing import get_plan_limits_from_metadata, get_stripe_price_match
from backend.services.instance_lifecycle import reconcile_account_instances
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request

router = APIRouter()

# Events that can change whether a subscription may run its hosted instance.
_LIFECYCLE_EVENT_TYPES = frozenset(
    {
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
        "invoice.payment_succeeded",
        "invoice.payment_failed",
    }
)
# Events whose handler writes the subscription binding or status. If one raises unexpectedly, the webhook answers
# 500 without recording it so Stripe redelivers it; those handlers are safe to run again.
_REDELIVERED_EVENT_TYPES = frozenset(
    {
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
        "invoice.payment_failed",
    }
)


class _PermanentEventError(ValueError):
    """The event can never be applied (for example a price without tier metadata); record it instead of retrying."""


def _timestamp_to_iso(timestamp: float) -> str:
    """Convert Unix timestamp to ISO format string."""
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat()


def _maybe_timestamp_to_iso(timestamp: float | None) -> str | None:
    """Convert Unix timestamp to ISO format string, or None if timestamp is None."""
    return _timestamp_to_iso(timestamp) if timestamp is not None else None


def _get_tier_from_price(price: dict) -> str:
    """Extract the canonical tier for a Stripe price."""
    if match := get_stripe_price_match(price.get("id")):
        return match.tier

    if (metadata := price.get("metadata", {})) and (tier := metadata.get("tier")):
        return tier

    msg = (
        f"Unable to determine tier from price. "
        f"Price metadata: {price.get('metadata')}, "
        f"lookup_key: {price.get('lookup_key')}"
    )
    raise _PermanentEventError(msg)


def _get_billing_cycle_from_price(price: dict) -> str:
    """Extract the canonical billing cycle for a Stripe price."""
    if match := get_stripe_price_match(price.get("id")):
        return match.billing_cycle

    if (metadata := price.get("metadata", {})) and (cycle := metadata.get("billing_cycle")):
        return cycle

    msg = f"Unable to determine billing cycle from price. Price metadata: {price.get('metadata')}"
    raise _PermanentEventError(msg)


class _SubscriptionFields(TypedDict):
    """Shared subscription persistence fields and event-specific additions."""

    stripe_subscription_id: str
    stripe_price_id: str | None
    tier: str
    status: str
    max_agents: int
    max_messages_per_day: int
    trial_ends_at: str | None
    updated_at: str
    current_period_start: NotRequired[str]
    current_period_end: NotRequired[str]
    account_id: NotRequired[str]
    cancelled_at: NotRequired[str | None]


def _subscription_fields(subscription: dict) -> _SubscriptionFields:
    """Project the fields shared by subscription creation and update events."""
    item = subscription["items"]["data"][0] if subscription.get("items", {}).get("data") else {}
    price_data = item["price"] if item else {}
    tier = _get_tier_from_price(price_data)
    _get_billing_cycle_from_price(price_data)
    limits = get_plan_limits_from_metadata(tier)

    subscription_data: _SubscriptionFields = {
        "stripe_subscription_id": subscription["id"],
        "stripe_price_id": price_data.get("id"),
        "tier": tier,
        "status": db_subscription_status(subscription["status"]),
        "max_agents": limits.get("max_agents", 1),
        "max_messages_per_day": limits.get("max_messages_per_day", 100),
        "trial_ends_at": _maybe_timestamp_to_iso(subscription.get("trial_end")),
        "updated_at": datetime.now(UTC).isoformat(),
    }

    # Since Stripe API version 2025-03-31.basil the billing period lives on each subscription item, not on the
    # subscription; our subscriptions have a single item.
    if start := item.get("current_period_start"):
        subscription_data["current_period_start"] = _timestamp_to_iso(start)
    if end := item.get("current_period_end"):
        subscription_data["current_period_end"] = _timestamp_to_iso(end)
    return subscription_data


def _account_id_for_stripe_subscription(sb: Any, stripe_subscription_id: str) -> str | None:
    """Return the account bound to a Stripe subscription, or None when no row uses it (for example a superseded one)."""
    rows = (
        sb.table("subscriptions")
        .select("account_id")
        .eq("stripe_subscription_id", stripe_subscription_id)
        .limit(1)
        .execute()
        .data
    )
    return rows[0]["account_id"] if rows else None


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
    subscription_data = _subscription_fields(subscription)
    subscription_data["account_id"] = account_id

    # Check if subscription already exists for this account
    existing = sb.table("subscriptions").select("id,stripe_subscription_id").eq("account_id", account_id).execute()
    current_stripe_id = existing.data[0].get("stripe_subscription_id") if existing.data else None
    if current_stripe_id and current_stripe_id != subscription["id"]:
        # A delayed creation event for an older Stripe subscription must not replace a newer binding.
        current_created = stripe.Subscription.retrieve(current_stripe_id)["created"]
        if subscription["created"] < current_created:
            logger.info(
                "Ignoring creation of Stripe subscription %s older than %s", subscription["id"], current_stripe_id
            )
            return True, account_id

    if existing.data:
        # Update existing subscription
        sb.table("subscriptions").update(subscription_data).eq("account_id", account_id).execute()
    else:
        # Create new subscription
        sb.table("subscriptions").insert(subscription_data).execute()

    logger.info(
        "Subscription created for account %s: tier=%s, status=%s",
        account_id,
        subscription_data["tier"],
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

    current = sb.table("subscriptions").select("stripe_subscription_id").eq("account_id", account_id).execute().data
    current_stripe_id = current[0].get("stripe_subscription_id") if current else None
    if current_stripe_id and current_stripe_id != subscription["id"]:
        logger.info("Ignoring update for superseded Stripe subscription %s", subscription["id"])
        return True, account_id

    subscription_data = _subscription_fields(subscription)
    subscription_data["cancelled_at"] = _maybe_timestamp_to_iso(subscription.get("canceled_at"))

    # Update subscription with tenant validation
    sb.table("subscriptions").update(subscription_data).eq("account_id", account_id).execute()

    logger.info(
        "Subscription updated for account %s: tier=%s, status=%s",
        account_id,
        subscription_data["tier"],
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

    account_id = _account_id_for_stripe_subscription(sb, subscription["id"])
    if account_id is None:
        logger.info("Ignoring deletion of Stripe subscription %s that no account uses", subscription["id"])
        return True, None

    # Update subscription status to cancelled with tenant validation
    sb.table("subscriptions").update(
        {
            "status": "cancelled",
            "cancelled_at": datetime.now(UTC).isoformat(),
            "updated_at": datetime.now(UTC).isoformat(),
        }
    ).eq("stripe_subscription_id", subscription["id"]).eq(
        "account_id",
        account_id,  # Double-check account ownership
    ).execute()

    return True, account_id


def _invoice_subscription_id(invoice: dict) -> str | None:
    """Return the Stripe subscription an invoice bills, or None for a one-off invoice.

    Since Stripe API version 2025-03-31.basil the subscription lives under ``parent.subscription_details``;
    the top-level ``invoice.subscription`` field no longer exists.
    """
    details = (invoice.get("parent") or {}).get("subscription_details") or {}
    return details.get("subscription")


def payment_row(sb: Any, invoice: dict) -> dict[str, Any] | None:
    """Build the ``payments`` row for a paid invoice, or None for a one-off invoice or one no account owns.

    Shared by the ``invoice.payment_succeeded`` webhook and ``backend.scripts.backfill_payments``.
    """
    # Skip if no subscription (one-time payments)
    subscription_id = _invoice_subscription_id(invoice)
    if not subscription_id:
        return None

    # Get account from customer, falling back to the account bound to the subscription
    customer_id = invoice["customer"]
    accounts = sb.table("accounts").select("id").eq("stripe_customer_id", customer_id).limit(1).execute().data
    account_id = accounts[0]["id"] if accounts else _account_id_for_stripe_subscription(sb, subscription_id)
    if account_id is None:
        logger.warning("No account found for customer %s in payment %s", customer_id, invoice["id"])
        return None

    paid_at = (invoice.get("status_transitions") or {}).get("paid_at") or invoice["created"]
    return {
        "invoice_id": invoice["id"],
        "subscription_id": subscription_id,
        "customer_id": customer_id,
        "account_id": account_id,  # Tenant isolation
        "amount": invoice["amount_paid"] / 100,
        "currency": invoice["currency"],
        "status": "succeeded",
        "created_at": _timestamp_to_iso(paid_at),
    }


def upsert_payment(sb: Any, row: dict[str, Any]) -> None:
    """Write a payment row; upserting on ``invoice_id`` keeps one row per invoice however often it is written."""
    sb.table("payments").upsert(row, on_conflict="invoice_id").execute()


def handle_payment_succeeded(invoice: dict) -> tuple[bool, str | None]:
    """Handle successful Stripe payment events.

    Returns:
        Tuple of (success, account_id) where account_id is used for webhook event tracking

    """
    logger.info("Payment succeeded: %s", invoice["id"])
    sb = ensure_supabase()
    row = payment_row(sb, invoice)
    if row is None:
        return False, None
    upsert_payment(sb, row)
    return True, row["account_id"]


def handle_payment_failed(invoice: dict) -> tuple[bool, str | None]:
    """Handle failed Stripe payment events.

    Returns:
        Tuple of (success, account_id) where account_id is used for webhook event tracking

    """
    logger.info("Payment failed: %s", invoice["id"])

    # Skip if no subscription
    subscription_id = _invoice_subscription_id(invoice)
    if not subscription_id:
        return False, None

    sb = ensure_supabase()

    account_id = _account_id_for_stripe_subscription(sb, subscription_id)
    if account_id is None:
        logger.info("Ignoring payment failure for Stripe subscription %s that no account uses", subscription_id)
        return True, None

    # Only an active subscription becomes past_due; past_due keeps the instance running, so a failed
    # first payment (incomplete) or a late event for a cancelled subscription must not reach it.
    sb.table("subscriptions").update({"status": "past_due", "updated_at": datetime.now(UTC).isoformat()}).eq(
        "stripe_subscription_id", subscription_id
    ).eq(
        "account_id",
        account_id,  # Tenant validation
    ).eq("status", "active").execute()

    return True, account_id


@router.post("/webhooks/stripe", response_model=WebhookResponse)
@limiter.limit("20/minute")
async def stripe_webhook(  # noqa: C901, PLR0912, PLR0915
    request: Request,
    background_tasks: BackgroundTasks,
    stripe_signature: Annotated[str | None, Header(alias="Stripe-Signature")] = None,
) -> dict[str, Any]:
    """Handle incoming Stripe webhook events."""
    # An empty secret makes the HMAC signature forgeable, so refuse every event.
    if not STRIPE_WEBHOOK_SECRET:
        logger.error("STRIPE_WEBHOOK_SECRET is not configured; rejecting Stripe webhook")
        raise HTTPException(status_code=503, detail="Webhook not configured")

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
    except _PermanentEventError as e:
        logger.exception("Webhook %s can never be applied; recording it", event.id)
        error_msg = str(e)
    except Exception as e:
        if event.type in _REDELIVERED_EVENT_TYPES:
            # Not recorded as processed, so Stripe redelivers it and the idempotent handler runs again.
            logger.exception("Webhook %s failed; asking Stripe to redeliver", event.id)
            raise HTTPException(status_code=500, detail="Failed to process event") from e
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

    # Stop, start, or reprovision instances after the response so Kubernetes trouble never fails the webhook.
    if account_id and event.type in _LIFECYCLE_EVENT_TYPES:
        background_tasks.add_task(reconcile_account_instances, account_id)

    if error_msg:
        return {"received": True, "error": error_msg}
    return {"received": True}
