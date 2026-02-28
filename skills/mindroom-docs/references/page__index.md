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

> [!TIP] **Matrix is the backbone** - MindRoom agents communicate through the Matrix protocol, which means they can be bridged to Discord, Slack, Telegram, and other platforms.

## Quick Start

### Recommended: Full Stack Docker Compose (backend + frontend + Matrix + Element)

**Prereqs:** Docker + Docker Compose.

```
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

```
# Using uv
uv tool install mindroom

# Or using pip
pip install mindroom
```

### Basic Usage (manual)

1. Create a `config.yaml`:

```
agents:
  assistant:
    display_name: Assistant
    role: A helpful AI assistant
    model: default
    rooms: [lobby]

models:
  default:
    provider: anthropic
    id: claude-sonnet-4-5-latest

defaults:
  tools: [scheduler]
  markdown: true
```

1. Set up your environment in `.env`:

```
# Matrix homeserver (must allow open registration)
MATRIX_HOMESERVER=https://matrix.example.com

# AI provider API keys
ANTHROPIC_API_KEY=your_api_key
```

1. Run MindRoom:

```
mindroom run
```

For local development with a host-installed backend plus Dockerized Synapse + Cinny (Linux/macOS), you can bootstrap the local stack with:

```
mindroom local-stack-setup --synapse-dir /path/to/mindroom-stack/local/matrix
mindroom run
```

## Features

| Feature                   | Description                                                       |
| ------------------------- | ----------------------------------------------------------------- |
| **Agents**                | Single-specialty actors with specific tools and instructions      |
| **Teams**                 | Collaborative bundles of agents (coordinate or collaborate modes) |
| **Router**                | Built-in traffic director that routes messages to the right agent |
| **Memory**                | Mem0-inspired memory system with agent, room, and team scopes     |
| **Knowledge Bases**       | File-backed RAG indexing with per-agent base assignment           |
| **Tools**                 | 100+ integrations for external services                           |
| **Skills**                | OpenClaw-compatible skills system for extended agent capabilities |
| **Scheduling**            | Schedule tasks with cron expressions or natural language          |
| **Voice**                 | Speech-to-text transcription for voice messages                   |
| **Images**                | Pass user-sent images to vision-capable AI models                 |
| **Cultures**              | Shared evolving principles across groups of agents                |
| **Authorization**         | Fine-grained user and room access control                         |
| **OpenAI-Compatible API** | Use agents from LibreChat, Open WebUI, or any OpenAI client       |
| **Hot Reload**            | Config changes are detected and agents restart automatically      |

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

- [Getting Started](https://docs.mindroom.chat/getting-started/index.md) - Installation and first steps
- [Hosted Matrix Deployment](https://docs.mindroom.chat/deployment/hosted-matrix/index.md) - Run only `uvx mindroom` locally against hosted Matrix
- [Configuration](https://docs.mindroom.chat/configuration/index.md) - All configuration options
- [Cultures](https://docs.mindroom.chat/configuration/cultures/index.md) - Configure shared agent cultures
- [Dashboard](https://docs.mindroom.chat/dashboard/index.md) - Web UI for configuration
- [OpenAI-Compatible API](https://docs.mindroom.chat/openai-api/index.md) - Use agents from any OpenAI-compatible client
- [Tools](https://docs.mindroom.chat/tools/index.md) - Available tool integrations
- [OpenClaw Import](https://docs.mindroom.chat/openclaw/index.md) - Reuse OpenClaw workspace files in MindRoom
- [MCP (Planned)](https://docs.mindroom.chat/tools/mcp/index.md) - Native MCP status and current plugin workaround
- [Skills](https://docs.mindroom.chat/skills/index.md) - OpenClaw-compatible skills system
- [Plugins](https://docs.mindroom.chat/plugins/index.md) - Extend with custom tools and skills
- [Knowledge Bases](https://docs.mindroom.chat/knowledge/index.md) - Configure RAG-backed document indexing
- [Memory System](https://docs.mindroom.chat/memory/index.md) - How agent memory works
- [Scheduling](https://docs.mindroom.chat/scheduling/index.md) - Schedule tasks with cron or natural language
- [Voice Messages](https://docs.mindroom.chat/voice/index.md) - Voice message transcription
- [Image Messages](https://docs.mindroom.chat/images/index.md) - Image analysis with vision models
- [Authorization](https://docs.mindroom.chat/authorization/index.md) - User and room access control
- [Architecture](https://docs.mindroom.chat/architecture/index.md) - How it works under the hood
- [Deployment](https://docs.mindroom.chat/deployment/index.md) - Docker and Kubernetes deployment
- [Bridges](https://docs.mindroom.chat/deployment/bridges/index.md) - Connect Telegram, Slack, and other platforms to Matrix
- [Sandbox Proxy](https://docs.mindroom.chat/deployment/sandbox-proxy/index.md) - Isolate code-execution tools in a sandbox
- [Google Services OAuth](https://docs.mindroom.chat/deployment/google-services-oauth/index.md) - Admin OAuth setup for Gmail/Calendar/Drive/Sheets
- [Google Services OAuth (Individual)](https://docs.mindroom.chat/deployment/google-services-user-oauth/index.md) - Single-user OAuth setup
- [CLI Reference](https://docs.mindroom.chat/cli/index.md) - Command-line interface

## License

- **Repository (except `saas-platform/`)**: Apache License 2.0
- **SaaS Platform** (`saas-platform/`): Business Source License 1.1 (converts to Apache 2.0 on 2030-02-06)
