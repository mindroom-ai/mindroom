#!/bin/bash
# Entrypoint script for Synapse container

# Generate signing key if it doesn't exist
if [ ! -f "/data/signing.key" ]; then
    echo "No signing key found. Generating one..."
    python -m synapse.crypto.signing_key -o /data/signing.key
    echo "Signing key generated."
fi

# Execute the original Synapse entrypoint
exec /start.py
