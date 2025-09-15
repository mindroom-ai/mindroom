#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "🚀 Starting fresh MindRoom K8s setup (kind)..."

echo "📦 Creating kind cluster..."
"${SCRIPT_DIR}/up.sh"

echo "🛠️  Building images and loading into kind..."
"${SCRIPT_DIR}/build_load_images.sh"

echo "🏗️ Installing platform chart..."
"${SCRIPT_DIR}/install_platform.sh"

echo "📊 Pods in mindroom-staging namespace:"
kubectl get pods -n mindroom-staging || true

echo ""
echo "📝 Next steps:"
echo "- Port-forward backend:  kubectl -n mindroom-staging port-forward svc/platform-backend 8000:8000"
echo "- Port-forward frontend: kubectl -n mindroom-staging port-forward svc/platform-frontend 3000:3000"
echo "- Delete cluster:        ${SCRIPT_DIR}/down.sh"
