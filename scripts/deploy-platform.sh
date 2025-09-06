#!/bin/bash

# Deploy MindRoom Platform to Production Servers
# This script deploys all platform services to the infrastructure created by Terraform

set -e

echo "🚀 MindRoom Platform Deployment Script"
echo "====================================="
echo ""

# Check if .env exists
if [ ! -f .env ]; then
    echo "❌ Error: .env file not found"
    echo "Please create a .env file with production credentials"
    exit 1
fi

# Load environment variables
source .env

# Get server IPs from Terraform
cd infrastructure/terraform
DOKKU_IP=$(terraform output -raw dokku_server_ip 2>/dev/null || echo "")
PLATFORM_IP=$(terraform output -raw platform_server_ip 2>/dev/null || echo "")
cd ../..

if [ -z "$DOKKU_IP" ] || [ -z "$PLATFORM_IP" ]; then
    echo "❌ Error: Could not get server IPs from Terraform"
    echo "Make sure infrastructure is deployed:"
    echo "  cd infrastructure/terraform && terraform apply"
    exit 1
fi

echo "📍 Server Information:"
echo "  Dokku Server: $DOKKU_IP"
echo "  Platform Server: $PLATFORM_IP"
echo ""

# Function to build and push Docker images
build_and_push_image() {
    local service=$1
    local dockerfile=$2
    local tag="${REGISTRY:-ghcr.io/mindroom}/${service}:latest"

    echo "🔨 Building $service..."
    docker build -f $dockerfile -t $tag .

    echo "📤 Pushing $service to registry..."
    docker push $tag
}

# Function to deploy service to platform server
deploy_to_platform() {
    local service=$1
    local port=$2

    echo "🚢 Deploying $service to platform server..."

    # Copy docker-compose files
    scp docker-compose.platform.yml root@$PLATFORM_IP:/opt/platform/
    scp .env root@$PLATFORM_IP:/opt/platform/

    # Deploy using docker-compose
    ssh root@$PLATFORM_IP "cd /opt/platform && docker-compose -f docker-compose.platform.yml up -d $service"

    echo "✅ $service deployed"
}

# Main deployment process
echo ""
echo "🎯 Starting deployment process..."
echo ""

# Step 1: Build all Docker images
echo "📦 Step 1: Building Docker images..."
echo ""

build_and_push_image "customer-portal" "deploy/Dockerfile.customer-portal"
build_and_push_image "admin-dashboard" "deploy/Dockerfile.admin-dashboard"
build_and_push_image "stripe-handler" "deploy/Dockerfile.stripe-handler"
build_and_push_image "dokku-provisioner" "deploy/Dockerfile.dokku-provisioner"

echo ""
echo "✅ All images built and pushed"
echo ""

# Step 2: Deploy platform services
echo "🌐 Step 2: Deploying platform services..."
echo ""

# Update environment on platform server
echo "📝 Updating environment variables..."
ssh root@$PLATFORM_IP "cat > /opt/platform/.env" < .env

# Deploy each service
deploy_to_platform "customer-portal" "3002"
deploy_to_platform "admin-dashboard" "3001"
deploy_to_platform "stripe-handler" "3007"
deploy_to_platform "dokku-provisioner" "8002"

echo ""
echo "✅ All services deployed"
echo ""

# Step 3: Configure Nginx
echo "🔧 Step 3: Configuring Nginx..."
cat > /tmp/nginx.conf <<'EOF'
# Customer Portal
server {
    listen 80;
    server_name app.mindroom.chat;

    location / {
        proxy_pass http://localhost:3002;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_cache_bypass $http_upgrade;
    }
}

# Admin Dashboard
server {
    listen 80;
    server_name admin.mindroom.chat;

    location / {
        proxy_pass http://localhost:3001;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_cache_bypass $http_upgrade;
    }
}

# Stripe Webhook Handler
server {
    listen 80;
    server_name webhooks.mindroom.chat;

    location /stripe {
        proxy_pass http://localhost:3007/webhook;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Important for Stripe webhooks
        proxy_buffering off;
        proxy_request_buffering off;
    }
}

# Dokku Provisioner API
server {
    listen 80;
    server_name api.mindroom.chat;

    location /provision {
        proxy_pass http://localhost:8002/api/v1/provision;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }

    location /health {
        proxy_pass http://localhost:8002/health;
    }
}
EOF

scp /tmp/nginx.conf root@$PLATFORM_IP:/etc/nginx/sites-available/mindroom-platform
ssh root@$PLATFORM_IP "ln -sf /etc/nginx/sites-available/mindroom-platform /etc/nginx/sites-enabled/"
ssh root@$PLATFORM_IP "nginx -t && systemctl reload nginx"

echo "✅ Nginx configured"
echo ""

# Step 4: Setup SSL certificates
echo "🔒 Step 4: Setting up SSL certificates..."
ssh root@$PLATFORM_IP "certbot certonly --nginx --non-interactive --agree-tos \
    --email ${ADMIN_EMAIL:-admin@mindroom.chat} \
    -d app.mindroom.chat -d admin.mindroom.chat \
    -d api.mindroom.chat -d webhooks.mindroom.chat" || true

echo ""

# Step 5: Health checks
echo "🏥 Step 5: Running health checks..."
echo ""

# Check each service
services=("app.mindroom.chat" "admin.mindroom.chat" "api.mindroom.chat/health" "webhooks.mindroom.chat/health")

for service in "${services[@]}"; do
    if curl -f -s "https://$service" > /dev/null 2>&1; then
        echo "✅ $service is healthy"
    else
        echo "⚠️ $service is not responding (might need SSL setup)"
    fi
done

echo ""
echo "🎉 Deployment complete!"
echo ""
echo "📝 Next steps:"
echo "1. Configure DNS to point to your servers:"
echo "   - *.mindroom.chat → $DOKKU_IP (for customer instances)"
echo "   - app.mindroom.chat → $PLATFORM_IP"
echo "   - admin.mindroom.chat → $PLATFORM_IP"
echo "   - api.mindroom.chat → $PLATFORM_IP"
echo "   - webhooks.mindroom.chat → $PLATFORM_IP"
echo ""
echo "2. Configure Stripe webhook endpoint:"
echo "   https://webhooks.mindroom.chat/stripe"
echo ""
echo "3. Test customer signup and provisioning:"
echo "   https://app.mindroom.chat"
echo ""
