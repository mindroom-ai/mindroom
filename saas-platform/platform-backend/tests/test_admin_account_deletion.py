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
        """Mock the lifecycle teardown and the final auth user deletion of an account."""
        lifecycle = "backend.routes.admin.instance_lifecycle"
        with (
            patch(f"{lifecycle}.tear_down_account", new=AsyncMock()) as tear_down,
            patch(f"{lifecycle}.delete_auth_user", new=AsyncMock()) as delete_auth_user,
        ):
            yield Mock(tear_down_account=tear_down, delete_auth_user=delete_auth_user)

    @staticmethod
    def _account_tables(mock_supabase: MagicMock, instances: list[dict]) -> None:
        """Wire the account and instance lookups."""
        account_mock = MagicMock()
        account_mock.select.return_value = account_mock
        account_mock.eq.return_value = account_mock
        account_mock.execute.return_value = Mock(
            data=[{"id": "account_123", "email": "user@example.com", "stripe_customer_id": "cus_123"}]
        )
        instances_mock = MagicMock()
        instances_mock.select.return_value = instances_mock
        instances_mock.eq.return_value = instances_mock
        instances_mock.execute.return_value = Mock(data=instances)

        tables = {"accounts": account_mock, "instances": instances_mock}
        mock_supabase.table.side_effect = lambda name: tables.get(name, MagicMock())

    def test_delete_account_complete_success(
        self, client: TestClient, mock_verify_admin: Mock, mock_supabase: MagicMock, mock_tear_down: Mock
    ):
        """The account is marked pending deletion and claimed, torn down, and only then loses its rows and login."""
        self._account_tables(
            mock_supabase, [{"instance_id": 1, "status": "running"}, {"instance_id": 2, "status": "deprovisioned"}]
        )
        steps: list[str] = []
        mock_supabase.rpc.side_effect = lambda name, _params: steps.append(name) or MagicMock()
        mock_tear_down.tear_down_account.side_effect = lambda _account_id: steps.append("tear down")
        mock_tear_down.delete_auth_user.side_effect = lambda _account_id: steps.append("delete auth user")

        response = client.delete("/admin/accounts/account_123/complete")

        assert response.status_code == 200
        assert response.json() == {"data": {"id": "account_123"}}
        assert steps == ["soft_delete_account", "tear down", "hard_delete_account", "delete auth user"]
        assert mock_supabase.rpc.call_args_list[0].args == (
            "soft_delete_account",
            {"target_account_id": "account_123", "reason": "admin_complete_deletion", "requested_by": "admin_123"},
        )
        # The claim ends the customer's restore window and lets hard_delete_account act on the account.
        claim = mock_supabase.table("accounts").update.call_args.args[0]
        assert set(claim) == {"hard_delete_started_at"}
        mock_tear_down.delete_auth_user.assert_awaited_once_with("account_123")

    def test_delete_account_not_found(
        self, client: TestClient, mock_verify_admin: Mock, mock_supabase: MagicMock, mock_tear_down: Mock
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
        mock_tear_down.tear_down_account.assert_not_awaited()

    def test_failed_teardown_keeps_the_account(
        self, client: TestClient, mock_verify_admin: Mock, mock_supabase: MagicMock, mock_tear_down: Mock
    ):
        """If billing or an uninstall fails, the rows that map releases and keys to the owner stay."""
        self._account_tables(mock_supabase, [{"instance_id": 1, "status": "running"}])
        mock_tear_down.tear_down_account.side_effect = RuntimeError("Failed to uninstall instance: Kubernetes error")

        response = client.delete("/admin/accounts/account_123/complete")

        assert response.status_code == 500
        assert "account rows were kept, but Stripe billing may already be cancelled" in response.json()["detail"]
        mock_tear_down.delete_auth_user.assert_not_awaited()

    def test_failed_auth_user_deletion_keeps_the_account_row(
        self, client: TestClient, mock_verify_admin: Mock, mock_supabase: MagicMock, mock_tear_down: Mock
    ):
        """The login and account row stay after a failed auth deletion, so retrying the deletion finishes it."""
        self._account_tables(mock_supabase, [])
        mock_tear_down.delete_auth_user.side_effect = RuntimeError("auth unavailable")

        response = client.delete("/admin/accounts/account_123/complete")

        assert response.status_code == 500
        assert "deleting the account's rows or login failed, so the account row was kept" in response.json()["detail"]

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
