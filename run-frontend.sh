#!/usr/bin/env bash

# Start MindRoom frontend

cd frontend

# Use empty VITE_API_URL for relative URLs (proxied to backend)
exec npm run dev -- --host 0.0.0.0 --port 3003
