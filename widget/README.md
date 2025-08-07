# MindRoom Configuration Widget

## Overview

This widget provides a visual interface for managing MindRoom agent configurations. It features two-way synchronization between a web UI and the `config.yaml` file, making it accessible to both technical and non-technical users.

## Quick Start

### Using Nix (Recommended for Screenshots)

```bash
# From project root - starts everything automatically
nix-shell widget/shell.nix --run "python take_screenshot.py"
```

### Manual Start

```bash
# Terminal 1: Start backend
cd widget/backend
uv sync                    # or: python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
uv run uvicorn src.main:app --reload

# Terminal 2: Start frontend
cd widget/frontend
npm install
npm run dev

# Access at http://localhost:3001
```

### Using the Convenience Script

```bash
# Standard systems
./widget/run.sh

# With custom backend port
BACKEND_PORT=8080 ./widget/run.sh

# On Nix systems (ensures all dependencies available)
./widget/run-nix.sh
```

## Architecture

### Frontend (React/TypeScript)
- **Location**: `widget/frontend/`
- **Port**: 3001 (or 3000 if available)
- **Technologies**: React 18, TypeScript, Tailwind CSS, Zustand, Vite
- **Key Files**:
  - `src/App.tsx` - Main application component
  - `src/store/configStore.ts` - State management with Zustand
  - `src/components/AgentEditor/` - Agent editing interface
  - `src/components/ModelConfig/` - Model configuration UI

### Backend (FastAPI/Python)
- **Location**: `widget/backend/`
- **Port**: 8001 (configurable via `BACKEND_PORT` environment variable)
- **Technologies**: FastAPI, PyYAML, Watchdog, Pydantic
- **Key Files**:
  - `src/main.py` - API endpoints and file watching
  - `pyproject.toml` - Python dependencies

## Features

### 1. Agent Management
- View all agents in a visual list
- Edit agent properties (name, role, tools, instructions, rooms)
- Add new agents with sensible defaults
- Delete agents with one click
- Real-time form validation

### 2. Model Configuration
- Configure AI models (OpenAI, Anthropic, Ollama, etc.)
- Manage API keys (placeholder encryption)
- Test model connections
- Add custom model configurations

### 3. Two-Way Synchronization
- **UI → File**: Changes save immediately to `config.yaml`
- **File → UI**: External edits detected and UI updates automatically
- No data loss or conflicts
- Preserves YAML formatting and comments

### 4. Developer Features
- Hot reload for both frontend and backend
- TypeScript for type safety
- Comprehensive error handling
- RESTful API with OpenAPI docs at http://localhost:8001/docs

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/config/load` | Load current configuration |
| PUT | `/api/config/save` | Save entire configuration |
| GET | `/api/config/agents` | List all agents |
| POST | `/api/config/agents` | Create new agent |
| PUT | `/api/config/agents/{id}` | Update specific agent |
| DELETE | `/api/config/agents/{id}` | Delete agent |
| GET | `/api/tools` | Get available tools list |
| GET | `/api/rooms` | Get all rooms from agents |
| POST | `/api/test/model` | Test model connection |

## Taking Screenshots

### Prerequisites

The widget includes Puppeteer-based screenshot functionality. You need Chrome/Chromium installed.

### With Nix (Easiest)

```bash
# From project root
nix-shell widget/shell.nix --run "python take_screenshot.py"
```

This automatically:
- Provides Chromium and all dependencies
- Starts both servers
- Takes screenshots (full page, agent selected, models tab)
- Saves to `widget/frontend/screenshots/`
- Stops servers when done

### Without Nix

Install Chrome/Chromium, then:
```bash
python take_screenshot.py
```

### Alternative (No Chrome)

If Chrome isn't available:
```bash
cd widget
python capture_state.py
```

This captures the widget state as JSON instead of screenshots.

## File Structure

```
widget/
├── frontend/                 # React application
│   ├── src/
│   │   ├── components/      # UI components
│   │   ├── store/          # State management
│   │   ├── services/       # API client
│   │   └── types/          # TypeScript types
│   ├── public/             # Static assets
│   └── package.json        # Node dependencies
├── backend/                # FastAPI server
│   ├── src/
│   │   └── main.py        # API and file watching
│   └── pyproject.toml     # Python config
├── run.sh                 # Start both servers
├── take_screenshot.py     # Screenshot automation
├── capture_state.py       # Alternative state capture
├── shell.nix             # Nix environment
└── README.md             # This file
```

## Development

### Environment Variables

- `BACKEND_PORT`: Set the backend server port (default: 8001)
  ```bash
  # Examples:
  BACKEND_PORT=8080 ./widget/run.sh
  export BACKEND_PORT=9000 && ./widget/run.sh
  ```

### Adding New Features

1. **New Agent Properties**: Update types in `frontend/src/types/config.ts`
2. **New API Endpoints**: Add to `backend/src/main.py`
3. **New UI Components**: Add to `frontend/src/components/`
4. **State Changes**: Update `frontend/src/store/configStore.ts`

### Testing

Currently manual testing. Run the widget and verify:
- Agents can be created, edited, deleted
- Changes sync to `config.yaml`
- External file edits appear in UI
- No data loss during operations

### Common Issues

1. **Port conflicts**: Backend uses 8001, frontend uses 3000
2. **File permissions**: Ensure write access to `config.yaml`
3. **Missing dependencies**: Run `npm install` and `uv sync`

## Matrix/Element Integration

The widget can be embedded directly into your Matrix client as a room widget. This allows you to configure MindRoom without leaving your chat interface.

**⚠️ Important:** Element Web (app.element.io) does NOT support custom widgets. You need:
- **Element Desktop** (download from https://element.io/download), OR
- **Self-hosted Element Web** with widgets enabled, OR
- **Use the widget directly** at http://localhost:3001 in a browser tab

**Quick Setup (Element Desktop only):**
1. Start the widget: `./widget/run.sh`
2. In Element Desktop, go to room settings → Widgets → Add Custom Widget
3. Enter URL: `http://localhost:3001/matrix-widget.html?url=http://localhost:3001`
4. Click "Add Widget"

See `WIDGET_INTEGRATION.md` for detailed setup instructions and production deployment.

## Future Enhancements

- [ ] Real API key encryption (currently placeholder)
- [ ] WebSocket for real-time multi-client sync
- [ ] Import/export configuration backups
- [ ] Undo/redo functionality
- [ ] Batch operations on agents
- [x] Matrix widget manifest for embedding

## Additional Documentation

- `WIDGET_INTEGRATION.md` - Complete integration and deployment guide
- `TESTING.md` - Comprehensive testing documentation
- `SCREENSHOT_INSTRUCTIONS.md` - Screenshot setup for documentation

## Support

The widget is designed to be self-explanatory, but key points:
- All changes auto-save
- The sync status indicator shows connection state
- Red means error, green means synced
- You can edit `config.yaml` directly - changes appear in UI
- The widget and manual editing can be used together
