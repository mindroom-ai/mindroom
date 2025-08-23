#!/bin/bash
# Script to pull latest images from Gitea registry and update deployments

set -e

REGISTRY="git.nijho.lt"
OWNER="basnijholt"
DEPLOY_DIR="$HOME/mindroom/deploy"

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${BLUE}🔄 Updating Mindroom deployments from Gitea registry...${NC}"

# Check if token is provided or in environment
if [ -z "$GITEA_TOKEN" ]; then
    echo -e "${RED}❌ GITEA_TOKEN not set. Please export GITEA_TOKEN or pass it as argument${NC}"
    echo "Usage: GITEA_TOKEN=your_token $0"
    exit 1
fi

# Login to registry
echo -e "${BLUE}🔐 Logging into Gitea registry...${NC}"
echo "$GITEA_TOKEN" | docker login "$REGISTRY" -u "$OWNER" --password-stdin

# Pull latest images
echo -e "${BLUE}📥 Pulling latest images...${NC}"
docker pull "$REGISTRY/$OWNER/mindroom-backend:latest"
docker pull "$REGISTRY/$OWNER/mindroom-frontend:latest"

# Tag for local use
echo -e "${BLUE}🏷️  Tagging images for local deployment...${NC}"
docker tag "$REGISTRY/$OWNER/mindroom-backend:latest" deploy-mindroom-backend:latest
docker tag "$REGISTRY/$OWNER/mindroom-frontend:latest" deploy-mindroom-frontend:latest

# Also tag without deploy- prefix for compatibility
docker tag "$REGISTRY/$OWNER/mindroom-backend:latest" mindroom-backend:latest
docker tag "$REGISTRY/$OWNER/mindroom-frontend:latest" mindroom-frontend:latest

# Update deployments
echo -e "${BLUE}🚀 Updating deployments...${NC}"
cd "$DEPLOY_DIR"

# Update try instance
echo -e "${GREEN}  Updating try.mindroom.chat...${NC}"
docker compose --env-file .env.try -f docker-compose.yml stop
docker compose --env-file .env.try -f docker-compose.yml rm -f
docker compose --env-file .env.try -f docker-compose.yml -f docker-compose.tuwunel.yml up -d

# Update alt instance
echo -e "${GREEN}  Updating alt.mindroom.chat...${NC}"
docker compose --env-file .env.alt -f docker-compose.yml stop
docker compose --env-file .env.alt -f docker-compose.yml rm -f
docker compose --env-file .env.alt -f docker-compose.yml -f docker-compose.tuwunel.yml up -d

# Show status
echo -e "${BLUE}📊 Current status:${NC}"
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Image}}" | grep -E "NAME|try|alt"

echo -e "${GREEN}✅ Deployment update complete!${NC}"
echo -e "${BLUE}Check your instances:${NC}"
echo "  - https://try.mindroom.chat"
echo "  - https://alt.mindroom.chat"