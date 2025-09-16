"""Tests for admin account deletion endpoint."""

from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from fastapi.testclient import TestClient


class TestAdminAccountDeletion:
    """Test admin account deletion endpoint."""

    @pytest.fixture
    def client(self) -> TestClient:
        """Create test client."""
        from main import app  # noqa: PLC0415

        return TestClient(app)

    @pytest.fixture
    def mock_verify_admin(self):
        """Mock admin verification."""
        from main import app  # noqa: PLC0415
        from backend.deps import verify_admin

        def override_verify_admin():
            return {
                "user_id": "admin_123",
                "email": "admin@example.com",
                "is_admin": True,
            }

        app.dependency_overrides[verify_admin] = override_verify_admin
        yield
        app.dependency_overrides.clear()

    @pytest.fixture
    def mock_supabase(self):
        """Mock Supabase client."""
        with patch("backend.routes.admin.ensure_supabase") as mock:
            sb = MagicMock()
            mock.return_value = sb
            yield sb

    @pytest.fixture
    def mock_uninstall_instance(self):
        """Mock uninstall_instance function."""
        with patch("backend.routes.admin.uninstall_instance") as mock:
            mock.return_value = {"success": True}
            yield mock

    def test_delete_account_complete_success(
        self,
        client: TestClient,
        mock_verify_admin: Mock,
        mock_supabase: MagicMock,
        mock_uninstall_instance: AsyncMock,
    ):
        """Test successful complete account deletion (without Stripe)."""
        # Setup account data without Stripe customer ID
        account_data = {
            "id": "account_123",
            "email": "user@example.com",
            "stripe_customer_id": None,  # No Stripe customer
        }

        # Setup instances data
        instances_data = [
            {"instance_id": 1, "status": "running"},
            {"instance_id": 2, "status": "stopped"},
        ]

        # Mock Supabase queries
        # Account lookup
        account_mock = MagicMock()
        account_mock.select.return_value = account_mock
        account_mock.eq.return_value = account_mock
        account_mock.execute.return_value = Mock(data=[account_data])

        # Instances lookup
        instances_mock = MagicMock()
        instances_mock.select.return_value = instances_mock
        instances_mock.eq.return_value = instances_mock
        instances_mock.execute.return_value = Mock(data=instances_data)

        # Account deletion
        delete_mock = MagicMock()
        delete_mock.delete.return_value = delete_mock
        delete_mock.eq.return_value = delete_mock
        delete_mock.execute.return_value = Mock(data=[])

        # Audit log insertion
        audit_mock = MagicMock()
        audit_mock.insert.return_value = audit_mock
        audit_mock.execute.return_value = Mock(data=[])

        def table_side_effect(table_name):
            if table_name == "accounts":
                return account_mock if not hasattr(account_mock, "_delete_called") else delete_mock
            elif table_name == "instances":
                return instances_mock
            elif table_name == "audit_logs":
                return audit_mock
            return MagicMock()

        mock_supabase.table.side_effect = table_side_effect

        # Make delete return the right mock
        account_mock.delete = lambda: delete_mock
        delete_mock._delete_called = True

        # Make the request
        response = client.delete("/admin/accounts/account_123/complete")

        # Assertions
        assert response.status_code == 200
        assert response.json() == {"data": {"id": "account_123"}}

        # Verify uninstall was called for both instances
        assert mock_uninstall_instance.call_count == 2

    def test_delete_account_not_found(
        self,
        client: TestClient,
        mock_verify_admin: Mock,
        mock_supabase: MagicMock,
    ):
        """Test deleting non-existent account."""
        # Mock empty account result
        account_mock = MagicMock()
        account_mock.select.return_value = account_mock
        account_mock.eq.return_value = account_mock
        account_mock.execute.return_value = Mock(data=[])

        mock_supabase.table.return_value = account_mock

        # Make the request
        response = client.delete("/admin/accounts/nonexistent_123/complete")

        # Assertions
        assert response.status_code == 404
        assert response.json()["detail"] == "Account not found"

    def test_delete_account_no_instances(
        self,
        client: TestClient,
        mock_verify_admin: Mock,
        mock_supabase: MagicMock,
        mock_uninstall_instance: AsyncMock,
    ):
        """Test deleting account with no instances."""
        # Setup account data without Stripe customer
        account_data = {
            "id": "account_123",
            "email": "user@example.com",
            "stripe_customer_id": None,
        }

        # Mock Supabase queries
        account_mock = MagicMock()
        account_mock.select.return_value = account_mock
        account_mock.eq.return_value = account_mock
        account_mock.execute.return_value = Mock(data=[account_data])

        instances_mock = MagicMock()
        instances_mock.select.return_value = instances_mock
        instances_mock.eq.return_value = instances_mock
        instances_mock.execute.return_value = Mock(data=[])  # No instances

        delete_mock = MagicMock()
        delete_mock.delete.return_value = delete_mock
        delete_mock.eq.return_value = delete_mock
        delete_mock.execute.return_value = Mock(data=[])

        audit_mock = MagicMock()
        audit_mock.insert.return_value = audit_mock
        audit_mock.execute.return_value = Mock(data=[])

        def table_side_effect(table_name):
            if table_name == "accounts":
                return account_mock if not hasattr(account_mock, "_delete_called") else delete_mock
            elif table_name == "instances":
                return instances_mock
            elif table_name == "audit_logs":
                return audit_mock
            return MagicMock()

        mock_supabase.table.side_effect = table_side_effect
        account_mock.delete = lambda: delete_mock
        delete_mock._delete_called = True

        # Make the request
        response = client.delete("/admin/accounts/account_123/complete")

        # Assertions
        assert response.status_code == 200
        assert response.json() == {"data": {"id": "account_123"}}

        # Verify uninstall was not called
        mock_uninstall_instance.assert_not_called()

    def test_delete_account_continues_on_instance_failure(
        self,
        client: TestClient,
        mock_verify_admin: Mock,
        mock_supabase: MagicMock,
    ):
        """Test that account deletion continues even if instance deprovisioning fails."""
        with patch("backend.routes.admin.uninstall_instance") as mock_uninstall:
            # Make uninstall fail
            mock_uninstall.side_effect = Exception("Kubernetes API error")

            # Setup account data
            account_data = {
                "id": "account_123",
                "email": "user@example.com",
                "stripe_customer_id": None,
            }

            # Setup instances data
            instances_data = [{"instance_id": 1, "status": "running"}]

            # Mock Supabase queries
            account_mock = MagicMock()
            account_mock.select.return_value = account_mock
            account_mock.eq.return_value = account_mock
            account_mock.execute.return_value = Mock(data=[account_data])

            instances_mock = MagicMock()
            instances_mock.select.return_value = instances_mock
            instances_mock.eq.return_value = instances_mock
            instances_mock.execute.return_value = Mock(data=instances_data)

            delete_mock = MagicMock()
            delete_mock.delete.return_value = delete_mock
            delete_mock.eq.return_value = delete_mock
            delete_mock.execute.return_value = Mock(data=[])

            audit_mock = MagicMock()
            audit_mock.insert.return_value = audit_mock
            audit_mock.execute.return_value = Mock(data=[])

            def table_side_effect(table_name):
                if table_name == "accounts":
                    return account_mock if not hasattr(account_mock, "_delete_called") else delete_mock
                elif table_name == "instances":
                    return instances_mock
                elif table_name == "audit_logs":
                    return audit_mock
                return MagicMock()

            mock_supabase.table.side_effect = table_side_effect
            account_mock.delete = lambda: delete_mock
            delete_mock._delete_called = True

            # Make the request
            response = client.delete("/admin/accounts/account_123/complete")

            # Should still succeed despite instance failure
            assert response.status_code == 200
            assert response.json() == {"data": {"id": "account_123"}}

    def test_generic_delete_blocks_account_deletion(
        self,
        client: TestClient,
        mock_verify_admin: Mock,
        mock_supabase: MagicMock,
    ):
        """Test that generic delete endpoint blocks account deletion."""
        # Make the request to generic delete endpoint
        response = client.delete("/admin/accounts/account_123")

        # Should be blocked
        assert response.status_code == 400
        assert "Use DELETE /admin/accounts/{account_id}/complete" in response.json()["detail"]

    def test_generic_delete_allows_other_resources(
        self,
        client: TestClient,
        mock_verify_admin: Mock,
        mock_supabase: MagicMock,
    ):
        """Test that generic delete endpoint works for non-account resources."""
        # Mock Supabase delete
        delete_mock = MagicMock()
        delete_mock.delete.return_value = delete_mock
        delete_mock.eq.return_value = delete_mock
        delete_mock.execute.return_value = Mock(data=[])

        audit_mock = MagicMock()
        audit_mock.insert.return_value = audit_mock
        audit_mock.execute.return_value = Mock(data=[])

        def table_side_effect(table_name):
            if table_name == "subscriptions":
                return delete_mock
            elif table_name == "audit_logs":
                return audit_mock
            return MagicMock()

        mock_supabase.table.side_effect = table_side_effect

        # Make the request for a subscription
        response = client.delete("/admin/subscriptions/sub_123")

        # Should succeed
        assert response.status_code == 200
        assert response.json() == {"data": {"id": "sub_123"}}
