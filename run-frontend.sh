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
  pnpm install
fi

if [ "$MODE" = "prod" ] || [ "$MODE" = "production" ]; then
    echo "Starting frontend in PRODUCTION mode..."
    pnpm run build
    # Serve production build with preview server
    # Use empty VITE_API_URL for relative URLs (proxied to backend)
    exec pnpm run preview -- --host 0.0.0.0 --port 3003
else
    echo "Starting frontend in DEVELOPMENT mode..."

    # Use empty VITE_API_URL for relative URLs (proxied to backend)
    exec pnpm run dev -- --host 0.0.0.0 --port 3003
fi
