#!/bin/bash

# Post-Terraform Deployment Script
# This script runs after Terraform creates the infrastructure
# It deploys the actual Docker images to the platform server

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}🚀 Post-Terraform Deployment${NC}"
echo "=================================="
echo ""

# Get server IPs from Terraform
if [ -z "$PLATFORM_IP" ] || [ -z "$DOKKU_IP" ]; then
    echo "Getting server IPs from Terraform..."
    cd infrastructure/terraform 2>/dev/null || cd saas-platform/infrastructure/terraform
    PLATFORM_IP=$(terraform output -raw platform_server_ip 2>/dev/null || echo "")
    DOKKU_IP=$(terraform output -raw dokku_server_ip 2>/dev/null || echo "")
    cd - > /dev/null

    if [ -z "$PLATFORM_IP" ] || [ -z "$DOKKU_IP" ]; then
        echo -e "${RED}❌ Could not get server IPs from Terraform${NC}"
        echo "Please set PLATFORM_IP and DOKKU_IP environment variables"
        exit 1
    fi
fi

echo "Platform Server: $PLATFORM_IP"
echo "Dokku Server: $DOKKU_IP"
echo ""

# Step 1: Transfer Docker images to platform server
echo -e "${YELLOW}📦 Step 1: Transferring Docker Images${NC}"
echo "======================================"

# Function to transfer image
transfer_image() {
    local service=$1
    local tag="${REGISTRY}/${service}:${DOCKER_ARCH:-amd64}"

    echo -e "${YELLOW}Transferring $service...${NC}"

    # Check if image exists locally
    if ! docker image inspect "$tag" > /dev/null 2>&1; then
        echo -e "${RED}Image $tag not found locally. Building...${NC}"
        docker build -f saas-platform/Dockerfile.$service -t "$tag" saas-platform/
    fi

    # Transfer image
    docker save "$tag" | gzip | ssh root@$PLATFORM_IP "gunzip | docker load"
    echo -e "${GREEN}✅ $service transferred${NC}"
}

# Transfer all service images
services=("customer-portal" "admin-dashboard" "stripe-handler" "dokku-provisioner")
for service in "${services[@]}"; do
    transfer_image "$service"
done

echo ""

# Step 2: Set up SSH keys for Dokku access
echo -e "${YELLOW}🔑 Step 2: Setting up SSH keys${NC}"
echo "================================"

# Wait for platform SSH key to be generated if needed
for i in {1..10}; do
    PLATFORM_KEY=$(ssh root@$PLATFORM_IP "cat /opt/platform/dokku-ssh-key.pub" 2>/dev/null)
    if [ -n "$PLATFORM_KEY" ]; then
        break
    fi
    echo "Waiting for SSH key generation... ($i/10)"
    sleep 2
done

if [ -n "$PLATFORM_KEY" ]; then
    # Add key to Dokku server if not already present
    ssh root@$DOKKU_IP "grep -q \"$PLATFORM_KEY\" /root/.ssh/authorized_keys 2>/dev/null || echo '$PLATFORM_KEY' >> /root/.ssh/authorized_keys"
    echo -e "${GREEN}✅ SSH key added to Dokku server${NC}"
else
    echo -e "${RED}❌ Platform SSH key not found after waiting${NC}"
    exit 1
fi

echo ""

# Step 3: Copy environment file to platform server
echo -e "${YELLOW}📋 Step 3: Setting up environment${NC}"
echo "================================="

# Copy .env file to platform server and fix format
scp -q .env root@$PLATFORM_IP:/root/.env.tmp
ssh root@$PLATFORM_IP "sed 's/^export //' /root/.env.tmp > /root/.env && rm /root/.env.tmp"
# Add DOKKU_USER=root to ensure dokku-provisioner connects as root
ssh root@$PLATFORM_IP "echo 'DOKKU_USER=root' >> /root/.env"
# Also copy to /opt/platform for the systemd service
ssh root@$PLATFORM_IP "cp /root/.env /opt/platform/.env"
echo -e "${GREEN}✅ Environment file copied and formatted${NC}"
echo ""

# Step 4: Deploy services on platform server
echo -e "${YELLOW}🚀 Step 4: Deploying Services${NC}"
echo "============================="

ssh root@$PLATFORM_IP << 'DEPLOY_SCRIPT'
# Create docker network if it doesn't exist
docker network create platform 2>/dev/null || true

