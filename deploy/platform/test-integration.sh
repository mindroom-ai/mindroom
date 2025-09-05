#!/bin/sh
# Simple integration test for local development

echo "🧪 MindRoom Platform Integration Test"
echo "====================================="
echo ""

# Check if .env exists
if [ ! -f ".env" ]; then
    echo "⚠️  Creating .env from template..."
    cp .env.example .env
    echo "Please configure .env with:"
    echo "  - SUPABASE_URL and keys (create project at supabase.com)"
    echo "  - STRIPE_SECRET_KEY (get from stripe.com dashboard)"
    echo ""
fi

# Test 1: Can we start the Stripe handler?
echo "📦 Test 1: Stripe Handler"
echo "--------------------------"
cd services/stripe-handler
if [ -d "dist" ]; then
    echo "✅ Build exists"
    # Start in background
    npm start > /tmp/stripe.log 2>&1 &
    STRIPE_PID=$!
    sleep 2

    # Test health endpoint
    curl -s http://localhost:3007/health > /dev/null 2>&1 && echo "✅ Health endpoint works" || echo "❌ Health endpoint failed"

    # Kill the process
    kill $STRIPE_PID 2>/dev/null
else
    echo "❌ Not built. Run: npm install && npm run build"
fi
cd ../..
echo ""

# Test 2: Check Docker
echo "🐳 Test 2: Docker"
echo "-----------------"
docker version > /dev/null 2>&1 && echo "✅ Docker is running" || echo "❌ Docker not available"
echo ""

# Test 3: Check if we can connect to deployed servers
echo "🌐 Test 3: Infrastructure"
echo "-------------------------"
if [ -d "infrastructure/terraform" ]; then
    cd infrastructure/terraform
    if [ -f "terraform.tfstate" ]; then
        DOKKU_IP=$(terraform output -raw dokku_server_ip 2>/dev/null)
        PLATFORM_IP=$(terraform output -raw platform_server_ip 2>/dev/null)

        if [ -n "$DOKKU_IP" ]; then
            echo "Dokku Server: $DOKKU_IP"
            timeout 2 nc -zv $DOKKU_IP 22 2>/dev/null && echo "  ✅ SSH port open" || echo "  ❌ SSH unreachable"
        fi

        if [ -n "$PLATFORM_IP" ]; then
            echo "Platform Server: $PLATFORM_IP"
            timeout 2 nc -zv $PLATFORM_IP 22 2>/dev/null && echo "  ✅ SSH port open" || echo "  ❌ SSH unreachable"
        fi
    else
        echo "ℹ️  Infrastructure not deployed yet"
    fi
    cd ../..
fi
echo ""

# Test 4: Local Docker Compose
echo "🐋 Test 4: Docker Compose"
echo "-------------------------"
if [ -f "deploy/platform/docker-compose.local.yml" ]; then
    echo "✅ Local docker-compose.yml exists"
    docker-compose -f deploy/platform/docker-compose.local.yml config > /dev/null 2>&1 && \
        echo "✅ Docker Compose config is valid" || \
        echo "⚠️  Docker Compose config has issues (missing env vars?)"
else
    echo "❌ docker-compose.local.yml not found"
fi
echo ""

echo "====================================="
echo "📊 SUMMARY"
echo ""
echo "To run the platform locally:"
echo "1. Configure .env with your Supabase and Stripe keys"
echo "2. Build services: cd services/stripe-handler && npm install && npm run build"
echo "3. Start with Docker: docker-compose -f deploy/platform/docker-compose.local.yml up"
echo ""
echo "Or test individual services:"
echo "  cd services/stripe-handler && npm start"
echo "  cd apps/customer-portal && npm run dev"
