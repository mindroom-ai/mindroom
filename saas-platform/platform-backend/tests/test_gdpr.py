"""Test GDPR endpoints functionality."""

import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
import stripe
from fastapi.testclient import TestClient

from main import app
from backend import auth_monitor, deps
from backend.deps import verify_user, verify_user_allow_deleted
from backend.routes import gdpr
from backend.services.instance_lifecycle import ScheduledBillingEnd

from tests.fake_supabase import FakeSupabase


@pytest.fixture
def client():
    """Create test client."""
    return TestClient(app)


@pytest.fixture
def mock_user():
    """Mock authenticated user."""
    return {
        "user_id": "00000000-0000-0000-0000-000000000001",
        "account_id": "00000000-0000-0000-0000-000000000002",
        "email": "test@example.com",
    }


@pytest.fixture
def mock_verify_user(mock_user):
    """Override verify_user dependency."""

    def override_verify_user():
        return mock_user

    app.dependency_overrides[verify_user] = override_verify_user
    app.dependency_overrides[verify_user_allow_deleted] = override_verify_user
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def mock_supabase():
    """Mock Supabase client."""
    with patch("backend.routes.gdpr.ensure_supabase") as mock:
        mock_sb = MagicMock()
        mock.return_value = mock_sb
        yield mock_sb


@pytest.fixture
def mock_lifecycle():
    """Stub the Stripe billing and instance lifecycle steps around the deletion RPCs; every step succeeds."""
    lifecycle = "backend.routes.gdpr.instance_lifecycle"
    with (
        patch(f"{lifecycle}.end_account_billing_at_period_end", new=AsyncMock(return_value=[])) as end_billing,
        patch(f"{lifecycle}.resume_account_billing", new=AsyncMock()) as resume_billing,
        patch(f"{lifecycle}.resume_subscriptions", new=AsyncMock()) as resume_subscriptions,
        patch(f"{lifecycle}.cancel_unpaid_subscriptions", new=AsyncMock()) as cancel_unpaid,
        patch(f"{lifecycle}.reconcile_account_instances", new=AsyncMock(return_value=[])) as reconcile,
        patch(f"{lifecycle}.restart_teardown_grace") as restart_teardown_grace,
    ):
        yield MagicMock(
            end_account_billing_at_period_end=end_billing,
            resume_account_billing=resume_billing,
            resume_subscriptions=resume_subscriptions,
            cancel_unpaid_subscriptions=cancel_unpaid,
            reconcile_account_instances=reconcile,
            restart_teardown_grace=restart_teardown_grace,
        )


def _account_deleted_at(mock_supabase: MagicMock, deleted_at: str | None) -> None:
    """Answer the route's pending-deletion lookup of the account."""
    lookup = mock_supabase.table.return_value.select.return_value.eq.return_value.limit.return_value
    lookup.execute.return_value = MagicMock(data=[{"deleted_at": deleted_at}])


