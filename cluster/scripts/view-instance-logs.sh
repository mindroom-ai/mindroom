#!/bin/bash
# Script to view logs for MindRoom instance components

CUSTOMER_ID=${1:-6ca9f23a}
COMPONENT=${2:-backend}

# Get kubeconfig path relative to this script's location
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
KUBECONFIG="$SCRIPT_DIR/../terraform/terraform-k8s/mindroom-k8s_kubeconfig.yaml"

echo "Viewing logs for $COMPONENT of instance $CUSTOMER_ID..."
echo ""

case $COMPONENT in
  backend)
    kubectl --kubeconfig=$KUBECONFIG logs -n mindroom-instances deployment/mindroom-backend-$CUSTOMER_ID -f
    ;;
  frontend)
    echo "Frontend is now served by the backend deployment."
    kubectl --kubeconfig=$KUBECONFIG logs -n mindroom-instances deployment/mindroom-backend-$CUSTOMER_ID -f
    ;;
  matrix|synapse)
    kubectl --kubeconfig=$KUBECONFIG logs -n mindroom-instances deployment/synapse-$CUSTOMER_ID -f
    ;;
  all)
    echo "=== BACKEND LOGS ==="
    kubectl --kubeconfig=$KUBECONFIG logs -n mindroom-instances deployment/mindroom-backend-$CUSTOMER_ID --tail=50
    echo ""
    echo "=== MATRIX/SYNAPSE LOGS ==="
    kubectl --kubeconfig=$KUBECONFIG logs -n mindroom-instances deployment/synapse-$CUSTOMER_ID --tail=50
    ;;
  *)
    echo "Usage: $0 [customer_id] [backend|frontend|matrix|all]"
    echo "Note: 'frontend' is an alias for backend logs because the dashboard is bundled into the backend."
    echo "Example: $0 6ca9f23a backend"
    exit 1
    ;;
esac
