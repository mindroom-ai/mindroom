#!/usr/bin/env bash
set -euo pipefail

# Create a local kind cluster for MindRoom with ingress

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLUSTER_NAME="mindroom"
KIND_CONFIG="${SCRIPT_DIR}/kind-config.yaml"

echo "[kind] Bringing up cluster '${CLUSTER_NAME}'..."

if ! command -v kind >/dev/null 2>&1; then
  echo "[error] 'kind' not found in PATH. If you use Nix, try: nix-shell cluster/k8s/kind/shell.nix" >&2
  exit 1
fi

if ! command -v kubectl >/dev/null 2>&1; then
  echo "[error] 'kubectl' not found in PATH. Enter your dev shell or install kubectl." >&2
  exit 1
fi

if [ ! -f "${KIND_CONFIG}" ]; then
  echo "[error] kind config not found at ${KIND_CONFIG}" >&2
  exit 1
fi

echo "[kind] Deleting any existing cluster (ignore errors)..."
kind delete cluster --name "${CLUSTER_NAME}" >/dev/null 2>&1 || true

echo "[kind] Creating cluster from ${KIND_CONFIG}..."
kind create cluster --name "${CLUSTER_NAME}" --config "${KIND_CONFIG}"

echo "[kind] Installing ingress-nginx for kind..."
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/main/deploy/static/provider/kind/deploy.yaml

echo "[kind] Waiting for ingress controller to be ready..."
kubectl wait --namespace ingress-nginx \
  --for=condition=ready pod \
  --selector=app.kubernetes.io/component=controller \
  --timeout=180s

echo "[kind] Cluster '${CLUSTER_NAME}' is ready."
echo "[kind] HTTP:  localhost:30080, HTTPS: localhost:30443"
