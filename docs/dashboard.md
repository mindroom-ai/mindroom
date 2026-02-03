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

The main dashboard shows a "System Overview" with real-time monitoring:

- **System stats cards** - Five stat cards showing:
  - Total agents (with online/busy/idle/offline breakdown)
  - Total rooms (with configured count)
  - Total teams (with member count)
  - Total models in configuration
  - Voice status (enabled/disabled with icon)
- **Agent status** - Online/busy/idle/offline indicators (simulated, refreshes every 30 seconds)
- **System Insights / Network graph** - Visual representation of agent-room-team relationships (desktop only, hidden on mobile)
- **Search and filter** - Search bar with type filters (Agents/Rooms/Teams toggle buttons)
- **Quick actions** - Clear Selection button and Export Config button (exports to JSON file)
- **Agent cards** - Clickable list with team memberships, room badges, and status indicators
- **Rooms overview** - Clickable list showing agents, teams, and model overrides per room
- **Details panel** - Shows selected agent or room details including tools, rooms, team memberships

### Agents

Create and configure AI agents with a visual editor:

- **Display name** - Human-readable name for the agent
- **Role description** - Description of the agent's purpose and capabilities
- **Model** - Select from configured AI models (defaults to 'default' model if not specified)
- **Tools** - Enable/disable tools organized into:
  - **Configured tools** - Tools with credentials already set up (shown with green badge)
  - **Default tools** - Tools that work without configuration (shown with secondary badge)
- **Instructions** - Custom behavior instructions (add/remove entries dynamically)
- **Agent rooms** - Select rooms where this agent can operate
- **History runs** - Number of previous conversation turns to include as context (1-20)

### Teams

Configure multi-agent collaboration:

- **Display name** - Human-readable name for the team
- **Team purpose** - Description of the team's purpose and what it does
- **Collaboration mode**:
  - **Coordinate** - Agents work sequentially, one after another (sequential mode)
  - **Collaborate** - Agents work simultaneously in parallel
- **Team model** - Optional model override for all agents in the team (select "Use default model" to inherit)
- **Team members** - Select agents that compose this team (with checkboxes)
- **Team rooms** - Select rooms where this team can operate

### Rooms

Manage Matrix room configuration:

- **Display name** - Human-readable name for the room
- **Description** - Describe the room's purpose
- **Room model** - Optional model override for agents and teams in this room (select "Use default model" to inherit)
- **Agents in room** - Which agents are members (their room list updates automatically when toggled)

### External Rooms

View and manage rooms that agents have joined but are not in the configuration. In the UI, this tab is labeled "External".

- **Summary** - Shows total external rooms across all agents
- **Per-agent view** - See which external rooms each agent has joined
- **Bulk selection** - Select/deselect all rooms for an agent
- **Leave rooms** - Remove agents from unconfigured rooms (supports bulk leave)
- **Open in Matrix** - Link to view the room in your Matrix client
- **Room names** - Displays room names when available, with room IDs shown below
- **Refresh** - Button to reload the room list from Matrix

### Models & API Keys

Configure AI model providers. This tab is labeled "Models & API Keys" in the UI.

- **Add models** - Define new model configurations with:
  - Configuration name (unique identifier used to reference the model)
  - Provider selection (from supported providers list)
  - Model ID (provider-specific identifier, e.g., "gpt-4", "claude-sonnet-4-20250514")
  - Host URL (for Ollama and custom providers)
  - Advanced settings (extra_kwargs JSON for provider-specific parameters)
- **Provider filter** - Filter models by provider
- **Provider logos** - Visual identification of providers using @lobehub/icons
- **Edit/delete models** - Manage existing configurations
- **Test connection** - Verify model accessibility (checks if model is configured)
- **Provider API keys** - Configure API keys for each provider (managed through the ApiKeyConfig component)

Supported providers:

- OpenAI (GPT models)
- Anthropic (Claude models)
- Google Gemini (also accessible as "google" provider)
- Ollama (local models)
- OpenRouter
- Groq
- DeepSeek
- Together AI
- Mistral
- Perplexity
- Cohere
- xAI (Grok models, also accessible as "grok" provider)
- Cerebras

