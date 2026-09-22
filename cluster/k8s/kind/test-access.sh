#!/usr/bin/env bash
set -euo pipefail

# Test access to the kind cluster services

echo "🧪 Testing MindRoom kind cluster access"
echo "========================================"
echo ""

# Colors for output
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { echo -e "${GREEN}✓${NC} $1"; }
log_error() { echo -e "${RED}✗${NC} $1"; }
log_warn() { echo -e "${YELLOW}⚠${NC} $1"; }

# Forwarders run sequentially; retain only the process owned by this invocation.
PORT_FORWARD_PID=""
PORT_FORWARD_LOG=""
cleanup_port_forward() {
    if [[ -n "$PORT_FORWARD_PID" ]]; then
        kill "$PORT_FORWARD_PID" 2>/dev/null || true
        # Give the child a short grace period before enforcing bounded cleanup.
        for _ in {1..10}; do
            if ! kill -0 "$PORT_FORWARD_PID" 2>/dev/null; then
                break
            fi
            sleep 0.1
        done
        if kill -0 "$PORT_FORWARD_PID" 2>/dev/null; then
            kill -KILL "$PORT_FORWARD_PID" 2>/dev/null || true
        fi
        wait "$PORT_FORWARD_PID" 2>/dev/null || true
        PORT_FORWARD_PID=""
    fi
    if [[ -n "$PORT_FORWARD_LOG" ]]; then
        rm -f -- "$PORT_FORWARD_LOG"
        PORT_FORWARD_LOG=""
    fi
}
trap cleanup_port_forward EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

start_port_forward() {
    local namespace="$1" service="$2" mapping="$3"
    local readiness="Forwarding from 127.0.0.1:${mapping%:*} -> ${mapping#*:}"
    local deadline=$((SECONDS + 30))
    PORT_FORWARD_LOG=$(mktemp "${TMPDIR:-/tmp}/mindroom-port-forward.XXXXXX")
    kubectl --context kind-mindroom port-forward --address 127.0.0.1 -n "$namespace" "$service" "$mapping" > "$PORT_FORWARD_LOG" 2>&1 &
    PORT_FORWARD_PID=$!
    while kill -0 "$PORT_FORWARD_PID" 2>/dev/null; do
        if grep -Fxq -- "$readiness" "$PORT_FORWARD_LOG" && kill -0 "$PORT_FORWARD_PID" 2>/dev/null; then
            return
        fi
        if (( SECONDS >= deadline )); then
            log_error "Timed out waiting for port-forward to listen on 127.0.0.1:${mapping%:*}."
            cat "$PORT_FORWARD_LOG"
            exit 1
        fi
        sleep 0.1
    done
    log_error "Port-forward failed to start; the local port may already be in use."
    cat "$PORT_FORWARD_LOG"
    exit 1
}

# Start port-forward for ingress
echo "🔌 Starting ingress port-forward..."
start_port_forward ingress-nginx svc/ingress-nginx-controller 8080:80

# Test platform access
echo ""
echo "📊 Testing Platform Access"
echo "--------------------------"

# Test platform frontend
echo -n "Testing platform frontend (http://platform.local:8080)... "
if curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8080 -H "Host: platform.local" | grep -q "200"; then
    log_info "Working!"
else
    log_error "Failed"
fi

# Test platform API
echo -n "Testing platform API (http://platform.local:8080/api/health)... "
API_RESPONSE=$(curl -s http://127.0.0.1:8080/api/health -H "Host: platform.local" 2>/dev/null || echo "error")
if echo "$API_RESPONSE" | grep -q "ok\|health"; then
    log_info "Working!"
elif echo "$API_RESPONSE" | grep -q "Invalid host"; then
    log_warn "Invalid host header - check ingress config"
else
    log_error "Failed: $API_RESPONSE"
fi

# Test instance if it exists
echo ""
echo "📊 Testing Instance Access"
echo "-------------------------"

INSTANCE_EXISTS=$(kubectl --context kind-mindroom get pods -n mindroom-instances --no-headers 2>/dev/null | wc -l)
if [ "$INSTANCE_EXISTS" -gt 0 ]; then
    echo -n "Testing instance frontend (http://instance1.local:8080)... "
    if curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8080 -H "Host: instance1.local" | grep -q "200\|404"; then
        log_info "Reachable!"
    else
        log_error "Failed"
    fi
else
    log_warn "No instances deployed yet"
fi

# Direct port-forward access (without ingress)
echo ""
echo "📊 Direct Service Access (without ingress)"
echo "-----------------------------------------"

# Kill ingress port-forward
cleanup_port_forward

# Platform frontend direct
echo "Testing direct platform frontend access..."
start_port_forward mindroom-staging svc/platform-frontend 3000:3000

if curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:3000 | grep -q "200"; then
    log_info "Platform frontend direct: http://localhost:3000 ✓"
else
    log_error "Platform frontend direct access failed"
fi
cleanup_port_forward

# Platform backend direct
echo "Testing direct platform backend access..."
start_port_forward mindroom-staging svc/platform-backend 8000:8000

if curl -s http://127.0.0.1:8000/health 2>/dev/null | grep -q "ok"; then
    log_info "Platform backend direct: http://localhost:8000 ✓"
else
    log_error "Platform backend direct access failed"
fi
cleanup_port_forward

# Show pod status
echo ""
echo "📊 Pod Status"
echo "------------"
echo "Platform pods:"
kubectl --context kind-mindroom get pods -n mindroom-staging --no-headers | while read line; do
    NAME=$(echo $line | awk '{print $1}')
    READY=$(echo $line | awk '{print $2}')
    STATUS=$(echo $line | awk '{print $3}')
    if [ "$STATUS" = "Running" ]; then
        echo -e "  ${GREEN}●${NC} $NAME ($READY)"
    else
        echo -e "  ${RED}●${NC} $NAME ($STATUS)"
    fi
done

if [ "$INSTANCE_EXISTS" -gt 0 ]; then
    echo ""
    echo "Instance pods:"
    kubectl --context kind-mindroom get pods -n mindroom-instances --no-headers | while read line; do
        NAME=$(echo $line | awk '{print $1}')
        READY=$(echo $line | awk '{print $2}')
        STATUS=$(echo $line | awk '{print $3}')
        if [ "$STATUS" = "Running" ]; then
            echo -e "  ${GREEN}●${NC} $NAME ($READY)"
        else
            echo -e "  ${RED}●${NC} $NAME ($STATUS)"
        fi
    done
fi

# Summary
echo ""
echo "📌 Access Summary"
echo "================"
echo ""
echo "With /etc/hosts entries (add these lines to /etc/hosts):"
echo "  127.0.0.1 platform.local"
echo "  127.0.0.1 instance1.local"
echo ""
echo "Then run port-forward and access:"
echo "  kubectl --context kind-mindroom port-forward -n ingress-nginx svc/ingress-nginx-controller 8080:80"
echo "  → Platform: http://platform.local:8080"
echo "  → Instance: http://instance1.local:8080"
echo ""
echo "Direct access (no /etc/hosts needed):"
echo "  Platform Frontend: kubectl --context kind-mindroom port-forward -n mindroom-staging svc/platform-frontend 3000:3000"
echo "  Platform Backend:  kubectl --context kind-mindroom port-forward -n mindroom-staging svc/platform-backend 8000:8000"
echo ""
echo "Clean up:"
echo "  kind delete cluster --name mindroom"
