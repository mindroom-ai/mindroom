# Screenshot Setup for MindRoom Configuration Widget

## Overview

The widget includes screenshot functionality to capture its visual appearance without opening a browser. This is useful for documentation, sharing UI states, and visual verification.

## Installation

### 1. Frontend Dependencies

The screenshot functionality requires Puppeteer, which has already been installed:

```bash
cd widget/frontend
npm install
```

### 2. System Dependencies

Puppeteer requires Chrome/Chromium to be installed. If you encounter errors about missing libraries, you may need to install system dependencies:

**Ubuntu/Debian:**
```bash
sudo apt-get update
sudo apt-get install -y \
  libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
  libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
  libgbm1 libxkbcommon0 libpango-1.0-0 libcairo2 \
  libasound2 libglib2.0-0
```

**macOS:**
Chrome dependencies are typically included with the system.

**Alternative: Use system Chrome**
You can modify the Puppeteer launch options in `widget/frontend/scripts/screenshot.cjs` to use your system Chrome:
```javascript
const browser = await puppeteer.launch({
  headless: true,
  executablePath: '/usr/bin/google-chrome', // or your Chrome path
  args: ["--no-sandbox", "--disable-setuid-sandbox"],
});
```

## Usage

### From Project Root

The easiest way to take screenshots:

```bash
python take_screenshot.py
```

This will:
1. Start the backend server (port 8000)
2. Start the frontend server (port 3001)
3. Take multiple screenshots:
   - Full page view
   - Selected agent view
   - Models tab view
4. Save screenshots to `widget/frontend/screenshots/`
5. Stop both servers

### Manual Screenshot (Frontend Only)

If you already have servers running:

```bash
cd widget/frontend
npm run screenshot
```

## Screenshot Types

The script captures three different views:

1. **Full Page** - The complete widget interface with agent list
2. **Selected Agent** - Widget with an agent selected showing details
3. **Models Tab** - The models and API keys configuration tab

## Output

Screenshots are saved with timestamps to:
```
widget/frontend/screenshots/
├── mindroom-config-fullpage-2024-01-01T12-00-00-000Z.png
├── mindroom-config-selected-2024-01-01T12-00-00-000Z.png
└── mindroom-config-models-2024-01-01T12-00-00-000Z.png
```

## Troubleshooting

### Browser Launch Errors

If you see "Failed to launch the browser process", install the system dependencies listed above.

### Port Conflicts

The script expects:
- Backend on port 8000
- Frontend on port 3001

If these ports are in use, stop existing servers before running the screenshot script.

### Virtual Environment

The backend requires the virtual environment to be set up:
```bash
cd widget/backend
uv sync
```

## Customization

You can modify `widget/frontend/scripts/screenshot.cjs` to:
- Change viewport size
- Add more interactions
- Capture different UI states
- Adjust wait times

The Python wrapper (`take_screenshot.py`) can be modified to:
- Change server startup times
- Add command-line options
- Customize output paths
