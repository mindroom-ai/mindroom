#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = ["stripe", "python-dotenv", "pyyaml"]
# ///
# ruff: noqa: ANN001, ANN201, PTH123
"""Sync pricing configuration with Stripe.

This script reads the pricing-config.yaml and creates/updates
products and prices in Stripe. It also updates the YAML file
with the generated Stripe price IDs.
"""

import os
import sys
from pathlib import Path

import stripe
import yaml
from dotenv import load_dotenv

# Load environment variables
env_path = Path(__file__).parent.parent / ".env"
load_dotenv(env_path)

# Initialize Stripe
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
if not stripe.api_key:
    print("❌ Error: STRIPE_SECRET_KEY not found in environment variables")
    sys.exit(1)

# Load pricing config
config_path = Path(__file__).parent.parent / "pricing-config.yaml"
with open(config_path) as f:
    config = yaml.safe_load(f)


def sync_product():
    """Create or update the main product in Stripe."""
    products = stripe.Product.list(limit=100)

    # Look for existing product
    existing_product = None
    for product in products.data:
        if product.metadata.get("platform") == "mindroom":
            existing_product = product
            break

    if existing_product:
        print(f"✅ Found existing product: {existing_product.id}")
        # Update product details
        stripe.Product.modify(
            existing_product.id,
            name=config["product"]["name"],
            description=config["product"]["description"],
        )
        return existing_product.id
    # Create new product
    product = stripe.Product.create(
        name=config["product"]["name"],
        description=config["product"]["description"],
        metadata=config["product"]["metadata"],
    )
    print(f"✅ Created new product: {product.id}")
    return product.id


def find_or_create_price(product_id, plan_key, plan_data, billing_cycle):
    """Find or create a Stripe price for a plan."""
    if plan_key == "free":
        return None  # No Stripe price needed for free plan

    if plan_key == "enterprise":
        return None  # Enterprise is custom pricing

    # Determine price amount
    amount = plan_data["price_monthly"] if billing_cycle == "monthly" else plan_data["price_yearly"]

    # Check if this is per-user pricing
    price_model = plan_data.get("price_model", "flat")

    # Search for existing price with matching metadata
    prices = stripe.Price.list(product=product_id, limit=100)
    for price in prices.data:
        metadata = price.metadata or {}
        if metadata.get("plan") == plan_key and metadata.get("billing_cycle") == billing_cycle:
            print(f"  Found existing price for {plan_key} ({billing_cycle}): {price.id}")
            return price.id

    # Create new price
    interval = "month" if billing_cycle == "monthly" else "year"

    price_params = {
        "product": product_id,
        "currency": "usd",
        "unit_amount": amount,
        "recurring": {
            "interval": interval if billing_cycle == "monthly" else "month",
            "interval_count": 1 if billing_cycle == "monthly" else 12,
        },
        "nickname": f"{plan_data['name']} ({billing_cycle.capitalize()})",
        "metadata": {
            "plan": plan_key,
            "billing_cycle": billing_cycle,
            "platform": "mindroom",
        },
        "lookup_key": f"{plan_key}_{billing_cycle}",
    }

    # Add per-user billing if applicable
    if price_model == "per_user":
        price_params["recurring"]["usage_type"] = "licensed"
        price_params["billing_scheme"] = "per_unit"

    price = stripe.Price.create(**price_params)
    print(f"  ✅ Created new price for {plan_key} ({billing_cycle}): {price.id}")
    return price.id


def update_config_with_price_ids(price_ids):
    """Update the YAML config file with Stripe price IDs."""
    # Read the current config
    with open(config_path) as f:
        config = yaml.safe_load(f)

    # Update price IDs
    for plan_key, ids in price_ids.items():
        if plan_key in config["plans"]:
            if ids.get("monthly"):
                config["plans"][plan_key]["stripe_price_id_monthly"] = ids["monthly"]
            if ids.get("yearly"):
                config["plans"][plan_key]["stripe_price_id_yearly"] = ids["yearly"]

    # Write back to file
    with open(config_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)

    print("\n✅ Updated pricing-config.yaml with Stripe price IDs")


def main():
    """Main sync function."""
    print("🚀 Syncing pricing configuration with Stripe...\n")

    # Create/update product
    product_id = sync_product()

    # Create/update prices for each plan
    price_ids = {}
    for plan_key, plan_data in config["plans"].items():
        if plan_key in ["free", "enterprise"]:
            continue  # Skip free and enterprise plans

        print(f"\nProcessing {plan_data['name']} plan...")
        price_ids[plan_key] = {}

        # Create monthly price
        monthly_id = find_or_create_price(product_id, plan_key, plan_data, "monthly")
        if monthly_id:
            price_ids[plan_key]["monthly"] = monthly_id

        # Create yearly price
        yearly_id = find_or_create_price(product_id, plan_key, plan_data, "yearly")
        if yearly_id:
            price_ids[plan_key]["yearly"] = yearly_id

    # Update config file with price IDs
    update_config_with_price_ids(price_ids)

    print("\n✨ Stripe sync complete!")
    print("\nPrice IDs have been saved to pricing-config.yaml")
    print("The backend and frontend will now use these prices automatically.")


if __name__ == "__main__":
    main()
