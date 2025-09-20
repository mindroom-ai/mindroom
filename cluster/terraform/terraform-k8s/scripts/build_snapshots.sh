#!/usr/bin/env bash
set -euo pipefail

# Build the required MicroOS snapshots for the kube-hetzner module via Packer.

ROOT_DIR=$(cd "$(dirname "$0")/.." && pwd)
REPO_ROOT=$(cd "$ROOT_DIR/../../.." && pwd)
ENV_FILE="${ENV_FILE:-$REPO_ROOT/saas-platform/.env}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE; please create it (see saas-platform/.env.example)." >&2
  exit 1
fi

if ! command -v packer >/dev/null 2>&1; then
  echo "[error] HashiCorp Packer is not installed or not on PATH." >&2
  echo "        Install Packer: https://developer.hashicorp.com/packer/downloads" >&2
  exit 1
fi

set -a
source "$ENV_FILE"
set +a

if [[ -z "${HCLOUD_TOKEN:-}" ]]; then
  echo "[error] HCLOUD_TOKEN is not set. Populate it in $ENV_FILE." >&2
  exit 1
fi

cd "$ROOT_DIR"

TEMPLATE="hcloud-microos-snapshots.pkr.hcl"

echo "Initializing Packer plugins..."
packer init "$TEMPLATE"

echo "Building MicroOS snapshots (this can take several minutes)..."
packer build "$TEMPLATE"

echo "Snapshots created. Verify in Hetzner Cloud -> Images if desired."
