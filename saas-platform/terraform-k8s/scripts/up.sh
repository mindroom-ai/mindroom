#!/usr/bin/env bash
set -euo pipefail

# Simple, repeatable deploy using env from saas-platform/.env
# Phases:
#  1) Create K3s cluster (targeted)
#  2) Apply platform + DNS (DNS auto-detected or forced via ENABLE_DNS=true)

ROOT_DIR=$(cd "$(dirname "$0")/.." && pwd)
ENV_FILE="$ROOT_DIR/../.env"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE; please create it and set HCLOUD_TOKEN, SUPABASE/STRIPE, and Porkbun keys (optional)." >&2
  exit 1
fi

set -a
source "$ENV_FILE"
set +a

cd "$ROOT_DIR"

echo "Initializing Terraform..."
terraform init -upgrade -input=false

# Phase 1: cluster only
echo "Applying cluster (phase 1)..."
terraform apply -auto-approve -target=module.kube-hetzner -var="hcloud_token=${HCLOUD_TOKEN}"

# Kubeconfig path (deterministic)
KUBECONFIG_PATH="$ROOT_DIR/${cluster_name:-mindroom-k8s}_kubeconfig.yaml"
if [[ ! -f "$KUBECONFIG_PATH" ]]; then
  # Fallback to output if file name overridden
  KUBECONFIG_PATH=$(terraform output -raw kubeconfig_path)
fi
export KUBECONFIG="$KUBECONFIG_PATH"

echo "Cluster nodes:"
kubectl get nodes -o wide || true

# Decide DNS
ENABLE_DNS=${ENABLE_DNS:-auto}
if [[ "$ENABLE_DNS" == "auto" ]]; then
  if [[ -n "${PORKBUN_API_KEY:-}" && -n "${PORKBUN_SECRET_API_KEY:-}" ]]; then
    TF_ENABLE_DNS=true
  else
    TF_ENABLE_DNS=false
  fi
else
  TF_ENABLE_DNS=true
fi

echo "DNS enablement: $TF_ENABLE_DNS"

echo "Applying platform (phase 2)..."
terraform apply -auto-approve \
  -var="hcloud_token=${HCLOUD_TOKEN}" \
  -var="deploy_platform=${DEPLOY_PLATFORM:-true}" \
  -var="enable_dns=${TF_ENABLE_DNS}" \
  ${PORKBUN_API_KEY:+-var="porkbun_api_key=${PORKBUN_API_KEY}"} \
  ${PORKBUN_SECRET_API_KEY:+-var="porkbun_secret_key=${PORKBUN_SECRET_API_KEY}"}

echo "Verifying namespace and ingress..."
kubectl get ns test || true
kubectl get all -n test || true
kubectl get ing -n test || true

echo "Done. KUBECONFIG=$KUBECONFIG"
