"""Regression coverage for hosted billing projections and account suspension."""

from unittest.mock import Mock, patch

import pytest
from backend.routes import admin, webhooks
from backend.deps import verify_admin
from fastapi.testclient import TestClient
from main import app
from backend.services import instance_lifecycle

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


def test_subscription_rebound_during_retrieve_is_not_overwritten():
    db = database()
    newer = {"stripe_subscription_id": "sub_new", "tier": "hobby", "status": "trialing"}

    def retrieve(subscription_id):
        assert subscription_id == "sub_1"
        db.row("subscriptions", id="local_1").update(newer)
        return subscription("active", "pro")

    with (
        patch.object(webhooks, "ensure_supabase", return_value=db),
        patch.object(webhooks.stripe.Subscription, "retrieve", side_effect=retrieve),
    ):
        assert webhooks.handle_subscription_updated(subscription("active", "pro")) == (True, "owner")

    row = db.row("subscriptions", id="local_1")
    assert row == {
        "id": "local_1",
        "account_id": "owner",
        "updated_at": "before",
        **newer,
    }


def test_account_suspension_bans_auth_and_generic_reactivation_unbans():
    db = database()
    db.auth.admin = Mock()
    app.dependency_overrides[verify_admin] = lambda: {"user_id": "admin"}
    try:
        with patch.object(admin, "ensure_supabase", return_value=db), patch.object(admin, "audit_log_entry"):
            client = TestClient(app)
            response = client.put("/admin/accounts/owner/status", json={"status": "suspended"})
            assert response.status_code == 200
            db.auth.admin.update_user_by_id.assert_called_with("owner", {"ban_duration": "876000h"})
            assert db.row("accounts", id="owner")["status"] == "suspended"
            response = client.put("/admin/accounts/owner", json={"status": "active", "deleted_at": None})
            assert response.status_code == 200
            db.auth.admin.update_user_by_id.assert_called_with("owner", {"ban_duration": "none"})
            assert db.row("accounts", id="owner")["status"] == "active"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("route", ["/admin/accounts/owner/status", "/admin/accounts/owner"])
def test_failed_auth_ban_returns_500_and_retry_repairs_it(route):
    db = database()
    db.auth.admin = Mock()
    db.auth.admin.update_user_by_id.side_effect = RuntimeError("Auth unavailable")
    app.dependency_overrides[verify_admin] = lambda: {"user_id": "admin"}
    try:
        with patch.object(admin, "ensure_supabase", return_value=db), patch.object(admin, "audit_log_entry"):
            client = TestClient(app)
            response = client.put(route, json={"status": "suspended"})
            assert response.status_code == 500
            assert db.row("accounts", id="owner")["status"] == "suspended"
            db.auth.admin.update_user_by_id.side_effect = None
            response = client.put(route, json={"status": "suspended"})
            assert response.status_code == 200
            db.auth.admin.update_user_by_id.assert_called_with("owner", {"ban_duration": "876000h"})
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("route,expected", [("/admin/accounts/missing/status", 404), ("/admin/accounts/missing", 200)])
def test_missing_account_update_does_not_call_auth(route, expected):
    db = database()
    db.auth.admin = Mock()
    db.auth.admin.update_user_by_id.side_effect = ValueError("Invalid user id")
    app.dependency_overrides[verify_admin] = lambda: {"user_id": "admin"}
    try:
        with patch.object(admin, "ensure_supabase", return_value=db), patch.object(admin, "audit_log_entry"):
            response = TestClient(app).put(route, json={"status": "suspended"})
        assert response.status_code == expected
        if expected == 200:
            assert response.json() == {"data": None}
        db.auth.admin.update_user_by_id.assert_not_called()
    finally:
        app.dependency_overrides.clear()


def test_suspended_account_moved_to_deleted_is_unbanned():
    db = database()
    db.row("accounts", id="owner")["status"] = "suspended"
    db.auth.admin = Mock()
    app.dependency_overrides[verify_admin] = lambda: {"user_id": "admin"}
    try:
        with patch.object(admin, "ensure_supabase", return_value=db), patch.object(admin, "audit_log_entry"):
            response = TestClient(app).put("/admin/accounts/owner/status", json={"status": "deleted"})
        assert response.status_code == 200
        assert db.row("accounts", id="owner")["status"] == "deleted"
        db.auth.admin.update_user_by_id.assert_called_once_with("owner", {"ban_duration": "none"})
    finally:
        app.dependency_overrides.clear()
