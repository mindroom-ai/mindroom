#!/usr/bin/env bash
set -euo pipefail

# Install/upgrade the platform Helm chart into the kind cluster

ROOT_DIR="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
CHART_DIR="${ROOT_DIR}/cluster/k8s/platform"
VALUES_FILE="${CHART_DIR}/values.yaml"
RELEASE_NAME="platform-staging"

if ! command -v helm >/dev/null 2>&1; then
  echo "[error] 'helm' not found in PATH." >&2
  exit 1
fi

if [ ! -d "${CHART_DIR}" ]; then
  echo "[error] Chart directory not found: ${CHART_DIR}" >&2
  exit 1
fi

echo "[helm] Rendering chart to verify..."
helm lint "${CHART_DIR}" || true

echo "[k8s] Ensuring namespace 'mindroom-instances' exists (for RBAC)..."
kubectl get ns mindroom-instances >/dev/null 2>&1 || kubectl create namespace mindroom-instances

echo "[helm] Installing/upgrading ${RELEASE_NAME}..."
helm upgrade --install "${RELEASE_NAME}" "${CHART_DIR}" -f "${VALUES_FILE}"

echo "[helm] Waiting for pods in namespace 'mindroom-staging' (best effort)..."
kubectl wait --for=condition=ready pod -n mindroom-staging --all --timeout=120s || true

echo "[helm] Release '${RELEASE_NAME}' is applied."
echo "[helm] Tip: port-forward backend: kubectl -n mindroom-staging port-forward svc/platform-backend 8000:8000"
