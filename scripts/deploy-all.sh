#!/bin/bash

# Complete MindRoom SaaS Platform Deployment Script
# This script handles the entire deployment process from infrastructure to services

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}🚀 MindRoom Complete Platform Deployment${NC}"
echo "=========================================="
echo ""

# Check prerequisites
echo -e "${YELLOW}📋 Checking prerequisites...${NC}"

if [ ! -f .env ]; then
    echo -e "${RED}❌ Error: .env file not found${NC}"
    echo "Please create a .env file with all required credentials"
    exit 1
fi

# Load environment variables
source .env

# Verify required environment variables
required_vars=(
    "HCLOUD_TOKEN"
    "SUPABASE_URL"
    "SUPABASE_SERVICE_KEY"
    "SUPABASE_DB_PASSWORD"
    "STRIPE_SECRET_KEY"
    "STRIPE_PUBLISHABLE_KEY"
    "GITEA_TOKEN"
    "GITEA_USER"
    "GITEA_URL"
    "REGISTRY"
)

# Optional DNS provider credentials
if [ -n "$PORKBUN_API_KEY" ] && [ -n "$PORKBUN_SECRET_KEY" ]; then
    echo "DNS Provider: Porkbun (configured)"
else
    echo -e "${YELLOW}⚠️  DNS Provider: Not configured (will use IP addresses only)${NC}"
fi

missing_vars=()
for var in "${required_vars[@]}"; do
    if [ -z "${!var}" ]; then
        missing_vars+=($var)
    fi
done

