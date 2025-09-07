#!/usr/bin/env bash

# MindRoom SaaS Platform CLI Helper
# Usage: ./scripts/mindroom-cli.sh [command]

set -e

KUBECONFIG="${KUBECONFIG:-./terraform-k8s/mindroom-k8s_kubeconfig.yaml}"

case "$1" in
    list|ls)
        echo "Customer Instances:"
        helm list -n mindroom-instances --kubeconfig=$KUBECONFIG 2>/dev/null || echo "  None"
        ;;

    pods)
        kubectl get pods -n mindroom-instances --kubeconfig=$KUBECONFIG
        ;;

    urls)
        echo "Customer Instance URLs:"
        kubectl get ingress -n mindroom-instances --kubeconfig=$KUBECONFIG -o custom-columns='CUSTOMER:.metadata.name,URL:.spec.rules[0].host' 2>/dev/null
        ;;

    status)
        echo "=== Customer Instances ==="
        kubectl get all -n mindroom-instances --kubeconfig=$KUBECONFIG 2>/dev/null || echo "No instances deployed"
        echo ""
        echo "=== Platform Services ==="
        kubectl get pods -n mindroom-staging --kubeconfig=$KUBECONFIG
        ;;

    logs)
        if [ -z "$2" ]; then
            echo "Usage: $0 logs <customer-id>"
            exit 1
        fi
        echo "Logs for customer: $2"
        kubectl logs -n mindroom-instances -l customer=$2 --all-containers=true --kubeconfig=$KUBECONFIG
        ;;

    provision)
        if [ -z "$2" ]; then
            echo "Usage: $0 provision <customer-id>"
            exit 1
        fi
        echo "Provisioning instance for: $2"
        curl -k -X POST https://api.staging.mindroom.chat/api/v1/provision \
            -H "Content-Type: application/json" \
            -H "Authorization: Bearer change_me_in_production_123" \
            -d "{
                \"account_id\": \"$2\",
                \"subscription_id\": \"sub-$2\",
                \"tier\": \"starter\",
                \"api_keys\": {
                    \"openai\": \"\",
                    \"anthropic\": \"\"
                }
            }" | jq
        ;;

    deprovision)
        if [ -z "$2" ]; then
            echo "Usage: $0 deprovision <customer-id>"
            exit 1
        fi
        echo "Deprovisioning instance for: $2"
        curl -k -X DELETE https://api.staging.mindroom.chat/api/v1/deprovision \
            -H "Content-Type: application/json" \
            -H "Authorization: Bearer change_me_in_production_123" \
            -d "{\"customer_id\": \"$2\", \"subscription_id\": \"sub-$2\"}" | jq
        ;;

    *)
        echo "MindRoom SaaS Platform CLI"
        echo ""
        echo "Usage: $0 <command> [options]"
        echo ""
        echo "Commands:"
        echo "  list, ls         List all customer instances"
        echo "  pods            Show all customer pods"
        echo "  urls            Show customer instance URLs"
        echo "  status          Show overall platform status"
        echo "  logs <id>       Show logs for a customer instance"
        echo "  provision <id>  Provision a new test instance"
        echo "  deprovision <id> Remove a customer instance"
        echo ""
        echo "Examples:"
        echo "  $0 list"
        echo "  $0 provision test-customer"
        echo "  $0 logs test-customer"
        echo "  $0 deprovision test-customer"
        ;;
esac
