#!/usr/bin/env bash

# Update API keys in platform and instance secrets
# Usage: ./scripts/update-api-keys.sh

set -e

# Get script directory and find .env file
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
ENV_FILE="$REPO_ROOT/saas-platform/.env"
KUBECONFIG="$SCRIPT_DIR/../terraform/terraform-k8s/mindroom-k8s_kubeconfig.yaml"

# Load environment variables from .env file
if [ -f "$ENV_FILE" ]; then
    echo "Loading API keys from $ENV_FILE"
    set -a
    . "$ENV_FILE"
    set +a
else
    echo "Error: $ENV_FILE not found"
    exit 1
fi

# Check if API keys are set
if [ -z "$OPENAI_API_KEY" ] && [ -z "$ANTHROPIC_API_KEY" ] && [ -z "$GOOGLE_API_KEY" ] && [ -z "$OPENROUTER_API_KEY" ] && [ -z "$DEEPSEEK_API_KEY" ]; then
    echo "Warning: No API keys found in .env file"
fi

# Function to generate the API keys patch JSON
generate_patch_json() {
    cat <<EOF
[
  {"op": "replace", "path": "/data/openai_key", "value": "$(echo -n "$OPENAI_API_KEY" | base64 -w0)"},
  {"op": "replace", "path": "/data/anthropic_key", "value": "$(echo -n "$ANTHROPIC_API_KEY" | base64 -w0)"},
  {"op": "replace", "path": "/data/google_key", "value": "$(echo -n "$GOOGLE_API_KEY" | base64 -w0)"},
  {"op": "replace", "path": "/data/openrouter_key", "value": "$(echo -n "$OPENROUTER_API_KEY" | base64 -w0)"},
  {"op": "replace", "path": "/data/deepseek_key", "value": "$(echo -n "$DEEPSEEK_API_KEY" | base64 -w0)"},
  {"op": "replace", "path": "/data/supabase_service_key", "value": "$(echo -n "$SUPABASE_SERVICE_KEY" | base64 -w0)"}
]
EOF
}

# Function to update a secret's API keys
update_secret() {
    local secret_name=$1
    local namespace=$2
    local description=$3

    echo "Updating $description..."
    kubectl patch secret "$secret_name" -n "$namespace" --kubeconfig="$KUBECONFIG" \
        --type='json' -p="$(generate_patch_json)" 2>/dev/null || \
        echo "  Warning: Failed to update some keys for $description"
}

# Update platform secrets
update_secret "platform-secrets" "mindroom-staging" "platform secrets"

echo "Restarting platform backend to pick up new keys..."
kubectl rollout restart deployment/platform-backend -n mindroom-staging --kubeconfig="$KUBECONFIG"
kubectl rollout status deployment/platform-backend -n mindroom-staging --kubeconfig="$KUBECONFIG"

# Update instance secrets
echo ""
echo "Updating instance secrets..."
INSTANCES=$(kubectl get secrets -n mindroom-instances --kubeconfig="$KUBECONFIG" -o name 2>/dev/null | grep "secret/mindroom-api-keys-" | sed 's|secret/||')

if [ -z "$INSTANCES" ]; then
    echo "No instance secrets found"
else
    for SECRET_NAME in $INSTANCES; do
        INSTANCE_ID=$(echo "$SECRET_NAME" | sed 's/mindroom-api-keys-//')
        update_secret "$SECRET_NAME" "mindroom-instances" "instance $INSTANCE_ID"

        echo "  Restarting instance $INSTANCE_ID to pick up new keys..."
        kubectl rollout restart deployment/mindroom-backend-$INSTANCE_ID -n mindroom-instances --kubeconfig="$KUBECONFIG" 2>/dev/null || echo "  Backend not found or restart failed"
    done
fi

echo ""
echo "✅ API keys update complete!"
echo ""
echo "Summary:"
echo "- Platform secrets updated and backend restarted"
if [ -n "$INSTANCES" ]; then
    echo "- Updated secrets for instances: $(echo $INSTANCES | sed 's/mindroom-api-keys-//g' | tr '\n' ' ')"
else
    echo "- No instances to update"
fi
