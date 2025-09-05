#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = ["stripe", "python-dotenv"]
# ///
"""Set up Stripe products and pricing for MindRoom SaaS platform."""

import os
import sys
from pathlib import Path

import stripe
from dotenv import load_dotenv

# Load environment variables from .env file
env_path = Path(__file__).parent.parent.parent / ".env"
load_dotenv(env_path)

# Configure Stripe
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")


def create_or_get_product(name: str, description: str) -> str:
    """Create a product or return existing one."""
    # Search for existing product
    products = stripe.Product.list(active=True, limit=100)
    for product in products.data:
        if product.name == name:
            print(f"  Found existing product: {name} ({product.id})")
            return product.id

    # Create new product
    product = stripe.Product.create(
        name=name,
        description=description,
        metadata={"platform": "mindroom"},
    )
    print(f"  Created product: {name} ({product.id})")
    return product.id


def create_or_get_price(product_id: str, amount: int, nickname: str, interval: str = "month") -> str:
    """Create a price or return existing one."""
    # Search for existing price
    prices = stripe.Price.list(product=product_id, active=True, limit=100)
    for price in prices.data:
        if price.unit_amount == amount and price.recurring and price.recurring.interval == interval:
            print(f"    Found existing price: {nickname} ({price.id})")
            return price.id

    # Create new price
    price = stripe.Price.create(
        product=product_id,
        unit_amount=amount,
        currency="usd",
        recurring={"interval": interval},
        nickname=nickname,
        metadata={"tier": nickname.lower()},
    )
    print(f"    Created price: {nickname} ({price.id})")
    return price.id


def setup_stripe_products() -> dict[str, str]:
    """Set up all MindRoom subscription products."""
    print("Setting up Stripe products and prices...")

    products_config = {
        "Free Trial": {
            "description": "Try MindRoom with limited features",
            "price": 0,
            "nickname": "Free",
        },
        "MindRoom Starter": {
            "description": "Perfect for small teams - 5 agents, 5000 messages/day",
            "price": 4900,  # $49.00
            "nickname": "Starter",
        },
        "MindRoom Professional": {
            "description": "For growing businesses - Unlimited agents, 50000 messages/day",
            "price": 19900,  # $199.00
            "nickname": "Professional",
        },
        "MindRoom Enterprise": {
            "description": "Custom solution with dedicated support",
            "price": 99900,  # $999.00
            "nickname": "Enterprise",
        },
    }

    price_ids = {}

    for product_name, config in products_config.items():
        product_id = create_or_get_product(product_name, config["description"])

        if config["price"] > 0:  # Skip creating price for free tier
            price_id = create_or_get_price(
                product_id,
                config["price"],
                config["nickname"],
            )
            price_ids[config["nickname"].lower()] = price_id

    return price_ids


def setup_webhook_endpoint() -> str | None:
    """Create or update webhook endpoint."""
    print("\nSetting up webhook endpoint...")

    webhook_url = f"https://webhooks.{os.environ.get('PLATFORM_DOMAIN', 'mindroom.chat')}/stripe"

    # Check for existing endpoints
    endpoints = stripe.WebhookEndpoint.list(limit=100)
    for endpoint in endpoints.data:
        if endpoint.url == webhook_url:
            print(f"  Found existing webhook: {webhook_url}")
            print(f"  Webhook ID: {endpoint.id}")
            print("  Note: Webhook secret cannot be retrieved for existing endpoints.")
            print("  Check your .env file for STRIPE_WEBHOOK_SECRET or recreate the webhook.")
            return None

    # Create new endpoint
    endpoint = stripe.WebhookEndpoint.create(
        url=webhook_url,
        enabled_events=[
            "customer.subscription.created",
            "customer.subscription.updated",
            "customer.subscription.deleted",
            "invoice.payment_succeeded",
            "invoice.payment_failed",
            "customer.created",
            "customer.updated",
        ],
    )
    print(f"  Created webhook: {webhook_url}")
    print(f"  Webhook secret: {endpoint.secret}")
    return endpoint.secret


if __name__ == "__main__":
    try:
        # Set up products and prices
        price_ids = setup_stripe_products()

        # Set up webhook
        webhook_secret = setup_webhook_endpoint()

        # Output configuration
        print("\n" + "=" * 50)
        print("✅ Stripe setup complete!")
        print("=" * 50)
        print("\nAdd these to your .env file:")
        print()
        for tier, price_id in price_ids.items():
            env_var = f"STRIPE_PRICE_{tier.upper()}"
            print(f"{env_var}={price_id}")
        if webhook_secret:
            print(f"STRIPE_WEBHOOK_SECRET={webhook_secret}")
        else:
            print("# STRIPE_WEBHOOK_SECRET already exists (check your .env file)")

    except stripe.error.StripeError as e:
        print(f"❌ Stripe Error: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Error: {e}")
        sys.exit(1)
