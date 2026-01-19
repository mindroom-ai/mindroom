#!/usr/bin/env bash

# Start MindRoom frontend
# Usage: ./run-frontend.sh [dev|prod]
# Default: dev

MODE="${1:-dev}"
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

cd "$SCRIPT_DIR/frontend"

# Install dependencies if node_modules is missing
if [ ! -d "node_modules" ]; then
  echo "Installing frontend dependencies..."
  bun install
fi

# Add node_modules/.bin to PATH for vite and other tools
export PATH="$PWD/node_modules/.bin:$PATH"

if [ "$MODE" = "prod" ] || [ "$MODE" = "production" ]; then
    echo "Starting frontend in PRODUCTION mode..."
    bun run build
    # Serve production build with preview server
    exec ./node_modules/.bin/vite preview --host 0.0.0.0 --port 3003
else
    echo "Starting frontend in DEVELOPMENT mode..."
    exec ./node_modules/.bin/vite --host 0.0.0.0 --port 3003
fi
