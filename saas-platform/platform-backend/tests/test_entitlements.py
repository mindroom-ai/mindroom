"""Subscription entitlement rules for hosted instances."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from backend.entitlements import assert_instance_entitlement, is_subscription_service_active


def _subscription(*, tier: str, status: str, trial_ends_at: str | None = None) -> dict:
    return {"id": "sub_123", "tier": tier, "status": status, "trial_ends_at": trial_ends_at}


def test_active_paid_subscription_can_run_instances() -> None:
    subscription = _subscription(tier="starter", status="active")

    assert is_subscription_service_active(subscription)
    assert_instance_entitlement(subscription, "start")


def test_unexpired_trial_can_run_instances() -> None:
    trial_end = datetime.now(UTC) + timedelta(days=2)
    subscription = _subscription(tier="starter", status="trialing", trial_ends_at=trial_end.isoformat())

    assert is_subscription_service_active(subscription)
    assert_instance_entitlement(subscription, "provision")


@pytest.mark.parametrize(
    "subscription",
    [
        _subscription(tier="free", status="active"),
        _subscription(tier="starter", status="trialing", trial_ends_at=(datetime.now(UTC) - timedelta(days=1)).isoformat()),
        _subscription(tier="starter", status="past_due"),
        _subscription(tier="starter", status="cancelled"),
        _subscription(tier="starter", status="paused"),
    ],
)
def test_inactive_or_free_subscription_cannot_run_instances(subscription: dict) -> None:
    assert not is_subscription_service_active(subscription)

    with pytest.raises(HTTPException) as exc:
        assert_instance_entitlement(subscription, "start")

    assert exc.value.status_code == 402
