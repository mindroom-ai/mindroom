#!/bin/bash
# Setup script for secure Matrix server configuration

set -e

echo "🔐 Secure Matrix Setup for MindRoom"
echo "===================================="
echo

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Check if instance name is provided
INSTANCE=${1:-default}
MATRIX_TYPE=${2:-synapse}

echo "Setting up secure Matrix for instance: $INSTANCE"
echo "Matrix server type: $MATRIX_TYPE"
echo

# Check if .env file exists
ENV_FILE=".env.$INSTANCE"
if [ ! -f "$ENV_FILE" ]; then
    echo -e "${RED}Error: Environment file $ENV_FILE not found!${NC}"
    echo "Please create an instance first with: ./deploy.py create $INSTANCE"
    exit 1
fi

# Source the environment file
source "$ENV_FILE"

# Function to generate secure random secret
generate_secret() {
    if command -v openssl &> /dev/null; then
        openssl rand -hex 32
    else
        python3 -c "import secrets; print(secrets.token_hex(32))"
    fi
}

if [ "$MATRIX_TYPE" == "synapse" ]; then
    echo -e "${BLUE}Configuring Synapse for secure operation...${NC}"

    # Create Synapse config directory
    SYNAPSE_DIR="$DATA_DIR/synapse"
    mkdir -p "$SYNAPSE_DIR"

    # Copy base configuration
    if [ -f "synapse-config/homeserver.yaml" ]; then
        cp "synapse-config/homeserver.yaml" "$SYNAPSE_DIR/homeserver.yaml"
    else
        echo -e "${YELLOW}Warning: synapse-config/homeserver.yaml not found, using template${NC}"
        # Create from our secure template
        cat > "$SYNAPSE_DIR/homeserver.yaml" << 'EOF'
# Secure Synapse Configuration
# Auto-generated for instance: INSTANCE_NAME

server_name: "INSTANCE_DOMAIN"
pid_file: /data/homeserver.pid
public_baseurl: "https://INSTANCE_DOMAIN"

listeners:
  - port: 8008
    tls: false
    type: http
    x_forwarded: true
    resources:
      - names: [client, federation]
        compress: false

database:
  name: psycopg2
  args:
    user: synapse
    password: synapse_password
    database: synapse
    host: postgres
    cp_min: 5
    cp_max: 10

# SECURITY: Registration disabled by default
enable_registration: false
registration_shared_secret: "REGISTRATION_SHARED_SECRET"

# Rate limiting
rc_registration:
  per_second: 0.17
  burst_count: 3

# Media settings
media_store_path: "/data/media_store"
max_upload_size: "50M"

# Security
password_config:
  enabled: true
  minimum_length: 8

allow_guest_access: false
report_stats: false

# Secrets
signing_key_path: "/data/signing.key"
macaroon_secret_key: "MACAROON_SECRET_KEY"
form_secret: "FORM_SECRET"

log_config: "/data/log.config"
EOF
    fi

    # Generate secrets
    echo "Generating secure secrets..."
    REGISTRATION_SECRET=$(generate_secret)
    MACAROON_SECRET=$(generate_secret)
    FORM_SECRET=$(generate_secret)

    # Update configuration with instance-specific values
    sed -i "s/INSTANCE_NAME/$INSTANCE/g" "$SYNAPSE_DIR/homeserver.yaml"
    sed -i "s/INSTANCE_DOMAIN/$INSTANCE_DOMAIN/g" "$SYNAPSE_DIR/homeserver.yaml"
    sed -i "s/REGISTRATION_SHARED_SECRET/$REGISTRATION_SECRET/g" "$SYNAPSE_DIR/homeserver.yaml"
    sed -i "s/MACAROON_SECRET_KEY/$MACAROON_SECRET/g" "$SYNAPSE_DIR/homeserver.yaml"
    sed -i "s/FORM_SECRET/$FORM_SECRET/g" "$SYNAPSE_DIR/homeserver.yaml"

    # Save the registration secret for bot registration
    echo "$REGISTRATION_SECRET" > "$DATA_DIR/.matrix_registration_secret"
    chmod 600 "$DATA_DIR/.matrix_registration_secret"

    echo -e "${GREEN}✅ Synapse configured securely!${NC}"
    echo
    echo -e "${YELLOW}📝 Important Security Information:${NC}"
    echo "• Registration is DISABLED by default"
    echo "• Bots will register using shared secret (stored in $DATA_DIR/.matrix_registration_secret)"
    echo "• To manually register a user:"
    echo "  docker exec ${INSTANCE}-synapse register_new_matrix_user \\"
    echo "    -u USERNAME -p PASSWORD -a -c /data/homeserver.yaml"
    echo

