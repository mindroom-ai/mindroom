#!/usr/bin/env bash

# Start both MindRoom bot and API server

trap 'kill $(jobs -p)' EXIT

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# Ensure uv is available
if ! command -v uv &> /dev/null; then
  echo "❌ uv not found. Install: curl -LsSf https://astral.sh/uv/install.sh | sh"
  exit 1
fi

# Detect container vs local dev environment
if [ -d "/app" ] && [ -f "/app/config.yaml" ]; then
  STORAGE_PATH="${STORAGE_PATH:-/app/mindroom_data}"
  CONFIG_PATH="${MINDROOM_CONFIG_PATH:-/app/config.yaml}"
else
  # Install dependencies if .venv doesn't exist
  if [ ! -d "$SCRIPT_DIR/.venv" ]; then
    echo "📦 Installing Python dependencies..."
    (cd "$SCRIPT_DIR" && uv sync --all-extras)
  fi
  STORAGE_PATH="${STORAGE_PATH:-$SCRIPT_DIR/mindroom_data}"
  CONFIG_PATH="${MINDROOM_CONFIG_PATH:-$SCRIPT_DIR/config.yaml}"
fi

CONFIG_TEMPLATE="${MINDROOM_CONFIG_TEMPLATE:-$CONFIG_PATH}"
CONFIG_DIR="$(dirname "$CONFIG_PATH")"
mkdir -p "$CONFIG_DIR" "$STORAGE_PATH"

if [ ! -f "$CONFIG_PATH" ] && [ -f "$CONFIG_TEMPLATE" ]; then
  cp "$CONFIG_TEMPLATE" "$CONFIG_PATH"
fi
[ ! -f "$CONFIG_PATH" ] && touch "$CONFIG_PATH"
chmod 600 "$CONFIG_PATH" 2>/dev/null || true

echo "Starting MindRoom backend..."
echo "  Storage: $STORAGE_PATH"
echo "  Config: $CONFIG_PATH"

# Start bot in background, API server in foreground
uv run python -m mindroom.cli run --log-level INFO --storage-path "$STORAGE_PATH" &
uv run uvicorn mindroom.api.main:app --host 0.0.0.0 --port 8765
