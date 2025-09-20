#!/usr/bin/env bash
set -euo pipefail

# Simple K8s deployment script
# Usage: ./deploy.sh [frontend|backend|platform-frontend|platform-backend]
# - Builds the image with correct context and args
# - Pushes to registry
# - Restarts the deployment in the env namespace

APP=${1:-platform-frontend}

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)

# Load env vars from saas-platform/.env
if [ ! -f "$SCRIPT_DIR/.env" ]; then
  echo "Error: $SCRIPT_DIR/.env file not found" >&2
  exit 1
fi

set -a
eval "$(uvx --from python-dotenv[cli] dotenv -f "$SCRIPT_DIR/.env" list --format shell)"
set +a


# Normalize app names
if [ "$APP" = "backend" ]; then APP="platform-backend"; fi
if [ "$APP" = "frontend" ]; then APP="platform-frontend"; fi

IMAGE="git.nijho.lt/basnijholt/$APP:latest"

echo "[build] Building $APP from repo root context..."
docker build \
  --build-arg SUPABASE_URL="${SUPABASE_URL:-}" \
  --build-arg SUPABASE_ANON_KEY="${SUPABASE_ANON_KEY:-}" \
  --build-arg PLATFORM_DOMAIN="${PLATFORM_DOMAIN:-}" \
  -t "$IMAGE" \
  -f "$SCRIPT_DIR/Dockerfile.$APP" \
  "$REPO_ROOT"

echo "[push] Pushing $IMAGE..."
docker push "$IMAGE"

# Kubeconfig and namespace
KUBECONFIG_PATH="$REPO_ROOT/cluster/terraform/terraform-k8s/mindroom-k8s_kubeconfig.yaml"
if [ ! -f "$KUBECONFIG_PATH" ]; then
  echo "Error: kubeconfig not found at $KUBECONFIG_PATH" >&2
  exit 1
fi
export KUBECONFIG="$KUBECONFIG_PATH"

NAMESPACE="mindroom-${ENVIRONMENT:-test}"

echo "[k8s] Restarting deployment/$APP in namespace $NAMESPACE..."
kubectl -n "$NAMESPACE" rollout restart "deployment/$APP"
echo "[k8s] Waiting for rollout..."
kubectl -n "$NAMESPACE" rollout status "deployment/$APP" --timeout=180s

echo "✅ Done!"
