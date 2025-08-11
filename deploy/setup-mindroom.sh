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

install_systemd_services() {
    local script_dir
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"

    log "Installing systemd services..."

    # Copy service files to systemd directory
    cp "$script_dir/mindroom-backend.service" /etc/systemd/system/
    cp "$script_dir/mindroom-widget.service" /etc/systemd/system/
    cp "$script_dir/mindroom-autoupdate.service" /etc/systemd/system/
    cp "$script_dir/mindroom-autoupdate.timer" /etc/systemd/system/

    # Reload systemd daemon
    systemctl daemon-reload

    log_success "Systemd services installed"
}

setup_log_files() {
    log "Setting up log files..."

    # Create log files with proper permissions
    touch /var/log/mindroom-backend.log
    touch /var/log/mindroom-widget.log
    touch /var/log/mindroom-autoupdate.log

    # Set ownership to basnijholt
    chown basnijholt:users /var/log/mindroom-backend.log
    chown basnijholt:users /var/log/mindroom-widget.log
    chown basnijholt:users /var/log/mindroom-autoupdate.log

    # Set permissions
    chmod 644 /var/log/mindroom-backend.log
    chmod 644 /var/log/mindroom-widget.log
    chmod 644 /var/log/mindroom-autoupdate.log

    log_success "Log files configured"
}

enable_services() {
    log "Enabling systemd services..."

    # Enable and start auto-update timer
    systemctl enable mindroom-autoupdate.timer
    systemctl start mindroom-autoupdate.timer

    log_success "Auto-update timer enabled and started"
    log "Services available:"
    log "  - mindroom-backend.service   (Manual start: sudo systemctl start mindroom-backend)"
    log "  - mindroom-widget.service    (Manual start: sudo systemctl start mindroom-widget)"
    log "  - mindroom-autoupdate.timer  (Auto-enabled, checks every 5 minutes)"
}

show_usage() {
    log "MindRoom services have been set up!"
    echo
    log "Available commands:"
    echo "  sudo systemctl start mindroom-backend     # Start the main MindRoom backend"
    echo "  sudo systemctl start mindroom-widget      # Start the configuration widget"
    echo "  sudo systemctl stop mindroom-backend      # Stop the main MindRoom backend"
    echo "  sudo systemctl stop mindroom-widget       # Stop the configuration widget"
    echo "  sudo systemctl status mindroom-backend    # Check backend status"
    echo "  sudo systemctl status mindroom-widget     # Check widget status"
    echo "  sudo systemctl status mindroom-autoupdate.timer  # Check auto-update timer"
    echo
    log "Log locations:"
    echo "  /var/log/mindroom-backend.log      # Backend logs"
    echo "  /var/log/mindroom-widget.log       # Widget logs"
    echo "  /var/log/mindroom-autoupdate.log   # Auto-update logs"
    echo
    log "The auto-update timer will check for new commits every 5 minutes and automatically"
    log "restart services if there are updates to the main branch."
    echo
    log_warning "Note: Services are not started automatically. Start them manually as needed:"
    echo "  sudo systemctl start mindroom-backend"
    echo "  sudo systemctl start mindroom-widget"
}

main() {
    log "Setting up MindRoom systemd services for NixOS..."

    check_root
    install_systemd_services
    setup_log_files
    enable_services
    show_usage

    log_success "Setup complete!"
}

main "$@"
