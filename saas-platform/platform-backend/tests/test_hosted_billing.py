"""Regression coverage for hosted billing projections and account suspension."""

from unittest.mock import Mock, patch

import pytest
from backend.pricing import get_stripe_price_id
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


async def apply_stripe_subscription(seam, db, remote, event=None):
    """Store `remote`, as Stripe now reports it, through the nightly refresh or a webhook handler that receives `event`."""
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
            getattr(webhooks, f"handle_subscription_{seam}")(event or remote)


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
    await apply_stripe_subscription(seam, db, subscription("past_due"))
    assert db.row("subscriptions", id="local_1")["status"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("seam", ["updated", "refresh"])
async def test_subscription_projection_uses_the_live_tier(seam):
    db = database()
    await apply_stripe_subscription(seam, db, subscription("active", "hobby"), event=subscription("active", "pro"))
    row = db.row("subscriptions", id="local_1")
    assert row["tier"] == "hobby"
    assert row["stripe_price_id"] == "price_hobby"


@pytest.mark.asyncio
@pytest.mark.parametrize("seam", ["updated", "refresh"])
@pytest.mark.parametrize(("billing_cycle", "unit_amount"), [("monthly", 1000), ("yearly", 9600)])
@pytest.mark.parametrize("listed", [True, False], ids=["listed", "unlisted"])
async def test_subscription_records_the_price_it_pays(seam, billing_cycle, unit_amount, listed):
    db = database()
    remote = subscription("active", "byok")
    price = remote["items"]["data"][0]["price"]
    price["unit_amount"] = unit_amount
    if listed:
        # A price in pricing-config.yaml takes its billing cycle from there, whatever its metadata says.
        price["id"] = get_stripe_price_id("byok", billing_cycle)
        price["metadata"] = {"tier": "byok", "billing_cycle": "yearly" if billing_cycle == "monthly" else "monthly"}
    else:
        price["metadata"] = {"tier": "byok", "billing_cycle": billing_cycle}
    await apply_stripe_subscription(seam, db, remote)
    row = db.row("subscriptions", id="local_1")
    assert (row["billing_cycle"], row["price_cents"]) == (billing_cycle, unit_amount)


@pytest.mark.asyncio
async def test_refresh_logs_the_fields_it_corrected():
    db = database()
    db.row("subscriptions", id="local_1")["stripe_price_id"] = "price_pro"
    remote = subscription("active", "pro")
    remote["items"]["data"][0]["price"]["unit_amount"] = 20000
    retrieve = Mock(return_value=remote)
    with (
        patch.object(instance_lifecycle, "stripe", Mock(api_key="test", Subscription=Mock(retrieve=retrieve))),
        patch.object(instance_lifecycle, "logger") as logger,
    ):
        await instance_lifecycle._refresh_status_from_stripe(db, "local_1")

    # A row stored before prices were recorded gets its price, and the warning says so instead of an unchanged status.
    logger.warning.assert_called_once_with(
        "Corrected subscription %s from Stripe: %s",
        "local_1",
        "billing_cycle None -> 'monthly', price_cents None -> 20000",
    )


@pytest.mark.asyncio
async def test_refresh_of_an_unchanged_subscription_writes_nothing():
    db = database()
    db.row("subscriptions", id="local_1").update(
        stripe_price_id="price_pro",
        billing_cycle="monthly",
        price_cents=20000,
        trial_ends_at="2027-01-15T08:00:00Z",  # The trial end Stripe reports, spelled differently.
    )
    remote = subscription("active", "pro") | {"trial_end": 1_800_000_000}
    remote["items"]["data"][0]["price"]["unit_amount"] = 20000
    retrieve = Mock(return_value=remote)
    with (
        patch.object(instance_lifecycle, "stripe", Mock(api_key="test", Subscription=Mock(retrieve=retrieve))),
        patch.object(instance_lifecycle, "logger") as logger,
    ):
        await instance_lifecycle._refresh_status_from_stripe(db, "local_1")

    assert db.row("subscriptions", id="local_1")["updated_at"] == "before"
    logger.warning.assert_not_called()


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
        with (
            patch.object(admin, "ensure_supabase", return_value=db),
            patch.object(admin, "audit_log_entry") as audit,
        ):
            client = TestClient(app)
            response = client.put(route, json={"status": "suspended"})
            assert response.status_code == 500
            assert db.row("accounts", id="owner")["status"] == "suspended"
            audit.assert_called_once()
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


@pytest.mark.parametrize("route", ["/admin/accounts/owner/status", "/admin/accounts/owner"])
@pytest.mark.parametrize("previous", ["active", "suspended"])
@pytest.mark.parametrize("status", ["suspended", "deleted", "pending_verification"])
def test_every_refused_status_bans_auth(route, previous, status):
    db = database()
    db.row("accounts", id="owner")["status"] = previous
    db.auth.admin = Mock()
    app.dependency_overrides[verify_admin] = lambda: {"user_id": "admin"}
    try:
        with patch.object(admin, "ensure_supabase", return_value=db), patch.object(admin, "audit_log_entry"):
            response = TestClient(app).put(route, json={"status": status})
        assert response.status_code == 200
        assert db.row("accounts", id="owner")["status"] == status
        db.auth.admin.update_user_by_id.assert_called_once_with("owner", {"ban_duration": "876000h"})
    finally:
        app.dependency_overrides.clear()


def test_suspended_account_awaiting_deletion_moved_to_deleted_is_unbanned_to_cancel():
    db = database()
    db.row("accounts", id="owner").update({"status": "suspended", "deleted_at": "2026-10-01T00:00:00+00:00"})
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


def test_generic_deleted_at_change_applies_auth_ban():
    db = database()
    db.row("accounts", id="owner").update({"status": "deleted", "deleted_at": "2026-10-01T00:00:00+00:00"})
    db.auth.admin = Mock()
    app.dependency_overrides[verify_admin] = lambda: {"user_id": "admin"}
    try:
        with patch.object(admin, "ensure_supabase", return_value=db), patch.object(admin, "audit_log_entry"):
            response = TestClient(app).put("/admin/accounts/owner", json={"deleted_at": None})
        assert response.status_code == 200
        db.auth.admin.update_user_by_id.assert_called_once_with("owner", {"ban_duration": "876000h"})
    finally:
        app.dependency_overrides.clear()
