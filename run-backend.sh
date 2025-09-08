#!/usr/bin/env bash

# Script to run both the MindRoom bot and API server in the backend container
# This provides a cleaner separation of concerns:
# - Backend container: Bot + API
# - Frontend container: UI only

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Default values
BACKEND_PORT=${BACKEND_PORT:-8765}
LOG_LEVEL=${LOG_LEVEL:-INFO}
STORAGE_PATH=${STORAGE_PATH:-/app/mindroom_data}

# Detect if running in Docker
if [ -f /.dockerenv ] || [ -n "$DOCKER_CONTAINER" ]; then
    HOST="0.0.0.0"
    echo -e "${BLUE}Starting MindRoom Backend Services (Docker mode)...${NC}"
else
    HOST="localhost"
    echo -e "${BLUE}Starting MindRoom Backend Services...${NC}"
fi

# Function to kill background processes on exit
cleanup() {
    echo -e "\n${BLUE}Shutting down backend services...${NC}"
    if [ ! -z "$BOT_PID" ]; then
        kill $BOT_PID 2>/dev/null
        echo "Bot stopped"
    fi
    if [ ! -z "$API_PID" ]; then
        kill $API_PID 2>/dev/null
        echo "API stopped"
    fi
    exit
}

trap cleanup EXIT INT TERM

# Ensure storage directory exists
mkdir -p "$STORAGE_PATH"
mkdir -p /app/logs

# Start the Matrix bot
echo -e "${GREEN}Starting MindRoom bot...${NC}"
.venv/bin/python -m mindroom.cli run \
    --log-level "$LOG_LEVEL" \
    --storage-path "$STORAGE_PATH" \
    2>&1 | tee -a /app/logs/bot.log &
BOT_PID=$!

# Give the bot a moment to initialize
sleep 3

# Start the API server
echo -e "${GREEN}Starting API server on port $BACKEND_PORT...${NC}"
.venv/bin/uvicorn mindroom.api.main:app \
    --host "$HOST" \
    --port "$BACKEND_PORT" \
    --log-level "${LOG_LEVEL,,}" \
    2>&1 | tee -a /app/logs/api.log &
API_PID=$!

echo -e "${GREEN}Backend services are running!${NC}"
echo -e "API Server: http://$HOST:$BACKEND_PORT"
echo -e "Bot: Running and connected to Matrix"
echo -e "\nPress Ctrl+C to stop all services"

# Monitor both processes
while true; do
    # Check if bot is still running
    if ! kill -0 $BOT_PID 2>/dev/null; then
        echo -e "${RED}Bot process died unexpectedly${NC}"
        cleanup
    fi

    # Check if API is still running
    if ! kill -0 $API_PID 2>/dev/null; then
        echo -e "${RED}API process died unexpectedly${NC}"
        cleanup
    fi

    sleep 5
done
