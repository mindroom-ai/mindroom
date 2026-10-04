---
icon: lucide/bot
---

# MindRoom

AI agents that live in Matrix and work everywhere via bridges.

<video controls playsinline preload="metadata" aria-label="Tour of MindRoom: approvals, a team thread, code, documents, and the browser" style="width: 100%">
  <source src="https://github.com/user-attachments/assets/d24be091-deb0-4be0-8346-3faa6b1e40c4#t=0.1" type="video/mp4" media="(prefers-color-scheme: dark)">
  <source src="https://github.com/user-attachments/assets/7c51fefe-a5d5-460b-ad97-d7886839af3b#t=0.1" type="video/mp4">
</video>

See the [Showcase](showcase.md) for more recordings.

## What is MindRoom?

MindRoom is an AI agent orchestration system with Matrix integration. It provides:

- **Multi-agent collaboration** - Configure multiple specialized agents that can work together
- **Matrix-native** - Agents live in Matrix rooms and respond to messages
- **Persistent memory** - Agent and team-scoped memory that persists across conversations
- **100+ tool integrations** - Connect to external services like GitHub, Slack, Gmail, and more
- **Hot-reload configuration** - Update `config.yaml` and agents restart automatically
- **Scheduled tasks** - Schedule agents to run at specific times with cron expressions or natural language
- **Voice messages** - Speech-to-text transcription with mention normalization and light ASR cleanup
- **Image analysis** - Pass images to vision-capable AI models for analysis
- **Matrix desktop bridge** - Observe or locally lease control of a computer, read selected folders, and run locally approved shell commands without opening inbound ports
- **Authorization** - Fine-grained access control for users and rooms

> [!TIP]
> **Matrix is the backbone** - MindRoom agents communicate through the Matrix protocol, which means they can be bridged to Discord, Slack, Telegram, and other platforms.

## Quick Start

See [Install & First Run](getting-started.md#recommended-hosted-matrix-local-mindroom-uvx-only).

## Features

| Feature | Description |
|---------|-------------|
| **Agents** | Single-specialty actors with specific tools and instructions |
| **Teams** | Collaborative bundles of agents (coordinate or collaborate modes) |
| **Router** | Built-in traffic director that routes messages to the right agent |
| **Memory** | Pluggable Mem0/ChromaDB and Markdown-file backends with agent and team scopes |
| **Knowledge Bases** | File-backed semantic RAG or files-only access with per-agent base assignment |
| **Tools** | 100+ integrations for external services |
| **Skills** | OpenClaw-compatible skills system for extended agent capabilities |
| **Scheduling** | Schedule tasks with cron expressions or natural language |
| **Voice** | Speech-to-text transcription for voice messages |
| **Images** | Pass user-sent images to vision-capable AI models |
| **Matrix Desktop Bridge** | Observe or locally lease control of a computer, read selected folders, and run locally approved shell commands over pinned Matrix E2EE without opening inbound ports |
| **File & Video Attachments** | Context-scoped file and video handling with attachment IDs |
| **Interactive Q&A** | Clickable multiple-choice questions via Matrix reactions |
| **Authorization** | Fine-grained user and room access control |
| **OpenAI-Compatible API** | Use agents from LibreChat, Open WebUI, or any OpenAI client |
| **Streaming** | Progressive message edits with presence-based gating and tool-call markers |
| **Chat Commands** | Built-in `!schedule <task>`, `!list_schedules`, `!cancel_schedule <id>`, `!edit_schedule <id> <task>`, `!desktop [setup\|status\|confirm\|rotate\|disconnect]`, `!mode <agent> minimal\|standard\|show\|reset` (minimal runs Bash where the agent's shell runs), `!model [name\|list\|reset]`, `!room_model [name\|list\|reset]` (set/reset require a room admin), `!thread_mode [room\|thread\|reset\|show]`, `!encrypt [confirm]`, `!e2ee`, `!help [topic]`, admin `!reload-plugins`, opt-in admin `!config <operation>`, and `!hi`; commands are normally handled by the router, while a Desktop-enabled agent can handle `!desktop` directly in a room containing only it and the requester |
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
- [Dashboard](dashboard.md) - Web UI for configuration
- [OpenAI-Compatible API](openai-api.md) - Use agents from any OpenAI-compatible client
- [Tools](tools/index.md) - Available tool integrations
- [Matrix Desktop Bridge](tools/desktop.md) - Securely observe or locally lease desktop and signed-in browser control, read selected folders, and run locally approved shell commands over Matrix
- [OpenClaw Import](openclaw.md) - Reuse OpenClaw workspace files in MindRoom
- [MCP](mcp.md) - Configure native MCP client servers and expose their tools to agents
- [Skills](skills.md) - OpenClaw-compatible skills system
- [Plugins](plugins.md) - Extend with custom tools, OAuth providers, and skills
- [OAuth Framework](oauth-framework.md) - Build scoped OAuth-backed tool integrations
- [Knowledge Bases](knowledge.md) - Configure semantic indexing or files-only knowledge access
- [Memory System](memory.md) - How agent memory works
- [Scheduling](scheduling.md) - Schedule tasks with cron or natural language
- [External Triggers](external-triggers.md) - Wake agents from signed watcher events
- [Agent Callbacks](external-triggers.md#agent-callbacks) - One-shot completion callbacks for spawned sub-agents
- [Voice Messages](voice.md) - Voice message transcription
- [Image Messages](images.md) - Image analysis with vision models
- [File & Video Attachments](attachments.md) - Context-scoped file and video handling
- [Streaming Responses](streaming.md) - Progressive message edits with presence-based gating
- [Chat Commands](chat-commands.md) - Built-in `!schedule <task>`, `!list_schedules`, `!cancel_schedule <id>`, `!edit_schedule <id> <task>`, `!desktop [setup|status|confirm|rotate|disconnect]`, `!mode <agent> minimal|standard|show|reset` (minimal runs Bash where the agent's shell runs), `!model [name|list|reset]`, `!room_model [name|list|reset]` (set/reset require a room admin), `!thread_mode [room|thread|reset|show]`, `!encrypt [confirm]`, `!e2ee`, `!help [topic]`, admin `!reload-plugins`, opt-in admin `!config <operation>`, and `!hi` commands
- [Interactive Q&A](interactive.md) - Clickable multiple-choice questions via Matrix reactions
- [Authorization](authorization.md) - User and room access control
- [Matrix Space](rooms.md#matrix-space) - Optional root Matrix Space for grouping managed rooms
- [Architecture](architecture/index.md) - How it works under the hood
- [Deployment](deployment/index.md) - Docker and Kubernetes deployment
- [Bridges](deployment/bridges/index.md) - Connect Telegram, Slack, and other platforms to Matrix
- [Sandbox Proxy](deployment/sandbox-proxy.md) - Isolate code-execution tools in a sandbox
- [Google Services OAuth](deployment/google-services-oauth.md) - Custom admin OAuth setup for Gmail/Calendar/Drive/Docs/Sheets/Tasks
- [Google Services OAuth (Local Install)](deployment/google-services-user-oauth.md) - Connect Google locally without Cloud setup
- [CLI Reference](cli.md) - Command-line interface
- [Support](support.md) - Contact and troubleshooting help
- [Privacy Policy](privacy.md) - Privacy and data handling information
- [Terms of Service](terms.md) - Terms for using MindRoom services and clients

## License

- **Repository (except `saas-platform/`)**: Apache License 2.0
- **SaaS Platform** (`saas-platform/`): Business Source License 1.1 (converts to Apache 2.0 on 2030-02-06)
