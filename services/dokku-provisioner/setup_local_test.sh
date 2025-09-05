#!/bin/bash
# Setup script for local testing of Dokku Provisioner

set -e

echo "🚀 MindRoom Dokku Provisioner - Local Test Setup"
echo "================================================"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Step 1: Check Python
echo -e "\n${BLUE}Step 1: Checking Python environment${NC}"
if command -v python3 &> /dev/null; then
    echo -e "${GREEN}✓${NC} Python3 found: $(python3 --version)"
else
    echo -e "${RED}✗${NC} Python3 not found. Please install Python 3.8+"
    exit 1
fi

# Step 2: Install Python dependencies
echo -e "\n${BLUE}Step 2: Installing Python dependencies${NC}"
pip3 install -r requirements.txt
pip3 install uvicorn[standard]  # For mock Supabase
echo -e "${GREEN}✓${NC} Python dependencies installed"

# Step 3: Create SSH directory and generate test key
echo -e "\n${BLUE}Step 3: Setting up SSH keys${NC}"
mkdir -p ssh

if [ ! -f ssh/dokku_key ]; then
    echo -e "${YELLOW}!${NC} No SSH key found. Creating a test key..."
    ssh-keygen -t ed25519 -f ssh/dokku_key -N "" -C "test@mindroom"
    echo -e "${GREEN}✓${NC} Test SSH key created at ssh/dokku_key"
    echo -e "${YELLOW}!${NC} Remember to add ssh/dokku_key.pub to your Dokku server"
else
    echo -e "${GREEN}✓${NC} SSH key already exists"
fi

# Step 4: Create test environment file
echo -e "\n${BLUE}Step 4: Creating test environment${NC}"
if [ ! -f .env ]; then
    cat > .env << 'EOF'
# Local Testing Configuration
DOKKU_HOST=localhost  # Change to your Dokku server IP
DOKKU_USER=dokku
DOKKU_SSH_KEY_PATH=/app/ssh/dokku_key
DOKKU_PORT=22

# Use nip.io for testing (no DNS needed)
BASE_DOMAIN=localhost.nip.io  # Change to <SERVER_IP>.nip.io

# Docker Images (using official images for testing)
MINDROOM_BACKEND_IMAGE=nginx:alpine  # Replace with actual image
MINDROOM_FRONTEND_IMAGE=nginx:alpine  # Replace with actual image

# Mock Supabase for testing
SUPABASE_URL=http://host.docker.internal:8003
SUPABASE_SERVICE_KEY=mock_key_for_testing

# Resource Defaults
DEFAULT_MEMORY_LIMIT=256m
DEFAULT_CPU_LIMIT=0.25

# Storage
INSTANCE_DATA_BASE=/tmp/dokku-storage

# Logging
LOG_LEVEL=DEBUG
EOF
    echo -e "${GREEN}✓${NC} Created .env file"
    echo -e "${YELLOW}!${NC} Please update DOKKU_HOST in .env with your server IP"
else
    echo -e "${GREEN}✓${NC} .env file already exists"
fi

# Step 5: Create docker-compose override for local testing
echo -e "\n${BLUE}Step 5: Creating docker-compose override${NC}"
cat > docker-compose.override.yml << 'EOF'
version: '3.8'

services:
  dokku-provisioner:
    environment:
      - PYTHONDONTWRITEBYTECODE=1
      - PYTHONUNBUFFERED=1
    extra_hosts:
      - "host.docker.internal:host-gateway"  # For accessing mock Supabase
    volumes:
      - /tmp/dokku-storage:/tmp/dokku-storage  # Local storage for testing

  mock-supabase:
    build:
      context: .
      dockerfile: Dockerfile
    container_name: mock-supabase
    command: python mock_supabase.py
    ports:
      - "8003:8003"
    networks:
      - mindroom-network
    volumes:
      - ./mock_supabase.py:/app/mock_supabase.py
EOF
echo -e "${GREEN}✓${NC} Created docker-compose.override.yml"

# Step 6: Create test data directory
echo -e "\n${BLUE}Step 6: Creating test directories${NC}"
mkdir -p /tmp/dokku-storage logs data
chmod 755 /tmp/dokku-storage
echo -e "${GREEN}✓${NC} Test directories created"

# Step 7: Display next steps
echo -e "\n${BLUE}=============================================${NC}"
echo -e "${GREEN}Setup Complete!${NC}"
echo -e "${BLUE}=============================================${NC}"
echo -e "\nNext steps:"
echo -e "1. ${YELLOW}Update .env${NC} with your Dokku server details:"
echo -e "   - Set DOKKU_HOST to your server IP"
echo -e "   - Set BASE_DOMAIN to <SERVER_IP>.nip.io"
echo -e ""
echo -e "2. ${YELLOW}Add SSH key to Dokku server${NC}:"
echo -e "   cat ssh/dokku_key.pub | ssh root@<SERVER_IP> \"sudo sshcommand acl-add dokku provisioner\""
echo -e ""
echo -e "3. ${YELLOW}Start the services${NC}:"
echo -e "   # Start mock Supabase"
echo -e "   python3 mock_supabase.py &"
echo -e ""
echo -e "   # Start provisioner"
echo -e "   docker-compose up --build"
echo -e ""
echo -e "4. ${YELLOW}Run tests${NC}:"
echo -e "   python3 test_provisioner.py starter"
echo -e "   python3 test_provisioner.py professional --no-cleanup"
echo -e ""
echo -e "${BLUE}Testing without a real Dokku server:${NC}"
echo -e "You can test the API without Dokku by using the mock setup."
echo -e "The provisioner will fail to connect but you can test the API flow."
