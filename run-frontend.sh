#!/usr/bin/env bash

# Script to run ONLY the MindRoom frontend
# The API now runs in the backend container for better separation of concerns

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Set port from environment variable or use default
FRONTEND_PORT=${FRONTEND_PORT:-3003}

# Detect if running in Docker
if [ -f /.dockerenv ] || [ -n "$DOCKER_CONTAINER" ]; then
    HOST="0.0.0.0"
    # In Docker, always use port 3003 internally
    INTERNAL_FRONTEND_PORT=3003
    echo -e "${BLUE}Starting MindRoom Frontend (Docker mode)...${NC}"
else
    HOST="localhost"
    INTERNAL_FRONTEND_PORT=${FRONTEND_PORT}
    echo -e "${BLUE}Starting MindRoom Frontend...${NC}"
fi

# Function to kill background process on exit
cleanup() {
    echo -e "\n${BLUE}Shutting down frontend...${NC}"
    kill $FRONTEND_PID 2>/dev/null
    exit
}

trap cleanup EXIT INT TERM

# Start frontend
echo -e "${GREEN}Starting frontend server on port $INTERNAL_FRONTEND_PORT...${NC}"
cd frontend

# Install dependencies if needed
if [ ! -d "node_modules" ]; then
    echo "Installing frontend dependencies with pnpm..."
    pnpm install
fi

# Set VITE_API_URL to empty string for production mode (uses relative URLs)
# This makes the frontend use /api/* paths which will be proxied to the backend
if [ "$HOST" = "0.0.0.0" ]; then
    VITE_API_URL="" FRONTEND_PORT=$INTERNAL_FRONTEND_PORT pnpm run dev:docker &
else
    VITE_API_URL="" FRONTEND_PORT=$INTERNAL_FRONTEND_PORT pnpm run dev &
fi
FRONTEND_PID=$!

cd ..  # Return to root directory

echo -e "${GREEN}Frontend is running!${NC}"
echo -e "URL: http://$HOST:$INTERNAL_FRONTEND_PORT"
echo -e "\nPress Ctrl+C to stop the frontend"

# Wait for the process
wait $FRONTEND_PID
