#!/usr/bin/env bash

# Start MindRoom frontend
# Usage: ./run-frontend.sh [dev|prod]
# Default: dev

MODE="${1:-dev}"

cd frontend

if [ "$MODE" = "prod" ] || [ "$MODE" = "production" ]; then
    echo "Starting frontend in PRODUCTION mode..."

    # Build production bundle if not already built
    if [ ! -d "dist" ]; then
        echo "Building production bundle..."
        npm run build
    else
        echo "Using existing production build in dist/"
    fi

    # Serve production build with preview server
    # Use empty VITE_API_URL for relative URLs (proxied to backend)
    exec npm run preview -- --host 0.0.0.0 --port 3003
else
    echo "Starting frontend in DEVELOPMENT mode..."

    # Use empty VITE_API_URL for relative URLs (proxied to backend)
    exec npm run dev -- --host 0.0.0.0 --port 3003
fi
