# MindRoom Linux Deployment

Simple systemd services to run MindRoom frontend and backend on any Linux distribution.

## Features

- **Auto-detects** user and project paths
- **Simple services** - just run MindRoom, no complexity
- **Reliable restarts** - services restart automatically on failure
- **Comprehensive logging** to `/var/log/mindroom-*.log`
- **Manual updates** - you control when to update

## Quick Start

```bash
# Clone your repo and setup
git clone [your-repo]
cd mindroom-2
sudo ./deploy/setup.sh

# Start services
sudo systemctl start mindroom-backend    # Main mindroom system
sudo systemctl start mindroom-widget     # Widget frontend/backend
```

## Available Commands

```bash
# Service management
sudo systemctl start mindroom-backend
sudo systemctl start mindroom-widget
sudo systemctl stop mindroom-backend
sudo systemctl stop mindroom-widget
sudo systemctl status mindroom-backend
sudo systemctl status mindroom-widget

# View logs
tail -f /var/log/mindroom-backend.log
tail -f /var/log/mindroom-widget.log
```

## Manual Updates

```bash
cd /path/to/mindroom
git pull origin main
uv sync --all-extras  # Installs main package with API
cd widget/frontend && pnpm install && cd ../..

# Restart services if running
sudo systemctl restart mindroom-backend
sudo systemctl restart mindroom-widget
```

## File Structure

```
deploy/
├── README.md                    # This file
├── setup.sh                    # One-command setup script
├── mindroom-backend.service     # Backend systemd service template
└── mindroom-widget.service      # Widget systemd service template
```

## How It Works

1. **Setup script** auto-detects your user and project paths
2. **Service templates** get filled in with your specific paths
3. **Services start** your applications and restart them on failure
4. **You handle updates** manually when you want them

Simple, reliable, no surprises! 🚀
