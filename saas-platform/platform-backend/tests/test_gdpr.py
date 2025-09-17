"""Test GDPR endpoints functionality."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from main import app
from backend.deps import verify_user


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

    def test_request_deletion_with_confirmation(self, client, mock_verify_user, mock_user, mock_supabase):
        """Test successful deletion request."""

        # Mock soft_delete_account function
        mock_rpc = MagicMock()
        mock_rpc.execute.return_value = MagicMock(data=None)
        mock_supabase.rpc.return_value = mock_rpc

        response = client.post(
            "/my/gdpr/request-deletion", headers={"Authorization": "Bearer test-token"}, json={"confirmation": True}
        )

        assert response.status_code == 200
        data = response.json()

        assert data["status"] == "deletion_scheduled"
        assert data["grace_period_days"] == 7  # Reduced from 30 for GDPR compliance
        assert "deletion_date" in data

        # Verify soft delete was called with correct reason
        mock_supabase.rpc.assert_called_with(
            "soft_delete_account",
            {
                "target_account_id": mock_user["account_id"],
                "reason": "gdpr_request",
                "requested_by": mock_user["account_id"],
            },
        )

    def test_cancel_deletion(self, client, mock_verify_user, mock_user, mock_supabase):
        """Test canceling deletion request."""

        # Mock account query to show it's soft-deleted
        mock_table = MagicMock()
        mock_supabase.table.return_value = mock_table
        mock_select = MagicMock()
        mock_table.select.return_value = mock_select
        mock_eq = MagicMock()
        mock_select.eq.return_value = mock_eq
        mock_eq.execute.return_value = MagicMock(data=[{"deleted_at": "2025-01-01T00:00:00Z"}])

        # Mock restore_account function
        mock_rpc = MagicMock()
        mock_rpc.execute.return_value = MagicMock(data=None)
        mock_supabase.rpc.return_value = mock_rpc

        response = client.post("/my/gdpr/cancel-deletion", headers={"Authorization": "Bearer test-token"})

        assert response.status_code == 200
        data = response.json()

        assert data["status"] == "success"
        assert "cancelled" in data["message"]

        # Verify restore was called
        mock_supabase.rpc.assert_called_with("restore_account", {"target_account_id": mock_user["account_id"]})

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

    def test_deletion_idempotent(self, client, mock_verify_user, mock_supabase):
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
