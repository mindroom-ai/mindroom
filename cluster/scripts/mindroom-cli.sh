#!/usr/bin/env bash

# MindRoom SaaS Platform CLI Helper
# Usage: ./scripts/mindroom-cli.sh [command]

set -e

# Get script directory and find .env file
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
ENV_FILE="$REPO_ROOT/saas-platform/.env"

# Load environment variables from .env file
if [ -f "$ENV_FILE" ]; then
    eval "$(uvx --from "python-dotenv[cli]" dotenv --file "$ENV_FILE" list --format shell)"
else
    echo "Warning: $ENV_FILE not found, some commands may not work"
fi
KUBECONFIG="$SCRIPT_DIR/../terraform/terraform-k8s/mindroom-k8s_kubeconfig.yaml"

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
            echo "Usage: $0 provision <instance-id>"
            echo "Note: instance-id will be used as the subdomain (e.g., 1, 2, test1)"
            exit 1
        fi
        echo "Provisioning instance: $2"
        # Use a test UUID for account_id
        TEST_ACCOUNT_ID="00000000-0000-0000-0000-000000000001"
        API_BASE="${API_URL:-https://api.${PLATFORM_DOMAIN:-staging.mindroom.chat}}"
        curl -k -X POST "$API_BASE/system/provision" \
            -H "Content-Type: application/json" \
            -H "Authorization: Bearer ${PROVISIONER_API_KEY}" \
            -d "{
                \"account_id\": \"$TEST_ACCOUNT_ID\",
                \"subscription_id\": \"sub-$2\",
                \"tier\": \"starter\",
                \"instance_id\": \"$2\"
            }"
        ;;

    deprovision)
        if [ -z "$2" ]; then
            echo "Usage: $0 deprovision <customer-id>"
            exit 1
        fi
        echo "Deprovisioning instance for: $2"
        API_BASE="${API_URL:-https://api.${PLATFORM_DOMAIN:-staging.mindroom.chat}}"
        curl -k -X DELETE "$API_BASE/system/instances/$2/uninstall" \
            -H "Authorization: Bearer ${PROVISIONER_API_KEY}" | jq
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
