"""Shared projection of Stripe subscriptions into hosted billing records."""

from datetime import UTC, datetime
from typing import Any, NotRequired, TypedDict, get_args

from backend.entitlements import db_subscription_status
from backend.pricing import BillingCycle, get_stripe_price_match


class PermanentEventError(ValueError):
    """The event can never be applied (for example a price without tier metadata); record it instead of retrying."""


def timestamp_to_iso(timestamp: float) -> str:
    """Convert Unix timestamp to ISO format string."""
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat()


def maybe_timestamp_to_iso(timestamp: float | None) -> str | None:
    """Convert Unix timestamp to ISO format string, or None if timestamp is None."""
    return timestamp_to_iso(timestamp) if timestamp is not None else None


def _plan_from_price(price: dict) -> tuple[str, BillingCycle]:
    """Return the canonical tier and billing cycle of a Stripe price, from the pricing config or its metadata."""
    if match := get_stripe_price_match(price.get("id")):
        return match.tier, match.billing_cycle

    metadata = price.get("metadata") or {}
    if not (tier := metadata.get("tier")):
        msg = (
            f"Unable to determine tier from price. "
            f"Price metadata: {price.get('metadata')}, "
            f"lookup_key: {price.get('lookup_key')}"
        )
        raise PermanentEventError(msg)
    if (billing_cycle := metadata.get("billing_cycle")) not in get_args(BillingCycle):
        msg = f"Unable to determine billing cycle from price. Price metadata: {price.get('metadata')}"
        raise PermanentEventError(msg)
    return tier, billing_cycle


class _SubscriptionFields(TypedDict):
    """Shared subscription persistence fields and event-specific additions."""

    stripe_subscription_id: str
    stripe_price_id: str | None
    tier: str
    billing_cycle: BillingCycle
    price_cents: int | None
    status: str
    trial_ends_at: str | None
    updated_at: str
    current_period_start: NotRequired[str]
    current_period_end: NotRequired[str]
    account_id: NotRequired[str]
    cancelled_at: NotRequired[str | None]


def subscription_fields(sb: Any, subscription: dict) -> _SubscriptionFields:
    """Project the fields shared by subscription creation and update events."""
    item = subscription["items"]["data"][0] if subscription.get("items", {}).get("data") else {}
    price_data = item["price"] if item else {}
    tier, billing_cycle = _plan_from_price(price_data)

    subscription_data: _SubscriptionFields = {
        "stripe_subscription_id": subscription["id"],
        "stripe_price_id": price_data.get("id"),
        "tier": tier,
        "billing_cycle": billing_cycle,
        # Amount Stripe bills each billing cycle, in cents; null for tiered, pay-what-you-want, and sub-cent prices.
        "price_cents": price_data.get("unit_amount"),
        "status": subscription_status(sb, subscription["status"], subscription["id"]),
        "trial_ends_at": maybe_timestamp_to_iso(subscription.get("trial_end")),
        "updated_at": datetime.now(UTC).isoformat(),
    }

    # Since Stripe API version 2025-03-31.basil the billing period lives on each subscription item, not on the
    # subscription; our subscriptions have a single item.
    if start := item.get("current_period_start"):
        subscription_data["current_period_start"] = timestamp_to_iso(start)
    if end := item.get("current_period_end"):
        subscription_data["current_period_end"] = timestamp_to_iso(end)
    return subscription_data


def subscription_status(sb: Any, status: str, stripe_subscription_id: str) -> str:
    """Keep retry grace only for subscriptions with an actual successful payment."""
    if status == "past_due":
        paid = (
            sb.table("payments")
            .select("id")
            .eq("subscription_id", stripe_subscription_id)
            .eq("status", "succeeded")
            .gt("amount", 0)
            .limit(1)
            .execute()
            .data
        )
        if not paid:
            return "unpaid"
    return db_subscription_status(status)
