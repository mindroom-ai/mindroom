#!/usr/bin/env bash

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}Starting MindRoom Configuration Widget...${NC}"

# Function to kill background processes on exit
cleanup() {
    echo -e "\n${BLUE}Shutting down servers...${NC}"
    kill $BACKEND_PID $FRONTEND_PID 2>/dev/null
    exit
}

trap cleanup EXIT INT TERM

# Start backend
echo -e "${GREEN}Starting backend server...${NC}"
cd backend

# Check if uv is available
if ! command -v uv &> /dev/null; then
    echo -e "${BLUE}Error: 'uv' is not installed.${NC}"
    echo "Please install uv: https://github.com/astral-sh/uv"
    echo "Or use run-nix.sh which provides all dependencies."
    exit 1
fi

echo "Using uv for Python dependencies..."
if [ ! -d ".venv" ]; then
    uv sync
fi
uv run uvicorn src.main:app --reload &
BACKEND_PID=$!

cd ..

# Wait a moment for backend to start
sleep 2

# Start frontend
echo -e "${GREEN}Starting frontend development server...${NC}"
cd frontend
if [ ! -d "node_modules" ]; then
    echo "Installing frontend dependencies..."
    npm install
fi

npm run dev &
FRONTEND_PID=$!
cd ..

echo -e "${GREEN}Widget is running!${NC}"
echo -e "Frontend: http://localhost:3000"
echo -e "Backend: http://localhost:8000"
echo -e "\nPress Ctrl+C to stop both servers"

# Wait for both processes
wait
