#!/usr/bin/env bash
set -euo pipefail

# Test access to the kind cluster services

export KUBECONFIG=~/.kube/kind-mindroom

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

# Kill any existing port-forwards
echo "🔧 Cleaning up existing port-forwards..."
pkill -f "kubectl port-forward" 2>/dev/null || true
sleep 2

# Start port-forward for ingress
echo "🔌 Starting ingress port-forward..."
kubectl port-forward -n ingress-nginx svc/ingress-nginx-controller 8080:80 > /tmp/ingress-pf.log 2>&1 &
INGRESS_PF_PID=$!
sleep 3

# Test platform access
echo ""
echo "📊 Testing Platform Access"
echo "--------------------------"

# Test platform frontend
echo -n "Testing platform frontend (http://platform.local:8080)... "
if curl -s -o /dev/null -w "%{http_code}" http://localhost:8080 -H "Host: platform.local" | grep -q "200"; then
    log_info "Working!"
else
    log_error "Failed"
fi

# Test platform API
echo -n "Testing platform API (http://platform.local:8080/api/health)... "
API_RESPONSE=$(curl -s http://localhost:8080/api/health -H "Host: platform.local" 2>/dev/null || echo "error")
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

INSTANCE_EXISTS=$(kubectl get pods -n mindroom-instances --no-headers 2>/dev/null | wc -l)
if [ "$INSTANCE_EXISTS" -gt 0 ]; then
    echo -n "Testing instance frontend (http://instance1.local:8080)... "
    if curl -s -o /dev/null -w "%{http_code}" http://localhost:8080 -H "Host: instance1.local" | grep -q "200\|404"; then
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
kill $INGRESS_PF_PID 2>/dev/null || true

# Platform frontend direct
echo "Testing direct platform frontend access..."
kubectl port-forward -n mindroom-staging svc/platform-frontend 3000:3000 > /tmp/pf-frontend.log 2>&1 &
PF_FRONTEND_PID=$!
sleep 3

if curl -s -o /dev/null -w "%{http_code}" http://localhost:3000 | grep -q "200"; then
    log_info "Platform frontend direct: http://localhost:3000 ✓"
else
    log_error "Platform frontend direct access failed"
fi
kill $PF_FRONTEND_PID 2>/dev/null || true

# Platform backend direct
echo "Testing direct platform backend access..."
kubectl port-forward -n mindroom-staging svc/platform-backend 8000:8000 > /tmp/pf-backend.log 2>&1 &
PF_BACKEND_PID=$!
sleep 3

if curl -s http://localhost:8000/health 2>/dev/null | grep -q "ok"; then
    log_info "Platform backend direct: http://localhost:8000 ✓"
else
    log_error "Platform backend direct access failed"
fi
kill $PF_BACKEND_PID 2>/dev/null || true

# Show pod status
echo ""
echo "📊 Pod Status"
echo "------------"
echo "Platform pods:"
kubectl get pods -n mindroom-staging --no-headers | while read line; do
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
    kubectl get pods -n mindroom-instances --no-headers | while read line; do
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
echo "  kubectl port-forward -n ingress-nginx svc/ingress-nginx-controller 8080:80"
echo "  → Platform: http://platform.local:8080"
echo "  → Instance: http://instance1.local:8080"
echo ""
echo "Direct access (no /etc/hosts needed):"
echo "  Platform Frontend: kubectl port-forward -n mindroom-staging svc/platform-frontend 3000:3000"
echo "  Platform Backend:  kubectl port-forward -n mindroom-staging svc/platform-backend 8000:8000"
echo ""
echo "Clean up:"
echo "  kind delete cluster --name mindroom"
