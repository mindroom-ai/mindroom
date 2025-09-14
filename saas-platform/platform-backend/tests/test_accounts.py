"""Comprehensive HTTP API tests for accounts endpoints."""

from datetime import UTC, datetime
from unittest.mock import MagicMock, Mock, patch

import pytest
from fastapi.testclient import TestClient


class TestAccountsEndpoints:
    """Test accounts endpoints via HTTP API."""

    @pytest.fixture
    def client(self) -> TestClient:
        """Create test client."""
        from main import app  # noqa: PLC0415

        return TestClient(app)

    @pytest.fixture
    def mock_supabase(self):
        """Mock Supabase client."""
        with patch("backend.routes.accounts.ensure_supabase") as mock:
            sb = MagicMock()
            mock.return_value = sb
            yield sb

    @pytest.fixture
    def mock_verify_user(self):
        """Mock user verification."""
        from main import app  # noqa: PLC0415
        from backend.deps import verify_user

        def override_verify_user():
            return {"account_id": "acc_test_123", "email": "test@example.com"}

        app.dependency_overrides[verify_user] = override_verify_user
        yield
        app.dependency_overrides.clear()

    def test_get_current_account_success(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_user: Mock,
    ):
        """Test getting current account with relations successfully."""
        # Setup
        account_data = {
            "id": "acc_test_123",
            "email": "test@example.com",
            "status": "active",
            "is_admin": False,
            "created_at": datetime.now(UTC).isoformat(),
            "updated_at": datetime.now(UTC).isoformat(),
            "subscriptions": [
                {
                    "id": "sub_123",
                    "account_id": "acc_test_123",
                    "tier": "professional",
                    "status": "active",
                    "instances": [
                        {
                            "id": "inst_123",
                            "instance_id": "123",
                            "status": "running",
                        }
                    ],
                }
            ],
        }
        mock_supabase.table().select().eq().single().execute.return_value = Mock(data=account_data)

        # Make request
        response = client.get("/my/account")

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "acc_test_123"
        assert data["email"] == "test@example.com"
        assert len(data["subscriptions"]) == 1
        assert data["subscriptions"][0]["tier"] == "professional"

    def test_get_current_account_not_found(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_user: Mock,
    ):
        """Test getting account when it doesn't exist."""
        # Setup
        mock_supabase.table().select().eq().single().execute.return_value = Mock(data=None)

        # Make request
        response = client.get("/my/account")

        # Verify
        assert response.status_code == 404
        assert "Account not found" in response.json()["detail"]

    def test_get_current_account_unauthorized(self, client: TestClient):
        """Test getting account without authentication."""
        from main import app  # noqa: PLC0415
        from backend.deps import verify_user
        from fastapi import HTTPException

        def override_verify_user():
            raise HTTPException(status_code=401, detail="Unauthorized")

        app.dependency_overrides[verify_user] = override_verify_user
        try:
            response = client.get("/my/account")
            assert response.status_code == 401
        finally:
            app.dependency_overrides.clear()

    def test_check_admin_status_true(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_user: Mock,
    ):
        """Test checking admin status when user is admin."""
        # Setup
        mock_supabase.table().select().eq().single().execute.return_value = Mock(data={"is_admin": True})

        # Make request
        response = client.get("/my/account/admin-status")

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["is_admin"] is True

    def test_check_admin_status_false(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_user: Mock,
    ):
        """Test checking admin status when user is not admin."""
        # Setup
        mock_supabase.table().select().eq().single().execute.return_value = Mock(data={"is_admin": False})

        # Make request
        response = client.get("/my/account/admin-status")

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["is_admin"] is False

    def test_check_admin_status_account_not_found(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_user: Mock,
    ):
        """Test checking admin status when account not found."""
        # Setup
        mock_supabase.table().select().eq().single().execute.return_value = Mock(data=None)

        # Make request
        response = client.get("/my/account/admin-status")

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["is_admin"] is False

    def test_setup_account_new_user(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_user: Mock,
    ):
        """Test setting up free tier account for new user."""
        # Setup
        # No existing subscription
        mock_supabase.table().select().eq().execute.return_value = Mock(data=[])

        # Mock insert
        new_subscription = {
            "id": "sub_new_123",
            "account_id": "acc_test_123",
            "tier": "free",
            "status": "active",
            "max_agents": 1,
            "max_messages_per_day": 100,
            "max_storage_gb": 10,
            "created_at": datetime.now(UTC).isoformat(),
        }
        mock_supabase.table().insert().execute.return_value = Mock(data=[new_subscription])

        # Make request
        response = client.post("/my/account/setup")

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert "Free tier account created" in data["message"]
        assert data["account_id"] == "acc_test_123"
        assert data["subscription"]["tier"] == "free"

    def test_setup_account_existing_user(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_user: Mock,
    ):
        """Test setting up account when user already has subscription."""
        # Setup - existing subscription
        mock_supabase.table().select().eq().execute.return_value = Mock(data=[{"id": "sub_existing_123"}])

        # Make request
        response = client.post("/my/account/setup")

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert "Account already setup" in data["message"]
        assert data["account_id"] == "acc_test_123"

    def test_setup_account_rate_limit(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_user: Mock,
    ):
        """Test rate limiting on account setup."""
        # Setup
        mock_supabase.table().select().eq().execute.return_value = Mock(data=[])
        mock_supabase.table().insert().execute.return_value = Mock(
            data=[
                {
                    "id": "sub_123",
                    "account_id": "acc_test_123",
                    "tier": "free",
                    "status": "active",
                    "max_agents": 1,
                    "max_messages_per_day": 100,
                    "max_storage_gb": 10,
                }
            ]
        )

        # Make multiple requests to trigger rate limit
        for i in range(6):
            response = client.post("/my/account/setup")
            if i < 5:
                # First 5 should succeed
                assert response.status_code == 200
            else:
                # 6th should be rate limited
                assert response.status_code == 429
                assert "Rate limit exceeded" in response.json()["error"]

    def test_get_account_with_no_subscriptions(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_user: Mock,
    ):
        """Test getting account with no subscriptions."""
        # Setup
        account_data = {
            "id": "acc_test_123",
            "email": "test@example.com",
            "status": "active",
            "is_admin": False,
            "created_at": datetime.now(UTC).isoformat(),
            "updated_at": datetime.now(UTC).isoformat(),
            "subscriptions": [],
        }
        mock_supabase.table().select().eq().single().execute.return_value = Mock(data=account_data)

        # Make request
        response = client.get("/my/account")

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "acc_test_123"
        assert data["subscriptions"] == []

    def test_get_account_with_multiple_subscriptions(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_user: Mock,
    ):
        """Test getting account with multiple subscriptions (edge case)."""
        # Setup
        account_data = {
            "id": "acc_test_123",
            "email": "test@example.com",
            "status": "active",
            "is_admin": False,
            "created_at": datetime.now(UTC).isoformat(),
            "updated_at": datetime.now(UTC).isoformat(),
            "subscriptions": [
                {
                    "id": "sub_1",
                    "account_id": "acc_test_123",
                    "tier": "starter",
                    "status": "cancelled",
                    "instances": [],
                },
                {
                    "id": "sub_2",
                    "account_id": "acc_test_123",
                    "tier": "professional",
                    "status": "active",
                    "instances": [
                        {"id": "inst_1", "instance_id": "1", "status": "running"},
                        {"id": "inst_2", "instance_id": "2", "status": "stopped"},
                    ],
                },
            ],
        }
        mock_supabase.table().select().eq().single().execute.return_value = Mock(data=account_data)

        # Make request
        response = client.get("/my/account")

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert len(data["subscriptions"]) == 2
        assert data["subscriptions"][1]["status"] == "active"
        assert len(data["subscriptions"][1]["instances"]) == 2

    def test_check_admin_status_missing_field(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_user: Mock,
    ):
        """Test checking admin status when is_admin field is missing."""
        # Setup - data exists but no is_admin field
        mock_supabase.table().select().eq().single().execute.return_value = Mock(data={"id": "acc_test_123"})

        # Make request
        response = client.get("/my/account/admin-status")

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["is_admin"] is False

    def test_setup_account_insert_failure(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_user: Mock,
    ):
        """Test handling database insert failure during account setup."""
        # Setup
        mock_supabase.table().select().eq().execute.return_value = Mock(data=[])

        # Mock insert failure
        mock_supabase.table().insert().execute.return_value = Mock(data=None)

        # Make request
        response = client.post("/my/account/setup")

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["subscription"] is None
