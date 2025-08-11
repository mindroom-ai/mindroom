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
    log "Creating systemd service files..."

    # Create mindroom-backend.service
    cat > /etc/systemd/system/mindroom-backend.service << EOF
[Unit]
Description=MindRoom Multi-Agent System Backend
After=network.target
Wants=network.target
StartLimitIntervalSec=0

[Service]
Type=simple
Restart=always
RestartSec=5
User=$PROJECT_USER
Group=$PROJECT_GROUP
WorkingDirectory=$PROJECT_DIR
Environment=PATH=/usr/local/bin:/usr/bin:/bin

# Ensure we're on the latest main branch
ExecStartPre=/bin/sh -c 'cd $PROJECT_DIR && git fetch origin && git reset --hard origin/main && uv sync --all-extras'

# Start the mindroom backend
ExecStart=$PROJECT_DIR/.venv/bin/python -m mindroom.cli run --log-level INFO --storage-path $PROJECT_DIR/tmp

# Logging
StandardOutput=append:/var/log/mindroom-backend.log
StandardError=append:/var/log/mindroom-backend.log

# Security
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=$PROJECT_DIR /var/log /tmp

[Install]
WantedBy=multi-user.target
EOF

    # Create mindroom-widget.service
    cat > /etc/systemd/system/mindroom-widget.service << EOF
[Unit]
Description=MindRoom Configuration Widget (Frontend + Backend)
After=network.target
Wants=network.target
StartLimitIntervalSec=0

[Service]
Type=simple
Restart=always
RestartSec=5
User=$PROJECT_USER
Group=$PROJECT_GROUP
WorkingDirectory=$PROJECT_DIR/widget
Environment=PATH=/usr/local/bin:/usr/bin:/bin
Environment=BACKEND_PORT=8001
Environment=FRONTEND_PORT=3003

# Ensure we're on the latest main branch and deps are up to date
ExecStartPre=/bin/sh -c 'cd $PROJECT_DIR && git fetch origin && git reset --hard origin/main'
ExecStartPre=/bin/sh -c 'cd $PROJECT_DIR/widget/backend && uv sync'
ExecStartPre=/bin/sh -c 'cd $PROJECT_DIR/widget/frontend && pnpm install'

# Use the existing run.sh script
ExecStart=$PROJECT_DIR/widget/run.sh

# Logging
StandardOutput=append:/var/log/mindroom-widget.log
StandardError=append:/var/log/mindroom-widget.log

# Security
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=$PROJECT_DIR /var/log /tmp

# Kill process group on stop (to handle background processes)
KillMode=process

[Install]
WantedBy=multi-user.target
EOF

    # Create mindroom-autoupdate.service
    cat > /etc/systemd/system/mindroom-autoupdate.service << EOF
[Unit]
Description=MindRoom Auto-Update Service
After=network.target
Wants=network.target

[Service]
Type=oneshot
User=$PROJECT_USER
Group=$PROJECT_GROUP
WorkingDirectory=$PROJECT_DIR
Environment=PATH=/usr/local/bin:/usr/bin:/bin

# Script to check for updates and restart services if needed
ExecStart=$PROJECT_DIR/deploy/autoupdate.sh

# Logging
StandardOutput=append:/var/log/mindroom-autoupdate.log
StandardError=append:/var/log/mindroom-autoupdate.log
EOF

    # Copy timer file as-is
    cp "$SCRIPT_DIR/mindroom-autoupdate.timer" /etc/systemd/system/

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
    touch /var/log/mindroom-autoupdate.log

    # Set ownership to project user
    chown "$PROJECT_USER:$PROJECT_GROUP" /var/log/mindroom-backend.log
    chown "$PROJECT_USER:$PROJECT_GROUP" /var/log/mindroom-widget.log
    chown "$PROJECT_USER:$PROJECT_GROUP" /var/log/mindroom-autoupdate.log

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
    log "MindRoom services have been set up for user: $PROJECT_USER"
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
    log "Project directory: $PROJECT_DIR"
    echo
    log "The auto-update timer will check for new commits every 5 minutes and automatically"
    log "restart services if there are updates to the main branch."
    echo
    log_warning "Note: Services are not started automatically. Start them manually as needed:"
    echo "  sudo systemctl start mindroom-backend"
    echo "  sudo systemctl start mindroom-widget"
    echo
    log_warning "To disable auto-updates during development:"
    echo "  touch $PROJECT_DIR/.no-autoupdate"
    echo "  rm $PROJECT_DIR/.no-autoupdate  # Re-enable"
}

main() {
    log "Setting up MindRoom systemd services for Linux..."

    check_root
    get_project_info

    # Store script directory for later use
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"

    create_service_files
    install_systemd_services
    setup_log_files
    enable_services
    show_usage

    log_success "Setup complete!"
}

main "$@"
