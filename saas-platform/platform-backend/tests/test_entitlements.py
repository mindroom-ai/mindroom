"""Subscription entitlement rules for hosted instances."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from backend.entitlements import assert_instance_entitlement, is_subscription_service_active, trial_days_remaining


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


def test_trial_days_remaining_rounds_up_partial_days() -> None:
    now = datetime(2026, 5, 17, 12, 0, tzinfo=UTC)
    subscription = _subscription(
        tier="starter",
        status="trialing",
        trial_ends_at=(now + timedelta(days=2, hours=1)).isoformat(),
    )

    assert trial_days_remaining(subscription, now=now) == 3


def test_trial_days_remaining_is_zero_for_expired_trials() -> None:
    now = datetime(2026, 5, 17, 12, 0, tzinfo=UTC)
    subscription = _subscription(
        tier="starter",
        status="trialing",
        trial_ends_at=(now - timedelta(seconds=1)).isoformat(),
    )

    assert trial_days_remaining(subscription, now=now) == 0


def test_trial_days_remaining_is_none_outside_trialing_status() -> None:
    subscription = _subscription(tier="starter", status="active", trial_ends_at=None)

    assert trial_days_remaining(subscription) is None


@pytest.mark.parametrize(
    "subscription",
    [
        _subscription(tier="free", status="active"),
        _subscription(
            tier="starter", status="trialing", trial_ends_at=(datetime.now(UTC) - timedelta(days=1)).isoformat()
        ),
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
