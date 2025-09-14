#!/usr/bin/env bash
set -euo pipefail

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

echo "Destroying platform and cluster..."
terraform destroy -auto-approve || true

echo "Done."
