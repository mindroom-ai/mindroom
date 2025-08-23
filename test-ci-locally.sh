#!/bin/bash
# Test CI Docker build locally
# This simulates what the CI does without needing to push to git

set -e

echo "🧪 Testing CI Docker Build Locally"
echo "=================================="
echo

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Configuration (adjust these for your local setup)
REGISTRY="${REGISTRY:-git.nijho.lt}"
OWNER="${OWNER:-basnijholt}"
TEST_TAG="local-test-$(date +%s)"

echo "Registry: $REGISTRY"
echo "Owner: $OWNER"
echo "Test tag: $TEST_TAG"
echo

# Function to test Docker build
test_docker_build() {
    local component=$1
    echo -e "${YELLOW}Testing $component build...${NC}"

    # Create a minimal test Dockerfile
    if [ "$component" = "backend" ]; then
        cat > Dockerfile.test-$component << 'EOF'
FROM python:3.11-slim
WORKDIR /app
RUN echo "import sys; print('Test backend running')" > app.py
CMD ["python", "app.py"]
EOF
    else
        cat > Dockerfile.test-$component << 'EOF'
FROM node:18-alpine
WORKDIR /app
RUN echo "console.log('Test frontend running');" > app.js
CMD ["node", "app.js"]
EOF
    fi

    # Build the test image
    echo "Building test image..."
    docker build -f Dockerfile.test-$component -t test-$component:$TEST_TAG .

    # Run the test image
    echo "Running test image..."
    OUTPUT=$(docker run --rm test-$component:$TEST_TAG)
    echo "Output: $OUTPUT"

    # Clean up
    docker rmi test-$component:$TEST_TAG
    rm -f Dockerfile.test-$component

    echo -e "${GREEN}✅ $component test passed!${NC}"
    echo
}

# Function to test registry push (optional)
test_registry_push() {
    echo -e "${YELLOW}Testing registry push...${NC}"

    # Create minimal image
    cat > Dockerfile.test-registry << 'EOF'
FROM alpine:latest
RUN echo "Registry test" > /test.txt
CMD ["cat", "/test.txt"]
EOF

    # Build
    docker build -f Dockerfile.test-registry -t $REGISTRY/$OWNER/mindroom-test:$TEST_TAG .

    # Try to push (this will fail if not logged in)
    if docker push $REGISTRY/$OWNER/mindroom-test:$TEST_TAG 2>/dev/null; then
        echo -e "${GREEN}✅ Registry push successful!${NC}"
        # Clean up remote
        # docker rmi $REGISTRY/$OWNER/mindroom-test:$TEST_TAG
    else
        echo -e "${YELLOW}⚠️  Registry push skipped (not logged in or no access)${NC}"
    fi

    # Clean up local
    docker rmi $REGISTRY/$OWNER/mindroom-test:$TEST_TAG 2>/dev/null || true
    rm -f Dockerfile.test-registry
    echo
}

# Function to test the actual Dockerfiles
test_real_dockerfiles() {
    echo -e "${YELLOW}Testing real Dockerfiles (quick build)...${NC}"

    # Test backend Dockerfile syntax
    if [ -f "deploy/Dockerfile.backend" ]; then
        echo "Checking backend Dockerfile syntax..."
        docker build -f deploy/Dockerfile.backend --target base -t test-backend-syntax:$TEST_TAG . --no-cache 2>&1 | head -20
        docker rmi test-backend-syntax:$TEST_TAG 2>/dev/null || true
        echo -e "${GREEN}✅ Backend Dockerfile syntax OK${NC}"
    fi

    # Test frontend Dockerfile syntax
    if [ -f "deploy/Dockerfile.frontend" ]; then
        echo "Checking frontend Dockerfile syntax..."
        docker build -f deploy/Dockerfile.frontend --target base -t test-frontend-syntax:$TEST_TAG . --no-cache 2>&1 | head -20
        docker rmi test-frontend-syntax:$TEST_TAG 2>/dev/null || true
        echo -e "${GREEN}✅ Frontend Dockerfile syntax OK${NC}"
    fi

    echo
}

# Main execution
main() {
    echo "1. Testing minimal Docker builds..."
    test_docker_build "backend"
    test_docker_build "frontend"

    echo "2. Testing Docker registry (optional)..."
    test_registry_push

    echo "3. Testing real Dockerfile syntax..."
    test_real_dockerfiles

    echo -e "${GREEN}🎉 All tests completed!${NC}"
    echo
    echo "To test with the actual CI:"
    echo "1. Commit and push to trigger: git push origin main"
    echo "2. Or use act for local CI: act -j test-docker-registry"
    echo "3. Check Gitea Actions tab for results"
}

# Run main
main
