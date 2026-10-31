#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = ["stripe", "python-dotenv", "pyyaml"]
# ///
# ruff: noqa: ANN001, ANN201, PTH123
"""Sync pricing configuration with Stripe.

This script reads pricing-config.yaml, creates or updates the MindRoom product in Stripe,
reuses or creates a price for each paid plan and billing cycle, and writes the price IDs back to the YAML file.
"""

import os
import sys
from pathlib import Path

import stripe
import yaml
from dotenv import load_dotenv

repo_root = Path(__file__).resolve().parents[2]
env_path = repo_root / "saas-platform" / ".env"
load_dotenv(env_path)

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
if not stripe.api_key:
    print("❌ Error: STRIPE_SECRET_KEY not found in environment variables")
    sys.exit(1)
# This script writes only the test-mode price fields, so live prices are created by hand.
if stripe.api_key.startswith(("sk_live_", "rk_live_")):
    print("❌ Error: STRIPE_SECRET_KEY is a live-mode key; this script syncs test-mode prices only.")
    print("Create live prices by hand: docs/deployment/saas-platform.md#changing-a-plan-price")
    sys.exit(1)

config_path = repo_root / "saas-platform" / "pricing-config.yaml"
with open(config_path) as f:
    config = yaml.safe_load(f)


def as_dict(value):
    """Convert Stripe metadata objects to plain dictionaries."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, stripe.StripeObject):
        return value.to_dict()
    return dict(value)


def sync_product():
    """Create or update the main product in Stripe."""
    for product in stripe.Product.list(limit=100).data:
        if as_dict(product.metadata).get("platform") == "mindroom":
            print(f"✅ Found existing product: {product.id}")
            stripe.Product.modify(
                product.id,
                name=config["product"]["name"],
                description=config["product"]["description"],
            )
            return product.id

    product = stripe.Product.create(
        name=config["product"]["name"],
        description=config["product"]["description"],
        metadata=config["product"]["metadata"],
    )
    print(f"✅ Created new product: {product.id}")
    return product.id


def find_or_create_price(product_id, plan_key, plan_data, billing_cycle):
    """Find or create the Stripe price that bills a plan's configured amount for one billing cycle."""
    amount = plan_data[f"price_{billing_cycle}"]

    # A Stripe price's amount never changes, so only an active price with the configured amount bills what the
    # pricing page shows.
    prices = stripe.Price.list(product=product_id, active=True, limit=100)
    for price in prices.data:
        metadata = as_dict(price.metadata)
        if (
            metadata.get("tier") == plan_key
            and metadata.get("billing_cycle") == billing_cycle
            and price.unit_amount == amount
        ):
            print(f"  Found existing price for {plan_key} ({billing_cycle}): {price.id}")
            return price.id

    price_params = {
        "product": product_id,
        "currency": "usd",
        "unit_amount": amount,
        "recurring": {
            "interval": "month",
            "interval_count": 1 if billing_cycle == "monthly" else 12,
        },
        "nickname": f"{plan_data['name']} ({billing_cycle.capitalize()})",
        "metadata": {
            "tier": plan_key,
            "billing_cycle": billing_cycle,
            "platform": "mindroom",
        },
        "lookup_key": f"{plan_key}_{billing_cycle}",
        # The price this one replaces after an amount change still holds the lookup key.
        "transfer_lookup_key": True,
    }

    price = stripe.Price.create(**price_params)
    print(f"  ✅ Created new price for {plan_key} ({billing_cycle}): {price.id}")
    return price.id


def update_config_with_price_ids(price_ids):
    """Update the YAML config file with Stripe price IDs."""
    for plan_key, ids in price_ids.items():
        config["plans"][plan_key]["stripe_price_id_monthly"] = ids["monthly"]
        config["plans"][plan_key]["stripe_price_id_yearly"] = ids["yearly"]

    with open(config_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)

    print("\n✅ Updated pricing-config.yaml with Stripe price IDs")


def main():
    """Main sync function."""
    print("🚀 Syncing pricing configuration with Stripe...\n")

    product_id = sync_product()

    price_ids = {}
    for plan_key, plan_data in config["plans"].items():
        if plan_key in ["free", "enterprise"]:
            continue  # The free plan needs no price, and enterprise pricing is custom.

        print(f"\nProcessing {plan_data['name']} plan...")
        price_ids[plan_key] = {
            cycle: find_or_create_price(product_id, plan_key, plan_data, cycle) for cycle in ("monthly", "yearly")
        }

    update_config_with_price_ids(price_ids)

    print("\n✨ Stripe sync complete!")
    print("\nPrice IDs have been saved to pricing-config.yaml")
    print("The backend and frontend will now use these prices automatically.")


if __name__ == "__main__":
    main()
