#!/bin/bash
# Setup script for Authelia authentication

set -e

echo "🔐 Authelia Setup for MindRoom"
echo "=============================="
echo

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Check if instance name is provided
INSTANCE=${1:-default}
echo "Setting up Authelia for instance: $INSTANCE"

# Check if .env file exists
ENV_FILE="deploy/.env.$INSTANCE"
if [ ! -f "$ENV_FILE" ]; then
    echo -e "${RED}Error: Environment file $ENV_FILE not found!${NC}"
    echo "Please create an instance first with: ./deploy.py create $INSTANCE"
    exit 1
fi

# Source the environment file
source "$ENV_FILE"

# Create data directories
echo "Creating Authelia directories..."
mkdir -p "$DATA_DIR/authelia"
mkdir -p "$DATA_DIR/authelia-redis"

# Copy configuration files
echo "Setting up configuration files..."
cp -r deploy/authelia-config/* "$DATA_DIR/authelia/"

# Generate secrets if not already set
if grep -q "CHANGE_THIS" "$DATA_DIR/authelia/configuration.yml"; then
    echo -e "${YELLOW}Generating secure secrets...${NC}"

    JWT_SECRET=$(openssl rand -hex 32)
    SESSION_SECRET=$(openssl rand -hex 32)

    # Update configuration with secrets
    if [[ "$OSTYPE" == "darwin"* ]]; then
        # macOS
        sed -i '' "s/CHANGE_THIS_TO_A_RANDOM_SECRET_USE_OPENSSL_RAND_HEX_32/$JWT_SECRET/g" "$DATA_DIR/authelia/configuration.yml"
        sed -i '' "s/CHANGE_THIS_TO_A_RANDOM_SECRET_USE_OPENSSL_RAND_HEX_32/$SESSION_SECRET/g" "$DATA_DIR/authelia/configuration.yml"
    else
        # Linux
        sed -i "s/CHANGE_THIS_TO_A_RANDOM_SECRET_USE_OPENSSL_RAND_HEX_32/$JWT_SECRET/g" "$DATA_DIR/authelia/configuration.yml"
        sed -i "s/CHANGE_THIS_TO_A_RANDOM_SECRET_USE_OPENSSL_RAND_HEX_32/$SESSION_SECRET/g" "$DATA_DIR/authelia/configuration.yml"
    fi
fi

# Update domain in configuration
if [[ "$OSTYPE" == "darwin"* ]]; then
    sed -i '' "s/mindroom.localhost/$INSTANCE_DOMAIN/g" "$DATA_DIR/authelia/configuration.yml"
else
    sed -i "s/mindroom.localhost/$INSTANCE_DOMAIN/g" "$DATA_DIR/authelia/configuration.yml"
fi

echo
echo -e "${GREEN}✅ Authelia configuration ready!${NC}"
echo
echo "📝 Next steps:"
echo
echo "1. Add users (current default: admin/mindroom):"
echo "   Generate password hash:"
echo "   docker run --rm authelia/authelia:latest authelia crypto hash generate argon2 --password 'your-password'"
echo "   Then edit: $DATA_DIR/authelia/users_database.yml"
echo
echo "2. Start with authentication:"
echo "   docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.authelia.yml -p $INSTANCE up -d"
echo
echo "3. Access your instance:"
echo "   - App: https://$INSTANCE_DOMAIN"
echo "   - Auth: https://auth.$INSTANCE_DOMAIN"
echo
echo "4. For production, update:"
echo "   - SMTP settings in configuration.yml for password reset emails"
echo "   - Access control rules for your domains"
echo "   - Enable 2FA by changing policy to 'two_factor'"
echo
echo -e "${YELLOW}⚠️  Security Notes:${NC}"
echo "   - Change the default admin password immediately!"
echo "   - Use strong passwords (12+ characters)"
echo "   - Enable 2FA for production"
echo "   - Regularly update Authelia image"
