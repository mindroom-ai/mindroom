#!/usr/bin/env bash
# Script to check Gitea Actions build status and registry images

set -e

REGISTRY="git.nijho.lt"
OWNER="basnijholt"
REPO="mindroom"

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${BLUE}🔍 Checking Gitea Actions build status...${NC}"
echo ""

# Check if token is provided
if [ -z "$GITEA_TOKEN" ]; then
    echo -e "${YELLOW}⚠️  GITEA_TOKEN not set. Some features may be limited.${NC}"
    echo "   To set: export GITEA_TOKEN=your_token"
    echo ""
fi

# Check latest workflow runs (requires curl and jq)
if command -v curl &> /dev/null && command -v jq &> /dev/null && [ -n "$GITEA_TOKEN" ]; then
    echo -e "${BLUE}📊 Recent workflow runs:${NC}"
    
    # Get workflow runs from Gitea API
    RUNS=$(curl -s -H "Authorization: token $GITEA_TOKEN" \
        "https://$REGISTRY/api/v1/repos/$OWNER/$REPO/actions/runs?limit=5" 2>/dev/null || echo "{}")
    
    if [ "$RUNS" != "{}" ] && [ "$(echo "$RUNS" | jq -r '.workflow_runs | length' 2>/dev/null)" != "0" ]; then
        echo "$RUNS" | jq -r '.workflow_runs[] | 
            "  [\(.status)] \(.name) - \(.head_branch) - \(.created_at | split("T")[0])"' 2>/dev/null || \
            echo "  Unable to parse workflow runs"
    else
        echo "  No workflow runs found or API access denied"
    fi
    echo ""
fi

# Check registry images
echo -e "${BLUE}🐳 Checking registry for existing images...${NC}"

# Function to check if image exists
check_image() {
    local image=$1
    local tag=${2:-latest}
    
    # Try to get manifest (will fail if image doesn't exist or no access)
    if docker manifest inspect "$REGISTRY/$OWNER/$image:$tag" &>/dev/null; then
        echo -e "${GREEN}  ✅ $image:$tag exists${NC}"
        
        # Get image details if possible
        docker manifest inspect "$REGISTRY/$OWNER/$image:$tag" 2>/dev/null | \
            jq -r '.manifests[] | "      - \(.platform.os)/\(.platform.architecture)"' 2>/dev/null || true
    else
        echo -e "${YELLOW}  ⏳ $image:$tag not found (may be building or private)${NC}"
    fi
}

# Check for images
check_image "mindroom-backend"
check_image "mindroom-frontend"
echo ""

# Show how to monitor builds
echo -e "${BLUE}📝 Useful commands:${NC}"
echo ""
echo "1. Watch Gitea Actions in browser:"
echo "   https://$REGISTRY/$OWNER/$REPO/actions"
echo ""
echo "2. Pull images when ready:"
echo "   docker pull $REGISTRY/$OWNER/mindroom-backend:latest"
echo "   docker pull $REGISTRY/$OWNER/mindroom-frontend:latest"
echo ""
echo "3. Update deployments:"
echo "   GITEA_TOKEN=your_token ~/mindroom/scripts/update-from-registry.sh"
echo ""

# Check if runner is configured
echo -e "${BLUE}🏃 Runner status:${NC}"
if systemctl is-active --quiet gitea-runner 2>/dev/null; then
    echo -e "${GREEN}  ✅ Gitea runner service is active${NC}"
elif docker ps | grep -q act_runner; then
    echo -e "${GREEN}  ✅ Gitea runner container is running${NC}"
else
    echo -e "${YELLOW}  ⚠️  No local Gitea runner detected${NC}"
    echo "     Your x86 runner should be registered at git.nijho.lt"
fi