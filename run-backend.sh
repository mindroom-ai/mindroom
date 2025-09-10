#!/usr/bin/env bash

# Start both MindRoom bot and API server

trap 'kill $(jobs -p)' EXIT

# Use STORAGE_PATH environment variable, defaulting to /mindroom_data if not set
STORAGE_PATH="${STORAGE_PATH:-/app/mindroom_data}"

# Create necessary directories
mkdir -p "$STORAGE_PATH"

# Start bot in background (logs to stdout)
.venv/bin/python -m mindroom.cli run --log-level INFO --storage-path "$STORAGE_PATH" &

# Start API server in foreground (logs to stdout)
.venv/bin/uvicorn mindroom.api.main:app --host 0.0.0.0 --port 8765
