#!/usr/bin/env bash
# Test MindRoom Helm chart locally with kind

set -e

CLUSTER_NAME="mindroom-test"
NAMESPACE="mindroom"

echo "🚀 Testing MindRoom Helm chart with kind"
echo "========================================="

# Check if kind cluster exists
if kind get clusters | grep -q "$CLUSTER_NAME"; then
    echo "✓ Using existing kind cluster: $CLUSTER_NAME"
else
    echo "Creating kind cluster: $CLUSTER_NAME"
    kind create cluster --name "$CLUSTER_NAME" --config - <<EOF
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
- role: control-plane
  extraPortMappings:
  - containerPort: 80
    hostPort: 80
    protocol: TCP
  - containerPort: 443
    hostPort: 443
    protocol: TCP
EOF
fi

# Set kubectl context
kubectl config use-context "kind-$CLUSTER_NAME"

# Create namespace
echo "Creating namespace: $NAMESPACE"
kubectl create namespace $NAMESPACE --dry-run=client -o yaml | kubectl apply -f -

# Install nginx ingress (for kind)
echo "Installing nginx ingress..."
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/main/deploy/static/provider/kind/deploy.yaml
echo "Waiting for ingress to be ready..."
kubectl wait --namespace ingress-nginx \
  --for=condition=ready pod \
  --selector=app.kubernetes.io/component=controller \
  --timeout=90s || true

# Test helm chart
echo "Testing Helm chart..."
cd mindroom

# Dry run first
echo "Running helm install --dry-run..."
helm install test . \
  --namespace $NAMESPACE \
  --set customer=test \
  --set domain=test.local \
  --set openai_key="${OPENAI_API_KEY:-sk-test}" \
  --set anthropic_key="${ANTHROPIC_API_KEY:-}" \
  --dry-run --debug > /tmp/helm-test.yaml

echo "✓ Helm dry-run successful"

# Actual install
read -p "Install chart to kind cluster? (y/N) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    helm install test . \
      --namespace $NAMESPACE \
      --set customer=test \
      --set domain=test.local \
      --set openai_key="${OPENAI_API_KEY:-sk-test}" \
      --set anthropic_key="${ANTHROPIC_API_KEY:-}"

    echo "Waiting for pods..."
    kubectl -n $NAMESPACE wait --for=condition=ready pod --all --timeout=120s || true

    echo ""
    echo "Deployment status:"
    kubectl -n $NAMESPACE get all

    echo ""
    echo "To access the services locally:"
    echo "  1. Add to /etc/hosts:"
    echo "     127.0.0.1 test.local m-test.local"
    echo "  2. Port forward:"
    echo "     kubectl -n $NAMESPACE port-forward svc/mindroom-test 3003:3003 &"
    echo "     kubectl -n $NAMESPACE port-forward svc/synapse-test 8008:8008 &"
    echo ""
    echo "To uninstall:"
    echo "  helm -n $NAMESPACE uninstall test"
fi

echo ""
echo "To delete the kind cluster:"
echo "  kind delete cluster --name $CLUSTER_NAME"