def test_delete_and_cancel_round_trip_never_makes_an_unpaid_subscription_provisionable() -> None:
    """A pro checkout whose card failed stays incomplete through a deletion and its cancellation.

    The fake RPCs apply only the account changes that `test_account_deletion_sql.py` checks the real functions
    make, so the subscription and instance rows keep whatever Stripe last reported.
    """
    account_id = "00000000-0000-0000-0000-000000000002"
    db = FakeSupabase(
        {
            "accounts": [{"id": account_id, "email": "test@example.com", "stripe_customer_id": "cus_1"}],
            "subscriptions": [
                {
                    "id": "sub-row-1",
                    "account_id": account_id,
                    "stripe_subscription_id": "sub_stripe_1",
                    "tier": "pro",
                    "status": "incomplete",
                    "trial_ends_at": None,
                    "updated_at": "2026-09-01T00:00:00+00:00",
                }
            ],
            "instances": [],
            "audit_logs": [],
        }
    )
    record_rpc = db.rpc
    account = db.row("accounts", id=account_id)

    def account_only_rpc(name: str, params: dict) -> object:
        account["deleted_at"] = datetime.now(UTC).isoformat() if name == "soft_delete_account" else None
        db.rpc_results[name] = True
        return record_rpc(name, params)

    provision = AsyncMock()
    app.dependency_overrides[verify_user] = lambda: {"account_id": account_id, "email": "test@example.com"}
    app.dependency_overrides[verify_user_allow_deleted] = lambda: {
        "account_id": account_id,
        "email": "test@example.com",
    }
    try:
        with (
            patch.object(db, "rpc", side_effect=account_only_rpc),
            patch("backend.routes.gdpr.ensure_supabase", return_value=db),
            patch("backend.routes.instances.ensure_supabase", return_value=db),
            patch("backend.services.instance_lifecycle.ensure_supabase", return_value=db),
            patch("backend.services.instance_lifecycle.stripe", MagicMock(api_key="")),
            patch("backend.services.provisioner_service.provision_instance", provision),
        ):
            client = TestClient(app)
            deleted = client.post("/my/gdpr/request-deletion", json={"confirmation": True})
            cancelled = client.post("/my/gdpr/cancel-deletion")
            provisioned = client.post("/my/instances/provision")
    finally:
        app.dependency_overrides.clear()

    assert (deleted.status_code, cancelled.status_code) == (200, 200)
    assert [name for name, _params in db.rpc_calls] == ["soft_delete_account", "restore_account"]
    assert db.row("subscriptions", id="sub-row-1")["status"] == "incomplete"
    assert provisioned.status_code == 402
    provision.assert_not_awaited()
    assert db.tables["instances"] == []


