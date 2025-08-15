#!/usr/bin/env bash

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Load .env file if it exists
if [ -f .env ]; then
    export $(grep -v '^#' .env | xargs)
fi

# Set ports from environment variables or use defaults
BACKEND_PORT=${BACKEND_PORT:-8765}
FRONTEND_PORT=${FRONTEND_PORT:-3003}

# Detect if running in Docker
if [ -f /.dockerenv ] || [ -n "$DOCKER_CONTAINER" ]; then
    HOST="0.0.0.0"
    echo -e "${BLUE}Starting MindRoom Configuration Widget (Docker mode)...${NC}"
else
    HOST="localhost"
    echo -e "${BLUE}Starting MindRoom Configuration Widget...${NC}"
fi

# Function to kill background processes on exit
cleanup() {
    echo -e "\n${BLUE}Shutting down servers...${NC}"
    kill $BACKEND_PID $FRONTEND_PID 2>/dev/null
    exit
}

trap cleanup EXIT INT TERM

# Start backend (now from main mindroom package)
echo -e "${GREEN}Starting backend server on port $BACKEND_PORT...${NC}"
cd ..  # Go to project root

echo "Using uv for Python dependencies..."
if [ ! -d ".venv" ]; then
    uv sync --all-extras
fi
uv run uvicorn mindroom.api.main:app --reload --host $HOST --port $BACKEND_PORT &
BACKEND_PID=$!

cd widget

# Wait a moment for backend to start
sleep 2

# Start frontend
echo -e "${GREEN}Starting frontend development server...${NC}"
cd ../frontend  # Frontend is now at root level
if [ ! -d "node_modules" ]; then
    echo "Installing frontend dependencies with pnpm..."
    pnpm install
fi

if [ "$HOST" = "0.0.0.0" ]; then
    VITE_BACKEND_PORT=$BACKEND_PORT BACKEND_PORT=$BACKEND_PORT FRONTEND_PORT=$FRONTEND_PORT pnpm run dev:docker &
else
    VITE_BACKEND_PORT=$BACKEND_PORT BACKEND_PORT=$BACKEND_PORT FRONTEND_PORT=$FRONTEND_PORT pnpm run dev &
fi
FRONTEND_PID=$!
cd ../widget  # Return to widget directory

echo -e "${GREEN}Widget is running!${NC}"
echo -e "Frontend: http://$HOST:$FRONTEND_PORT"
echo -e "Backend: http://$HOST:$BACKEND_PORT"
echo -e "\nPress Ctrl+C to stop both servers"

# Wait for both processes
wait
