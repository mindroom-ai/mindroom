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

# Fix SSH_AUTH_SOCK issue with kube-hetzner module
unset SSH_AUTH_SOCK

cd "$ROOT_DIR"

echo "Initializing Terraform..."
terraform init -upgrade -input=false

echo "Applying cluster (phase 1)..."
terraform apply -auto-approve -target=module.kube-hetzner

# Determine kubeconfig path from output
if KUBECONFIG_PATH=$(terraform output -raw kubeconfig_path 2>/dev/null); then
  :
else
  KUBECONFIG_PATH="$ROOT_DIR/${TF_VAR_cluster_name:-mindroom-k8s}_kubeconfig.yaml"
fi
export KUBECONFIG="$KUBECONFIG_PATH"

echo "Cluster nodes:"
kubectl get nodes -o wide || true

echo "Validating DNS credentials (required)..."
if [[ -z "${PORKBUN_API_KEY:-}" || -z "${PORKBUN_SECRET_API_KEY:-}" ]]; then
  echo "[error] Missing Porkbun credentials. Set PORKBUN_API_KEY and PORKBUN_SECRET_API_KEY in saas-platform/.env" >&2
  exit 1
fi

echo "Applying platform (phase 2, with DNS)..."
# Set deploy_platform as env var since it's not in .env by default
export TF_VAR_deploy_platform="${DEPLOY_PLATFORM:-true}"
terraform apply -auto-approve

echo "Verifying namespace and ingress..."
kubectl get ns || true
kubectl get all -n ${TF_VAR_environment:-test} || true
kubectl get ing -n ${TF_VAR_environment:-test} || true

echo "Done. KUBECONFIG=$KUBECONFIG"
