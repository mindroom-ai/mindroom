#!/bin/bash
set -e

echo "🧠 MindRoom Platform Setup"
echo "========================="

# Check prerequisites
check_command() {
    if ! command -v $1 &> /dev/null; then
        echo "❌ $1 is not installed. Please install it first."
        exit 1
    fi
}

echo "Checking prerequisites..."
check_command docker
check_command npm
check_command node

# Create .env from example
if [ ! -f .env ]; then
    echo "Creating .env file from template..."
    cp .env.example .env
    echo "⚠️  Please edit .env with your actual values"
    exit 1
fi

# Load environment
source .env

# Initialize Supabase
echo "Setting up Supabase..."
cd supabase
npx supabase init || true
if [ -n "$SUPABASE_PROJECT_ID" ]; then
    npx supabase link --project-ref $SUPABASE_PROJECT_ID
    npx supabase db push
    npx supabase functions deploy
fi
cd ..

# Install dependencies for each service
echo "Installing dependencies..."

echo "  - Stripe Handler..."
cd services/stripe-handler
npm install
cd ../..

echo "  - Customer Portal..."
cd apps/customer-portal
npm install
cd ../..

echo "  - Admin Dashboard..."
cd apps/admin-dashboard
npm install
cd ../..

# Setup Dokku SSH key
echo "Setting up Dokku SSH access..."
mkdir -p deploy/platform/ssh
if [ ! -f deploy/platform/ssh/dokku_key ]; then
    ssh-keygen -t rsa -b 4096 -f deploy/platform/ssh/dokku_key -N ""
    echo "⚠️  Add this public key to your Dokku server:"
    cat deploy/platform/ssh/dokku_key.pub
fi

# Create Docker network
echo "Creating Docker network..."
docker network create mindroom-platform 2>/dev/null || true

echo "✅ Setup complete!"
echo ""
echo "Next steps:"
echo "1. Configure your .env file with actual values"
echo "2. Add the SSH public key to your Dokku server"
echo "3. Run: ./scripts/start-local.sh"
