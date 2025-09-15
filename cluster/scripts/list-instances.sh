#!/usr/bin/env bash

# List all deployed MindRoom customer instances
# Usage: ./scripts/list-instances.sh

set -e

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Get kubeconfig path relative to this script's location
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
KUBECONFIG="$SCRIPT_DIR/../terraform/terraform-k8s/mindroom-k8s_kubeconfig.yaml"

if [ ! -f "$KUBECONFIG" ]; then
    echo "Error: Could not find kubeconfig file at $KUBECONFIG"
    exit 1
fi

echo -e "${BLUE}======================================${NC}"
echo -e "${BLUE}    MindRoom Instance Overview${NC}"
echo -e "${BLUE}======================================${NC}"
echo ""

# Count instances
INSTANCE_COUNT=$(helm list -n mindroom-instances --kubeconfig=$KUBECONFIG 2>/dev/null | grep -c mindroom || echo "0")
echo -e "${GREEN}Total Instances:${NC} $INSTANCE_COUNT"
echo ""

if [ "$INSTANCE_COUNT" -gt 0 ]; then
    echo -e "${YELLOW}Deployed Instances:${NC}"
    echo "-------------------"
    helm list -n mindroom-instances --kubeconfig=$KUBECONFIG --output table
    echo ""
fi

# Show pods status
PODS=$(kubectl get pods -n mindroom-instances --kubeconfig=$KUBECONFIG 2>/dev/null | grep -v "^NAME" || true)
if [ -n "$PODS" ]; then
    echo -e "${YELLOW}Instance Pods Status:${NC}"
    echo "---------------------"
    kubectl get pods -n mindroom-instances --kubeconfig=$KUBECONFIG -o wide 2>/dev/null
    echo ""
fi

# Show accessible URLs
INGRESSES=$(kubectl get ingress -n mindroom-instances --kubeconfig=$KUBECONFIG 2>/dev/null | grep -v "^NAME" || true)
if [ -n "$INGRESSES" ]; then
    echo -e "${YELLOW}Customer URLs:${NC}"
    echo "--------------"
    kubectl get ingress -n mindroom-instances --kubeconfig=$KUBECONFIG -o custom-columns='CUSTOMER:.metadata.name,FRONTEND:.spec.rules[0].host,API:.spec.rules[1].host,MATRIX:.spec.rules[2].host' 2>/dev/null
    echo ""
fi

# Show resource usage
if [ "$INSTANCE_COUNT" -gt 0 ]; then
    echo -e "${YELLOW}Resource Usage:${NC}"
    echo "---------------"
    kubectl top pods -n mindroom-instances --kubeconfig=$KUBECONFIG 2>/dev/null || echo "Metrics not available (metrics-server may not be installed)"
    echo ""
fi

# Platform services status
echo -e "${YELLOW}Platform Services Status:${NC}"
echo "-------------------------"
kubectl get deployments -n mindroom-staging --kubeconfig=$KUBECONFIG 2>/dev/null | grep -E "NAME|provisioner|platform-frontend|stripe|backend" || echo "Platform services not found"
echo ""

# Quick health check
echo -e "${YELLOW}Quick Health Check:${NC}"
echo "-------------------"
DOMAIN="${PLATFORM_DOMAIN:-staging.mindroom.chat}"
echo -n "Instance Provisioner: "
curl -s -k "https://api.${DOMAIN}/health" 2>/dev/null | jq -r '.status' 2>/dev/null || echo "Not accessible"
echo -n "Customer Portal: "
curl -s -o /dev/null -w "%{http_code}\n" -k "https://app.${DOMAIN}" 2>/dev/null || echo "Not accessible"
