"""Stripe mock configuration for tests."""

import types
from typing import Any
from unittest.mock import MagicMock


class MockStripeError:
    """Mock Stripe error classes."""

    AuthenticationError = Exception
    APIConnectionError = Exception
    StripeError = Exception
    InvalidRequestError = Exception


class MockCheckoutSession:
    """Mock Stripe Checkout Session."""

    class Session:
        """Mock Session class."""

        @staticmethod
        def create(**kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
            """Mock create method."""
            return {
                "id": "cs_test_123",
                "url": "https://checkout.stripe.com/test",
                "payment_status": "unpaid",
                "metadata": kwargs.get("metadata", {}),
            }


class MockProduct:
    """Mock Stripe Product."""

    @staticmethod
    def list(**kwargs: Any) -> Any:  # noqa: ANN401, ARG004
        """Mock list method."""
        mock_response = MagicMock()
        mock_response.data = [
            types.SimpleNamespace(
                id="prod_test",
                name="MindRoom Subscription",
                metadata={"platform": "mindroom"},
                active=True,
            ),
        ]
        return mock_response

    @staticmethod
    def retrieve(product_id: str) -> Any:  # noqa: ANN401
        """Mock retrieve method."""
        return types.SimpleNamespace(
            id=product_id,
            name="MindRoom Subscription",
            metadata={"platform": "mindroom"},
        )


class MockPrice:
    """Mock Stripe Price."""

    @staticmethod
    def list(**kwargs: Any) -> Any:  # noqa: ANN401, ARG004
        """Mock list method."""
        mock_response = MagicMock()
        mock_response.data = [
            types.SimpleNamespace(
                id="price_1S6FvF3GVsrZHuzXrDZ5H7EW",
                product="prod_test",
                unit_amount=1000,
                recurring=types.SimpleNamespace(interval="month"),
                metadata={"plan": "starter", "billing_cycle": "monthly"},
                active=True,
            ),
            types.SimpleNamespace(
                id="price_1S6FvF3GVsrZHuzXDjv76gwE",
                product="prod_test",
                unit_amount=9600,
                recurring=types.SimpleNamespace(interval="year"),
                metadata={"plan": "starter", "billing_cycle": "yearly"},
                active=True,
            ),
            types.SimpleNamespace(
                id="price_1S6FvG3GVsrZHuzXBwljASJB",
                product="prod_test",
                unit_amount=800,
                recurring=types.SimpleNamespace(interval="month"),
                metadata={"plan": "professional", "billing_cycle": "monthly"},
                active=True,
            ),
            types.SimpleNamespace(
                id="price_1S6FvG3GVsrZHuzXQV9y2VEo",
                product="prod_test",
                unit_amount=7680,
                recurring=types.SimpleNamespace(interval="year"),
                metadata={"plan": "professional", "billing_cycle": "yearly"},
                active=True,
            ),
        ]
        mock_response.has_more = False
        mock_response.auto_paging_iter = lambda: mock_response.data
        return mock_response

    @staticmethod
    def retrieve(price_id: str) -> Any:  # noqa: ANN401
        """Mock retrieve method."""
        prices = {
            "price_1S6FvF3GVsrZHuzXrDZ5H7EW": {
                "product": "prod_test",
                "amount": 1000,
                "interval": "month",
                "interval_count": 1,
                "plan": "starter",
                "billing_cycle": "monthly",
            },
            "price_1S6FvF3GVsrZHuzXDjv76gwE": {
                "product": "prod_test",
                "amount": 9600,
                "interval": "year",
                "interval_count": 1,
                "plan": "starter",
                "billing_cycle": "yearly",
            },
            "price_1S6FvG3GVsrZHuzXBwljASJB": {
                "product": "prod_test",
                "amount": 800,
                "interval": "month",
                "interval_count": 1,
                "plan": "professional",
                "billing_cycle": "monthly",
            },
            "price_1S6FvG3GVsrZHuzXQV9y2VEo": {
                "product": "prod_test",
                "amount": 7680,
                "interval": "month",
                "interval_count": 12,
                "plan": "professional",
                "billing_cycle": "yearly",
            },
        }
        if price_id in prices:
            data = prices[price_id]
            return types.SimpleNamespace(
                id=price_id,
                product=data["product"],
                unit_amount=data["amount"],
                recurring=types.SimpleNamespace(
                    interval=data["interval"],
                    interval_count=data.get("interval_count", 1),
                ),
                metadata={"plan": data["plan"], "billing_cycle": data["billing_cycle"]},
                active=True,
            )
        # Raise InvalidRequestError for unknown price IDs
        msg = f"No such price: {price_id}"
        raise MockStripeError.InvalidRequestError(msg)


class MockWebhook:
    """Mock Stripe Webhook."""

    @staticmethod
    def construct_event(payload: bytes, sig_header: str, webhook_secret: str) -> dict[str, Any]:  # noqa: ARG004
        """Mock construct_event method."""
        return {
            "type": "payment_intent.succeeded",
            "data": {"object": {"id": "pi_test_123"}},
        }


class MockCustomer:
    """Mock Stripe Customer."""

    @staticmethod
    def create(**kwargs: Any) -> Any:  # noqa: ANN401
        """Mock create method."""
        return types.SimpleNamespace(
            id="cus_test_123",
            email=kwargs.get("email", "test@example.com"),
            metadata=kwargs.get("metadata", {}),
        )


class MockSubscription:
    """Mock Stripe Subscription."""

    @staticmethod
    def list(**kwargs: Any) -> Any:  # noqa: ANN401, ARG004
        """Mock list method."""
        mock_response = MagicMock()
        mock_response.data = []  # No existing subscriptions
        return mock_response


class MockBillingPortalSession:
    """Mock Stripe Billing Portal Session."""

    @staticmethod
    def create(**kwargs: Any) -> Any:  # noqa: ANN401, ARG004
        """Mock create method."""
        return types.SimpleNamespace(
            url="https://billing.stripe.com/test_portal",
        )


def create_stripe_mock() -> types.ModuleType:
    """Create a complete mock Stripe module."""
    mock = types.ModuleType("stripe")
    mock.api_key = "sk_test_mock"
    mock.error = MockStripeError()
    mock.Product = MockProduct()
    mock.Price = MockPrice()
    mock.checkout = types.SimpleNamespace(Session=MockCheckoutSession.Session)
    mock.Webhook = MockWebhook()
    mock.Customer = MockCustomer()
    mock.Subscription = MockSubscription()
    mock.billing_portal = types.SimpleNamespace(Session=MockBillingPortalSession())
    return mock
