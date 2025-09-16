"""Test GDPR endpoints functionality."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.fixture
def client():
    """Create test client."""
    return TestClient(app)


@pytest.fixture
def mock_user():
    """Mock authenticated user."""
    return {
        "user_id": "test-user-id",
        "account_id": "test-account-id",
        "email": "test@example.com",
    }


@pytest.fixture
def mock_supabase():
    """Mock Supabase client."""
    with patch("backend.routes.gdpr.supabase") as mock:
        yield mock


class TestGDPREndpoints:
    """Test GDPR compliance endpoints."""

    def test_export_data_unauthenticated(self, client):
        """Test export requires authentication."""
        response = client.get("/my/gdpr/export-data")
        assert response.status_code == 401

    @patch("backend.routes.gdpr.verify_user")
    def test_export_data_success(self, mock_verify, client, mock_user, mock_supabase):
        """Test successful data export."""
        mock_verify.return_value = mock_user

        # Mock database responses
        mock_account = MagicMock()
        mock_account.data = {
            "email": "test@example.com",
            "full_name": "Test User",
            "company_name": "Test Company",
            "created_at": "2025-01-01T00:00:00Z",
        }

        mock_subscriptions = MagicMock()
        mock_subscriptions.data = [{"id": "sub-1", "tier": "pro", "status": "active"}]

        mock_instances = MagicMock()
        mock_instances.data = [{"id": "inst-1", "name": "test-instance"}]

        mock_audit_logs = MagicMock()
        mock_audit_logs.data = [{"action": "login", "created_at": "2025-01-01T00:00:00Z"}]

        # Setup mock chain
        mock_table = MagicMock()
        mock_supabase.table.return_value = mock_table
        mock_table.select.return_value = mock_table
        mock_table.eq.return_value = mock_table
        mock_table.single.return_value = mock_table
        mock_table.execute.side_effect = [mock_account, mock_subscriptions, mock_instances, mock_audit_logs]

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

    @patch("backend.routes.gdpr.verify_user")
    def test_request_deletion_without_confirmation(self, mock_verify, client, mock_user):
        """Test deletion request requires confirmation."""
        mock_verify.return_value = mock_user

        response = client.post("/my/gdpr/request-deletion", headers={"Authorization": "Bearer test-token"})

        assert response.status_code == 200
        data = response.json()
        assert "confirm deletion" in data["message"].lower()

    @patch("backend.routes.gdpr.verify_user")
    def test_request_deletion_with_confirmation(self, mock_verify, client, mock_user, mock_supabase):
        """Test successful deletion request."""
        mock_verify.return_value = mock_user

        # Mock soft_delete_account function
        mock_rpc = MagicMock()
        mock_rpc.execute.return_value = MagicMock(data=None)
        mock_supabase.rpc.return_value = mock_rpc

        response = client.post(
            "/my/gdpr/request-deletion?confirmation=true", headers={"Authorization": "Bearer test-token"}
        )

        assert response.status_code == 200
        data = response.json()

        assert data["status"] == "deletion_scheduled"
        assert data["grace_period_days"] == 30
        assert "final_deletion_date" in data

        # Verify soft delete was called
        mock_supabase.rpc.assert_called_with(
            "soft_delete_account", {"target_account_id": mock_user["account_id"], "reason": "user_request"}
        )

    @patch("backend.routes.gdpr.verify_user")
    def test_cancel_deletion(self, mock_verify, client, mock_user, mock_supabase):
        """Test canceling deletion request."""
        mock_verify.return_value = mock_user

        # Mock restore_account function
        mock_rpc = MagicMock()
        mock_rpc.execute.return_value = MagicMock(data=None)
        mock_supabase.rpc.return_value = mock_rpc

        response = client.post("/my/gdpr/cancel-deletion", headers={"Authorization": "Bearer test-token"})

        assert response.status_code == 200
        data = response.json()

        assert data["status"] == "deletion_cancelled"
        assert "account_restored" in data["message"]

        # Verify restore was called
        mock_supabase.rpc.assert_called_with("restore_account", {"target_account_id": mock_user["account_id"]})

    @patch("backend.routes.gdpr.verify_user")
    def test_update_consent(self, mock_verify, client, mock_user, mock_supabase):
        """Test updating consent preferences."""
        mock_verify.return_value = mock_user

        mock_table = MagicMock()
        mock_supabase.table.return_value = mock_table
        mock_insert = MagicMock()
        mock_table.insert.return_value = mock_insert
        mock_insert.execute.return_value = MagicMock()

        consent_data = {"necessary": True, "analytics": False, "marketing": False}

        response = client.post(
            "/my/gdpr/consent", headers={"Authorization": "Bearer test-token"}, json={"consents": consent_data}
        )

        assert response.status_code == 200
        data = response.json()

        assert data["status"] == "consent_updated"
        assert data["consents"] == consent_data

        # Verify database insert
        mock_supabase.table.assert_called_with("user_consents")
        insert_call = mock_table.insert.call_args[0][0]
        assert insert_call["account_id"] == mock_user["account_id"]

    @patch("backend.routes.gdpr.verify_user")
    def test_export_data_with_empty_results(self, mock_verify, client, mock_user, mock_supabase):
        """Test export with no data."""
        mock_verify.return_value = mock_user

        # Mock empty responses
        mock_empty = MagicMock()
        mock_empty.data = None

        mock_table = MagicMock()
        mock_supabase.table.return_value = mock_table
        mock_table.select.return_value = mock_table
        mock_table.eq.return_value = mock_table
        mock_table.single.return_value = mock_table
        mock_table.execute.return_value = mock_empty

        response = client.get("/my/gdpr/export-data", headers={"Authorization": "Bearer test-token"})

        assert response.status_code == 200
        data = response.json()

        # Should still have structure even with no data
        assert data["personal_data"]["email"] is None
        assert data["subscriptions"] == []
        assert data["instances"] == []

    @patch("backend.routes.gdpr.verify_user")
    def test_deletion_idempotent(self, mock_verify, client, mock_user, mock_supabase):
        """Test deletion request is idempotent."""
        mock_verify.return_value = mock_user

        mock_rpc = MagicMock()
        mock_rpc.execute.return_value = MagicMock(data=None)
        mock_supabase.rpc.return_value = mock_rpc

        # Request deletion twice
        for _ in range(2):
            response = client.post(
                "/my/gdpr/request-deletion?confirmation=true", headers={"Authorization": "Bearer test-token"}
            )
            assert response.status_code == 200
            assert response.json()["status"] == "deletion_scheduled"
