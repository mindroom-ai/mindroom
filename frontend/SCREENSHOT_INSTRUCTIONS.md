# Screenshot Instructions

This document provides instructions for taking screenshots of the MindRoom dashboard.

## Prerequisites

This project runs on a **Nix system**. All commands should be run using `nix-shell`.

## Quick Start

### 1. Start MindRoom

```bash
uv sync --all-extras
uv run mindroom run
```

The bundled dashboard is available at `http://localhost:8765`.

### 2. Take screenshots

```bash
cd frontend
bun run screenshot
```

To target a different URL, set `DEMO_URL`.
If you prefer the Python wrapper, run `python take_screenshot.py` with an optional port or URL argument.

### 3. Find screenshots

Screenshots are saved in: `frontend/screenshots/`

## Detailed Instructions

### Starting the Dashboard

The default workflow is to run the backend, which now serves the dashboard directly on port `8765`.

If you are iterating on the React app itself, you can still run the frontend dev server separately with `./run-frontend.sh`, then point the screenshot script at that URL with `DEMO_URL=http://localhost:3003`.

### Taking Screenshots

The screenshot script reads the target URL from `DEMO_URL` and otherwise defaults to `http://localhost:8765`.

Example:

```bash
cd frontend
DEMO_URL="http://localhost:8765" bun run screenshot
```

Or:

```bash
python frontend/take_screenshot.py
python frontend/take_screenshot.py 3003
python frontend/take_screenshot.py http://localhost:3003
```

The script captures:

- Full page view
- Agents tab (with selected agent)
- Models tab

### Output Files

Screenshots are saved with timestamps:

```
frontend/screenshots/
├── mindroom-dashboard-fullpage-YYYY-MM-DDTHH-mm-ss-sssZ.png
├── mindroom-dashboard-agents-YYYY-MM-DDTHH-mm-ss-sssZ.png
└── mindroom-dashboard-models-YYYY-MM-DDTHH-mm-ss-sssZ.png
```

## Frontend Dev Server Mode

```bash
./run-frontend.sh
cd frontend
DEMO_URL="http://localhost:3003" bun run screenshot
```

## Troubleshooting

### Port Issues

- The default dashboard URL is `http://localhost:8765`
- If you use `run-frontend.sh`, point `DEMO_URL` at the frontend dev server instead

### Config File

The backend needs a valid `config.yaml` for the dashboard to load correctly.

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
- **By default, no port is required** - The screenshot script targets `http://localhost:8765`
- **Set `DEMO_URL` when needed** - Use this for the frontend dev server or a remote instance
- **Servers must be running** - Script doesn't start servers
- **Config file required** - Backend needs `config.yaml` to work
