#!/bin/bash
# MindRoom API Key Rotation Helper

set -euo pipefail

# Create secure temporary directory
SECURE_DIR=$(mktemp -d /tmp/mindroom-secrets.XXXXXX)
chmod 700 "$SECURE_DIR"

# Create template for new secrets
cat > "$SECURE_DIR/new-secrets.env" << EOF
OPENAI_API_KEY=sk-proj-REPLACE-WITH-NEW-KEY
ANTHROPIC_API_KEY=sk-ant-REPLACE-WITH-NEW-KEY
GOOGLE_API_KEY=REPLACE-WITH-NEW-KEY
OPENROUTER_API_KEY=sk-or-v1-REPLACE-WITH-NEW-KEY
DEEPSEEK_API_KEY=sk-REPLACE-WITH-NEW-KEY
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_ANON_KEY=REPLACE-WITH-NEW-KEY
SUPABASE_SERVICE_KEY=REPLACE-WITH-NEW-KEY
STRIPE_PUBLISHABLE_KEY=pk_live_REPLACE-WITH-NEW-KEY
STRIPE_SECRET_KEY=sk_live_REPLACE-WITH-NEW-KEY
STRIPE_WEBHOOK_SECRET=whsec_REPLACE-WITH-NEW-KEY
PROVISIONER_API_KEY=$(openssl rand -hex 32)
GOOGLE_CLIENT_ID=REPLACE-WITH-NEW-ID
GOOGLE_CLIENT_SECRET=REPLACE-WITH-NEW-SECRET
EOF

echo "Edit: $SECURE_DIR/new-secrets.env"
echo "Apply: ./scripts/apply-rotated-keys.sh $SECURE_DIR/new-secrets.env"
