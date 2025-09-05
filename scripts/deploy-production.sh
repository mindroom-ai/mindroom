#!/bin/bash
set -e

echo "🚀 Deploying MindRoom Platform to Production"

# Check environment
if [ "$ENVIRONMENT" != "production" ]; then
    echo "⚠️  Warning: ENVIRONMENT is not set to 'production'"
    echo "Set ENVIRONMENT=production to proceed with production deployment"
    read -p "Continue anyway? (yes/no): " confirm
    if [ "$confirm" != "yes" ]; then
        exit 1
    fi
fi

# Load environment variables
if [ ! -f .env ]; then
    echo "❌ No .env file found"
    exit 1
fi
source .env

# Build and push Docker images
echo "Building Docker images..."

# Build each service
docker build -t ${REGISTRY}/mindroom-stripe-handler services/stripe-handler
docker build -t ${REGISTRY}/mindroom-dokku-provisioner services/dokku-provisioner
docker build -t ${REGISTRY}/mindroom-customer-portal apps/customer-portal
docker build -t ${REGISTRY}/mindroom-admin-dashboard apps/admin-dashboard

# Push to registry
echo "Pushing to registry..."
docker push ${REGISTRY}/mindroom-stripe-handler
docker push ${REGISTRY}/mindroom-dokku-provisioner
docker push ${REGISTRY}/mindroom-customer-portal
docker push ${REGISTRY}/mindroom-admin-dashboard

# Deploy with Docker Swarm
echo "Deploying to swarm..."
docker stack deploy -c deploy/platform/docker-compose.prod.yml mindroom-platform

# Run database migrations
echo "Running database migrations..."
cd supabase
npx supabase db push --db-url $DATABASE_URL
cd ..

echo "✅ Deployment complete!"
