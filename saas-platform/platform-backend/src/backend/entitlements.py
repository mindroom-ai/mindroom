"""Subscription entitlement checks for hosted infrastructure."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException

PAID_TIERS = frozenset({"starter", "professional", "enterprise"})


def _parse_timestamp(value: str | None) -> datetime | None:
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
    if status == "active":
        return True

    if status != "trialing":
        return False

    trial_ends_at = _parse_timestamp(subscription.get("trial_ends_at"))
    if trial_ends_at is None:
        return False

    reference_time = (now or datetime.now(UTC)).astimezone(UTC)
    return trial_ends_at > reference_time


def is_expired_trial(subscription: dict[str, Any], *, now: datetime | None = None) -> bool:
    """Return whether a subscription is still marked trialing after its end timestamp."""
    if str(subscription.get("status") or "") != "trialing":
        return False

    trial_ends_at = _parse_timestamp(subscription.get("trial_ends_at"))
    if trial_ends_at is None:
        return False

    reference_time = (now or datetime.now(UTC)).astimezone(UTC)
    return trial_ends_at <= reference_time


def _entitlement_failure_detail(subscription: dict[str, Any], action: str) -> str:
    tier = str(subscription.get("tier") or "free")
    status = str(subscription.get("status") or "unknown")

    if tier == "free":
        return f"Upgrade to a paid plan or start a trial before you {action} a hosted MindRoom instance."

    if status == "trialing":
        return "Your MindRoom trial has expired. Add billing or choose a paid plan to continue using the instance."

    if status == "past_due":
        return "Payment is past due. Update billing before you run the MindRoom instance."

    if status in {"cancelled", "paused"}:
        return "This subscription is not active. Reactivate billing before you run the MindRoom instance."

    return "This subscription is not entitled to run a hosted MindRoom instance."


def assert_instance_entitlement(subscription: dict[str, Any], action: str) -> None:
    """Raise when the subscription cannot run hosted instance infrastructure."""
    if is_subscription_service_active(subscription):
        return

    raise HTTPException(status_code=402, detail=_entitlement_failure_detail(subscription, action))
