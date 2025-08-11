# MindRoom NixOS Auto-Deploy Setup

This directory contains systemd services and scripts to automatically run MindRoom frontend and backend on NixOS with auto-updates from the main git branch.

## Features

- **Auto-updates**: Automatically pulls latest changes from main branch every 5 minutes
- **Service management**: Systemd services for reliable backend and frontend operation
- **Automatic restarts**: Services restart automatically when new code is pulled
- **Logging**: Comprehensive logging to `/var/log/mindroom-*.log`
- **Security**: Services run as non-root user with restricted filesystem access

## Quick Start

1. **Install services** (run as root):
   ```bash
   sudo ./nixos/setup-mindroom.sh
   ```

2. **Start services**:
   ```bash
   # Start the main MindRoom backend (mindroom run)
   sudo systemctl start mindroom-backend

   # Start the configuration widget (frontend + widget backend)
   sudo systemctl start mindroom-widget
   ```

3. **Check status**:
   ```bash
   sudo systemctl status mindroom-backend
   sudo systemctl status mindroom-widget
   sudo systemctl status mindroom-autoupdate.timer
   ```

## Services Overview

### `mindroom-backend.service`
- Runs `mindroom run` command (the main multi-agent system)
- Auto-updates dependencies and code before starting
- Logs to `/var/log/mindroom-backend.log`
- Accessible via Matrix rooms

### `mindroom-widget.service`
- Runs the configuration widget (`widget/run.sh`)
- Starts both widget backend (port 8001) and frontend (port 3003)
- Auto-updates both backend and frontend dependencies
- Logs to `/var/log/mindroom-widget.log`
- Frontend accessible at http://localhost:3003

### `mindroom-autoupdate.timer`
- Runs every 5 minutes to check for git updates
- Automatically restarts services when new code is available
- Logs to `/var/log/mindroom-autoupdate.log`
- Shows commit changes and update status

## Manual Commands

```bash
# Service management
sudo systemctl start mindroom-backend
sudo systemctl stop mindroom-backend
sudo systemctl restart mindroom-backend
sudo systemctl status mindroom-backend

sudo systemctl start mindroom-widget
sudo systemctl stop mindroom-widget
sudo systemctl restart mindroom-widget
sudo systemctl status mindroom-widget

# Auto-update management
sudo systemctl status mindroom-autoupdate.timer
sudo systemctl stop mindroom-autoupdate.timer
sudo systemctl start mindroom-autoupdate.timer

# View logs
tail -f /var/log/mindroom-backend.log
tail -f /var/log/mindroom-widget.log
tail -f /var/log/mindroom-autoupdate.log

# Manual update check
sudo -u basnijholt /home/basnijholt/Work/mindroom-2/nixos/autoupdate.sh
```

## How Auto-Updates Work

1. **Timer triggers**: Every 5 minutes, the `mindroom-autoupdate.timer` runs
2. **Git check**: Script fetches from `origin/main` and compares commits
3. **Update process**: If new commits exist:
   - Resets local repo to `origin/main`
   - Updates all dependencies (`uv sync`, `pnpm install`)
   - Restarts any currently running services
4. **Logging**: All changes and restart operations are logged

## File Structure

```
nixos/
├── README.md                     # This file
├── setup-mindroom.sh             # Installation script (run as root)
├── autoupdate.sh                 # Auto-update logic script
├── mindroom-backend.service      # Main mindroom backend service
├── mindroom-widget.service       # Widget frontend+backend service
├── mindroom-autoupdate.service   # Auto-update service definition
└── mindroom-autoupdate.timer     # Timer for auto-updates (every 5min)
```

## Security Features

- Services run as non-root user (`basnijholt`)
- Restricted filesystem access (`ProtectSystem=strict`, `ProtectHome=read-only`)
- No new privileges allowed (`NoNewPrivileges=true`)
- Logs stored in system log directory with proper permissions

## Troubleshooting

1. **Check service status**:
   ```bash
   sudo systemctl status mindroom-backend
   sudo systemctl status mindroom-widget
   ```

2. **View recent logs**:
   ```bash
   sudo journalctl -u mindroom-backend -n 50
   sudo journalctl -u mindroom-widget -n 50
   ```

3. **Check auto-update logs**:
   ```bash
   tail -n 100 /var/log/mindroom-autoupdate.log
   ```

4. **Test manual update**:
   ```bash
   sudo -u basnijholt /home/basnijholt/Work/mindroom-2/nixos/autoupdate.sh
   ```

5. **If services fail to start**:
   - Check that all dependencies are installed (`uv`, `pnpm`)
   - Verify that the git repository is clean and on main branch
   - Check file permissions and ownership
   - Review logs for specific error messages
