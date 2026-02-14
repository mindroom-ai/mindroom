#!/usr/bin/env bash

# Start MindRoom (bot + API server)

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

if ! command -v uv &> /dev/null; then
  echo "❌ uv not found. Install: curl -LsSf https://astral.sh/uv/install.sh | sh"
  exit 1
fi

(cd "$SCRIPT_DIR" && uv sync --all-extras)

echo "Starting MindRoom backend..."
uv run mindroom run "$@"
