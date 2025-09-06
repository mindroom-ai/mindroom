#!/usr/bin/env bash

# Setup kind cluster for local K8s testing

set -e

echo "Setting up kind cluster..."

# Check if kind is available
if ! command -v kind &> /dev/null; then
    echo "Error: kind not found. Please run this in nix-shell"
    exit 1
fi

# Delete existing cluster if present
kind delete cluster --name mindroom 2>/dev/null || true

# Create kind cluster
echo "Creating cluster..."
kind create cluster --config kind-config.yaml

# Install nginx ingress
echo "Installing ingress..."
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/main/deploy/static/provider/kind/deploy.yaml

# Wait for ingress
kubectl wait --namespace ingress-nginx \
  --for=condition=ready pod \
  --selector=app.kubernetes.io/component=controller \
  --timeout=90s

echo "✅ Cluster ready"
echo ""
echo "Next steps:"
echo "1. ./setup-secrets.sh"
echo "2. kubectl apply -f deploy.yaml"
