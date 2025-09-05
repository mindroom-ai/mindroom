#!/usr/bin/env bash
# Run any command with .env loaded using uvx and python-dotenv

if ! command -v uvx &> /dev/null; then
    echo "❌ uvx not found. Please install uv first."
    exit 1
fi

# Run the command with .env loaded
exec uvx --from "python-dotenv[cli]" dotenv run -- "$@"
