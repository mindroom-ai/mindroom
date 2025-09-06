#!/bin/sh
set -e

# Generate runtime config from environment variables
cat > /usr/share/nginx/html/config.js <<EOF
window.ENV_CONFIG = {
  VITE_SUPABASE_URL: "${SUPABASE_URL:-}",
  VITE_SUPABASE_SERVICE_KEY: "${SUPABASE_SERVICE_KEY:-}",
  VITE_PROVISIONER_URL: "${PROVISIONER_URL:-http://instance-provisioner:8002}",
  VITE_PROVISIONER_API_KEY: "${PROVISIONER_API_KEY:-}",
  VITE_STRIPE_SECRET_KEY: "${STRIPE_SECRET_KEY:-}"
};
EOF

echo "Runtime configuration generated:"
cat /usr/share/nginx/html/config.js

# Execute the original nginx command
exec "$@"
