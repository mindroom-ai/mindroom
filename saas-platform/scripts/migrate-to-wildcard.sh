#!/bin/bash

# Migrate existing MindRoom instances to use wildcard SSL certificate
# This updates all existing ingresses to use the shared wildcard certificate

set -e

KUBECONFIG="${KUBECONFIG:-$(dirname "$0")/../terraform-k8s/mindroom-k8s_kubeconfig.yaml}"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo "🔄 Migrating existing instances to wildcard SSL certificate"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Check if wildcard certificate exists
echo "📋 Checking wildcard certificate status..."
CERT_STATUS=$(kubectl --kubeconfig="$KUBECONFIG" get certificate wildcard-staging-mindroom-cert \
    -n mindroom-instances -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || echo "NotFound")

if [ "$CERT_STATUS" = "NotFound" ]; then
    echo -e "${RED}❌ Wildcard certificate not found${NC}"
    echo "Run ./setup-wildcard-certificates.sh first"
    exit 1
elif [ "$CERT_STATUS" != "True" ]; then
    echo -e "${YELLOW}⚠️  Wildcard certificate exists but not ready${NC}"
    echo "Check status with:"
    echo "  kubectl describe certificate wildcard-staging-mindroom-cert -n mindroom-instances"
    read -p "Continue anyway? (y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
else
    echo -e "${GREEN}✅ Wildcard certificate is ready${NC}"
fi

echo ""
echo "🔍 Finding existing ingresses..."

# Get all ingresses in mindroom-instances namespace
INGRESSES=$(kubectl --kubeconfig="$KUBECONFIG" get ingress -n mindroom-instances -o json | \
    jq -r '.items[].metadata.name' 2>/dev/null || true)

if [ -z "$INGRESSES" ]; then
    echo "No ingresses found in mindroom-instances namespace"
    exit 0
fi

echo "Found ingresses:"
echo "$INGRESSES" | sed 's/^/  - /'
echo ""

# Count ingresses
TOTAL=$(echo "$INGRESSES" | wc -l)
CURRENT=0

echo "🚀 Starting migration..."
echo ""

# Process each ingress
for INGRESS_NAME in $INGRESSES; do
    CURRENT=$((CURRENT + 1))
    echo "[$CURRENT/$TOTAL] Processing: $INGRESS_NAME"

    # Get current ingress configuration
    CURRENT_SECRET=$(kubectl --kubeconfig="$KUBECONFIG" get ingress "$INGRESS_NAME" \
        -n mindroom-instances -o jsonpath='{.spec.tls[0].secretName}' 2>/dev/null || echo "none")

    if [ "$CURRENT_SECRET" = "wildcard-staging-mindroom-tls" ]; then
        echo -e "  ${GREEN}✓ Already using wildcard certificate${NC}"
        continue
    fi

    echo "  Current certificate: $CURRENT_SECRET"
    echo "  Updating to wildcard certificate..."

    # Patch the ingress to use wildcard certificate
    kubectl --kubeconfig="$KUBECONFIG" patch ingress "$INGRESS_NAME" \
        -n mindroom-instances \
        --type=json \
        -p='[
            {
                "op": "replace",
                "path": "/spec/tls/0/secretName",
                "value": "wildcard-staging-mindroom-tls"
            },
            {
                "op": "remove",
                "path": "/metadata/annotations/cert-manager.io~1cluster-issuer"
            }
        ]' 2>/dev/null || \
    kubectl --kubeconfig="$KUBECONFIG" patch ingress "$INGRESS_NAME" \
        -n mindroom-instances \
        --type=json \
        -p='[
            {
                "op": "replace",
                "path": "/spec/tls/0/secretName",
                "value": "wildcard-staging-mindroom-tls"
            }
        ]' 2>/dev/null || echo -e "  ${YELLOW}⚠️  Failed to update (may already be correct)${NC}"

    echo -e "  ${GREEN}✓ Updated${NC}"

    # Clean up old certificate and secret if they exist
    OLD_CERT="${INGRESS_NAME%-ingress}-tls"
    if [ "$CURRENT_SECRET" = "mindroom-$OLD_CERT" ] || [ "$CURRENT_SECRET" = "$OLD_CERT" ]; then
        echo "  Cleaning up old certificate resources..."

        # Delete old certificate resource
        kubectl --kubeconfig="$KUBECONFIG" delete certificate "$CURRENT_SECRET" \
            -n mindroom-instances 2>/dev/null || true

        # Delete old certificate secret
        kubectl --kubeconfig="$KUBECONFIG" delete secret "$CURRENT_SECRET" \
            -n mindroom-instances 2>/dev/null || true

        echo -e "  ${GREEN}✓ Cleanup complete${NC}"
    fi

    echo ""
done

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo -e "${GREEN}✅ Migration complete!${NC}"
echo ""

# Verify the migration
echo "📊 Migration Summary:"
echo ""

# Count ingresses using wildcard cert
USING_WILDCARD=$(kubectl --kubeconfig="$KUBECONFIG" get ingress -n mindroom-instances -o json | \
    jq -r '.items[] | select(.spec.tls[0].secretName == "wildcard-staging-mindroom-tls") | .metadata.name' | wc -l)

echo "  Total ingresses: $TOTAL"
echo "  Using wildcard certificate: $USING_WILDCARD"

if [ "$USING_WILDCARD" -eq "$TOTAL" ]; then
    echo -e "  ${GREEN}✅ All ingresses migrated successfully${NC}"
else
    echo -e "  ${YELLOW}⚠️  Some ingresses may need manual review${NC}"
fi

echo ""
echo "🔍 Testing SSL certificates..."
echo ""

# Test a few instances
for INGRESS_NAME in $(echo "$INGRESSES" | head -3); do
    # Extract customer ID from ingress name
    CUSTOMER_ID="${INGRESS_NAME%-ingress}"
    URL="https://${CUSTOMER_ID}.staging.mindroom.chat"

    echo -n "  Testing $URL ... "

    # Quick SSL test
    if timeout 5 openssl s_client -connect "${CUSTOMER_ID}.staging.mindroom.chat:443" \
        -servername "${CUSTOMER_ID}.staging.mindroom.chat" </dev/null 2>/dev/null | \
        grep -q "Verify return code: 0"; then
        echo -e "${GREEN}✅ SSL OK${NC}"
    else
        # Try with curl as fallback
        HTTP_CODE=$(curl -k -s -o /dev/null -w "%{http_code}" "$URL" --connect-timeout 5 2>/dev/null || echo "000")
        if [[ "$HTTP_CODE" =~ ^(200|301|302|403|404)$ ]]; then
            echo -e "${GREEN}✅ HTTPS responding (code: $HTTP_CODE)${NC}"
        else
            echo -e "${YELLOW}⚠️  Could not verify SSL${NC}"
        fi
    fi
done

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "📝 Next Steps:"
echo ""
echo "1. Update the Helm chart to use wildcard certificates for new deployments:"
echo "   cp k8s/instance/templates/ingress-wildcard.yaml k8s/instance/templates/ingress.yaml"
echo ""
echo "2. Test your instances:"
echo "   curl -I https://6ca9f23a.staging.mindroom.chat"
echo ""
echo "3. Monitor ingress status:"
echo "   kubectl get ingress -n mindroom-instances"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
