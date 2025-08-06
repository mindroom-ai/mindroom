# MindRoom Configuration Widget

A Matrix widget that provides a visual interface for managing MindRoom agent configurations and API keys.

## Features

- **Visual Agent Management**: Add, edit, and delete agents through a user-friendly interface
- **Two-Way Sync**: Changes made in the widget sync with `config.yaml` and vice versa
- **Model Configuration**: Configure AI models and their API keys
- **Tool Selection**: Easy checkbox interface for selecting agent tools
- **Room Management**: Assign agents to specific Matrix rooms
- **Real-time Updates**: File watcher detects changes to config.yaml

## Quick Start

### Backend Setup

1. Navigate to the backend directory:
```bash
cd widget/backend
```

2. Create a virtual environment and install dependencies:
```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

3. Start the backend server:
```bash
python src/main.py
```

The backend will run on http://localhost:8001

### Frontend Setup

1. Navigate to the frontend directory:
```bash
cd widget/frontend
```

2. Install dependencies:
```bash
npm install
```

3. Start the development server:
```bash
npm run dev
```

The widget will be available at http://localhost:3000

## Using the Widget in Matrix

1. Add the widget to your Matrix room using Element or another compatible client
2. Use the widget URL: `http://localhost:3000` (or your deployed URL)
3. The widget will load with your current agent configuration

## Architecture

### Frontend
- **React** with TypeScript for the UI
- **Zustand** for state management
- **TanStack Query** for API communication
- **Tailwind CSS** for styling
- **Vite** for fast development

### Backend
- **FastAPI** for the REST API
- **PyYAML** for config file handling
- **Watchdog** for file monitoring
- Two-way sync between UI and config.yaml

## Development

### Adding New Features

1. **Frontend Components**: Add new components in `frontend/src/components/`
2. **API Endpoints**: Add new endpoints in `backend/src/main.py`
3. **State Management**: Update the Zustand store in `frontend/src/store/configStore.ts`

### Testing

Frontend:
```bash
npm run type-check  # TypeScript checking
npm run lint        # ESLint
```

Backend:
```bash
python -m pytest    # Run tests (when implemented)
```

## Configuration

The widget reads and writes to the `config.yaml` file in the MindRoom root directory. Changes are synchronized in real-time.

## Security Notes

- API keys should be encrypted before storage (TODO: implement encryption)
- Use HTTPS in production
- Implement proper authentication for the widget

## Future Enhancements

- [ ] API key encryption
- [ ] Model connection testing
- [ ] Import/export configurations
- [ ] Backup and restore
- [ ] Advanced validation
- [ ] Multi-user support
