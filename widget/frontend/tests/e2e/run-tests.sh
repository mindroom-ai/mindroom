#!/bin/bash

# Script to run E2E tests locally

echo "🚀 Starting E2E tests for Widget Configuration"
echo "================================================"

# Colors for output
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Check if backend is running
echo -e "${YELLOW}Checking backend...${NC}"
if curl -s http://localhost:8765/api/health > /dev/null; then
    echo -e "${GREEN}✓ Backend is running${NC}"
else
    echo -e "${RED}✗ Backend is not running. Please start it first:${NC}"
    echo "  cd widget/backend && python src/main.py"
    exit 1
fi

# Check if frontend is running
echo -e "${YELLOW}Checking frontend...${NC}"
if curl -s http://localhost:3003 > /dev/null; then
    echo -e "${GREEN}✓ Frontend is running${NC}"
else
    echo -e "${YELLOW}⚠ Frontend is not running. Will start it automatically...${NC}"
fi

# Install Playwright browsers if needed
if [ ! -d "$HOME/.cache/ms-playwright" ]; then
    echo -e "${YELLOW}Installing Playwright browsers...${NC}"
    npx playwright install
fi

# Run tests
echo -e "${YELLOW}Running tests...${NC}"
echo "================================================"

# You can pass arguments to this script, e.g.:
# ./run-tests.sh --headed  (to see the browser)
# ./run-tests.sh --debug   (to debug tests)
# ./run-tests.sh --ui      (to use Playwright UI)

pnpm test:e2e "$@"

# Check exit code
if [ $? -eq 0 ]; then
    echo -e "${GREEN}✓ All tests passed!${NC}"
else
    echo -e "${RED}✗ Some tests failed. Check the report above.${NC}"
    echo -e "${YELLOW}Tip: Run with --headed to see what's happening in the browser${NC}"
fi
