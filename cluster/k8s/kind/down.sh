#!/usr/bin/env bash
set -euo pipefail

CLUSTER_NAME="mindroom"

if ! command -v kind >/dev/null 2>&1; then
  echo "[error] 'kind' not found in PATH." >&2
  exit 1
fi

echo "[kind] Deleting cluster '${CLUSTER_NAME}'..."
kind delete cluster --name "${CLUSTER_NAME}" || true
echo "[kind] Done."
