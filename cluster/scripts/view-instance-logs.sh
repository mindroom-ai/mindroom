#!/bin/bash
# Script to view logs for MindRoom instance components

CUSTOMER_ID=${1:-6ca9f23a}
COMPONENT=${2:-mindroom}

# Get kubeconfig path relative to this script's location
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
KUBECONFIG="$SCRIPT_DIR/../terraform/terraform-k8s/mindroom-k8s_kubeconfig.yaml"

echo "Viewing logs for $COMPONENT of instance $CUSTOMER_ID..."
echo ""

case $COMPONENT in
  mindroom)
    kubectl --kubeconfig=$KUBECONFIG logs -n mindroom-instances deployment/mindroom-$CUSTOMER_ID -f
    ;;
  matrix|synapse)
    kubectl --kubeconfig=$KUBECONFIG logs -n mindroom-instances deployment/synapse-$CUSTOMER_ID -f
    ;;
  all)
    echo "=== MINDROOM LOGS ==="
    kubectl --kubeconfig=$KUBECONFIG logs -n mindroom-instances deployment/mindroom-$CUSTOMER_ID --tail=50
    echo ""
    echo "=== MATRIX/SYNAPSE LOGS ==="
    kubectl --kubeconfig=$KUBECONFIG logs -n mindroom-instances deployment/synapse-$CUSTOMER_ID --tail=50
    ;;
  *)
    echo "Usage: $0 [customer_id] [mindroom|matrix|all]"
    echo "Example: $0 6ca9f23a mindroom"
    exit 1
    ;;
esac
