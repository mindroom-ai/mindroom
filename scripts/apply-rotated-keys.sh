#!/bin/bash
# Apply rotated API keys to Kubernetes and local environment

set -euo pipefail

if [ $# -ne 1 ]; then
    echo "Usage: $0 <path-to-new-secrets.env>" >&2
    exit 1
fi

SECRETS_FILE="$1"

# Load the new secrets
set -a
source "$SECRETS_FILE"
set +a

# Update Kubernetes secrets
if kubectl cluster-info > /dev/null 2>&1; then

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

fi

# Update local .env file (backup existing)
if [ -f ".env" ]; then
    cp .env .env.backup.$(date +%Y%m%d_%H%M%S)
fi

# Create new .env file
cat > .env << EOF
OPENAI_API_KEY=$OPENAI_API_KEY
ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY
GOOGLE_API_KEY=$GOOGLE_API_KEY
OPENROUTER_API_KEY=$OPENROUTER_API_KEY
DEEPSEEK_API_KEY=$DEEPSEEK_API_KEY
SUPABASE_URL=$SUPABASE_URL
SUPABASE_ANON_KEY=$SUPABASE_ANON_KEY
SUPABASE_SERVICE_KEY=$SUPABASE_SERVICE_KEY
STRIPE_PUBLISHABLE_KEY=$STRIPE_PUBLISHABLE_KEY
STRIPE_SECRET_KEY=$STRIPE_SECRET_KEY
STRIPE_WEBHOOK_SECRET=$STRIPE_WEBHOOK_SECRET
PROVISIONER_API_KEY=$PROVISIONER_API_KEY
GOOGLE_CLIENT_ID=$GOOGLE_CLIENT_ID
GOOGLE_CLIENT_SECRET=$GOOGLE_CLIENT_SECRET
EOF

# Update saas-platform .env if it exists
if [ -d "saas-platform" ] && [ -f "saas-platform/.env" ]; then
    cp saas-platform/.env saas-platform/.env.backup.$(date +%Y%m%d_%H%M%S)
    cp .env saas-platform/.env
fi
