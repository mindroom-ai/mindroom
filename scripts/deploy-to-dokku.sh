#!/bin/bash
# Deploy the MindRoom platform services to Dokku

set -e

echo "🚀 Deploying MindRoom Platform to Dokku"
echo "======================================="

# Check for DOKKU_HOST environment variable
if [ -z "$DOKKU_HOST" ]; then
    echo "❌ DOKKU_HOST not set. Please configure your .env file"
    exit 1
fi

# Load environment
if [ -f .env ]; then
    source .env
fi

echo "Creating Dokku apps for platform services..."

# Create Dokku apps for platform services
ssh dokku@$DOKKU_HOST apps:create mindroom-stripe-handler 2>/dev/null || echo "App mindroom-stripe-handler already exists"
ssh dokku@$DOKKU_HOST apps:create mindroom-provisioner 2>/dev/null || echo "App mindroom-provisioner already exists"
ssh dokku@$DOKKU_HOST apps:create mindroom-customer-portal 2>/dev/null || echo "App mindroom-customer-portal already exists"
ssh dokku@$DOKKU_HOST apps:create mindroom-admin-dashboard 2>/dev/null || echo "App mindroom-admin-dashboard already exists"

# Set up databases
echo "Setting up databases..."
ssh dokku@$DOKKU_HOST postgres:create platform-db 2>/dev/null || echo "Database platform-db already exists"
ssh dokku@$DOKKU_HOST postgres:link platform-db mindroom-stripe-handler 2>/dev/null || echo "Link already exists"
ssh dokku@$DOKKU_HOST postgres:link platform-db mindroom-provisioner 2>/dev/null || echo "Link already exists"

# Redis for sessions
echo "Setting up Redis..."
ssh dokku@$DOKKU_HOST redis:create platform-redis 2>/dev/null || echo "Redis platform-redis already exists"
ssh dokku@$DOKKU_HOST redis:link platform-redis mindroom-stripe-handler 2>/dev/null || echo "Link already exists"

# Set environment variables for each app
echo "Configuring environment variables..."

# Stripe Handler
ssh dokku@$DOKKU_HOST config:set mindroom-stripe-handler \
    PORT=5000 \
    STRIPE_SECRET_KEY="$STRIPE_SECRET_KEY" \
    STRIPE_WEBHOOK_SECRET="$STRIPE_WEBHOOK_SECRET" \
    SUPABASE_URL="$SUPABASE_URL" \
    SUPABASE_SERVICE_KEY="$SUPABASE_SERVICE_KEY" \
    DOKKU_PROVISIONER_URL="http://mindroom-provisioner.dokku:5000"

# Dokku Provisioner
ssh dokku@$DOKKU_HOST config:set mindroom-provisioner \
    DOKKU_HOST="$DOKKU_HOST" \
    BASE_DOMAIN="$BASE_DOMAIN" \
    SUPABASE_URL="$SUPABASE_URL" \
    SUPABASE_SERVICE_KEY="$SUPABASE_SERVICE_KEY"

# Customer Portal
ssh dokku@$DOKKU_HOST config:set mindroom-customer-portal \
    NEXT_PUBLIC_SUPABASE_URL="$SUPABASE_URL" \
    NEXT_PUBLIC_SUPABASE_ANON_KEY="$SUPABASE_ANON_KEY" \
    NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY="$STRIPE_PUBLISHABLE_KEY"

# Admin Dashboard
ssh dokku@$DOKKU_HOST config:set mindroom-admin-dashboard \
    REACT_APP_SUPABASE_URL="$SUPABASE_URL" \
    REACT_APP_SUPABASE_SERVICE_KEY="$SUPABASE_SERVICE_KEY" \
    REACT_APP_PROVISIONER_URL="http://mindroom-provisioner.dokku:5000" \
    REACT_APP_STRIPE_SECRET_KEY="$STRIPE_SECRET_KEY"

# Set up domains
echo "Configuring domains..."
ssh dokku@$DOKKU_HOST domains:add mindroom-stripe-handler webhooks.$BASE_DOMAIN
ssh dokku@$DOKKU_HOST domains:add mindroom-provisioner api.$BASE_DOMAIN
ssh dokku@$DOKKU_HOST domains:add mindroom-customer-portal app.$BASE_DOMAIN
ssh dokku@$DOKKU_HOST domains:add mindroom-admin-dashboard admin.$BASE_DOMAIN

# Deploy each service
echo "Deploying services..."

# Add git remotes
echo "Adding git remotes..."
git remote add dokku-stripe-handler dokku@$DOKKU_HOST:mindroom-stripe-handler 2>/dev/null || true
git remote add dokku-provisioner dokku@$DOKKU_HOST:mindroom-provisioner 2>/dev/null || true
git remote add dokku-customer-portal dokku@$DOKKU_HOST:mindroom-customer-portal 2>/dev/null || true
git remote add dokku-admin-dashboard dokku@$DOKKU_HOST:mindroom-admin-dashboard 2>/dev/null || true

echo ""
echo "✅ Dokku apps configured!"
echo ""
echo "To deploy each service, run from their respective directories:"
echo "  cd services/stripe-handler && git push dokku-stripe-handler main"
echo "  cd services/dokku-provisioner && git push dokku-provisioner main"
echo "  cd apps/customer-portal && git push dokku-customer-portal main"
echo "  cd apps/admin-dashboard && git push dokku-admin-dashboard main"
echo ""
echo "After deployment, enable SSL:"
echo "  ssh dokku@$DOKKU_HOST letsencrypt mindroom-stripe-handler"
echo "  ssh dokku@$DOKKU_HOST letsencrypt mindroom-provisioner"
echo "  ssh dokku@$DOKKU_HOST letsencrypt mindroom-customer-portal"
echo "  ssh dokku@$DOKKU_HOST letsencrypt mindroom-admin-dashboard"
