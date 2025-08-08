#!/bin/bash
# Run cleanup script against dockerized PostgreSQL

set -e

# Get the absolute path to the script directory
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Use synapse-postgres as default container name (override with POSTGRES_CONTAINER env var)
POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-synapse-postgres}"

# Check if container exists
if ! docker inspect "$POSTGRES_CONTAINER" >/dev/null 2>&1; then
    echo "Error: Container '$POSTGRES_CONTAINER' not found"
    echo "Set POSTGRES_CONTAINER environment variable to specify a different container"
    exit 1
fi

# Get container's network
NETWORK=$(docker inspect "$POSTGRES_CONTAINER" --format '{{range $key, $value := .NetworkSettings.Networks}}{{$key}}{{end}}' | head -n1)

# Get container's PostgreSQL host (should be the container name in the docker network)
POSTGRES_HOST="$POSTGRES_CONTAINER"

# Run the Python cleanup script inside a temporary Docker container that can access the database
docker run --rm \
    --network "$NETWORK" \
    -v "${SCRIPT_DIR}:/scripts:ro" \
    -e SYNAPSE_DB_HOST="$POSTGRES_HOST" \
    -e SYNAPSE_DB_PORT=5432 \
    -e SYNAPSE_DB_NAME=synapse \
    -e SYNAPSE_DB_USER=synapse \
    -e SYNAPSE_DB_PASSWORD=synapse_password \
    python:3.11-slim \
    bash -c "
        pip install --quiet psycopg2-binary typer rich && \
        python /scripts/cleanup_agent_edits.py \
            --host $POSTGRES_HOST \
            --port 5432 \
            --database synapse \
            --user synapse \
            --password synapse_password \
            $*
    "
