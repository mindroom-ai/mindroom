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

# Load env vars from saas-platform directory
if [ -f .env ]; then
    source .env
else
    echo "Error: .env file not found in saas-platform directory"
    exit 1
fi

# Build with appropriate args based on app
echo "Building $APP..."
if [ "$APP" = "customer-portal" ]; then
    # Customer portal needs NEXT_PUBLIC_ vars at build time
    docker build \
        --build-arg NEXT_PUBLIC_SUPABASE_URL="$SUPABASE_URL" \
        --build-arg NEXT_PUBLIC_SUPABASE_ANON_KEY="$SUPABASE_ANON_KEY" \
        -t git.nijho.lt/basnijholt/$APP:latest \
        -f Dockerfile.$APP .
else
    # Other apps don't need build args - secrets stay server-side
    docker build -t git.nijho.lt/basnijholt/$APP:latest -f Dockerfile.$APP .
fi

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
