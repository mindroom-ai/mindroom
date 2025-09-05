#!/bin/sh
# Start MindRoom platform services locally (without Docker)

echo "🚀 Starting MindRoom Platform Services Locally"
echo "=============================================="
echo ""

# Load environment
if [ -f .env ]; then
    export $(grep -v '^#' .env | xargs)
    echo "✅ Environment loaded"
else
    echo "❌ No .env file found"
    exit 1
fi

# Function to start a service
start_service() {
    local name=$1
    local dir=$2
    local cmd=$3
    local port=$4

    echo "Starting $name on port $port..."
    cd "$dir" 2>/dev/null || {
        echo "  ❌ Directory not found: $dir"
        return 1
    }

    # Kill any existing process on this port
    lsof -ti:$port | xargs kill -9 2>/dev/null

    # Start the service
    $cmd > /tmp/${name}.log 2>&1 &
    echo "  PID: $!"

    cd - > /dev/null
}

echo "📦 Starting services..."
echo ""

# Start Stripe Handler
echo "1️⃣ Stripe Handler"
start_service "stripe-handler" "services/stripe-handler" "npm start" 3007

# Give it time to start
sleep 2

# Test the service
echo ""
echo "🧪 Testing services..."
curl -s http://localhost:3007/health > /dev/null && echo "✅ Stripe Handler: OK" || echo "❌ Stripe Handler: Failed"

echo ""
echo "=============================================="
echo "✅ Services started!"
echo ""
echo "📊 Service URLs:"
echo "  - Stripe Handler: http://localhost:3007"
echo "  - Health Check:   http://localhost:3007/health"
echo ""
echo "📝 Logs:"
echo "  - Stripe Handler: tail -f /tmp/stripe-handler.log"
echo ""
echo "🛑 To stop all services:"
echo "  pkill -f 'npm start'"
echo ""
echo "⚠️  Note: Only Stripe Handler is currently functional."
echo "Other services need additional setup:"
echo "  - Dokku Provisioner needs Python environment"
echo "  - Customer Portal needs build step"
echo "  - Admin Dashboard needs build step"