# Stop any existing placeholder or old containers
docker stop placeholder customer-portal admin-dashboard stripe-handler dokku-provisioner 2>/dev/null || true
docker rm placeholder customer-portal admin-dashboard stripe-handler dokku-provisioner 2>/dev/null || true

# Source environment
source /root/.env

# Run Customer Portal
docker run -d \
  --name customer-portal \
  --network platform \
  --restart unless-stopped \
  -p 3000:3000 \
  --env-file /root/.env \
  git.nijho.lt/basnijholt/customer-portal:amd64

# Run Admin Dashboard (serves on port 80, mapped to 3001)
docker run -d \
  --name admin-dashboard \
  --network platform \
  --restart unless-stopped \
  -p 3001:80 \
  --env-file /root/.env \
  git.nijho.lt/basnijholt/admin-dashboard:amd64

# Run Stripe Handler (service runs on port 3005 internally)
docker run -d \
  --name stripe-handler \
  --network platform \
  --restart unless-stopped \
  -p 3002:3005 \
  --env-file /root/.env \
  git.nijho.lt/basnijholt/stripe-handler:amd64

# Run Dokku Provisioner (service runs on port 8002 internally)
# Note: We need to copy the SSH key into the container with correct permissions
# because the container runs as nodejs user and can't read root-owned files
docker run -d \
  --name dokku-provisioner \
  --network platform \
  --restart unless-stopped \
  -p 3003:8002 \
  --env-file /root/.env \
  -e DOKKU_SSH_KEY_PATH=/app/dokku-key \
  -e DOKKU_USER=root \
  git.nijho.lt/basnijholt/dokku-provisioner:amd64

# Copy SSH key into container with correct permissions
docker cp /opt/platform/dokku-ssh-key dokku-provisioner:/app/dokku-key
docker exec -u root dokku-provisioner sh -c 'chmod 600 /app/dokku-key && chown nodejs:nodejs /app/dokku-key'
docker restart dokku-provisioner

# Fix any nginx config issues (from cloud-init double $$ escaping)
find /etc/nginx -type f \( -name '*.conf' -o -name 'default' -o -name 'platform-services' \) -exec sed -i 's/\$\$/\$/g' {} \;

# Restart Nginx to ensure configs are loaded
nginx -t && (systemctl is-active nginx && systemctl reload nginx || systemctl start nginx)

# Check status
echo ""
echo "Container Status:"
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
DEPLOY_SCRIPT

echo ""
echo -e "${GREEN}✅ Services deployed successfully!${NC}"
echo ""

# Step 5: Verify deployment
echo -e "${YELLOW}🔍 Step 5: Verifying Deployment${NC}"
echo "================================"

# Test endpoints
echo "Testing service endpoints..."
echo ""

# Test customer portal
response=$(curl -s -o /dev/null -w "%{http_code}" http://app.mindroom.chat || echo "000")
if [ "$response" = "200" ]; then
    echo -e "${GREEN}✅ Customer Portal: http://app.mindroom.chat (OK)${NC}"
else
    echo -e "${RED}❌ Customer Portal: http://app.mindroom.chat (Failed - $response)${NC}"
fi

# Test admin dashboard
response=$(curl -s -o /dev/null -w "%{http_code}" http://admin.mindroom.chat || echo "000")
if [ "$response" = "200" ]; then
    echo -e "${GREEN}✅ Admin Dashboard: http://admin.mindroom.chat (OK)${NC}"
else
    echo -e "${RED}❌ Admin Dashboard: http://admin.mindroom.chat (Failed - $response)${NC}"
fi

# Test API endpoint
response=$(curl -s -o /dev/null -w "%{http_code}" http://api.mindroom.chat/health || echo "000")
if [ "$response" = "200" ] || [ "$response" = "404" ]; then
    echo -e "${GREEN}✅ API Endpoint: http://api.mindroom.chat (OK)${NC}"
else
    echo -e "${YELLOW}⚠️  API Endpoint: http://api.mindroom.chat (May need configuration)${NC}"
fi

echo ""
echo -e "${GREEN}🎉 Post-deployment complete!${NC}"
echo ""
echo "Next steps:"
echo "1. Configure Stripe webhook endpoint in Stripe Dashboard:"
echo "   Webhook URL: http://webhooks.mindroom.chat/stripe/webhook"
echo "2. Test customer signup at http://app.mindroom.chat"
echo "3. Access admin dashboard at http://admin.mindroom.chat"
echo "4. Monitor logs: ssh root@$PLATFORM_IP docker logs -f <service-name>"
