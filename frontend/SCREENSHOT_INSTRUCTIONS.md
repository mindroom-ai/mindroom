# Screenshot Instructions

This document provides clear instructions for taking screenshots of the MindRoom Configuration Widget.

## Prerequisites

This project runs on a **Nix system**. All commands should be run using `nix-shell`.

## Quick Start (For AI Assistants)

### 1. Start the servers

```bash
cd widget
nix-shell shell.nix --run "./run.sh"
```

Note the port number shown in the output (e.g., "Frontend: http://localhost:3003")

### 2. Take screenshots (in a new terminal)

```bash
cd widget
nix-shell shell.nix --run "python take_screenshot.py 3003"
```

Replace `3003` with the actual port number if you're using a custom port.

### 3. Find screenshots

Screenshots are saved in: `frontend/screenshots/`

## Detailed Instructions

### Starting the Servers

The `run.sh` script starts both backend and frontend:

- **Backend**: Port 8001 (or set with `BACKEND_PORT` environment variable)
- **Frontend**: Port 3003 (or set with `FRONTEND_PORT` environment variable)

The script will show which ports are being used:

```
Widget is running!
Frontend: http://localhost:3003
Backend: http://localhost:8001
```

### Taking Screenshots

The `take_screenshot.py` script requires the port as an argument:

```bash
python take_screenshot.py <port>
```

Example:

```bash
python take_screenshot.py 3003
```

The script captures:

- Full page view
- Agents tab (with selected agent)
- Models tab

### Output Files

Screenshots are saved with timestamps:

```
frontend/screenshots/
├── mindroom-config-fullpage-YYYY-MM-DDTHH-mm-ss-sssZ.png
├── mindroom-config-agents-YYYY-MM-DDTHH-mm-ss-sssZ.png
└── mindroom-config-models-YYYY-MM-DDTHH-mm-ss-sssZ.png
```

## Manual Method (If Script Fails)

```bash
cd widget
DEMO_URL="http://localhost:3003" nix-shell shell.nix --run "cd frontend && bun run screenshot"
```

## Troubleshooting

### Port Issues

- Always check the output of `run.sh` for the actual port

### Config File

The backend needs `config.yaml` at the project root:

```
/home/basnijholt/Work/mindroom-2/config.yaml
```

### Browser/Puppeteer Issues

The Nix shell provides all necessary dependencies. If screenshots fail:

1. Make sure you're using `nix-shell`
2. Check that the servers are running
3. Verify the port number is correct

## System Dependencies (Already in Nix Shell)

The `shell.nix` file includes:

- Chromium for Puppeteer
- Node.js and bun
- Python and uv
- All necessary system libraries

## Important Notes

- **Always use nix-shell** - Required for Chrome/Puppeteer
- **Port is required** - Must specify the frontend port
- **Servers must be running** - Script doesn't start servers
- **Config file required** - Backend needs `config.yaml` to work
