# MindRoom

AI agents that live in Matrix and work everywhere via bridges.

<video controls playsinline preload="metadata" aria-label="Tour of MindRoom: approvals, a team thread, code, documents, and the browser" style="width: 100%">
  <source src="https://github.com/user-attachments/assets/d24be091-deb0-4be0-8346-3faa6b1e40c4#t=0.1" type="video/mp4" media="(prefers-color-scheme: dark)">
  <source src="https://github.com/user-attachments/assets/7c51fefe-a5d5-460b-ad97-d7886839af3b#t=0.1" type="video/mp4">
</video>

See the [Showcase](https://docs.mindroom.chat/showcase/) for more recordings.

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

See [Install & First Run](https://docs.mindroom.chat/getting-started/#recommended-hosted-matrix-local-mindroom-uvx-only).

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

- [Getting Started](https://docs.mindroom.chat/getting-started/) - Installation and first steps
- [Hosted Matrix Deployment](https://docs.mindroom.chat/deployment/hosted-matrix/) - Run only `uvx mindroom` locally against hosted Matrix
- [Configuration](https://docs.mindroom.chat/configuration/) - All configuration options
- [Dashboard](https://docs.mindroom.chat/dashboard/) - Web UI for configuration
- [OpenAI-Compatible API](https://docs.mindroom.chat/openai-api/) - Use agents from any OpenAI-compatible client
- [Tools](https://docs.mindroom.chat/tools/) - Available tool integrations
- [Matrix Desktop Bridge](https://docs.mindroom.chat/tools/desktop/) - Securely observe or locally lease desktop and signed-in browser control, read selected folders, and run locally approved shell commands over Matrix
- [OpenClaw Import](https://docs.mindroom.chat/openclaw/) - Reuse OpenClaw workspace files in MindRoom
- [MCP](https://docs.mindroom.chat/mcp/) - Configure native MCP client servers and expose their tools to agents
- [Skills](https://docs.mindroom.chat/skills/) - OpenClaw-compatible skills system
- [Plugins](https://docs.mindroom.chat/plugins/) - Extend with custom tools, OAuth providers, and skills
- [OAuth Framework](https://docs.mindroom.chat/oauth-framework/) - Build scoped OAuth-backed tool integrations
- [Knowledge Bases](https://docs.mindroom.chat/knowledge/) - Configure semantic indexing or files-only knowledge access
- [Memory System](https://docs.mindroom.chat/memory/) - How agent memory works
- [Scheduling](https://docs.mindroom.chat/scheduling/) - Schedule tasks with cron or natural language
- [External Triggers](https://docs.mindroom.chat/external-triggers/) - Wake agents from signed watcher events
- [Agent Callbacks](https://docs.mindroom.chat/external-triggers/#agent-callbacks) - One-shot completion callbacks for spawned sub-agents
- [Voice Messages](https://docs.mindroom.chat/voice/) - Voice message transcription
- [Image Messages](https://docs.mindroom.chat/images/) - Image analysis with vision models
- [File & Video Attachments](https://docs.mindroom.chat/attachments/) - Context-scoped file and video handling
- [Streaming Responses](https://docs.mindroom.chat/streaming/) - Progressive message edits with presence-based gating
- [Chat Commands](https://docs.mindroom.chat/chat-commands/) - Built-in `!schedule <task>`, `!list_schedules`, `!cancel_schedule <id>`, `!edit_schedule <id> <task>`, `!desktop [setup|status|confirm|rotate|disconnect]`, `!mode <agent> minimal|standard|show|reset` (minimal runs Bash where the agent's shell runs), `!model [name|list|reset]`, `!room_model [name|list|reset]` (set/reset require a room admin), `!thread_mode [room|thread|reset|show]`, `!encrypt [confirm]`, `!e2ee`, `!help [topic]`, admin `!reload-plugins`, opt-in admin `!config <operation>`, and `!hi` commands
- [Interactive Q&A](https://docs.mindroom.chat/interactive/) - Clickable multiple-choice questions via Matrix reactions
- [Authorization](https://docs.mindroom.chat/authorization/) - User and room access control
- [Matrix Space](https://docs.mindroom.chat/rooms/#matrix-space) - Optional root Matrix Space for grouping managed rooms
- [Architecture](https://docs.mindroom.chat/architecture/) - How it works under the hood
- [Deployment](https://docs.mindroom.chat/deployment/) - Docker and Kubernetes deployment
- [Bridges](https://docs.mindroom.chat/deployment/bridges/) - Connect Telegram, Slack, and other platforms to Matrix
- [Sandbox Proxy](https://docs.mindroom.chat/deployment/sandbox-proxy/) - Isolate code-execution tools in a sandbox
- [Google Services OAuth](https://docs.mindroom.chat/deployment/google-services-oauth/) - Custom admin OAuth setup for Gmail/Calendar/Drive/Docs/Sheets/Tasks
- [Google Services OAuth (Local Install)](https://docs.mindroom.chat/deployment/google-services-user-oauth/) - Connect Google locally without Cloud setup
- [CLI Reference](https://docs.mindroom.chat/cli/) - Command-line interface
- [Support](https://docs.mindroom.chat/support/) - Contact and troubleshooting help
- [Privacy Policy](https://docs.mindroom.chat/privacy/) - Privacy and data handling information
- [Terms of Service](https://docs.mindroom.chat/terms/) - Terms for using MindRoom services and clients

## License

- **Repository (except `saas-platform/`)**: Apache License 2.0
- **SaaS Platform** (`saas-platform/`): Business Source License 1.1 (converts to Apache 2.0 on 2030-02-06)
