"""Comprehensive HTTP API tests for Stripe route endpoints."""

from unittest.mock import MagicMock, Mock, patch

import pytest
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

        # Mock pricing functions
        with (
            patch(
                "backend.routes.stripe_routes.get_stripe_price_id",
                return_value="price_test_123",
            ),
            patch(
                "backend.routes.stripe_routes.is_trial_enabled_for_plan",
                return_value=False,
            ),
        ):
            mock_checkout_session = Mock()
            mock_checkout_session.url = "https://checkout.stripe.com/pay/cs_test_123"
            mock_stripe.checkout.Session.create.return_value = mock_checkout_session

            # Make request
            response = client.post(
                "/stripe/checkout",
                json={
                    "tier": "starter",
                    "billing_cycle": "monthly",
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
        response = client.post("/stripe/portal")

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
        mock_supabase.table().select().eq().single().execute.return_value = Mock(data={"stripe_customer_id": None})

        # Make request
        response = client.post("/stripe/portal")

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

        # Wrap the side_effect in a try-except in the route
        # The route should catch this Exception and return 500 with proper message
        mock_stripe.billing_portal.Session.create.side_effect = Exception("Portal error")

        # Make request
        response = client.post("/stripe/portal")

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
        mock_supabase.table().select().eq().single().execute.return_value = Mock(data={"stripe_customer_id": None})

        # Mock pricing functions
        with (
            patch(
                "backend.routes.stripe_routes.get_stripe_price_id",
                return_value="price_test_123",
            ),
            patch(
                "backend.routes.stripe_routes.is_trial_enabled_for_plan",
                return_value=False,
            ),
        ):
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
                    "tier": "starter",
                    "billing_cycle": "monthly",
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

        # Mock pricing functions
        with (
            patch(
                "backend.routes.stripe_routes.get_stripe_price_id",
                return_value="price_test_123",
            ),
            patch(
                "backend.routes.stripe_routes.is_trial_enabled_for_plan",
                return_value=False,
            ),
        ):
            mock_checkout_session = Mock()
            mock_checkout_session.url = "https://checkout.stripe.com/pay/cs_test_123"
            mock_stripe.checkout.Session.create.return_value = mock_checkout_session

            # Make request with quantity
            response = client.post(
                "/stripe/checkout",
                json={
                    "tier": "professional",
                    "billing_cycle": "monthly",
                    "quantity": 5,
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

        # Mock pricing functions
        with (
            patch(
                "backend.routes.stripe_routes.get_stripe_price_id",
                return_value="price_test_123",
            ),
            patch(
                "backend.routes.stripe_routes.is_trial_enabled_for_plan",
                return_value=False,
            ),
        ):
            mock_stripe.checkout.Session.create.side_effect = Exception("Checkout error")

            # Make request
            response = client.post(
                "/stripe/checkout",
                json={
                    "tier": "starter",
                    "billing_cycle": "monthly",
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
        mock_supabase.table().select().eq().single().execute.return_value = Mock(data=None)

        # Make request
        response = client.post("/stripe/portal")

        # Verify - the route actually returns "No Stripe customer found"
        assert response.status_code == 404
        assert "No Stripe customer" in response.json()["detail"]

    def test_unauthorized_access(self, client: TestClient):
        """Test accessing endpoints without authentication."""
        from main import app  # noqa: PLC0415
        from backend.deps import verify_user_optional

        # Mock pricing functions, user dependency, and Stripe
        with (
            patch(
                "backend.routes.stripe_routes.get_stripe_price_id",
                return_value="price_test_123",
            ),
            patch(
                "backend.routes.stripe_routes.is_trial_enabled_for_plan",
                return_value=False,
            ),
            patch("backend.routes.stripe_routes.stripe") as mock_stripe,
        ):
            # Mock the checkout session creation
            mock_checkout_session = Mock()
            mock_checkout_session.url = "https://checkout.stripe.com/pay/cs_test_123"
            mock_stripe.checkout.Session.create.return_value = mock_checkout_session

            def override_verify_user_optional():
                return None  # Return None to simulate no user

            app.dependency_overrides[verify_user_optional] = override_verify_user_optional
            try:
                # When there's no authenticated user, checkout should still work
                # as stripe_routes allows optional user for checkout
                response = client.post(
                    "/stripe/checkout",
                    json={
                        "tier": "starter",
                        "billing_cycle": "monthly",
                    },
                )
                # The route actually allows unauthenticated access
                assert response.status_code == 200
                assert response.json()["url"] == "https://checkout.stripe.com/pay/cs_test_123"
            finally:
                app.dependency_overrides.clear()
