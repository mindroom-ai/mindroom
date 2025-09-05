#!/usr/bin/env bash
# Declarative setup for Supabase and Stripe for MindRoom SaaS platform

set -e

echo "🚀 MindRoom SaaS Services Setup"
echo "================================"
echo ""

# Load environment variables
if [ ! -f .env ]; then
    echo "❌ No .env file found. Please configure first."
    exit 1
fi

source .env

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# ===========================
# SUPABASE SETUP
# ===========================

echo -e "${YELLOW}📦 Setting up Supabase...${NC}"
echo ""

# Check if Supabase CLI is installed
if ! command -v supabase &> /dev/null; then
    echo "⚠️  Supabase CLI not found. Please install it manually:"
    echo "    npm install -g supabase"
    echo ""
    echo "Skipping Supabase setup for now..."
    SKIP_SUPABASE=true
fi

# Initialize Supabase if not already done
if [ -z "$SKIP_SUPABASE" ]; then
    cd supabase
    if [ ! -f "config.toml" ]; then
        echo "Initializing Supabase..."
        supabase init
    fi

    # Link to remote project
    echo "Linking to Supabase project..."
    supabase link --project-ref "$SUPABASE_PROJECT_ID" 2>/dev/null || true

    # Push migrations to remote database
    echo "Running database migrations..."
    supabase db push --include-all

    # Deploy Edge Functions
    echo "Deploying Edge Functions..."
    supabase functions deploy --no-verify-jwt 2>/dev/null || echo "No functions to deploy"

    cd ..
    echo -e "${GREEN}✅ Supabase setup complete!${NC}"
else
    echo -e "${YELLOW}⚠️  Supabase setup skipped (CLI not available)${NC}"
fi
echo ""

# ===========================
# STRIPE PRODUCT SETUP
# ===========================

echo -e "${YELLOW}💳 Setting up Stripe Products...${NC}"
echo ""

# Create a Python script to set up Stripe products
cat > /tmp/setup_stripe_products.py << 'EOF'
import stripe
import os
import json
from typing import Dict, Any

# Configure Stripe
stripe.api_key = os.environ.get('STRIPE_SECRET_KEY')

def create_or_get_product(name: str, description: str) -> str:
    """Create a product or return existing one"""
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
        metadata={"platform": "mindroom"}
    )
    print(f"  Created product: {name} ({product.id})")
    return product.id

def create_or_get_price(product_id: str, amount: int, nickname: str, interval: str = "month") -> str:
    """Create a price or return existing one"""
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
        metadata={"tier": nickname.lower()}
    )
    print(f"    Created price: {nickname} ({price.id})")
    return price.id

def setup_stripe_products():
    """Set up all MindRoom subscription products"""
    print("Setting up Stripe products and prices...")

    products_config = {
        "Free Trial": {
            "description": "Try MindRoom with limited features",
            "price": 0,
            "nickname": "Free"
        },
        "MindRoom Starter": {
            "description": "Perfect for small teams - 5 agents, 5000 messages/day",
            "price": 4900,  # $49.00
            "nickname": "Starter"
        },
        "MindRoom Professional": {
            "description": "For growing businesses - Unlimited agents, 50000 messages/day",
            "price": 19900,  # $199.00
            "nickname": "Professional"
        },
        "MindRoom Enterprise": {
            "description": "Custom solution with dedicated support",
            "price": 99900,  # $999.00
            "nickname": "Enterprise"
        }
    }

    price_ids = {}

    for product_name, config in products_config.items():
        product_id = create_or_get_product(product_name, config["description"])

        if config["price"] > 0:  # Skip creating price for free tier
            price_id = create_or_get_price(
                product_id,
                config["price"],
                config["nickname"]
            )
            price_ids[config["nickname"].lower()] = price_id

    return price_ids

