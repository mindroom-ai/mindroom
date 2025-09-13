#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "$0")/.." && pwd)
ENV_FILE="$ROOT_DIR/../.env"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE; cannot load HCLOUD_TOKEN." >&2
  exit 1
fi

set -a
source "$ENV_FILE"
set +a

cd "$ROOT_DIR"
terraform init -upgrade -input=false
terraform destroy -auto-approve -var="hcloud_token=${HCLOUD_TOKEN}" || true
