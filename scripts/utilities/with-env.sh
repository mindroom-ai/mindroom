#!/bin/bash

# Run any command with .env file loaded
# Usage: ./scripts/with-env.sh <command> [args...]
#
# Examples:
#   ./scripts/with-env.sh npm run dev
#   ./scripts/with-env.sh docker-compose up
#   ./scripts/with-env.sh terraform apply

set -e

if [ ! -f .env ]; then
    echo "❌ Error: .env file not found"
    echo "Please create a .env file in the project root"
    exit 1
fi

if [ $# -eq 0 ]; then
    echo "Usage: $0 <command> [args...]"
    echo ""
    echo "This script runs any command with environment variables from .env loaded."
    echo ""
    echo "Examples:"
    echo "  $0 npm run dev"
    echo "  $0 docker-compose up"
    echo "  $0 terraform apply"
    exit 1
fi

# Use uvx with python-dotenv if available, otherwise fall back to source
if command -v uvx &> /dev/null; then
    exec uvx --from "python-dotenv[cli]" dotenv run -- "$@"
else
    # Fallback to sourcing .env
    set -a
    source .env
    set +a
    exec "$@"
fi
