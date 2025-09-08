#!/bin/bash

# Port forwarding script for MindRoom development
# Usage: ./scripts/forward-ports.sh [remote-host]

REMOTE_HOST=${1:-"your-server-hostname"}

echo "🚀 Forwarding MindRoom ports from $REMOTE_HOST..."

ssh -N \
  -L 3002:localhost:3002 \
  -L 3007:localhost:3007 \
  -L 8002:localhost:8002 \
  -L 5433:localhost:5433 \
  -L 6380:localhost:6380 \
  $REMOTE_HOST &

SSH_PID=$!
echo "✅ Port forwarding started (PID: $SSH_PID)"
echo ""
echo "Forwarded ports:"
echo "  Customer Portal:    http://localhost:3002"
echo "  Stripe Handler:     http://localhost:3007"
echo "  Dokku Provisioner:  http://localhost:8002"
echo "  PostgreSQL:         localhost:5433"
echo "  Redis:              localhost:6380"
echo ""
echo "Press Ctrl+C to stop forwarding..."

# Wait for Ctrl+C
trap "kill $SSH_PID; echo ''; echo '🛑 Port forwarding stopped'; exit" INT
wait $SSH_PID
