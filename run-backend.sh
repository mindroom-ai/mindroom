#!/usr/bin/env bash

# Start both MindRoom bot and API server

trap 'kill $(jobs -p)' EXIT

# Detect script directory for resolving paths
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# Detect if running in container or local dev environment
if [ -d "/app" ] && [ -f "/app/config.yaml" ]; then
  # Container environment
  STORAGE_PATH="${STORAGE_PATH:-/app/mindroom_data}"
  CONFIG_PATH="${MINDROOM_CONFIG_PATH:-/app/config.yaml}"
  PYTHON_CMD=".venv/bin/python"
  UVICORN_CMD=".venv/bin/uvicorn"
else
  # Local development environment
  STORAGE_PATH="${STORAGE_PATH:-$SCRIPT_DIR/mindroom_data}"
  CONFIG_PATH="${MINDROOM_CONFIG_PATH:-$SCRIPT_DIR/config.yaml}"
  # Use uv run for local dev (works with nix-shell)
  PYTHON_CMD="uv run python"
  UVICORN_CMD="uv run uvicorn"
fi

CONFIG_TEMPLATE="${MINDROOM_CONFIG_TEMPLATE:-$CONFIG_PATH}"

# Ensure config directory exists and seed from template when necessary
CONFIG_DIR="$(dirname "$CONFIG_PATH")"
mkdir -p "$CONFIG_DIR"

if [ ! -f "$CONFIG_PATH" ] && [ -f "$CONFIG_TEMPLATE" ]; then
  cp "$CONFIG_TEMPLATE" "$CONFIG_PATH"
fi

# Guarantee the config file exists so the backend can write to it later
if [ ! -f "$CONFIG_PATH" ]; then
  touch "$CONFIG_PATH"
fi

# Ensure the runtime user can read/write the config (ignore errors on read-only mounts)
chmod 600 "$CONFIG_PATH" 2>/dev/null || true

# Create necessary directories
mkdir -p "$STORAGE_PATH"

echo "Starting MindRoom backend..."
echo "  Storage: $STORAGE_PATH"
echo "  Config: $CONFIG_PATH"

# Start bot in background (logs to stdout)
$PYTHON_CMD -m mindroom.cli run --log-level INFO --storage-path "$STORAGE_PATH" &

# Start API server in foreground (logs to stdout)
$UVICORN_CMD mindroom.api.main:app --host 0.0.0.0 --port 8765
