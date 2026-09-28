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
            return {"user_id": "admin_123", "email": "admin@example.com", "is_admin": True}

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
    def mock_tear_down(self):
        """Mock the lifecycle teardown that cancels billing and uninstalls every instance of the account."""
        with patch("backend.routes.admin.instance_lifecycle.tear_down_account", new=AsyncMock()) as mock:
            yield mock

    @staticmethod
    def _account_tables(mock_supabase: MagicMock, instances: list[dict]) -> MagicMock:
        """Wire the account, instance, and audit tables; return the account delete query."""
        account_mock = MagicMock()
        account_mock.select.return_value = account_mock
        account_mock.eq.return_value = account_mock
        account_mock.execute.return_value = Mock(
            data=[{"id": "account_123", "email": "user@example.com", "stripe_customer_id": "cus_123"}]
        )
        delete_mock = MagicMock()
        delete_mock.eq.return_value = delete_mock
        delete_mock.execute.return_value = Mock(data=[])
        account_mock.delete.return_value = delete_mock

        instances_mock = MagicMock()
        instances_mock.select.return_value = instances_mock
        instances_mock.eq.return_value = instances_mock
        instances_mock.execute.return_value = Mock(data=instances)

        tables = {"accounts": account_mock, "instances": instances_mock}
        mock_supabase.table.side_effect = lambda name: tables.get(name, MagicMock())
        return delete_mock

    def test_delete_account_complete_success(
        self, client: TestClient, mock_verify_admin: Mock, mock_supabase: MagicMock, mock_tear_down: AsyncMock
    ):
        """Billing ends and every instance is uninstalled before the account rows are deleted."""
        delete_mock = self._account_tables(
            mock_supabase, [{"instance_id": 1, "status": "running"}, {"instance_id": 2, "status": "deprovisioned"}]
        )

        async def rows_still_present(_account_id: str) -> None:
            delete_mock.execute.assert_not_called()

        mock_tear_down.side_effect = rows_still_present

        response = client.delete("/admin/accounts/account_123/complete")

        assert response.status_code == 200
        assert response.json() == {"data": {"id": "account_123"}}
        mock_tear_down.assert_awaited_once_with("account_123")
        delete_mock.execute.assert_called_once_with()

    def test_delete_account_not_found(
        self, client: TestClient, mock_verify_admin: Mock, mock_supabase: MagicMock, mock_tear_down: AsyncMock
    ):
        """Test deleting non-existent account."""
        account_mock = MagicMock()
        account_mock.select.return_value = account_mock
        account_mock.eq.return_value = account_mock
        account_mock.execute.return_value = Mock(data=[])
        mock_supabase.table.return_value = account_mock

        response = client.delete("/admin/accounts/nonexistent_123/complete")

        assert response.status_code == 404
        assert response.json()["detail"] == "Account not found"
        mock_tear_down.assert_not_awaited()

    def test_failed_teardown_keeps_the_account(
        self, client: TestClient, mock_verify_admin: Mock, mock_supabase: MagicMock, mock_tear_down: AsyncMock
    ):
        """If billing or an uninstall fails, the rows that map releases and keys to the owner stay."""
        delete_mock = self._account_tables(mock_supabase, [{"instance_id": 1, "status": "running"}])
        mock_tear_down.side_effect = RuntimeError("Failed to uninstall instance: Kubernetes API error")

        response = client.delete("/admin/accounts/account_123/complete")

        assert response.status_code == 500
        assert "account rows were kept, but Stripe billing may already be cancelled" in response.json()["detail"]
        delete_mock.execute.assert_not_called()

    def test_generic_delete_blocks_account_deletion(
        self, client: TestClient, mock_verify_admin: Mock, mock_supabase: MagicMock
    ):
        """Test that generic delete endpoint blocks account deletion."""
        # Make the request to generic delete endpoint
        response = client.delete("/admin/accounts/account_123")

        # Should be blocked
        assert response.status_code == 400
        assert "Use DELETE /admin/accounts/{account_id}/complete" in response.json()["detail"]

    def test_generic_delete_allows_other_resources(
        self, client: TestClient, mock_verify_admin: Mock, mock_supabase: MagicMock
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
