"""Utility tests for Stripe debugging and validation."""

import os
from pathlib import Path

import pytest
import stripe
import yaml


@pytest.mark.skipif(
    not os.getenv("STRIPE_SECRET_KEY"),
    reason="STRIPE_SECRET_KEY not set",
)
class TestStripeDebugUtils:
    """Utility tests for debugging Stripe integration."""

    @pytest.fixture(autouse=True)
    def setup(self) -> None:
        """Set up Stripe API key."""
        stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

    def test_list_all_products(self) -> None:
        """List all Stripe products for debugging."""
        products = stripe.Product.list(limit=100)

        print("\n📦 All Stripe Products:")
        for product in products.data:
            print(f"  - {product.name} (ID: {product.id})")
            if product.metadata:
                print(f"    Metadata: {product.metadata}")

        assert len(products.data) >= 0  # Just ensure we can list products

    def test_compare_yaml_to_stripe(self) -> None:
        """Compare YAML configuration to actual Stripe prices."""
        # Load YAML config
        config_path = Path(__file__).parent.parent.parent / "pricing-config.yaml"
        with config_path.open() as f:
            yaml_config = yaml.safe_load(f)

        print("\n🔍 Pricing Comparison (YAML vs Stripe):")

        for plan_key, plan in yaml_config["plans"].items():
            if plan_key in ["starter", "professional"]:
                print(f"\n{plan['name']} Plan:")

                # Check monthly price
                monthly_id = plan.get("stripe_price_id_monthly")
                if monthly_id:
                    try:
                        stripe_price = stripe.Price.retrieve(monthly_id)
                        yaml_price = plan["price_monthly"]

                        match = "✅" if stripe_price.unit_amount == yaml_price else "❌"
                        print(
                            f"  Monthly: YAML=${yaml_price / 100:.2f} vs Stripe=${stripe_price.unit_amount / 100:.2f} {match}",
                        )
                    except stripe.error.InvalidRequestError:
                        print(f"  Monthly: ❌ Price ID {monthly_id} not found in Stripe")
                else:
                    print("  Monthly: ⚠️  No Stripe ID configured")

                # Check yearly price
                yearly_id = plan.get("stripe_price_id_yearly")
                if yearly_id:
                    try:
                        stripe_price = stripe.Price.retrieve(yearly_id)
                        yaml_price = plan["price_yearly"]

                        # For yearly prices, both plans use the same logic
                        expected = yaml_price

                        match = "✅" if stripe_price.unit_amount == expected else "❌"
                        print(
                            f"  Yearly: YAML=${yaml_price / 100:.2f} vs Stripe=${stripe_price.unit_amount / 100:.2f} {match}",
                        )
                    except stripe.error.InvalidRequestError:
                        print(f"  Yearly: ❌ Price ID {yearly_id} not found in Stripe")
                else:
                    print("  Yearly: ⚠️  No Stripe ID configured")

    def test_find_orphaned_prices(self) -> None:
        """Find Stripe prices that aren't in our configuration."""
        # Load YAML config
        config_path = Path(__file__).parent.parent.parent / "pricing-config.yaml"
        with config_path.open() as f:
            yaml_config = yaml.safe_load(f)

        # Collect all configured price IDs
        configured_ids = set()
        for plan in yaml_config["plans"].values():
            if monthly_id := plan.get("stripe_price_id_monthly"):
                configured_ids.add(monthly_id)
            if yearly_id := plan.get("stripe_price_id_yearly"):
                configured_ids.add(yearly_id)

        # Find MindRoom prices in Stripe
        products = stripe.Product.list(limit=100)
        mindroom_products = [p for p in products.data if p.metadata.get("platform") == "mindroom"]

        if mindroom_products:
            print("\n🔍 Checking for orphaned prices:")
            for product in mindroom_products:
                prices = stripe.Price.list(product=product.id, limit=100)
                for price in prices.data:
                    if price.id not in configured_ids:
                        print(f"  ⚠️  Orphaned price: {price.id}")
                        print(f"     Amount: ${price.unit_amount / 100:.2f}")
                        print(f"     Metadata: {price.metadata}")

        assert True  # This is just a utility test
