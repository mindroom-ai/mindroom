#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = ["stripe", "python-dotenv", "requests"]
# ///
# ruff: noqa: ANN001, ANN201, PTH123
"""Setup Stripe webhooks for MindRoom platform.

This script creates or updates webhook endpoints in Stripe to handle
subscription lifecycle events and payment processing.
"""

import os
import sys
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

# Get API URL from environment or use default
api_url = os.getenv("API_URL") or (
    f"https://api.{os.getenv('PLATFORM_DOMAIN')}"
    if os.getenv("PLATFORM_DOMAIN")
    else "https://api.staging.mindroom.chat"
)
if "localhost" in api_url or "127.0.0.1" in api_url:
    print("⚠️  Warning: Using localhost API URL. Webhooks won't work with local development.")
    print("   Use ngrok or similar to expose your local server for webhook testing.")

# Webhook events we need to listen for
WEBHOOK_EVENTS = [
    # Subscription lifecycle
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "customer.subscription.trial_will_end",
    # Payment events
    "invoice.payment_succeeded",
    "invoice.payment_failed",
    # Customer events (optional but useful)
    "customer.created",
    "customer.updated",
]


def find_existing_webhook():
    """Find existing webhook endpoint for our API URL."""
    try:
        endpoints = stripe.WebhookEndpoint.list(limit=100)
        webhook_url = f"{api_url}/webhooks/stripe"

        for endpoint in endpoints.data:
            if endpoint.url == webhook_url:
                return endpoint
    except Exception as e:
        print(f"❌ Error listing webhooks: {e}")
        return None
    else:
        return None


def create_or_update_webhook():
    """Create or update the webhook endpoint."""
    webhook_url = f"{api_url}/webhooks/stripe"
    existing = find_existing_webhook()

    if existing:
        print(f"📝 Found existing webhook: {existing.id}")
        print(f"   URL: {existing.url}")
        print(f"   Status: {existing.status}")

        # Update the webhook to ensure it has all required events
        try:
            updated = stripe.WebhookEndpoint.modify(
                existing.id,
                enabled_events=WEBHOOK_EVENTS,
                description="MindRoom Platform Webhook",
            )
            print(f"✅ Updated webhook with {len(WEBHOOK_EVENTS)} events")
        except Exception as e:
            print(f"❌ Error updating webhook: {e}")
            return None
        else:
            return updated
    else:
        print("🆕 Creating new webhook endpoint...")
        print(f"   URL: {webhook_url}")

        try:
            webhook = stripe.WebhookEndpoint.create(
                url=webhook_url,
                enabled_events=WEBHOOK_EVENTS,
                description="MindRoom Platform Webhook",
            )
            print(f"✅ Created webhook: {webhook.id}")
        except Exception as e:
            print(f"❌ Error creating webhook: {e}")
            return None
        else:
            return webhook


def save_webhook_secret(webhook):
    """Save the webhook secret to .env file."""
    if not webhook:
        print("⚠️  No webhook to save")
        return

    # The secret is only available when creating a new webhook
    secret = getattr(webhook, "secret", None)
    if not secret:
        print("⚠️  Webhook secret not available (only provided when creating new webhooks)")
        print("   The existing webhook secret in your .env should still be valid")
        return

    secret = webhook.secret
    print(f"\n🔐 Webhook Signing Secret: {secret}")

    # Update .env file if it exists
    env_file = Path(__file__).parent.parent / ".env"
    if env_file.exists():
        with open(env_file) as f:
            lines = f.readlines()

        # Check if STRIPE_WEBHOOK_SECRET already exists
        found = False
        for i, line in enumerate(lines):
            if line.startswith("STRIPE_WEBHOOK_SECRET="):
                lines[i] = f"STRIPE_WEBHOOK_SECRET={secret}\n"
                found = True
                break

        if not found:
            # Add it to the end
            if not lines[-1].endswith("\n"):
                lines.append("\n")
            lines.append(f"STRIPE_WEBHOOK_SECRET={secret}\n")

        with open(env_file, "w") as f:
            f.writelines(lines)
        print("✅ Updated .env file with webhook secret")

    # Also provide instructions for Kubernetes
    print("\n📋 To update Kubernetes secret, run:")
    print("   kubectl patch secret platform-secrets -n mindroom-staging \\")
    print(f'     -p \'{{"data":{{"STRIPE_WEBHOOK_SECRET":"{secret}"}}}}\' --type=merge')

    print("\n   Or add to your Helm values:")
    print("   stripe:")
    print(f"     webhookSecret: {secret}")


def test_webhook_connectivity():
    """Test if the webhook URL is accessible."""
    import requests  # noqa: PLC0415

    webhook_url = f"{api_url}/webhooks/stripe"
    print("\n🧪 Testing webhook connectivity...")
    print(f"   URL: {webhook_url}")

    try:
        # Try a simple POST request (it should return 400 due to missing signature)
        response = requests.post(webhook_url, json={}, timeout=5)
        if response.status_code == 400:
            print("✅ Webhook endpoint is reachable (returned 400 as expected)")
            return True
        print(f"⚠️  Unexpected response: {response.status_code}")
        return False  # noqa: TRY300
    except requests.exceptions.RequestException as e:
        print(f"❌ Cannot reach webhook endpoint: {e}")
        print("   Make sure your backend is deployed and accessible")
        return False


def list_recent_events():
    """List recent webhook events to check if they're being received."""
    print("\n📊 Recent webhook events (last 10):")
    try:
        events = stripe.Event.list(limit=10)
        if not events.data:
            print("   No events found")
        else:
            for event in events.data:
                status = "✅" if event.request and event.request.get("id") else "⏳"
                print(f"   {status} {event.type} - {event.created}")
    except Exception as e:
        print(f"❌ Error listing events: {e}")


def main():
    """Main function to setup webhooks."""
    print("🚀 MindRoom Stripe Webhook Setup")
    print("=" * 50)

    # Check if we're in test mode
    if stripe.api_key.startswith("sk_test_"):
        print("🧪 Using TEST mode Stripe API")
    else:
        print("🔴 Using LIVE mode Stripe API")

    # Test connectivity first
    if not test_webhook_connectivity():
        print("\n⚠️  Warning: Cannot reach webhook endpoint.")
        print("   The webhook will be created but won't work until the backend is accessible.")
        response = input("   Continue anyway? (y/n): ")
        if response.lower() != "y":
            sys.exit(1)

    # Create or update webhook
    webhook = create_or_update_webhook()
    if webhook:
        save_webhook_secret(webhook)

        print("\n📋 Webhook Configuration:")
        print(f"   ID: {webhook.id}")
        print(f"   URL: {webhook.url}")
        print(f"   Status: {webhook.status}")
        print(f"   Events: {len(webhook.enabled_events)} events configured")

        # List recent events
        list_recent_events()

        print("\n✅ Webhook setup complete!")
        print("\n📝 Next steps:")
        print("   1. Deploy the backend with the webhook secret")
        print("   2. Test by completing a Stripe checkout")
        print("   3. Check logs: kubectl logs deployment/platform-backend -n mindroom-staging")
    else:
        print("\n❌ Failed to setup webhook")
        sys.exit(1)


if __name__ == "__main__":
    main()
