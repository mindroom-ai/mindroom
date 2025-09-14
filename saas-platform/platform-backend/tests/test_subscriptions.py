"""Comprehensive HTTP API tests for subscriptions endpoints."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, Mock, patch

import pytest
from fastapi.testclient import TestClient


class TestSubscriptionsEndpoints:
    """Test subscriptions endpoints via HTTP API."""

    @pytest.fixture
    def client(self) -> TestClient:
        """Create test client."""
        from main import app  # noqa: PLC0415

        return TestClient(app)

    @pytest.fixture
    def mock_supabase(self):
        """Mock Supabase client."""
        with patch("backend.routes.subscriptions.ensure_supabase") as mock:
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

    @pytest.fixture
    def mock_stripe(self):
        """Mock Stripe client."""
        with patch("backend.routes.subscriptions.stripe") as mock:
            yield mock

    def test_get_user_subscription_success(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_user: Mock,
    ):
        """Test getting user's subscription successfully."""
        # Setup
        subscription = {
            "id": "sub_123",
            "account_id": "acc_test_123",
            "tier": "professional",
            "status": "active",
            "stripe_subscription_id": "stripe_sub_123",
            "current_period_start": datetime.now(UTC).isoformat(),
            "current_period_end": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
            "max_agents": 10,
            "max_messages_per_day": 1000,
            "max_storage_gb": 100,
            "created_at": datetime.now(UTC).isoformat(),
            "updated_at": datetime.now(UTC).isoformat(),
        }
        mock_supabase.table().select().eq().limit().execute.return_value = Mock(
            data=[subscription]
        )

        # Make request
        response = client.get("/my/subscription")

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "sub_123"
        assert data["tier"] == "professional"
        assert data["status"] == "active"

    def test_get_user_subscription_not_found(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_user: Mock,
    ):
        """Test getting subscription when user has none - returns free tier."""
        # Setup - no subscription found
        mock_supabase.table().select().eq().limit().execute.return_value = Mock(data=[])

        # Make request
        response = client.get("/my/subscription")

        # Verify - should return free tier
        assert response.status_code == 200
        data = response.json()
        assert data["tier"] == "free"
        assert data["status"] == "active"
        assert data["max_agents"] == 1
        assert data["max_messages_per_day"] == 100

    def test_cancel_subscription_success(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_stripe: Mock,
        mock_verify_user: Mock,
    ):
        """Test canceling subscription successfully."""
        # Setup
        subscription = {
            "id": "sub_123",
            "account_id": "acc_test_123",
            "stripe_subscription_id": "stripe_sub_123",
            "status": "active",
        }
        mock_supabase.table().select().eq().limit().execute.return_value = Mock(
            data=[subscription]
        )

        # Mock Stripe cancellation
        mock_stripe_sub = Mock()
        mock_stripe_sub.status = "canceled"
        mock_stripe_sub.canceled_at = 1700000000
        mock_stripe_sub.id = "stripe_sub_123"
        mock_stripe.Subscription.modify.return_value = mock_stripe_sub

        mock_supabase.table().update().eq().execute.return_value = Mock(
            data=[{"id": "sub_123", "status": "cancelled"}]
        )

        # Make request with valid JSON body
        response = client.post(
            "/my/subscription/cancel", json={"cancel_at_period_end": True}
        )

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "cancelled" in data["message"] or "canceled" in data["message"]

    def test_cancel_subscription_no_subscription(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_user: Mock,
    ):
        """Test canceling when no subscription exists."""
        # Setup
        mock_supabase.table().select().eq().limit().execute.return_value = Mock(data=[])

        # Make request
        response = client.post(
            "/my/subscription/cancel", json={"cancel_at_period_end": True}
        )

        # Verify
        assert response.status_code == 404
        assert "No subscription found" in response.json()["detail"]

    def test_cancel_subscription_already_cancelled(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_user: Mock,
    ):
        """Test canceling already cancelled subscription."""
        # Setup
        subscription = {
            "id": "sub_123",
            "account_id": "acc_test_123",
            "status": "cancelled",
            "stripe_subscription_id": "stripe_sub_123",
        }
        mock_supabase.table().select().eq().limit().execute.return_value = Mock(
            data=[subscription]
        )

        # Make request
        response = client.post(
            "/my/subscription/cancel", json={"cancel_at_period_end": True}
        )

        # Verify
        assert response.status_code == 400
        assert "already cancelled" in response.json()["detail"]

    def test_cancel_subscription_stripe_error(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_stripe: Mock,
        mock_verify_user: Mock,
    ):
        """Test canceling subscription with Stripe error."""
        # Setup
        subscription = {
            "id": "sub_123",
            "account_id": "acc_test_123",
            "stripe_subscription_id": "stripe_sub_123",
            "status": "active",
        }
        mock_supabase.table().select().eq().limit().execute.return_value = Mock(
            data=[subscription]
        )

        # Mock Stripe error
        mock_stripe.Subscription.modify.side_effect = Exception("Stripe error")

        # Make request
        response = client.post(
            "/my/subscription/cancel", json={"cancel_at_period_end": True}
        )

        # Verify
        assert response.status_code == 500
        assert "Failed to cancel" in response.json()["detail"]

    def test_reactivate_subscription_success(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_stripe: Mock,
        mock_verify_user: Mock,
    ):
        """Test reactivating cancelled subscription."""
        # Setup
        subscription = {
            "id": "sub_123",
            "account_id": "acc_test_123",
            "stripe_subscription_id": "stripe_sub_123",
            "status": "cancelled",
        }
        mock_supabase.table().select().eq().limit().execute.return_value = Mock(
            data=[subscription]
        )

        # Mock Stripe reactivation
        mock_stripe_sub = Mock()
        mock_stripe_sub.status = "active"
        mock_stripe_sub.cancel_at_period_end = False
        mock_stripe_sub.id = "stripe_sub_123"  # Set the id attribute properly
        mock_stripe.Subscription.modify.return_value = mock_stripe_sub

        mock_supabase.table().update().eq().execute.return_value = Mock(
            data=[{"id": "sub_123", "status": "active"}]
        )

        # Make request
        response = client.post("/my/subscription/reactivate")

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "reactivated" in data["message"]
        assert data["subscription_id"] == "stripe_sub_123"

    def test_reactivate_subscription_not_cancelled(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_user: Mock,
    ):
        """Test reactivating active subscription."""
        # Setup
        subscription = {
            "id": "sub_123",
            "account_id": "acc_test_123",
            "status": "active",
            "stripe_subscription_id": "stripe_sub_123",
        }
        mock_supabase.table().select().eq().limit().execute.return_value = Mock(
            data=[subscription]
        )

        # Make request
        response = client.post("/my/subscription/reactivate")

        # Verify
        assert response.status_code == 400
        assert "not cancelled" in response.json()["detail"]

    def test_reactivate_no_subscription(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_user: Mock,
    ):
        """Test reactivating when no subscription exists."""
        # Setup - no subscription
        mock_supabase.table().select().eq().limit().execute.return_value = Mock(data=[])

        # Make request
        response = client.post("/my/subscription/reactivate")

        # Verify
        assert response.status_code == 404
        assert "No subscription found" in response.json()["detail"]

    def test_unauthorized_access(self, client: TestClient):
        """Test accessing endpoints without authentication."""
        from main import app  # noqa: PLC0415
        from backend.deps import verify_user
        from fastapi import HTTPException

        def override_verify_user():
            raise HTTPException(status_code=401, detail="Unauthorized")

        app.dependency_overrides[verify_user] = override_verify_user
        try:
            response = client.get("/my/subscription")
            assert response.status_code == 401
        finally:
            app.dependency_overrides.clear()

    def test_subscription_with_trial(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_user: Mock,
    ):
        """Test getting subscription with trial period."""
        # Setup
        trial_end = datetime.now(UTC) + timedelta(days=7)
        subscription = {
            "id": "sub_123",
            "account_id": "acc_test_123",
            "tier": "professional",
            "status": "trialing",
            "trial_ends_at": trial_end.isoformat(),
            "stripe_subscription_id": "stripe_sub_123",
            "max_agents": 10,
            "max_messages_per_day": 1000,
            "max_storage_gb": 100,
            "created_at": datetime.now(UTC).isoformat(),
            "updated_at": datetime.now(UTC).isoformat(),
        }
        mock_supabase.table().select().eq().limit().execute.return_value = Mock(
            data=[subscription]
        )

        # Make request
        response = client.get("/my/subscription")

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "trialing"
        assert data["trial_ends_at"] is not None
