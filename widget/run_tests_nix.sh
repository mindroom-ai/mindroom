#!/usr/bin/env bash

# Run tests using Nix shell environment
# This ensures all dependencies are available

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

echo "Running MindRoom Widget Tests with Nix..."
echo "========================================="

# Use nix-shell to run the tests
nix-shell "$SCRIPT_DIR/shell.nix" --run "$SCRIPT_DIR/run_tests.sh"
