#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = ["stripe", "python-dotenv"]
# ///
"""Test script for Stripe connection and product listing."""

import os
from pathlib import Path

import stripe
from dotenv import load_dotenv

# Load environment variables from .env file
env_path = Path(__file__).parent.parent / ".env"
load_dotenv(env_path)

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")

# Test the connection and list products
try:
    products = stripe.Product.list(limit=10)
    print(f"Connected to Stripe! Found {len(products.data)} products")
    for product in products.data:
        print(f"  - {product.name}")
        # List prices for this product
        prices = stripe.Price.list(product=product.id, limit=5)
        for price in prices.data:
            amount = price.unit_amount / 100 if price.unit_amount else 0
            print(f"    Price: ${amount:.2f}/{price.recurring.interval if price.recurring else 'one-time'}")
except Exception as e:
    print(f"Error: {e}")
