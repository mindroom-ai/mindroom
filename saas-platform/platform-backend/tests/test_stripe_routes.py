"""Comprehensive HTTP API tests for Stripe route endpoints."""

from unittest.mock import MagicMock, Mock, patch

import pytest
import stripe
from fastapi.testclient import TestClient


class TestStripeRoutesEndpoints:
    """Test Stripe route endpoints via HTTP API."""

    @pytest.fixture
    def client(self) -> TestClient:
        """Create test client."""
        from main import app  # noqa: PLC0415

        return TestClient(app)

    @pytest.fixture
    def mock_supabase(self):
        """Mock Supabase client."""
        with patch("backend.routes.stripe_routes.ensure_supabase") as mock:
            sb = MagicMock()
            mock.return_value = sb
            yield sb

    @pytest.fixture
    def mock_stripe(self):
        """Mock Stripe client."""
        with patch("backend.routes.stripe_routes.stripe") as mock:
            yield mock

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

    def test_create_checkout_session_success(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_stripe: Mock,
        mock_verify_user: Mock,
    ):
        """Test creating checkout session successfully."""
        # Setup
        mock_supabase.table().select().eq().single().execute.return_value = Mock(
            data={"stripe_customer_id": "cus_test_123"}
        )

        mock_checkout_session = Mock()
        mock_checkout_session.url = "https://checkout.stripe.com/pay/cs_test_123"
        mock_stripe.checkout.Session.create.return_value = mock_checkout_session

        # Make request
        response = client.post(
            "/stripe/checkout",
            json={
                "price_id": "price_test_123",
                "success_url": "https://example.com/success",
                "cancel_url": "https://example.com/cancel",
            },
        )

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["url"] == "https://checkout.stripe.com/pay/cs_test_123"

    def test_create_customer_portal_session_success(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_stripe: Mock,
        mock_verify_user: Mock,
    ):
        """Test creating customer portal session successfully."""
        # Setup
        mock_supabase.table().select().eq().single().execute.return_value = Mock(
            data={"stripe_customer_id": "cus_test_123"}
        )

        mock_portal_session = Mock()
        mock_portal_session.url = "https://billing.stripe.com/session/test_123"
        mock_stripe.billing_portal.Session.create.return_value = mock_portal_session

        # Make request
        response = client.post(
            "/stripe/portal", json={"return_url": "https://example.com/account"}
        )

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["url"] == "https://billing.stripe.com/session/test_123"

    def test_create_customer_portal_no_customer(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_user: Mock,
    ):
        """Test creating portal session when no Stripe customer exists."""
        # Setup
        mock_supabase.table().select().eq().single().execute.return_value = Mock(
            data={"stripe_customer_id": None}
        )

        # Make request
        response = client.post(
            "/stripe/portal", json={"return_url": "https://example.com/account"}
        )

        # Verify
        assert response.status_code == 404
        assert "No Stripe customer" in response.json()["detail"]

    def test_create_customer_portal_stripe_error(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_stripe: Mock,
        mock_verify_user: Mock,
    ):
        """Test handling Stripe error when creating portal session."""
        # Setup
        mock_supabase.table().select().eq().single().execute.return_value = Mock(
            data={"stripe_customer_id": "cus_test_123"}
        )

        mock_stripe.billing_portal.Session.create.side_effect = (
            stripe.error.StripeError("Portal error")
        )

        # Make request
        response = client.post(
            "/stripe/portal", json={"return_url": "https://example.com/account"}
        )

        # Verify
        assert response.status_code == 500
        assert "Failed to create" in response.json()["detail"]

    def test_create_checkout_new_customer(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_stripe: Mock,
        mock_verify_user: Mock,
    ):
        """Test creating checkout for new customer."""
        # Setup - no existing customer
        mock_supabase.table().select().eq().single().execute.return_value = Mock(
            data={"stripe_customer_id": None}
        )

        # Mock customer creation
        mock_customer = Mock()
        mock_customer.id = "cus_new_123"
        mock_stripe.Customer.create.return_value = mock_customer

        # Mock checkout session
        mock_checkout_session = Mock()
        mock_checkout_session.url = "https://checkout.stripe.com/pay/cs_test_123"
        mock_stripe.checkout.Session.create.return_value = mock_checkout_session

        # Mock update
        mock_supabase.table().update().eq().execute.return_value = Mock()

        # Make request
        response = client.post(
            "/stripe/checkout",
            json={
                "price_id": "price_test_123",
                "success_url": "https://example.com/success",
                "cancel_url": "https://example.com/cancel",
            },
        )

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["url"] == "https://checkout.stripe.com/pay/cs_test_123"

    def test_checkout_with_quantity(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_stripe: Mock,
        mock_verify_user: Mock,
    ):
        """Test creating checkout with quantity for professional plan."""
        # Setup
        mock_supabase.table().select().eq().single().execute.return_value = Mock(
            data={"stripe_customer_id": "cus_test_123"}
        )

        mock_checkout_session = Mock()
        mock_checkout_session.url = "https://checkout.stripe.com/pay/cs_test_123"
        mock_stripe.checkout.Session.create.return_value = mock_checkout_session

        # Make request with quantity
        response = client.post(
            "/stripe/checkout",
            json={
                "price_id": "price_professional_monthly",
                "quantity": 5,
                "success_url": "https://example.com/success",
                "cancel_url": "https://example.com/cancel",
            },
        )

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["url"] == "https://checkout.stripe.com/pay/cs_test_123"

    def test_checkout_stripe_error(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_stripe: Mock,
        mock_verify_user: Mock,
    ):
        """Test handling Stripe error during checkout."""
        # Setup
        mock_supabase.table().select().eq().single().execute.return_value = Mock(
            data={"stripe_customer_id": "cus_test_123"}
        )

        mock_stripe.checkout.Session.create.side_effect = stripe.error.StripeError(
            "Checkout error"
        )

        # Make request
        response = client.post(
            "/stripe/checkout",
            json={
                "price_id": "price_test_123",
                "success_url": "https://example.com/success",
                "cancel_url": "https://example.com/cancel",
            },
        )

        # Verify
        assert response.status_code == 500
        assert "Failed to create checkout" in response.json()["detail"]

    def test_portal_account_not_found(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_user: Mock,
    ):
        """Test portal when account not found."""
        # Setup
        mock_supabase.table().select().eq().single().execute.return_value = Mock(
            data=None
        )

        # Make request
        response = client.post(
            "/stripe/portal", json={"return_url": "https://example.com/account"}
        )

        # Verify
        assert response.status_code == 404
        assert "Account not found" in response.json()["detail"]

    def test_unauthorized_access(self, client: TestClient):
        """Test accessing endpoints without authentication."""
        from main import app  # noqa: PLC0415
        from backend.deps import verify_user
        from fastapi import HTTPException

        def override_verify_user():
            raise HTTPException(status_code=401, detail="Unauthorized")

        app.dependency_overrides[verify_user] = override_verify_user
        try:
            response = client.post(
                "/stripe/checkout",
                json={
                    "price_id": "price_test_123",
                    "success_url": "https://example.com/success",
                    "cancel_url": "https://example.com/cancel",
                },
            )
            assert response.status_code == 401
        finally:
            app.dependency_overrides.clear()
