#!/usr/bin/env bash

# Start MindRoom frontend
# Usage: ./run-frontend.sh [dev|prod]

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

if ! command -v bun &> /dev/null; then
  echo "❌ bun not found. Install: curl -fsSL https://bun.sh/install | bash"
  exit 1
fi

cd "$SCRIPT_DIR/frontend"
bun install

if [ "${1:-dev}" = "prod" ] || [ "$1" = "production" ]; then
  bun run build
  exec bun run vite preview --host 0.0.0.0 --port 3003
else
  exec bun run vite --host 0.0.0.0 --port 3003
fi
