#!/usr/bin/env bash
set -euo pipefail

# Deploy K3s cluster and platform via Terraform/Helm

ROOT_DIR=$(cd "$(dirname "$0")/.." && pwd)
REPO_ROOT=$(cd "$ROOT_DIR/../../.." && pwd)
ENV_FILE="${ENV_FILE:-$REPO_ROOT/saas-platform/.env}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE; please create it (see saas-platform/.env.example)." >&2
  exit 1
fi

set -a
source "$ENV_FILE"
set +a

cd "$ROOT_DIR"

echo "Initializing Terraform..."
terraform init -upgrade -input=false

echo "Applying cluster (phase 1)..."
terraform apply -auto-approve -target=module.kube-hetzner -var="hcloud_token=${HCLOUD_TOKEN}"

# Determine kubeconfig path from output
if KUBECONFIG_PATH=$(terraform output -raw kubeconfig_path 2>/dev/null); then
  :
else
  KUBECONFIG_PATH="$ROOT_DIR/${TF_VAR_cluster_name:-mindroom-k8s}_kubeconfig.yaml"
fi
export KUBECONFIG="$KUBECONFIG_PATH"

echo "Cluster nodes:"
kubectl get nodes -o wide || true

# Decide DNS
if [[ "${ENABLE_DNS:-auto}" == "auto" ]]; then
  TF_ENABLE_DNS=$([[ -n "${PORKBUN_API_KEY:-}" && -n "${PORKBUN_SECRET_API_KEY:-}" ]] && echo true || echo false)
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
kubectl get ns || true
kubectl get all -n ${TF_VAR_environment:-test} || true
kubectl get ing -n ${TF_VAR_environment:-test} || true

echo "Done. KUBECONFIG=$KUBECONFIG"
