#!/usr/bin/env bash

set -euo pipefail

# Colors for logging
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log() {
    echo -e "${BLUE}[$(date '+%Y-%m-%d %H:%M:%S')]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[$(date '+%Y-%m-%d %H:%M:%S')]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[$(date '+%Y-%m-%d %H:%M:%S')]${NC} $1"
}

log_error() {
    echo -e "${RED}[$(date '+%Y-%m-%d %H:%M:%S')]${NC} $1"
}

PROJECT_DIR="/home/basnijholt/Work/mindroom-2"

# Change to project directory
cd "$PROJECT_DIR"

# Check if we're in a git repository
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    log_error "Not in a git repository!"
    exit 1
fi

# Get current branch name
CURRENT_BRANCH=$(git branch --show-current)

# Default to main branch if current branch doesn't exist on remote
TARGET_BRANCH="$CURRENT_BRANCH"

# Fetch the latest changes from origin for current branch
log "Fetching latest changes from origin for branch $CURRENT_BRANCH..."
if ! git fetch origin "$CURRENT_BRANCH" 2>/dev/null; then
    log_warning "Branch $CURRENT_BRANCH not found on remote, falling back to main"
    TARGET_BRANCH="main"
    git fetch origin main
fi

# Get current and remote commit hashes
CURRENT_COMMIT=$(git rev-parse HEAD)
REMOTE_COMMIT=$(git rev-parse "origin/$TARGET_BRANCH")

if [ "$CURRENT_COMMIT" = "$REMOTE_COMMIT" ]; then
    log "Already up to date (commit: ${CURRENT_COMMIT:0:8})"
    exit 0
fi

log "New changes detected!"
log "Current commit: ${CURRENT_COMMIT:0:8}"
log "Remote commit:  ${REMOTE_COMMIT:0:8}"

# Show what changes are incoming
log "Changes incoming:"
git log --oneline "$CURRENT_COMMIT".."$REMOTE_COMMIT"

# Reset to the latest branch
log "Updating to latest $TARGET_BRANCH branch..."
git reset --hard "origin/$TARGET_BRANCH"

# Update dependencies
log "Updating root dependencies..."
if ! uv sync --all-extras; then
    log_error "Failed to update root dependencies"
    exit 1
fi

# Update widget backend dependencies
log "Updating widget backend dependencies..."
cd widget/backend
if ! uv sync; then
    log_error "Failed to update widget backend dependencies"
    exit 1
fi
cd ../..

# Update widget frontend dependencies
log "Updating widget frontend dependencies..."
cd widget/frontend
if ! pnpm install; then
    log_error "Failed to update widget frontend dependencies"
    exit 1
fi
cd ../..

# Check if services are running and restart them
SERVICES_TO_RESTART=()

if systemctl is-active --quiet mindroom-backend.service; then
    log "MindRoom backend is running, will restart"
    SERVICES_TO_RESTART+=("mindroom-backend.service")
fi

if systemctl is-active --quiet mindroom-widget.service; then
    log "MindRoom widget is running, will restart"
    SERVICES_TO_RESTART+=("mindroom-widget.service")
fi

if [ ${#SERVICES_TO_RESTART[@]} -gt 0 ]; then
    log "Restarting services: ${SERVICES_TO_RESTART[*]}"
    for service in "${SERVICES_TO_RESTART[@]}"; do
        log "Restarting $service..."
        if systemctl restart "$service"; then
            log_success "Successfully restarted $service"
        else
            log_error "Failed to restart $service"
        fi
    done
else
    log_warning "No services were running, nothing to restart"
fi

log_success "Auto-update complete! Updated to commit: ${REMOTE_COMMIT:0:8}"
