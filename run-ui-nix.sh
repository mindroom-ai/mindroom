#!/usr/bin/env bash

# Run the widget using Nix shell environment
# This ensures all dependencies are available

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

echo "Starting MindRoom Configuration Widget with Nix..."

# Use nix-shell to run the regular run-ui.sh script
nix-shell "$SCRIPT_DIR/shell.nix" --run "$SCRIPT_DIR/run-ui.sh"
