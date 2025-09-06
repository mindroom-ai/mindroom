#!/usr/bin/env bash
set -e

# Simple K8s deployment script
# Usage: ./deploy.sh [app-name]
#
# Available apps:
#   ./deploy.sh customer-portal
#   ./deploy.sh admin-dashboard
#   ./deploy.sh stripe-handler
#   ./deploy.sh instance-provisioner  # (was dokku-provisioner)
#
# Note: Uses 'latest' tag and forces K8s to pull the new image via rollout restart

APP=${1:-customer-portal}

# Load env vars
source ../.env

# Build args for customer-portal (Next.js needs these at build time)
if [ "$APP" = "customer-portal" ]; then
    BUILD_ARGS="--build-arg NEXT_PUBLIC_SUPABASE_URL=$SUPABASE_URL \
                --build-arg NEXT_PUBLIC_SUPABASE_ANON_KEY=$SUPABASE_ANON_KEY \
                --build-arg NEXT_PUBLIC_APP_URL=https://app.staging.mindroom.chat"
else
    BUILD_ARGS=""
fi

echo "Building $APP..."
docker build $BUILD_ARGS -t git.nijho.lt/basnijholt/$APP:latest -f Dockerfile.$APP .

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
