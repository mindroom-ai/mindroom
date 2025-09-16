#!/bin/bash
# Apply rotated API keys to Kubernetes and local environment

set -euo pipefail

if [ $# -ne 1 ]; then
    echo "Usage: $0 <path-to-new-secrets.env>"
    exit 1
fi

SECRETS_FILE="$1"

if [ ! -f "$SECRETS_FILE" ]; then
    echo "❌ Error: Secrets file not found: $SECRETS_FILE"
    exit 1
fi

echo "🔐 Applying Rotated API Keys"
echo "============================"

# Load the new secrets
set -a
source "$SECRETS_FILE"
set +a

# Check for Kubernetes access
if ! kubectl cluster-info > /dev/null 2>&1; then
    echo "⚠️  Warning: kubectl not configured. Skipping Kubernetes updates."
    echo "Set KUBECONFIG or configure kubectl to update cluster secrets."
    SKIP_K8S=true
else
    SKIP_K8S=false
fi

# Update Kubernetes secrets if accessible
if [ "$SKIP_K8S" = false ]; then
    echo "📦 Updating Kubernetes secrets..."

    # Create namespace if it doesn't exist
    kubectl create namespace mindroom-secrets --dry-run=client -o yaml | kubectl apply -f -
    kubectl create namespace mindroom-staging --dry-run=client -o yaml | kubectl apply -f -
    kubectl create namespace mindroom-instances --dry-run=client -o yaml | kubectl apply -f -

    # Delete existing secrets (if any) and create new ones
    kubectl delete secret api-keys --namespace=mindroom-staging --ignore-not-found
    kubectl delete secret platform-secrets --namespace=mindroom-staging --ignore-not-found

    # Create API keys secret
    kubectl create secret generic api-keys \
        --from-literal=openai-api-key="$OPENAI_API_KEY" \
        --from-literal=anthropic-api-key="$ANTHROPIC_API_KEY" \
        --from-literal=google-api-key="$GOOGLE_API_KEY" \
        --from-literal=openrouter-api-key="$OPENROUTER_API_KEY" \
        --from-literal=deepseek-api-key="$DEEPSEEK_API_KEY" \
        --namespace=mindroom-staging

    # Create platform secrets
    kubectl create secret generic platform-secrets \
        --from-literal=supabase-url="$SUPABASE_URL" \
        --from-literal=supabase-anon-key="$SUPABASE_ANON_KEY" \
        --from-literal=supabase-service-key="$SUPABASE_SERVICE_KEY" \
        --from-literal=stripe-publishable-key="$STRIPE_PUBLISHABLE_KEY" \
        --from-literal=stripe-secret-key="$STRIPE_SECRET_KEY" \
        --from-literal=stripe-webhook-secret="$STRIPE_WEBHOOK_SECRET" \
        --from-literal=provisioner-api-key="$PROVISIONER_API_KEY" \
        --from-literal=google-client-id="$GOOGLE_CLIENT_ID" \
        --from-literal=google-client-secret="$GOOGLE_CLIENT_SECRET" \
        --namespace=mindroom-staging

    echo "✅ Kubernetes secrets updated"
fi

# Update local .env file (backup existing)
if [ -f ".env" ]; then
    cp .env .env.backup.$(date +%Y%m%d_%H%M%S)
    echo "📝 Backed up existing .env file"
fi

# Create new .env file from template
cat > .env << EOF
# MindRoom Environment Configuration
# Generated: $(date)
# ⚠️  DO NOT COMMIT THIS FILE

# AI Provider Keys
OPENAI_API_KEY=$OPENAI_API_KEY
ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY
GOOGLE_API_KEY=$GOOGLE_API_KEY
OPENROUTER_API_KEY=$OPENROUTER_API_KEY
DEEPSEEK_API_KEY=$DEEPSEEK_API_KEY

# Supabase Configuration
SUPABASE_URL=$SUPABASE_URL
SUPABASE_ANON_KEY=$SUPABASE_ANON_KEY
SUPABASE_SERVICE_KEY=$SUPABASE_SERVICE_KEY

# Stripe Configuration
STRIPE_PUBLISHABLE_KEY=$STRIPE_PUBLISHABLE_KEY
STRIPE_SECRET_KEY=$STRIPE_SECRET_KEY
STRIPE_WEBHOOK_SECRET=$STRIPE_WEBHOOK_SECRET

# Internal Configuration
PROVISIONER_API_KEY=$PROVISIONER_API_KEY

# OAuth Configuration
GOOGLE_CLIENT_ID=$GOOGLE_CLIENT_ID
GOOGLE_CLIENT_SECRET=$GOOGLE_CLIENT_SECRET

# Other existing configuration (preserved from backup if exists)
EOF

echo "✅ Local .env file updated"

# Update saas-platform .env if it exists
if [ -d "saas-platform" ]; then
    if [ -f "saas-platform/.env" ]; then
        cp saas-platform/.env saas-platform/.env.backup.$(date +%Y%m%d_%H%M%S)
    fi
    cp .env saas-platform/.env
    echo "✅ Updated saas-platform/.env"
fi

echo ""
echo "🎯 Next Steps:"
echo "1. ⚠️  IMMEDIATELY revoke old API keys in each provider's dashboard"
echo "2. Delete the secrets file: rm -f $SECRETS_FILE"
echo "3. Restart services to pick up new configuration:"
echo "   - Kubernetes: kubectl rollout restart deployment -n mindroom-staging"
echo "   - Docker: docker-compose restart"
echo "4. Test that services are working with new keys"
echo ""
echo "✅ API key rotation applied successfully!"
