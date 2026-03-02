---
icon: lucide/bot
---

# MindRoom

AI agents that live in Matrix and work everywhere via bridges.

## What is MindRoom?

MindRoom is an AI agent orchestration system with Matrix integration. It provides:

- **Multi-agent collaboration** - Configure multiple specialized agents that can work together
- **Matrix-native** - Agents live in Matrix rooms and respond to messages
- **Persistent memory** - Agent, room, and team-scoped memory that persists across conversations
- **100+ tool integrations** - Connect to external services like GitHub, Slack, Gmail, and more
- **Hot-reload configuration** - Update `config.yaml` and agents restart automatically
- **Scheduled tasks** - Schedule agents to run at specific times with cron expressions or natural language
- **Voice messages** - Speech-to-text transcription with intelligent command recognition
- **Image analysis** - Pass images to vision-capable AI models for analysis
- **Authorization** - Fine-grained access control for users and rooms

> [!TIP]
> **Matrix is the backbone** - MindRoom agents communicate through the Matrix protocol, which means they can be bridged to Discord, Slack, Telegram, and other platforms.

## Quick Start

### Recommended: Full Stack Docker Compose (backend + frontend + Matrix + Element)

**Prereqs:** Docker + Docker Compose.

```bash
git clone https://github.com/mindroom-ai/mindroom-stack
cd mindroom-stack
cp .env.example .env
$EDITOR .env  # add at least one AI provider key

docker compose up -d
```

Open:

- MindRoom UI: http://localhost:3003
- Element: http://localhost:8080
- Matrix homeserver: http://matrix.localhost:8008

### Manual Install (advanced)

Use this if you already have a Matrix homeserver and want to run MindRoom directly.

```bash
# Using uv
uv tool install mindroom

# Or using pip
pip install mindroom
```

### Basic Usage (manual)

1. Create a `config.yaml`:

```yaml
agents:
  assistant:
    display_name: Assistant
    role: A helpful AI assistant
    model: default
    rooms: [lobby]

models:
  default:
    provider: openai
    id: gpt-5.2

defaults:
  tools: [scheduler]
  markdown: true
```

2. Set up your environment in `.env`:

```bash
# Matrix homeserver (must allow open registration)
MATRIX_HOMESERVER=https://matrix.example.com

# AI provider API keys
OPENAI_API_KEY=your_api_key
```

3. Run MindRoom:

```bash
mindroom run
```

For local development with a host-installed backend plus Dockerized Synapse + Cinny
(Linux/macOS), you can bootstrap the local stack with:

```bash
mindroom local-stack-setup --synapse-dir /path/to/mindroom-stack/local/matrix
mindroom run
```

## Features

| Feature | Description |
|---------|-------------|
| **Agents** | Single-specialty actors with specific tools and instructions |
| **Teams** | Collaborative bundles of agents (coordinate or collaborate modes) |
| **Router** | Built-in traffic director that routes messages to the right agent |
| **Memory** | Mem0-inspired memory system with agent, room, and team scopes |
| **Knowledge Bases** | File-backed RAG indexing with per-agent base assignment |
| **Tools** | 100+ integrations for external services |
| **Skills** | OpenClaw-compatible skills system for extended agent capabilities |
| **Scheduling** | Schedule tasks with cron expressions or natural language |
| **Voice** | Speech-to-text transcription for voice messages |
| **Images** | Pass user-sent images to vision-capable AI models |
| **File & Video Attachments** | Context-scoped file and video handling with attachment IDs |
| **Cultures** | Shared evolving principles across groups of agents |
| **Authorization** | Fine-grained user and room access control |
| **OpenAI-Compatible API** | Use agents from LibreChat, Open WebUI, or any OpenAI client |
| **Hot Reload** | Config changes are detected and agents restart automatically |

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                 Matrix Homeserver                    │
└─────────────────────┬───────────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────────┐
│              MultiAgentOrchestrator                  │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐   │
│  │ Router  │ │ Agent 1 │ │ Agent 2 │ │  Team   │   │
│  └─────────┘ └─────────┘ └─────────┘ └─────────┘   │
└─────────────────────────────────────────────────────┘
```

## Documentation

- [Getting Started](getting-started.md) - Installation and first steps
- [Hosted Matrix Deployment](deployment/hosted-matrix.md) - Run only `uvx mindroom` locally against hosted Matrix
- [Configuration](configuration/index.md) - All configuration options
- [Cultures](configuration/cultures.md) - Configure shared agent cultures
- [Dashboard](dashboard.md) - Web UI for configuration
- [OpenAI-Compatible API](openai-api.md) - Use agents from any OpenAI-compatible client
- [Tools](tools/index.md) - Available tool integrations
- [OpenClaw Import](openclaw.md) - Reuse OpenClaw workspace files in MindRoom
- [MCP (Planned)](tools/mcp.md) - Native MCP status and current plugin workaround
- [Skills](skills.md) - OpenClaw-compatible skills system
- [Plugins](plugins.md) - Extend with custom tools and skills
- [Knowledge Bases](knowledge.md) - Configure RAG-backed document indexing
- [Memory System](memory.md) - How agent memory works
- [Scheduling](scheduling.md) - Schedule tasks with cron or natural language
- [Voice Messages](voice.md) - Voice message transcription
- [Image Messages](images.md) - Image analysis with vision models
- [File & Video Attachments](attachments.md) - Context-scoped file and video handling
- [Authorization](authorization.md) - User and room access control
- [Architecture](architecture/index.md) - How it works under the hood
- [Deployment](deployment/index.md) - Docker and Kubernetes deployment
- [Bridges](deployment/bridges/index.md) - Connect Telegram, Slack, and other platforms to Matrix
- [Sandbox Proxy](deployment/sandbox-proxy.md) - Isolate code-execution tools in a sandbox
- [Google Services OAuth](deployment/google-services-oauth.md) - Admin OAuth setup for Gmail/Calendar/Drive/Sheets
- [Google Services OAuth (Individual)](deployment/google-services-user-oauth.md) - Single-user OAuth setup
- [CLI Reference](cli.md) - Command-line interface
- [Support](support.md) - Contact and troubleshooting help
- [Privacy Policy](privacy.md) - Privacy and data handling information
- [Terms of Service](terms.md) - Terms for using MindRoom services and clients

## License

- **Repository (except `saas-platform/`)**: Apache License 2.0
- **SaaS Platform** (`saas-platform/`): Business Source License 1.1 (converts to Apache 2.0 on 2030-02-06)
