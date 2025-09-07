#!/usr/bin/env bash
# Database management wrapper

# Show help if no arguments provided
if [ $# -eq 0 ]; then
    python scripts/db-manager.py --help
else
    python scripts/db-manager.py "$@"
fi