class TestGDPREndpoints:
    """Test GDPR compliance endpoints."""

    def test_export_data_unauthenticated(self, client):
        """Test export requires authentication."""
        response = client.get("/my/gdpr/export-data")
        assert response.status_code == 401

    def test_export_data_success(self, client, mock_verify_user, mock_supabase):
        """Test successful data export."""

        # Mock database responses
        mock_account = MagicMock()
        mock_account.data = [
            {
                "email": "test@example.com",
                "full_name": "Test User",
                "company_name": "Test Company",
                "created_at": "2025-01-01T00:00:00Z",
            }
        ]

        mock_subscriptions = MagicMock()
        mock_subscriptions.data = [{"id": "sub-1", "tier": "pro", "status": "active"}]

        mock_instances = MagicMock()
        mock_instances.data = [{"id": "inst-1", "name": "test-instance"}]

        mock_usage = MagicMock()
        mock_usage.data = []

        mock_audit_logs = MagicMock()
        mock_audit_logs.data = [{"action": "login", "created_at": "2025-01-01T00:00:00Z"}]

        mock_payments = MagicMock()
        mock_payments.data = []

        # Setup mock chain - need separate mocks for each table call
        mock_table = MagicMock()
        mock_supabase.table.return_value = mock_table
        mock_table.select.return_value = mock_table
        mock_table.eq.return_value = mock_table
        mock_table.in_.return_value = mock_table
        mock_table.execute.side_effect = [
            mock_account,
            mock_subscriptions,
            mock_instances,
            mock_usage,
            mock_audit_logs,
            mock_payments,
        ]

        response = client.get("/my/gdpr/export-data", headers={"Authorization": "Bearer test-token"})

        assert response.status_code == 200
        data = response.json()

        # Verify export structure
        assert "export_date" in data
        assert "account_id" in data
        assert "personal_data" in data
        assert "subscriptions" in data
        assert "instances" in data
        assert "activity_history" in data
        assert "data_processing_purposes" in data
        assert "data_retention_periods" in data
        assert "account UUID" in data["data_retention_periods"]["audit_logs"]

        # Verify personal data
        assert data["personal_data"]["email"] == "test@example.com"
        assert data["personal_data"]["full_name"] == "Test User"

    def test_request_deletion_without_confirmation(self, client, mock_verify_user):
        """Test deletion request requires confirmation."""

        response = client.post(
            "/my/gdpr/request-deletion", headers={"Authorization": "Bearer test-token"}, json={"confirmation": False}
        )

        assert response.status_code == 200
        data = response.json()
        assert "confirm deletion" in data["message"].lower()

    def test_request_deletion_with_confirmation(
        self, client, mock_verify_user, mock_user, mock_supabase, mock_lifecycle
    ):
        """Test successful deletion request."""

        # Mock soft_delete_account function
        mock_rpc = MagicMock()
        mock_rpc.execute.return_value = MagicMock(data=None)
        mock_supabase.rpc.return_value = mock_rpc

        async def end_billing_before_soft_delete(_account_id: str) -> None:
            mock_rpc.execute.assert_not_called()

        async def hold_after_soft_delete(_account_id: str) -> list[str]:
            mock_rpc.execute.assert_called_once_with()
            return []

        mock_lifecycle.end_account_billing_at_period_end.side_effect = end_billing_before_soft_delete
        mock_lifecycle.reconcile_account_instances.side_effect = hold_after_soft_delete

        response = client.post(
            "/my/gdpr/request-deletion", headers={"Authorization": "Bearer test-token"}, json={"confirmation": True}
        )

        assert response.status_code == 200
        data = response.json()

        assert data["status"] == "deletion_scheduled"
        assert data["grace_period_days"] == 7  # Reduced from 30 for GDPR compliance
        assert "deletion_date" in data
        assert "Within 7 days" in data["cancellation"]
        assert "account UUID" in data["data_retained"]

        # Verify soft delete was called with correct reason
        mock_supabase.rpc.assert_called_with(
            "soft_delete_account",
            {
                "target_account_id": mock_user["account_id"],
                "reason": "gdpr_request",
                "requested_by": mock_user["account_id"],
            },
        )
        # Billing is set to end with its period before the account is marked pending deletion; instances stop after.
        mock_lifecycle.end_account_billing_at_period_end.assert_awaited_once_with(mock_user["account_id"])
        mock_lifecycle.reconcile_account_instances.assert_awaited_once_with(mock_user["account_id"])
        assert "Your hosted instances were stopped." in data["message"]
        assert "end at the end of their current billing period" in data["message"]

    def test_request_deletion_reports_instances_that_could_not_be_stopped(
        self, client, mock_verify_user, mock_supabase, mock_lifecycle
    ):
        """The deletion is recorded, and the message says the stop failed instead of claiming it happened."""
        mock_lifecycle.reconcile_account_instances.return_value = ["instance 7: kubectl scale failed"]

        response = client.post(
            "/my/gdpr/request-deletion", headers={"Authorization": "Bearer test-token"}, json={"confirmation": True}
        )

        assert response.status_code == 200
        assert "Stopping your hosted instances failed and is retried automatically." in response.json()["message"]

    def test_request_deletion_changes_nothing_when_stripe_cannot_schedule_the_end_of_billing(
        self, client, mock_verify_user, mock_supabase, mock_lifecycle
    ):
        """A Stripe failure is reported before the soft delete, so the request can simply be retried."""
        mock_lifecycle.end_account_billing_at_period_end.side_effect = stripe.APIConnectionError("stripe unavailable")

        response = client.post(
            "/my/gdpr/request-deletion", headers={"Authorization": "Bearer test-token"}, json={"confirmation": True}
        )

        assert response.status_code == 502
        assert "your account was not deleted" in response.json()["detail"]
        mock_supabase.rpc.assert_not_called()
        mock_supabase.table.assert_not_called()
        mock_lifecycle.reconcile_account_instances.assert_not_awaited()

    def test_failed_soft_delete_resumes_the_billing_it_had_scheduled_to_end(
        self, client, mock_verify_user, mock_user, mock_supabase, mock_lifecycle
    ):
        """If the deletion cannot be recorded, the billing this request set to end renews again, and only that."""
        mock_lifecycle.end_account_billing_at_period_end.return_value = [ScheduledBillingEnd("sub_a", "none")]
        mock_supabase.rpc.return_value.execute.side_effect = RuntimeError("connection reset")
        _account_deleted_at(mock_supabase, None)

        response = client.post(
            "/my/gdpr/request-deletion", headers={"Authorization": "Bearer test-token"}, json={"confirmation": True}
        )

        assert response.status_code == 500
        assert response.json()["detail"] == "Your account was not deleted and your billing is unchanged. Try again."
        mock_lifecycle.resume_subscriptions.assert_awaited_once_with([ScheduledBillingEnd("sub_a", "none")])
        mock_lifecycle.resume_account_billing.assert_not_awaited()
        mock_lifecycle.reconcile_account_instances.assert_not_awaited()

    def test_failed_soft_delete_whose_outcome_cannot_be_checked_reports_that(
        self, client, mock_verify_user, mock_supabase, mock_lifecycle
    ):
        """When even the follow-up lookup fails, nothing is undone and the customer is told the state is unknown."""
        mock_lifecycle.end_account_billing_at_period_end.return_value = [ScheduledBillingEnd("sub_a", "none")]
        mock_supabase.rpc.return_value.execute.side_effect = RuntimeError("connection reset")
        lookup = mock_supabase.table.return_value.select.return_value.eq.return_value.limit.return_value
        lookup.execute.side_effect = RuntimeError("connection reset")

        response = client.post(
            "/my/gdpr/request-deletion", headers={"Authorization": "Bearer test-token"}, json={"confirmation": True}
        )

        assert response.status_code == 500
        assert "could not confirm whether your deletion request was recorded" in response.json()["detail"]
        assert "may be set to end at the end of its billing period" in response.json()["detail"]
        mock_lifecycle.resume_subscriptions.assert_not_awaited()

    @pytest.mark.parametrize(
        "failure", [stripe.APIConnectionError("stripe unavailable"), RuntimeError("database unavailable")]
    )
    def test_unpaid_subscription_cancel_failure_keeps_the_recorded_deletion(
        self, client, mock_verify_user, mock_supabase, mock_lifecycle, failure
    ):
        """Instances are held first, and the nightly cleanup retries the cancellation, so the request succeeds."""
        steps: list[str] = []
        mock_lifecycle.reconcile_account_instances.side_effect = lambda _account_id: steps.append("hold") or []

        def cancel_fails(_account_id: str) -> None:
            steps.append("cancel unpaid")
            raise failure

        mock_lifecycle.cancel_unpaid_subscriptions.side_effect = cancel_fails

        response = client.post(
            "/my/gdpr/request-deletion", headers={"Authorization": "Bearer test-token"}, json={"confirmation": True}
        )

        assert response.status_code == 200
        assert response.json()["status"] == "deletion_scheduled"
        assert steps == ["hold", "cancel unpaid"]

    def test_soft_delete_that_committed_before_its_response_was_lost_stands(
        self, client, mock_verify_user, mock_user, mock_supabase, mock_lifecycle
    ):
        """The account is pending deletion after all, so its billing stays set to end and its instances stop."""
        mock_lifecycle.end_account_billing_at_period_end.return_value = [ScheduledBillingEnd("sub_a", "none")]
        mock_supabase.rpc.return_value.execute.side_effect = RuntimeError("connection reset")
        _account_deleted_at(mock_supabase, datetime.now(UTC).isoformat())

        response = client.post(
            "/my/gdpr/request-deletion", headers={"Authorization": "Bearer test-token"}, json={"confirmation": True}
        )

        assert response.status_code == 200
        mock_lifecycle.resume_subscriptions.assert_not_awaited()
        mock_lifecycle.reconcile_account_instances.assert_awaited_once_with(mock_user["account_id"])

    def test_cancel_deletion(self, client, mock_verify_user, mock_user, mock_supabase, mock_lifecycle):
        """Test canceling deletion request."""

        # Mock account query to show it's soft-deleted
        mock_table = MagicMock()
        mock_supabase.table.return_value = mock_table
        mock_select = MagicMock()
        mock_table.select.return_value = mock_select
        mock_eq = MagicMock()
        mock_select.eq.return_value = mock_eq
        deleted_at = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        mock_eq.limit.return_value.execute.return_value = MagicMock(data=[{"deleted_at": deleted_at}])

        # Mock restore_account function
        mock_rpc = MagicMock()
        mock_rpc.execute.return_value = MagicMock(data=True)
        mock_supabase.rpc.return_value = mock_rpc

        response = client.post("/my/gdpr/cancel-deletion", headers={"Authorization": "Bearer test-token"})

        assert response.status_code == 200
        data = response.json()

        assert data["status"] == "success"
        assert "cancelled" in data["message"]

        # The RPC restores the account and records the cancellation atomically.
        mock_supabase.rpc.assert_called_once_with("restore_account", {"target_account_id": mock_user["account_id"]})
        mock_rpc.execute.assert_called_once_with()
        mock_supabase.table.assert_called_once_with("accounts")
        # Billing the deletion set to end renews again, and held instances resume only when their subscription is
        # entitled.
        mock_lifecycle.restart_teardown_grace.assert_called_once_with(mock_user["account_id"])
        mock_lifecycle.resume_account_billing.assert_awaited_once_with(mock_user["account_id"])
        mock_lifecycle.reconcile_account_instances.assert_awaited_once_with(mock_user["account_id"])
        assert "Stripe could not resume" not in data["message"]

    def test_cancel_deletion_reports_billing_stripe_could_not_resume(
        self, client, mock_verify_user, mock_supabase, mock_lifecycle
    ):
        """The account is restored either way; the message says the subscription still ends if Stripe failed."""
        deleted_at = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        _account_deleted_at(mock_supabase, deleted_at)
        mock_supabase.rpc.return_value.execute.return_value = MagicMock(data=True)
        mock_lifecycle.resume_account_billing.side_effect = stripe.APIConnectionError("stripe unavailable")
        mock_lifecycle.reconcile_account_instances.return_value = ["instance 7: helm failed"]

        response = client.post("/my/gdpr/cancel-deletion", headers={"Authorization": "Bearer test-token"})

        assert response.status_code == 200
        message = response.json()["message"]
        assert "still ends at the end of its billing period" in message
        assert "Restarting your hosted instances failed and is retried automatically." in message

    def test_cancel_deletion_reports_a_restore_the_database_refused(
        self, client, mock_verify_user, mock_supabase, mock_lifecycle
    ):
        """After the grace period, or for a suspended account, restore_account returns false and nothing resumes."""
        deleted_at = (datetime.now(UTC) - timedelta(days=8)).isoformat()
        _account_deleted_at(mock_supabase, deleted_at)
        mock_supabase.rpc.return_value.execute.return_value = MagicMock(data=False)

        response = client.post("/my/gdpr/cancel-deletion", headers={"Authorization": "Bearer test-token"})

        assert response.status_code == 409
        mock_supabase.rpc.assert_called_once_with(
            "restore_account", {"target_account_id": "00000000-0000-0000-0000-000000000002"}
        )
        mock_lifecycle.resume_account_billing.assert_not_awaited()
        mock_lifecycle.reconcile_account_instances.assert_not_awaited()

    def test_update_consent(self, client, mock_verify_user, mock_user, mock_supabase):
        """Test updating consent preferences."""

        mock_table = MagicMock()
        mock_supabase.table.return_value = mock_table
        mock_update = MagicMock()
        mock_table.update.return_value = mock_update
        mock_eq = MagicMock()
        mock_update.eq.return_value = mock_eq
        mock_eq.execute.return_value = MagicMock()

        response = client.post(
            "/my/gdpr/consent",
            headers={"Authorization": "Bearer test-token"},
            json={"marketing": False, "analytics": True},
        )

        assert response.status_code == 200
        data = response.json()

        assert data["status"] == "success"
        assert data["consent"]["marketing"] is False
        assert data["consent"]["analytics"] is True
        assert data["consent"]["essential"] is True

        # Verify database update
        mock_supabase.table.assert_any_call("accounts")
        update_call = mock_table.update.call_args[0][0]
        assert update_call["consent_marketing"] is False
        assert update_call["consent_analytics"] is True

    def test_export_data_with_empty_results(self, client, mock_verify_user, mock_supabase):
        """Test export with no data."""

        # Mock empty responses
        mock_empty = MagicMock()
        mock_empty.data = []

        mock_table = MagicMock()
        mock_supabase.table.return_value = mock_table
        mock_table.select.return_value = mock_table
        mock_table.eq.return_value = mock_table
        mock_table.in_.return_value = mock_table
        mock_table.execute.return_value = mock_empty

        response = client.get("/my/gdpr/export-data", headers={"Authorization": "Bearer test-token"})

        assert response.status_code == 200
        data = response.json()

        # Should still have structure even with no data
        assert data["personal_data"]["email"] is None
        assert data["subscriptions"] == []
        assert data["instances"] == []

    def test_deletion_idempotent(self, client, mock_verify_user, mock_supabase, mock_lifecycle):
        """Test deletion request is idempotent."""

        mock_rpc = MagicMock()
        mock_rpc.execute.return_value = MagicMock(data=None)
        mock_supabase.rpc.return_value = mock_rpc

        # Request deletion twice
        for _ in range(2):
            response = client.post(
                "/my/gdpr/request-deletion", headers={"Authorization": "Bearer test-token"}, json={"confirmation": True}
            )
            assert response.status_code == 200
            assert response.json()["status"] == "deletion_scheduled"