elif [ "$MATRIX_TYPE" == "tuwunel" ]; then
    echo -e "${BLUE}Configuring Tuwunel for secure operation...${NC}"

    # Create Tuwunel config directory
    TUWUNEL_DIR="$DATA_DIR/tuwunel"
    mkdir -p "$TUWUNEL_DIR"

    # Copy secure configuration
    if [ -f "tuwunel-secure.toml" ]; then
        cp "tuwunel-secure.toml" "$TUWUNEL_DIR/tuwunel.toml"
    else
        echo -e "${YELLOW}Using standard tuwunel.toml - updating for security${NC}"
        cp "tuwunel.toml" "$TUWUNEL_DIR/tuwunel.toml"
        # Disable registration
        sed -i 's/allow_registration = true/allow_registration = false/g' "$TUWUNEL_DIR/tuwunel.toml"
        sed -i 's/yes_i_am_very_very_sure_i_want_an_open_registration_server_prone_to_abuse = true/yes_i_am_very_very_sure_i_want_an_open_registration_server_prone_to_abuse = false/g' "$TUWUNEL_DIR/tuwunel.toml"
        sed -i 's/allow_guest_registration = true/allow_guest_registration = false/g' "$TUWUNEL_DIR/tuwunel.toml"
    fi

    echo -e "${GREEN}✅ Tuwunel configured securely!${NC}"
    echo
    echo -e "${YELLOW}📝 Important Security Information:${NC}"
    echo "• Registration is DISABLED"
    echo "• Guest access is DISABLED"
    echo "• Bots must be pre-created manually before starting MindRoom"
    echo
fi

# Create bot registration script
cat > "$DATA_DIR/register-bot.sh" << 'EOF'
#!/bin/bash
# Register a MindRoom bot user

HOMESERVER=${1:-http://localhost:8008}
USERNAME=${2:-mindroom_bot}
PASSWORD=${3:-$(openssl rand -hex 16 2>/dev/null || python3 -c "import secrets; print(secrets.token_hex(16))")}

echo "Registering bot: $USERNAME"

if [ -f ".matrix_registration_secret" ]; then
    SECRET=$(cat .matrix_registration_secret)
    # Use shared secret registration for Synapse
    docker exec -it ${INSTANCE_NAME}-synapse register_new_matrix_user \
        -u "$USERNAME" -p "$PASSWORD" -a -c /data/homeserver.yaml
else
    echo "Manual registration required for Tuwunel"
    echo "Username: $USERNAME"
    echo "Password: $PASSWORD"
    echo "Please manually create this user in your Matrix server"
fi
EOF

chmod +x "$DATA_DIR/register-bot.sh"

echo -e "${GREEN}✅ Secure Matrix setup complete!${NC}"
echo
echo "🚀 Next steps:"
echo "1. Start your instance: ./deploy.py start $INSTANCE"
echo "2. Register bots: $DATA_DIR/register-bot.sh"
echo "3. Configure MindRoom with bot credentials"
echo
echo -e "${YELLOW}⚠️  Security Best Practices:${NC}"
echo "• Never enable open registration on public servers"
echo "• Use strong passwords for all bot accounts"
echo "• Regularly rotate the registration shared secret"
echo "• Monitor server logs for unauthorized access attempts"
echo "• Consider using registration tokens for controlled user onboarding"
echo
echo "For more control over registration, see:"
echo "• Synapse: https://matrix-org.github.io/synapse/latest/usage/administration/admin_api/registration_tokens.html"
echo "• Tuwunel: Check admin commands in the documentation"
