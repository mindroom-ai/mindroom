"""Integration tests for Stripe functionality."""

import os
from unittest.mock import Mock, patch

import pytest
import stripe
from backend.pricing import get_stripe_price_id, load_pricing_config_model
from fastapi.testclient import TestClient

# Skip these tests if no Stripe key is available
pytestmark = pytest.mark.skipif(
    not os.getenv("STRIPE_SECRET_KEY"),
    reason="STRIPE_SECRET_KEY not set",
)


class TestStripeIntegration:
    """Test Stripe API integration."""

    @pytest.fixture(autouse=True)
    def setup(self) -> None:
        """Set up Stripe API key."""
        stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

    def test_stripe_connection(self) -> None:
        """Test that we can connect to Stripe."""
        try:
            products = stripe.Product.list(limit=1)
            assert products is not None
        except stripe.error.AuthenticationError:
            pytest.fail("Failed to authenticate with Stripe")

    def test_mindroom_product_exists(self) -> None:
        """Test that MindRoom product exists in Stripe."""
        products = stripe.Product.list(limit=100)

        mindroom_products = [p for p in products.data if p.metadata.get("platform") == "mindroom"]

        assert len(mindroom_products) > 0, "No MindRoom product found in Stripe"

        # Verify product details
        product = mindroom_products[0]
        assert product.name == "MindRoom Subscription"
        assert product.metadata.get("platform") == "mindroom"

    def test_stripe_prices_match_config(self) -> None:
        """Test that Stripe prices match our configuration."""
        config = load_pricing_config_model()

        # Test Starter plan prices
        starter_monthly_id = config.plans["starter"].stripe_price_id_monthly
        if starter_monthly_id:
            price = stripe.Price.retrieve(starter_monthly_id)
            assert price.unit_amount == 1000  # $10.00
            assert price.recurring.interval == "month"
            assert price.metadata.get("plan") == "starter"
            assert price.metadata.get("billing_cycle") == "monthly"

        # Test Professional plan prices
        professional_yearly_id = config.plans["professional"].stripe_price_id_yearly
        if professional_yearly_id:
            price = stripe.Price.retrieve(professional_yearly_id)
            # Professional yearly is $76.80/month billed annually
            assert price.unit_amount == 7680  # $76.80
            assert price.recurring.interval == "month"
            assert price.recurring.interval_count == 12
            assert price.metadata.get("plan") == "professional"
            assert price.metadata.get("billing_cycle") == "yearly"

    def test_all_configured_prices_exist(self) -> None:
        """Test that all configured Stripe price IDs actually exist."""
        config = load_pricing_config_model()

        for plan_name, plan in config.plans.items():
            if plan.stripe_price_id_monthly:
                try:
                    price = stripe.Price.retrieve(plan.stripe_price_id_monthly)
                    assert price.active, f"{plan_name} monthly price is not active"
                except stripe.error.InvalidRequestError:
                    pytest.fail(f"{plan_name} monthly price ID {plan.stripe_price_id_monthly} not found")

            if plan.stripe_price_id_yearly:
                try:
                    price = stripe.Price.retrieve(plan.stripe_price_id_yearly)
                    assert price.active, f"{plan_name} yearly price is not active"
                except stripe.error.InvalidRequestError:
                    pytest.fail(f"{plan_name} yearly price ID {plan.stripe_price_id_yearly} not found")


class TestCheckoutEndpoint:
    """Test Stripe checkout endpoint."""

    @pytest.fixture
    def client(self) -> TestClient:
        """Create test client."""
        from main import app  # noqa: PLC0415

        return TestClient(app)

    @pytest.fixture(autouse=True)
    def setup(self) -> None:
        """Set up Stripe API key."""
        stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

    def test_checkout_creates_session(self, client: TestClient) -> None:
        """Test that checkout endpoint creates a Stripe session."""
        # Mock the Stripe checkout session creation
        with patch("stripe.checkout.Session.create") as mock_create:
            mock_session = Mock()
            mock_session.url = "https://checkout.stripe.com/test_session"
            mock_create.return_value = mock_session

            response = client.post(
                "/stripe/checkout",
                json={"tier": "starter", "billing_cycle": "monthly"},
            )

            assert response.status_code == 200
            data = response.json()
            assert "url" in data
            assert data["url"] == "https://checkout.stripe.com/test_session"

            # Verify the session was created with correct parameters
            mock_create.assert_called_once()
            call_args = mock_create.call_args[1]
            assert call_args["mode"] == "subscription"
            assert len(call_args["line_items"]) == 1
            assert call_args["line_items"][0]["price"] == get_stripe_price_id("starter", "monthly")

    def test_checkout_invalid_plan(self, client: TestClient) -> None:
        """Test checkout with invalid plan."""
        response = client.post(
            "/stripe/checkout",
            json={"tier": "invalid_plan", "billing_cycle": "monthly"},
        )

        assert response.status_code == 400
        assert "Invalid tier" in response.json()["detail"]

    def test_checkout_invalid_billing_cycle(self, client: TestClient) -> None:
        """Test checkout with invalid billing cycle."""
        response = client.post(
            "/stripe/checkout",
            json={"tier": "starter", "billing_cycle": "weekly"},
        )

        assert response.status_code == 400
        assert "Invalid billing cycle" in response.json()["detail"]

    def test_checkout_professional_with_quantity(self, client: TestClient) -> None:
        """Test checkout for professional plan with quantity."""
        with patch("stripe.checkout.Session.create") as mock_create:
            mock_session = Mock()
            mock_session.url = "https://checkout.stripe.com/test_session"
            mock_create.return_value = mock_session

            response = client.post(
                "/stripe/checkout",
                json={
                    "tier": "professional",
                    "billing_cycle": "yearly",
                    "quantity": 5,
                },
            )

            assert response.status_code == 200

            # Verify quantity was passed for per-user pricing
            mock_create.assert_called_once()
            call_args = mock_create.call_args[1]
            assert call_args["line_items"][0]["quantity"] == 5


class TestPricingEndpoints:
    """Test pricing API endpoints."""

    @pytest.fixture
    def client(self) -> TestClient:
        """Create test client."""
        from main import app  # noqa: PLC0415

        return TestClient(app)

    def test_pricing_config_endpoint(self, client: TestClient) -> None:
        """Test the pricing config endpoint."""
        response = client.get("/pricing/config")

        assert response.status_code == 200
        data = response.json()

        # Check structure
        assert "plans" in data
        assert "product" in data
        assert "trial" in data
        assert "discounts" in data

        # Check that Stripe IDs are included
        assert data["plans"]["starter"]["stripe_price_id_monthly"] is not None
        assert data["plans"]["professional"]["stripe_price_id_yearly"] is not None

    def test_stripe_price_endpoint(self, client: TestClient) -> None:
        """Test the Stripe price retrieval endpoint."""
        response = client.get("/pricing/stripe-price/starter/monthly")

        assert response.status_code == 200
        data = response.json()

        assert "price_id" in data
        assert data["price_id"] == get_stripe_price_id("starter", "monthly")
        assert data["plan"] == "starter"
        assert data["billing_cycle"] == "monthly"

    def test_stripe_price_endpoint_invalid(self, client: TestClient) -> None:
        """Test Stripe price endpoint with invalid parameters."""
        # Invalid plan
        response = client.get("/pricing/stripe-price/invalid/monthly")
        assert response.status_code == 404

        # Invalid billing cycle
        response = client.get("/pricing/stripe-price/starter/weekly")
        assert response.status_code == 400
