#!/usr/bin/env bash
# Redeploy MindRoom frontend for all customer instances

set -e

# Get script directory and project root
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
KUBECONFIG="$REPO_ROOT/cluster/terraform/terraform-k8s/mindroom-k8s_kubeconfig.yaml"

echo "📦 Building mindroom-frontend..."
cd "$REPO_ROOT"
docker build -t git.nijho.lt/basnijholt/mindroom-frontend:latest -f local/instances/deploy/Dockerfile.frontend .

echo "⬆️ Pushing to registry..."
docker push git.nijho.lt/basnijholt/mindroom-frontend:latest

echo "🔄 Restarting all customer frontend deployments..."
kubectl get deployments -n mindroom-instances --kubeconfig="$KUBECONFIG" \
    | grep mindroom-frontend \
    | awk '{print $1}' \
    | while read deployment; do
        echo "  Restarting $deployment..."
        kubectl rollout restart deployment/$deployment -n mindroom-instances --kubeconfig="$KUBECONFIG"
    done

echo "⏳ Waiting for rollouts to complete..."
kubectl get deployments -n mindroom-instances --kubeconfig="$KUBECONFIG" \
    | grep mindroom-frontend \
    | awk '{print $1}' \
    | while read deployment; do
        echo "  Waiting for $deployment..."
        kubectl rollout status deployment/$deployment -n mindroom-instances --kubeconfig="$KUBECONFIG"
    done

echo "✅ Redeploy completed for all customer frontend instances"