if [ ${#missing_vars[@]} -ne 0 ]; then
    echo -e "${RED}❌ Missing required environment variables:${NC}"
    printf '%s\n' "${missing_vars[@]}"
    exit 1
fi

echo -e "${GREEN}✅ All required environment variables found${NC}"
echo ""

# Step 1: Deploy Infrastructure
echo -e "${YELLOW}📦 Step 1: Deploying Infrastructure with Terraform${NC}"
echo "=================================================="

cd infrastructure/terraform

# Initialize Terraform
echo "Initializing Terraform..."
terraform init -upgrade

# Plan the deployment
echo "Planning infrastructure deployment..."
terraform plan -out=tfplan

# Apply the infrastructure
echo "Deploying infrastructure..."
terraform apply tfplan
rm tfplan

# Get the deployed server IPs
DOKKU_IP=$(terraform output -raw dokku_server_ip)
PLATFORM_IP=$(terraform output -raw platform_server_ip)
DOMAIN=$(terraform output -raw domain_name 2>/dev/null || echo "mindroom.chat")

cd ../..

echo -e "${GREEN}✅ Infrastructure deployed successfully${NC}"
echo "  Dokku Server: $DOKKU_IP"
echo "  Platform Server: $PLATFORM_IP"
echo "  Domain: $DOMAIN"
echo ""

# Step 2: Setup SSH keys for deployment
echo -e "${YELLOW}🔑 Step 2: Setting up SSH access${NC}"
echo "================================="

# Add servers to known hosts
ssh-keyscan -H $DOKKU_IP >> ~/.ssh/known_hosts 2>/dev/null
ssh-keyscan -H $PLATFORM_IP >> ~/.ssh/known_hosts 2>/dev/null

echo -e "${GREEN}✅ SSH access configured${NC}"
echo ""

# Step 3: Run Database Migrations
echo -e "${YELLOW}🗄️  Step 3: Running Database Migrations${NC}"
echo "======================================"

# Use the migration script to run migrations via SSH
bash scripts/run-migrations.sh

echo -e "${GREEN}✅ Database migrations completed${NC}"
echo ""

# Step 4: Build and Push Docker Images
echo -e "${YELLOW}🐳 Step 4: Building and Pushing Docker Images${NC}"
echo "============================================="

# Authenticate with Gitea registry
echo "Authenticating with Gitea registry..."
echo "$GITEA_TOKEN" | docker login $GITEA_URL -u $GITEA_USER --password-stdin

# Build and push each service
services=(
    "customer-portal:deploy/Dockerfile.customer-portal"
    "admin-dashboard:deploy/Dockerfile.admin-dashboard"
    "stripe-handler:deploy/Dockerfile.stripe-handler"
    "dokku-provisioner:deploy/Dockerfile.dokku-provisioner"
)

for service_def in "${services[@]}"; do
    IFS=':' read -r service dockerfile <<< "$service_def"
    tag="${REGISTRY}/${service}:${DOCKER_ARCH:-amd64}"

    echo -e "${YELLOW}Building $service...${NC}"
    docker build -f $dockerfile -t $tag .

    echo -e "${YELLOW}Pushing $service...${NC}"
    docker push $tag
done

echo -e "${GREEN}✅ All Docker images built and pushed${NC}"
echo ""

# Step 5: Deploy Platform Services
echo -e "${YELLOW}🚀 Step 5: Deploying Platform Services${NC}"
echo "======================================"

# Deploy services to platform server
echo "Deploying to platform server..."

ssh root@$PLATFORM_IP << 'EOF'
# Create docker network if it doesn't exist
docker network create platform 2>/dev/null || true

# Stop existing containers
docker stop customer-portal admin-dashboard stripe-handler dokku-provisioner 2>/dev/null || true
docker rm customer-portal admin-dashboard stripe-handler dokku-provisioner 2>/dev/null || true

# Pull latest images
source /root/.env
echo "$GITEA_TOKEN" | docker login $GITEA_URL -u $GITEA_USER --password-stdin

docker pull ${REGISTRY}/customer-portal:${DOCKER_ARCH}
docker pull ${REGISTRY}/admin-dashboard:${DOCKER_ARCH}
docker pull ${REGISTRY}/stripe-handler:${DOCKER_ARCH}
docker pull ${REGISTRY}/dokku-provisioner:${DOCKER_ARCH}

# Run Customer Portal
docker run -d \
  --name customer-portal \
  --network platform \
  --restart unless-stopped \
  -p 3000:3000 \
  --env-file /root/.env \
  ${REGISTRY}/customer-portal:${DOCKER_ARCH}

# Run Admin Dashboard
docker run -d \
  --name admin-dashboard \
  --network platform \
  --restart unless-stopped \
  -p 3001:3001 \
  --env-file /root/.env \
  ${REGISTRY}/admin-dashboard:${DOCKER_ARCH}

# Run Stripe Handler
docker run -d \
  --name stripe-handler \
  --network platform \
  --restart unless-stopped \
  -p 8001:8001 \
  --env-file /root/.env \
  ${REGISTRY}/stripe-handler:${DOCKER_ARCH}

# Run Dokku Provisioner
docker run -d \
  --name dokku-provisioner \
  --network platform \
  --restart unless-stopped \
  -p 8002:8002 \
  -v /root/.ssh:/root/.ssh:ro \
  --env-file /root/.env \
  ${REGISTRY}/dokku-provisioner:${DOCKER_ARCH}

# Check status
docker ps
EOF

echo -e "${GREEN}✅ Platform services deployed${NC}"
echo ""

# Step 6: Configure Nginx on Platform Server
echo -e "${YELLOW}🌐 Step 6: Configuring Nginx${NC}"
echo "============================"

ssh root@$PLATFORM_IP << EOF
# Install nginx if not present
apt-get update && apt-get install -y nginx certbot python3-certbot-nginx

# Configure nginx for each service
cat > /etc/nginx/sites-available/platform << 'NGINX'
server {
    listen 80;
    server_name portal.$DOMAIN;

    location / {
        proxy_pass http://localhost:3000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}

server {
    listen 80;
    server_name admin.$DOMAIN;

    location / {
        proxy_pass http://localhost:3001;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}

server {
    listen 80;
    server_name api.$DOMAIN;

    location /stripe {
        proxy_pass http://localhost:8001;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    location /provision {
        proxy_pass http://localhost:8002;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
NGINX

# Enable the site
ln -sf /etc/nginx/sites-available/platform /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx

# Get SSL certificates (optional, will fail if DNS not propagated yet)
certbot --nginx -d portal.$DOMAIN -d admin.$DOMAIN -d api.$DOMAIN --non-interactive --agree-tos --email admin@$DOMAIN || true
EOF

echo -e "${GREEN}✅ Nginx configured${NC}"
echo ""

# Step 7: Setup Stripe Products
echo -e "${YELLOW}💳 Step 7: Setting up Stripe Products${NC}"
echo "====================================="

cd scripts
node setup-stripe-products.js
cd ..

echo -e "${GREEN}✅ Stripe products configured${NC}"
echo ""

# Step 8: Verify Deployment
echo -e "${YELLOW}🔍 Step 8: Verifying Deployment${NC}"
echo "================================"

# Check if services are running
echo "Checking platform services..."
ssh root@$PLATFORM_IP "docker ps --format 'table {{.Names}}\t{{.Status}}'"

# Test API endpoints
echo ""
echo "Testing API endpoints..."
curl -s -o /dev/null -w "Customer Portal: %{http_code}\n" http://$PLATFORM_IP:3000/health || echo "Customer Portal: Not responding"
curl -s -o /dev/null -w "Admin Dashboard: %{http_code}\n" http://$PLATFORM_IP:3001/health || echo "Admin Dashboard: Not responding"
curl -s -o /dev/null -w "Stripe Handler: %{http_code}\n" http://$PLATFORM_IP:8001/health || echo "Stripe Handler: Not responding"
curl -s -o /dev/null -w "Dokku Provisioner: %{http_code}\n" http://$PLATFORM_IP:8002/health || echo "Dokku Provisioner: Not responding"

echo ""
echo -e "${GREEN}🎉 Deployment Complete!${NC}"
echo "======================="
echo ""
echo "Access your platform at:"
echo "  Customer Portal: http://portal.$DOMAIN (or http://$PLATFORM_IP:3000)"
echo "  Admin Dashboard: http://admin.$DOMAIN (or http://$PLATFORM_IP:3001)"
echo "  API Endpoint: http://api.$DOMAIN"
echo ""
echo "Dokku Server: ssh dokku@$DOKKU_IP"
echo "Platform Server: ssh root@$PLATFORM_IP"
echo ""
echo "Next steps:"
echo "1. Configure Stripe webhook endpoint: http://api.$DOMAIN/stripe/webhook"
echo "2. Test customer signup flow at http://portal.$DOMAIN"
echo "3. Monitor deployments at http://admin.$DOMAIN"
