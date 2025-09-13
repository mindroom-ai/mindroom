#!/usr/bin/env bash
set -euo pipefail

# Deprecated shim: forward to consolidated cluster scripts
REPO_ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
echo "[deprecated] Use cluster/terraform/terraform-k8s/scripts/destroy.sh or 'just cluster-tf-destroy'"
exec bash "$REPO_ROOT/cluster/terraform/terraform-k8s/scripts/destroy.sh"