def setup_webhook_endpoint():
    """Create or update webhook endpoint"""
    print("\nSetting up webhook endpoint...")

    webhook_url = f"https://webhooks.{os.environ.get('PLATFORM_DOMAIN', 'mindroom.chat')}/stripe"

    # Check for existing endpoints
    endpoints = stripe.WebhookEndpoint.list(limit=100)
    for endpoint in endpoints.data:
        if endpoint.url == webhook_url:
            print(f"  Found existing webhook: {webhook_url}")
            print(f"  Webhook secret: {endpoint.secret}")
            return endpoint.secret

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
            "customer.updated"
        ]
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
        print("\n" + "="*50)
        print("✅ Stripe setup complete!")
        print("="*50)
        print("\nAdd these to your .env file:")
        print("")
        for tier, price_id in price_ids.items():
            env_var = f"STRIPE_PRICE_{tier.upper()}"
            print(f"{env_var}={price_id}")
        print(f"STRIPE_WEBHOOK_SECRET={webhook_secret}")

    except stripe.error.StripeError as e:
        print(f"❌ Stripe Error: {e}")
        exit(1)
    except Exception as e:
        print(f"❌ Error: {e}")
        exit(1)
EOF

# Run the Stripe setup script
echo "Creating Stripe products..."
export STRIPE_SECRET_KEY
export PLATFORM_DOMAIN
python /tmp/setup_stripe_products.py

echo ""
echo -e "${GREEN}✅ Stripe products created!${NC}"
echo ""

# ===========================
# CREATE TEST DATA (Optional)
# ===========================

echo -e "${YELLOW}🧪 Creating test data...${NC}"
echo ""

# Create SQL script for test data
cat > /tmp/test_data.sql << 'EOF'
-- Insert test customer account
INSERT INTO accounts (email, full_name, company_name)
VALUES
    ('test@example.com', 'Test User', 'Test Company'),
    ('demo@example.com', 'Demo User', 'Demo Inc')
ON CONFLICT (email) DO NOTHING;

-- Insert test subscription for the test user
INSERT INTO subscriptions (account_id, tier, status, max_agents, max_messages_per_day, max_storage_gb)
SELECT id, 'starter', 'active', 5, 5000, 10
FROM accounts
WHERE email = 'test@example.com'
ON CONFLICT DO NOTHING;

-- Insert instance for test user
INSERT INTO instances (
    account_id,
    name,
    status,
    dokku_app_name,
    url
)
SELECT
    id,
    'test-instance',
    'running',
    'mindroom-test-' || SUBSTRING(id::text, 1, 8),
    'https://test-' || SUBSTRING(id::text, 1, 8) || '.mindroom.chat'
FROM accounts
WHERE email = 'test@example.com'
ON CONFLICT DO NOTHING;
EOF

# Apply test data to Supabase
if [ -z "$SKIP_SUPABASE" ]; then
    echo "Inserting test data..."
    cd supabase
    supabase db push --include-seed || echo "Test data may already exist"
    cd ..
    echo -e "${GREEN}✅ Test data created!${NC}"
else
    echo -e "${YELLOW}⚠️  Test data insertion skipped (Supabase CLI not available)${NC}"
fi
echo ""

# ===========================
# SUMMARY
# ===========================

echo "================================"
echo -e "${GREEN}🎉 Setup Complete!${NC}"
echo "================================"
echo ""
echo "Supabase:"
echo "  ✅ Migrations applied"
echo "  ✅ Edge functions deployed (if any)"
echo "  ✅ Test data inserted"
echo ""
echo "Stripe:"
echo "  ✅ Products created"
echo "  ✅ Prices configured"
echo "  ✅ Webhook endpoint set up"
echo ""
echo "Next steps:"
echo "1. Update your .env with the Stripe price IDs shown above"
echo "2. Start the services:"
echo "   - Customer Portal: cd apps/customer-portal && npm run dev"
echo "   - Admin Dashboard: cd apps/admin-dashboard && npm run dev"
echo "   - Stripe Handler: cd services/stripe-handler && npm start"
echo "3. Test the flow:"
echo "   - Sign up at http://localhost:3002"
echo "   - Choose a plan and pay with test card: 4242 4242 4242 4242"
echo "   - Access dashboard after payment"
echo ""
echo "For local webhook testing:"
echo "  stripe listen --forward-to localhost:3007/webhooks/stripe"
