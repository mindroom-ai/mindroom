---
icon: lucide/layout-dashboard
---

# Web Dashboard

MindRoom includes a web dashboard for configuring agents, teams, rooms, and integrations without editing YAML files. Changes are synchronized to `config.yaml` in real-time.

## Accessing the Dashboard

**Standalone Mode:**

```bash
mindroom run              # Start the backend
cd frontend && bun run dev  # Start the frontend (in another terminal)
```

The dashboard will be available at `http://localhost:3003`.

**SaaS Platform:** Access your dashboard at `https://<instance-id>.mindroom.chat`

## Dashboard Tabs

### Dashboard (Overview)

The main dashboard shows system stats and monitoring:

- **Stats cards** - Agents (with status breakdown), rooms, teams, models, and voice status
- **Network graph** - Visual representation of agent-room-team relationships (desktop only)
- **Search and filter** - Filter by agents, rooms, or teams
- **Export Config** - Download configuration as JSON

### Agents

Configure AI agents:

- **Display name** and **Role description**
- **Model** - Select from configured models
- **Tools** - Organized into configured tools (green badge) and default tools (no config needed)
- **Instructions** - Custom behavior instructions
- **Rooms** - Where the agent operates
- **Learning** - Enable or disable Agno Learning per agent (enabled by default)
- **Learning mode** - Choose `always` (automatic extraction) or `agentic` (tool-driven)
- **History runs** - Conversation turns to include as context (1-20)

### Teams

Configure multi-agent collaboration:

- **Display name** and **Team purpose**
- **Collaboration mode** - Coordinate (sequential) or Collaborate (parallel)
- **Team model** - Optional model override
- **Team members** and **Team rooms**

### Rooms

Manage Matrix room configuration:

- **Display name** and **Description**
- **Room model** - Optional model override
- **Agents in room** - Select which agents have access

### External Rooms

View and manage rooms that agents have joined but are not in the configuration:

- **Per-agent view** with room names and IDs
- **Bulk selection** and **Leave rooms** functionality
- **Open in Matrix** - Link to view in your Matrix client

### Models & API Keys

Configure AI model providers:

- **Add/edit models** with provider, model ID, host URL, and advanced settings
- **Provider filter** to show models by provider
- **Test connection** to verify model accessibility
- **Provider API keys** section for configuring credentials

**Runtime-supported providers:** OpenAI, Anthropic, Google Gemini (`google`/`gemini`), Ollama, OpenRouter, Groq, DeepSeek, Cerebras

### Memory

Configure the embedder for agent memory:

- **Provider** - Ollama (local), OpenAI, HuggingFace, or Sentence Transformers
- **Model** - Provider-specific embedding models
- **Host URL** - For Ollama provider

### Knowledge

Manage file-backed RAG knowledge bases:

- **Create/edit/delete knowledge bases** with `path` and `watch` settings
- **Upload and remove files** per knowledge base
- **Reindex** a knowledge base on demand
- **Track index status** (`file_count` and `indexed_count`)
- **Assign agents** to a specific knowledge base from the Agents tab

Git-backed knowledge bases are supported, but Git settings are currently configured in `config.yaml` (`knowledge_bases.<id>.git`), not via dedicated dashboard controls yet.

- The dashboard preserves existing `git` settings when you edit `path`/`watch`.
- `/api/knowledge/bases/{base_id}/files` reflects the manager's filtered file set (for example `include_patterns`/`exclude_patterns`).

### Credentials

Manage service credentials directly from the dashboard:

- **List configured credential services** from `CredentialsManager`
- **Create/select service names** (for example `github_private` or `model:sonnet`)
- **Edit raw JSON credential payloads** and save via `/api/credentials/{service}`
- **Test credentials existence** using `/api/credentials/{service}/test`
- **Delete credential sets** using `/api/credentials/{service}`

### Voice

Configure voice message handling:

- **Enable/disable** voice message support
- **Speech-to-Text** - OpenAI Whisper or self-hosted
- **Command Intelligence** - Model selection for command recognition

### Integrations

Connect external services to enable agent capabilities:

- **Categories** - Email & Calendar, Communication, Shopping, Entertainment, Social, Development, Research, Smart Home, Information
- **Search and filter** by status (Available, Unconfigured, Configured, Coming Soon)
- **OAuth flows** for Google, Spotify, Home Assistant, etc.

## Features

### Real-time Sync

The sync status indicator in the header shows:

- **Synced** - All changes saved
- **Syncing...** - Save in progress
- **Sync Error** - Sync failed
- **Disconnected** - Lost connection to backend

### Theme and Responsive Design

Toggle between dark and light themes. The dashboard adapts to desktop and mobile devices.

## API Endpoints

The dashboard communicates with the backend API at `/api/`:

### Configuration

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/config/load` | Fetch current configuration |
| PUT | `/api/config/save` | Save full configuration |
| GET | `/api/config/agents` | List all agents |
| POST | `/api/config/agents` | Create new agent |
| PUT | `/api/config/agents/{id}` | Update agent |
| DELETE | `/api/config/agents/{id}` | Delete agent |
| GET | `/api/config/teams` | List all teams |
| POST | `/api/config/teams` | Create new team |
| PUT | `/api/config/teams/{id}` | Update team |
| DELETE | `/api/config/teams/{id}` | Delete team |
| GET | `/api/config/models` | List model configurations |
| PUT | `/api/config/models/{id}` | Update model configuration |
| GET | `/api/config/room-models` | Get room model overrides |
| PUT | `/api/config/room-models` | Update room model overrides |

### Credentials

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/credentials/list` | List services with credentials |
| GET | `/api/credentials/{service}/status` | Get credential status |
| GET | `/api/credentials/{service}` | Get credentials for editing |
| POST | `/api/credentials/{service}` | Set credentials |
| POST | `/api/credentials/{service}/api-key` | Set API key |
| GET | `/api/credentials/{service}/api-key` | Get masked API key |
| POST | `/api/credentials/{service}/test` | Test credentials validity |
| DELETE | `/api/credentials/{service}` | Delete credentials |

### Knowledge

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/knowledge/bases` | List configured knowledge bases |
| GET | `/api/knowledge/bases/{base_id}/files` | List files in a knowledge base |
| POST | `/api/knowledge/bases/{base_id}/upload` | Upload one or more files |
| DELETE | `/api/knowledge/bases/{base_id}/files/{path}` | Delete a file from disk and index |
| GET | `/api/knowledge/bases/{base_id}/status` | Get indexing status |
| POST | `/api/knowledge/bases/{base_id}/reindex` | Rebuild the index for a base |

### Tools & Matrix

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/tools` | List available tools |
| GET | `/api/rooms` | List configured rooms |
| GET | `/api/matrix/agents/rooms` | Get all agents' room memberships |
| GET | `/api/matrix/agents/{id}/rooms` | Get specific agent's rooms |
| POST | `/api/matrix/rooms/leave` | Leave a single room |
| POST | `/api/matrix/rooms/leave-bulk` | Leave multiple rooms |
| POST | `/api/test/model` | Test model connection |
