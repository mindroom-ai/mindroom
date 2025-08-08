#!/bin/bash
# Simple wrapper script for cleaning up agent edit history
# Can be run via cron, e.g.: 0 */6 * * * /path/to/cleanup_agent_edits.sh

set -e

# Change to script directory
cd "$(dirname "$0")"

# Load environment if available
if [ -f "../.env" ]; then
    export $(grep -v '^#' ../.env | xargs)
fi

# Default database connection (for Docker setup)
export SYNAPSE_DB_HOST="${SYNAPSE_DB_HOST:-localhost}"
export SYNAPSE_DB_PORT="${SYNAPSE_DB_PORT:-5432}"
export SYNAPSE_DB_NAME="${SYNAPSE_DB_NAME:-synapse}"
export SYNAPSE_DB_USER="${SYNAPSE_DB_USER:-synapse}"
export SYNAPSE_DB_PASSWORD="${SYNAPSE_DB_PASSWORD:-synapse_password}"

# Run cleanup
# - Keep only the last edit for each message
# - Clean edits older than 1 hour
# - Only clean messages with 10+ edits (since you update every 0.1s, this is ~1 second of streaming)
uv run scripts/cleanup_agent_edits.py \
    --keep-last 1 \
    --older-than 1 \
    --min-edits 10 \
    "$@"
