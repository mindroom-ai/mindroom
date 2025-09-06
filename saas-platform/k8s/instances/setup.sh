#!/bin/bash
# Quick setup script for MindRoom with Matrix

set -e

CUSTOMER="${1:-demo}"
DOMAIN="${2:-demo.mindroom.chat}"

echo "=== Installing MindRoom with Matrix for $CUSTOMER ==="

# Check for required env vars
if [ -z "$OPENAI_API_KEY" ] && [ -z "$ANTHROPIC_API_KEY" ]; then
    echo "ERROR: Set OPENAI_API_KEY or ANTHROPIC_API_KEY"
    exit 1
fi

# Install the chart
helm install $CUSTOMER . \
  --set customer=$CUSTOMER \
  --set domain=$DOMAIN \
  --set openai_key="${OPENAI_API_KEY:-}" \
  --set anthropic_key="${ANTHROPIC_API_KEY:-}"

echo "=== Waiting for pods to be ready ==="
kubectl wait --for=condition=ready pod -l app=mindroom-$CUSTOMER --timeout=60s || true
kubectl wait --for=condition=ready pod -l app=synapse-$CUSTOMER --timeout=60s || true

echo "=== Note: Matrix Admin User ==="
echo "After Synapse is running, create an admin user with:"
echo ""
echo "kubectl exec -it \$(kubectl get pod -l app=synapse-$CUSTOMER -o jsonpath='{.items[0].metadata.name}') -- \\"
echo "  register_new_matrix_user -c /data/homeserver.yaml \\"
echo "  -u admin -p ${MATRIX_ADMIN_PASSWORD:-admin123} --admin \\"
echo "  http://localhost:8008"

echo "=== Installation Complete ==="
echo "MindRoom: https://$DOMAIN"
echo "Matrix: https://m-$DOMAIN"
echo "Matrix admin: admin / ${MATRIX_ADMIN_PASSWORD:-admin123}"
