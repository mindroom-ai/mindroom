#!/usr/bin/env bash

# Create K8s secrets from .env file
# This keeps secrets out of version control

set -e

if [ ! -f "../.env" ]; then
    echo "Error: ../.env file not found"
    exit 1
fi

echo "Creating mindroom namespace..."
kubectl create namespace mindroom --dry-run=client -o yaml | kubectl apply -f -

echo "Creating secrets from .env..."
# Delete existing secret if present
kubectl delete secret mindroom-secrets -n mindroom 2>/dev/null || true

# Create secret from env file
kubectl create secret generic mindroom-secrets \
  --from-env-file=../.env \
  --namespace=mindroom

echo "Creating registry secret..."
source ../.env
kubectl create secret docker-registry gitea-registry \
  --docker-server=git.nijho.lt \
  --docker-username=basnijholt \
  --docker-password="$GITEA_TOKEN" \
  --namespace=mindroom \
  --dry-run=client -o yaml | kubectl apply -f -

echo "Secrets created successfully"
