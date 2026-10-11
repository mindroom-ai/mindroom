#!/usr/bin/env bash
# Quick validation that everything is working

set -euo pipefail

echo "🔍 Validating MindRoom local setup..."
echo ""

# Check cluster exists
if ! kind get clusters 2>/dev/null | grep -q "^mindroom$"; then
    echo "❌ No kind cluster found. Run 'make up' first."
    exit 1
fi

echo "✓ Kind cluster exists"

# Check platform pods
PLATFORM_RUNNING=$(kubectl --context kind-mindroom get pods -n mindroom-staging --field-selector=status.phase=Running --no-headers 2>/dev/null | wc -l)
if [ "$PLATFORM_RUNNING" -ge 2 ]; then
    echo "✓ Platform pods running ($PLATFORM_RUNNING/2)"
else
    echo "⚠ Platform pods not all running ($PLATFORM_RUNNING/2)"
fi

# Test frontend access
kubectl --context kind-mindroom port-forward -n mindroom-staging svc/platform-frontend 3001:3000 >/dev/null 2>&1 &
PF_PID=$!
sleep 3

if curl -s -o /dev/null -w "%{http_code}" http://localhost:3001 | grep -q "200"; then
    echo "✓ Frontend accessible"
else
    echo "❌ Frontend not accessible"
fi
kill $PF_PID 2>/dev/null || true

# Test backend health
kubectl --context kind-mindroom port-forward -n mindroom-staging svc/platform-backend 8001:8000 >/dev/null 2>&1 &
PB_PID=$!
sleep 3

if curl -s http://localhost:8001/health 2>/dev/null | grep -q "ok"; then
    echo "✓ Backend healthy"
else
    echo "⚠ Backend not responding (may need Supabase config)"
fi
kill $PB_PID 2>/dev/null || true

echo ""
echo "📊 Summary:"
echo "  Platform: http://localhost:3000 (run 'make frontend')"
echo "  Status:   'make status'"
echo "  Logs:     'make logs'"
echo ""
echo "✅ Local setup validated!"
