#!/usr/bin/env bash
# Run any command with .env loaded using uvx and python-dotenv

exec uvx --from "python-dotenv[cli]" dotenv run -- "$@"
