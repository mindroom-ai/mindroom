#!/usr/bin/env bash
set -euo pipefail

# Simple script to set up terraform state symlinks
# Keeps all critical terraform files in ~/mindroom-state/

STATE_DIR="${HOME}/mindroom-state"
TF_DIR="$(cd "$(dirname "$0")/../../cluster/terraform/terraform-k8s" && pwd)"

echo "Setting up Terraform state directory at ${STATE_DIR}"

# Create state directory
mkdir -p "${STATE_DIR}"

# Move existing state files if they exist (one-time migration)
if [[ -f "${TF_DIR}/terraform.tfstate" ]] && [[ ! -L "${TF_DIR}/terraform.tfstate" ]]; then
    echo "Moving existing terraform.tfstate to ${STATE_DIR}"
    mv "${TF_DIR}/terraform.tfstate" "${STATE_DIR}/"
    mv "${TF_DIR}/terraform.tfstate.backup" "${STATE_DIR}/" 2>/dev/null || true
fi

# Move kubeconfig if it exists
if [[ -f "${TF_DIR}/mindroom-k8s_kubeconfig.yaml" ]] && [[ ! -L "${TF_DIR}/mindroom-k8s_kubeconfig.yaml" ]]; then
    echo "Moving existing kubeconfig to ${STATE_DIR}"
    mv "${TF_DIR}/mindroom-k8s_kubeconfig.yaml" "${STATE_DIR}/"
fi

# Create symlinks
cd "${TF_DIR}"

# Terraform state
if [[ ! -L "terraform.tfstate" ]]; then
    ln -sf "${STATE_DIR}/terraform.tfstate" terraform.tfstate
    echo "✓ Linked terraform.tfstate"
fi

if [[ ! -L "terraform.tfstate.backup" ]]; then
    ln -sf "${STATE_DIR}/terraform.tfstate.backup" terraform.tfstate.backup
    echo "✓ Linked terraform.tfstate.backup"
fi

# Kubeconfig
if [[ ! -L "mindroom-k8s_kubeconfig.yaml" ]]; then
    ln -sf "${STATE_DIR}/mindroom-k8s_kubeconfig.yaml" mindroom-k8s_kubeconfig.yaml
    echo "✓ Linked kubeconfig"
fi

# Create a README in the state directory
cat > "${STATE_DIR}/README.md" << EOF
# MindRoom Terraform State

This directory contains critical Terraform state files.
These files are symlinked from the repository.

## Files
- terraform.tfstate - Current infrastructure state
- terraform.tfstate.backup - Previous state backup
- mindroom-k8s_kubeconfig.yaml - Kubernetes cluster access

## Important
- Never delete these files without destroying infrastructure first
- These files are included in your backup strategy
- Repository clones will need to run: just cluster-tf-state-setup
EOF

echo ""
echo "✅ Terraform state directory configured at ${STATE_DIR}"
echo "   Your critical files are now centralized and symlinked."
