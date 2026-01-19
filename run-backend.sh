#!/usr/bin/env bash

# Start both MindRoom bot and API server

trap 'kill $(jobs -p)' EXIT

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# Ensure uv is available
if ! command -v uv &> /dev/null; then
  echo "❌ uv not found. Install: curl -LsSf https://astral.sh/uv/install.sh | sh"
  exit 1
fi

# Sync dependencies (fast no-op if already current)
(cd "$SCRIPT_DIR" && uv sync --all-extras)

# Set paths (container uses /app, local uses script dir)
APP_DIR="${SCRIPT_DIR}"
[ -d "/app" ] && [ -f "/app/config.yaml" ] && APP_DIR="/app"

STORAGE_PATH="${STORAGE_PATH:-$APP_DIR/mindroom_data}"
CONFIG_PATH="${MINDROOM_CONFIG_PATH:-$APP_DIR/config.yaml}"

mkdir -p "$(dirname "$CONFIG_PATH")" "$STORAGE_PATH"
[ ! -f "$CONFIG_PATH" ] && touch "$CONFIG_PATH"
chmod 600 "$CONFIG_PATH" 2>/dev/null || true

echo "Starting MindRoom backend..."
echo "  Storage: $STORAGE_PATH"
echo "  Config: $CONFIG_PATH"

# Start bot in background, API server in foreground
uv run python -m mindroom.cli run --log-level INFO --storage-path "$STORAGE_PATH" &
uv run uvicorn mindroom.api.main:app --host 0.0.0.0 --port 8765
