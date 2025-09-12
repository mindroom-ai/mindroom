#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = ["stripe", "python-dotenv", "pyyaml"]
# ///
"""Test script for Stripe connection and product listing."""

import os
import sys
from pathlib import Path

import stripe
import yaml
from dotenv import load_dotenv

# Load environment variables from .env file (should be in saas-platform root)
env_path = Path(__file__).parent.parent.parent / ".env"
load_dotenv(env_path)

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")

if not stripe.api_key:
    print("❌ No STRIPE_SECRET_KEY found in environment")
    print(f"   Checked: {env_path}")
    sys.exit(1)

# Load our pricing config
config_path = Path(__file__).parent.parent.parent / "pricing-config.yaml"
print(f"📋 Loading pricing config from: {config_path}")
with config_path.open() as f:
    pricing_config = yaml.safe_load(f)

print("\n🎯 Expected Pricing (from YAML):")
for plan_key, plan in pricing_config["plans"].items():
    if plan_key in ["starter", "professional"]:
        monthly = plan["price_monthly"] / 100
        yearly = plan["price_yearly"] / 100
        print(f"  {plan['name']}:")
        print(f"    Monthly: ${monthly:.2f}")
        print(f"    Yearly: ${yearly:.2f} (20% off)")
        if plan.get("stripe_price_id_monthly"):
            print("    ✅ Has Stripe IDs configured")
        else:
            print("    ⚠️  No Stripe IDs yet (run sync-stripe-prices.py)")

# Test the connection and list products
print("\n🔍 Current Stripe Products:")
try:
    products = stripe.Product.list(limit=10)
    print(f"Connected to Stripe! Found {len(products.data)} products\n")

    mindroom_found = False
    for product in products.data:
        # Check if this is our MindRoom product
        if product.metadata.get("platform") == "mindroom":
            mindroom_found = True
            print(f"✅ MindRoom Product: {product.name}")
            print(f"   ID: {product.id}")
        else:
            print(f"  - {product.name}")

        # List prices for this product
        prices = stripe.Price.list(product=product.id, limit=10)
        for price in prices.data:
            amount = price.unit_amount / 100 if price.unit_amount else 0
            interval = price.recurring.interval if price.recurring else "one-time"
            metadata = price.metadata or {}
            plan = metadata.get("plan", "unknown")
            billing = metadata.get("billing_cycle", "")

            if metadata.get("platform") == "mindroom":
                print(f"    ✅ {plan.capitalize()} ({billing}): ${amount:.2f}/{interval}")
            else:
                print(f"    Price: ${amount:.2f}/{interval}")

    if not mindroom_found:
        print("\n⚠️  No MindRoom product found in Stripe yet.")
        print("   Run: ./scripts/sync-stripe-prices.py")

except Exception as e:
    print(f"❌ Error connecting to Stripe: {e}")
