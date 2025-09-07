#!/bin/bash

# Setup wildcard SSL certificates for *.staging.mindroom.chat
# This script configures cert-manager with Porkbun DNS-01 challenge for wildcard certificates

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
K8S_DIR="$SCRIPT_DIR/../k8s"
KUBECONFIG="${KUBECONFIG:-$SCRIPT_DIR/../terraform-k8s/mindroom-k8s_kubeconfig.yaml}"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo "🌐 Setting up wildcard SSL certificates for *.staging.mindroom.chat"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Check prerequisites
echo "📋 Checking prerequisites..."

if ! command -v kubectl &> /dev/null; then
    echo -e "${RED}❌ kubectl is not installed${NC}"
    exit 1
fi

if [ ! -f "$KUBECONFIG" ]; then
    echo -e "${RED}❌ Kubeconfig not found at: $KUBECONFIG${NC}"
    exit 1
fi

# Check if cert-manager is installed
if ! kubectl --kubeconfig="$KUBECONFIG" get namespace cert-manager &> /dev/null; then
    echo -e "${RED}❌ cert-manager is not installed${NC}"
    echo "Install it with:"
    echo "  kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.13.0/cert-manager.yaml"
    exit 1
fi

echo -e "${GREEN}✅ Prerequisites checked${NC}"
echo ""

# Get Porkbun API credentials
echo "🔑 Porkbun API Configuration"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "You need Porkbun API credentials to set up DNS-01 challenge for wildcard certificates."
echo "Get them from: https://porkbun.com/account/api"
echo ""

# Check if credentials are in environment variables
if [ -n "$PORKBUN_API_KEY" ] && [ -n "$PORKBUN_SECRET_KEY" ]; then
    echo -e "${GREEN}✅ Using Porkbun credentials from environment variables${NC}"
    API_KEY="$PORKBUN_API_KEY"
    SECRET_KEY="$PORKBUN_SECRET_KEY"
else
    # Prompt for credentials
    read -p "Enter your Porkbun API Key: " API_KEY
    read -s -p "Enter your Porkbun Secret Key: " SECRET_KEY
    echo ""
fi

if [ -z "$API_KEY" ] || [ -z "$SECRET_KEY" ]; then
    echo -e "${RED}❌ Porkbun API credentials are required${NC}"
    exit 1
fi

echo ""
echo "🚀 Starting wildcard certificate setup..."
echo ""

# Step 1: Deploy Porkbun webhook
echo "1️⃣  Deploying Porkbun webhook for DNS-01 challenge..."

# Create temporary directory for webhook manifests with credentials
TEMP_DIR=$(mktemp -d)
cp -r "$K8S_DIR/cert-manager/porkbun-webhook/"* "$TEMP_DIR/"

# Replace placeholders with actual credentials in the secret file
sed -i "s/YOUR_PORKBUN_API_KEY/$API_KEY/" "$TEMP_DIR/02-secret.yaml"
sed -i "s/YOUR_PORKBUN_SECRET_KEY/$SECRET_KEY/" "$TEMP_DIR/02-secret.yaml"

# Apply webhook configuration
kubectl --kubeconfig="$KUBECONFIG" apply -f "$TEMP_DIR/"

# Clean up temp directory
rm -rf "$TEMP_DIR"

# Wait for webhook to be ready
echo "   ⏳ Waiting for webhook deployment..."
kubectl --kubeconfig="$KUBECONFIG" wait --for=condition=Available --timeout=120s \
    deployment/cert-manager-webhook-porkbun \
    -n cert-manager-webhook-porkbun || true

echo -e "${GREEN}   ✅ Porkbun webhook deployed${NC}"
echo ""

# Step 2: Create ClusterIssuers for DNS-01
echo "2️⃣  Creating ClusterIssuers for DNS-01 challenge..."

# First, we need to ensure the porkbun-api-key secret exists in cert-manager namespace too
kubectl --kubeconfig="$KUBECONFIG" create secret generic porkbun-api-key \
    --from-literal=api-key="$API_KEY" \
    --from-literal=secret-key="$SECRET_KEY" \
    --namespace=cert-manager \
    --dry-run=client -o yaml | kubectl --kubeconfig="$KUBECONFIG" apply -f -

# Apply DNS-01 ClusterIssuers
kubectl --kubeconfig="$KUBECONFIG" apply -f "$K8S_DIR/cert-manager/cluster-issuer-dns01-prod.yaml"
kubectl --kubeconfig="$KUBECONFIG" apply -f "$K8S_DIR/cert-manager/cluster-issuer-dns01-staging.yaml"

