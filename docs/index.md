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
- **120+ tool integrations** - Connect to external services like GitHub, Slack, Gmail, and more
- **Hot-reload configuration** - Update `config.yaml` and agents restart automatically
- **Scheduled tasks** - Schedule agents to run at specific times with cron expressions or natural language
- **Voice messages** - Speech-to-text transcription with intelligent command recognition
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
    model: sonnet
    rooms: [lobby]

models:
  sonnet:
    provider: anthropic
    id: claude-sonnet-4-latest

defaults:
  num_history_runs: 5
  markdown: true
```

2. Set up your environment in `.env`:

```bash
# Matrix homeserver (must allow open registration)
MATRIX_HOMESERVER=https://matrix.example.com

# AI provider API keys
ANTHROPIC_API_KEY=your_api_key
```

3. Run MindRoom:

```bash
mindroom run
```

## Features

| Feature | Description |
|---------|-------------|
| **Agents** | Single-specialty actors with specific tools and instructions |
| **Teams** | Collaborative bundles of agents (coordinate or collaborate modes) |
| **Router** | Built-in traffic director that routes messages to the right agent |
| **Memory** | Mem0-inspired memory system with agent, room, and team scopes |
| **Tools** | 120+ integrations for external services |
| **Skills** | OpenClaw-compatible skills system for extended agent capabilities |
| **Scheduling** | Schedule tasks with cron expressions or natural language |
| **Voice** | Speech-to-text transcription for voice messages |
| **Authorization** | Fine-grained user and room access control |
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
- [Configuration](configuration/index.md) - All configuration options
- [Dashboard](dashboard.md) - Web UI for configuration
- [Tools](tools/index.md) - Available tool integrations
- [Skills](skills.md) - OpenClaw-compatible skills system
- [Plugins](plugins.md) - Extend with custom tools and skills
- [Memory System](memory.md) - How agent memory works
- [Scheduling](scheduling.md) - Schedule tasks with cron or natural language
- [Voice Messages](voice.md) - Voice message transcription
- [Authorization](authorization.md) - User and room access control
- [Architecture](architecture/index.md) - How it works under the hood
- [Deployment](deployment/index.md) - Docker and Kubernetes deployment
- [CLI Reference](cli.md) - Command-line interface

## License

MIT
