#!/usr/bin/env bash

# Run MindRoom with Nix shell environment
# This ensures all dependencies are available

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

echo "Starting MindRoom with Nix..."

# Start both backend and frontend in nix-shell
trap 'kill $(jobs -p)' EXIT

# Backend (bot + API)
nix-shell "$SCRIPT_DIR/shell.nix" --run ".venv/bin/python -m mindroom.cli run --log-level INFO --storage-path ./mindroom_data" &
nix-shell "$SCRIPT_DIR/shell.nix" --run ".venv/bin/uvicorn mindroom.api.main:app --reload --host localhost --port 8765" &

# Frontend
cd frontend
nix-shell "$SCRIPT_DIR/shell.nix" --run "pnpm run dev"
