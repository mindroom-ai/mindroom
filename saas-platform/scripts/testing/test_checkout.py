#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx", "python-dotenv"]
# ///
"""Test script for Stripe checkout integration."""

from pathlib import Path

import httpx
from dotenv import load_dotenv

# Load environment variables
env_path = Path(__file__).parent.parent.parent / ".env"
load_dotenv(env_path)

API_URL = "http://localhost:8765"


def test_checkout_endpoint() -> None:
    """Test the /stripe/checkout endpoint."""
    print("🧪 Testing Stripe checkout endpoint...")

    # Test checkout for Starter plan (monthly)
    response = httpx.post(
        f"{API_URL}/stripe/checkout",
        json={"tier": "starter", "billing_cycle": "monthly"},
    )

    if response.status_code == 200:
        data = response.json()
        if "url" in data and data["url"].startswith("https://checkout.stripe.com"):
            print("✅ Starter monthly checkout: SUCCESS")
            print(f"   Checkout URL created: {data['url'][:50]}...")
        else:
            print("❌ Starter monthly checkout: Invalid response")
    else:
        print(f"❌ Starter monthly checkout: HTTP {response.status_code}")
        print(f"   Error: {response.text}")

    # Test checkout for Professional plan (yearly)
    response = httpx.post(
        f"{API_URL}/stripe/checkout",
        json={"tier": "professional", "billing_cycle": "yearly"},
    )

    if response.status_code == 200:
        data = response.json()
        if "url" in data and data["url"].startswith("https://checkout.stripe.com"):
            print("✅ Professional yearly checkout: SUCCESS")
            print(f"   Checkout URL created: {data['url'][:50]}...")
        else:
            print("❌ Professional yearly checkout: Invalid response")
    else:
        print(f"❌ Professional yearly checkout: HTTP {response.status_code}")
        print(f"   Error: {response.text}")


def test_pricing_config() -> None:
    """Test the /pricing/config endpoint."""
    print("\n🧪 Testing pricing config endpoint...")

    response = httpx.get(f"{API_URL}/pricing/config")
    if response.status_code == 200:
        data = response.json()
        # Check that we have the right prices
        starter = data["plans"]["starter"]
        professional = data["plans"]["professional"]

        if starter["stripe_price_id_monthly"] and professional["stripe_price_id_yearly"]:
            print("✅ Pricing config has Stripe IDs")
            print(f"   Starter monthly: {starter['price_monthly']}")
            print(f"   Professional yearly: {professional['price_yearly']}")
        else:
            print("❌ Pricing config missing Stripe IDs")
    else:
        print(f"❌ Pricing config: HTTP {response.status_code}")


if __name__ == "__main__":
    print("=" * 50)
    print("Stripe Integration Test Suite")
    print("=" * 50)
    print(f"\nTesting against: {API_URL}")
    print("Note: Backend server must be running on port 8765\n")

    try:
        test_pricing_config()
        test_checkout_endpoint()
        print("\n✅ All tests completed!")
    except httpx.ConnectError:
        print("\n❌ Could not connect to backend server")
        print("   Run: uvicorn main:app --host 0.0.0.0 --port 8765")
