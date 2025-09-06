#!/bin/bash

# Complete cleanup script for MindRoom SaaS Platform
# This removes all infrastructure and services

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${RED}⚠️  MindRoom Platform Complete Cleanup${NC}"
echo "======================================"
echo ""
echo "This will destroy:"
echo "  - All Hetzner servers"
echo "  - All DNS records"
echo "  - All Docker containers on servers"
echo "  - Terraform state"
echo ""

read -p "Are you sure you want to continue? (yes/no): " confirm
if [ "$confirm" != "yes" ]; then
    echo "Cleanup cancelled"
    exit 0
fi

# Load environment variables
if [ -f .env ]; then
    if command -v uvx &> /dev/null; then
        # Export all env vars for child processes when using uvx
        set -a
        eval "$(uvx --from 'python-dotenv[cli]' dotenv list --format shell)"
        set +a
    else
        source .env
    fi
fi

# Step 1: Destroy Infrastructure with Terraform
echo -e "${YELLOW}🗑️  Step 1: Destroying Infrastructure${NC}"
echo "===================================="

if [ -d saas-platform/infrastructure/terraform ]; then
    cd saas-platform/infrastructure/terraform

    if [ -f terraform.tfstate ]; then
        echo "Running terraform destroy..."
        terraform destroy -auto-approve || true

        echo "Cleaning up Terraform files..."
        rm -f terraform.tfstate terraform.tfstate.backup .terraform.lock.hcl
        rm -rf .terraform
    else
        echo "No Terraform state found"
    fi

    cd ../../..
fi

echo -e "${GREEN}✅ Infrastructure destroyed${NC}"
echo ""

# Step 2: Clean up local Docker images
echo -e "${YELLOW}🐳 Step 2: Cleaning up Docker images${NC}"
echo "===================================="

if [ -n "$REGISTRY" ]; then
    echo "Removing local Docker images..."
    docker rmi ${REGISTRY}/customer-portal:${DOCKER_ARCH:-amd64} 2>/dev/null || true
    docker rmi ${REGISTRY}/admin-dashboard:${DOCKER_ARCH:-amd64} 2>/dev/null || true
    docker rmi ${REGISTRY}/stripe-handler:${DOCKER_ARCH:-amd64} 2>/dev/null || true
    docker rmi ${REGISTRY}/instance-provisioner:${DOCKER_ARCH:-amd64} 2>/dev/null || true
fi

echo -e "${GREEN}✅ Docker images cleaned up${NC}"
echo ""

# Step 3: Clean up temporary files
echo -e "${YELLOW}📁 Step 3: Cleaning up temporary files${NC}"
echo "======================================"

# Remove any generated files
rm -f scripts/terraform.tfplan 2>/dev/null || true
rm -rf deploy/tmp 2>/dev/null || true
rm -rf deploy/logs 2>/dev/null || true

echo -e "${GREEN}✅ Temporary files cleaned up${NC}"
echo ""

echo -e "${GREEN}🎉 Cleanup Complete!${NC}"
echo "===================="
echo ""
echo "All infrastructure and services have been removed."
echo "To redeploy, run: ./scripts/deploy-all.sh"
