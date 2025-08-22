#!/bin/bash
# Simple Authelia setup for localhost testing

set -e

echo "🔐 Setting up Authelia for localhost testing"
echo "==========================================="
echo

# Create directories
mkdir -p deploy/instance_data/local-test/authelia

# Copy configuration
cp deploy/authelia-config/configuration-localhost.yml deploy/instance_data/local-test/authelia/configuration.yml
cp deploy/authelia-config/users_database.yml deploy/instance_data/local-test/authelia/

# Generate secrets
if command -v openssl &> /dev/null; then
    JWT_SECRET=$(openssl rand -hex 32)
    SESSION_SECRET=$(openssl rand -hex 32)
    ENCRYPTION_KEY=$(openssl rand -hex 32)
else
    JWT_SECRET=$(python -c "import secrets; print(secrets.token_hex(32))")
    SESSION_SECRET=$(python -c "import secrets; print(secrets.token_hex(32))")
    ENCRYPTION_KEY=$(python -c "import secrets; print(secrets.token_hex(32))")
fi

# Replace secrets in configuration
CONFIG_FILE="deploy/instance_data/local-test/authelia/configuration.yml"
sed -i "0,/CHANGE_THIS_TO_A_RANDOM_SECRET_USE_OPENSSL_RAND_HEX_32/s//$JWT_SECRET/" "$CONFIG_FILE"
sed -i "0,/CHANGE_THIS_TO_A_RANDOM_SECRET_USE_OPENSSL_RAND_HEX_32/s//$SESSION_SECRET/" "$CONFIG_FILE"
sed -i "0,/CHANGE_THIS_TO_A_RANDOM_SECRET_USE_OPENSSL_RAND_HEX_32/s//$ENCRYPTION_KEY/" "$CONFIG_FILE"

echo "✅ Configuration ready!"
echo
echo "📝 Default credentials: admin / mindroom"
echo
echo "To start the services, run:"
echo "  export INSTANCE_NAME=local-test"
echo "  export INSTANCE_DOMAIN=localhost"
echo "  export DATA_DIR=$(pwd)/deploy/instance_data/local-test"
echo "  export BACKEND_PORT=8767"
echo "  export FRONTEND_PORT=3005"
echo "  docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.authelia.yml -p local-test up -d"
echo
echo "Then access:"
echo "  - Frontend: http://localhost:3005"
echo "  - Authelia: http://localhost:9091"