### Memory

Configure the embedder for agent memory storage and retrieval:

- **Embedder provider** - Choose where embeddings are computed:
  - Ollama (Local) - Uses local Ollama server for embeddings
  - OpenAI - Cloud embeddings using OpenAI API
  - HuggingFace - Cloud embeddings using HuggingFace API
  - Sentence Transformers - Local embeddings using sentence-transformers library
- **Embedding model** - Select the specific model for the provider:
  - Ollama: nomic-embed-text, all-minilm, mxbai-embed-large
  - OpenAI: text-embedding-ada-002, text-embedding-3-small, text-embedding-3-large
  - HuggingFace: sentence-transformers/all-MiniLM-L6-v2, sentence-transformers/all-mpnet-base-v2
  - Sentence Transformers: all-MiniLM-L6-v2, all-mpnet-base-v2, multi-qa-MiniLM-L6-cos-v1
- **Host URL** - Configure Ollama server location (for Ollama provider only, default: http://localhost:11434)
- **API key notice** - Shows a note for OpenAI and HuggingFace providers about required environment variables

### Voice

Configure voice message handling. The Voice page shows two cards: Voice Message Support configuration and Voice Features information.

- **Enable/disable** - Toggle voice message support (checkbox in header)
- **Speech-to-Text (STT)**:
  - Provider: OpenAI Whisper (Cloud) or Self-hosted (OpenAI-compatible via "custom" option)
  - Model: Whisper model name (default: whisper-1)
  - API key (optional for OpenAI, uses OPENAI_API_KEY environment variable if not set)
  - Host URL (for self-hosted/custom providers only)
- **Command Intelligence** (advanced settings, hidden by default):
  - AI model for processing transcriptions (selects from configured models)
  - Confidence threshold for command recognition (slider from 0 to 1, default 0.7)
- **Voice Features card** - Lists supported features including automatic transcription, smart command recognition, agent name detection, cloud/self-hosted support, and multi-language support

### Integrations

Manage tool integrations. The integrations page is titled "Service Integrations" and allows connecting external services to enable agent capabilities.

- **Browse integrations** - Searchable list organized by category tabs:
  - All (shows all integrations)
  - Email & Calendar
  - Communication
  - Shopping
  - Entertainment
  - Social
  - Development
  - Research
  - Smart Home
  - Information
- **Search** - Search integrations by name or description
- **Filter by status** - Show All, Available, Unconfigured, Configured, Coming Soon
- **Configure** - Set up API keys and options via enhanced configuration dialogs
- **OAuth flows** - Connect Google, Spotify, Home Assistant, etc.
- **Edit/disconnect** - Manage connected integrations (Edit or Disconnect buttons)
- **Status badges** - Connected (green), Available (amber), Coming Soon

## Features

### Real-time Sync

Changes in the dashboard are immediately reflected in `config.yaml`. The sync status indicator in the header shows:

- **Synced** (green checkmark) - All changes saved
- **Syncing...** (spinning icon) - Save in progress
- **Sync Error** (alert icon) - Sync failed
- **Disconnected** (wifi-off icon) - Lost connection to backend

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
- `GET /api/credentials/:service/api-key` - Get masked API key (includes `has_key` status)
- `POST /api/credentials/:service/test` - Test credentials validity
- `DELETE /api/credentials/:service` - Delete credentials

### Tools & Matrix

- `GET /api/tools` - List available tools with configuration status
- `GET /api/rooms` - List configured rooms (extracted from agent configurations)
- `GET /api/matrix/agents/rooms` - Get all agents' room memberships (configured, joined, unconfigured)
- `GET /api/matrix/agents/:id/rooms` - Get specific agent's room memberships
- `POST /api/matrix/rooms/leave` - Leave a single room (requires `agent_id` and `room_id`)
- `POST /api/matrix/rooms/leave-bulk` - Leave multiple rooms (batch operation)
- `POST /api/test/model` - Test model connection (requires `modelId`)
