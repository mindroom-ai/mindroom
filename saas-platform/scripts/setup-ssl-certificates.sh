#!/bin/bash

# Setup SSL certificates with cert-manager and Let's Encrypt
# This script configures cert-manager to issue SSL certificates for MindRoom instances

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
K8S_DIR="$SCRIPT_DIR/../k8s"
KUBECONFIG="${KUBECONFIG:-$SCRIPT_DIR/../terraform-k8s/mindroom-k8s_kubeconfig.yaml}"

echo "🔐 Setting up SSL certificates with cert-manager..."
echo "Using kubeconfig: $KUBECONFIG"

# Check if kubectl is available
if ! command -v kubectl &> /dev/null; then
    echo "❌ kubectl is not installed. Please install kubectl first."
    exit 1
fi

# Check if cert-manager is installed
echo "📦 Checking cert-manager installation..."
if ! kubectl --kubeconfig="$KUBECONFIG" get namespace cert-manager &> /dev/null; then
    echo "❌ cert-manager namespace not found. Please ensure cert-manager is installed."
    echo "You can install it with:"
    echo "  kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.13.0/cert-manager.yaml"
    exit 1
fi

# Wait for cert-manager to be ready
echo "⏳ Waiting for cert-manager to be ready..."
kubectl --kubeconfig="$KUBECONFIG" wait --for=condition=Available --timeout=300s \
    deployment/cert-manager \
    deployment/cert-manager-webhook \
    deployment/cert-manager-cainjector \
    -n cert-manager 2>/dev/null || true

# Apply ClusterIssuer configurations
echo "🔧 Creating ClusterIssuers for Let's Encrypt..."
kubectl --kubeconfig="$KUBECONFIG" apply -f "$K8S_DIR/cert-manager/cluster-issuer-prod.yaml"
kubectl --kubeconfig="$KUBECONFIG" apply -f "$K8S_DIR/cert-manager/cluster-issuer-staging.yaml"

# Verify ClusterIssuer creation
echo "✅ Verifying ClusterIssuer..."
kubectl --kubeconfig="$KUBECONFIG" get clusterissuer

# Check existing certificates
echo ""
echo "📜 Checking existing certificates in mindroom-instances namespace..."
kubectl --kubeconfig="$KUBECONFIG" get certificates -n mindroom-instances 2>/dev/null || echo "No certificates found (namespace might not exist yet)"

# Instructions
echo ""
echo "✅ SSL certificate setup complete!"
echo ""
echo "📝 Next steps:"
echo "1. Deploy or redeploy your MindRoom instances"
echo "2. Certificates will be automatically requested from Let's Encrypt"
echo "3. Check certificate status with:"
echo "   kubectl --kubeconfig=$KUBECONFIG get certificates -n mindroom-instances"
echo ""
echo "🔍 Troubleshooting commands:"
echo "   # Check certificate status"
echo "   kubectl --kubeconfig=$KUBECONFIG describe certificate <cert-name> -n mindroom-instances"
echo ""
echo "   # Check cert-manager logs"
echo "   kubectl --kubeconfig=$KUBECONFIG logs -n cert-manager deployment/cert-manager"
echo ""
echo "   # Check certificate challenges"
echo "   kubectl --kubeconfig=$KUBECONFIG get challenges -n mindroom-instances"
echo ""
echo "⚠️  Note: Let's Encrypt rate limits apply:"
echo "   - Production: 50 certificates per week per registered domain"
echo "   - Use letsencrypt-staging for testing to avoid rate limits"
