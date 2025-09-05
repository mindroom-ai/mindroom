#!/usr/bin/env bash

# Integration test for Stripe handler service
set -e

echo "🧪 Starting Stripe Handler Integration Tests..."

# Colors for output
GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Configuration
PORT=3006
BASE_URL="http://localhost:$PORT"

# Start the server in the background
echo "Starting server on port $PORT..."
NODE_ENV=test npm start &
SERVER_PID=$!

# Function to cleanup on exit
cleanup() {
    echo "Cleaning up..."
    kill $SERVER_PID 2>/dev/null || true
    exit
}
trap cleanup EXIT

# Wait for server to start
echo "Waiting for server to start..."
for i in {1..10}; do
    if curl -s $BASE_URL/health > /dev/null 2>&1; then
        echo -e "${GREEN}✓ Server started successfully${NC}"
        break
    fi
    sleep 1
done

# Test 1: Health check
echo "Test 1: Health check endpoint..."
HEALTH_RESPONSE=$(curl -s $BASE_URL/health)
if echo "$HEALTH_RESPONSE" | grep -q "healthy"; then
    echo -e "${GREEN}✓ Health check passed${NC}"
else
    echo -e "${RED}✗ Health check failed${NC}"
    echo "$HEALTH_RESPONSE"
    exit 1
fi

# Test 2: Test webhook endpoint (development only)
echo "Test 2: Webhook test endpoint..."
WEBHOOK_TEST_RESPONSE=$(curl -s $BASE_URL/webhooks/test)
if echo "$WEBHOOK_TEST_RESPONSE" | grep -q "Webhook endpoint is working"; then
    echo -e "${GREEN}✓ Webhook test endpoint passed${NC}"
else
    echo -e "${RED}✗ Webhook test endpoint failed${NC}"
    echo "$WEBHOOK_TEST_RESPONSE"
fi

# Test 3: Mock webhook with invalid signature
echo "Test 3: Webhook with invalid signature..."
WEBHOOK_RESPONSE=$(curl -s -X POST $BASE_URL/webhooks/stripe \
    -H "Content-Type: application/json" \
    -H "stripe-signature: invalid_signature" \
    -d '{"id":"evt_test","type":"customer.subscription.created"}' \
    -w "\n%{http_code}")

HTTP_CODE=$(echo "$WEBHOOK_RESPONSE" | tail -n1)
if [ "$HTTP_CODE" = "400" ]; then
    echo -e "${GREEN}✓ Invalid signature correctly rejected (400)${NC}"
else
    echo -e "${RED}✗ Invalid signature not rejected (expected 400, got $HTTP_CODE)${NC}"
    exit 1
fi

# Test 4: Missing signature
echo "Test 4: Webhook without signature..."
WEBHOOK_RESPONSE=$(curl -s -X POST $BASE_URL/webhooks/stripe \
    -H "Content-Type: application/json" \
    -d '{"id":"evt_test","type":"customer.subscription.created"}' \
    -w "\n%{http_code}")

HTTP_CODE=$(echo "$WEBHOOK_RESPONSE" | tail -n1)
if [ "$HTTP_CODE" = "400" ]; then
    echo -e "${GREEN}✓ Missing signature correctly rejected (400)${NC}"
else
    echo -e "${RED}✗ Missing signature not rejected (expected 400, got $HTTP_CODE)${NC}"
    exit 1
fi

echo -e "\n${GREEN}✅ All tests passed!${NC}"
echo "Server logs:"
echo "------------"
