---
icon: lucide/layout-dashboard
---

# Web Dashboard

MindRoom includes a full-featured web dashboard for configuring agents, teams, rooms, and integrations without editing YAML files.

## Overview

The dashboard is a React application that provides a visual interface for all MindRoom configuration. Changes made in the dashboard are synchronized to `config.yaml` in real-time.

## Accessing the Dashboard

### Standalone Mode

When running MindRoom locally:

```bash
# Start the backend
mindroom run

# In another terminal, start the frontend
cd frontend && bun run dev
```

The dashboard will be available at `http://localhost:3003`.

### SaaS Platform

For hosted instances, access your dashboard at:
```
https://<instance-id>.mindroom.chat
```

## Dashboard Tabs

### Dashboard (Overview)

The main dashboard shows:

- **System stats** - Total agents, rooms, teams, and models
- **Agent status** - Online/busy/idle/offline indicators
- **Network graph** - Visual representation of agent-room relationships
- **Quick filters** - Search and filter by type

### Agents

Create and configure AI agents with a visual editor:

- **Display name** - Human-readable name shown in Matrix
- **Role** - Description guiding agent behavior
- **Model** - Select from configured AI models
- **Tools** - Enable/disable tools with a checkbox list
- **Skills** - Add skills for specialized capabilities
- **Instructions** - Custom behavior instructions
- **Rooms** - Select which rooms the agent joins

### Teams

Configure multi-agent collaboration:

- **Team members** - Select agents to include
- **Collaboration mode**:
  - **Coordinate** - Lead agent orchestrates work
  - **Collaborate** - All agents respond in parallel
- **Rooms** - Where the team responds

### Rooms

Manage Matrix room configuration:

- **Room name** - Display name for the room
- **Agents** - Which agents are members
- **Model override** - Use a different model for this room
- **Authorization** - Per-room access control

### External Rooms

View and manage rooms that exist on Matrix but aren't configured:

- **Discover** - See unconfigured rooms
- **Add to config** - Bring rooms under management
- **Ignore** - Hide rooms you don't want to configure

### Models & API Keys

Configure AI model providers:

- **Add models** - Define new model configurations
- **Provider logos** - Visual identification of providers
- **API keys** - Securely store provider credentials
- **Test connection** - Verify model accessibility

Supported providers:
- Anthropic (Claude)
- OpenAI (GPT)
- Google (Gemini)
- Ollama (local models)
- Groq
- Cerebras

### Memory

Configure the persistent memory system:

- **Embedder** - Choose embedding model for semantic search
- **Provider** - OpenAI, HuggingFace, or self-hosted
- **LLM** - Model for memory operations

### Voice

Configure voice message handling:

- **Enable/disable** - Toggle voice processing
- **STT provider** - Speech-to-text service
- **Model** - Whisper or compatible
- **Confidence threshold** - Command recognition sensitivity

### Integrations

Manage 80+ tool integrations:

- **Browse integrations** - Searchable list by category
- **Configure** - Set up API keys and options
- **OAuth flows** - Connect Google, Spotify, etc.
- **Test** - Verify integration connectivity

## Features

### Real-time Sync

Changes in the dashboard are immediately reflected in `config.yaml`. The sync status indicator shows:

- **Synced** - All changes saved
- **Syncing** - Save in progress
- **Error** - Sync failed (with retry option)

### Dark/Light Theme

Toggle between dark and light themes using the theme button in the header. The preference is saved locally.

### Responsive Design

The dashboard works on desktop and mobile devices with adaptive layouts.

### Authentication

When running with the SaaS platform, authentication is handled via Supabase JWT tokens. The dashboard will redirect to login if authentication is required.

## Configuration Storage

All dashboard changes are persisted to:

1. **config.yaml** - Primary configuration file
2. **Credentials store** - Secure storage for API keys (`mindroom_data/credentials/`)

## API Backend

The dashboard communicates with the MindRoom backend API at `/api/`. Key endpoints:

- `GET /api/config` - Fetch current configuration
- `PUT /api/config` - Update configuration
- `GET /api/credentials/:service` - Get credential status
- `PUT /api/credentials/:service` - Store credentials
- `GET /api/tools` - List available tools
- `GET /api/integrations` - List integration status
