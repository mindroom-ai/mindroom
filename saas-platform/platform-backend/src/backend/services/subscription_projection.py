"""Shared projection of Stripe subscriptions into hosted billing records."""

from datetime import UTC, datetime
from typing import Any, NotRequired, TypedDict

from backend.entitlements import db_subscription_status
from backend.pricing import get_plan_limits_from_metadata, get_stripe_price_match


class PermanentEventError(ValueError):
    """The event can never be applied (for example a price without tier metadata); record it instead of retrying."""


def timestamp_to_iso(timestamp: float) -> str:
    """Convert Unix timestamp to ISO format string."""
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat()


def maybe_timestamp_to_iso(timestamp: float | None) -> str | None:
    """Convert Unix timestamp to ISO format string, or None if timestamp is None."""
    return timestamp_to_iso(timestamp) if timestamp is not None else None


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
    raise PermanentEventError(msg)


def _get_billing_cycle_from_price(price: dict) -> str:
    """Extract the canonical billing cycle for a Stripe price."""
    if match := get_stripe_price_match(price.get("id")):
        return match.billing_cycle

    if (metadata := price.get("metadata", {})) and (cycle := metadata.get("billing_cycle")):
        return cycle

    msg = f"Unable to determine billing cycle from price. Price metadata: {price.get('metadata')}"
    raise PermanentEventError(msg)


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


def subscription_fields(sb: Any, subscription: dict) -> _SubscriptionFields:
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
        "status": subscription_status(sb, subscription["status"], subscription["id"]),
        "max_agents": limits.get("max_agents", 1),
        "max_messages_per_day": limits.get("max_messages_per_day", 100),
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