echo -e "${GREEN}   ✅ ClusterIssuers created${NC}"
echo ""

# Step 3: Create namespace if it doesn't exist
echo "3️⃣  Ensuring mindroom-instances namespace exists..."
kubectl --kubeconfig="$KUBECONFIG" create namespace mindroom-instances --dry-run=client -o yaml | \
    kubectl --kubeconfig="$KUBECONFIG" apply -f -
echo -e "${GREEN}   ✅ Namespace ready${NC}"
echo ""

# Step 4: Create the wildcard certificate
echo "4️⃣  Requesting wildcard certificate from Let's Encrypt..."
kubectl --kubeconfig="$KUBECONFIG" apply -f "$K8S_DIR/cert-manager/wildcard-certificate.yaml"

echo "   ⏳ Waiting for certificate to be issued (this may take 2-5 minutes)..."
echo ""

# Monitor certificate status
for i in {1..30}; do
    STATUS=$(kubectl --kubeconfig="$KUBECONFIG" get certificate wildcard-staging-mindroom-cert \
        -n mindroom-instances -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || echo "False")

    if [ "$STATUS" = "True" ]; then
        echo -e "${GREEN}   ✅ Wildcard certificate issued successfully!${NC}"
        break
    fi

    echo "   ⏳ Checking certificate status... ($i/30)"

    # Show any events or challenges
    if [ $((i % 5)) -eq 0 ]; then
        echo "   📝 Current status:"
        kubectl --kubeconfig="$KUBECONFIG" describe certificate wildcard-staging-mindroom-cert \
            -n mindroom-instances | grep -A5 "Status:" || true

        # Check for challenges
        CHALLENGES=$(kubectl --kubeconfig="$KUBECONFIG" get challenges -n mindroom-instances 2>/dev/null || true)
        if [ -n "$CHALLENGES" ]; then
            echo "   🔄 Active DNS challenges:"
            echo "$CHALLENGES"
        fi
    fi

    sleep 10
done

# Final status check
STATUS=$(kubectl --kubeconfig="$KUBECONFIG" get certificate wildcard-staging-mindroom-cert \
    -n mindroom-instances -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || echo "False")

if [ "$STATUS" != "True" ]; then
    echo -e "${YELLOW}⚠️  Certificate not ready yet. Check status with:${NC}"
    echo "   kubectl --kubeconfig=$KUBECONFIG describe certificate wildcard-staging-mindroom-cert -n mindroom-instances"
    echo ""
    echo "Common issues:"
    echo "- DNS propagation delay (wait a few more minutes)"
    echo "- Porkbun API not enabled for domain"
    echo "- Wrong API credentials"
else
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo -e "${GREEN}✅ Wildcard certificate setup complete!${NC}"
    echo ""
    echo "📝 Certificate covers:"
    echo "   • *.staging.mindroom.chat"
    echo "   • *.api.staging.mindroom.chat"
    echo "   • *.matrix.staging.mindroom.chat"
    echo ""
fi

# Step 5: Update existing ingresses to use wildcard certificate
echo ""
echo "5️⃣  Updating existing instances to use wildcard certificate..."

# Get all existing ingresses
INGRESSES=$(kubectl --kubeconfig="$KUBECONFIG" get ingress -n mindroom-instances -o name 2>/dev/null || true)

if [ -n "$INGRESSES" ]; then
    echo -e "${YELLOW}⚠️  Found existing ingresses that need updating:${NC}"
    echo "$INGRESSES"
    echo ""
    echo "To update them to use the wildcard certificate, run:"
    echo "   $SCRIPT_DIR/migrate-to-wildcard.sh"
else
    echo "   No existing instances found"
fi

echo ""
echo "📚 Next Steps:"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "1. Update the Helm chart to use wildcard certificates:"
echo "   cp $K8S_DIR/instance/templates/ingress-wildcard.yaml $K8S_DIR/instance/templates/ingress.yaml"
echo ""
echo "2. For existing instances, update their ingresses:"
echo "   $SCRIPT_DIR/migrate-to-wildcard.sh"
echo ""
echo "3. Test SSL on any instance:"
echo "   curl -I https://6ca9f23a.staging.mindroom.chat"
echo ""
echo "4. Monitor certificate status:"
echo "   kubectl --kubeconfig=$KUBECONFIG get certificate -n mindroom-instances"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
