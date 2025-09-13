#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "$0")/.." && pwd)
KUBECONFIG_PATH="$ROOT_DIR/${cluster_name:-mindroom-k8s}_kubeconfig.yaml"
export KUBECONFIG="$KUBECONFIG_PATH"

echo "Nodes:"
kubectl get nodes -o wide || true
echo
echo "Core pods:"
kubectl get pods -A | sed -n '1,200p' || true
echo
echo "Platform (test) resources:"
kubectl get all -n test || true
kubectl get ing -n test || true
