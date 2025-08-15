#!/bin/bash
# Entrypoint script for Synapse container

# Ensure proper ownership of data directories
echo "Setting permissions for UID ${UID:-1000} and GID ${GID:-1000}..."
chown -R ${UID:-1000}:${GID:-1000} /data
# Ensure media_store directory exists with correct permissions
mkdir -p /data/media_store
chown -R ${UID:-1000}:${GID:-1000} /data/media_store
chmod -R 755 /data/media_store

# Generate signing key if it doesn't exist
if [ ! -f "/data/signing.key" ]; then
    echo "No signing key found. Generating one..."
    python -m synapse.crypto.signing_key -o /data/signing.key
    echo "Signing key generated."
fi

# Execute the original Synapse entrypoint
exec /start.py
