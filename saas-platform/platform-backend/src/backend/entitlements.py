"""Subscription entitlement checks for hosted infrastructure."""

from __future__ import annotations

from datetime import UTC, datetime
from math import ceil
from typing import Any

from backend.pricing import get_plan_limits_from_metadata
from fastapi import HTTPException

PAID_TIERS = frozenset({"byok", "hobby", "pro", "enterprise"})
# past_due keeps service running while Stripe retries the payment; Stripe then moves the
# subscription to canceled or unpaid, which stops the instance.
SERVICE_STATUSES = frozenset({"active", "past_due"})


def db_subscription_status(stripe_status: str) -> str:
    """Map a Stripe subscription status to the stored status; the database spells it `cancelled`."""
    return "cancelled" if stripe_status == "canceled" else stripe_status


def parse_timestamp(value: str | None) -> datetime | None:
    """Parse a Supabase ISO timestamp into an aware UTC datetime."""
    if not value:
        return None

    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def is_subscription_service_active(subscription: dict[str, Any], *, now: datetime | None = None) -> bool:
    """Return whether a subscription may run customer infrastructure."""
    tier = str(subscription.get("tier") or "free")
    if tier not in PAID_TIERS:
        return False

    status = str(subscription.get("status") or "")
    if status in SERVICE_STATUSES:
        return True

    if status != "trialing":
        return False

    trial_ends_at = parse_timestamp(subscription.get("trial_ends_at"))
    if trial_ends_at is None:
        return False

    reference_time = (now or datetime.now(UTC)).astimezone(UTC)
    return trial_ends_at > reference_time


def is_expired_trial(subscription: dict[str, Any], *, now: datetime | None = None) -> bool:
    """Return whether a subscription is still marked trialing after its end timestamp."""
    if str(subscription.get("status") or "") != "trialing":
        return False

    trial_ends_at = parse_timestamp(subscription.get("trial_ends_at"))
    if trial_ends_at is None:
        return False

    reference_time = (now or datetime.now(UTC)).astimezone(UTC)
    return trial_ends_at <= reference_time


def trial_days_remaining(subscription: dict[str, Any], *, now: datetime | None = None) -> int | None:
    """Return rounded-up trial days remaining, or None when there is no trial clock."""
    if str(subscription.get("status") or "") not in {"trialing", "paused"}:
        return None

    trial_ends_at = parse_timestamp(subscription.get("trial_ends_at"))
    if trial_ends_at is None:
        return None

    reference_time = (now or datetime.now(UTC)).astimezone(UTC)
    seconds_remaining = (trial_ends_at - reference_time).total_seconds()
    if seconds_remaining <= 0:
        return 0
    return ceil(seconds_remaining / 86_400)


def decorate_subscription_for_response(
    subscription: dict[str, Any], *, plan_limits: dict[str, int] | None = None
) -> dict[str, Any]:
    """Add computed fields required by subscription API response models."""
    tier = str(subscription.get("tier") or "free")
    limits = plan_limits or get_plan_limits_from_metadata(tier)
    subscription["max_storage_gb"] = limits["max_storage_gb"]
    subscription["can_run_instances"] = is_subscription_service_active(subscription)
    subscription["trial_days_remaining"] = trial_days_remaining(subscription)
    return subscription


def _entitlement_failure_detail(subscription: dict[str, Any], action: str) -> str:
    tier = str(subscription.get("tier") or "free")
    status = str(subscription.get("status") or "unknown")

    if tier == "free":
        return f"Choose a plan before you {action} a hosted MindRoom instance."

    if status == "trialing":
        return "Your MindRoom trial has expired. Add billing or choose a paid plan to continue using the instance."

    if status == "unpaid":
        return "Payment failed. Update billing before you run the MindRoom instance."

    if status in {"cancelled", "paused", "incomplete", "incomplete_expired"}:
        return "This subscription is not active. Reactivate billing before you run the MindRoom instance."

    return "This subscription is not entitled to run a hosted MindRoom instance."


def assert_instance_entitlement(subscription: dict[str, Any], action: str) -> None:
    """Raise when the subscription cannot run hosted instance infrastructure."""
    if is_subscription_service_active(subscription):
        return

    raise HTTPException(status_code=402, detail=_entitlement_failure_detail(subscription, action))
