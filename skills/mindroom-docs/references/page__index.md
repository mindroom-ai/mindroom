# MindRoom

AI agents that live in Matrix and work everywhere via bridges.

## What is MindRoom?

MindRoom is an AI agent orchestration system with Matrix integration. It provides:

- **Multi-agent collaboration** - Configure multiple specialized agents that can work together
- **Matrix-native** - Agents live in Matrix rooms and respond to messages
- **Persistent memory** - Agent and team-scoped memory that persists across conversations
- **100+ tool integrations** - Connect to external services like GitHub, Slack, Gmail, and more
- **Hot-reload configuration** - Update `config.yaml` and agents restart automatically
- **Scheduled tasks** - Schedule agents to run at specific times with cron expressions or natural language
- **Voice messages** - Speech-to-text transcription with intelligent command recognition
- **Image analysis** - Pass images to vision-capable AI models for analysis
- **Matrix desktop bridge** - Observe or locally lease control of a computer without opening inbound ports
- **Authorization** - Fine-grained access control for users and rooms

> [!TIP]
> **Matrix is the backbone** - MindRoom agents communicate through the Matrix protocol, which means they can be bridged to Discord, Slack, Telegram, and other platforms.

## Quick Start

### Recommended: Hosted Matrix + Local MindRoom (`uvx` only)

You only run MindRoom locally; the Matrix homeserver is hosted at `mindroom.chat` and the chat UI at `chat.mindroom.chat`.
Watch the 2-minute setup video:

[![MindRoom: installing and talking to my first AI agent in 2 minutes](https://img.youtube.com/vi/jR3xLUxyWhg/maxresdefault.jpg)](https://youtu.be/jR3xLUxyWhg)

**Prerequisite:** Install [uv](https://docs.astral.sh/uv/getting-started/installation/).

```bash
# Create ~/.mindroom/config.yaml and ~/.mindroom/.env with hosted defaults
uvx mindroom config init

# Add model auth (e.g. OPENAI_API_KEY or ANTHROPIC_API_KEY)
$EDITOR ~/.mindroom/.env

# Generate pair code in https://chat.mindroom.chat:
# Settings -> Local MindRoom -> Generate Pair Code
uvx mindroom connect --pair-code ABCD-EFGH

# Start MindRoom
uvx mindroom run
```

See [Getting Started](https://docs.mindroom.chat/getting-started/) for the full walkthrough and [Hosted Matrix Deployment](https://docs.mindroom.chat/deployment/hosted-matrix/) for architecture details.

### Preferred alternative: NixOS LXC container (agent-controlled machine)

Use this when you want to give a MindRoom agent full freedom over its own virtual machine while you, from the host, control precisely what it can see.
A standalone NixOS flake provisions the virtual machine — an Incus LXC system container running NixOS — with the full MindRoom stack (MindRoom, Tuwunel Matrix homeserver, MindRoom Chat, Element, Caddy) plus Docker and secrets wiring, so the agent can rebuild and manage the persistent virtual machine it runs on — unlike the mostly stateless Docker Compose stack below — without ever touching the host.
It is slightly harder to set up by hand, but asking a coding agent such as Codex or Claude Code to do it is trivial: the repo ships machine-oriented instructions in `AGENTS.md`.
See [mindroom-ai/lxc-nixos](https://github.com/mindroom-ai/lxc-nixos) for the full setup.

### Alternative: Full Stack Docker Compose (bundled dashboard + Matrix + MindRoom client)

Use this when you want everything local: the bundled MindRoom dashboard, Matrix homeserver, and a Matrix client in one stack.

**Prereqs:** Docker + Docker Compose.

```bash
git clone https://github.com/mindroom-ai/mindroom-stack
cd mindroom-stack
cp .env.example .env
$EDITOR .env  # add at least one AI provider key

docker compose up -d
```

Open:

- MindRoom UI: http://localhost:8765
- MindRoom client: http://localhost:8080
- Matrix homeserver: http://localhost:8008

The stack uses published `mindroom`, `mindroom-chat`, and `mindroom-tuwunel` images by default.

If you access the stack from another device, set `CLIENT_HOMESERVER_URL=http://<host-ip>:8008` in `.env` before starting it.

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
    id: gpt-5.6

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

For local development with a host-installed backend plus Dockerized Synapse + MindRoom Chat (Linux/macOS), you can bootstrap the local stack with:

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
| **Memory** | Mem0-inspired memory system with agent and team scopes |
| **Knowledge Bases** | File-backed semantic RAG or files-only access with per-agent base assignment |
| **Tools** | 100+ integrations for external services |
| **Skills** | OpenClaw-compatible skills system for extended agent capabilities |
| **Scheduling** | Schedule tasks with cron expressions or natural language |
| **Voice** | Speech-to-text transcription for voice messages |
| **Images** | Pass user-sent images to vision-capable AI models |
| **Matrix Desktop Bridge** | Observe or locally lease control of a computer over pinned Matrix E2EE without opening inbound ports |
| **File & Video Attachments** | Context-scoped file and video handling with attachment IDs |
| **Cultures** | Shared evolving principles across groups of agents |
| **Interactive Q&A** | Clickable multiple-choice questions via Matrix reactions |
| **Authorization** | Fine-grained user and room access control |
| **OpenAI-Compatible API** | Use agents from LibreChat, Open WebUI, or any OpenAI client |
| **Streaming** | Progressive message edits with presence-based gating and tool-call markers |
| **Chat Commands** | Built-in `!schedule <task>`, `!list_schedules`, `!cancel_schedule <id>`, `!edit_schedule <id> <task>`, `!model [name\|list\|reset]`, `!thread_mode [room\|thread\|reset\|show]`, `!encrypt [confirm]`, `!e2ee`, `!help [topic]`, admin `!reload-plugins`, opt-in admin `!config <operation>`, and `!hi` commands handled by the router |
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

- [Getting Started](https://docs.mindroom.chat/getting-started/) - Installation and first steps
- [Hosted Matrix Deployment](https://docs.mindroom.chat/deployment/hosted-matrix/) - Run only `uvx mindroom` locally against hosted Matrix
- [Configuration](https://docs.mindroom.chat/configuration/) - All configuration options
- [Cultures](https://docs.mindroom.chat/configuration/cultures/) - Configure shared agent cultures
- [Dashboard](https://docs.mindroom.chat/dashboard/) - Web UI for configuration
- [OpenAI-Compatible API](https://docs.mindroom.chat/openai-api/) - Use agents from any OpenAI-compatible client
- [Tools](https://docs.mindroom.chat/tools/) - Available tool integrations
- [OpenClaw Import](https://docs.mindroom.chat/openclaw/) - Reuse OpenClaw workspace files in MindRoom
- [MCP](https://docs.mindroom.chat/mcp/) - Configure native MCP client servers and expose their tools to agents
- [Skills](https://docs.mindroom.chat/skills/) - OpenClaw-compatible skills system
- [Plugins](https://docs.mindroom.chat/plugins/) - Extend with custom tools, OAuth providers, and skills
- [OAuth Framework](https://docs.mindroom.chat/oauth-framework/) - Build scoped OAuth-backed tool integrations
- [Knowledge Bases](https://docs.mindroom.chat/knowledge/) - Configure semantic indexing or files-only knowledge access
- [Memory System](https://docs.mindroom.chat/memory/) - How agent memory works
- [Scheduling](https://docs.mindroom.chat/scheduling/) - Schedule tasks with cron or natural language
- [External Triggers](https://docs.mindroom.chat/external-triggers/) - Wake agents from signed watcher events
- [Agent Callbacks](https://docs.mindroom.chat/agent-callbacks/) - One-shot completion callbacks for spawned sub-agents
- [Voice Messages](https://docs.mindroom.chat/voice/) - Voice message transcription
- [Image Messages](https://docs.mindroom.chat/images/) - Image analysis with vision models
- [File & Video Attachments](https://docs.mindroom.chat/attachments/) - Context-scoped file and video handling
- [Streaming Responses](https://docs.mindroom.chat/streaming/) - Progressive message edits with presence-based gating
- [Chat Commands](https://docs.mindroom.chat/chat-commands/) - Built-in `!schedule <task>`, `!list_schedules`, `!cancel_schedule <id>`, `!edit_schedule <id> <task>`, `!model [name|list|reset]`, `!thread_mode [room|thread|reset|show]`, `!encrypt [confirm]`, `!e2ee`, `!help [topic]`, admin `!reload-plugins`, opt-in admin `!config <operation>`, and `!hi` commands
- [Interactive Q&A](https://docs.mindroom.chat/interactive/) - Clickable multiple-choice questions via Matrix reactions
- [Authorization](https://docs.mindroom.chat/authorization/) - User and room access control
- [Matrix Space](https://docs.mindroom.chat/matrix-space/) - Optional root Matrix Space for grouping managed rooms
- [Architecture](https://docs.mindroom.chat/architecture/) - How it works under the hood
- [Deployment](https://docs.mindroom.chat/deployment/) - Docker and Kubernetes deployment
- [Bridges](https://docs.mindroom.chat/deployment/bridges/) - Connect Telegram, Slack, and other platforms to Matrix
- [Sandbox Proxy](https://docs.mindroom.chat/deployment/sandbox-proxy/) - Isolate code-execution tools in a sandbox
- [Google Services OAuth](https://docs.mindroom.chat/deployment/google-services-oauth/) - Custom admin OAuth setup for Gmail/Calendar/Drive/Sheets
- [Google Services OAuth (Local Install)](https://docs.mindroom.chat/deployment/google-services-user-oauth/) - Connect Google locally without Cloud setup
- [CLI Reference](https://docs.mindroom.chat/cli/) - Command-line interface
- [Support](https://docs.mindroom.chat/support/) - Contact and troubleshooting help
- [Privacy Policy](https://docs.mindroom.chat/privacy/) - Privacy and data handling information
- [Terms of Service](https://docs.mindroom.chat/terms/) - Terms for using MindRoom services and clients

## License

- **Repository (except `saas-platform/`)**: Apache License 2.0
- **SaaS Platform** (`saas-platform/`): Business Source License 1.1 (converts to Apache 2.0 on 2030-02-06)
