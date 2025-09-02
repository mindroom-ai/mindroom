#!/usr/bin/env bash

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Load .env file if it exists (only set variables that aren't already set)
if [ -f .env ]; then
    # Use a subshell to avoid polluting the environment
    while IFS='=' read -r key value; do
        # Only set if not already set
        if [[ ! -v $key ]] && [[ $key != \#* ]] && [[ -n $key ]]; then
            export "$key=$value"
        fi
    done < <(grep -v '^#' .env | grep -v '^$')
fi

# Set ports from environment variables or use defaults
BACKEND_PORT=${BACKEND_PORT:-8765}
FRONTEND_PORT=${FRONTEND_PORT:-3003}

# Detect if running in Docker
if [ -f /.dockerenv ] || [ -n "$DOCKER_CONTAINER" ]; then
    HOST="0.0.0.0"
    # In Docker, always use port 3003 for the frontend internally
    # The external port mapping is handled by Docker
    INTERNAL_FRONTEND_PORT=3003
    echo -e "${BLUE}Starting MindRoom Configuration Widget (Docker mode)...${NC}"
else
    HOST="localhost"
    INTERNAL_FRONTEND_PORT=${FRONTEND_PORT}
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

# Ensure venv exists (in Docker it's pre-installed, locally we create it)
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    uv sync --all-extras
fi

# Use the venv directly (works everywhere)
echo "Starting backend with existing virtual environment..."
.venv/bin/uvicorn mindroom.api.main:app --reload --host $HOST --port $BACKEND_PORT &
BACKEND_PID=$!

# Wait a moment for backend to start
sleep 2

# Start frontend
echo -e "${GREEN}Starting frontend development server...${NC}"
cd frontend
if [ ! -d "node_modules" ]; then
    echo "Installing frontend dependencies with pnpm..."
    pnpm install
fi

if [ "$HOST" = "0.0.0.0" ]; then
    VITE_BACKEND_PORT=$BACKEND_PORT BACKEND_PORT=$BACKEND_PORT FRONTEND_PORT=$INTERNAL_FRONTEND_PORT pnpm run dev:docker &
else
    VITE_BACKEND_PORT=$BACKEND_PORT BACKEND_PORT=$BACKEND_PORT FRONTEND_PORT=$INTERNAL_FRONTEND_PORT pnpm run dev &
fi
FRONTEND_PID=$!
cd ..  # Return to root directory

echo -e "${GREEN}Widget is running!${NC}"
echo -e "Frontend: http://$HOST:$INTERNAL_FRONTEND_PORT"
echo -e "Backend: http://$HOST:$BACKEND_PORT"
echo -e "\nPress Ctrl+C to stop both servers"

# Wait for both processes
wait
