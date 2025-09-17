#!/usr/bin/env bash

# Start both MindRoom bot and API server

trap 'kill $(jobs -p)' EXIT

# Use STORAGE_PATH environment variable, defaulting to /mindroom_data if not set
STORAGE_PATH="${STORAGE_PATH:-/app/mindroom_data}"

# Determine config location. Allow overrides so we can keep the writable config
# on the persistent volume even when a read-only template is mounted at
# /app/config.yaml (e.g. Kubernetes ConfigMap).
CONFIG_PATH="${MINDROOM_CONFIG_PATH:-/app/config.yaml}"
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

# Start bot in background (logs to stdout)
.venv/bin/python -m mindroom.cli run --log-level INFO --storage-path "$STORAGE_PATH" &

# Start API server in foreground (logs to stdout)
.venv/bin/uvicorn mindroom.api.main:app --host 0.0.0.0 --port 8765
