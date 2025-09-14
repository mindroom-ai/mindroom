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
    # Frontend has two containers: nginx-auth and mindroom-frontend
    echo "Which container? [nginx/frontend]"
    read -r CONTAINER
    if [ "$CONTAINER" = "nginx" ]; then
      kubectl --kubeconfig=$KUBECONFIG logs -n mindroom-instances deployment/mindroom-frontend-$CUSTOMER_ID -c nginx-auth -f
    else
      kubectl --kubeconfig=$KUBECONFIG logs -n mindroom-instances deployment/mindroom-frontend-$CUSTOMER_ID -c mindroom-frontend -f
    fi
    ;;
  matrix|synapse)
    kubectl --kubeconfig=$KUBECONFIG logs -n mindroom-instances deployment/synapse-$CUSTOMER_ID -f
    ;;
  all)
    echo "=== BACKEND LOGS ==="
    kubectl --kubeconfig=$KUBECONFIG logs -n mindroom-instances deployment/mindroom-backend-$CUSTOMER_ID --tail=50
    echo ""
    echo "=== FRONTEND LOGS ==="
    kubectl --kubeconfig=$KUBECONFIG logs -n mindroom-instances deployment/mindroom-frontend-$CUSTOMER_ID -c mindroom-frontend --tail=50
    echo ""
    echo "=== MATRIX/SYNAPSE LOGS ==="
    kubectl --kubeconfig=$KUBECONFIG logs -n mindroom-instances deployment/synapse-$CUSTOMER_ID --tail=50
    ;;
  *)
    echo "Usage: $0 [customer_id] [backend|frontend|matrix|all]"
    echo "Example: $0 6ca9f23a backend"
    exit 1
    ;;
esac
