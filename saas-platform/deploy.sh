#!/usr/bin/env bash
set -e

# Simple K8s deployment script
# Usage: ./deploy.sh [app-name]
#
# Available apps:
#   ./deploy.sh customer-portal
#   ./deploy.sh admin-api         # (formerly admin-dashboard)
#   ./deploy.sh stripe-handler
#   ./deploy.sh instance-provisioner
#
# Note: Uses 'latest' tag and forces K8s to pull the new image via rollout restart

APP=${1:-customer-portal}

# Load env vars
source ../.env

# No more build args needed - all secrets stay server-side!
echo "Building $APP..."
docker build -t git.nijho.lt/basnijholt/$APP:latest -f Dockerfile.$APP .

echo "Pushing $APP..."
docker push git.nijho.lt/basnijholt/$APP:latest

echo "Updating deployment..."
cd terraform-k8s

# Force Kubernetes to pull the new image by restarting the deployment
echo "Restarting $APP deployment to pull new image..."
kubectl --kubeconfig=./mindroom-k8s_kubeconfig.yaml rollout restart deployment/$APP -n mindroom-staging

# Wait for rollout to complete
echo "Waiting for rollout to complete..."
kubectl --kubeconfig=./mindroom-k8s_kubeconfig.yaml rollout status deployment/$APP -n mindroom-staging

echo "✅ Done!"
