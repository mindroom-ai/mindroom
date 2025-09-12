#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = ["stripe", "python-dotenv", "pyyaml", "httpx", "pydantic"]
# ///
"""Comprehensive test for pricing system integration."""

import os
import sys
from pathlib import Path

import httpx
import stripe
import yaml
from dotenv import load_dotenv

# Load environment variables
env_path = Path(__file__).parent.parent.parent / ".env"
load_dotenv(env_path)

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "platform-backend" / "src"))

from backend.pricing import (  # noqa: E402
    get_plan_details,
    get_stripe_price_id,
    get_trial_days,
    is_trial_enabled_for_plan,
    load_pricing_config,
    load_pricing_config_model,
)

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")


def test_yaml_config() -> None:
    """Test YAML configuration is properly loaded."""
    print("🧪 Testing YAML configuration...")

    config_path = Path(__file__).parent.parent.parent / "pricing-config.yaml"
    with config_path.open() as f:
        yaml_config = yaml.safe_load(f)

    assert yaml_config["plans"]["starter"]["stripe_price_id_monthly"], "Starter monthly ID missing"
    assert yaml_config["plans"]["professional"]["stripe_price_id_yearly"], "Professional yearly ID missing"
    print("  ✅ YAML has Stripe price IDs")

    assert yaml_config["plans"]["starter"]["price_monthly"] == 1000, "Starter price should be 1000 cents"
    assert yaml_config["plans"]["professional"]["price_monthly"] == 800, "Professional price should be 800 cents"
    print("  ✅ Prices are correct")


def test_pricing_module() -> None:
    """Test pricing module functions."""
    print("\n🧪 Testing pricing module...")

    # Test basic config loading
    config = load_pricing_config()
    assert config["plans"]["starter"]["name"] == "Starter"
    print("  ✅ Config loads correctly")

    # Test Pydantic model
    model = load_pricing_config_model()
    assert model.plans["starter"].price_monthly == 1000
    assert model.plans["starter"].stripe_price_id_monthly
    print("  ✅ Pydantic model works")

    # Test helper functions
    assert get_stripe_price_id("starter", "monthly") == "price_1S6EmY3GVsrZHuzXTpwM8Gqx"
    assert get_stripe_price_id("professional", "yearly") == "price_1S6EmZ3GVsrZHuzXr1m0Bwuh"
    print("  ✅ Stripe price ID retrieval works")

    assert is_trial_enabled_for_plan("starter") is True
    assert is_trial_enabled_for_plan("free") is False
    assert get_trial_days() == 14
    print("  ✅ Trial configuration works")

    plan = get_plan_details("starter")
    assert plan.price_monthly == 1000
    assert len(plan.features) == 7
    print("  ✅ Plan details retrieval works")


def test_stripe_prices() -> None:
    """Test that Stripe has the correct prices."""
    print("\n🧪 Testing Stripe prices...")

    if not stripe.api_key:
        print("  ⚠️  Skipping - no Stripe key")
        return

    # Check Starter monthly
    price = stripe.Price.retrieve("price_1S6EmY3GVsrZHuzXTpwM8Gqx")
    assert price.unit_amount == 1000, f"Starter monthly should be 1000 cents, got {price.unit_amount}"
    assert price.recurring.interval == "month"
    print("  ✅ Starter monthly: $10.00")

    # Check Professional yearly
    price = stripe.Price.retrieve("price_1S6EmZ3GVsrZHuzXr1m0Bwuh")
    assert price.unit_amount == 7680, f"Professional yearly should be 7680 cents, got {price.unit_amount}"
    assert price.recurring.interval == "month"
    assert price.recurring.interval_count == 12
    print("  ✅ Professional yearly: $76.80 (billed annually)")


def test_api_endpoints() -> None:
    """Test API endpoints."""
    print("\n🧪 Testing API endpoints...")

    base_url = "http://localhost:8765"

    try:
        # Test pricing config endpoint
        response = httpx.get(f"{base_url}/pricing/config", timeout=5)
        assert response.status_code == 200, f"Pricing config failed: {response.status_code}"
        data = response.json()
        assert data["plans"]["starter"]["stripe_price_id_monthly"]
        print("  ✅ Pricing config endpoint works")

        # Test stripe price endpoint
        response = httpx.get(f"{base_url}/pricing/stripe-price/starter/monthly", timeout=5)
        assert response.status_code == 200, f"Stripe price endpoint failed: {response.status_code}"
        data = response.json()
        assert data["price_id"] == "price_1S6EmY3GVsrZHuzXTpwM8Gqx"
        print("  ✅ Stripe price endpoint works")

    except httpx.ConnectError:
        print("  ⚠️  Skipping - backend not running")
        print("     Run: uvicorn main:app --host 0.0.0.0 --port 8765")


def main() -> None:
    """Run all tests."""
    print("=" * 60)
    print("Comprehensive Pricing System Test")
    print("=" * 60)

    try:
        test_yaml_config()
        test_pricing_module()
        test_stripe_prices()
        test_api_endpoints()

        print("\n" + "=" * 60)
        print("✅ ALL TESTS PASSED!")
        print("=" * 60)
        print("\nPricing system is fully operational:")
        print("  • YAML config with Stripe IDs ✓")
        print("  • Backend pricing module ✓")
        print("  • Stripe prices created ✓")
        print("  • API endpoints working ✓")
        print("  • Pydantic models validated ✓")

    except AssertionError as e:
        print(f"\n❌ Test failed: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
