#!/bin/bash
# Run cleanup script against dockerized PostgreSQL

set -e

# Change to script directory
cd "$(dirname "$0")"

# Use synapse-postgres as default container name
POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-synapse-postgres}"

# Get container's PostgreSQL host (should be the container name in the docker network)
POSTGRES_HOST="$POSTGRES_CONTAINER"

# Run the Python cleanup script inside a temporary Docker container that can access the database
docker run --rm \
    --network $(docker inspect "$POSTGRES_CONTAINER" --format '{{range $key, $value := .NetworkSettings.Networks}}{{$key}}{{end}}' | head -n1) \
    -v "$PWD:/scripts:ro" \
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
