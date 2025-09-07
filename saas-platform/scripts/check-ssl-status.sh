#!/bin/bash

# Check SSL certificate status for MindRoom instances
# This script helps diagnose SSL certificate issues

set -e

KUBECONFIG="${KUBECONFIG:-$(dirname "$0")/../terraform-k8s/mindroom-k8s_kubeconfig.yaml}"
INSTANCE="${1:-foo}"  # Default to 'foo' if no instance name provided

echo "🔍 SSL Certificate Status Check for instance: $INSTANCE"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Check if ClusterIssuer exists
echo ""
echo "1️⃣  ClusterIssuer Status:"
if kubectl --kubeconfig="$KUBECONFIG" get clusterissuer letsencrypt-prod &> /dev/null; then
    echo "✅ letsencrypt-prod ClusterIssuer exists"
    kubectl --kubeconfig="$KUBECONFIG" get clusterissuer letsencrypt-prod
else
    echo "❌ letsencrypt-prod ClusterIssuer NOT FOUND"
    echo "   Run: ./scripts/setup-ssl-certificates.sh"
fi

# Check Ingress
echo ""
echo "2️⃣  Ingress Configuration:"
if kubectl --kubeconfig="$KUBECONFIG" get ingress -n mindroom-instances "${INSTANCE}-ingress" &> /dev/null; then
    echo "✅ Ingress found for $INSTANCE"
    kubectl --kubeconfig="$KUBECONFIG" get ingress -n mindroom-instances "${INSTANCE}-ingress" -o wide

    # Check TLS configuration
    echo ""
    echo "   TLS Hosts:"
    kubectl --kubeconfig="$KUBECONFIG" get ingress -n mindroom-instances "${INSTANCE}-ingress" \
        -o jsonpath='{.spec.tls[*].hosts[*]}' | tr ' ' '\n' | sed 's/^/   - /'
else
    echo "❌ No Ingress found for instance: $INSTANCE"
fi

# Check Certificate
echo ""
echo "3️⃣  Certificate Status:"
CERT_NAME="mindroom-${INSTANCE}-tls"
if kubectl --kubeconfig="$KUBECONFIG" get certificate -n mindroom-instances "$CERT_NAME" &> /dev/null; then
    echo "✅ Certificate resource exists"
    kubectl --kubeconfig="$KUBECONFIG" get certificate -n mindroom-instances "$CERT_NAME"

    # Get certificate details
    echo ""
    echo "   Certificate Details:"
    kubectl --kubeconfig="$KUBECONFIG" describe certificate -n mindroom-instances "$CERT_NAME" | \
        grep -E "Status:|Message:|Reason:|Last Transition Time:" | head -8
else
    echo "❌ No Certificate resource found"
    echo "   cert-manager should create this automatically if ClusterIssuer exists"
fi

# Check Secret
echo ""
echo "4️⃣  TLS Secret:"
if kubectl --kubeconfig="$KUBECONFIG" get secret -n mindroom-instances "$CERT_NAME" &> /dev/null; then
    echo "✅ TLS Secret exists"

    # Check certificate expiry
    CERT_DATA=$(kubectl --kubeconfig="$KUBECONFIG" get secret -n mindroom-instances "$CERT_NAME" \
        -o jsonpath='{.data.tls\.crt}' | base64 -d)

    if [ -n "$CERT_DATA" ]; then
        echo "   Certificate details:"
        echo "$CERT_DATA" | openssl x509 -noout -subject -issuer -dates | sed 's/^/   /'
    fi
else
    echo "❌ TLS Secret not found"
fi

# Check Challenges
echo ""
echo "5️⃣  Active Challenges:"
CHALLENGES=$(kubectl --kubeconfig="$KUBECONFIG" get challenges -n mindroom-instances 2>/dev/null | grep "$INSTANCE" || true)
if [ -n "$CHALLENGES" ]; then
    echo "⚠️  Active ACME challenges found:"
    echo "$CHALLENGES"
    echo ""
    echo "   This means cert-manager is trying to get a certificate."
    echo "   Check challenge details with:"
    echo "   kubectl describe challenges -n mindroom-instances"
else
    echo "✅ No active challenges (certificate might be issued or not requested yet)"
fi

# DNS Check
echo ""
echo "6️⃣  DNS Resolution:"
for domain in "${INSTANCE}.staging.mindroom.chat" "${INSTANCE}.api.staging.mindroom.chat" "${INSTANCE}.matrix.staging.mindroom.chat"; do
    if host "$domain" &> /dev/null; then
        IP=$(host "$domain" | grep "has address" | head -1 | awk '{print $4}')
        echo "✅ $domain → $IP"
    else
        echo "❌ $domain - DNS not configured"
    fi
done

# HTTPS Test
echo ""
echo "7️⃣  HTTPS Connectivity Test:"
URL="https://${INSTANCE}.staging.mindroom.chat"
if curl -k -s -o /dev/null -w "%{http_code}" "$URL" --connect-timeout 5 | grep -q "200\|301\|302\|403\|404"; then
    echo "✅ HTTPS endpoint is responding"

    # Check certificate
    echo "   Certificate info:"
    echo | openssl s_client -connect "${INSTANCE}.staging.mindroom.chat:443" -servername "${INSTANCE}.staging.mindroom.chat" 2>/dev/null | \
        openssl x509 -noout -subject -issuer 2>/dev/null | sed 's/^/   /' || echo "   Could not retrieve certificate info"
else
    echo "❌ HTTPS endpoint not accessible"
    echo "   This could mean:"
    echo "   - Certificate not issued yet"
    echo "   - Ingress controller issues"
    echo "   - Network/firewall blocking"
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📝 Summary:"
if kubectl --kubeconfig="$KUBECONFIG" get clusterissuer letsencrypt-prod &> /dev/null && \
   kubectl --kubeconfig="$KUBECONFIG" get certificate -n mindroom-instances "$CERT_NAME" &> /dev/null 2>&1; then
    READY=$(kubectl --kubeconfig="$KUBECONFIG" get certificate -n mindroom-instances "$CERT_NAME" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)
    if [ "$READY" = "True" ]; then
        echo "✅ SSL setup appears to be working correctly!"
    else
        echo "⚠️  Certificate exists but not ready. Check challenges and cert-manager logs."
    fi
else
    echo "❌ SSL setup incomplete. Run ./scripts/setup-ssl-certificates.sh first"
fi
