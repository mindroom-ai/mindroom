#!/usr/bin/env bash
# The MindRoom dashboard is bundled into the backend image.

set -e

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)

echo "MindRoom frontend is bundled into the backend image."
echo "Redeploying the backend for all customer instances instead."
exec "$SCRIPT_DIR/redeploy-mindroom-backend.sh"
