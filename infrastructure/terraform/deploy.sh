#!/bin/bash
set -e

# MindRoom SaaS Infrastructure Deployment Script
echo "🚀 MindRoom SaaS Infrastructure Deployment"
echo "=========================================="

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Check prerequisites
check_command() {
    if ! command -v $1 &> /dev/null; then
        echo -e "${RED}❌ $1 is not installed${NC}"
        echo "Please install $1 first"
        exit 1
    else
        echo -e "${GREEN}✓ $1 found${NC}"
    fi
}

echo -e "\n${YELLOW}Checking prerequisites...${NC}"
check_command terraform
check_command ssh-keygen
check_command curl

# Generate SSH keys if needed
echo -e "\n${YELLOW}Setting up SSH keys...${NC}"

if [ ! -f ~/.ssh/id_rsa ]; then
    echo "Generating admin SSH key..."
    ssh-keygen -t rsa -b 4096 -f ~/.ssh/id_rsa -N ""
    echo -e "${GREEN}✓ Admin SSH key generated${NC}"
else
    echo -e "${GREEN}✓ Admin SSH key exists${NC}"
fi

if [ ! -d ssh ]; then
    mkdir -p ssh
fi

if [ ! -f ssh/dokku_provisioner ]; then
    echo "Generating Dokku provisioner SSH key..."
    ssh-keygen -t rsa -b 4096 -f ssh/dokku_provisioner -N ""
    echo -e "${GREEN}✓ Dokku provisioner key generated${NC}"
else
    echo -e "${GREEN}✓ Dokku provisioner key exists${NC}"
fi

# Check for terraform.tfvars
if [ ! -f terraform.tfvars ]; then
    echo -e "\n${RED}❌ No terraform.tfvars file found${NC}"
    echo ""
    echo "Please create terraform.tfvars with:"
    echo "  - hcloud_token: Your Hetzner Cloud API token"
    echo "  - supabase_url: Your Supabase project URL"
    echo "  - supabase_service_key: Your Supabase service key"
    echo "  - stripe_secret_key: Your Stripe secret key"
    echo "  - stripe_webhook_secret: Your Stripe webhook secret"
    echo "  - admin_ips: List of allowed SSH IPs"
    echo ""
    exit 1
fi

# Initialize Terraform
echo -e "\n${YELLOW}Initializing Terraform...${NC}"
terraform init

# Show plan
echo -e "\n${YELLOW}Planning infrastructure changes...${NC}"
terraform plan -out=tfplan

# Ask for confirmation
echo -e "\n${YELLOW}Ready to deploy infrastructure${NC}"
echo "This will create:"
echo "  - 2 Hetzner Cloud servers"
echo "  - 2 storage volumes"
echo "  - Private network"
echo "  - Firewall rules"
echo ""
read -p "Continue with deployment? (yes/no): " confirm

if [ "$confirm" != "yes" ]; then
    echo "Deployment cancelled"
    exit 0
fi

# Apply Terraform
echo -e "\n${YELLOW}Deploying infrastructure...${NC}"
terraform apply tfplan

# Save outputs
echo -e "\n${YELLOW}Saving outputs...${NC}"
terraform output -json > outputs.json
echo -e "${GREEN}✓ Outputs saved to outputs.json${NC}"

# Extract IPs
DOKKU_IP=$(terraform output -raw dokku_server_ip)
PLATFORM_IP=$(terraform output -raw platform_server_ip)

# Display next steps
echo -e "\n${GREEN}🎉 Infrastructure deployed successfully!${NC}"
echo ""
echo "Server IPs:"
echo "  Dokku:    $DOKKU_IP"
echo "  Platform: $PLATFORM_IP"
echo ""
echo -e "${YELLOW}Next steps:${NC}"
echo ""
echo "1. Configure DNS records for mindroom.chat:"
echo "   A     mindroom.chat              $PLATFORM_IP"
echo "   A     *.mindroom.chat            $DOKKU_IP"
echo "   A     app.mindroom.chat          $PLATFORM_IP"
echo "   A     admin.mindroom.chat        $PLATFORM_IP"
echo "   A     api.mindroom.chat          $PLATFORM_IP"
echo "   A     webhooks.mindroom.chat     $PLATFORM_IP"
echo ""
echo "2. Wait for DNS propagation (5-30 minutes)"
echo ""
echo "3. Complete SSL setup (if needed):"
echo "   ssh root@$PLATFORM_IP"
echo "   certbot certonly --nginx --non-interactive --agree-tos \\"
echo "     --email admin@mindroom.chat \\"
echo "     -d mindroom.chat -d app.mindroom.chat -d admin.mindroom.chat \\"
echo "     -d api.mindroom.chat -d webhooks.mindroom.chat"
echo ""
echo "4. Deploy platform services code to:"
echo "   /mnt/platform-data/stripe-handler"
echo "   /mnt/platform-data/dokku-provisioner"
echo "   /mnt/platform-data/customer-portal"
echo "   /mnt/platform-data/admin-dashboard"
echo ""
echo "5. Configure Stripe webhook:"
echo "   URL: https://webhooks.mindroom.chat/stripe"
echo ""
echo "6. Run Supabase migrations"
echo ""
echo -e "${GREEN}SSH Commands:${NC}"
echo "  ssh root@$DOKKU_IP     # Dokku server"
echo "  ssh root@$PLATFORM_IP  # Platform server"
echo ""
echo "Admin Dashboard: https://admin.mindroom.chat"
echo "  Username: admin"
echo "  Password: MindRoom2024!"
echo ""
echo "For detailed instructions, see README.md"
