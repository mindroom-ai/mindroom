#!/usr/bin/env bash
# Redeploy MindRoom frontend for all customer instances

set -e

echo "📦 Building mindroom-frontend..."
cd /home/basnijholt/Work/mindroom-2
docker build -t git.nijho.lt/basnijholt/mindroom-frontend:latest -f deploy/Dockerfile.frontend .

echo "⬆️ Pushing to registry..."
docker push git.nijho.lt/basnijholt/mindroom-frontend:latest

echo "🔄 Restarting all customer frontend deployments..."
cd /home/basnijholt/Work/mindroom-2/saas-platform
kubectl get deployments -n mindroom-instances --kubeconfig=./terraform-k8s/mindroom-k8s_kubeconfig.yaml \
    | grep mindroom-frontend \
    | awk '{print $1}' \
    | while read deployment; do
        echo "  Restarting $deployment..."
        kubectl rollout restart deployment/$deployment -n mindroom-instances --kubeconfig=./terraform-k8s/mindroom-k8s_kubeconfig.yaml
    done

echo "⏳ Waiting for rollouts to complete..."
kubectl get deployments -n mindroom-instances --kubeconfig=./terraform-k8s/mindroom-k8s_kubeconfig.yaml \
    | grep mindroom-frontend \
    | awk '{print $1}' \
    | while read deployment; do
        echo "  Waiting for $deployment..."
        kubectl rollout status deployment/$deployment -n mindroom-instances --kubeconfig=./terraform-k8s/mindroom-k8s_kubeconfig.yaml
    done

echo "✅ Redeploy completed for all customer frontend instances"
