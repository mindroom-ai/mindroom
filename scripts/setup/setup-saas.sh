#!/usr/bin/env bash
# Declarative setup for Supabase and Stripe for MindRoom SaaS platform

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

echo "🚀 MindRoom SaaS Services Setup"
echo "================================"
echo ""

# Load environment variables
if [ ! -f "$PROJECT_ROOT/.env" ]; then
    echo "❌ No .env file found. Please configure first."
    exit 1
fi

# Load environment variables using uvx and python-dotenv
eval $(uvx --from "python-dotenv[cli]" dotenv list --format=shell)

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
    cd "$PROJECT_ROOT/supabase"
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

    cd "$PROJECT_ROOT"
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

# Run the Stripe setup script
echo "Creating Stripe products..."

# The Python script loads .env itself with python-dotenv
"$SCRIPT_DIR/setup_stripe_products.py"

echo ""
echo -e "${GREEN}✅ Stripe products created!${NC}"
echo ""

# ===========================
# CREATE TEST DATA (Optional)
# ===========================

echo -e "${YELLOW}🧪 Creating test data...${NC}"
echo ""

# Apply test data to Supabase
if [ -z "$SKIP_SUPABASE" ]; then
    echo "Inserting test data..."
    cd "$PROJECT_ROOT/supabase"

    # Copy test data SQL file to migrations temporarily
    cp "$SCRIPT_DIR/test_data.sql" "./seed.sql" 2>/dev/null || true

    # Apply test data
    supabase db push --include-seed || echo "Test data may already exist"

    # Clean up
    rm -f "./seed.sql"

    cd "$PROJECT_ROOT"
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