@pytest.mark.usefixtures("mock_lifecycle")
def test_cached_token_cannot_provision_after_requesting_deletion(monkeypatch: pytest.MonkeyPatch) -> None:
    """Requesting deletion drops the account's cached auth, so the same token is refused on its next request."""
    monkeypatch.setattr(app, "dependency_overrides", {})
    monkeypatch.setattr(auth_monitor, "failed_attempts", defaultdict(list))
    monkeypatch.setattr(auth_monitor, "blocked_ips", {})
    deps._auth_cache.clear()
    account = {"id": "user_123", "email": "user@example.test", "status": "active", "deleted_at": None}
    auth_user = MagicMock()
    auth_user.user.id = "user_123"
    auth_user.user.email = "user@example.test"
    auth_client = MagicMock()
    auth_client.auth.get_user.return_value = auth_user
    sb = MagicMock()
    sb.table().select().eq().single().execute.side_effect = lambda: MagicMock(data=dict(account))

    def soft_delete(_name: str, _params: dict) -> MagicMock:
        account.update(status="deleted", deleted_at="2026-09-28T00:00:00Z")
        return MagicMock()

    sb.rpc.side_effect = soft_delete
    monkeypatch.setattr(deps, "_ensure_auth_client", lambda: auth_client)
    monkeypatch.setattr(deps, "ensure_supabase", lambda: sb)
    monkeypatch.setattr(gdpr, "ensure_supabase", lambda: sb)
    token = jwt.encode({"sub": "user_123", "exp": int(time.time()) + 300}, "secret", algorithm="HS256")
    headers = {"Authorization": f"Bearer {token}"}
    client = TestClient(app)

    deletion = client.post("/my/gdpr/request-deletion", headers=headers, json={"confirmation": True})
    provision = client.post("/my/instances/provision", headers=headers)

    assert deletion.status_code == 200
    assert provision.status_code == 403
    deps._auth_cache.clear()
