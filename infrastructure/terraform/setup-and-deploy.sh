#!/bin/bash

# Quick setup and deployment script for MindRoom infrastructure
set -e

echo "🚀 MindRoom Infrastructure Setup"
echo "================================"
echo ""

# Check if terraform.tfvars exists
if [ ! -f terraform.tfvars ]; then
    echo "❌ terraform.tfvars not found!"
    echo ""
    echo "Create it with these required values:"
    echo ""
    cat << 'EOF'
# REQUIRED - Get these from your accounts:
hcloud_token = "YOUR_HETZNER_TOKEN"

# Porkbun DNS (get from https://porkbun.com/account/api)
porkbun_api_key = "pk1_..."
porkbun_secret_key = "sk1_..."

# Your domain
domain = "mindroom.chat"

# Supabase (from project settings)
supabase_url = "https://YOUR_PROJECT.supabase.co"
supabase_service_key = "eyJ..."

# Stripe (from dashboard)
stripe_secret_key = "sk_test_..."
stripe_webhook_secret = "whsec_..."

# SSH keys (generate if needed: ssh-keygen -t ed25519)
ssh_public_key_path = "~/.ssh/id_ed25519.pub"
dokku_provisioner_key_path = "~/.ssh/id_ed25519.pub"
EOF
    exit 1
fi

echo "✅ Configuration file found"
echo ""

# Initialize Terraform
echo "📦 Initializing Terraform..."
terraform init

echo ""
echo "🔍 Planning infrastructure..."
terraform plan -out=tfplan

echo ""
read -p "Do you want to apply these changes? (yes/no) " -r
if [[ ! $REPLY =~ ^yes$ ]]; then
    echo "Cancelled"
    exit 0
fi

echo ""
echo "🏗️ Creating infrastructure..."
terraform apply tfplan

echo ""
echo "✅ Infrastructure created!"
echo ""
echo "📝 Server Information:"
echo "====================="
terraform output -json | python3 -c "
import json, sys
data = json.load(sys.stdin)
print(f\"Dokku Server IP: {data.get('dokku_server_ip', {}).get('value', 'N/A')}\")
print(f\"Platform Server IP: {data.get('platform_server_ip', {}).get('value', 'N/A')}\")
print()
print('SSH Commands:')
print(f\"  Dokku: {data.get('ssh_command_dokku', {}).get('value', 'N/A')}\")
print(f\"  Platform: {data.get('ssh_command_platform', {}).get('value', 'N/A')}\")
"

echo ""
echo "⏳ Servers are running cloud-init scripts (10-15 minutes)..."
echo "   They will reboot automatically when ready."
echo ""
echo "📌 Next Steps:"
echo "1. Wait 10-15 minutes for cloud-init to complete"
echo "2. Test SSH access with the commands above"
echo "3. Deploy platform services:"
echo "   cd ../.. && ./scripts/deploy-platform.sh"
echo ""
echo "🌐 DNS Records Created:"
echo "  - mindroom.chat → Platform server"
echo "  - *.mindroom.chat → Dokku server (for customer instances)"
echo "  - app.mindroom.chat → Platform server"
echo "  - admin.mindroom.chat → Platform server"
echo "  - api.mindroom.chat → Platform server"
echo "  - webhooks.mindroom.chat → Platform server"
echo ""
echo "Your other DNS records remain untouched!"
