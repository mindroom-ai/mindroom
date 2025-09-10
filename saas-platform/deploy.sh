#!/usr/bin/env bash
set -e

# Simple K8s deployment script
# Usage: ./deploy.sh [app-name]
#
# Available apps:
#   ./deploy.sh platform-frontend
#   ./deploy.sh backend
#
# Note: Uses 'latest' tag and forces K8s to pull the new image via rollout restart

APP=${1:-platform-frontend}

# Load env vars from saas-platform directory
if [ ! -f .env ]; then
    echo "Error: .env file not found in saas-platform directory"
    exit 1
fi

# Use python-dotenv for proper .env parsing
set -a
eval "$(uvx --from 'python-dotenv[cli]' dotenv list --format shell)"
set +a

# Map app names to Docker image names
if [ "$APP" = "backend" ]; then
    APP="platform-backend"
fi
if [ "$APP" = "frontend" ]; then
    APP="platform-frontend"
fi

# Build with appropriate args based on app
echo "Building $APP..."
if [ "$APP" = "platform-frontend" ]; then
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
