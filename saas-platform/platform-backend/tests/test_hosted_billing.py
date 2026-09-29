"""Regression coverage for hosted billing projections and account suspension."""

import json
from unittest.mock import AsyncMock, Mock, patch

import pytest
from backend.routes import admin, webhooks
from backend.services import instance_lifecycle, provisioner_service

from tests.fake_supabase import FakeSupabase


def subscription(status="active", tier="hobby"):
    return {
        "id": "sub_1",
        "customer": "cus_1",
        "status": status,
        "trial_end": None,
        "items": {
            "data": [
                {
                    "price": {
                        "id": f"price_{tier}",
                        "metadata": {"tier": tier, "billing_cycle": "monthly"},
                    }
                }
            ]
        },
    }


def database():
    return FakeSupabase(
        {
            "accounts": [{"id": "owner", "stripe_customer_id": "cus_1", "status": "active"}],
            "subscriptions": [
                {
                    "id": "local_1",
                    "account_id": "owner",
                    "stripe_subscription_id": "sub_1",
                    "status": "active",
                    "tier": "pro",
                    "updated_at": "before",
                }
            ],
        }
    )


@pytest.mark.asyncio
async def test_plan_changes_preserve_the_published_key():
    db = FakeSupabase(
        {
            "instances": [
                {
                    "instance_id": 7,
                    "openrouter_key_hash": "hash_7",
                    "openrouter_key_limit_usd": 150,
                    "openrouter_key_limit_reset": "monthly",
                }
            ]
        }
    )
    with (
        patch.object(provisioner_service, "OPENROUTER_PROVISIONING_API_KEY", "test-management"),
        patch.object(provisioner_service, "_existing_instance_secret_value", AsyncMock(return_value="same-key")),
        patch("backend.openrouter._send_http_request", return_value=(200, b"{}")) as request,
    ):
        for tier, limit in [("hobby", 15), ("byok", 0), ("pro", 150)]:
            key, created = await provisioner_service._provision_openrouter_key(
                sb=db,
                account_id="owner",
                instance_id="7",
                tier=tier,
                existing_instance_row=db.row("instances", instance_id=7),
                namespace="test",
            )
            assert (key, created) == ("same-key", None)
            row = db.row("instances", instance_id=7)
            assert row["openrouter_key_hash"] == "hash_7"
            assert row["openrouter_key_limit_usd"] == limit
            method, url, _, body = request.call_args.args
            assert method == "PATCH"
            assert url.endswith("/keys/hash_7")
            assert json.loads(body) == {"limit": limit}


@pytest.mark.asyncio
@pytest.mark.parametrize("seam", ["created", "updated", "refresh", "payment_failed"])
@pytest.mark.parametrize(
    "amount,paid_subscription,expected",
    [
        (0, "sub_1", "unpaid"),
        (15, "other", "unpaid"),
        (15, "sub_1", "past_due"),
    ],
)
async def test_past_due_requires_a_paid_payment(seam, amount, paid_subscription, expected):
    db = database()
    db.tables["payments"] = [{"subscription_id": paid_subscription, "amount": amount, "status": "succeeded"}]
    remote = subscription("past_due")
    with (
        patch.object(webhooks, "ensure_supabase", return_value=db),
        patch.object(
            instance_lifecycle, "stripe", Mock(api_key="test", Subscription=Mock(retrieve=Mock(return_value=remote)))
        ),
        patch.object(webhooks.stripe.Subscription, "retrieve", return_value=remote),
    ):
        if seam == "refresh":
            await instance_lifecycle._refresh_status_from_stripe(db, "local_1")
        elif seam == "payment_failed":
            webhooks.handle_payment_failed(
                {"id": "in_1", "parent": {"subscription_details": {"subscription": "sub_1"}}}
            )
        else:
            getattr(webhooks, f"handle_subscription_{seam}")(remote)
    assert db.row("subscriptions", id="local_1")["status"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("seam", ["updated", "refresh"])
async def test_subscription_projection_uses_the_live_tier(seam):
    db = database()
    remote = subscription("active", "hobby")
    with (
        patch.object(webhooks, "ensure_supabase", return_value=db),
        patch.object(
            instance_lifecycle, "stripe", Mock(api_key="test", Subscription=Mock(retrieve=Mock(return_value=remote)))
        ),
        patch.object(webhooks.stripe.Subscription, "retrieve", return_value=remote),
    ):
        if seam == "refresh":
            await instance_lifecycle._refresh_status_from_stripe(db, "local_1")
        else:
            webhooks.handle_subscription_updated(subscription("active", "pro"))
    row = db.row("subscriptions", id="local_1")
    assert row["tier"] == "hobby"
    assert row["stripe_price_id"] == "price_hobby"


@pytest.mark.asyncio
async def test_account_suspension_bans_auth_and_reactivation_unbans():
    db = database()
    db.auth.admin = Mock()
    with patch.object(admin, "ensure_supabase", return_value=db), patch.object(admin, "audit_log_entry"):
        for status, duration in [("suspended", "876000h"), ("active", "none")]:
            await admin.update_account_status(
                "owner", admin.UpdateAccountStatusRequest(status=status), {"user_id": "admin"}
            )
            db.auth.admin.update_user_by_id.assert_called_with("owner", {"ban_duration": duration})
            assert db.row("accounts", id="owner")["status"] == status
