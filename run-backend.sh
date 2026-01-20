#!/usr/bin/env bash

# Start both MindRoom bot and API server

trap 'kill $(jobs -p)' EXIT

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

if ! command -v uv &> /dev/null; then
  echo "❌ uv not found. Install: curl -LsSf https://astral.sh/uv/install.sh | sh"
  exit 1
fi

(cd "$SCRIPT_DIR" && uv sync --all-extras)

echo "Starting MindRoom backend..."

# Start bot in background, API server in foreground
uv run python -m mindroom.cli run --log-level INFO &
uv run uvicorn mindroom.api.main:app --host 0.0.0.0 --port 8765
