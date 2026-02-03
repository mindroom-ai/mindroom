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

- **System stats** - Total agents, rooms, teams, models, and voice status
- **Agent status** - Online/busy/idle/offline indicators (simulated)
- **Network graph** - Visual representation of agent-room-team relationships (desktop only)
- **Search and filter** - Search bar with type filters (agents/rooms/teams toggle)
- **Quick actions** - Clear selection, export configuration as JSON
- **Agent cards** - Clickable list with team memberships and room badges
- **Rooms overview** - Clickable list showing agents and teams per room
- **Details panel** - Shows selected agent or room details

### Agents

Create and configure AI agents with a visual editor:

- **Display name** - Human-readable name shown in Matrix
- **Role description** - Description guiding agent behavior
- **Model** - Select from configured AI models
- **Tools** - Enable/disable tools organized into:
  - **Configured tools** - Tools with credentials already set up
  - **Default tools** - Tools that work without configuration
- **Instructions** - Custom behavior instructions (add/remove entries)
- **Agent rooms** - Select rooms where this agent can operate
- **History runs** - Number of previous conversation turns as context

### Teams

Configure multi-agent collaboration:

- **Display name** - Human-readable name for the team
- **Team purpose** - Description of what the team does
- **Collaboration mode**:
  - **Coordinate** - Agents work sequentially, one after another
  - **Collaborate** - Agents work simultaneously in parallel
- **Team model** - Optional model override for all agents in the team
- **Team members** - Select agents to include
- **Team rooms** - Select rooms where this team can operate

### Rooms

Manage Matrix room configuration:

- **Display name** - Human-readable name for the room
- **Description** - Describe the room's purpose
- **Room model** - Optional model override for agents and teams in this room
- **Agents in room** - Which agents are members (their room list updates automatically)

### External Rooms

View and manage rooms that agents have joined but are not in the configuration:

- **Summary** - Shows total external rooms across all agents
- **Per-agent view** - See which external rooms each agent has joined
- **Bulk selection** - Select/deselect all rooms for an agent
- **Leave rooms** - Remove agents from unconfigured rooms
- **Open in Matrix** - Link to view the room in your Matrix client

### Models & API Keys

Configure AI model providers:

- **Add models** - Define new model configurations with:
  - Configuration name (unique identifier)
  - Provider selection
  - Model ID (provider-specific identifier)
  - Host URL (for Ollama)
  - Advanced settings (JSON for provider-specific parameters)
- **Provider filter** - Filter models by provider
- **Provider logos** - Visual identification of providers
- **Edit/delete models** - Manage existing configurations
- **Test connection** - Verify model accessibility
- **Provider API keys** - Configure API keys for each provider

Supported providers:

- OpenAI (GPT models)
- Anthropic (Claude models)
- Google Gemini
- Ollama (local models)
- OpenRouter
- Groq
- DeepSeek
- Together AI
- Mistral
- Perplexity
- Cohere
- xAI (Grok models)
- Cerebras

### Memory

Configure the embedder for agent memory storage and retrieval:

- **Embedder provider** - Choose where embeddings are computed:
  - Ollama (Local)
  - OpenAI
  - HuggingFace
  - Sentence Transformers
- **Embedding model** - Select the specific model for the provider
- **Host URL** - Configure Ollama server location (for Ollama provider)

### Voice

Configure voice message handling:

- **Enable/disable** - Toggle voice message support
- **Speech-to-Text (STT)**:
  - Provider: OpenAI Whisper (Cloud) or Self-hosted (OpenAI-compatible)
  - Model: Whisper model name
  - API key (optional for OpenAI, uses OPENAI_API_KEY environment variable if not set)
  - Host URL (for self-hosted providers)
- **Command Intelligence** (advanced settings):
  - AI model for processing transcriptions
  - Confidence threshold for command recognition

### Integrations

Manage tool integrations:

- **Browse integrations** - Searchable list organized by category:
  - Email & Calendar
  - Communication
  - Shopping
  - Entertainment
  - Social
  - Development
  - Research
  - Smart Home
  - Information
- **Filter by status** - Show All, Available, Unconfigured, Configured, Coming Soon
- **Configure** - Set up API keys and options via dialogs
- **OAuth flows** - Connect Google, Spotify, Home Assistant, etc.
- **Edit/disconnect** - Manage connected integrations

## Features

### Real-time Sync

Changes in the dashboard are immediately reflected in `config.yaml`. The sync status indicator shows:

- **Synced** - All changes saved
- **Syncing...** - Save in progress
- **Sync Error** - Sync failed
- **Disconnected** - Lost connection to backend

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

### Configuration

- `POST /api/config/load` - Fetch current configuration
- `PUT /api/config/save` - Save full configuration
- `GET /api/config/agents` - List all agents
- `POST /api/config/agents` - Create new agent
- `PUT /api/config/agents/:id` - Update agent
- `DELETE /api/config/agents/:id` - Delete agent
- `GET /api/config/teams` - List all teams
- `POST /api/config/teams` - Create new team
- `PUT /api/config/teams/:id` - Update team
- `DELETE /api/config/teams/:id` - Delete team
- `GET /api/config/models` - List model configurations
- `PUT /api/config/models/:id` - Update model configuration
- `GET /api/config/room-models` - Get room model overrides
- `PUT /api/config/room-models` - Update room model overrides

### Credentials

- `GET /api/credentials/list` - List services with credentials
- `GET /api/credentials/:service/status` - Get credential status
- `GET /api/credentials/:service` - Get credentials for editing
- `POST /api/credentials/:service` - Set credentials
- `POST /api/credentials/:service/api-key` - Set API key
- `GET /api/credentials/:service/api-key` - Get masked API key
- `POST /api/credentials/:service/test` - Test credentials validity
- `DELETE /api/credentials/:service` - Delete credentials

### Tools & Matrix

- `GET /api/tools` - List available tools with status
- `GET /api/rooms` - List configured rooms
- `GET /api/matrix/agents/rooms` - Get all agents' room memberships
- `GET /api/matrix/agents/:id/rooms` - Get specific agent's room memberships
- `POST /api/matrix/rooms/leave` - Leave a single room
- `POST /api/matrix/rooms/leave-bulk` - Leave multiple rooms
- `POST /api/test/model` - Test model connection
