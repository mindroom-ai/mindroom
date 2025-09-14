#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "$0")/.." && pwd)
REPO_ROOT=$(cd "$ROOT_DIR/../../.." && pwd)
ENV_FILE="${ENV_FILE:-$REPO_ROOT/saas-platform/.env}"

set -a
[[ -f "$ENV_FILE" ]] && source "$ENV_FILE"
set +a

# Fix SSH_AUTH_SOCK issue with kube-hetzner module
unset SSH_AUTH_SOCK

cd "$ROOT_DIR"

echo "Terraform workspace: $(pwd)"
echo "Terraform outputs (if any):"
terraform output || true

if KUBECONFIG_PATH=$(terraform output -raw kubeconfig_path 2>/dev/null); then
  export KUBECONFIG="$KUBECONFIG_PATH"
fi

echo "Kubernetes status:"
kubectl get nodes -o wide || true
kubectl get ns || true
kubectl get ing -A || true
