#!/usr/bin/env bash

# Run MindRoom with Nix shell environment

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

echo "Starting MindRoom with Nix..."

trap 'kill $(jobs -p)' EXIT

# Run backend in nix-shell
nix-shell "$SCRIPT_DIR/shell.nix" --run "$SCRIPT_DIR/run-backend.sh" &

# Run frontend in nix-shell
nix-shell "$SCRIPT_DIR/shell.nix" --run "$SCRIPT_DIR/run-frontend.sh"
