#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = ["stripe", "python-dotenv"]
# ///
# ruff: noqa: ANN001, ANN201, S110, DTZ006, B007, PERF102
"""Clean up duplicate Stripe subscriptions for test customers.

This script lists all subscriptions for a customer and allows you to
cancel duplicates while keeping the most recent one.
"""

import os
import sys
from datetime import datetime
from pathlib import Path

import stripe
from dotenv import load_dotenv

# Load environment variables
env_path = Path(__file__).parent.parent / ".env"
load_dotenv(env_path)

# Initialize Stripe
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
if not stripe.api_key:
    print("❌ Error: STRIPE_SECRET_KEY not found in environment variables")
    sys.exit(1)


def list_all_subscriptions():
    """List all subscriptions grouped by customer."""
    print("🔍 Fetching all subscriptions...")

    subscriptions = stripe.Subscription.list(limit=100)

    # Group by customer
    customers = {}
    for sub in subscriptions.data:
        customer_id = sub.customer

        # Get customer email
        customer_email = "Unknown"
        try:
            customer = stripe.Customer.retrieve(customer_id)
            customer_email = customer.email or "Unknown"
        except Exception:
            pass

        if customer_id not in customers:
            customers[customer_id] = {
                "email": customer_email,
                "subscriptions": [],
            }

        customers[customer_id]["subscriptions"].append(sub)

    return customers


def display_subscriptions(customers):
    """Display subscriptions grouped by customer."""
    print("\n📊 Subscriptions by Customer:")
    print("=" * 60)

    for customer_id, data in customers.items():
        subs = data["subscriptions"]
        if len(subs) > 1:  # Only show customers with multiple subscriptions
            print(f"\n👤 Customer: {data['email']} ({customer_id})")
            print(f"   ⚠️  Has {len(subs)} subscriptions (should only have 1)")

            for i, sub in enumerate(subs, 1):
                created = datetime.fromtimestamp(sub.created).strftime("%Y-%m-%d %H:%M")
                trial_end = "N/A"
                if sub.trial_end:
                    trial_end = datetime.fromtimestamp(sub.trial_end).strftime("%Y-%m-%d")

                # Get price info
                price_info = "Unknown"
                try:
                    if sub.items and hasattr(sub.items, "data") and sub.items.data:
                        item = sub.items.data[0]
                        price = item.price
                        amount = price.unit_amount / 100 if price.unit_amount else 0
                        interval = price.recurring.interval if price.recurring else "once"
                        price_info = f"${amount:.2f}/{interval}"
                except Exception:
                    pass  # Keep "Unknown" as price_info

                print(f"   {i}. ID: {sub.id}")
                print(f"      Status: {sub.status}")
                print(f"      Created: {created}")
                print(f"      Trial ends: {trial_end}")
                print(f"      Price: {price_info}")


def cleanup_duplicates(customers):
    """Cancel duplicate subscriptions, keeping only the most recent one."""
    print("\n🧹 Cleanup Options:")
    print("1. Cancel all duplicates (keep most recent)")
    print("2. Cancel all duplicates (keep oldest)")
    print("3. Manual selection")
    print("4. Exit without changes")

    choice = input("\nSelect option (1-4): ")

    if choice == "4":
        print("👋 Exiting without changes")
        return

    for customer_id, data in customers.items():
        subs = data["subscriptions"]
        if len(subs) <= 1:
            continue

        print(f"\n🔧 Processing customer: {data['email']}")

        # Sort by creation date
        subs_sorted = sorted(subs, key=lambda x: x.created)

        if choice == "1":
            # Keep most recent
            to_keep = subs_sorted[-1]
            to_cancel = subs_sorted[:-1]
        elif choice == "2":
            # Keep oldest
            to_keep = subs_sorted[0]
            to_cancel = subs_sorted[1:]
        elif choice == "3":
            # Manual selection
            print(f"   Which subscription to keep for {data['email']}?")
            for i, sub in enumerate(subs_sorted, 1):
                created = datetime.fromtimestamp(sub.created).strftime("%Y-%m-%d")
                print(f"   {i}. {sub.id} (created {created}, status: {sub.status})")

            keep_idx = int(input("   Keep subscription number: ")) - 1
            to_keep = subs_sorted[keep_idx]
            to_cancel = [s for s in subs_sorted if s.id != to_keep.id]
        else:
            print("   ❌ Invalid choice, skipping")
            continue

        print(f"   ✅ Keeping: {to_keep.id}")

        for sub in to_cancel:
            try:
                print(f"   🗑️  Cancelling: {sub.id}...")
                stripe.Subscription.delete(sub.id)
                print("      ✅ Cancelled successfully")
            except Exception as e:
                print(f"      ❌ Error cancelling: {e}")


def main():
    """Main function."""
    print("🚀 Stripe Duplicate Subscription Cleanup")
    print("=" * 60)

    # Check if we're in test mode
    if stripe.api_key.startswith("sk_test_"):
        print("🧪 Using TEST mode Stripe API")
    else:
        print("🔴 Using LIVE mode Stripe API")
        response = input("⚠️  Are you sure you want to continue in LIVE mode? (yes/no): ")
        if response.lower() != "yes":
            sys.exit(1)

    # List all subscriptions
    customers = list_all_subscriptions()

    # Count duplicates
    duplicate_count = sum(1 for data in customers.values() if len(data["subscriptions"]) > 1)

    if duplicate_count == 0:
        print("\n✅ No duplicate subscriptions found!")
        return

    print(f"\n⚠️  Found {duplicate_count} customers with duplicate subscriptions")

    # Display subscriptions
    display_subscriptions(customers)

    # Offer cleanup
    response = input("\n🧹 Would you like to clean up duplicates? (yes/no): ")
    if response.lower() == "yes":
        cleanup_duplicates(customers)
        print("\n✅ Cleanup complete!")
    else:
        print("\n👋 Exiting without changes")


if __name__ == "__main__":
    main()
