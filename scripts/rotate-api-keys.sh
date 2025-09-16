#!/bin/bash
# MindRoom API Key Rotation Helper
# This script helps rotate API keys safely

set -euo pipefail

echo "🔐 MindRoom API Key Rotation Helper"
echo "===================================="
echo ""
echo "⚠️  IMPORTANT: This script helps you rotate API keys safely."
echo "You must manually generate new keys from each provider first!"
echo ""

# Check if we're in the right directory
if [ ! -f "pyproject.toml" ] || [ ! -d "saas-platform" ]; then
    echo "❌ Error: Run this script from the MindRoom project root"
    exit 1
fi

# Create secure temporary directory
SECURE_DIR=$(mktemp -d /tmp/mindroom-secrets.XXXXXX)
chmod 700 "$SECURE_DIR"
echo "📁 Created secure temporary directory: $SECURE_DIR"

# Create template for new secrets
cat > "$SECURE_DIR/new-secrets.env" << 'EOF'
# ⚠️  REPLACE THESE WITH YOUR NEW API KEYS
# Generate new keys from:
# - OpenAI: https://platform.openai.com/api-keys
# - Anthropic: https://console.anthropic.com/account/keys
# - Google: https://console.cloud.google.com/apis/credentials
# - OpenRouter: https://openrouter.ai/keys
# - Deepseek: https://platform.deepseek.com/api_keys
# - Stripe: https://dashboard.stripe.com/apikeys
# - Supabase: Project Settings > API

# AI Provider Keys
OPENAI_API_KEY=sk-proj-REPLACE-WITH-NEW-KEY
ANTHROPIC_API_KEY=sk-ant-REPLACE-WITH-NEW-KEY
GOOGLE_API_KEY=REPLACE-WITH-NEW-KEY
OPENROUTER_API_KEY=sk-or-v1-REPLACE-WITH-NEW-KEY
DEEPSEEK_API_KEY=sk-REPLACE-WITH-NEW-KEY

# Infrastructure Keys
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_ANON_KEY=REPLACE-WITH-NEW-KEY
SUPABASE_SERVICE_KEY=REPLACE-WITH-NEW-KEY

# Payment Keys
STRIPE_PUBLISHABLE_KEY=pk_live_REPLACE-WITH-NEW-KEY
STRIPE_SECRET_KEY=sk_live_REPLACE-WITH-NEW-KEY
STRIPE_WEBHOOK_SECRET=whsec_REPLACE-WITH-NEW-KEY

# Internal Keys (auto-generated)
PROVISIONER_API_KEY=$(openssl rand -hex 32)

# OAuth Keys
GOOGLE_CLIENT_ID=REPLACE-WITH-NEW-ID
GOOGLE_CLIENT_SECRET=REPLACE-WITH-NEW-SECRET
EOF

echo ""
echo "📝 Template created at: $SECURE_DIR/new-secrets.env"
echo ""
echo "NEXT STEPS:"
echo "1. Edit $SECURE_DIR/new-secrets.env with your NEW API keys"
echo "2. Run: ./scripts/apply-rotated-keys.sh $SECURE_DIR/new-secrets.env"
echo "3. Immediately revoke OLD keys in each provider's dashboard"
echo "4. Run: rm -rf $SECURE_DIR (to clean up)"
echo ""
echo "⚠️  Security Notes:"
echo "- Never commit the new-secrets.env file"
echo "- Delete it immediately after applying"
echo "- Ensure all team members update their local .env files"
