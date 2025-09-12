#!/usr/bin/env python3
"""Setup Stripe products and prices for MindRoom SaaS platform.

This script:
1. Creates or updates the MindRoom product in Stripe
2. Creates prices for each plan (monthly and yearly)
3. Updates the pricing-config.yaml with the correct Stripe price IDs
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import stripe
import yaml

# Load environment variables
sys.path.append(str(Path(__file__).parent.parent))
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

# Configure Stripe
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

if not stripe.api_key:
    print("❌ STRIPE_SECRET_KEY not found in environment")
    sys.exit(1)

print(f"✅ Using Stripe API key: {stripe.api_key[:12]}...")


def find_or_create_product() -> str:
    """Find existing MindRoom product or create a new one."""
    print("\n🔍 Looking for existing MindRoom product...")

    # Search for existing product
    products = stripe.Product.list(limit=100)
    for product in products.data:
        if product.metadata.get("platform") == "mindroom":
            print(f"✅ Found existing product: {product.id} - {product.name}")
            return product.id

    # Create new product
    print("📦 Creating new MindRoom product...")
    product = stripe.Product.create(
        name="MindRoom Subscription",
        description="AI-powered team collaboration platform",
        metadata={
            "platform": "mindroom",
        },
    )
    print(f"✅ Created product: {product.id}")
    return product.id


def create_or_update_price(
    product_id: str,
    tier: str,
    amount: int,
    interval: str,
    per_user: bool = False,
) -> str:
    """Create or update a price for a specific plan."""
    # Build metadata
    metadata = {
        "platform": "mindroom",
        "tier": tier,
        "billing_cycle": "yearly" if interval == "year" else "monthly",
    }

    # Build lookup key
    lookup_key = f"mindroom_{tier}_{interval}"

    print(f"\n💰 Setting up price for {tier} ({interval})...")
    print(f"   Amount: ${amount / 100:.2f} per {interval}")
    if per_user:
        print("   Type: Per-user pricing")

    # Check if price with this lookup key exists
    try:
        existing_prices = stripe.Price.list(
            lookup_keys=[lookup_key],
            limit=1,
        )
        if existing_prices.data:
            price = existing_prices.data[0]
            print(f"   ✅ Found existing price: {price.id}")

            # Archive old price if amount changed
            if price.unit_amount != amount:
                print(f"   ⚠️  Price changed from ${price.unit_amount / 100:.2f} to ${amount / 100:.2f}")
                print("   📦 Archiving old price and creating new one...")
                stripe.Price.modify(price.id, active=False)
            else:
                return price.id
    except Exception:  # noqa: S110
        pass

    # Create new price
    price = stripe.Price.create(
        product=product_id,
        unit_amount=amount,
        currency="usd",
        recurring={"interval": interval},
        lookup_key=lookup_key,
        metadata=metadata,
    )
    print(f"   ✅ Created price: {price.id}")
    return price.id


def update_yaml_config(price_ids: dict) -> None:
    """Update the pricing-config.yaml with Stripe price IDs."""
    config_path = Path(__file__).parent.parent / "pricing-config.yaml"

    print(f"\n📝 Updating {config_path}...")

    # Load existing config
    with config_path.open() as f:
        config = yaml.safe_load(f)

    # Update price IDs
    for plan, ids in price_ids.items():
        if plan in config["plans"]:
            config["plans"][plan]["stripe_price_id_monthly"] = ids["monthly"]
            config["plans"][plan]["stripe_price_id_yearly"] = ids["yearly"]
            print(f"   ✅ Updated {plan} price IDs")

    # Save updated config
    with config_path.open("w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)

    print(f"✅ Updated {config_path}")


def main() -> None:
    """Main setup function."""
    print("🚀 Setting up Stripe prices for MindRoom SaaS platform")
    print("=" * 60)

    # Find or create product
    product_id = find_or_create_product()

    # Define pricing (in cents)
    pricing = {
        "starter": {
            "monthly": 1000,  # $10/month
            "yearly": 9600,  # $96/year ($8/month with 20% discount)
            "per_user": False,
        },
        "professional": {
            "monthly": 800,  # $8/user/month
            "yearly": 7680,  # $76.80/user/year ($6.40/month with 20% discount)
            "per_user": True,
        },
    }

    # Create prices
    price_ids = {}

    for tier, prices in pricing.items():
        price_ids[tier] = {}

        # Monthly price
        price_ids[tier]["monthly"] = create_or_update_price(
            product_id=product_id,
            tier=tier,
            amount=prices["monthly"],
            interval="month",
            per_user=prices["per_user"],
        )

        # Yearly price
        price_ids[tier]["yearly"] = create_or_update_price(
            product_id=product_id,
            tier=tier,
            amount=prices["yearly"],
            interval="year",
            per_user=prices["per_user"],
        )

    # Update YAML config
    update_yaml_config(price_ids)

    print("\n" + "=" * 60)
    print("✅ Stripe setup complete!")
    print("\nPrice IDs have been added to pricing-config.yaml")
    print("The configuration is ready to use.")

    # Print summary
    print("\n📊 Summary:")
    for tier, ids in price_ids.items():
        print(f"\n{tier.title()} Plan:")
        print(f"  Monthly: {ids['monthly']}")
        print(f"  Yearly:  {ids['yearly']}")


if __name__ == "__main__":
    main()
