"""Test GDPR endpoints functionality."""

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import stripe
from fastapi.testclient import TestClient

from main import app
from backend.deps import verify_user

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "supabase/migrations"


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
    """Stub the instance lifecycle steps the deletion routes run after their RPC."""
    with (
        patch("backend.routes.gdpr.instance_lifecycle.cancel_account_billing", new=AsyncMock()) as cancel_billing,
        patch("backend.routes.gdpr.instance_lifecycle.reconcile_account_instances", new=AsyncMock()) as reconcile,
    ):
        yield MagicMock(cancel_account_billing=cancel_billing, reconcile_account_instances=reconcile)


def _function_body(sql: str, name: str) -> str:
    match = re.search(rf"CREATE OR REPLACE FUNCTION {name}\(.*?\$\$(.*?)\$\$", sql, re.DOTALL)
    assert match is not None, name
    return match.group(1)


def test_account_deletion_functions_change_only_the_account() -> None:
    """Soft delete and restore change only the account, restore ends with the grace period, and hard delete
    spares a restored account."""
    migration = (MIGRATIONS_DIR / "005_account_deletion_and_instance_uniqueness.sql").read_text(encoding="utf-8")
    baseline = (MIGRATIONS_DIR / "000_consolidated_complete_schema.sql").read_text(encoding="utf-8")

    assert migration.lstrip().startswith("--")
    assert "BEGIN;" in migration
    assert migration.rstrip().endswith("COMMIT;")
    for sql in (migration, baseline):
        for name in ("soft_delete_account", "restore_account"):
            body = _function_body(sql, name)
            assert "UPDATE accounts" in body
            assert "subscriptions" not in body
            assert "instances" not in body
        assert "AND deleted_at > NOW() - INTERVAL '7 days'" in _function_body(sql, "restore_account")
        assert "deleted_at IS NOT NULL) THEN" in _function_body(sql, "hard_delete_account")


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

        async def cancel_billing_before_soft_delete(_account_id: str) -> None:
            mock_rpc.execute.assert_not_called()

        async def hold_after_soft_delete(_account_id: str) -> None:
            mock_rpc.execute.assert_called_once_with()

        mock_lifecycle.cancel_account_billing.side_effect = cancel_billing_before_soft_delete
        mock_lifecycle.reconcile_account_instances.side_effect = hold_after_soft_delete

        response = client.post(
            "/my/gdpr/request-deletion", headers={"Authorization": "Bearer test-token"}, json={"confirmation": True}
        )

        assert response.status_code == 200
        data = response.json()

        assert data["status"] == "deletion_scheduled"
        assert data["grace_period_days"] == 7  # Reduced from 30 for GDPR compliance
        assert "deletion_date" in data
        assert "still pending deletion" in data["cancellation"]
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
        # Billing ends before the account is marked pending deletion, and its instances are held after.
        mock_lifecycle.cancel_account_billing.assert_awaited_once_with(mock_user["account_id"])
        mock_lifecycle.reconcile_account_instances.assert_awaited_once_with(mock_user["account_id"])
        assert "cancelled" in data["message"]

    def test_request_deletion_changes_nothing_when_stripe_cannot_cancel(
        self, client, mock_verify_user, mock_supabase, mock_lifecycle
    ):
        """A Stripe failure is reported before the soft delete, so the request can simply be retried."""
        mock_lifecycle.cancel_account_billing.side_effect = stripe.APIConnectionError("stripe unavailable")

        response = client.post(
            "/my/gdpr/request-deletion", headers={"Authorization": "Bearer test-token"}, json={"confirmation": True}
        )

        assert response.status_code == 502
        assert "nothing was deleted" in response.json()["detail"]
        mock_supabase.rpc.assert_not_called()
        mock_lifecycle.reconcile_account_instances.assert_not_awaited()

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
        mock_eq.execute.return_value = MagicMock(data=[{"deleted_at": deleted_at}])

        # Mock restore_account function
        mock_rpc = MagicMock()
        mock_rpc.execute.return_value = MagicMock(data=None)
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
        # Held instances resume only when their subscription is entitled.
        mock_lifecycle.reconcile_account_instances.assert_awaited_once_with(mock_user["account_id"])

    def test_cancel_deletion_is_refused_after_the_grace_period(
        self, client, mock_verify_user, mock_supabase, mock_lifecycle
    ):
        """Once cleanup may have uninstalled the instances, the account can no longer be restored."""
        deleted_at = (datetime.now(UTC) - timedelta(days=8)).isoformat()
        mock_supabase.table().select().eq().execute.return_value = MagicMock(data=[{"deleted_at": deleted_at}])

        response = client.post("/my/gdpr/cancel-deletion", headers={"Authorization": "Bearer test-token"})

        assert response.status_code == 409
        mock_supabase.rpc.assert_not_called()
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
