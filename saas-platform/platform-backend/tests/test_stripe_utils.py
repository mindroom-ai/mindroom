"""Utility tests for Stripe debugging and validation."""

import os
from pathlib import Path

import pytest
import stripe
import yaml
from dotenv import load_dotenv

# Load environment variables from saas-platform/.env
env_path = Path(__file__).parent.parent.parent / ".env"
if env_path.exists():
    load_dotenv(env_path)

# Check if we have real Stripe credentials
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
HAS_STRIPE_CREDENTIALS = bool(STRIPE_SECRET_KEY and STRIPE_SECRET_KEY.startswith("sk_test_"))


# Tests will use mocked or real Stripe depending on credentials
class TestStripeDebugUtils:
    """Utility tests for debugging Stripe integration."""

    @pytest.fixture(autouse=True)
    def setup(self) -> None:
        """Set up Stripe API key."""
        if HAS_STRIPE_CREDENTIALS:
            stripe.api_key = STRIPE_SECRET_KEY
        else:
            # Use mock key for tests
            stripe.api_key = "sk_test_mock"

    @pytest.mark.skipif(not HAS_STRIPE_CREDENTIALS, reason="Requires real Stripe API credentials")
    def test_mindroom_product_configuration(self) -> None:
        """Test that MindRoom products are properly configured in Stripe."""
        products = stripe.Product.list(limit=100)

        # Find MindRoom products
        mindroom_products = [p for p in products.data if p.metadata.get("platform") == "mindroom"]

        # Should have at least one MindRoom product
        assert len(mindroom_products) > 0, "No MindRoom products found in Stripe"

        # Verify the product is properly configured
        product = mindroom_products[0]
        assert product.name == "MindRoom Subscription", f"Product name mismatch: {product.name}"
        assert product.active, "MindRoom product is not active"
        assert product.metadata.get("platform") == "mindroom", "Missing platform metadata"

    def _check_price(self, plan_name: str, price_id: str | None, yaml_price: int, billing_cycle: str) -> str | None:
        """Check a single price against Stripe. Returns error message if mismatch."""
        if not price_id:
            return None  # No ID configured is OK for free/enterprise plans

        try:
            stripe_price = stripe.Price.retrieve(price_id)
            if stripe_price.unit_amount != yaml_price:
                return f"{plan_name} {billing_cycle}: YAML=${yaml_price / 100:.2f} vs Stripe=${stripe_price.unit_amount / 100:.2f}"
        except stripe.error.InvalidRequestError:
            return f"{plan_name} {billing_cycle}: Price ID {price_id} not found in Stripe"

        return None

    @pytest.mark.skipif(not HAS_STRIPE_CREDENTIALS, reason="Requires real Stripe API credentials")
    def test_yaml_prices_match_stripe(self) -> None:
        """Test that YAML configuration matches actual Stripe prices."""
        # Load YAML config
        config_path = Path(__file__).parent.parent.parent / "pricing-config.yaml"
        with config_path.open() as f:
            yaml_config = yaml.safe_load(f)

        errors = []

        for plan_key, plan in yaml_config["plans"].items():
            if plan_key not in ["starter", "professional"]:
                continue  # Only check paid plans with Stripe IDs

            # Check monthly price
            if error := self._check_price(
                plan["name"],
                plan.get("stripe_price_id_monthly"),
                plan["price_monthly"],
                "monthly",
            ):
                errors.append(error)
            elif plan_key not in ["free", "enterprise"] and not plan.get("stripe_price_id_monthly"):
                errors.append(f"{plan['name']} monthly: No Stripe ID configured")

            # Check yearly price
            if error := self._check_price(
                plan["name"],
                plan.get("stripe_price_id_yearly"),
                plan["price_yearly"],
                "yearly",
            ):
                errors.append(error)
            elif plan_key not in ["free", "enterprise"] and not plan.get("stripe_price_id_yearly"):
                errors.append(f"{plan['name']} yearly: No Stripe ID configured")

        assert len(errors) == 0, "Price mismatches found:\n" + "\n".join(errors)
