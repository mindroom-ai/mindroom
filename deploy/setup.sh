#!/usr/bin/env bash

set -euo pipefail

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

log() {
    echo -e "${BLUE}[MindRoom Setup]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[MindRoom Setup]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[MindRoom Setup]${NC} $1"
}

log_error() {
    echo -e "${RED}[MindRoom Setup]${NC} $1"
}

check_root() {
    if [ "$EUID" -ne 0 ]; then
        log_error "This script must be run as root (use sudo)"
        exit 1
    fi
}

get_project_info() {
    # Get the directory where this script is located
    local script_dir
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"

    # Get the project directory (parent of deploy/)
    PROJECT_DIR="$(dirname "$script_dir")"

    # Get the user who owns the project directory
    PROJECT_USER="$(stat -c '%U' "$PROJECT_DIR")"
    PROJECT_GROUP="$(stat -c '%G' "$PROJECT_DIR")"

    log "Detected project directory: $PROJECT_DIR"
    log "Detected project user: $PROJECT_USER"
    log "Detected project group: $PROJECT_GROUP"
}

create_service_files() {
    log "Creating systemd service files from templates..."

    # Store script directory for later use
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"

    # Create systemd service files from templates
    sed "s|PROJECT_USER|$PROJECT_USER|g; s|PROJECT_GROUP|$PROJECT_GROUP|g; s|PROJECT_DIR|$PROJECT_DIR|g" \
        "$SCRIPT_DIR/mindroom-backend.service" > /etc/systemd/system/mindroom-backend.service

    sed "s|PROJECT_USER|$PROJECT_USER|g; s|PROJECT_GROUP|$PROJECT_GROUP|g; s|PROJECT_DIR|$PROJECT_DIR|g" \
        "$SCRIPT_DIR/mindroom-widget.service" > /etc/systemd/system/mindroom-widget.service

    log_success "Systemd service files created"
}

install_systemd_services() {
    log "Installing systemd services..."

    # Reload systemd daemon
    systemctl daemon-reload

    log_success "Systemd services installed"
}

setup_log_files() {
    log "Setting up log files..."

    # Create log files with proper permissions
    touch /var/log/mindroom-backend.log
    touch /var/log/mindroom-widget.log

    # Set ownership to project user
    chown "$PROJECT_USER:$PROJECT_GROUP" /var/log/mindroom-backend.log
    chown "$PROJECT_USER:$PROJECT_GROUP" /var/log/mindroom-widget.log

    # Set permissions
    chmod 644 /var/log/mindroom-backend.log
    chmod 644 /var/log/mindroom-widget.log

    log_success "Log files configured"
}

show_usage() {
    log "MindRoom services have been set up for user: $PROJECT_USER"
    echo
    log "Available commands:"
    echo "  sudo systemctl start mindroom-backend     # Start the main MindRoom backend"
    echo "  sudo systemctl start mindroom-widget      # Start the configuration widget"
    echo "  sudo systemctl stop mindroom-backend      # Stop the main MindRoom backend"
    echo "  sudo systemctl stop mindroom-widget       # Stop the configuration widget"
    echo "  sudo systemctl status mindroom-backend    # Check backend status"
    echo "  sudo systemctl status mindroom-widget     # Check widget status"
    echo
    log "Log locations:"
    echo "  /var/log/mindroom-backend.log      # Backend logs"
    echo "  /var/log/mindroom-widget.log       # Widget logs"
    echo
    log "Project directory: $PROJECT_DIR"
    echo
    log_warning "Note: Services are not started automatically. Start them manually as needed:"
    echo "  sudo systemctl start mindroom-backend"
    echo "  sudo systemctl start mindroom-widget"
    echo
    log "To update: pull latest git changes, then restart services"
}

main() {
    log "Setting up MindRoom systemd services..."

    check_root
    get_project_info
    create_service_files
    install_systemd_services
    setup_log_files
    show_usage

    log_success "Setup complete!"
}

main "$@"
