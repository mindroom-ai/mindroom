#!/usr/bin/env bash
# Redeploy MindRoom backend for all customer instances

set -e

echo "📦 Building mindroom-backend..."
cd /home/basnijholt/Work/mindroom-2
docker build -t git.nijho.lt/basnijholt/mindroom-backend:prod -f deploy/Dockerfile.backend .

echo "⬆️ Pushing to registry..."
docker push git.nijho.lt/basnijholt/mindroom-backend:prod

echo "🔄 Restarting all customer backend deployments..."
cd /home/basnijholt/Work/mindroom-2/saas-platform
kubectl get deployments -n mindroom-instances --kubeconfig=./terraform-k8s/mindroom-k8s_kubeconfig.yaml \
    | grep mindroom-backend \
    | awk '{print $1}' \
    | while read deployment; do
        echo "  Restarting $deployment..."
        kubectl rollout restart deployment/$deployment -n mindroom-instances --kubeconfig=./terraform-k8s/mindroom-k8s_kubeconfig.yaml
    done

echo "⏳ Waiting for rollouts to complete..."
kubectl get deployments -n mindroom-instances --kubeconfig=./terraform-k8s/mindroom-k8s_kubeconfig.yaml \
    | grep mindroom-backend \
    | awk '{print $1}' \
    | while read deployment; do
        echo "  Waiting for $deployment..."
        kubectl rollout status deployment/$deployment -n mindroom-instances --kubeconfig=./terraform-k8s/mindroom-k8s_kubeconfig.yaml
    done

echo "✅ Redeploy completed for all customer instances"
