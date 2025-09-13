"""Stripe mock configuration for tests."""

import types
from typing import Any
from unittest.mock import MagicMock


class MockStripeError:
    """Mock Stripe error classes."""

    AuthenticationError = Exception
    APIConnectionError = Exception
    StripeError = Exception


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
                metadata={},
            ),
            types.SimpleNamespace(
                id="price_1S6FvF3GVsrZHuzXDjv76gwE",
                product="prod_test",
                unit_amount=9600,
                recurring=types.SimpleNamespace(interval="year"),
                metadata={},
            ),
            types.SimpleNamespace(
                id="price_1S6FvG3GVsrZHuzXBwljASJB",
                product="prod_test",
                unit_amount=800,
                recurring=types.SimpleNamespace(interval="month"),
                metadata={},
            ),
            types.SimpleNamespace(
                id="price_1S6FvG3GVsrZHuzXQV9y2VEo",
                product="prod_test",
                unit_amount=7680,
                recurring=types.SimpleNamespace(interval="year"),
                metadata={},
            ),
        ]
        mock_response.has_more = False
        mock_response.auto_paging_iter = lambda: mock_response.data
        return mock_response

    @staticmethod
    def retrieve(price_id: str) -> Any:  # noqa: ANN401
        """Mock retrieve method."""
        prices = {
            "price_1S6FvF3GVsrZHuzXrDZ5H7EW": ("prod_test", 1000, "month"),
            "price_1S6FvF3GVsrZHuzXDjv76gwE": ("prod_test", 9600, "year"),
            "price_1S6FvG3GVsrZHuzXBwljASJB": ("prod_test", 800, "month"),
            "price_1S6FvG3GVsrZHuzXQV9y2VEo": ("prod_test", 7680, "year"),
        }
        if price_id in prices:
            product_id, amount, interval = prices[price_id]
            return types.SimpleNamespace(
                id=price_id,
                product=product_id,
                unit_amount=amount,
                recurring=types.SimpleNamespace(interval=interval),
                metadata={},
            )
        msg = f"Price {price_id} not found"
        raise MockStripeError.StripeError(msg)


class MockWebhook:
    """Mock Stripe Webhook."""

    @staticmethod
    def construct_event(payload: bytes, sig_header: str, webhook_secret: str) -> dict[str, Any]:  # noqa: ARG004
        """Mock construct_event method."""
        return {
            "type": "payment_intent.succeeded",
            "data": {"object": {"id": "pi_test_123"}},
        }


def create_stripe_mock() -> types.ModuleType:
    """Create a complete mock Stripe module."""
    mock = types.ModuleType("stripe")
    mock.api_key = "sk_test_mock"
    mock.error = MockStripeError()
    mock.Product = MockProduct()
    mock.Price = MockPrice()
    mock.checkout = types.SimpleNamespace(Session=MockCheckoutSession.Session)
    mock.Webhook = MockWebhook()
    return mock
