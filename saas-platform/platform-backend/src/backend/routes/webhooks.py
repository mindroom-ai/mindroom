"""Webhook handlers for external services."""

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request

from backend.config import STRIPE_WEBHOOK_SECRET, logger, stripe
from backend.deps import ensure_supabase, limiter
from backend.models import WebhookResponse
from backend.services.instance_lifecycle import ENDED_STRIPE_STATUSES, reconcile_account_instances
from backend.services.subscription_projection import (
    PermanentEventError,
    maybe_timestamp_to_iso,
    subscription_fields,
    subscription_status,
    timestamp_to_iso,
)

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
# Events whose handler writes the subscription binding or status, or the payment records that decide whether a failed
# renewal keeps the past_due grace period. If one raises unexpectedly, the webhook answers 500 without recording it so
# Stripe redelivers it; those handlers are safe to run again.
_REDELIVERED_EVENT_TYPES = frozenset(
    {
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
        "invoice.payment_succeeded",
        "invoice.payment_failed",
    }
)


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


def _without_repeated_trial(subscription: dict) -> dict | None:
    """Return the subscription to store, or None when it duplicates an earlier trial and was cancelled.

    Checkout grants one trial per customer, but checkout sessions opened side by side can each carry one. Only the
    earliest trial counts: a later one is cancelled while the earlier subscription still runs, since the customer
    would otherwise pay twice, and otherwise it ends at once so the subscription is paid from the start.
    """
    if subscription.get("trial_start") is None:
        return subscription
    order = (subscription["created"], subscription["id"])
    history = stripe.Subscription.list(customer=subscription["customer"], status="all", limit=100).auto_paging_iter()
    earlier = [other for other in history if other.trial_start is not None and (other.created, other.id) < order]
    if not earlier:
        return subscription
    current = stripe.Subscription.retrieve(subscription["id"])
    if current["status"] in ENDED_STRIPE_STATUSES:
        # A redelivered event for a duplicate this handler already cancelled; the account keeps its binding.
        return None
    if current["status"] != "trialing":
        return current
    if any(other.status not in ENDED_STRIPE_STATUSES for other in earlier):
        logger.warning("Cancelling Stripe subscription %s: it duplicates an earlier trial", subscription["id"])
        stripe.Subscription.cancel(subscription["id"])
        return None
    logger.warning(
        "Ending the trial of Stripe subscription %s: customer %s already had one",
        subscription["id"],
        subscription["customer"],
    )
    return stripe.Subscription.modify(subscription["id"], trial_end="now")


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

    stored = _without_repeated_trial(subscription)
    if stored is None:
        return True, account_id
    subscription_data = subscription_fields(sb, stored)
    subscription_data["account_id"] = account_id
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
    # An update can arrive before the creation event, so it must not bind a duplicate trial either.
    if not current_stripe_id and _without_repeated_trial(subscription) is None:
        return True, account_id

    subscription = stripe.Subscription.retrieve(subscription["id"])
    subscription_data = subscription_fields(sb, subscription)
    subscription_data["cancelled_at"] = maybe_timestamp_to_iso(subscription.get("canceled_at"))

    # Update subscription with tenant validation
    query = sb.table("subscriptions").update(subscription_data).eq("account_id", account_id)
    if current_stripe_id:
        query = query.eq("stripe_subscription_id", current_stripe_id)
    query.execute()

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
        "created_at": timestamp_to_iso(paid_at),
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
    sb.table("subscriptions").update(
        {"status": subscription_status(sb, "past_due", subscription_id), "updated_at": datetime.now(UTC).isoformat()}
    ).eq("stripe_subscription_id", subscription_id).eq(
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
    except PermanentEventError as e:
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
