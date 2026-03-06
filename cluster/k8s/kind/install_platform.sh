#!/usr/bin/env bash
set -euo pipefail

# Install/upgrade the platform Helm chart into the kind cluster

ROOT_DIR="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
CHART_DIR="${ROOT_DIR}/cluster/k8s/platform"
VALUES_FILE="${CHART_DIR}/values.yaml"
RELEASE_NAME="platform-staging"
PROVISIONER_API_KEY="${PROVISIONER_API_KEY:-kind-provisioner-key}"
INSTANCE_BASE_DOMAIN="${INSTANCE_BASE_DOMAIN:-local}"
INSTANCE_STORAGE_CLASS_NAME="${INSTANCE_STORAGE_CLASS_NAME:-standard}"
INSTANCE_MINDROOM_IMAGE="${INSTANCE_MINDROOM_IMAGE:-ghcr.io/mindroom-ai/mindroom:latest}"
INSTANCE_MINDROOM_IMAGE_PULL_POLICY="${INSTANCE_MINDROOM_IMAGE_PULL_POLICY:-IfNotPresent}"
INSTANCE_SYNAPSE_IMAGE="${INSTANCE_SYNAPSE_IMAGE:-matrixdotorg/synapse:latest}"
INSTANCE_SYNAPSE_IMAGE_PULL_POLICY="${INSTANCE_SYNAPSE_IMAGE_PULL_POLICY:-IfNotPresent}"

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
helm_args=(
  upgrade --install "${RELEASE_NAME}" "${CHART_DIR}" -f "${VALUES_FILE}"
  --set monitoring.enabled=false
  --set imagePullPolicy=IfNotPresent
  --set "provisioner.apiKey=${PROVISIONER_API_KEY}"
  --set "provisioner.instanceBaseDomain=${INSTANCE_BASE_DOMAIN}"
  --set "provisioner.instanceStorageClassName=${INSTANCE_STORAGE_CLASS_NAME}"
  --set "provisioner.instanceMindroomImage=${INSTANCE_MINDROOM_IMAGE}"
  --set "provisioner.instanceMindroomImagePullPolicy=${INSTANCE_MINDROOM_IMAGE_PULL_POLICY}"
  --set "provisioner.instanceSynapseImage=${INSTANCE_SYNAPSE_IMAGE}"
  --set "provisioner.instanceSynapseImagePullPolicy=${INSTANCE_SYNAPSE_IMAGE_PULL_POLICY}"
)

if [ -n "${SUPABASE_URL:-}" ]; then
  helm_args+=(--set "supabase.url=${SUPABASE_URL}")
fi
if [ -n "${SUPABASE_ANON_KEY:-}" ]; then
  helm_args+=(--set "supabase.anonKey=${SUPABASE_ANON_KEY}")
fi
if [ -n "${SUPABASE_SERVICE_ROLE_KEY:-}" ]; then
  helm_args+=(--set "supabase.serviceKey=${SUPABASE_SERVICE_ROLE_KEY}")
fi

helm "${helm_args[@]}"

echo "[helm] Waiting for pods in namespace 'mindroom-staging' (best effort)..."
kubectl wait --for=condition=ready pod -n mindroom-staging --all --timeout=120s || true

echo "[helm] Release '${RELEASE_NAME}' is applied."
echo "[helm] Tip: port-forward backend: kubectl -n mindroom-staging port-forward svc/platform-backend 8000:8000"
